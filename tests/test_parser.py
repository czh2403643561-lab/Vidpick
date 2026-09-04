import pytest

from douyin_parser import DouyinParseError, DouyinSession, _find_aweme_object, _video_from_aweme, _works_from_awemes, canonical_work_url, extract_target_aweme_id, normalize_douyin_url, normalize_profile_cards, parse_html, parse_profile_page, parse_target_html


def test_parse_html_uses_metadata_and_embedded_data() -> None:
    page = """
    <html><head>
      <title>小明的抖音 - 抖音</title>
      <meta property="og:title" content="春日记录">
      <meta property="og:description" content="这是一段完整的作品正文，来自页面元数据。">
      <script type="application/ld+json">
        {"@type":"VideoObject","name":"春日记录","description":"这是一段完整的作品正文，来自页面元数据。","author":{"@type":"Person","name":"小明"}}
      </script>
    </head><body><h1>春日记录</h1></body></html>
    """
    info = parse_html(page, "https://www.douyin.com/video/123")

    assert info.author == "小明"
    assert info.title == "春日记录"
    assert "完整的作品正文" in info.content
    assert info.url.endswith("/123")


def test_parse_html_falls_back_to_rendered_text() -> None:
    page = """
    <html><head><title>小红的抖音</title></head>
    <body><main><h1>旅行日记</h1><p>这是一段渲染后的完整正文内容，用于验证页面文本 fallback。</p></main></body></html>
    """
    info = parse_html(page, "https://v.douyin.com/abc", "小红的抖音\n旅行日记\n这是一段渲染后的完整正文内容，用于验证页面文本 fallback。")

    assert info.author == "小红"
    assert info.title == "旅行日记"
    assert "渲染后的完整正文" in info.content


def test_profile_parser_dedupes_cards_and_uses_profile_title() -> None:
    profile = parse_profile_page(
        "https://www.douyin.com/user/example",
        "LightNING的抖音 - 抖音",
        "LightNING\n关注",
        [
            {"url": "https://www.douyin.com/note/123", "cover_url": "https://cover/1.jpg", "title": "置顶\n5.2万\n第一篇作品"},
            {"url": "https://www.douyin.com/note/123?share=1", "cover_url": "", "title": "重复"},
            {"url": "https://www.douyin.com/video/456", "cover_url": "", "title": "第二篇作品"},
        ],
    )

    assert profile.author == "LightNING"
    assert len(profile.works) == 2
    assert profile.works[0].title == "第一篇作品"


def test_normalize_profile_cards_ignores_non_work_links() -> None:
    works = normalize_profile_cards([
        {"url": "https://www.douyin.com/user/a", "title": "主页"},
        {"url": "//www.douyin.com/video/999", "title": "作品"},
    ])
    assert [work.url for work in works] == ["https://www.douyin.com/video/999"]


def test_normalize_douyin_url_extracts_a_link_from_share_text() -> None:
    value = "复制打开抖音 [https://v.douyin.com/example/](https://v.douyin.com/example/) 查看作品"
    assert normalize_douyin_url(value) == "https://v.douyin.com/example/"


def test_profile_cards_keep_structured_fields() -> None:
    work = normalize_profile_cards([{
        "url": "https://www.douyin.com/note/123",
        "title": "标题",
        "aweme_id": "123",
        "author": "博主",
        "desc": "完整正文",
        "work_type": "image",
        "cover_url": "https://clean.example/01.webp",
        "image_urls": ("https://clean.example/01.webp", "https://clean.example/02.webp"),
        "image_total": 2,
    }])[0]
    assert (work.aweme_id, work.author, work.desc, work.work_type, work.cover_url, work.image_urls, work.image_total) == (
        "123", "博主", "完整正文", "image", "https://clean.example/01.webp",
        ("https://clean.example/01.webp", "https://clean.example/02.webp"), 2,
    )


def test_structured_profile_aweme_keeps_clean_image_media() -> None:
    works = _works_from_awemes([{
        "aweme_id": "123",
        "desc": "图文正文",
        "author": {"nickname": "测试博主"},
        "images": [
            {"url_list": ["https://clean.example/01.webp"], "download_url_list": ["https://watermark.example/01.webp"]},
            {"url_list": ["https://clean.example/02.webp"], "download_url_list": ["https://watermark.example/02.webp"]},
        ],
    }])

    assert len(works) == 1
    work = works[0]
    assert (work.work_type, work.cover_url, work.image_urls, work.image_total) == (
        "image", "https://clean.example/01.webp",
        ("https://clean.example/01.webp", "https://clean.example/02.webp"), 2,
    )
    assert all("watermark" not in value for value in work.image_urls)


@pytest.mark.parametrize("url, expected", [
    ("https://www.douyin.com/video/123", "123"),
    ("https://www.douyin.com/note/123", "123"),
])
def test_extract_target_aweme_id(url, expected) -> None:
    assert extract_target_aweme_id(url) == expected


