import json

import pytest

from douyin_parser import VideoInfo
from storage import AlreadyCollectedError, get_author_dir, get_work_dir, is_collected, save_video, safe_filename


def test_safe_filename_handles_windows_names() -> None:
    cleaned = safe_filename('A<>:"/\\|?*', "fallback")
    assert cleaned.startswith("A")
    assert all(char not in cleaned for char in '<>:"/\\|?*')
    assert safe_filename("", "fallback") == "fallback"
    assert safe_filename("CON", "fallback").startswith("_")


def test_save_video_uses_id_work_folder_and_preserves_old_flat_file(tmp_path) -> None:
    output = tmp_path / "output"
    author_dir = get_author_dir("测试博主", output)
    author_dir.mkdir(parents=True)
    old_flat = author_dir / "旧版作品.txt"
    old_flat.write_text("旧内容", encoding="utf-8")
    info = VideoInfo("测试博主", "同名/作品" * 20, "正文内容", "https://www.douyin.com/video/123", "123")

    saved_author, content_path, _ = save_video(info, output)

    assert saved_author == author_dir.resolve()
    assert content_path == get_work_dir("测试博主", info.title, "123", output).resolve() / "content.txt"
    assert content_path.name == "content.txt"
    assert "__123" in content_path.parent.name
    assert len(content_path.parent.name) <= 100
    assert content_path.read_text(encoding="utf-8") == "正文内容\n"
    assert old_flat.read_text(encoding="utf-8") == "旧内容"
    assert list(author_dir.glob("*.txt")) == [old_flat]

    record = json.loads((author_dir / "data.jsonl").read_text(encoding="utf-8"))
    assert {"aweme_id", "author", "title", "url", "content", "collected_at"} <= record.keys()


def test_duplicate_save_requires_overwrite_and_upserts_one_record(tmp_path) -> None:
    output = tmp_path / "output"
    info = VideoInfo("测试博主", "同名作品", "第一次", "https://www.douyin.com/video/123", "123")
    _, first_path, _ = save_video(info, output)

    with pytest.raises(AlreadyCollectedError):
        save_video(info, output)

    _, second_path, _ = save_video(VideoInfo("测试博主", "改后标题", "第二次", info.url, "123"), output, overwrite=True)
    records = [json.loads(line) for line in (output / "测试博主" / "data.jsonl").read_text(encoding="utf-8").splitlines()]

    assert second_path == first_path
    assert second_path.read_text(encoding="utf-8") == "第二次\n"
    assert not list((output / "测试博主").glob("* (2).txt"))
    assert len([record for record in records if record["aweme_id"] == "123"]) == 1
    assert records[0]["title"] == "改后标题"


def test_legacy_jsonl_url_is_used_for_duplicate_detection_without_deleting_unknown_history(tmp_path) -> None:
    output = tmp_path / "output"
    author_dir = get_author_dir("测试博主", output)
    author_dir.mkdir(parents=True)
    (author_dir / "data.jsonl").write_text(
        json.dumps({"author": "测试博主", "title": "旧作品", "url": "https://www.douyin.com/user/self?vid=123"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"author": "测试博主", "title": "无法确认", "url": "https://www.douyin.com/user/self"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    assert is_collected("测试博主", "123", output)
    assert not is_collected("测试博主", "999", output)

    save_video(VideoInfo("测试博主", "更新作品", "新正文", "https://www.douyin.com/video/123", "123"), output, overwrite=True)
    lines = (author_dir / "data.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert len(records) == 2
    assert any(record.get("url") == "https://www.douyin.com/user/self" for record in records)
    assert sum(record.get("aweme_id") == "123" for record in records) == 1
