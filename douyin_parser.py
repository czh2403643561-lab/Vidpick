"""Douyin page loading and resilient metadata/content extraction."""

from __future__ import annotations

from dataclasses import dataclass
import html as html_lib
import json
import re
from typing import Any, Callable
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


class DouyinParseError(RuntimeError):
    """Raised when a Douyin page cannot be loaded or parsed."""


@dataclass(frozen=True)
class VideoInfo:
    author: str
    title: str
    content: str
    url: str


ProgressCallback = Callable[[str], None]


def normalize_douyin_url(value: str) -> str:
    """Validate a common Douyin URL and add https when it was omitted."""

    value = value.strip()
    if not value:
        raise DouyinParseError("请输入抖音作品链接")
    if not re.match(r"^https?://", value, re.IGNORECASE):
        value = "https://" + value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        raise DouyinParseError("链接格式不正确，请输入 http(s) 抖音链接")
    if host != "douyin.com" and not host.endswith(".douyin.com"):
        raise DouyinParseError("只支持 douyin.com 域名的链接")
    if not parsed.path or parsed.path == "/":
        raise DouyinParseError("链接中没有发现作品地址")
    return value


def extract_video(url: str, progress: ProgressCallback | None = None) -> VideoInfo:
    """Open a Douyin URL in Chromium and extract the first complete usable record."""

    normalized_url = normalize_douyin_url(url)
    report = progress or (lambda _message: None)

    try:
        with sync_playwright() as playwright:
            report("启动 Chromium")
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1440, "height": 1000},
                )
                page = context.new_page()
                report("打开链接并等待跳转")
                page.goto(normalized_url, wait_until="domcontentloaded", timeout=35_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=12_000)
                except PlaywrightTimeoutError:
                    report("页面仍在加载，继续读取当前内容")
                page.wait_for_timeout(1_500)
                report("读取页面数据")
                page_html = page.content()
                try:
                    body_text = page.locator("body").inner_text(timeout=5_000)
                except Exception:
                    body_text = ""
                final_url = page.url or normalized_url
                report("解析作者和作品正文")
                return parse_html(page_html, final_url, body_text)
            finally:
                browser.close()
    except DouyinParseError:
        raise
    except PlaywrightTimeoutError as exc:
        raise DouyinParseError("网络访问超时，请检查链接或网络后重试") from exc
    except Exception as exc:
        message = str(exc).strip().splitlines()[0] if str(exc).strip() else "未知错误"
        raise DouyinParseError(f"访问或解析页面失败：{message}") from exc


def parse_html(page_html: str, final_url: str, body_text: str = "") -> VideoInfo:
    """Parse metadata, embedded page data, and rendered text without one CSS dependency."""

    if not page_html.strip():
        raise DouyinParseError("页面内容为空，无法解析")

    json_objects = _extract_json_objects(page_html)
    author_candidates: list[tuple[int, str]] = []
    title_candidates: list[tuple[int, str]] = []
    content_candidates: list[tuple[int, str]] = []
    for obj in json_objects:
        _collect_json_candidates(obj, author_candidates, title_candidates, content_candidates)

    meta_title = _meta_value(page_html, "og:title", "property") or _meta_value(
        page_html, "twitter:title", "name"
    )
    meta_description = _meta_value(page_html, "og:description", "property") or _meta_value(
        page_html, "description", "name"
    )
    document_title = _tag_text(page_html, "title")
    headings = _tag_texts(page_html, ("h1", "h2"))
    visible_text = _clean_text(body_text) or _visible_text(page_html)

    for value in _key_value_strings(page_html, {"nickname", "author_name", "authorName", "user_name"}):
        author_candidates.append((5, value))
    for value in _key_value_strings(page_html, {"desc", "description", "content", "articleBody"}):
        content_candidates.append((3, value))

    if meta_description:
        content_candidates.append((4, meta_description))
    if meta_title:
        title_candidates.append((5, meta_title))
    if document_title:
        title_candidates.append((2, document_title))
    title_candidates.extend((1, heading) for heading in headings)

    author = _pick_candidate(author_candidates, max_length=120)
    title = _pick_title(title_candidates)
    content = _pick_content(content_candidates)

    if not author:
        author = _author_from_text(document_title or meta_title or visible_text)
    if not content:
        content = _content_from_visible_text(visible_text, headings)
    if not title and content:
        title = _first_line(content, 80)
    if not author:
        raise DouyinParseError("未能识别博主名称，页面可能需要登录或结构发生变化")
    if not content:
        raise DouyinParseError("未能识别作品完整正文，页面可能需要登录或结构发生变化")

    return VideoInfo(
        author=_clean_text(author),
        title=_clean_text(title) or "未命名作品",
        content=_clean_text(content),
        url=final_url,
    )


def _extract_json_objects(page_html: str) -> list[Any]:
    objects: list[Any] = []
    script_pattern = re.compile(
        r"<script\b[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL
    )
    for match in script_pattern.finditer(page_html):
        raw = html_lib.unescape(match.group(1)).strip()
        if not raw:
            continue
        candidates = [raw]
        assignment = re.search(r"(?:=|:)\s*(\{.*\}|\[.*\])\s*;?\s*$", raw, re.DOTALL)
        if assignment:
            candidates.append(assignment.group(1))
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, (dict, list)):
                objects.append(parsed)
                break
    return objects


