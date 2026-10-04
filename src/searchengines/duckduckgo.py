# -*- coding: utf-8 -*-
"""
DuckDuckGo 搜索后端。
使用 duckduckgo_search 库，失败时自动降级到 HTML 解析。
"""

import time
import warnings
# 消除 ddgs/duckduckgo_search 改名警告
warnings.filterwarnings("ignore", message="This package.*has been renamed")

from typing import Dict, List, Optional

from src.cache import make_cache_key, cache_get, cache_set
from src.utils import normalize_url, filter_junk_results


# DuckDuckGo HTML 搜索 URL（降级备用）
_DDG_HTML_URL = "https://html.duckduckgo.com/html/?q={q}"

# HTML 兜底请求超时（秒）。必须留出余量：库路径 + 兜底路径的总耗时
# 不能超过上层 ENGINE_TIMEOUT，否则整个引擎被判超时、结果全部丢弃。
_HTML_FALLBACK_TIMEOUT = 5

# 兜底路径总预算（秒）。单次 requests 的 timeout 只管单次收发，
# “先取首页 cookie 再重试”这条支路会发 3 次请求，按单次超时累加能到 15s+，
# 实测足以把引擎拖过聚合层的超时线。这里给整条兜底路径设总预算。
_HTML_FALLBACK_BUDGET = 8

# 兜底路径冷却：实测本机网络下 html.duckduckgo.com 只会返回 202 空壳（无结果）。
# 一次失败后 10 分钟内不再尝试，避免每次搜索都白等一遍。
_HTML_FALLBACK_COOLDOWN = 600
_html_fallback_dead_until = 0.0

# 新闻端点冷却：ddgs.news 直连 duckduckgo.com（本机不可达，实测每次都超时 6s）。
# 一次失败后同样进入冷却，避免每个新闻查询都白等一个超时周期。
_NEWS_COOLDOWN = 600
_news_dead_until = 0.0


def _html_fallback_available() -> bool:
    return time.time() >= _html_fallback_dead_until


def _mark_html_fallback_dead() -> None:
    global _html_fallback_dead_until
    _html_fallback_dead_until = time.time() + _HTML_FALLBACK_COOLDOWN


def _news_library_available() -> bool:
    return time.time() >= _news_dead_until


def _mark_news_library_dead() -> None:
    global _news_dead_until
    _news_dead_until = time.time() + _NEWS_COOLDOWN


# time_range → ddgs 库 timelimit 参数（d/w/m/y）
_DDGS_TIMELIMIT = {
    "day": "d",
    "week": "w",
    "month": "m",
    "year": "y",
}

# time_range → HTML 版日期范围（天数窗口）
_DDG_HTML_DAYS = {
    "day": 1,
    "week": 7,
    "month": 30,
    "year": 365,
}


def _ddg_date_range(time_range: str) -> Optional[str]:
    """time_range → DuckDuckGo HTML 的 df=YYYY-MM-DD..YYYY-MM-DD 日期范围。"""
    days = _DDG_HTML_DAYS.get(time_range)
    if not days:
        return None
    from datetime import date, timedelta
    today = date.today()
    start = today - timedelta(days=days)
    return f"{start.isoformat()}..{today.isoformat()}"


def _get_proxy_for_ddgs():
    """获取 DDGS 库所需的代理字符串（首选 https，降级到 http）。"""
    from src.config import get_proxies
    proxies = get_proxies()
    if not proxies:
        return None
    return proxies.get("https") or proxies.get("http") or list(proxies.values())[0]


