import importlib.util
import json
from pathlib import Path

import pytest

from douyin_parser import VideoInfo


SPEC = importlib.util.spec_from_file_location("vidpick_cli", Path(__file__).resolve().parents[1] / "muse_linux" / "vidpick_cli.py")
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


def test_share_text_extracts_short_link_and_rejects_other_hosts():
    assert cli.share_url("看这个！ https://v.douyin.com/abc123/ 复制打开") == "https://v.douyin.com/abc123/"
    with pytest.raises(ValueError, match="没有找到抖音链接"):
        cli.share_url("https://example.com/video/123")


def test_download_writes_video_and_metadata_with_chinese(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "OUTPUT", tmp_path / "output")
    info = VideoInfo("测试博主", "中文标题", "正文", "https://www.douyin.com/video/123", "123", video_url="https://example.com/video.mp4")
    monkeypatch.setattr(cli, "extract_video", lambda _url: info)
    monkeypatch.setattr(cli, "duration_seconds", lambda _path: 12.5)

    def fake_save(_info, folder):
        (folder / "video.mp4").write_bytes(b"mock-video")
        return True

    monkeypatch.setattr(cli, "save_original_video", fake_save)
    video = cli.download("分享 https://www.douyin.com/video/123")
    metadata = json.loads(video.with_suffix(".json").read_text(encoding="utf-8"))
    assert video.is_absolute() and video.stat().st_size == 10
    assert metadata == {"标题": "中文标题", "作者": "测试博主", "视频ID": "123", "时长": 12.5, "原始链接": "https://www.douyin.com/video/123", "文件大小": 10}


def test_download_rejects_image_work(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "OUTPUT", tmp_path / "output")
    monkeypatch.setattr(cli, "extract_video", lambda _url: VideoInfo("博主", "图文", "正文", "https://www.douyin.com/note/123", "123", work_type="image"))
    with pytest.raises(ValueError, match="没有可下载的 mp4"):
        cli.download("https://www.douyin.com/note/123")
