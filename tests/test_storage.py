import json

import pytest

from douyin_parser import VideoInfo
import storage
from storage import AlreadyCollectedError, AssetState, CollectionOptions, get_asset_state, get_author_dir, get_work_dir, is_collected, save_media, save_selected_assets, save_video, safe_filename


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
    assert {"aweme_id", "author", "title", "url", "content", "work_type", "cover_url", "image_urls", "image_total", "collected_at"} <= record.keys()


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


def test_risky_only_image_source_is_reported_as_unavailable(tmp_path) -> None:
    info = VideoInfo(
        "测试博主", "作品", "正文", "https://www.douyin.com/note/123", "123",
        work_type="image", image_total=1,
    )

    result = save_media(info, tmp_path / "作品__123")

    assert (result.total, result.saved, result.cover_saved) == (1, 0, False)
    assert not (tmp_path / "作品__123" / "images").exists()


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


def test_selected_assets_support_text_only_and_images_only(monkeypatch, tmp_path) -> None:
    def fake_download(url, stem):
        path = stem.with_suffix(".webp")
        path.write_bytes(url.encode())
        return path

    monkeypatch.setattr(storage, "_download_to_stem", fake_download)
    image_info = VideoInfo(
        "测试博主", "图文", "正文", "https://www.douyin.com/note/1", "1",
        work_type="image", cover_url="https://clean/01", image_urls=("https://clean/01", "https://clean/02"), image_total=2,
    )
    text_result = save_selected_assets(image_info, tmp_path, CollectionOptions(text=True, images=False))
    assert (text_result.work_dir / "content.txt").exists()
    assert not (text_result.work_dir / "images").exists()
    assert not list(text_result.work_dir.glob("cover.*"))

    image_only = VideoInfo(
        "测试博主", "图文二", "不应写入", "https://www.douyin.com/note/2", "2",
        work_type="image", cover_url="https://clean/01", image_urls=("https://clean/01", "https://clean/02"), image_total=2,
    )
    image_result = save_selected_assets(image_only, tmp_path, CollectionOptions(text=False, images=True))
    assert not (image_result.work_dir / "content.txt").exists()
    assert len(list((image_result.work_dir / "images").iterdir())) == 2
    assert len(list(image_result.work_dir.glob("cover.*"))) == 1
    assert get_asset_state(image_only, tmp_path).images
    record = next(json.loads(line) for line in (image_result.author_dir / "data.jsonl").read_text(encoding="utf-8").splitlines() if '"2"' in line)
    assert record["saved_assets"] == {"text": False, "images": True, "video": False}
    assert "content" not in record