def _search_via_library(keyword: str, max_results: int, time_range: Optional[str] = None) -> Optional[List[Dict]]:
    """通过 ddgs 库搜索（首选，快但易被限）。

    显式指定 backend/region/timeout，不用 "auto"：
    auto 会把 wikipedia/grokipedia 排在最前，裸词查询会退化成词典词条，
    且后端逐个轮转容易撞上整体超时。

    返回 None 表示库本身报错（可尝试 HTML 兜底）；返回 [] 表示后端正常但没有结果。
    """
    import logging

    logger = logging.getLogger("web-search.duckduckgo")
    try:
        # 兼容新旧包名
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS

        from src.config import DDGS_BACKENDS, DDGS_REGION, DDGS_TIMEOUT

        kwargs = {"timeout": DDGS_TIMEOUT}
        proxy_str = _get_proxy_for_ddgs()
        if proxy_str:
            # ddgs v9 参数名为 proxy；旧版 duckduckgo_search 用 proxies
            if hasattr(DDGS, "text") and DDGS.__module__.startswith("ddgs"):
                kwargs["proxy"] = proxy_str
            else:
                kwargs["proxies"] = proxy_str

        # 时间过滤：ddgs.text 的 timelimit 支持 d/w/m/y
        if time_range and time_range in _DDGS_TIMELIMIT:
            kwargs["timelimit"] = _DDGS_TIMELIMIT[time_range]

        results = []
        with DDGS(**kwargs) as ddgs:
            for r in ddgs.text(keyword, max_results=max_results,
                               backend=DDGS_BACKENDS, region=DDGS_REGION):
                results.append({
                    "title": r.get("title", "").strip(),
                    "url": normalize_url(r.get("href", "")),
                    "snippet": r.get("body", "").strip(),
                    "engine": "duckduckgo",
                })
        if results:
            return filter_junk_results(results)
        return []
    except Exception as exc:
        logger.info("ddgs 库搜索失败，降级 HTML: %r", exc)
        return None


def _search_via_html(keyword: str, max_results: int, time_range: Optional[str] = None, language: Optional[str] = None) -> List[Dict]:
    """降级方案：解析 DuckDuckGo 的 HTML 版搜索结果。"""
    from urllib.parse import quote
    from bs4 import BeautifulSoup
    import requests

    encoded_q = quote(keyword, safe="")
    url = _DDG_HTML_URL.format(q=encoded_q)
    # 时间过滤：df=YYYY-MM-DD..YYYY-MM-DD（DuckDuckGo 日期范围）
    date_range = _ddg_date_range(time_range) if time_range else None
    if date_range:
        url += f"&df={quote(date_range, safe='')}"
    # 语言偏好：hl=zh-CN
    if language:
        url += f"&hl={quote(language, safe='')}"

    # 使用更逼真的浏览器请求头，避免被 202 拦截
    ddg_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "Referer": "https://duckduckgo.com/",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    # 整条兜底路径的总预算：超预算后不再发起新请求，直接返回已拿到的内容
    deadline = time.time() + _HTML_FALLBACK_BUDGET

    def _left() -> float:
        return max(1.0, min(_HTML_FALLBACK_TIMEOUT, deadline - time.time()))

    try:
        resp = requests.get(url, headers=ddg_headers, timeout=_left())
        resp.raise_for_status()
        resp.encoding = "utf-8"
    except Exception:
        return []

    # 如果返回 202 或空内容，尝试带 cookies 的 session
    if (resp.status_code == 202 or len(resp.text) < 2000) and time.time() < deadline:
        try:
            from src.utils import make_session
            session = make_session()
            # 先访问首页获取 cookies
            session.get("https://duckduckgo.com/", timeout=_left())
            resp = session.get(url, timeout=_left())
            resp.encoding = "utf-8"
        except Exception:
            return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for result in soup.select(".result"):
        title_el = result.select_one(".result__title a, .result__a")
        snippet_el = result.select_one(".result__snippet, .snippet")

        if not title_el:
            continue

        title = title_el.get_text(strip=True)
        href = title_el.get("href", "")

        # DuckDuckGo 的链接是重定向格式，需要提取真实 URL
        if "uddg=" in href:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(href)
            qs = parse_qs(parsed.query)
            href = qs.get("uddg", [href])[0]

        snippet = snippet_el.get_text(strip=True) if snippet_el else ""

        if title and href:
            results.append({
                "title": title,
                "url": normalize_url(href),
                "snippet": snippet,
                "engine": "duckduckgo",
            })
            if len(results) >= max_results:
                break

    return results


