"""Vidpick desktop application."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys

from PySide6.QtCore import QObject, QThread, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from douyin_parser import DouyinParseError, VideoInfo, extract_video
from storage import save_video


class RecognitionWorker(QObject):
    progress = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url

    @Slot()
    def run(self) -> None:
        try:
            info = extract_video(self.url, self.progress.emit)
            self.succeeded.emit(info)
        except Exception as exc:
            self.failed.emit(str(exc) or "识别失败，请稍后重试")
        finally:
            self.finished.emit()


class SaveWorker(QObject):
    progress = Signal(int, str)
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, info: VideoInfo, output_root: Path) -> None:
        super().__init__()
        self.info = info
        self.output_root = output_root

    @Slot()
    def run(self) -> None:
        try:
            self.progress.emit(35, "准备输出目录")
            self.progress.emit(65, "写入 TXT 文案")
            author_dir, txt_path, collected_at = save_video(self.info, self.output_root)
            self.progress.emit(90, "追加 JSONL 记录")
            self.succeeded.emit((author_dir, txt_path, collected_at))
        except Exception as exc:
            self.failed.emit(str(exc) or "保存失败，请检查输出目录权限")
        finally:
            self.finished.emit()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._recognized: VideoInfo | None = None
        self._last_output_dir: Path | None = None
        self._thread: QThread | None = None
        self._worker: QObject | None = None
        self._build_ui()
        self._apply_style()
        self._append_log("应用已启动，等待输入抖音链接")

    def _build_ui(self) -> None:
        self.setWindowTitle("Vidpick")
        self.setMinimumSize(760, 680)
        self.resize(900, 760)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(32, 28, 32, 28)
        root.setSpacing(18)

        title_row = QHBoxLayout()
        title_block = QVBoxLayout()
        title = QLabel("Vidpick")
        title.setObjectName("appTitle")
        subtitle = QLabel("本地抖音文案采集")
        subtitle.setObjectName("subTitle")
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        title_row.addLayout(title_block)
        title_row.addStretch()
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["单个链接", "批量链接（暂未启用）"])
        self.mode_combo.setToolTip("当前仅支持单个链接采集")
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        title_row.addWidget(self.mode_combo)
        root.addLayout(title_row)

        input_group = QGroupBox("链接")
        input_layout = QGridLayout(input_group)
        input_layout.setContentsMargins(18, 18, 18, 18)
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("粘贴 douyin.com 分享链接或短链接")
        self.url_input.textChanged.connect(self._input_changed)
        self.recognize_button = QPushButton("识别")
        self.recognize_button.setObjectName("primaryButton")
        self.recognize_button.clicked.connect(self._recognize)
        input_layout.addWidget(self.url_input, 0, 0)
        input_layout.addWidget(self.recognize_button, 0, 1)
        root.addWidget(input_group)

        result_group = QGroupBox("识别结果")
        result_layout = QFormLayout(result_group)
        result_layout.setContentsMargins(18, 18, 18, 18)
        result_layout.setHorizontalSpacing(20)
        self.result_status = QLabel("未识别")
        self.result_status.setObjectName("statusNeutral")
        self.author_value = QLabel("—")
        self.author_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.title_value = QLabel("—")
        self.title_value.setWordWrap(True)
        self.title_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.final_url_value = QLabel("—")
        self.final_url_value.setWordWrap(True)
        self.final_url_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.content_preview = QLabel("识别成功后显示正文摘要")
        self.content_preview.setObjectName("mutedText")
        self.content_preview.setWordWrap(True)
        self.content_preview.setMaximumHeight(72)
        result_layout.addRow("状态", self.result_status)
        result_layout.addRow("博主", self.author_value)
        result_layout.addRow("标题 / 摘要", self.title_value)
        result_layout.addRow("最终链接", self.final_url_value)
        result_layout.addRow("正文", self.content_preview)
        root.addWidget(result_group)

        task_group = QGroupBox("任务")
        task_layout = QVBoxLayout(task_group)
        task_layout.setContentsMargins(18, 18, 18, 18)
        task_top = QHBoxLayout()
        self.step_label = QLabel("等待识别")
        self.step_label.setObjectName("stepLabel")
        task_top.addWidget(self.step_label)
        task_top.addStretch()
        self.start_button = QPushButton("开始任务")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self._start_task)
        task_top.addWidget(self.start_button)
        task_layout.addLayout(task_top)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        task_layout.addWidget(self.progress_bar)
        root.addWidget(task_group)

        log_group = QGroupBox("状态 / 日志")
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(18, 18, 18, 18)
        self.log_area = QPlainTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMinimumHeight(130)
        self.log_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        log_layout.addWidget(self.log_area)
        root.addWidget(log_group, 1)

        bottom_row = QHBoxLayout()
        bottom_row.addStretch()
        self.open_folder_button = QPushButton("打开保存文件夹")
        self.open_folder_button.setEnabled(False)
        self.open_folder_button.clicked.connect(self._open_folder)
        bottom_row.addWidget(self.open_folder_button)
        root.addLayout(bottom_row)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #f5f6f8; color: #1d1d1f; }
            QGroupBox { background: white; border: 1px solid #e4e6eb; border-radius: 14px; margin-top: 10px; padding-top: 12px; font-weight: 600; }
            QGroupBox::title { subcontrol-origin: margin; left: 16px; padding: 0 5px; color: #63666f; }
            QLabel#appTitle { font-size: 28px; font-weight: 700; color: #111216; }
            QLabel#subTitle, QLabel#mutedText { color: #777b85; }
            QLabel#stepLabel { color: #535762; font-weight: 600; }
            QLabel#statusNeutral { color: #777b85; }
            QLineEdit, QPlainTextEdit, QComboBox { background: #fbfbfc; border: 1px solid #dfe2e8; border-radius: 10px; padding: 9px 11px; }
            QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus { border: 1px solid #7d9df2; }
            QPushButton { background: #eef1f7; border: none; border-radius: 10px; padding: 10px 18px; font-weight: 600; }
            QPushButton:hover { background: #e0e6f5; }
            QPushButton:disabled { color: #a8abb2; background: #eceef2; }
            QPushButton#primaryButton, QPushButton:default { background: #2775e8; color: white; }
            QPushButton#primaryButton:hover { background: #1f63c8; }
            QProgressBar { background: #e8ebf1; border: none; border-radius: 6px; height: 12px; text-align: center; color: #525761; }
            QProgressBar::chunk { background: #2775e8; border-radius: 6px; }
            """
        )

    def _append_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_area.appendPlainText(f"[{stamp}] {message}")
        scrollbar = self.log_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    @Slot(str)
    def _worker_progress(self, message: str) -> None:
        self.step_label.setText(message)
        self._append_log(message)

    @Slot(int, str)
    def _save_progress(self, percent: int, message: str) -> None:
        self.progress_bar.setValue(percent)
        self.step_label.setText(message)
        self._append_log(message)

    def _set_busy(self, busy: bool) -> None:
        self.mode_combo.setEnabled(not busy)
        self.url_input.setEnabled(not busy and self.mode_combo.currentIndex() == 0)
        self.recognize_button.setEnabled(not busy and self.mode_combo.currentIndex() == 0)
        self.start_button.setEnabled(not busy and self._recognized is not None and self.mode_combo.currentIndex() == 0)

    def _input_changed(self, _value: str) -> None:
        if self._thread is not None:
            return
        self._recognized = None
        self.start_button.setEnabled(False)
        self.open_folder_button.setEnabled(False)
        self.result_status.setText("未识别")
        self.result_status.setObjectName("statusNeutral")
        self.result_status.style().unpolish(self.result_status)
        self.result_status.style().polish(self.result_status)
        self.author_value.setText("—")
        self.title_value.setText("—")
        self.final_url_value.setText("—")
        self.content_preview.setText("识别成功后显示正文摘要")
        self.step_label.setText("等待识别")
        self.progress_bar.setValue(0)

    def _mode_changed(self, index: int) -> None:
        if index == 1:
            self._recognized = None
            self.start_button.setEnabled(False)
            self.recognize_button.setEnabled(False)
            self.url_input.setEnabled(False)
            self.result_status.setText("暂未启用")
            self.step_label.setText("批量模式暂未启用")
            self._append_log("批量模式暂未启用，本次仅支持单个链接")
        else:
            self.url_input.setEnabled(True)
            self.recognize_button.setEnabled(True)
            self._input_changed(self.url_input.text())

    def _recognize(self) -> None:
        if not self.url_input.text().strip():
            self.result_status.setText("失败：请输入链接")
            self._append_log("错误：请输入抖音作品链接")
            return
        self._recognized = None
        self._last_output_dir = None
        self.start_button.setEnabled(False)
        self.open_folder_button.setEnabled(False)
        self.result_status.setText("识别中…")
        self.author_value.setText("—")
        self.title_value.setText("—")
        self.final_url_value.setText("—")
        self.content_preview.setText("正在读取页面…")
        self.progress_bar.setValue(5)
        self.step_label.setText("准备识别")
        self._append_log("开始识别链接")
        worker = RecognitionWorker(self.url_input.text())
        self._run_worker(worker, worker.succeeded, worker.failed, worker.progress)

    @Slot(object)
    def _recognition_succeeded(self, info: VideoInfo) -> None:
        self._recognized = info
        self.progress_bar.setValue(100)
        self.step_label.setText("识别完成，可以开始任务")
        self.result_status.setText("成功")
        self.result_status.setObjectName("statusSuccess")
        self.result_status.style().unpolish(self.result_status)
        self.result_status.style().polish(self.result_status)
        self.author_value.setText(info.author)
        self.title_value.setText(info.title)
        self.final_url_value.setText(info.url)
        summary = info.content.replace("\n", " ")
        self.content_preview.setText(summary[:240] + ("…" if len(summary) > 240 else ""))
        self._append_log(f"识别成功：{info.author} / {info.title}")

    @Slot(str)
    def _recognition_failed(self, message: str) -> None:
        self._recognized = None
        self.progress_bar.setValue(0)
        self.step_label.setText("识别失败，请修正后重试")
        self.result_status.setText("失败")
        self.result_status.setObjectName("statusError")
        self.result_status.style().unpolish(self.result_status)
        self.result_status.style().polish(self.result_status)
        self.content_preview.setText(message)
        self._append_log(f"错误：{message}")

    def _start_task(self) -> None:
        if self._recognized is None:
            return
        self.progress_bar.setValue(10)
        self.step_label.setText("开始保存任务")
        self.result_status.setText("执行中…")
        self._append_log("识别结果有效，开始保存任务")
        output_root = Path(__file__).resolve().parent / "output"
        worker = SaveWorker(self._recognized, output_root)
        self._run_worker(worker, worker.succeeded, worker.failed, worker.progress)

    @Slot(object)
    def _save_succeeded(self, result: tuple[Path, Path, str]) -> None:
        author_dir, txt_path, collected_at = result
        self._last_output_dir = author_dir
        self.progress_bar.setValue(100)
        self.step_label.setText("任务完成")
        self.result_status.setText("成功")
        self.result_status.setObjectName("statusSuccess")
        self.result_status.style().unpolish(self.result_status)
        self.result_status.style().polish(self.result_status)
        self.open_folder_button.setEnabled(True)
        self._append_log(f"已保存：{txt_path.name}")
        self._append_log(f"任务完成：{author_dir}（{collected_at}）")

    @Slot(str)
    def _save_failed(self, message: str) -> None:
        self.progress_bar.setValue(0)
        self.step_label.setText("保存失败，请重试")
        self.result_status.setText("失败")
        self.result_status.setObjectName("statusError")
        self.result_status.style().unpolish(self.result_status)
        self.result_status.style().polish(self.result_status)
        self._append_log(f"错误：{message}")

    def _run_worker(self, worker: QObject, success_signal, failed_signal, progress_signal) -> None:
        if self._thread is not None:
            return
        self._set_busy(True)
        thread = QThread(self)
        worker.moveToThread(thread)
        self._thread = thread
        self._worker = worker
        if isinstance(worker, RecognitionWorker):
            success_signal.connect(self._recognition_succeeded)
            failed_signal.connect(self._recognition_failed)
            progress_signal.connect(self._worker_progress)
        else:
            success_signal.connect(self._save_succeeded)
            failed_signal.connect(self._save_failed)
            progress_signal.connect(self._save_progress)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._worker_finished)
        thread.start()

    @Slot()
    def _worker_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._set_busy(False)

    def _open_folder(self) -> None:
        if self._last_output_dir and self._last_output_dir.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_output_dir)))
            self._append_log("已打开本次保存文件夹")
        else:
            self._append_log("保存文件夹不存在，请先完成一次任务")


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Vidpick")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())
