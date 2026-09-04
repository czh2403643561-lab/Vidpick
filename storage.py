"""Small, safe filesystem writer for collected Douyin records."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import re

from douyin_parser import VideoInfo


def safe_filename(value: str, fallback: str, max_length: int = 100) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "")
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    if value.upper() in {"CON", "PRN", "AUX", "NUL"} or re.match(r"^(COM|LPT)[0-9]$", value.upper()):
        value = "_" + value
    return value[:max_length].rstrip(" .") or fallback


def _next_txt_path(author_dir: Path, title: str) -> Path:
    base = safe_filename(title, "未命名作品")
    candidate = author_dir / f"{base}.txt"
    number = 2
    while candidate.exists():
        candidate = author_dir / f"{base} ({number}).txt"
        number += 1
    return candidate


def save_video(info: VideoInfo, output_root: str | Path = "output") -> tuple[Path, Path, str]:
    """Write one non-overwriting TXT and append one JSONL record."""

    author_dir = Path(output_root) / safe_filename(info.author, "未命名博主")
    author_dir.mkdir(parents=True, exist_ok=True)
    txt_path = _next_txt_path(author_dir, info.title)
    collected_at = datetime.now().astimezone().isoformat(timespec="seconds")
    txt_content = (
        f"作者：{info.author}\n"
        f"标题：{info.title}\n"
        f"来源链接：{info.url}\n"
        f"采集时间：{collected_at}\n\n"
        f"{info.content}\n"
    )
    with txt_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(txt_content)

    record = asdict(info)
    record["collected_at"] = collected_at
    jsonl_path = author_dir / "data.jsonl"
    with jsonl_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return author_dir.resolve(), txt_path.resolve(), collected_at
