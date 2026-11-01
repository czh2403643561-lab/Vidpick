"""Headless, one-video Vidpick entry point for Muse."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

from douyin_parser import extract_video, normalize_douyin_url
from storage import get_work_dir, save_original_video


BASE = Path(__file__).resolve().parent
OUTPUT = BASE / "output"
LOG = BASE / "logs" / "vidpick.log"
DEFAULT_BROWSER = Path("/opt/meta-chromium/chrome")


def share_url(text: str) -> str:
    """Find the first Douyin link in a URL or Chinese share message."""
    matches = re.findall(r"https?://[^\s\]\)<>，。；;]+", text)
    for candidate in matches:
        candidate = candidate.rstrip(".,!?）】\"'")
        host = (urlparse(candidate).hostname or "").lower().rstrip(".")
        if host == "douyin.com" or host.endswith(".douyin.com"):
            return normalize_douyin_url(candidate)
    raise ValueError("没有找到抖音链接，请发送完整的分享链接或分享文案")


def duration_seconds(path: Path) -> float | None:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=12, check=True,
        )
        return round(float(result.stdout.strip()), 3)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def download(raw_text: str) -> Path:
    url = share_url(raw_text)
    info = extract_video(url)
    if info.work_type != "video":
        raise ValueError("这个链接是图文作品，没有可下载的 mp4 视频")
    if not info.aweme_id:
        raise ValueError("没有识别到视频 ID，请确认链接仍可访问")
    work_dir = get_work_dir(info.author, info.title, info.aweme_id, OUTPUT)
    work_dir.mkdir(parents=True, exist_ok=True)
    video = work_dir / "video.mp4"
    if not video.is_file() or video.stat().st_size == 0:
        if not save_original_video(info, work_dir):
            raise ValueError("视频下载失败：播放地址不可用或 cookies 已过期")
    duration = duration_seconds(video)
    if duration is None or duration <= 0:
        raise ValueError("视频已下载，但 ffprobe 无法读取时长；请检查 ffmpeg 安装或视频文件")
    metadata = {
        "标题": info.title,
        "作者": info.author,
        "视频ID": info.aweme_id,
        "时长": duration,
        "原始链接": url,
        "文件大小": video.stat().st_size,
    }
    metadata_path = video.with_suffix(".json")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=work_dir, suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(metadata_path)
    return video.resolve()


def worker(args: argparse.Namespace) -> int:
    cache_dir = BASE / ".cache"
    (cache_dir / "tmp").mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TMPDIR", str(cache_dir / "tmp"))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache_dir))
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BASE / ".cache" / "ms-playwright"))
    browser = args.browser or os.environ.get("VIDPICK_CHROMIUM", "")
    if not browser and DEFAULT_BROWSER.is_file():
        browser = str(DEFAULT_BROWSER)
    if browser:
        if not Path(browser).is_file():
            raise ValueError(f"找不到 Chromium：{browser}")
        os.environ["VIDPICK_CHROMIUM"] = browser
    os.environ["VIDPICK_PROFILE_DIR"] = str(BASE / "browser_profile")
    cookies = args.cookies or os.environ.get("VIDPICK_COOKIES", "")
    if cookies:
        if not Path(cookies).is_file():
            raise ValueError(f"找不到 cookies 文件：{cookies}")
        os.environ["VIDPICK_COOKIES"] = cookies
    print(download(args.share_text))
    return 0


def run(args: argparse.Namespace) -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if args.worker:
        try:
            return worker(args)
        except Exception as exc:
            print(str(exc) or "下载失败", file=sys.stderr)
            return 1
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--timeout", str(args.timeout)]
    if args.browser:
        command += ["--browser", args.browser]
    if args.cookies:
        command += ["--cookies", args.cookies]
    command += ["--", args.share_text]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"运行超时（{args.timeout} 秒）\n")
        print(f"下载超时（{args.timeout} 秒），请稍后重试；日志：{LOG}", file=sys.stderr)
        return 124
    if process.returncode:
        reason = stderr.strip().splitlines()[-1] if stderr.strip() else "下载失败，请检查链接或 cookies"
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(stderr or reason + "\n")
        print(f"{reason}；日志：{LOG}", file=sys.stderr)
        return process.returncode
    print(stdout.strip())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Vidpick Muse Linux：下载单个抖音视频")
    parser.add_argument("share_text", help="抖音链接或整段分享文案")
    parser.add_argument("--browser", help="Chromium 可执行文件路径")
    parser.add_argument("--cookies", help="Playwright cookies.json 路径")
    parser.add_argument("--timeout", type=int, default=180, help="总超时秒数，默认 180")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("超时必须至少为 1 秒")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
