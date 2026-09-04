import pytest

from douyin_parser import DouyinParseError, _find_aweme_object, extract_target_aweme_id, normalize_douyin_url, normalize_profile_cards, parse_html, parse_profile_page, parse_target_html


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
    }])[0]
    assert (work.aweme_id, work.author, work.desc) == ("123", "博主", "完整正文")


@pytest.mark.parametrize("url, expected", [
    ("https://www.douyin.com/video/123", "123"),
    ("https://www.douyin.com/note/123", "123"),
    ("https://www.douyin.com/user/self?from_tab_name=main&modal_id=123&showTab=like", "123"),
    ("https://www.douyin.com/user/self?vid=123", "123"),
])
def test_extract_target_aweme_id(url, expected) -> None:
    assert extract_target_aweme_id(url) == expected


def test_target_lookup_ignores_response_order() -> None:
    payload = {"aweme_list": [{"aweme_id": "111"}, {"aweme_id": "123"}, {"aweme_id": "999"}]}
    assert _find_aweme_object(payload, "123")["aweme_id"] == "123"
    assert _find_aweme_object(payload, "555") is None


def test_target_html_fails_closed_when_only_other_work_exists() -> None:
    html = '<script type="application/json">{"aweme_list":[{"aweme_id":"111","desc":"别的作品"}]}</script>'
    with pytest.raises(DouyinParseError, match="未能确认目标"):
        parse_target_html(html, "https://www.douyin.com/video/123", "123")


def test_homepage_without_work_id_is_not_a_single_work() -> None:
    assert extract_target_aweme_id("https://www.douyin.com/user/abc") is None
