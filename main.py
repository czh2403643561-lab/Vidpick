"""Vidpick desktop application."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import time

from PySide6.QtCore import QObject, QThread, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QVBoxLayout, QWidget

from douyin_parser import DouyinSession, ProfileInfo, ProfileWork, VideoInfo, extract_profile, extract_video
from storage import save_video


class ModeLogStore:
    def __init__(self) -> None:
        self._lines = {"single": [], "batch": []}

    def append(self, mode: str, line: str) -> None:
        self._lines[mode].append(line)

    def text(self, mode: str) -> str:
        return "\n".join(self._lines[mode])


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
    def __init__(self, info: VideoInfo, output_root: Path) -> None: super().__init__(); self.info = info; self.output_root = output_root
    @Slot()
    def run(self) -> None:
        try:
            self.progress.emit(40, "写入 TXT 文案"); result = save_video(self.info, self.output_root); self.progress.emit(90, "追加 JSONL 记录"); self.succeeded.emit(result)
        except Exception as exc: self.failed.emit(str(exc) or "保存失败，请检查输出目录权限")
        finally: self.finished.emit()


class BatchWorker(QObject):
    progress = Signal(int, int, str, str); log = Signal(str); succeeded = Signal(object); failed = Signal(str); finished = Signal()
    def __init__(self, works: list[ProfileWork], output_root: Path) -> None: super().__init__(); self.works = works; self.output_root = output_root
    @Slot()
    def run(self) -> None:
        success = failed = 0; output_dir = None; started = time.monotonic(); total = len(self.works)
        session = None
        try:
            for index, work in enumerate(self.works, 1):
                    self.progress.emit(index, total, work.title, "准备保存")
                    try:
                        if work.desc:
                            info = VideoInfo(work.author or "未命名博主", work.title, work.desc, work.url)
                            self.progress.emit(index, total, work.title, "使用主页作品正文")
                        else:
                            if session is None: session = DouyinSession().__enter__()
                            info = session.extract_video(work.url, lambda step: self.progress.emit(index, total, work.title, step))
                        output_dir, txt_path, _ = save_video(info, self.output_root); success += 1; self.log.emit(f"成功 {index}/{total}：{txt_path.name}")
                    except Exception as exc:
                        failed += 1; self.log.emit(f"失败 {index}/{total}：{work.title} — {str(exc) or '未知错误'}")
            self.succeeded.emit((output_dir, success, failed, time.monotonic() - started))
        except Exception as exc: self.failed.emit(str(exc) or "批量任务无法启动")
        finally:
            if session is not None: session.__exit__(None, None, None)
            self.finished.emit()


class WorksSelectionDialog(QDialog):
    def __init__(self, profile: ProfileInfo, parent=None) -> None:
        super().__init__(parent); self.profile = profile; self.checks: list[QCheckBox] = []; self.manager = QNetworkAccessManager(self); self.setWindowTitle(f"选择 {profile.author} 的作品"); self.resize(820, 680); self._build()
    def _build(self) -> None:
        root = QVBoxLayout(self); root.addWidget(QLabel(f"已识别 {len(self.profile.works)} 个当前可访问的公开作品")); scroll = QScrollArea(); scroll.setWidgetResizable(True); content = QWidget(); grid = QGridLayout(content); grid.setSpacing(12)
        for i, work in enumerate(self.profile.works):
            card = QGroupBox(); layout = QVBoxLayout(card); image = QLabel("封面加载中"); image.setAlignment(Qt.AlignCenter); image.setFixedSize(170, 150); image.setStyleSheet("background:#eef1f5;border-radius:8px;color:#858995;"); layout.addWidget(image, alignment=Qt.AlignCenter)
            title = QLabel(_short(work.title, 42)); title.setWordWrap(True); title.setFixedHeight(42); layout.addWidget(title); check = QCheckBox("选择"); check.stateChanged.connect(self._update_count); layout.addWidget(check); self.checks.append(check)
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
        super().__init__(); self.single: VideoInfo | None = None; self.profile: ProfileInfo | None = None; self.selected: list[ProfileWork] = []; self.output_dir: Path | None = None; self.thread: QThread | None = None; self.worker: QObject | None = None; self.mode_logs = ModeLogStore(); self._build(); self._style(); self._log("应用已启动")
    def _build(self) -> None:
        self.setWindowTitle("Vidpick"); self.resize(900, 760); self.setMinimumSize(760, 650); central = QWidget(); self.setCentralWidget(central); root = QVBoxLayout(central); root.setContentsMargins(30, 26, 30, 26); root.setSpacing(14)
        header = QHBoxLayout(); title = QLabel("Vidpick"); title.setObjectName("title"); header.addWidget(title); header.addStretch(); self.mode = QComboBox(); self.mode.addItems(["单个链接", "博主主页批量"]); self.mode.currentIndexChanged.connect(self._mode_changed); header.addWidget(self.mode); root.addLayout(header)
        link_group = QGroupBox("链接"); row = QHBoxLayout(link_group); self.url = QLineEdit(); self.url.setPlaceholderText("粘贴 douyin.com 作品分享链接"); self.url.textChanged.connect(self._reset); self.recognize = QPushButton("识别"); self.recognize.setObjectName("primary"); self.recognize.clicked.connect(self._recognize); row.addWidget(self.url); row.addWidget(self.recognize); root.addWidget(link_group)
        result = QGroupBox("识别结果"); form = QFormLayout(result); self.status = QLabel("未识别"); self.author = QLabel("—"); self.summary = QLabel("—"); self.summary.setWordWrap(True); self.detail = QLabel("识别成功后显示作品信息"); self.detail.setWordWrap(True); form.addRow("状态", self.status); form.addRow("博主", self.author); form.addRow("作品", self.summary); form.addRow("详情", self.detail); root.addWidget(result)
        task = QGroupBox("任务"); task_layout = QVBoxLayout(task); top = QHBoxLayout(); self.step = QLabel("等待识别"); top.addWidget(self.step); top.addStretch(); self.select_works = QPushButton("选择作品"); self.select_works.setEnabled(False); self.select_works.clicked.connect(self._choose_works); top.addWidget(self.select_works); self.start = QPushButton("开始任务"); self.start.setEnabled(False); self.start.clicked.connect(self._start); top.addWidget(self.start); task_layout.addLayout(top); self.progress = QProgressBar(); self.progress.setValue(0); task_layout.addWidget(self.progress); root.addWidget(task)
        log_box = QGroupBox("状态 / 日志"); log_layout = QVBoxLayout(log_box); self.logs = QPlainTextEdit(); self.logs.setReadOnly(True); self.logs.setMinimumHeight(150); log_layout.addWidget(self.logs); root.addWidget(log_box, 1); footer = QHBoxLayout(); footer.addStretch(); self.open_folder = QPushButton("打开保存文件夹"); self.open_folder.setEnabled(False); self.open_folder.clicked.connect(self._open_folder); footer.addWidget(self.open_folder); root.addLayout(footer)
    def _style(self) -> None:
        self.setStyleSheet("QMainWindow,QWidget{background:#f5f6f8;color:#1d1d1f} QGroupBox{background:white;border:1px solid #e1e4e9;border-radius:14px;margin-top:10px;padding-top:12px;font-weight:600} QGroupBox::title{left:14px;padding:0 4px} QLabel#title{font-size:28px;font-weight:700} QLineEdit,QPlainTextEdit,QComboBox{background:#fbfbfc;border:1px solid #dfe2e8;border-radius:10px;padding:9px} QPushButton{background:#edf0f5;border:none;border-radius:10px;padding:10px 16px;font-weight:600} QPushButton#primary{background:#2775e8;color:white} QPushButton:disabled{color:#a8abb2;background:#eceef2} QProgressBar{border:none;border-radius:6px;background:#e8ebf1;height:14px;text-align:center} QProgressBar::chunk{background:#2775e8;border-radius:6px}")
    def _mode_key(self) -> str: return "batch" if self.mode.currentIndex() else "single"
    def _log(self, text: str) -> None:
        line = f"[{datetime.now():%H:%M:%S}] {text}"; self.mode_logs.append(self._mode_key(), line); self.logs.appendPlainText(line); self.logs.verticalScrollBar().setValue(self.logs.verticalScrollBar().maximum())
    def _show_mode_log(self) -> None:
        self.logs.setPlainText(self.mode_logs.text(self._mode_key())); self.logs.verticalScrollBar().setValue(self.logs.verticalScrollBar().maximum())
    def _mode_changed(self, index: int) -> None: self.url.setPlaceholderText("粘贴 douyin.com 博主主页分享链接" if index else "粘贴 douyin.com 作品分享链接"); self._show_mode_log(); self._reset()
    def _reset(self, *_args) -> None:
        if self.thread: return
        self.single = None; self.profile = None; self.selected = []; self.start.setEnabled(False); self.select_works.setEnabled(False); self.open_folder.setEnabled(False); self.status.setText("未识别"); self.author.setText("—"); self.summary.setText("—"); self.detail.setText("识别成功后显示作品信息"); self.progress.setValue(0); self.step.setText("等待识别")
    def _set_busy(self, busy: bool) -> None: self.mode.setEnabled(not busy); self.url.setEnabled(not busy); self.recognize.setEnabled(not busy); self.select_works.setEnabled(not busy and self.profile is not None); self.start.setEnabled(not busy and bool(self.single or self.selected))
    def _recognize(self) -> None:
        if not self.url.text().strip(): self.status.setText("失败：请输入链接"); return
        self._reset(); self.status.setText("识别中…"); self.step.setText("准备识别"); self.progress.setValue(5); self._log("开始识别链接"); self._run(RecognitionWorker("batch" if self.mode.currentIndex() else "single", self.url.text()))
    def _start(self) -> None:
        root = Path(__file__).resolve().parent / "output"; self.open_folder.setEnabled(False)
        if self.single: self._log("开始保存单条作品"); self._run(SaveWorker(self.single, root))
        else: self._log(f"开始批量任务：{len(self.selected)} 项"); self._run(BatchWorker(self.selected, root))
    def _run(self, worker: QObject) -> None:
        self._set_busy(True); thread = QThread(self); worker.moveToThread(thread); self.thread = thread; self.worker = worker; thread.started.connect(worker.run); worker.finished.connect(thread.quit); worker.finished.connect(worker.deleteLater); thread.finished.connect(thread.deleteLater); thread.finished.connect(self._finished)
        if isinstance(worker, RecognitionWorker): worker.progress.connect(self._progress_text); worker.succeeded.connect(self._recognized); worker.failed.connect(self._failed)
        elif isinstance(worker, SaveWorker): worker.progress.connect(self._save_progress); worker.succeeded.connect(self._saved); worker.failed.connect(self._failed)
        else: worker.progress.connect(self._batch_progress); worker.log.connect(self._log); worker.succeeded.connect(self._batch_done); worker.failed.connect(self._failed)
        thread.start()
    @Slot(str)
    def _progress_text(self, text: str) -> None: self.step.setText(text); self._log(text)
    @Slot(int, str)
    def _save_progress(self, value: int, text: str) -> None: self.progress.setValue(value); self.step.setText(text); self._log(text)
    @Slot(object)
    def _recognized(self, result: object) -> None:
        self.progress.setValue(100)
        if isinstance(result, VideoInfo): self.single = result; self.author.setText(result.author); self.summary.setText(result.title); self.detail.setText(result.content[:240] + ("…" if len(result.content) > 240 else "")); self.status.setText("成功"); self.step.setText("识别完成，可以开始任务"); self._log(f"识别成功：{result.author} / {result.title}")
        else:
            self.profile = result; self.author.setText(result.author); self.summary.setText(f"识别到 {len(result.works)} 个公开作品"); self.detail.setText("请选择要采集的作品"); self.status.setText("成功"); self.select_works.setEnabled(True); self._log(f"主页识别成功：{result.author}，{len(result.works)} 项"); self._choose_works()
    def _choose_works(self) -> None:
        if not self.profile: return
        dialog = WorksSelectionDialog(self.profile, self)
        if dialog.exec() == QDialog.Accepted:
            self.selected = dialog.selected(); self.summary.setText(f"识别到 {len(self.profile.works)} 个公开作品，已选择 {len(self.selected)} 项"); self.detail.setText("已选择作品，点击“开始任务”后顺序采集"); self.step.setText("选择完成，可以开始任务"); self.select_works.setText(f"重新选择（已选 {len(self.selected)}）"); self.start.setEnabled(True)
        else: self.step.setText("可点击“选择作品”重新打开")
    @Slot(int, int, str, str)
    def _batch_progress(self, index: int, total: int, title: str, step: str) -> None: self.progress.setValue(int((index - 1) * 100 / total)); self.step.setText(f"当前 {index}/{total}：{_short(title, 36)} — {step}"); self._log(self.step.text())
    @Slot(object)
    def _saved(self, result: object) -> None: self.output_dir, txt, when = result; self.progress.setValue(100); self.status.setText("成功"); self.step.setText("任务完成"); self.open_folder.setEnabled(True); self._log(f"已保存：{txt.name}（{when}）")
    @Slot(object)
    def _batch_done(self, result: object) -> None:
        folder, success, failed, elapsed = result; self.output_dir = folder; self.progress.setValue(100); self.status.setText("完成" if not failed else "完成（含失败项）"); self.step.setText(f"批量完成：成功 {success}，失败 {failed}"); self.open_folder.setEnabled(folder is not None); self._log(f"批量结束：成功 {success}，失败 {failed}，耗时 {elapsed:.1f} 秒")
    @Slot(str)
    def _failed(self, message: str) -> None: self.status.setText("失败"); self.step.setText("任务失败，可修正后重试"); self.progress.setValue(0); self._log(f"错误：{message}")
    @Slot()
    def _finished(self) -> None: self.thread = None; self.worker = None; self._set_busy(False)
    def _open_folder(self) -> None:
        if self.output_dir and self.output_dir.exists(): QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output_dir)))


def _short(value: str, size: int) -> str: return value[:size] + ("…" if len(value) > size else "")
def run() -> int:
    app = QApplication(sys.argv); app.setApplicationName("Vidpick"); window = MainWindow(); window.show(); return app.exec()
if __name__ == "__main__": raise SystemExit(run())
