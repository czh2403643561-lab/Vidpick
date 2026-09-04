from pathlib import Path

import pytest

import main
from douyin_parser import ProfileWork, VideoInfo
from PySide6.QtWidgets import QApplication


def test_mode_logs_do_not_mix() -> None:
    logs = main.ModeLogStore()
    logs.append("single", "single message")
    logs.append("batch", "batch message")

    assert logs.text("single") == "single message"
    assert logs.text("batch") == "batch message"


def test_recognize_does_not_start_a_second_worker_when_thread_exists(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = main.MainWindow()
    started = []
    monkeypatch.setattr(window, "_run", lambda worker: started.append(worker))
    window.thread = object()

    window._recognize()

    assert started == []
    window.close()


def test_single_mode_hides_selection_and_busy_recognize_button_is_disabled(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = main.MainWindow()
    assert window.select_works.isHidden()
    window.mode.setCurrentIndex(1)
    assert not window.select_works.isHidden()
    window.mode.setCurrentIndex(0)
    assert window.select_works.isHidden()
    assert "QPushButton#primary:disabled" in window.styleSheet()

    window.url.setText("https://www.douyin.com/video/123")
    monkeypatch.setattr(window, "_run", lambda _worker: None)
    window._recognize()

    assert not window.recognize.isEnabled()
    assert not window.url.isEnabled()
    assert not window.mode.isEnabled()
    window.close()


class FakeMessageBox:
    Warning = 1
    AcceptRole = 2
    DestructiveRole = 3
    RejectRole = 4
    choice = "cancel"

    def __init__(self, *_args, **_kwargs):
        self.buttons = []
        self.clicked = None

    def addButton(self, text, _role):
        button = text
        self.buttons.append(button)
        return button

    def setDefaultButton(self, _button):
        return None

    def exec(self):
        if self.choice == "overwrite":
            self.clicked = next(button for button in self.buttons if "覆盖" in button)
        elif self.choice == "skip":
            self.clicked = next(button for button in self.buttons if "跳过" in button)
        elif self.choice == "return":
            self.clicked = next(button for button in self.buttons if button == "返回")

    def clickedButton(self):
        return self.clicked


def test_single_duplicate_cancel_or_overwrite_is_handled_in_ui(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = main.MainWindow()
    window.single = VideoInfo("测试博主", "作品", "正文", "https://www.douyin.com/video/123", "123")
    started = []
    monkeypatch.setattr(main, "is_collected", lambda *_args: True)
    monkeypatch.setattr(main, "QMessageBox", FakeMessageBox)
    monkeypatch.setattr(window, "_run", lambda worker: started.append(worker))

    FakeMessageBox.choice = "cancel"
    window._start()
    assert started == []

    FakeMessageBox.choice = "overwrite"
    window._start()
    assert len(started) == 1
    assert started[0].overwrite is True
    window.close()


@pytest.mark.parametrize("choice, expected_ids, overwrite, skipped", [
    ("skip", ["2"], False, 1),
    ("overwrite", ["1", "2"], True, 0),
    ("return", [], False, 0),
])
def test_batch_duplicate_summary_actions(monkeypatch, choice, expected_ids, overwrite, skipped) -> None:
    app = QApplication.instance() or QApplication([])
    window = main.MainWindow()
    window.profile = main.ProfileInfo("测试博主", "https://www.douyin.com/user/test", ())
    window.selected = [
        ProfileWork("https://www.douyin.com/video/1", "", "旧", "1", "测试博主", "旧正文"),
        ProfileWork("https://www.douyin.com/video/2", "", "新", "2", "测试博主", "新正文"),
    ]
    started = []
    FakeMessageBox.choice = choice
    monkeypatch.setattr(main, "is_collected", lambda _author, aweme_id, _root: aweme_id == "1")
    monkeypatch.setattr(main, "QMessageBox", FakeMessageBox)
    monkeypatch.setattr(window, "_run", lambda worker: started.append(worker))

    window._start()

    assert len(started) == (0 if choice == "return" else 1)
    if started:
        assert [work.aweme_id for work in started[0].works] == expected_ids
        assert started[0].overwrite is overwrite
        assert started[0].skipped == skipped
    window.close()


def test_batch_worker_passes_progress_and_continues_after_one_failure(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def extract_video(self, url, progress=None):
            calls.append(url)
            if progress:
                progress("解析正文")
            if url.endswith("bad"):
                raise RuntimeError("页面不可用")
            return VideoInfo("测试博主", "测试作品", "正文", url)

    def fake_save(info, root):
        folder = Path(root) / info.author
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{len(calls)}.txt"
        path.write_text(info.content, encoding="utf-8")
        return folder, path, "2026-01-01T00:00:00+08:00"

    monkeypatch.setattr(main, "DouyinSession", FakeSession)
    monkeypatch.setattr(main, "save_video", fake_save)
    worker = main.BatchWorker(
        [
            ProfileWork("https://www.douyin.com/video/good-1", "", "第一项"),
            ProfileWork("https://www.douyin.com/video/bad", "", "失败项"),
            ProfileWork("https://www.douyin.com/video/good-2", "", "第三项"),
        ],
        tmp_path,
    )
    completed = []
    worker.succeeded.connect(completed.append)
    worker.run()

    assert calls == [
        "https://www.douyin.com/video/good-1",
        "https://www.douyin.com/video/bad",
        "https://www.douyin.com/video/good-2",
    ]
    folder, success, skipped, failed, _ = completed[0]
    assert folder == tmp_path / "测试博主"
    assert (success, skipped, failed) == (2, 0, 1)


def test_batch_worker_saves_profile_desc_without_browser(monkeypatch, tmp_path) -> None:
    class UnexpectedSession:
        def __enter__(self):
            raise AssertionError("已有正文不应启动浏览器")

    monkeypatch.setattr(main, "DouyinSession", UnexpectedSession)
    worker = main.BatchWorker([ProfileWork("https://www.douyin.com/note/1", "", "标题", "1", "博主", "完整正文")], tmp_path)
    completed = []
    worker.succeeded.connect(completed.append)
    worker.run()

    folder, success, skipped, failed, _ = completed[0]
    assert folder.name == "博主"
    assert (success, skipped, failed) == (1, 0, 0)
