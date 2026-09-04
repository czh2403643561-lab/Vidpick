"""Filesystem storage and duplicate detection for collected Douyin works."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

from douyin_parser import VideoInfo, extract_target_aweme_id


class StorageError(RuntimeError):
    """Raised when a work cannot be stored safely."""


class AlreadyCollectedError(StorageError):
    """Raised when a new save would overwrite an existing work."""


def safe_filename(value: str, fallback: str, max_length: int = 100) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "")
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    if value.upper() in {"CON", "PRN", "AUX", "NUL"} or re.match(r"^(COM|LPT)[0-9]$", value.upper()):
        value = "_" + value
    return value[:max_length].rstrip(" .") or fallback


def get_author_dir(author: str, output_root: str | Path = "output") -> Path:
    """Return the safe directory used by one author."""

    return Path(output_root) / safe_filename(author, "未命名博主")


def get_work_dir(author: str, title: str, aweme_id: str, output_root: str | Path = "output") -> Path:
    """Return the stable work directory, keeping the unique ID visible."""

    safe_id = safe_filename(str(aweme_id).strip(), "")
    if not safe_id:
        raise StorageError("作品缺少 aweme_id，无法安全保存")
    work_name = f"{safe_filename(title, '未命名作品', max_length=80)}__{safe_id}"
    return get_author_dir(author, output_root) / work_name


def aweme_id_from_record(record: dict[str, Any]) -> str:
    """Read an ID, including from legacy records that only stored a URL."""

    value = str(record.get("aweme_id", "")).strip()
    if value:
        return value
    return extract_target_aweme_id(str(record.get("url", ""))) or ""


def _jsonl_path(author_dir: Path) -> Path:
    return author_dir / "data.jsonl"


def _records(author_dir: Path) -> list[dict[str, Any]]:
    path = _jsonl_path(author_dir)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def get_collected_aweme_ids(author: str, output_root: str | Path = "output") -> set[str]:
    """Return IDs known for an author, including IDs inferred from old URLs."""

    author_dir = get_author_dir(author, output_root)
    ids = {aweme_id_from_record(record) for record in _records(author_dir)}
    if author_dir.exists():
        for path in author_dir.iterdir():
            if path.is_dir() and "__" in path.name:
                candidate = path.name.rsplit("__", 1)[1].strip()
                if candidate:
                    ids.add(candidate)
    return {value for value in ids if value}


def is_collected(author: str, aweme_id: str, output_root: str | Path = "output") -> bool:
    """Check collection status by ID, never by title or URL text alone."""

    target = str(aweme_id).strip()
    return bool(target and target in get_collected_aweme_ids(author, output_root))


def _find_work_dir(author_dir: Path, aweme_id: str) -> Path | None:
    suffix = f"__{str(aweme_id).strip()}"
    if not author_dir.exists():
        return None
    for path in author_dir.iterdir():
        if path.is_dir() and path.name.endswith(suffix):
            return path
    return None


def _resolved_info(info: VideoInfo) -> VideoInfo:
    aweme_id = info.aweme_id.strip() or extract_target_aweme_id(info.url) or ""
    if not aweme_id:
        raise StorageError("作品缺少 aweme_id，无法安全保存")
    return info if info.aweme_id == aweme_id else replace(info, aweme_id=aweme_id)


def upsert_jsonl(author_dir: Path, record: dict[str, Any]) -> None:
    """Upsert one ID while preserving legacy records with no reliable ID."""

    path = _jsonl_path(author_dir)
    old_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    target = str(record["aweme_id"])
    new_lines: list[str] = []
    inserted = False
    for line in old_lines:
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            new_lines.append(line)
            continue
        if isinstance(value, dict) and aweme_id_from_record(value) == target:
            if not inserted:
                new_lines.append(json.dumps(record, ensure_ascii=False))
                inserted = True
            continue
        new_lines.append(line)
    if not inserted:
        new_lines.append(json.dumps(record, ensure_ascii=False))
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8", newline="\n")


def save_video(
    info: VideoInfo,
    output_root: str | Path = "output",
    *,
    overwrite: bool = False,
) -> tuple[Path, Path, str]:
    """Save one work as ``<title>__<aweme_id>/content.txt`` and upsert JSONL."""

    info = _resolved_info(info)
    author_dir = get_author_dir(info.author, output_root)
    existing_work_dir = _find_work_dir(author_dir, info.aweme_id)
    if (existing_work_dir or is_collected(info.author, info.aweme_id, output_root)) and not overwrite:
        raise AlreadyCollectedError("这个作品之前已经采集过。")
    work_dir = existing_work_dir or get_work_dir(info.author, info.title, info.aweme_id, output_root)
    content_path = work_dir / "content.txt"

    author_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    collected_at = datetime.now().astimezone().isoformat(timespec="seconds")
    content_path.write_text(info.content + "\n", encoding="utf-8", newline="\n")
    record = asdict(info)
    record["collected_at"] = collected_at
    upsert_jsonl(author_dir, record)
    return author_dir.resolve(), content_path.resolve(), collected_at
