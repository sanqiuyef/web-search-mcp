# -*- coding: utf-8 -*-
"""
SearXNG 元搜索引擎后端（Phase 2 预留）。
通过 SearXNG API 获取 70+ 搜索引擎的聚合结果。
"""

from typing import Dict, List, Optional
from urllib.parse import urljoin

import requests

from src.cache import make_cache_key, cache_get, cache_set


def search_searxng_items(
    keyword: str,
    max_results: int,
    categories: Optional[str] = None,
    engines: Optional[str] = None,
    language: str = "zh-CN",
    time_range: Optional[str] = None,
) -> List[Dict]:
    """
    SearXNG 搜索，返回统一格式。

    需要配置环境变量 WS_SEARXNG_URL 指向 SearXNG 实例地址。
    可选配置 WS_SEARXNG_USERNAME / WS_SEARXNG_PASSWORD 用于 Basic Auth。
    """
    from src.config import SEARXNG_URL, SEARXNG_USERNAME, SEARXNG_PASSWORD, SEARCH_TIMEOUT

    if not SEARXNG_URL:
        return []

    cache_key = make_cache_key("searxng", keyword, max_results,
                               f"{categories or 'general'}:{engines or ''}:{language}:{time_range or ''}")
    cached = cache_get(cache_key)
    if cached:
        return cached

    params = {
        "q": keyword,
        "format": "json",
        "language": language,
    }
    if categories:
        params["categories"] = categories
    if engines:
        params["engines"] = engines
    if time_range:
        params["time_range"] = time_range

    auth = None
    if SEARXNG_USERNAME and SEARXNG_PASSWORD:
        auth = (SEARXNG_USERNAME, SEARXNG_PASSWORD)

    try:
        url = urljoin(SEARXNG_URL.rstrip("/") + "/", "search")
        resp = requests.get(url, params=params, auth=auth, timeout=SEARCH_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        # 解析 SearXNG JSON 格式
        # ref: https://docs.searxng.org/dev/search_api.html
        results = []
        for item in data.get("results", []):
            results.append({
                "title": item.get("title", "").strip(),
                "url": item.get("url", ""),
                "snippet": item.get("content", "").strip(),
                "engine": item.get("engine", "searxng"),
                "_searxng_category": item.get("category", ""),
                "_searxng_score": item.get("score", 0),
            })

        # 按 SearXNG 评分排序
        results.sort(key=lambda x: x.get("_searxng_score", 0), reverse=True)
        results = results[:max_results]

        # 清理内部字段
        for r in results:
            r.pop("_searxng_score", None)
            r.pop("_searxng_category", None)

        cache_set(cache_key, results)
        return results
    except Exception:
        return []
