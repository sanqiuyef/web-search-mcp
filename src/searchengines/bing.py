# -*- coding: utf-8 -*-
"""
Bing 搜索后端（国内可直连）。
支持指数退避重试 + 多级 CSS 选择器降级。
"""

from typing import Dict, List, Optional
from urllib.parse import quote

from bs4 import BeautifulSoup

from src.cache import make_cache_key, cache_get, cache_set
from src.utils import normalize_url, make_session
from src.retry import retry


# 多级 CSS 选择器：Bing 改版时自动降级
_BING_RESULT_SELECTORS = [
    "li.b_algo",            # 经典版
    "li.b_ans",             # 新版答案卡片
    ".b_algo",              # 部分区域版
    "#b_results > li",      # 通用结果列表
    "[data-bm='0'] > li",   # 数据驱动版
]

# 非结果容器：广告、分页条、提示信息。落到 "#b_results > li" 这类宽选择器时
# 会把它们当成结果（实测分页条能抽出“下一页”链接）。
_BING_NON_RESULT_CLASSES = ("b_ad", "b_pag", "b_msg", "b_algoheader", "b_ans_ads")


def _is_organic_result_node(li) -> bool:
    """判断节点是否为自然结果，排除广告/分页/提示容器。"""
    classes = " ".join(li.get("class") or [])
    return not any(bad in classes for bad in _BING_NON_RESULT_CLASSES)

# time_range → Bing filters 日期窗口（天）
_BING_TIME_DAYS = {
    "day": 1,
    "week": 7,
    "month": 30,
    "year": 365,
}


def _bing_time_filter(time_range: str) -> str:
    """time_range → Bing filters 参数（ex1:"ez5_YYYYMMDD_YYYYMMDD"）。"""
    from datetime import date, timedelta
    days = _BING_TIME_DAYS.get(time_range, 365)
    today = date.today()
    start = today - timedelta(days=days)
    return f'ex1:"ez5_{start.strftime("%Y%m%d")}_{today.strftime("%Y%m%d")}"'

_BING_TITLE_SELECTORS = [
    "h2 a",
    "a[href]",
    "h2",
]

_BING_SNIPPET_SELECTORS = [
    ".b_caption p",
    ".b_caption",
    ".b_snippet",
    ".b_lineclamp2",
    "p",
]


def _extract_bing_item(li) -> Optional[Dict]:
    """从单个结果元素中提取信息，尝试多组选择器。"""
    if not _is_organic_result_node(li):
        return None

    title_el = None
    for sel in _BING_TITLE_SELECTORS:
        title_el = li.select_one(sel)
        if title_el and title_el.get_text(strip=True):
            break

    if not title_el:
        return None

    title = title_el.get_text(strip=True)
    href = title_el.get("href", "") if title_el.name == "a" else ""

    # 如果标题不在 <a> 里，尝试从其他 <a> 提取 href
    if not href:
        link_el = li.select_one("a[href]")
        if link_el:
            href = link_el.get("href", "")

    if not title or not href or not href.startswith(("http://", "https://")):
        return None

    body = ""
    for sel in _BING_SNIPPET_SELECTORS:
        body_el = li.select_one(sel)
        if body_el and body_el.get_text(strip=True):
            body = body_el.get_text(strip=True)
            break

    return {
        "title": title,
        "url": normalize_url(href),
        "snippet": body,
        "engine": "bing",
    }


@retry(max_attempts=2, base_delay=1.0, exceptions=(ConnectionError, TimeoutError, OSError))
def _do_search_bing(
    keyword: str,
    max_results: int,
    time_range: Optional[str] = None,
    language: Optional[str] = None,
) -> List[Dict]:
    """执行实际的 Bing 搜索（带重试）。

    重试必须挂在真正发请求的函数上：外层 search_bing_items 会吞掉所有异常，
    装饰在它上面等于不重试。
    """
    session = make_session()
    encoded_q = quote(keyword, safe="")
    url = f"https://cn.bing.com/search?q={encoded_q}&count={max_results}"
    # 时间过滤：filters=ex1:"ez5_YYYYMMDD_YYYYMMDD"
    if time_range and time_range in _BING_TIME_DAYS:
        url += f"&filters={quote(_bing_time_filter(time_range), safe='')}"
    # 语言偏好
    if language:
        url += f"&setlang={quote(language, safe='')}"
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    seen_urls = set()

    # 尝试多组选择器
    for selector in _BING_RESULT_SELECTORS:
        items = soup.select(selector)
        if not items:
            continue
        for li in items:
            item = _extract_bing_item(li)
            if item and item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                results.append(item)
                if len(results) >= max_results:
                    return results

    return results


def search_bing_items(
    keyword: str,
    max_results: int,
    time_range: Optional[str] = None,
    language: Optional[str] = None,
) -> List[Dict]:
    """Bing 网页搜索，返回统一格式。

    time_range: day|week|month|year，仅对近期内容过滤
    language: 语言偏好，如 zh-CN
    """
    cache_key = make_cache_key("bing", keyword, max_results,
                               f"{time_range or ''}:{language or ''}")
    cached = cache_get(cache_key)
    if cached:
        return cached
    try:
        results = _do_search_bing(keyword, max_results, time_range, language)
    except Exception:
        # 网络/HTTP 失败：不写缓存，否则一次抖动会让该查询空 5 分钟
        return []
    if results:
        cache_set(cache_key, results)
    return results


@retry(max_attempts=2, base_delay=1.0, exceptions=(ConnectionError, TimeoutError, OSError))
def _do_search_bing_news(keyword: str, max_results: int) -> List[Dict]:
    """执行实际的 Bing 新闻搜索（带重试）。"""
    session = make_session()
    encoded_q = quote(keyword, safe="")
    url = f"https://cn.bing.com/news/search?q={encoded_q}&count={max_results}"
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    seen_urls = set()

    # 多级选择器匹配 Bing 新闻的不同版式
    news_card_selectors = [
        "a.title",
        "a[href*='news']",
        "article a[href]",
        ".news-card a",
        "[data-content] a",
    ]
    snippet_selectors = [
        "p, .snippet, .caption p, .news-snippet, .b_snippet",
    ]

    for sel in news_card_selectors:
        for card in soup.select(sel):
            title = card.get_text(strip=True)
            href = card.get("href", "")
            if not title or not href or href in seen_urls:
                continue
            if not href.startswith(("http://", "https://")):
                continue
            seen_urls.add(href)
            # 尝试多条摘要选择器
            snippet = ""
            for s_sel in snippet_selectors:
                container = card.find_parent(["div", "article", "li"])
                if container:
                    snippet_el = container.select_one(s_sel)
                    if snippet_el:
                        snippet = snippet_el.get_text(strip=True)
                        break
            results.append({
                "title": title,
                "url": normalize_url(href),
                "snippet": snippet,
                "engine": "bing",
            })
            if len(results) >= max_results:
                return results

    return results


def search_bing_news_items(keyword: str, max_results: int) -> List[Dict]:
    """Bing 新闻搜索。"""
    cache_key = make_cache_key("bing_news", keyword, max_results, "news")
    cached = cache_get(cache_key)
    if cached:
        return cached
    try:
        results = _do_search_bing_news(keyword, max_results)
    except Exception:
        return []
    if results:
        cache_set(cache_key, results)
    return results