def _collect_json_candidates(
    value: Any,
    authors: list[tuple[int, str]],
    titles: list[tuple[int, str]],
    contents: list[tuple[int, str]],
) -> None:
    author_keys = {"author", "author_name", "authorname", "nickname", "username", "uname"}
    title_keys = {"headline", "title", "video_title", "videotitle", "item_title", "itemtitle"}
    content_keys = {"desc", "description", "content", "articlebody", "article_body", "text"}
    if isinstance(value, dict):
        for key, child in value.items():
            key_name = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if key_name in author_keys:
                text = _value_text(child)
                if text:
                    authors.append((5 if key_name in {"nickname", "authorname"} else 4, text))
            if key_name in title_keys:
                text = _value_text(child)
                if text:
                    titles.append((4 if key_name != "text" else 1, text))
            if key_name in content_keys:
                text = _value_text(child)
                if 4 <= len(text) <= 5_000:
                    priority = 5 if key_name in {"desc", "articlebody"} else 3
                    contents.append((priority, text))
            _collect_json_candidates(child, authors, titles, contents)
    elif isinstance(value, list):
        for child in value:
            _collect_json_candidates(child, authors, titles, contents)


def _value_text(value: Any) -> str:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, dict):
        for key in ("name", "nickname", "nick_name", "title", "text", "desc", "description"):
            if key in value:
                text = _value_text(value[key])
                if text:
                    return text
    return ""


def _key_value_strings(page_html: str, keys: set[str]) -> list[str]:
    values: list[str] = []
    for key in keys:
        escaped = re.escape(key)
        pattern = re.compile(
            rf"[\"']{escaped}[\"']\s*:\s*[\"']((?:\\.|[^\"'\\])*)[\"']",
            re.IGNORECASE,
        )
        for match in pattern.finditer(page_html):
            raw = match.group(1)
            try:
                decoded = json.loads('"' + raw.replace('"', '\\"') + '"')
            except json.JSONDecodeError:
                decoded = raw.replace(r"\n", "\n").replace(r"\u0026", "&")
            text = _clean_text(decoded)
            if text:
                values.append(text)
    return values


def _meta_value(page_html: str, name: str, attribute: str) -> str:
    pattern = re.compile(
        rf"<meta\b(?=[^>]*\b{attribute}\s*=\s*[\"']{re.escape(name)}[\"'])[^>]*>",
        re.IGNORECASE,
    )
    match = pattern.search(page_html)
    if not match:
        return ""
    content = re.search(r"\bcontent\s*=\s*[\"'](.*?)[\"']", match.group(0), re.IGNORECASE | re.DOTALL)
    return _clean_text(html_lib.unescape(content.group(1))) if content else ""


def _tag_text(page_html: str, tag: str) -> str:
    texts = _tag_texts(page_html, (tag,))
    return texts[0] if texts else ""


def _tag_texts(page_html: str, tags: tuple[str, ...]) -> list[str]:
    tag_pattern = "|".join(tags)
    pattern = re.compile(rf"<({tag_pattern})\b[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
    return [_clean_text(re.sub(r"<[^>]+>", " ", match.group(2))) for match in pattern.finditer(page_html)]


def _visible_text(page_html: str) -> str:
    without_hidden = re.sub(r"<(script|style|noscript|svg)\b.*?</\1>", " ", page_html, flags=re.I | re.S)
    return _clean_text(re.sub(r"<[^>]+>", "\n", without_hidden))


def _clean_text(value: str) -> str:
    value = html_lib.unescape(str(value)).replace("\xa0", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s*\n+", "\n", value)
    return value.strip()


def _pick_candidate(candidates: list[tuple[int, str]], max_length: int) -> str:
    usable = [(priority, _clean_text(value)) for priority, value in candidates if value]
    usable = [(priority, value[:max_length]) for priority, value in usable if len(value) <= max_length]
    if not usable:
        return ""
    return max(usable, key=lambda item: (item[0], min(len(item[1]), 60), -len(item[1])))[1]


def _pick_title(candidates: list[tuple[int, str]]) -> str:
    usable = sorted(candidates, key=lambda item: (item[0], min(len(item[1]), 60)), reverse=True)
    for _, candidate in usable:
        title = _clean_text(candidate)
        title = re.sub(r"\s*[-_|｜]\s*(抖音|Douyin).*$", "", title, flags=re.I).strip()
        if re.fullmatch(r".+的抖音", title):
            continue
        title = re.sub(r"^.*?的抖音\s*[-_|｜]?\s*", "", title).strip() or title
        if title:
            return title
    return ""


def _pick_content(candidates: list[tuple[int, str]]) -> str:
    usable: list[tuple[int, str]] = []
    for priority, value in candidates:
        text = _clean_text(value)
        if 4 <= len(text) <= 5_000 and text not in {item[1] for item in usable}:
            usable.append((priority, text))
    if not usable:
        return ""
    return max(usable, key=lambda item: (item[0], len(item[1])))[1]


def _author_from_text(value: str) -> str:
    value = _clean_text(value)
    patterns = (
        r"(?:作者|博主|用户)\s*[:：]\s*([^|｜\n]{1,80})",
        r"^([^|｜\n]{1,80})的抖音",
    )
    for pattern in patterns:
        match = re.search(pattern, value, re.IGNORECASE)
        if match:
            return _clean_text(match.group(1))
    return ""


def _content_from_visible_text(visible_text: str, headings: list[str]) -> str:
    if not visible_text:
        return ""
    heading_set = {_clean_text(item) for item in headings}
    lines = [_clean_text(line) for line in visible_text.splitlines()]
    lines = [line for line in lines if len(line) >= 4 and line not in heading_set]
    if not lines:
        return ""
    return max(lines, key=len)[:5_000]


def _first_line(value: str, max_length: int) -> str:
    return _clean_text(value.splitlines()[0])[:max_length]