def test_extract_target_aweme_id_from_profile_vid() -> None:
    assert extract_target_aweme_id("https://www.douyin.com/user/self?from_tab_name=main&vid=123") == "123"


def test_extract_target_aweme_id_from_profile_modal_id() -> None:
    assert extract_target_aweme_id("https://www.douyin.com/user/self?from_tab_name=main&modal_id=123") == "123"


def test_canonical_work_url_uses_target_video_page_for_profile_query() -> None:
    profile_url = "https://www.douyin.com/user/self?from_tab_name=main&vid=123"
    assert canonical_work_url(profile_url, "123") == "https://www.douyin.com/video/123"


def test_target_lookup_ignores_response_order() -> None:
    payload = {"aweme_list": [{"aweme_id": "111"}, {"aweme_id": "123"}, {"aweme_id": "999"}]}
    assert _find_aweme_object(payload, "123")["aweme_id"] == "123"
    assert _find_aweme_object(payload, "555") is None


def test_response_capture_only_returns_requested_aweme() -> None:
    class Response:
        url = "https://www.douyin.com/aweme/v1/web/aweme/post/"
        headers = {"content-type": "application/json"}

        @staticmethod
        def json():
            return {"aweme_list": [{"aweme_id": "111"}, {"aweme_id": "123", "desc": "目标作品"}, {"aweme_id": "999"}]}

    session = DouyinSession()
    session._reset_aweme_capture("123")
    session._capture_response(Response())

    assert session._wait_for_target_aweme("123") == {"aweme_id": "123", "desc": "目标作品"}


def test_target_html_returns_the_verified_aweme_id() -> None:
    html = '<script type="application/json">{"aweme_list":[{"aweme_id":"123","desc":"目标正文","author":{"nickname":"测试博主"}}]}</script>'

    assert parse_target_html(html, "https://www.douyin.com/video/123", "123").aweme_id == "123"


def test_target_aweme_media_prefers_clean_image_urls_and_keeps_order() -> None:
    info = _video_from_aweme({
        "aweme_id": "123",
        "desc": "图文作品",
        "author": {"nickname": "测试博主"},
        "images": [
            {"url_list": ["https://clean.example/a.webp"], "download_url_list": ["https://watermark.example/a.webp"]},
            {"url_list": ["https://clean.example/a.webp"], "download_url_list": ["https://watermark.example/a-copy.webp"]},
            {"url_list": ["https://clean.example/b.webp"]},
        ],
    }, "https://www.douyin.com/note/123")

    assert info.aweme_id == "123"
    assert info.work_type == "image"
    assert info.image_total == 2
    assert info.cover_url == "https://clean.example/a.webp"
    assert info.image_urls == ("https://clean.example/a.webp", "https://clean.example/b.webp")


def test_target_aweme_media_uses_clean_url_list_when_it_is_the_only_source() -> None:
    info = _video_from_aweme({
        "aweme_id": "124",
        "desc": "图文作品",
        "author": {"nickname": "测试博主"},
        "images": [{"url_list": ["https://clean.example/only.webp"]}],
    }, "https://www.douyin.com/note/124")

    assert (info.work_type, info.image_total, info.cover_url, info.image_urls) == (
        "image", 1, "https://clean.example/only.webp", ("https://clean.example/only.webp",),
    )


def test_target_aweme_media_does_not_treat_watermarked_download_as_clean() -> None:
    info = _video_from_aweme({
        "aweme_id": "125",
        "desc": "图文作品",
        "author": {"nickname": "测试博主"},
        "images": [{"download_url_list": ["https://watermark.example/only.webp"]}],
    }, "https://www.douyin.com/note/125")

    assert (info.work_type, info.image_total, info.cover_url, info.image_urls) == ("image", 1, "", ())


def test_target_aweme_video_uses_its_own_cover_without_images() -> None:
    info = _video_from_aweme({
        "aweme_id": "456",
        "desc": "视频作品",
        "author": {"nickname": "测试博主"},
        "video": {"origin_cover": {"url_list": ["https://cover/original.jpg"]}},
    }, "https://www.douyin.com/video/456")

    assert (info.work_type, info.cover_url, info.image_urls, info.image_total) == ("video", "https://cover/original.jpg", (), 0)


def test_target_html_fails_closed_when_only_other_work_exists() -> None:
    html = '<script type="application/json">{"aweme_list":[{"aweme_id":"111","desc":"别的作品"}]}</script>'
    with pytest.raises(DouyinParseError, match="未能确认目标"):
        parse_target_html(html, "https://www.douyin.com/video/123", "123")


def test_homepage_without_work_id_is_not_a_single_work() -> None:
    assert extract_target_aweme_id("https://www.douyin.com/user/abc") is None
