import json

from douyin_parser import VideoInfo
from storage import safe_filename, save_video


def test_safe_filename_handles_windows_names() -> None:
    cleaned = safe_filename('A<>:"/\\|?*', "fallback")
    assert cleaned.startswith("A")
    assert all(char not in cleaned for char in '<>:"/\\|?*')
    assert safe_filename("", "fallback") == "fallback"
    assert safe_filename("CON", "fallback").startswith("_")


def test_save_video_does_not_overwrite_txt(tmp_path) -> None:
    info = VideoInfo(
        author="测试博主",
        title="同名/作品",
        content="正文内容",
        url="https://www.douyin.com/video/123",
    )

    author_dir, first_txt, _ = save_video(info, tmp_path / "output")
    _, second_txt, _ = save_video(info, tmp_path / "output")

    assert author_dir.name == "测试博主"
    assert first_txt.name == "同名_作品.txt"
    assert second_txt.name == "同名_作品 (2).txt"
    assert first_txt.read_text(encoding="utf-8").startswith("作者：测试博主")
    records = [json.loads(line) for line in (author_dir / "data.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert records[0]["author"] == "测试博主"
    assert records[0]["content"] == "正文内容"
