# -*- coding: utf-8 -*-
"""
聚合搜索工具：多引擎并行、结果去重融合评分。
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from src.config import (
    MAX_RESULTS_LIMIT,
    SEARXNG_URL,
    DEFAULT_LANGUAGE,
    TIME_RANGE_VALUES,
    SEARCH_TIMEOUT,
    RELEVANCE_MIN_STRONG,
    RELEVANCE_REPORT_DROPPED,
)
from src.ratelimit import search_limiter, engine_limiters, fetch_limiter
from src.searchengines.duckduckgo import search_duckduckgo_items, search_duckduckgo_news_items
from src.searchengines.bing import search_bing_items, search_bing_news_items
from src.searchengines.google import search_google_items, search_google_news_items
from src.searchengines.searxng import search_searxng_items
from src.utils import (
    merge_results,
    format_merged_results,
    make_tool_result,
    make_error_result,
    filter_junk_results,
    split_by_relevance,
    note_engine_result,
    engine_available,
    engine_cooldown_remaining,
    ENGINE_LABELS,
    has_proxy,
)

logger = logging.getLogger("web-search.advanced")

# 单个引擎的墙钟超时：超过即放弃该引擎的结果，用其余引擎继续出结果。
# 取全局 SEARCH_TIMEOUT 的 1.5 倍，留出重试余量但不允许无限拖长。
ENGINE_TIMEOUT = max(int(SEARCH_TIMEOUT * 1.5), 10)


def _build_engine_map(category: str, time_range: Optional[str] = None,
                      language: Optional[str] = None) -> Dict[str, callable]:
    """构建引擎映射，按可用性条件包含 SearXNG 与 Google。"""
    if category == "news":
        base = {
            "duckduckgo": search_duckduckgo_news_items,
            "bing": search_bing_news_items,
            "google": search_google_news_items,
        }
    else:
        # 网页搜索透传时间/语言参数
        base = {
            "duckduckgo": lambda kw, mr: search_duckduckgo_items(kw, mr, time_range, language),
            "bing": lambda kw, mr: search_bing_items(kw, mr, time_range, language),
            "google": lambda kw, mr: search_google_items(kw, mr, time_range, language),
        }

    # Google 无代理时必然返回空：不放进引擎列表，避免“引擎里列着但从未供数”
    if not has_proxy():
        base.pop("google", None)

    # 仅当配置了 SearXNG URL 时加入（SearXNG 原生支持 categories/time_range/language）
    if SEARXNG_URL:
        base["searxng"] = lambda kw, mr: search_searxng_items(
            kw, mr, categories=category, time_range=time_range, language=language
        )

    return base


def _fetch_content_for_results(results: List[Dict], max_chars: int = 1000) -> Dict[str, str]:
    """抓取结果正文（带限流），返回 {url: content}。失败/被限的条目跳过。"""
    from src.tools.web_fetch import web_fetch

    contents: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        future_map = {}
        for item in results[:5]:  # 最多抓前 5 条，避免烧额度/拖慢响应
            url = item.get("url", "")
            if not url:
                continue
            # 不在此预检限流：web_fetch 内部会检查 fetch_limiter 并返回 error 结果
            future_map[executor.submit(web_fetch, url, max_chars, "text")] = url
        for future in as_completed(future_map):
            url = future_map[future]
            try:
                res = future.result()
                meta = res.get("meta") or {}
                if not meta.get("error"):
                    content = (res.get("content") or "").strip()
                    if content:
                        contents[url] = content
            except Exception:
                continue
    return contents


def _run_engines(engine_map: Dict[str, callable], keyword: str,
                 max_results: int) -> Tuple[List[Tuple[str, List[Dict]]], List[str]]:
    """并行执行各引擎（带逐引擎墙钟超时），返回 (成功结果, 错误说明)。

    任一引擎卡住都不拖死整次聚合：超时的引擎记为错误并触发熔断计数。
    """
    import concurrent.futures as _cf

    engine_results: List[Tuple[str, List[Dict]]] = []
    engine_errors: List[str] = []
    executor = ThreadPoolExecutor(max_workers=max(1, len(engine_map)))
    try:
        def _search_with_limit(name: str, fn: callable) -> tuple:
            """返回 (engine_name, results_or_None, error_or_None)"""
            # 检查引擎级率限（每个引擎独立）
            eng_limiter = engine_limiters.get(name)
            if eng_limiter and not eng_limiter.allow():
                return (name, None, f"{name} 请求频率已达上限")
            try:
                res = fn(keyword, max_results) or []
                return (name, res, None)
            except Exception as e:
                return (name, None, str(e))

        future_map = {executor.submit(_search_with_limit, name, fn): name
                      for name, fn in engine_map.items()}
        done, not_done = _cf.wait(future_map, timeout=ENGINE_TIMEOUT)
        for future in done:
            try:
                name, res, err = future.result()
                if res:
                    engine_results.append((name, res))
                    note_engine_result(name, True)
                elif err:
                    engine_errors.append(err)
                    logger.debug("[%s] %s", name, err)
            except Exception as e:
                engine_errors.append(str(e))
        # 超时引擎：记录但不阻塞返回；连续超时会触发熔断，避免一直被拖住
        for future in not_done:
            future.cancel()
            name = future_map[future]
            note_engine_result(name, False)
            left = engine_cooldown_remaining(name)
            suffix = f"，已停用 {left}s" if left else ""
            engine_errors.append(f"{name} 超时(>{ENGINE_TIMEOUT}s){suffix}")
    finally:
        # 不等待未完成线程（cancel 对已运行线程无效），直接释放
        executor.shutdown(wait=False)

    return engine_results, engine_errors


def web_search_advanced(
    keyword: str,
    max_results: Optional[int] = 10,
    category: Optional[str] = "general",
    engines: Optional[str] = None,
    time_range: Optional[str] = None,
    language: Optional[str] = None,
    fetch_content: bool = False,
) -> dict:
    """
    聚合搜索：并行查询多个引擎，合并去重后按评分排序。

    Args:
        keyword: 搜索关键词
        max_results: 最大结果数（1-20）
        category: 搜索类别 general | news（默认 general）
        engines: 要查询的引擎，逗号分隔如 "duckduckgo,bing,searxng"。默认全部可用引擎。
        time_range: 时间范围过滤 day | week | month | year，仅返回该时间内的结果
        language: 语言偏好，如 zh-CN / en（默认 zh-CN）
        fetch_content: 为 True 时抓取前 5 条结果的正文附在返回里（供直接引用）
    """
    max_results = min(max_results, MAX_RESULTS_LIMIT)
    category = category or "general"
    language = language or DEFAULT_LANGUAGE

    if time_range and time_range not in TIME_RANGE_VALUES:
        return make_error_result(
            f"time_range 仅支持 {', '.join(TIME_RANGE_VALUES)}（当前: {time_range}）"
        )

    # 选择引擎映射（条件包含 SearXNG，透传时间/语言）
    engine_map = _build_engine_map(category, time_range, language)

    # 熔断中的引擎本轮不参与，并说明原因
    cooling = [name for name in engine_map if not engine_available(name)]
    for name in cooling:
        engine_map.pop(name)

    # 用户指定引擎过滤
    if engines:
        selected = [e.strip().lower() for e in engines.split(",")]
        engine_map = {k: v for k, v in engine_map.items() if k in selected}

    if not engine_map:
        if cooling:
            return make_error_result(
                "搜索引擎均处于临时停用状态（此前连续超时）："
                + "、".join(f"{ENGINE_LABELS.get(n, n)} 约 {engine_cooldown_remaining(n)}s 后恢复"
                            for n in cooling)
                + "\n  可直接重试，或稍后再试。"
            )
        return make_error_result("没有可用的搜索引擎，请检查参数或网络配置。")

    # 全局率限检查（整次搜索只扣一次，不是每个引擎扣一次）
    if not search_limiter.allow():
        return make_error_result("搜索请求过于频繁，请稍后再试。")

    engine_results, engine_errors = _run_engines(engine_map, keyword, max_results)

    # 新闻分类当前没有可用引擎（实测：Bing 新闻页是 JS 空壳、DDG 新闻端点不可达、
    # Google 新闻需要代理），退化为网页搜索 + 时间过滤，而不是返回空结果
    news_fallback_note = ""
    if not engine_results and category == "news":
        fallback_map = _build_engine_map("general", time_range, language)
        fallback_map = {k: v for k, v in fallback_map.items() if engine_available(k)}
        if engines:
            selected = [e.strip().lower() for e in engines.split(",")]
            fallback_map = {k: v for k, v in fallback_map.items() if k in selected}
        if fallback_map:
            engine_map = fallback_map
            category = "general"
            engine_results, fallback_errors = _run_engines(engine_map, keyword, max_results)
            if engine_results:
                # 新闻引擎的报错不再展示：已换成网页搜索，那些报错只会干扰判断
                engine_errors = []
                news_fallback_note = (
                    "新闻分类无可用引擎（Bing 新闻页需 JS 渲染、DuckDuckGo 新闻端点不可达），"
                    "已改用网页搜索" + ("（含时间过滤）" if time_range else "")
                )
            else:
                engine_errors.extend(fallback_errors)

    if not engine_results:
        msg = f"所有搜索引擎对「{keyword}」均未返回结果。"
        if engine_errors:
            msg += "\n  引擎状态: " + "; ".join(engine_errors)
        if not has_proxy():
            msg += "\n🛑 当前未配置代理，Google 未参与检索（仅 DuckDuckGo/Bing）。"
        return make_error_result(msg, detail="; ".join(engine_errors) if engine_errors else None)

    # 清洗：剔除词典/百科噪声，再按查询词命中情况分出强相关（A）/弱相关（B）/不相关（C）
    engine_strong: Dict[str, List[Dict]] = {}
    engine_weak: Dict[str, List[Dict]] = {}
    dropped_notes: List[str] = []
    for name, raw in engine_results:
        kept = filter_junk_results(raw)
        strong_hits, weak_hits, dropped = split_by_relevance(keyword, kept)
        # 剔除条数够多、或该引擎整批结果都被剔除时给出说明：
        # 后者是“引擎返回了偏题结果集”，不说明会让上层以为该引擎只是没结果
        if dropped and (dropped >= RELEVANCE_REPORT_DROPPED or dropped == len(kept)):
            dropped_notes.append(
                f"{ENGINE_LABELS.get(name, name)} 剔除 {dropped} 条与查询词不匹配的结果"
            )
        if strong_hits:
            engine_strong[name] = strong_hits
        if weak_hits:
            engine_weak[name] = weak_hits

    # 引擎内聚：某引擎一条强相关都没有，说明它对本次查询没有实质命中，
    # 其“弱相关”结果通常是只蹭到泛化词（如查询里的地名/年份）的页面，不采用。
    # 只有所有引擎都没有强相关结果时，才整体退回弱相关结果。
    any_strong = bool(engine_strong)
    strong_lists = [v for v in engine_strong.values() if v]
    if any_strong:
        weak_lists = [v for k, v in engine_weak.items() if k in engine_strong]
        for name in engine_weak:
            if name not in engine_strong:
                dropped_notes.append(
                    f"{ENGINE_LABELS.get(name, name)} 无强相关结果，未采用其弱相关结果"
                )
    else:
        weak_lists = [v for v in engine_weak.values() if v]

    # 强相关优先：够用就只给强相关结果；不足时才用弱相关结果补位
    merged = merge_results(strong_lists, max_results, keyword=keyword) if strong_lists else []
    min_strong = min(RELEVANCE_MIN_STRONG, max_results)
    weak_used = False
    if len(merged) < min_strong and weak_lists:
        merged_weak = merge_results(weak_lists, max_results, keyword=keyword)
        merged = (merged + merged_weak)[:max_results]
        weak_used = True

    if not merged:
        msg = (
            f"未找到与「{keyword}」相关的结果：引擎返回的内容与查询词不匹配，已全部剔除。\n"
            "  可尝试更换关键词（去掉年份、地名等泛化词，改用更具体的主题词）后重试。"
        )
        if dropped_notes:
            msg += "\n  " + "；".join(dropped_notes)
        if engine_errors:
            msg += "\n  引擎状态: " + "; ".join(engine_errors)
        return make_error_result(msg, detail="; ".join(dropped_notes))

    # 合并后重排编号，保证与展示顺序一致
    for i, item in enumerate(merged, 1):
        item["index"] = i

    # 可选：抓取前几条结果的正文（供直接引用）
    fetched_contents: Dict[str, str] = {}
    if fetch_content and merged:
        fetched_contents = _fetch_content_for_results(merged)

    # 附加引擎状态说明（部分引擎失败 / 已剔除不相关结果 / 结果相关性弱）
    notes = list(dropped_notes)
    if news_fallback_note:
        notes.append(news_fallback_note)
    if weak_used:
        notes.append("强相关结果不足，已用相关性较弱的结果补足（标注 ⚠️）")
    engine_note = ""
    if engine_errors:
        notes.append("部分引擎状态: " + "; ".join(engine_errors))
    if notes:
        engine_note = "\n\n⚠️ " + "；".join(notes)

    # 格式化输出
    engine_names = [ENGINE_LABELS.get(e, e) for e in engine_map.keys()]
    markdown = format_merged_results(keyword, merged, category, engine_names) + engine_note

    # fetch_content：把正文拼到 markdown 里，供 AI 直接引用（SearXNG 式 search+scrape 一体化）
    if fetched_contents:
        markdown += "\n\n### 结果正文（fetch_content）\n"
        for item in merged:
            content = fetched_contents.get(item["url"])
            if content:
                markdown += (
                    f"\n**[{item.get('index', '?')}] {item['title']}** — {item['url']}\n"
                    f"{content}\n"
                )

    # 结构化结果
    structured_results = [
        {
            "index": item.get("index"),
            "title": item["title"],
            "url": item["url"],
            "snippet": item["snippet"],
            "sources": item["sources"],
            "source_count": item.get("source_count"),
            "best_rank": item.get("best_rank"),
            "rrf_score": item.get("score"),
            "strength": item.get("strength"),
            "relevance": item.get("relevance_tier"),
        }
        for item in merged
    ]

    meta = {
        "keyword": keyword,
        "category": category,
        "engines": engine_names,
        "total_results": len(merged),
        "weak_fill": weak_used,
    }
    if dropped_notes:
        meta["filtered"] = dropped_notes
    if time_range:
        meta["time_range"] = time_range
    if language:
        meta["language"] = language
    if fetched_contents:
        meta["fetched_content"] = len(fetched_contents)

    return make_tool_result(
        markdown,
        results=structured_results,
        meta=meta,
    )
