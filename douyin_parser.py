"""Douyin page loading and resilient metadata/content extraction."""

from __future__ import annotations

from dataclasses import dataclass
import html as html_lib
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlparse

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
    aweme_id: str = ""
    work_type: str = "video"
    cover_url: str = ""
    image_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileWork:
    url: str
    cover_url: str
    title: str
    aweme_id: str = ""
    author: str = ""
    desc: str = ""


@dataclass(frozen=True)
class ProfileInfo:
    author: str
    url: str
    works: tuple[ProfileWork, ...]


ProgressCallback = Callable[[str], None]
PROFILE_DIR = Path(__file__).resolve().parent / "browser_profile"


def normalize_douyin_url(value: str) -> str:
    """Validate a common Douyin URL and add https when it was omitted."""

    value = value.strip()
    links = re.findall(r"https?://[^\s\]\)]+douyin\.com[^\s\]\)]*", value, re.IGNORECASE)
    if links:
        value = links[0]
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


def extract_target_aweme_id(value: str) -> str | None:
    """Return only the concrete work ID encoded by a Douyin URL or share text."""
    try:
        url = normalize_douyin_url(value)
    except DouyinParseError:
        return None
    parsed = urlparse(url)
    match = re.search(r"/(?:video|note)/(\d+)", parsed.path)
    if match:
        return match.group(1)
    query = parse_qs(parsed.query)
    for key in ("modal_id", "vid", "aweme_id"):
        if query.get(key) and re.fullmatch(r"\d+", query[key][0]):
            return query[key][0]
    return None


def canonical_work_url(value: str, target_aweme_id: str) -> str:
    """Return the direct work page for a known target ID.

    A direct video/note URL is already the best address.  Web profile URLs
    carrying ``vid``/``modal_id`` instead start from the video detail route.
    """

    normalized = normalize_douyin_url(value)
    direct = re.search(r"/(?:video|note)/(\d+)", urlparse(normalized).path)
    if direct and direct.group(1) == target_aweme_id:
        return normalized
    return f"https://www.douyin.com/video/{target_aweme_id}"


def _target_navigation_urls(normalized_url: str, target_aweme_id: str) -> tuple[str, ...]:
    """Try a direct work page first; only then use narrow fallbacks."""

    primary = canonical_work_url(normalized_url, target_aweme_id)
    direct = re.search(r"/(?:video|note)/(\d+)", urlparse(normalized_url).path)
    if direct and direct.group(1) == target_aweme_id:
        return (primary,)
    candidates = (
        primary,
        f"https://www.douyin.com/note/{target_aweme_id}",
        normalized_url,
    )
    return tuple(dict.fromkeys(candidates))


def extract_video(url: str, progress: ProgressCallback | None = None) -> VideoInfo:
    """Open a Douyin URL in Chromium and extract the first complete usable record."""

    with DouyinSession(progress) as session:
        return session.extract_video(url)


def extract_profile(url: str, progress: ProgressCallback | None = None) -> ProfileInfo:
    """Open a Douyin profile and return every public work visible to this environment."""

    with DouyinSession(progress) as session:
        return session.extract_profile(url)


