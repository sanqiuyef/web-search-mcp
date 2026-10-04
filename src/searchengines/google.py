# -*- coding: utf-8 -*-
"""
Google 搜索后端（需要代理）。
支持指数退避重试 + 多级 CSS 选择器降级。
"""

from typing import Dict, List, Optional
from urllib.parse import urlparse, parse_qs, quote

from bs4 import BeautifulSoup

from src.cache import make_cache_key, cache_get, cache_set
from src.utils import normalize_url, has_proxy, make_session
from src.retry import retry


# 多级 CSS 选择器：Google 改版时自动降级
_GOOGLE_RESULT_SELECTORS = [
    "div.g",                        # 经典版
    "div.MjjYud",                   # 2024+ 新版
    "div[data-hveid]",              # 数据驱动版
    "div[data-sokoban-container]",  # 2025 新版
    "#search div[role='list'] > div",  # 搜索结果列表
]

_GOOGLE_TITLE_SELECTORS = [
    "h3.LC20lb",     # 当前标准标题类
    "h3",            # 通用 h3
    "a > h3",        # 链接内的 h3
    "a[href]",       # 如果标题不在 h3 里
]

_GOOGLE_SNIPPET_SELECTORS = [
    "div.VwiC3b",         # 当前标准摘要类
    "span.st",            # 旧版摘要
    "div[data-sncf]",     # 数据驱动版摘要
    ".lEBKkf",            # 另一新版摘要
    "span.aCOpRe",        # 移动端摘要
    "div[role='heading']", # 通用 heading
]

_GOOGLE_NEWS_CARD_SELECTORS = [
    "article",
    "div[data-id]",
    ".B1uWZe",
    ".xrnccd",
]

# time_range → Google tbs=qdr: 参数（d/w/m/y）
_GOOGLE_QDR = {
    "day": "d",
    "week": "w",
    "month": "m",
    "year": "y",
}


def _google_lang_param(language: str) -> str:
    """language (zh-CN/en/...) → Google lr 参数 (lang_zh-CN)。"""
    return f"lang_{language}"


def _extract_google_result(result_el) -> Optional[Dict]:
    """从单个 Google 结果元素提取信息，尝试多组选择器。"""
    # 提取标题 + 链接
    title = ""
    href = ""
    for sel in _GOOGLE_TITLE_SELECTORS:
        title_el = result_el.select_one(sel)
        if not title_el:
            continue
        t = title_el.get_text(strip=True)
        if not t:
            continue
        title = t
        # 找到链接
        link_el = title_el if title_el.name == "a" else title_el.find_parent("a") or result_el.select_one("a[href]")
        if link_el:
            h = link_el.get("href", "")
            if h:
                # Google 搜索结果链接通常是 /url?q=REAL_URL 格式
                if h.startswith("/url?"):
                    qs_parsed = parse_qs(urlparse(h).query)
                    h = qs_parsed.get("q", [h])[0]
                href = h
                break

    if not title or not href:
        return None

    # 提取摘要
    snippet = ""
    for sel in _GOOGLE_SNIPPET_SELECTORS:
        body_el = result_el.select_one(sel)
        if body_el:
            s = body_el.get_text(strip=True)
            if s:
                snippet = s
                break

    return {
        "title": title,
        "url": normalize_url(href),
        "snippet": snippet,
        "engine": "google",
    }


@retry(max_attempts=2, base_delay=1.0, exceptions=(ConnectionError, TimeoutError, OSError))
def _do_search_google(
    keyword: str,
    max_results: int,
    time_range: Optional[str] = None,
    language: Optional[str] = None,
) -> List[Dict]:
    """执行实际的 Google 搜索（带重试）。

    重试挂在真正发请求的函数上：外层 search_google_items 会吞掉异常。
    """
    session = make_session()
    encoded_q = quote(keyword, safe="")
    url = f"https://www.google.com/search?q={encoded_q}&num={max_results}&hl=zh-CN"
    # 时间过滤：tbs=qdr:d|w|m|y
    if time_range and time_range in _GOOGLE_QDR:
        url += f"&tbs=qdr:{_GOOGLE_QDR[time_range]}"
    # 语言偏好：lr=lang_zh-CN
    if language:
        url += f"&lr={quote(_google_lang_param(language), safe='')}"
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    seen_urls = set()

    for selector in _GOOGLE_RESULT_SELECTORS:
        items = soup.select(selector)
        if not items:
            continue
        for item in items:
            parsed = _extract_google_result(item)
            if parsed and parsed["url"] not in seen_urls:
                seen_urls.add(parsed["url"])
                results.append(parsed)
                if len(results) >= max_results:
                    return results

    return results


def search_google_items(
    keyword: str,
    max_results: int,
    time_range: Optional[str] = None,
    language: Optional[str] = None,
) -> List[Dict]:
    """Google 网页搜索，返回统一格式（需要代理）。

    time_range: day|week|month|year，仅对近期内容过滤
    language: 语言偏好，如 zh-CN
    """
    if not has_proxy():
        return []

    cache_key = make_cache_key("google", keyword, max_results,
                               f"{time_range or ''}:{language or ''}")
    cached = cache_get(cache_key)
    if cached:
        return cached
    try:
        results = _do_search_google(keyword, max_results, time_range, language)
    except Exception:
        return []
    if results:
        cache_set(cache_key, results)
    return results


@retry(max_attempts=2, base_delay=1.0, exceptions=(ConnectionError, TimeoutError, OSError))
def _do_search_google_news(keyword: str, max_results: int) -> List[Dict]:
    """执行实际的 Google 新闻搜索（带重试）。"""
    session = make_session()
    encoded_q = quote(keyword, safe="")
    url = f"https://news.google.com/search?q={encoded_q}&hl=zh-CN"
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    seen_urls = set()

    for selector in _GOOGLE_NEWS_CARD_SELECTORS:
        for article in soup.select(selector):
            title_el = article.select_one("h3 a, a[href*='.'], h4")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            if not title or not href:
                continue
            if href.startswith("./"):
                href = "https://news.google.com/" + href[2:]
            if href in seen_urls:
                continue
            seen_urls.add(href)

            body_el = article.select_one("h4, div[role='heading'], p, .snippet")
            body = body_el.get_text(strip=True) if body_el else ""

            results.append({
                "title": title,
                "url": normalize_url(href),
                "snippet": body,
                "engine": "google",
            })
            if len(results) >= max_results:
                return results

    return results


def search_google_news_items(keyword: str, max_results: int) -> List[Dict]:
    """Google 新闻搜索，返回统一格式（需要代理）。"""
    if not has_proxy():
        return []

    cache_key = make_cache_key("google_news", keyword, max_results, "news")
    cached = cache_get(cache_key)
    if cached:
        return cached
    try:
        results = _do_search_google_news(keyword, max_results)
    except Exception:
        return []
    if results:
        cache_set(cache_key, results)
    return results
