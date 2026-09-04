from pathlib import Path

import main
from douyin_parser import ProfileWork, VideoInfo


def test_mode_logs_do_not_mix() -> None:
    logs = main.ModeLogStore()
    logs.append("single", "single message")
    logs.append("batch", "batch message")

    assert logs.text("single") == "single message"
    assert logs.text("batch") == "batch message"


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
    folder, success, failed, _ = completed[0]
    assert folder == tmp_path / "测试博主"
    assert (success, failed) == (2, 1)