def search_duckduckgo_items(
    keyword: str,
    max_results: int,
    time_range: Optional[str] = None,
    language: Optional[str] = None,
) -> List[Dict]:
    """DuckDuckGo 网页搜索，返回统一格式。优先用库，库报错时才降级 HTML 解析。

    time_range: day|week|month|year，仅对近期内容过滤
    language: 语言偏好，如 zh-CN（仅 HTML 降级路径生效）
    """
    cache_key = make_cache_key("duckduckgo", keyword, max_results,
                               f"{time_range or ''}:{language or ''}")
    cached = cache_get(cache_key)
    if cached:
        return cached

    # 方案一：用库（快，但可能被限）
    results = _search_via_library(keyword, max_results, time_range)
    if results is None:
        # 方案二：库报错才降级到 HTML 解析（后端无结果属于正常空结果，不兜底）
        if _html_fallback_available():
            results = _search_via_html(keyword, max_results, time_range, language)
            if not results:
                _mark_html_fallback_dead()
            results = filter_junk_results(results)
        else:
            results = []

    if results:
        cache_set(cache_key, results)
    return results


def search_duckduckgo_news_items(keyword: str, max_results: int) -> List[Dict]:
    """DuckDuckGo 新闻搜索。同样带降级。"""
    from urllib.parse import quote
    from bs4 import BeautifulSoup

    cache_key = make_cache_key("duckduckgo_news", keyword, max_results, "news")
    cached = cache_get(cache_key)
    if cached:
        return cached

    results = []

    # 方案一：用库（端点不可达时进入冷却，不再每次都白等一个超时周期）
    if _news_library_available():
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS

            from src.config import DDGS_TIMEOUT

            proxy_str = _get_proxy_for_ddgs()
            kwargs = {"timeout": DDGS_TIMEOUT}
            if proxy_str:
                if DDGS.__module__.startswith("ddgs"):
                    kwargs["proxy"] = proxy_str
                else:
                    kwargs["proxies"] = proxy_str
            with DDGS(**kwargs) as ddgs:
                for r in ddgs.news(keyword, max_results=max_results):
                    results.append({
                        "title": r.get("title", "").strip(),
                        "url": normalize_url(r.get("url", "")),
                        "snippet": r.get("body", "").strip(),
                        "engine": "duckduckgo",
                    })
            results = filter_junk_results(results)
            if results:
                cache_set(cache_key, results)
                return results
        except Exception:
            _mark_news_library_dead()

    # 方案二：降级到 HTML 解析（与网页搜索共用同一套冷却与超时）
    if not _html_fallback_available():
        return results
    try:
        from src.utils import make_session
        session = make_session()
        encoded_q = quote(keyword, safe="")
        url = f"https://html.duckduckgo.com/html/?q={encoded_q}&t=h_&ia=news"
        resp = session.get(url, timeout=_HTML_FALLBACK_TIMEOUT)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        for result in soup.select(".result"):
            title_el = result.select_one(".result__title a, .result__a")
            snippet_el = result.select_one(".result__snippet, .snippet")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            if "uddg=" in href:
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(href)
                qs = parse_qs(parsed.query)
                href = qs.get("uddg", [href])[0]
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            if title and href:
                results.append({
                    "title": title,
                    "url": normalize_url(href),
                    "snippet": snippet,
                    "engine": "duckduckgo",
                })
                if len(results) >= max_results:
                    break
    except Exception:
        _mark_html_fallback_dead()
        return results

    results = filter_junk_results(results)
    if results:
        cache_set(cache_key, results)
    return results
