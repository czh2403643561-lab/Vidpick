import json

import pytest

from douyin_parser import VideoInfo
import storage
from storage import AlreadyCollectedError, get_author_dir, get_work_dir, is_collected, save_media, save_video, safe_filename


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
    assert {"aweme_id", "author", "title", "url", "content", "work_type", "cover_url", "image_urls", "collected_at"} <= record.keys()


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


def test_save_image_media_rebuilds_only_managed_files_and_reuses_first_download(monkeypatch, tmp_path) -> None:
    work_dir = tmp_path / "测试博主" / "作品__123"
    images_dir = work_dir / "images"
    images_dir.mkdir(parents=True)
    (work_dir / "cover.jpg").write_bytes(b"old-cover")
    (images_dir / "01.jpg").write_bytes(b"old-1")
    (images_dir / "03.jpg").write_bytes(b"old-3")
    (work_dir / "future-file.bin").write_bytes(b"keep")
    calls: list[str] = []

    def fake_download(url: str, stem):
        calls.append(url)
        path = stem.with_suffix(".webp")
        path.write_bytes(url.encode())
        return path

    monkeypatch.setattr(storage, "_download_to_stem", fake_download)
    info = VideoInfo(
        "测试博主", "作品", "正文", "https://www.douyin.com/note/123", "123",
        work_type="image", image_urls=("https://image/one", "https://image/two"),
    )

    result = save_media(info, work_dir, overwrite=True)

    assert result.total == result.saved == 2
    assert result.cover_saved
    assert calls == ["https://image/one", "https://image/two"]
    assert sorted(path.name for path in (work_dir / "images").iterdir()) == ["01.webp", "02.webp"]
    assert (work_dir / "cover.webp").read_bytes() == b"https://image/one"
    assert (work_dir / "future-file.bin").read_bytes() == b"keep"


def test_media_download_failure_keeps_saved_content(monkeypatch, tmp_path) -> None:
    info = VideoInfo(
        "测试博主", "作品", "正文", "https://www.douyin.com/note/123", "123",
        work_type="image", image_urls=("https://image/one", "https://image/two"),
    )
    _, content_path, _ = save_video(info, tmp_path)

    def fake_download(url: str, stem):
        if url.endswith("two"):
            raise OSError("network failed")
        path = stem.with_suffix(".jpg")
        path.write_bytes(b"image")
        return path

    monkeypatch.setattr(storage, "_download_to_stem", fake_download)
    result = save_media(info, content_path.parent)

    assert (result.total, result.saved, result.cover_saved) == (2, 1, True)
    assert content_path.read_text(encoding="utf-8") == "正文\n"
    assert (content_path.parent / "images" / "01.jpg").exists()
    assert not (content_path.parent / "images" / "02.jpg").exists()


def test_save_video_media_only_downloads_cover(monkeypatch, tmp_path) -> None:
    work_dir = tmp_path / "测试博主" / "视频__456"
    work_dir.mkdir(parents=True)
    calls: list[str] = []

    def fake_download(url: str, stem):
        calls.append(url)
        path = stem.with_suffix(".jpg")
        path.write_bytes(b"cover")
        return path

    monkeypatch.setattr(storage, "_download_to_stem", fake_download)
    info = VideoInfo(
        "测试博主", "视频", "正文", "https://www.douyin.com/video/456", "456",
        work_type="video", cover_url="https://cover/video.jpg",
    )

    result = save_media(info, work_dir)

    assert (result.total, result.saved, result.cover_saved) == (1, 1, True)
    assert calls == ["https://cover/video.jpg"]
    assert (work_dir / "cover.jpg").exists()
    assert not (work_dir / "images").exists()
