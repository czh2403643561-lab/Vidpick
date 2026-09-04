"""Filesystem storage and duplicate detection for collected Douyin works."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from douyin_parser import VideoInfo, extract_target_aweme_id


class StorageError(RuntimeError):
    """Raised when a work cannot be stored safely."""


class AlreadyCollectedError(StorageError):
    """Raised when a new save would overwrite an existing work."""


@dataclass(frozen=True)
class MediaSaveResult:
    total: int = 0
    saved: int = 0
    cover_saved: bool = False
    newly_saved: int = 0


@dataclass(frozen=True)
class CollectionOptions:
    text: bool = True
    images: bool = True

    def __post_init__(self) -> None:
        if not self.text and not self.images:
            raise StorageError("至少选择文案或图片其中一项")


@dataclass(frozen=True)
class AssetState:
    text: bool = False
    images: bool = False

    def is_complete_for(self, options: CollectionOptions) -> bool:
        return (not options.text or self.text) and (not options.images or self.images)

    def has_requested_asset(self, options: CollectionOptions) -> bool:
        return (options.text and self.text) or (options.images and self.images)


@dataclass(frozen=True)
class SelectedSaveResult:
    author_dir: Path
    work_dir: Path
    before: AssetState
    after: AssetState
    text_saved: bool
    media: MediaSaveResult
    collected_at: str


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


def _work_paths(info: VideoInfo, output_root: str | Path) -> tuple[VideoInfo, Path, Path]:
    info = _resolved_info(info)
    author_dir = get_author_dir(info.author, output_root)
    work_dir = _find_work_dir(author_dir, info.aweme_id) or get_work_dir(info.author, info.title, info.aweme_id, output_root)
    return info, author_dir, work_dir


def get_asset_state(info: VideoInfo, output_root: str | Path = "output") -> AssetState:
    """Read the actually available assets, never trusting JSONL alone."""

    info, _author_dir, work_dir = _work_paths(info, output_root)
    text = (work_dir / "content.txt").is_file()
    if info.work_type == "image":
        expected = info.image_total or len(info.image_urls)
        image_dir = work_dir / "images"
        managed = [path for path in image_dir.glob("*.*") if path.is_file() and re.fullmatch(r"\d+", path.stem)] if image_dir.is_dir() else []
        images = bool(expected and len(managed) >= expected and _cover_path(work_dir) is not None)
    else:
        images = _cover_path(work_dir) is not None
    return AssetState(text=text, images=images)


def save_selected_assets(
    info: VideoInfo,
    output_root: str | Path,
    options: CollectionOptions,
    *,
    overwrite: bool = False,
    progress: Callable[[str], None] | None = None,
) -> SelectedSaveResult:
    """Save only requested resources and merge their real state into JSONL."""

    info, author_dir, work_dir = _work_paths(info, output_root)
    author_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    before = get_asset_state(info, output_root)
    text_saved = False
    if options.text and (overwrite or not before.text):
        (work_dir / "content.txt").write_text(info.content + "\n", encoding="utf-8", newline="\n")
        text_saved = True

    media = MediaSaveResult()
    if options.images and (overwrite or not before.images):
        media = save_media(info, work_dir, overwrite=overwrite, only_missing=not overwrite, progress=progress)
    elif options.images:
        media = _existing_media_result(info, work_dir)

    after = get_asset_state(info, output_root)
    collected_at = datetime.now().astimezone().isoformat(timespec="seconds")
    _upsert_selected_record(author_dir, info, after, collected_at, text_saved=text_saved)
    return SelectedSaveResult(author_dir.resolve(), work_dir.resolve(), before, after, text_saved, media, collected_at)


def _upsert_selected_record(author_dir: Path, info: VideoInfo, state: AssetState, collected_at: str, *, text_saved: bool) -> None:
    old = next((record for record in _records(author_dir) if aweme_id_from_record(record) == info.aweme_id), {})
    record = dict(old)
    current = asdict(info)
    for key in ("aweme_id", "author", "title", "url", "work_type", "cover_url", "image_urls", "image_total"):
        record[key] = current[key]
    if text_saved:
        record["content"] = info.content
    elif "content" not in old:
        record.pop("content", None)
    record["saved_assets"] = {"text": state.text, "images": state.images}
    record["collected_at"] = collected_at
    upsert_jsonl(author_dir, record)


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


def save_media(
    info: VideoInfo,
    work_dir: Path,
    *,
    overwrite: bool = False,
    only_missing: bool = False,
    progress: Callable[[str], None] | None = None,
) -> MediaSaveResult:
    """Download only Vidpick-managed media without risking the saved正文."""

    if overwrite:
        _clear_managed_media(work_dir)

    report = progress or (lambda _message: None)
    if info.work_type == "image":
        total = info.image_total or len(info.image_urls)
        if not info.image_urls:
            return MediaSaveResult(total=total)
        image_dir = work_dir / "images"
        image_dir.mkdir(exist_ok=True)
        saved = 0
        newly_saved = 0
        first_image: Path | None = None
        for index, url in enumerate(info.image_urls, 1):
            existing = _managed_image_path(image_dir, index) if only_missing else None
            if existing is not None:
                saved += 1
                if index == 1:
                    first_image = existing
                continue
            report(f"下载无水印图片 {index}/{len(info.image_urls)}")
            try:
                image_path = _download_to_stem(url, image_dir / f"{index:02d}")
            except Exception:
                continue
            saved += 1
            newly_saved += 1
            if index == 1:
                first_image = image_path
        if first_image is not None and (overwrite or _cover_path(work_dir) is None):
            _copy_as_cover(first_image, work_dir)
        return MediaSaveResult(total=total, saved=saved, cover_saved=_cover_path(work_dir) is not None, newly_saved=newly_saved)

    if info.cover_url:
        if only_missing and _cover_path(work_dir) is not None:
            return MediaSaveResult(total=1, saved=1, cover_saved=True)
        report("下载作品封面")
        try:
            _download_to_stem(info.cover_url, work_dir / "cover")
            return MediaSaveResult(total=1, saved=1, cover_saved=True, newly_saved=1)
        except Exception:
            return MediaSaveResult(total=1, saved=0, cover_saved=False)
    return MediaSaveResult()


def _clear_managed_media(work_dir: Path) -> None:
    for path in work_dir.glob("cover.*"):
        if path.is_file():
            path.unlink()
    image_dir = work_dir / "images"
    if image_dir.exists() and image_dir.is_dir():
        shutil.rmtree(image_dir)


def _managed_image_path(image_dir: Path, index: int) -> Path | None:
    return next((path for path in image_dir.glob(f"{index:02d}.*") if path.is_file()), None)


def _cover_path(work_dir: Path) -> Path | None:
    return next((path for path in work_dir.glob("cover.*") if path.is_file()), None)


def _existing_media_result(info: VideoInfo, work_dir: Path) -> MediaSaveResult:
    if info.work_type == "image":
        total = info.image_total or len(info.image_urls)
        image_dir = work_dir / "images"
        saved = sum(_managed_image_path(image_dir, index) is not None for index in range(1, total + 1)) if image_dir.is_dir() else 0
        return MediaSaveResult(total=total, saved=saved, cover_saved=_cover_path(work_dir) is not None)
    return MediaSaveResult(total=1, saved=1 if _cover_path(work_dir) is not None else 0, cover_saved=_cover_path(work_dir) is not None) if info.cover_url else MediaSaveResult()


def _copy_as_cover(source: Path, work_dir: Path) -> None:
    for path in work_dir.glob("cover.*"):
        if path.is_file():
            path.unlink()
    destination = work_dir / f"cover{source.suffix or '.jpg'}"
    temp_path = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copyfile(source, temp_path)
    temp_path.replace(destination)


def _download_to_stem(url: str, stem: Path) -> Path:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.douyin.com/"})
    temporary_path: Path | None = None
    try:
        with urlopen(request, timeout=15) as response:
            extension = _media_extension(response.headers.get_content_type(), url)
            destination = stem.with_suffix(extension)
            with tempfile.NamedTemporaryFile(delete=False, dir=destination.parent, suffix=".part") as handle:
                temporary_path = Path(handle.name)
                shutil.copyfileobj(response, handle)
        temporary_path.replace(destination)
        return destination
    except Exception:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
        raise


def _media_extension(content_type: str, url: str) -> str:
    known = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
    if content_type.lower() in known:
        return known[content_type.lower()]
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".jpg"