class DouyinSession:
    """One Playwright browser/context, reusable for profile and sequential video work."""

    def __init__(self, progress: ProgressCallback | None = None) -> None:
        self._report = progress or (lambda _message: None)
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._awemes: list[dict[str, Any]] = []
        self._target_aweme_id: str | None = None
        self._target_aweme: dict[str, Any] | None = None

    def __enter__(self) -> "DouyinSession":
        try:
            self._report("启动 Chromium")
            self._playwright = sync_playwright().start()
            PROFILE_DIR.mkdir(exist_ok=True)
            self._context = self._playwright.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=True,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1440, "height": 1000},
            )
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._page.on("response", self._capture_response)
            return self
        except Exception as exc:
            self.__exit__(None, None, None)
            raise _as_parse_error(exc, "启动 Chromium 失败") from exc

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        if self._context is not None:
            self._context.close()
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._page = self._context = self._browser = self._playwright = None

    def extract_video(self, url: str, progress: ProgressCallback | None = None) -> VideoInfo:
        normalized_url = normalize_douyin_url(url)
        requested_target = extract_target_aweme_id(normalized_url)
        if not requested_target and "/user/" in urlparse(normalized_url).path:
            raise DouyinParseError("这是博主主页链接，请切换到博主主页批量模式")
        page = self._require_page()
        report = progress or self._report
        try:
            targets = _target_navigation_urls(normalized_url, requested_target) if requested_target else (normalized_url,)
            last_error: DouyinParseError | None = None
            for index, navigation_url in enumerate(targets):
                self._reset_aweme_capture(requested_target)
                report("打开目标作品页" if index == 0 else "尝试备用作品地址")
                page.goto(navigation_url, wait_until="domcontentloaded", timeout=35_000)
                target = requested_target or extract_target_aweme_id(page.url)
                if not target:
                    raise DouyinParseError("未能确认目标作品 ID，请重试")
                self._target_aweme_id = target
                report("等待目标作品数据")
                matched = self._wait_for_target_aweme(target)
                report("解析作者和作品正文")
                if matched:
                    structured = _video_from_aweme(matched, page.url or navigation_url)
                    if structured.content and structured.aweme_id == target:
                        return structured
                try:
                    return parse_target_html(page.content(), page.url or navigation_url, target)
                except DouyinParseError as exc:
                    last_error = exc
            raise last_error or DouyinParseError("未能确认目标作品数据，请重试")
        except DouyinParseError:
            raise
        except Exception as exc:
            raise _as_parse_error(exc, "访问或解析作品页面失败") from exc

    def extract_profile(self, url: str) -> ProfileInfo:
        normalized_url = normalize_douyin_url(url)
        page = self._require_page()
        try:
            self._reset_aweme_capture()
            self._report("打开博主主页并等待跳转")
            page.goto(normalized_url, wait_until="domcontentloaded", timeout=35_000)
            self._wait_for_page(page)
            self._report("读取公开作品列表")
            works = _works_from_awemes(self._awemes)
            if not works:
                works = self._scroll_profile_works(page)
            if not works:
                self._report("未读取到作品，正在重新加载一次")
                self._reset_aweme_capture(); page.reload(wait_until="domcontentloaded", timeout=35_000); self._wait_for_page(page)
                works = _works_from_awemes(self._awemes) or self._scroll_profile_works(page)
            return parse_profile_page(page.url or normalized_url, page.title(), _body_text(page), works)
        except DouyinParseError:
            raise
        except Exception as exc:
            raise _as_parse_error(exc, "访问或解析博主主页失败") from exc

    def _wait_for_page(self, page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=12_000)
        except PlaywrightTimeoutError:
            self._report("页面仍在加载，继续读取当前内容")
        page.wait_for_timeout(1_500)

    def _scroll_profile_works(self, page) -> list[dict[str, str]]:
        cards: list[dict[str, str]] = []
        known_urls: set[str] = set()
        idle_rounds = 0
        for _ in range(40):
            current_cards = _read_profile_cards(page)
            new_cards = [item for item in current_cards if item.get("url") not in known_urls]
            if new_cards:
                cards.extend(new_cards)
                known_urls.update(item["url"] for item in new_cards)
                idle_rounds = 0
                self._report(f"已识别 {len(cards)} 个公开作品")
            else:
                idle_rounds += 1
            if idle_rounds >= 3:
                break
            page.mouse.wheel(0, 1_600)
            page.wait_for_timeout(1_200)
        return cards

    def _require_page(self):
        if self._page is None:
            raise DouyinParseError("浏览器会话尚未启动")
        return self._page

    def _wait_for_target_aweme(self, target: str) -> dict[str, Any] | None:
        for _ in range(40):
            if self._target_aweme and str(self._target_aweme.get("aweme_id", "")) == target:
                return self._target_aweme
            for item in self._awemes:
                if str(item.get("aweme_id", "")) == target:
                    return item
            self._page.wait_for_timeout(100)
        return None

    def _reset_aweme_capture(self, target: str | None = None) -> None:
        self._awemes.clear()
        self._target_aweme_id = target
        self._target_aweme = None

    def _capture_response(self, response) -> None:
        if "/aweme/" not in response.url or "json" not in response.headers.get("content-type", "").lower():
            return
        try:
            payload = response.json()
            if isinstance(payload, dict):
                detail = payload.get("aweme_detail")
                items = payload.get("aweme_list")
                captured = ([detail] if isinstance(detail, dict) else []) + (items if isinstance(items, list) else [])
                for item in captured:
                    if not isinstance(item, dict):
                        continue
                    self._awemes.append(item)
                    if self._target_aweme_id and str(item.get("aweme_id", "")) == self._target_aweme_id:
                        self._target_aweme = item
        except Exception:
            return


