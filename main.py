"""Vidpick desktop application."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import time

from PySide6.QtCore import QObject, QSettings, QThread, QSize, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QButtonGroup, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from douyin_parser import DouyinSession, ProfileInfo, ProfileWork, VideoInfo, extract_profile, extract_video
from storage import AssetState, CollectionOptions, SelectedSaveResult, get_asset_state, save_selected_assets


ICON_PATH = Path(__file__).resolve().parent / "assets" / "vidpick-icon.ico"
PASTE_ICON_PATH = Path(__file__).resolve().parent / "assets" / "paste.svg"
SETTINGS_ICON_PATH = Path(__file__).resolve().parent / "assets" / "settings.svg"


class ModeLogStore:
    def __init__(self) -> None:
        self._lines = {"single": [], "batch": []}

    def append(self, mode: str, line: str) -> None:
        self._lines[mode].append(line)

    def text(self, mode: str) -> str:
        return "\n".join(self._lines[mode])


class SettingsDialog(QDialog):
    def __init__(self, options: CollectionOptions, parent=None) -> None:
        super().__init__(parent); self.setWindowTitle("采集内容设置"); self.setMinimumWidth(300)
        root = QVBoxLayout(self); group = QGroupBox("采集内容"); layout = QVBoxLayout(group)
        self.text = QCheckBox("文案（content.txt）"); self.images = QCheckBox("图片（图文图片与封面 / 视频封面）")
        self.text.setChecked(options.text); self.images.setChecked(options.images); layout.addWidget(self.text); layout.addWidget(self.images); root.addWidget(group)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok); self.buttons.button(QDialogButtonBox.Ok).setText("保存设置"); self.buttons.accepted.connect(self.accept); self.buttons.rejected.connect(self.reject); root.addWidget(self.buttons)
        self.text.stateChanged.connect(self._update_accept); self.images.stateChanged.connect(self._update_accept); self._update_accept()
    def _update_accept(self) -> None:
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(self.text.isChecked() or self.images.isChecked())
    def options(self) -> CollectionOptions:
        return CollectionOptions(text=self.text.isChecked(), images=self.images.isChecked())


class RecognitionWorker(QObject):
    progress = Signal(str); succeeded = Signal(object); failed = Signal(str); finished = Signal()
    def __init__(self, mode: str, url: str) -> None: super().__init__(); self.mode = mode; self.url = url
    @Slot()
    def run(self) -> None:
        try: self.succeeded.emit(extract_profile(self.url, self.progress.emit) if self.mode == "batch" else extract_video(self.url, self.progress.emit))
        except Exception as exc: self.failed.emit(str(exc) or "识别失败，请稍后重试")
        finally: self.finished.emit()


class SaveWorker(QObject):
    progress = Signal(int, str); succeeded = Signal(object); failed = Signal(str); finished = Signal()
    def __init__(self, info: VideoInfo, output_root: Path, options: CollectionOptions, overwrite: bool = False) -> None: super().__init__(); self.info = info; self.output_root = output_root; self.options = options; self.overwrite = overwrite
    @Slot()
    def run(self) -> None:
        try:
            self.progress.emit(35, "准备保存所选内容")
            result = save_selected_assets(self.info, self.output_root, self.options, overwrite=self.overwrite, progress=lambda step: self.progress.emit(60, step))
            self.progress.emit(90, "整理保存结果")
            self.succeeded.emit((result, media))
        except Exception as exc: self.failed.emit(str(exc) or "保存失败，请检查输出目录权限")
        finally: self.finished.emit()


class BatchWorker(QObject):
    progress = Signal(int, int, str, str); log = Signal(str); succeeded = Signal(object); failed = Signal(str); finished = Signal()
    def __init__(self, works: list[ProfileWork], output_root: Path, options: CollectionOptions | None = None, overwrite: bool = False, skipped: int = 0, overwrite_ids: set[str] | None = None) -> None: super().__init__(); self.works = works; self.output_root = output_root; self.options = options or CollectionOptions(); self.overwrite = overwrite; self.skipped = skipped; self.overwrite_ids = overwrite_ids if overwrite_ids is not None else ({work.aweme_id for work in works} if overwrite else set())
    @Slot()
    def run(self) -> None:
        success = failed = 0; output_dir = None; skipped = self.skipped; started = time.monotonic(); total = len(self.works)
        session = None
        try:
            for index, work in enumerate(self.works, 1):
                    self.progress.emit(index, total, work.title, "准备保存")
                    try:
                        if _profile_work_has_complete_media(work, self.options):
                            info = _profile_work_info(work)
                            self.progress.emit(index, total, work.title, "使用主页作品数据")
                        else:
                            if session is None: session = DouyinSession().__enter__()
                            info = session.extract_video(work.url, lambda step: self.progress.emit(index, total, work.title, step))
                        result = save_selected_assets(info, self.output_root, self.options, overwrite=work.aweme_id in self.overwrite_ids, progress=lambda step: self.progress.emit(index, total, work.title, step))
                        output_dir = result.author_dir
                        success += 1; self.log.emit(_batch_save_message(index, total, work.title, info, result, self.options))
                    except Exception as exc:
                        failed += 1; self.log.emit(f"保存失败 {index}/{total}：{work.title} — {str(exc) or '未知错误'}")
            self.succeeded.emit((output_dir, success, skipped, failed, time.monotonic() - started))
        except Exception as exc: self.failed.emit(str(exc) or "批量任务无法启动")
        finally:
            if session is not None: session.__exit__(None, None, None)
            self.finished.emit()


class WorksSelectionDialog(QDialog):
    def __init__(self, profile: ProfileInfo, options: CollectionOptions, parent=None) -> None:
        super().__init__(parent); self.profile = profile; self.options = options; self.checks: list[QCheckBox] = []; self.manager = QNetworkAccessManager(self); self.setWindowTitle(f"选择 {profile.author} 的作品"); self.resize(820, 680); self._build()
    def _build(self) -> None:
        root = QVBoxLayout(self); root.addWidget(QLabel(f"已识别 {len(self.profile.works)} 个当前可访问的公开作品")); scroll = QScrollArea(); scroll.setWidgetResizable(True); content = QWidget(); grid = QGridLayout(content); grid.setSpacing(12)
        output_root = Path(__file__).resolve().parent / "output"
        for i, work in enumerate(self.profile.works):
            card = QGroupBox(); layout = QVBoxLayout(card); image = QLabel("封面加载中"); image.setAlignment(Qt.AlignCenter); image.setFixedSize(170, 150); image.setStyleSheet("background:#eef1f5;border-radius:8px;color:#858995;"); layout.addWidget(image, alignment=Qt.AlignCenter)
            state = _profile_work_asset_state(work, output_root, self.options)
            state_text = "已完整" if state.is_complete_for(self.options) else "待补齐" if state.has_requested_asset(self.options) else ""
            title_text = _short(work.title, 36) + (f"\n{state_text}" if state_text else "")
            title = QLabel(title_text); title.setWordWrap(True); title.setFixedHeight(42); layout.addWidget(title); check = QCheckBox("选择"); check.stateChanged.connect(self._update_count); layout.addWidget(check); self.checks.append(check)
            if work.cover_url: self._load_cover(work.cover_url, image)
            grid.addWidget(card, i // 4, i % 4)
        scroll.setWidget(content); root.addWidget(scroll, 1); bottom = QHBoxLayout(); self.count = QLabel(); all_button = QPushButton("全选"); none_button = QPushButton("取消全选"); all_button.clicked.connect(lambda: self._set_all(True)); none_button.clicked.connect(lambda: self._set_all(False)); bottom.addWidget(self.count); bottom.addStretch(); bottom.addWidget(all_button); bottom.addWidget(none_button)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok); self.buttons.button(QDialogButtonBox.Ok).setText("确认选择"); self.buttons.accepted.connect(self.accept); self.buttons.rejected.connect(self.reject); bottom.addWidget(self.buttons); root.addLayout(bottom); self._update_count()
    def _load_cover(self, url: str, label: QLabel) -> None:
        reply = self.manager.get(QNetworkRequest(QUrl(url)))
        def done() -> None:
            data = reply.readAll(); pixmap = QPixmap(); reply.deleteLater()
            if pixmap.loadFromData(data): label.setPixmap(pixmap.scaled(label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else: label.setText("封面不可用")
        reply.finished.connect(done)
    def _set_all(self, checked: bool) -> None:
        for check in self.checks: check.setChecked(checked)
    def _update_count(self) -> None:
        count = sum(check.isChecked() for check in self.checks); self.count.setText(f"已选 {count} 项"); self.buttons.button(QDialogButtonBox.Ok).setEnabled(count > 0)
    def selected(self) -> list[ProfileWork]: return [work for work, check in zip(self.profile.works, self.checks) if check.isChecked()]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__(); self.setWindowIcon(QIcon(str(ICON_PATH))); self.current_mode = "single"; self.single: VideoInfo | None = None; self.profile: ProfileInfo | None = None; self.selected: list[ProfileWork] = []; self.output_dir: Path | None = None; self.thread: QThread | None = None; self.worker: QObject | None = None; self.preview_manager = QNetworkAccessManager(self); self.preview_reply = None; self.app_settings = QSettings("Vidpick", "Vidpick"); self.options = _load_collection_options(self.app_settings); self.mode_logs = ModeLogStore(); self._build(); self._style(); self._refresh_collection_status()
    def _build(self) -> None:
        self.setWindowTitle("Vidpick"); self.resize(980, 760); self.setMinimumSize(820, 650); central = QWidget(); central.setObjectName("central_widget"); self.setCentralWidget(central); root = QVBoxLayout(central); root.setContentsMargins(30, 26, 30, 26); root.setSpacing(14)
        header = QHBoxLayout(); header.setSpacing(10); title = QLabel("Vidpick"); title.setObjectName("title"); header.addWidget(title); header.addStretch()
        self.collection_status = QWidget(); self.collection_status.setObjectName("collection_status"); self.collection_status.setFixedSize(122, 48); status_layout = QVBoxLayout(self.collection_status); status_layout.setContentsMargins(9, 4, 9, 4); status_layout.setSpacing(0)
        for label_text, attribute in (("文案", "collection_text_state"), ("图片/封面", "collection_images_state")):
            status_row = QHBoxLayout(); status_row.setContentsMargins(0, 0, 0, 0); status_row.setSpacing(4); label = QLabel(label_text); label.setObjectName("collection_status_label"); value = QLabel(); value.setObjectName("collection_status_value"); setattr(self, attribute, value); status_row.addWidget(label); status_row.addStretch(); status_row.addWidget(value); status_layout.addLayout(status_row)
        header.addWidget(self.collection_status); self.settings_button = QPushButton(); self.settings_button.setObjectName("utility"); self.settings_button.setIcon(QIcon(str(SETTINGS_ICON_PATH))); self.settings_button.setIconSize(QSize(20, 20)); self.settings_button.setToolTip("采集内容设置"); self.settings_button.clicked.connect(self._edit_settings); header.addWidget(self.settings_button)
        mode_container = QWidget(); mode_container.setObjectName("mode_container"); mode_container.setFixedWidth(300); mode_layout = QHBoxLayout(mode_container); mode_layout.setContentsMargins(4, 4, 4, 4); mode_layout.setSpacing(3); self.mode_group = QButtonGroup(self); self.mode_group.setExclusive(True); self.mode_buttons: dict[str, QPushButton] = {}
        for mode, label in (("single", "单个作品"), ("batch", "博主批量")):
            button = QPushButton(label); button.setObjectName("mode_button"); button.setCheckable(True); button.setChecked(mode == self.current_mode); button.clicked.connect(lambda _checked, selected_mode=mode: self._mode_changed(selected_mode)); self.mode_group.addButton(button); self.mode_buttons[mode] = button; mode_layout.addWidget(button)
        header.addWidget(mode_container); root.addLayout(header)

        info_column = QWidget(); info_layout = QVBoxLayout(info_column); info_layout.setContentsMargins(0, 0, 0, 0); info_layout.setSpacing(14)
        link_group = QGroupBox(); link_group.setObjectName("link_card"); link_group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed); link_layout = QVBoxLayout(link_group); link_layout.setContentsMargins(12, 6, 12, 10); link_layout.setSpacing(6); link_layout.addWidget(_card_title("链接")); row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(8); self.url = QLineEdit(); self.url.setPlaceholderText("粘贴抖音网页版作品地址"); self.url.textChanged.connect(self._reset); self.paste_button = QPushButton(); self.paste_button.setObjectName("utility"); self.paste_button.setIcon(QIcon(str(PASTE_ICON_PATH))); self.paste_button.setIconSize(QSize(20, 20)); self.paste_button.setToolTip("粘贴剪贴板内容"); self.paste_button.setAccessibleName("粘贴剪贴板内容"); self.paste_button.clicked.connect(self._paste_clipboard); self.recognize = QPushButton("识别"); self.recognize.setObjectName("primary"); self.recognize.clicked.connect(self._recognize); row.addWidget(self.url); row.addWidget(self.paste_button); row.addWidget(self.recognize); link_layout.addLayout(row); info_layout.addWidget(link_group)
        result = QGroupBox(); result.setObjectName("result_card"); result_layout = QVBoxLayout(result); result_layout.setContentsMargins(12, 6, 12, 10); result_layout.setSpacing(6); result_layout.addWidget(_card_title("识别结果")); result_grid = QGridLayout(); result_grid.setContentsMargins(0, 0, 0, 0); result_grid.setHorizontalSpacing(10); result_grid.setVerticalSpacing(4); self.status = QLabel("未识别"); self.author = QLabel("—"); self.summary = QLabel("—"); self.summary.setWordWrap(True); self.work_type = QLabel("—"); self.detail = QLabel("识别成功后显示作品信息"); self.detail.setWordWrap(True); self.result_field_labels = []
        for row, (field_name, value_label) in enumerate((("状态", self.status), ("博主", self.author), ("作品", self.summary), ("类型", self.work_type), ("详情", self.detail))):
            field_label = QLabel(field_name); field_label.setObjectName("result_label"); field_label.setFixedWidth(44); field_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter); value_label.setObjectName("result_value"); value_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter); self.result_field_labels.append(field_label); result_grid.addWidget(field_label, row, 0); result_grid.addWidget(value_label, row, 1)
        result_grid.setColumnStretch(1, 1); result_layout.addLayout(result_grid); info_layout.addWidget(result)
        self.preview_card = QGroupBox(); self.preview_card.setObjectName("preview_card"); self.preview_card.setFixedWidth(230); preview_layout = QVBoxLayout(self.preview_card); preview_layout.setContentsMargins(12, 6, 12, 10); preview_layout.setSpacing(6); preview_layout.addWidget(_card_title("作品预览")); self.preview_image = QLabel("暂无预览"); self.preview_image.setObjectName("preview_image"); self.preview_image.setAlignment(Qt.AlignCenter); self.preview_image.setWordWrap(True); self.preview_image.setFixedSize(200, 220); preview_layout.addWidget(self.preview_image, alignment=Qt.AlignCenter)
        info_row = QHBoxLayout(); info_row.setSpacing(14); info_row.addWidget(info_column, 1); info_row.addWidget(self.preview_card); root.addLayout(info_row)
        self.task_card = QGroupBox(); self.task_card.setObjectName("task_card"); task_layout = QVBoxLayout(self.task_card); task_layout.setContentsMargins(12, 6, 12, 10); task_layout.setSpacing(8); task_layout.addWidget(_card_title("任务")); top = QHBoxLayout(); top.setContentsMargins(0, 0, 0, 0); self.step = QLabel("等待识别"); top.addWidget(self.step); top.addStretch(); self.select_works = QPushButton("选择作品"); self.select_works.setEnabled(False); self.select_works.setVisible(False); self.select_works.clicked.connect(self._choose_works); top.addWidget(self.select_works); self.start = QPushButton("开始任务"); self.start.setEnabled(False); self.start.clicked.connect(self._start); top.addWidget(self.start); task_layout.addLayout(top); self.progress = QProgressBar(); self.progress.setValue(0); task_layout.addWidget(self.progress); root.addWidget(self.task_card)
        self.log_card = QGroupBox(); self.log_card.setObjectName("log_card"); log_layout = QVBoxLayout(self.log_card); log_layout.setContentsMargins(12, 6, 12, 10); log_layout.setSpacing(8); log_layout.addWidget(_card_title("状态 / 日志")); self.logs = QPlainTextEdit(); self.logs.setReadOnly(True); self.logs.setMinimumHeight(150); log_layout.addWidget(self.logs); root.addWidget(self.log_card, 1); footer = QHBoxLayout(); footer.addStretch(); self.open_folder = QPushButton("打开保存文件夹"); self.open_folder.setEnabled(False); self.open_folder.clicked.connect(self._open_folder); footer.addWidget(self.open_folder); root.addLayout(footer)
    def _style(self) -> None:
        self.setStyleSheet("QMainWindow{background:#f5f6f8;color:#1d1d1f} QWidget#central_widget{background:#f5f6f8} QLabel{background:transparent} QGroupBox{background:white;border:1px solid #e1e4e9;border-radius:14px;margin-top:10px;padding-top:12px;font-weight:600} QGroupBox#link_card,QGroupBox#result_card,QGroupBox#preview_card,QGroupBox#task_card,QGroupBox#log_card{margin-top:0;padding-top:0} QGroupBox::title{subcontrol-origin:margin;left:12px;top:1px;padding:0 4px;background:transparent} QLabel#title{font-size:28px;font-weight:700} QLabel#card_title{color:#111827;font-size:16px;font-weight:700} QLineEdit,QPlainTextEdit{background:#fbfbfc;border:1px solid #dfe2e8;border-radius:10px;padding:9px} QPushButton{background:#edf0f5;border:none;border-radius:10px;padding:10px 16px;font-weight:600} QPushButton:hover{background:#e2e7ef} QPushButton:pressed{background:#d5dce8} QPushButton:disabled{color:#a8abb2;background:#eceef2} QPushButton#primary{background:#2775e8;color:white} QPushButton#primary:hover{background:#3c86ef;color:white} QPushButton#primary:pressed{background:#1f62c7;color:white} QPushButton#primary:disabled{background:#b8bdc7;color:#737983} QPushButton#utility{padding:0;min-width:38px;max-width:38px;min-height:38px;max-height:38px} QPushButton#utility:hover{background:#e1e8f3} QPushButton#utility:pressed{background:#cbd6e7} QWidget#mode_container{background:#e9eef7;border-radius:12px} QPushButton#mode_button{background:transparent;color:#667085;border-radius:9px;padding:7px 12px;min-height:16px} QPushButton#mode_button:hover{background:#dce5f4;color:#334155} QPushButton#mode_button:pressed{background:#cbd8ee;color:#334155} QPushButton#mode_button:checked{background:#355fc1;color:white} QPushButton#mode_button:checked:pressed{background:#294eaa;color:white} QPushButton#mode_button:disabled{color:#a8abb2;background:transparent} QWidget#collection_status{background:#eef2f8;border-radius:10px} QLabel#collection_status_label{color:#667085;font-size:11px} QLabel#collection_status_value{color:#355fc1;font-size:11px;font-weight:600} QLabel#result_label{color:#344054;font-weight:500} QLabel#result_value{font-weight:400} QLabel#preview_image{background:#f7f9fc;border:1px solid #e2e7ef;border-radius:10px;color:#8992a3;padding:6px} QProgressBar{border:none;border-radius:6px;background:#e8ebf1;height:14px;text-align:center} QProgressBar::chunk{background:#2775e8;border-radius:6px}")
    def _mode_key(self) -> str: return self.current_mode
    def _log(self, text: str) -> None:
        line = f"[{datetime.now():%H:%M:%S}] {text}"; self.mode_logs.append(self._mode_key(), line); self.logs.appendPlainText(line); self.logs.verticalScrollBar().setValue(self.logs.verticalScrollBar().maximum())
    def _show_mode_log(self) -> None:
        self.logs.setPlainText(self.mode_logs.text(self._mode_key())); self.logs.verticalScrollBar().setValue(self.logs.verticalScrollBar().maximum())
    def _set_preview_message(self, text: str) -> None:
        self.preview_reply = None
        self.preview_image.setPixmap(QPixmap()); self.preview_image.setText(text)
    def _load_preview(self, url: str) -> None:
        if not url:
            self._set_preview_message("预览不可用"); return
        self._set_preview_message("正在加载预览…")
        reply = self.preview_manager.get(QNetworkRequest(QUrl(url))); self.preview_reply = reply
        def done() -> None:
            if reply is not self.preview_reply:
                reply.deleteLater(); return
            data = reply.readAll(); self.preview_reply = None; pixmap = QPixmap(); reply.deleteLater()
            if pixmap.loadFromData(data):
                self.preview_image.setText(""); self.preview_image.setPixmap(pixmap.scaled(190, 210, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                self._set_preview_message("预览不可用")
        reply.finished.connect(done)
    def _paste_clipboard(self) -> None:
        text = QApplication.clipboard().text().strip()
        self.url.clear()
        if text:
            self.url.setText(text)
    def _edit_settings(self) -> None:
        dialog = SettingsDialog(self.options, self)
        if dialog.exec() == QDialog.Accepted:
            self.options = dialog.options()
            _save_collection_options(self.app_settings, self.options)
            self._refresh_collection_status()
    def _refresh_collection_status(self) -> None:
        self.collection_text_state.setText("✓" if self.options.text else "未选")
        self.collection_images_state.setText("✓" if self.options.images else "未选")
    def _work_type_text(self, info: VideoInfo) -> str:
        if info.work_type != "image":
            return "视频"
        total = info.image_total or len(info.image_urls)
        return f"图文 · {total} 张" if len(info.image_urls) == total else f"图文 · {len(info.image_urls)}/{total} 张可用"
    def _mode_changed(self, mode: str) -> None:
        if mode == self.current_mode: return
        self.current_mode = mode; self.mode_buttons[mode].setChecked(True); self.url.setPlaceholderText("粘贴 douyin.com 博主主页分享链接" if mode == "batch" else "粘贴抖音网页版作品地址"); self.select_works.setVisible(mode == "batch"); self.select_works.setText("选择作品"); self.preview_card.setVisible(mode == "single"); self._show_mode_log(); self._reset()
    def _reset(self, *_args) -> None:
        if self.thread: return
        self.single = None; self.profile = None; self.selected = []; self.start.setEnabled(False); self.select_works.setEnabled(False); self.open_folder.setEnabled(False); self.status.setText("未识别"); self.author.setText("—"); self.summary.setText("—"); self.work_type.setText("—"); self.detail.setText("识别成功后显示作品信息"); self.progress.setValue(0); self.step.setText("等待识别")
        if self.current_mode == "single": self._set_preview_message("暂无预览")
    def _set_busy(self, busy: bool) -> None:
        for button in self.mode_buttons.values(): button.setEnabled(not busy)
        self.url.setEnabled(not busy); self.paste_button.setEnabled(not busy); self.recognize.setEnabled(not busy); self.settings_button.setEnabled(not busy); self.select_works.setEnabled(not busy and self.profile is not None); self.start.setEnabled(not busy and bool(self.single or self.selected))
    def _recognize(self) -> None:
        if self.thread is not None: return
        if not self.url.text().strip(): self.status.setText("失败：请输入链接"); return
        self._set_busy(True)
        self._reset(); self.status.setText("识别中…"); self.step.setText("准备识别"); self.progress.setValue(5)
        if self.current_mode == "single": self._set_preview_message("正在获取作品信息…")
        self._run(RecognitionWorker(self.current_mode, self.url.text()))
    def _start(self) -> None:
        if self.thread is not None: return
        root = Path(__file__).resolve().parent / "output"; self.open_folder.setEnabled(False)
        if self.single:
            state = get_asset_state(self.single, root)
            overwrite = False
            if state.is_complete_for(self.options):
                box = QMessageBox(QMessageBox.Warning, "重复作品", "这个作品所选内容已经采集过。", parent=self)
                overwrite_button = box.addButton("覆盖所选内容", QMessageBox.AcceptRole)
                box.addButton("取消任务", QMessageBox.RejectRole)
                box.exec()
                if box.clickedButton() is not overwrite_button:
                    self.status.setText("已取消"); self.step.setText("本次任务已取消"); return
                overwrite = True
            self._run(SaveWorker(self.single, root, self.options, overwrite=overwrite))
            return
        if not self.selected: return
        states = {work.aweme_id: _profile_work_asset_state(work, root, self.options) for work in self.selected}
        complete = [work for work in self.selected if states[work.aweme_id].is_complete_for(self.options)]
        partial = [work for work in self.selected if not states[work.aweme_id].is_complete_for(self.options) and states[work.aweme_id].has_requested_asset(self.options)]
        new = [work for work in self.selected if not states[work.aweme_id].has_requested_asset(self.options)]
        overwrite_ids: set[str] = set(); skipped = 0; works = self.selected
        if complete:
            message = f"已选择 {len(self.selected)} 个作品：\n需要新采集 {len(new)} 个\n需要补齐 {len(partial)} 个\n所选内容已完整 {len(complete)} 个"
            box = QMessageBox(QMessageBox.Warning, "发现已完整作品", message, parent=self)
            skip_button = box.addButton("跳过重复作品并继续", QMessageBox.AcceptRole)
            overwrite_button = box.addButton("覆盖重复作品并继续", QMessageBox.DestructiveRole)
            box.addButton("返回", QMessageBox.RejectRole)
            box.setDefaultButton(skip_button)
            box.exec()
            if box.clickedButton() is skip_button:
                complete_ids = {work.aweme_id for work in complete}; works = [work for work in self.selected if work.aweme_id not in complete_ids]; skipped = len(complete)
            elif box.clickedButton() is overwrite_button:
                overwrite_ids = {work.aweme_id for work in complete}
            else:
                self.step.setText("已返回，未启动任务"); return
        self._run(BatchWorker(works, root, self.options, overwrite=bool(overwrite_ids), skipped=skipped, overwrite_ids=overwrite_ids))
    def _run(self, worker: QObject) -> None:
        if self.thread is not None: return
        self._set_busy(True); thread = QThread(self); worker.moveToThread(thread); self.thread = thread; self.worker = worker; thread.started.connect(worker.run); worker.finished.connect(thread.quit); worker.finished.connect(worker.deleteLater); thread.finished.connect(thread.deleteLater); thread.finished.connect(self._finished)
        if isinstance(worker, RecognitionWorker): worker.progress.connect(self._progress_text); worker.succeeded.connect(self._recognized); worker.failed.connect(self._recognition_failed)
        elif isinstance(worker, SaveWorker): worker.progress.connect(self._save_progress); worker.succeeded.connect(self._saved); worker.failed.connect(self._save_failed)
        else: worker.progress.connect(self._batch_progress); worker.log.connect(self._log); worker.succeeded.connect(self._batch_done); worker.failed.connect(self._batch_failed)
        thread.start()
    @Slot(str)
    def _progress_text(self, text: str) -> None: self.step.setText(text)
    @Slot(int, str)
    def _save_progress(self, value: int, text: str) -> None: self.progress.setValue(value); self.step.setText(text)
    @Slot(object)
    def _recognized(self, result: object) -> None:
        self.progress.setValue(100)
        if isinstance(result, VideoInfo): self.single = result; self.select_works.setVisible(False); self.author.setText(result.author); self.summary.setText(result.title); self.work_type.setText(self._work_type_text(result)); self.detail.setText(result.content[:240] + ("…" if len(result.content) > 240 else "")); self.status.setText("成功"); self.step.setText("识别完成，可以开始任务"); self._load_preview(result.cover_url); self._log(f"识别成功：{result.author} / {result.title} · {self._work_type_text(result)}")
        else:
            self.profile = result; self.preview_card.setVisible(False); self.select_works.setVisible(True); self.author.setText(result.author); self.summary.setText(f"识别到 {len(result.works)} 个公开作品"); self.work_type.setText("—"); self.detail.setText("请选择要采集的作品"); self.status.setText("成功"); self.select_works.setEnabled(True); self._log(f"识别成功：{result.author}，共 {len(result.works)} 个作品"); self._choose_works()
    def _choose_works(self) -> None:
        if not self.profile: return
        dialog = WorksSelectionDialog(self.profile, self.options, self)
        if dialog.exec() == QDialog.Accepted:
            self.selected = dialog.selected(); self.summary.setText(f"识别到 {len(self.profile.works)} 个公开作品，已选择 {len(self.selected)} 项"); self.detail.setText("已选择作品，点击“开始任务”后顺序采集"); self.step.setText("选择完成，可以开始任务"); self.select_works.setText(f"重新选择（已选 {len(self.selected)}）"); self.start.setEnabled(True)
        else: self.step.setText("可点击“选择作品”重新打开")
    @Slot(int, int, str, str)
    def _batch_progress(self, index: int, total: int, title: str, step: str) -> None: self.progress.setValue(int((index - 1) * 100 / total)); self.step.setText(f"当前 {index}/{total}：{_short(title, 36)} — {step}")
    @Slot(object)
    def _saved(self, result: object) -> None:
        saved = result; self.output_dir = saved.author_dir; self.progress.setValue(100); self.status.setText("成功"); self.step.setText("任务完成"); self.open_folder.setEnabled(True)
        self._log(_selected_save_message(self.single.title if self.single else saved.work_dir.name, self.single, saved, self.options))
    @Slot(object)
    def _batch_done(self, result: object) -> None:
        folder, success, skipped, failed, elapsed = result; self.output_dir = folder; self.progress.setValue(100); self.status.setText("完成" if not failed else "完成（含失败项）"); self.step.setText(f"批量完成：成功 {success} / 跳过 {skipped} / 失败 {failed}"); self.open_folder.setEnabled(folder is not None); self._log(f"批量完成：成功 {success}，跳过 {skipped}，失败 {failed}，耗时 {elapsed:.1f} 秒")
    @Slot(str)
    def _failed(self, message: str) -> None: self.status.setText("失败"); self.step.setText("任务失败，可修正后重试"); self.progress.setValue(0)
    @Slot(str)
    def _recognition_failed(self, message: str) -> None: self._failed(message); self._set_preview_message("预览不可用") if self.current_mode == "single" else None; self._log(f"识别失败：{message}")
    @Slot(str)
    def _save_failed(self, message: str) -> None: self._failed(message); self._log(f"保存失败：{message}")
    @Slot(str)
    def _batch_failed(self, message: str) -> None: self._failed(message); self._log(f"批量失败：{message}")
    @Slot()
    def _finished(self) -> None: self.thread = None; self.worker = None; self._set_busy(False)
    def _open_folder(self) -> None:
        if self.output_dir and self.output_dir.exists(): QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output_dir)))


def _short(value: str, size: int) -> str: return value[:size] + ("…" if len(value) > size else "")
def _card_title(text: str) -> QLabel:
    label = QLabel(text); label.setObjectName("card_title"); label.setFixedHeight(20); label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter); return label
def _load_collection_options(settings: QSettings) -> CollectionOptions:
    text = settings.value("collect_text", True, type=bool); images = settings.value("collect_images", True, type=bool)
    return CollectionOptions(text=text, images=images) if text or images else CollectionOptions()
def _save_collection_options(settings: QSettings, options: CollectionOptions) -> None:
    settings.setValue("collect_text", options.text); settings.setValue("collect_images", options.images); settings.sync()
def _profile_work_info(work: ProfileWork) -> VideoInfo:
    return VideoInfo(work.author or "未命名博主", work.title, work.desc, work.url, work.aweme_id, work.work_type or "video", work.cover_url, work.image_urls, work.image_total)
def _profile_work_has_complete_media(work: ProfileWork, options: CollectionOptions) -> bool:
    if options.text and not work.desc:
        return False
    if not options.images:
        return True
    if work.work_type == "image":
        return bool(work.image_total and len(work.image_urls) == work.image_total and work.cover_url == work.image_urls[0])
    return work.work_type == "video" and bool(work.cover_url)
def _profile_work_asset_state(work: ProfileWork, output_root: Path, options: CollectionOptions) -> AssetState:
    state = get_asset_state(_profile_work_info(work), output_root)
    media_options = CollectionOptions(text=False, images=True)
    return AssetState(state.text, False) if options.images and not _profile_work_has_complete_media(work, media_options) else state
def _image_phrase(info: VideoInfo, saved: SelectedSaveResult) -> tuple[bool, str]:
    media = saved.media
    if info.work_type == "image":
        if media.total and media.saved == media.total:
            return True, f"{media.saved} 张图片"
        if media.saved:
            return False, f"无水印图片 {media.saved}/{media.total}"
        return False, "未获取到可靠无水印图片"
    if media.cover_saved:
        return True, "封面"
    return False, "封面未保存" if info.cover_url else "未获取到可靠图片来源"
def _selected_save_message(title: str, info: VideoInfo | None, saved: SelectedSaveResult, options: CollectionOptions) -> str:
    if info is None:
        return f"保存成功：{title}"
    complete_media, image_phrase = _image_phrase(info, saved)
    topping_up = saved.before.has_requested_asset(options) and not saved.before.is_complete_for(options)
    if topping_up:
        additions = []
        if options.text and not saved.before.text and saved.after.text:
            additions.append("新增文案")
        if options.images and not saved.before.images:
            additions.append(f"新增 {saved.media.newly_saved} 张图片" if info.work_type == "image" and complete_media else image_phrase)
        return f"补齐完成：{title} · {' + '.join(additions) or '所选内容'}"
    if options.images and not complete_media:
        text_part = "文案成功，" if options.text and saved.after.text else ""
        return f"保存完成：{title} · {text_part}{image_phrase}"
    parts = []
    if options.text and saved.after.text:
        parts.append("文案")
    if options.images:
        parts.append(image_phrase)
    return f"保存成功：{title} · {' + '.join(parts)}"
def _batch_save_message(index: int, total: int, title: str, info: VideoInfo, saved: SelectedSaveResult, options: CollectionOptions) -> str:
    message = _selected_save_message(title, info, saved, options)
    return message.replace("：", f" {index}/{total}：", 1)
def run() -> int:
    app = QApplication(sys.argv); app.setApplicationName("Vidpick"); app.setWindowIcon(QIcon(str(ICON_PATH))); window = MainWindow(); window.show(); return app.exec()
if __name__ == "__main__": raise SystemExit(run())
