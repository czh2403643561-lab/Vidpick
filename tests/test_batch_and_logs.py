from pathlib import Path

import pytest

import main
from douyin_parser import ProfileWork, VideoInfo
from storage import AssetState, CollectionOptions, MediaSaveResult, SelectedSaveResult
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox


def test_mode_logs_do_not_mix() -> None:
    logs = main.ModeLogStore()
    logs.append("single", "single message")
    logs.append("batch", "batch message")

    assert logs.text("single") == "single message"
    assert logs.text("batch") == "batch message"


def test_collection_options_persist_and_settings_require_one_choice(tmp_path) -> None:
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat)
    main._save_collection_options(settings, CollectionOptions(text=False, images=True))
    assert main._load_collection_options(settings) == CollectionOptions(text=False, images=True)

    app = QApplication.instance() or QApplication([])
    dialog = main.SettingsDialog(CollectionOptions())
    dialog.text.setChecked(False)
    dialog.images.setChecked(False)
    assert not dialog.buttons.button(QDialogButtonBox.Ok).isEnabled()
    dialog.text.setChecked(True)
    assert dialog.buttons.button(QDialogButtonBox.Ok).isEnabled()
    dialog.close()


def test_collection_status_reflects_options_refreshes_and_survives_mode_switch(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = main.MainWindow()
    window.app_settings = QSettings(str(tmp_path / "window-settings.ini"), QSettings.IniFormat)

    assert not window.collection_status.isHidden()
    assert window.collection_text_state.text() == ("✓" if window.options.text else "未选")
    assert window.collection_images_state.text() == ("✓" if window.options.images else "未选")

    class FakeSettingsDialog:
        def __init__(self, *_args, **_kwargs):
            pass

        def exec(self):
            return QDialog.Accepted

        def options(self):
            return CollectionOptions(text=False, images=True)

    monkeypatch.setattr(main, "SettingsDialog", FakeSettingsDialog)
    window._edit_settings()
    assert window.collection_text_state.text() == "未选"
    assert window.collection_images_state.text() == "✓"

    window.mode_buttons["batch"].click()
    window.mode_buttons["single"].click()
    assert not window.collection_status.isHidden()
    window.close()


def test_result_fields_and_card_spacing_are_aligned() -> None:
    app = QApplication.instance() or QApplication([])
    window = main.MainWindow()

    assert {label.width() for label in window.result_field_labels} == {44}
    assert [label.text() for label in window.result_field_labels] == ["状态", "博主", "作品", "类型", "详情"]
    for card in (window.findChild(main.QGroupBox, "link_card"), window.findChild(main.QGroupBox, "result_card"), window.task_card, window.log_card):
        assert card.title() == ""
        assert card.findChild(main.QLabel, "card_title").height() == 20
    assert window.task_card.layout().contentsMargins().left() == 12
    assert window.log_card.layout().contentsMargins().left() == 12
    window.close()


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
    window.mode_buttons["batch"].click()
    assert not window.select_works.isHidden()
    window.mode_buttons["single"].click()
    assert window.select_works.isHidden()
    assert "QPushButton#primary:disabled" in window.styleSheet()
    assert "QLabel{background:transparent}" in window.styleSheet()

    window.url.setText("https://www.douyin.com/video/123")
    monkeypatch.setattr(window, "_run", lambda _worker: None)
    window._recognize()

    assert not window.recognize.isEnabled()
    assert not window.paste_button.isEnabled()
    assert not window.url.isEnabled()
    assert all(not button.isEnabled() for button in window.mode_buttons.values())
    window.close()


def test_clipboard_paste_replaces_input_in_both_modes() -> None:
    app = QApplication.instance() or QApplication([])
    window = main.MainWindow()
    clipboard = app.clipboard()
    original = clipboard.text()
    try:
        assert not window.paste_button.icon().isNull()
        clipboard.setText("  abc [https://www.douyin.com/](https://www.douyin.com/) xyz  ")
        window.url.setText("旧链接")
        window.paste_button.click()
        assert window.url.text() == "abc [https://www.douyin.com/](https://www.douyin.com/) xyz"

        window.mode_buttons["batch"].click()
        clipboard.setText("博主主页内容")
        window.url.setText("另一个旧链接")
        window.paste_button.click()
        assert window.url.text() == "博主主页内容"

        window.mode_buttons["single"].click()
        clipboard.clear()
        window.url.setText("待清空")
        window.paste_button.click()
        assert window.url.text() == ""
    finally:
        clipboard.setText(original)
        window.close()


def test_pasting_new_link_clears_previous_single_result_and_preview() -> None:
    app = QApplication.instance() or QApplication([])
    window = main.MainWindow()
    clipboard = app.clipboard()
    original = clipboard.text()
    try:
        window._recognized(VideoInfo("测试博主", "旧作品", "正文", "https://www.douyin.com/video/1", "1"))
        assert window.single is not None
        assert window.preview_image.text() == "预览不可用"

        clipboard.setText("https://www.douyin.com/video/2")
        window.paste_button.click()

        assert window.single is None
        assert window.status.text() == "未识别"
        assert window.summary.text() == "—"
        assert window.preview_image.text() == "暂无预览"
        assert not window.start.isEnabled()
    finally:
        clipboard.setText(original)
        window.close()


def test_startup_log_is_empty_and_internal_progress_is_not_logged() -> None:
    app = QApplication.instance() or QApplication([])
    window = main.MainWindow()

    assert window.logs.toPlainText() == ""
    window._progress_text("打开作品页面")
    window._save_progress(40, "写入作品内容")
    window._batch_progress(1, 1, "作品", "解析正文")
    assert window.logs.toPlainText() == ""

    window._recognized(VideoInfo("测试博主", "作品", "正文", "https://www.douyin.com/video/1", "1"))
    assert "识别成功：测试博主 / 作品" in window.logs.toPlainText()
    window.close()


def test_mode_logs_remain_independent_and_window_has_icon() -> None:
    app = QApplication.instance() or QApplication([])
    window = main.MainWindow()

    assert not window.windowIcon().isNull()
    window._log("单个日志")
    window.mode_buttons["batch"].click()
    assert window.logs.toPlainText() == ""
    window._log("批量日志")
    window.mode_buttons["single"].click()
    assert window.logs.toPlainText().endswith("单个日志")
    window.close()


def test_single_mode_shows_media_type_and_preview_card_but_batch_hides_it() -> None:
    app = QApplication.instance() or QApplication([])
    window = main.MainWindow()
    info = VideoInfo(
        "测试博主", "图文作品", "正文", "https://www.douyin.com/note/1", "1",
        work_type="image", image_urls=("https://image/1.webp", "https://image/2.webp"),
    )

    window._recognized(info)

    assert window.work_type.text() == "图文 · 2 张"
    assert not window.preview_card.isHidden()
    assert window.preview_image.text() == "预览不可用"
    window.mode_buttons["batch"].click()
    assert window.preview_card.isHidden()
    window.mode_buttons["single"].click()
    assert not window.preview_card.isHidden()
    window.close()


def test_single_preview_uses_the_same_clean_cover_url_as_image_data(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = main.MainWindow()
    preview_urls: list[str] = []
    monkeypatch.setattr(window, "_load_preview", preview_urls.append)
    info = VideoInfo(
        "测试博主", "图文作品", "正文", "https://www.douyin.com/note/1", "1",
        work_type="image", cover_url="https://clean.example/01.webp",
        image_urls=("https://clean.example/01.webp", "https://clean.example/02.webp"), image_total=2,
    )

    window._recognized(info)

    assert info.cover_url == info.image_urls[0]
    assert preview_urls == ["https://clean.example/01.webp"]
    window.close()


def test_single_save_log_reports_partial_image_download() -> None:
    app = QApplication.instance() or QApplication([])
    window = main.MainWindow()
    window.options = CollectionOptions()
    window.single = VideoInfo(
        "测试博主", "图文作品", "正文", "https://www.douyin.com/note/1", "1",
        work_type="image", image_urls=("https://image/1.webp", "https://image/2.webp"),
    )

    window._saved(SelectedSaveResult(Path("output") / "测试博主", Path("output") / "测试博主" / "作品__1", AssetState(), AssetState(text=True), True, MediaSaveResult(total=2, saved=1, cover_saved=True), "2026-09-04"))

    assert "保存完成：图文作品 · 文案成功，无水印图片 1/2" in window.logs.toPlainText()
    window.close()


def test_single_save_log_marks_risky_only_image_source_as_unavailable() -> None:
    app = QApplication.instance() or QApplication([])
    window = main.MainWindow()
    window.options = CollectionOptions()
    window.single = VideoInfo(
        "测试博主", "图文作品", "正文", "https://www.douyin.com/note/1", "1",
        work_type="image", image_total=1,
    )

    window._saved(SelectedSaveResult(Path("output") / "测试博主", Path("output") / "测试博主" / "作品__1", AssetState(), AssetState(text=True), True, MediaSaveResult(total=1, saved=0), "2026-09-04"))

    assert "保存完成：图文作品 · 文案成功，未获取到可靠无水印图片" in window.logs.toPlainText()
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
    monkeypatch.setattr(main, "get_asset_state", lambda *_args: AssetState(text=True, images=True))
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
    window.options = CollectionOptions(text=True, images=False)
    window.selected = [
        ProfileWork("https://www.douyin.com/video/1", "", "旧", "1", "测试博主", "旧正文"),
        ProfileWork("https://www.douyin.com/video/2", "", "新", "2", "测试博主", "新正文"),
    ]
    started = []
    FakeMessageBox.choice = choice
    monkeypatch.setattr(main, "get_asset_state", lambda info, *_args: AssetState(text=info.aweme_id == "1"))
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

    def fake_save(info, root, options, overwrite=False, progress=None):
        folder = Path(root) / info.author
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{len(calls)}"
        path.write_text(info.content, encoding="utf-8")
        return SelectedSaveResult(folder, path, AssetState(), AssetState(text=True), True, MediaSaveResult(), "2026-01-01T00:00:00+08:00")

    monkeypatch.setattr(main, "DouyinSession", FakeSession)
    monkeypatch.setattr(main, "save_selected_assets", fake_save)
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


def test_batch_worker_saves_complete_profile_media_without_browser(monkeypatch, tmp_path) -> None:
    class UnexpectedSession:
        def __enter__(self):
            raise AssertionError("完整主页媒体不应启动浏览器")

    monkeypatch.setattr(main, "DouyinSession", UnexpectedSession)
    captured = []

    def fake_save(info, root, options, overwrite=False, progress=None):
        captured.append(("selected", info, overwrite))
        folder = Path(root) / info.author / "标题__1"
        folder.mkdir(parents=True)
        (folder / "content.txt").write_text(info.content, encoding="utf-8")
        image_dir = folder / "images"
        image_dir.mkdir()
        first = image_dir / "01.webp"
        first.write_bytes(b"clean")
        (image_dir / "02.webp").write_bytes(b"clean")
        (folder / "cover.webp").write_bytes(first.read_bytes())
        return SelectedSaveResult(folder.parent, folder, AssetState(), AssetState(text=True, images=True), True, MediaSaveResult(total=2, saved=2, cover_saved=True, newly_saved=2), "2026-01-01T00:00:00+08:00")

    monkeypatch.setattr(main, "save_selected_assets", fake_save)
    work = ProfileWork(
        "https://www.douyin.com/note/1", "https://clean.example/01.webp", "标题", "1", "博主", "完整正文",
        work_type="image", image_urls=("https://clean.example/01.webp", "https://clean.example/02.webp"), image_total=2,
    )
    worker = main.BatchWorker([work], tmp_path)
    completed = []
    logs = []
    worker.succeeded.connect(completed.append)
    worker.log.connect(logs.append)
    worker.run()

    folder, success, skipped, failed, _ = completed[0]
    assert folder.name == "博主"
    assert (success, skipped, failed) == (1, 0, 0)
    assert [kind for kind, *_ in captured] == ["selected"]
    saved_info = captured[0][1]
    assert saved_info.image_urls == work.image_urls
    assert saved_info.cover_url == work.image_urls[0]
    assert (folder / "标题__1" / "images" / "01.webp").exists()
    assert (folder / "标题__1" / "cover.webp").read_bytes() == b"clean"
    assert "保存成功 1/1：标题 · 文案 + 2 张图片" in logs


def test_batch_worker_fetches_detail_only_when_profile_media_is_incomplete(monkeypatch, tmp_path) -> None:
    calls = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def extract_video(self, url, progress=None):
            calls.append(url)
            return VideoInfo(
                "博主", "标题", "完整正文", url, "1", "image",
                "https://clean.example/01.webp", ("https://clean.example/01.webp", "https://clean.example/02.webp"), 2,
            )

    def fake_save(info, root, options, overwrite=False, progress=None):
        folder = Path(root) / info.author
        folder.mkdir(parents=True)
        path = folder / "作品__1"
        path.mkdir()
        (path / "content.txt").write_text(info.content, encoding="utf-8")
        return SelectedSaveResult(folder, path, AssetState(), AssetState(text=True), True, MediaSaveResult(total=2, saved=1, cover_saved=True, newly_saved=1), "2026-01-01T00:00:00+08:00")

    monkeypatch.setattr(main, "DouyinSession", FakeSession)
    monkeypatch.setattr(main, "save_selected_assets", fake_save)
    work = ProfileWork(
        "https://www.douyin.com/note/1", "https://clean.example/01.webp", "标题", "1", "博主", "主页正文",
        work_type="image", image_urls=("https://clean.example/01.webp",), image_total=2,
    )
    worker = main.BatchWorker([work], tmp_path)
    completed = []
    logs = []
    worker.succeeded.connect(completed.append)
    worker.log.connect(logs.append)
    worker.run()

    assert calls == [work.url]
    assert completed[0][1:4] == (1, 0, 0)
    assert "保存完成 1/1：标题 · 文案成功，无水印图片 1/2" in logs