def parse_profile_page(
    final_url: str,
    document_title: str,
    body_text: str,
    cards: Iterable[dict[str, str]],
) -> ProfileInfo:
    """Pure profile parsing: suitable for unit tests and independent of page selectors."""

    author = _profile_author(document_title, body_text)
    card_list = list(cards)
    works = tuple(card_list) if all(isinstance(card, ProfileWork) for card in card_list) else tuple(normalize_profile_cards(card_list))
    if not author:
        raise DouyinParseError("未能识别博主名称，页面可能需要登录或结构发生变化")
    if not works:
        if "/user/self" in final_url:
            raise DouyinParseError("需要登录：当前链接依赖登录状态")
        raise DouyinParseError("未识别到可访问的公开作品，可能需要登录或页面受限")
    return ProfileInfo(author=author, url=final_url, works=works)


def parse_target_html(page_html: str, final_url: str, target_aweme_id: str) -> VideoInfo:
    """Fail closed: only parse an embedded object that proves it is the requested work."""
    for obj in _extract_json_objects(page_html):
        found = _find_aweme_object(obj, target_aweme_id)
        if found:
            info = _video_from_aweme(found, final_url)
            if info.author and info.content:
                return info
    raise DouyinParseError("未能确认目标作品数据，请重试")


def _find_aweme_object(value: Any, target_aweme_id: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if str(value.get("aweme_id", "")) == target_aweme_id:
            return value
        for child in value.values():
            found = _find_aweme_object(child, target_aweme_id)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_aweme_object(child, target_aweme_id)
            if found:
                return found
    return None


def normalize_profile_cards(cards: Iterable[dict[str, str]]) -> list[ProfileWork]:
    """Clean/dedupe browser card data while keeping only real video or note links."""

    works: list[ProfileWork] = []
    seen_urls: set[str] = set()
    for card in cards:
        url = _canonical_work_url(str(card.get("url", "")))
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        title = _profile_card_title(str(card.get("title", "")))
        cover_url = str(card.get("cover_url", "")).strip()
        if not re.match(r"^https?://", cover_url, re.IGNORECASE):
            cover_url = ""
        works.append(ProfileWork(url=url, cover_url=cover_url, title=title or "未命名作品", aweme_id=str(card.get("aweme_id", "")), author=str(card.get("author", "")), desc=str(card.get("desc", ""))))
    return works


def _works_from_awemes(items: Iterable[dict[str, Any]]) -> list[ProfileWork]:
    cards = []
    for item in items:
        aweme_id = str(item.get("aweme_id", "")); desc = _clean_text(item.get("desc", "")); author = _clean_text(item.get("author", {}).get("nickname", "")) if isinstance(item.get("author"), dict) else ""
        video = item.get("video", {}) if isinstance(item.get("video"), dict) else {}; cover = video.get("cover", {}) if isinstance(video.get("cover"), dict) else {}; urls = cover.get("url_list", []) if isinstance(cover.get("url_list"), list) else []
        if aweme_id: cards.append({"url": f"https://www.douyin.com/{'note' if item.get('images') else 'video'}/{aweme_id}", "cover_url": urls[0] if urls else "", "title": desc, "aweme_id": aweme_id, "author": author, "desc": desc})
    return normalize_profile_cards(cards)


def _video_from_aweme(item: dict[str, Any], url: str) -> VideoInfo:
    author = _clean_text(item.get("author", {}).get("nickname", "")) if isinstance(item.get("author"), dict) else ""
    desc = _clean_text(item.get("desc", ""))
    work_type, cover_url, image_urls = _media_from_aweme(item)
    return VideoInfo(
        author=author,
        title=_first_line(desc, 80) or "未命名作品",
        content=desc,
        url=url,
        aweme_id=str(item.get("aweme_id", "")),
        work_type=work_type,
        cover_url=cover_url,
        image_urls=image_urls,
    )


def _media_from_aweme(item: dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    """Read media only from the already verified target aweme object."""

    images = item.get("images")
    if isinstance(images, list):
        image_urls: list[str] = []
        seen: set[str] = set()
        for image in images:
            if not isinstance(image, dict):
                continue
            media_url = _best_image_url(image)
            if media_url and media_url not in seen:
                seen.add(media_url)
                image_urls.append(media_url)
        if image_urls:
            return "image", image_urls[0], tuple(image_urls)

    video = item.get("video")
    cover_url = ""
    if isinstance(video, dict):
        for key in ("origin_cover", "cover"):
            cover_url = _first_url(video.get(key))
            if cover_url:
                break
    return "video", cover_url, ()


def _best_image_url(image: dict[str, Any]) -> str:
    """Prefer Douyin's original/download URLs over display thumbnail URLs."""

    for key in ("download_url_list", "url_list"):
        urls = image.get(key)
        if isinstance(urls, list):
            for value in urls:
                if isinstance(value, str) and value.startswith(("https://", "http://")):
                    return value
    return ""


def _first_url(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    urls = value.get("url_list")
    if not isinstance(urls, list):
        return ""
    return next((url for url in urls if isinstance(url, str) and url.startswith(("https://", "http://"))), "")


def _read_profile_cards(page) -> list[dict[str, str]]:
    primary = page.locator("[data-e2e='user-post-list'] a[href*='/video/'], [data-e2e='user-post-list'] a[href*='/note/']")
    locator = primary if primary.count() else page.locator("a[href*='/video/'], a[href*='/note/']")
    return locator.evaluate_all(
        """els => els.filter(a => !a.closest('footer')).map(a => {
            const image = a.querySelector('img');
            const paragraph = a.querySelector('p');
            return {
                url: a.href,
                cover_url: image ? (image.currentSrc || image.src || '') : '',
                title: (paragraph ? paragraph.innerText : '') || a.innerText || (image ? image.alt : '') || ''
            };
        })"""
    )


def _body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5_000)
    except Exception:
        return ""


def _as_parse_error(exc: Exception, prefix: str) -> DouyinParseError:
    if isinstance(exc, PlaywrightTimeoutError):
        return DouyinParseError("网络访问超时，请检查链接或网络后重试")
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else "未知错误"
    return DouyinParseError(f"{prefix}：{message}")


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
    rendered_content = _content_from_visible_text(visible_text, headings, content)

    if not author:
        for source in (document_title, meta_title, visible_text):
            author = _author_from_text(source)
            if author:
                break
    if rendered_content and (not content or len(rendered_content) > len(content)):
        content = rendered_content
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
        r"[-—]\s*([^\n]{1,80}?)于\d{4,}发布",
        r"(?:^|\n)([^\n]{1,80})\s*\n+\s*(?:粉丝|获赞)",
        r"(?:作者|博主|用户)\s*[:：]\s*([^|｜\n]{1,80})",
        r"^([^|｜\n]{1,80})的抖音",
    )
    for pattern in patterns:
        match = re.search(pattern, value, re.IGNORECASE)
        if match:
            return _clean_text(match.group(1))
    return ""


def _profile_author(document_title: str, body_text: str) -> str:
    for source in (document_title, body_text):
        match = re.search(r"^(.+?)的抖音(?:\s*[-_|｜].*)?$", _clean_text(source), re.IGNORECASE)
        if match:
            return _clean_text(match.group(1))
    body_match = re.search(r"(?:^|\n)([^\n]{1,80})\s*\n+\s*(?:关注|粉丝|获赞)", body_text)
    return _clean_text(body_match.group(1)) if body_match else ""


def _canonical_work_url(value: str) -> str:
    value = value.strip()
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if not host.endswith("douyin.com") or not re.search(r"/(?:video|note)/\d+", parsed.path):
        return ""
    match = re.search(r"/(?:video|note)/\d+", parsed.path)
    return f"https://www.douyin.com{match.group(0)}" if match else ""


def _profile_card_title(value: str) -> str:
    lines = [_clean_text(line) for line in value.splitlines()]
    useful = [
        line
        for line in lines
        if line
        and line != "置顶"
        and not re.fullmatch(r"[\d.]+(?:万)?", line)
    ]
    title = " ".join(useful)
    if "：" in title and len(title.split("：", 1)[0]) <= 40:
        title = title.split("：", 1)[1]
    return _clean_text(title)[:240]


def _content_from_visible_text(visible_text: str, headings: list[str], seed: str = "") -> str:
    if not visible_text:
        return ""
    heading_set = {_clean_text(item) for item in headings}
    lines = [_clean_text(line) for line in visible_text.splitlines()]
    lines = [line for line in lines if len(line) >= 4 and line not in heading_set]
    if not lines:
        return ""
    seed = _clean_text(seed).replace("\n", " ")[:32]
    if seed:
        matching_lines = [line for line in lines if seed in line]
        if matching_lines:
            return max(matching_lines, key=len)[:5_000]
    return max(lines, key=len)[:5_000]


def _first_line(value: str, max_length: int) -> str:
    return _clean_text(value.splitlines()[0])[:max_length]