def test_selected_assets_fill_missing_without_rewriting_and_merge_jsonl(monkeypatch, tmp_path) -> None:
    downloads = []

    def fake_download(url, stem):
        downloads.append(url)
        path = stem.with_suffix(".webp")
        path.write_bytes(f"image-{len(downloads)}".encode())
        return path

    monkeypatch.setattr(storage, "_download_to_stem", fake_download)
    info = VideoInfo(
        "测试博主", "图文", "第一次正文", "https://www.douyin.com/note/1", "1",
        work_type="image", cover_url="https://clean/01", image_urls=("https://clean/01", "https://clean/02"), image_total=2,
    )
    first = save_selected_assets(info, tmp_path, CollectionOptions(text=True, images=False))
    second = save_selected_assets(info, tmp_path, CollectionOptions(text=True, images=True))
    assert second.before.text and not second.before.images
    assert not second.text_saved
    assert second.media.newly_saved == 2
    assert (second.work_dir / "content.txt").read_text(encoding="utf-8") == "第一次正文\n"

    image_bytes = (second.work_dir / "images" / "01.webp").read_bytes()
    updated = VideoInfo(
        "测试博主", "图文", "第二次正文", info.url, "1",
        work_type="image", cover_url=info.cover_url, image_urls=info.image_urls, image_total=2,
    )
    third = save_selected_assets(updated, tmp_path, CollectionOptions(text=True, images=True))
    assert third.before.text and third.before.images
    assert not third.text_saved and third.media.newly_saved == 0
    assert (third.work_dir / "images" / "01.webp").read_bytes() == image_bytes
    record = json.loads((third.author_dir / "data.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["content"] == "第一次正文"
    assert record["saved_assets"] == {"text": True, "images": True, "video": False}


def test_selected_overwrite_only_changes_requested_asset(monkeypatch, tmp_path) -> None:
    def fake_download(url, stem):
        path = stem.with_suffix(".webp")
        path.write_bytes(b"new-media")
        return path

    monkeypatch.setattr(storage, "_download_to_stem", fake_download)
    original = VideoInfo("测试博主", "图文", "旧正文", "https://www.douyin.com/note/1", "1", "image", "https://clean/01", ("https://clean/01",), 1)
    saved = save_selected_assets(original, tmp_path, CollectionOptions())
    old_image = (saved.work_dir / "images" / "01.webp").read_bytes()
    changed = VideoInfo("测试博主", "图文", "新正文", original.url, "1", "image", original.cover_url, original.image_urls, 1)
    text_overwrite = save_selected_assets(changed, tmp_path, CollectionOptions(text=True, images=False), overwrite=True)
    assert (text_overwrite.work_dir / "content.txt").read_text(encoding="utf-8") == "新正文\n"
    assert (text_overwrite.work_dir / "images" / "01.webp").read_bytes() == old_image
    media_overwrite = save_selected_assets(original, tmp_path, CollectionOptions(text=False, images=True), overwrite=True)
    assert (media_overwrite.work_dir / "content.txt").read_text(encoding="utf-8") == "新正文\n"
    assert (media_overwrite.work_dir / "images" / "01.webp").read_bytes() == b"new-media"


def test_asset_state_is_complete_only_for_requested_options() -> None:
    state = AssetState(text=True, images=False)
    assert state.is_complete_for(CollectionOptions(text=True, images=False))
    assert not state.is_complete_for(CollectionOptions(text=True, images=True))
    assert state.has_requested_asset(CollectionOptions(text=True, images=True))
    video_missing = AssetState(text=True, images=True, video=False)
    assert not video_missing.is_complete_for(CollectionOptions(text=True, images=True, video=True))
    assert video_missing.has_requested_asset(CollectionOptions(text=True, images=True, video=True))
    assert video_missing.is_complete_for(CollectionOptions(text=True, images=True, video=True), video_applicable=False)


def test_video_only_save_is_independent_from_text_and_cover(monkeypatch, tmp_path) -> None:
    calls = []

    def fake_video_download(url, destination):
        calls.append(url)
        destination.write_bytes(b"clean-video")

    monkeypatch.setattr(storage, "_download_video_to_path", fake_video_download)
    info = VideoInfo(
        "测试博主", "视频", "不应写入", "https://www.douyin.com/video/456", "456",
        work_type="video", cover_url="https://cover/video.jpg", video_url="https://video.example/clean.mp4",
    )

    result = save_selected_assets(info, tmp_path, CollectionOptions(text=False, images=False, video=True))

    assert calls == ["https://video.example/clean.mp4"]
    assert (result.work_dir / "video.mp4").read_bytes() == b"clean-video"
    assert not (result.work_dir / "content.txt").exists()
    assert not list(result.work_dir.glob("cover.*"))
    assert not (result.work_dir / "images").exists()
    assert result.after == AssetState(video=True)
    record = json.loads((result.author_dir / "data.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["saved_assets"] == {"text": False, "images": False, "video": True}


def test_video_asset_state_and_selective_overwrite(monkeypatch, tmp_path) -> None:
    versions = iter((b"first", b"second"))

    def fake_video_download(_url, destination):
        destination.write_bytes(next(versions))

    monkeypatch.setattr(storage, "_download_video_to_path", fake_video_download)
    info = VideoInfo("测试博主", "视频", "旧正文", "https://www.douyin.com/video/456", "456", work_type="video", cover_url="https://cover/video.jpg", video_url="https://video.example/clean.mp4")
    first = save_selected_assets(info, tmp_path, CollectionOptions(text=True, images=False, video=True))
    (first.work_dir / "cover.jpg").write_bytes(b"keep-cover")

    overwritten = save_selected_assets(VideoInfo("测试博主", "视频", "新正文", info.url, "456", work_type="video", cover_url=info.cover_url, video_url=info.video_url), tmp_path, CollectionOptions(text=False, images=False, video=True), overwrite=True)

    assert get_asset_state(info, tmp_path).video
    assert overwritten.before.video and overwritten.after.video and overwritten.video_newly_saved
    assert (overwritten.work_dir / "video.mp4").read_bytes() == b"second"
    assert (overwritten.work_dir / "content.txt").read_text(encoding="utf-8") == "旧正文\n"
    assert (overwritten.work_dir / "cover.jpg").read_bytes() == b"keep-cover"


def test_video_download_failure_keeps_existing_file_and_cleans_part(monkeypatch, tmp_path) -> None:
    destination = tmp_path / "video.mp4"
    destination.write_bytes(b"old-complete-video")

    class Headers:
        @staticmethod
        def get_content_type():
            return "video/mp4"

    class BrokenResponse:
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            if not hasattr(self, "read_once"):
                self.read_once = True
                return b"partial"
            raise OSError("network interrupted")

    monkeypatch.setattr(storage, "urlopen", lambda *_args, **_kwargs: BrokenResponse())
    with pytest.raises(OSError, match="network interrupted"):
        storage._download_video_to_path("https://video.example/clean.mp4", destination)

    assert destination.read_bytes() == b"old-complete-video"
    assert not list(tmp_path.glob("*.part"))


def test_collection_options_support_video_but_require_one_resource() -> None:
    assert CollectionOptions(text=False, images=False, video=True).video
    with pytest.raises(storage.StorageError, match="至少选择"):
        CollectionOptions(text=False, images=False, video=False)


def test_video_only_rejects_image_without_creating_a_work_directory(tmp_path) -> None:
    info = VideoInfo("测试博主", "图文", "正文", "https://www.douyin.com/note/1", "1", work_type="image")

    with pytest.raises(storage.StorageError, match="图文作品"):
        save_selected_assets(info, tmp_path, CollectionOptions(text=False, images=False, video=True))

    assert not (tmp_path / "测试博主").exists()
