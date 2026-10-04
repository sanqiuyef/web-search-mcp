# -*- coding: utf-8 -*-
"""批量聚合搜索工具。"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List

from src.tools.advanced_search import web_search_advanced
from src.utils import make_error_result, make_tool_result


def batch_web_search(queries: List[Dict[str, Any]]) -> dict:
    """并行执行 1 到 5 条独立的聚合搜索，并保持输入顺序返回结果。"""
    if not isinstance(queries, list) or not 1 <= len(queries) <= 5:
        return make_error_result("queries 必须是包含 1 到 5 条搜索请求的列表。")

    normalized = []
    for index, query in enumerate(queries, 1):
        if not isinstance(query, dict) or not isinstance(query.get("keyword"), str) or not query["keyword"].strip():
            return make_error_result(f"第 {index} 条请求缺少非空 keyword。")

        max_results = query.get("max_results", 10)
        if not isinstance(max_results, int) or not 1 <= max_results <= 20:
            return make_error_result(f"第 {index} 条请求的 max_results 必须是 1 到 20 的整数。")

        category = query.get("category", "general")
        if category not in ("general", "news"):
            return make_error_result(f"第 {index} 条请求的 category 仅支持 general 或 news。")

        engines = query.get("engines")
        if engines is not None and not isinstance(engines, str):
            return make_error_result(f"第 {index} 条请求的 engines 必须是逗号分隔的字符串。")

        normalized.append({
            "keyword": query["keyword"].strip(),
            "max_results": max_results,
            "category": category,
            "engines": engines,
        })

    responses = [None] * len(normalized)
    with ThreadPoolExecutor(max_workers=len(normalized)) as executor:
        future_to_index = {
            executor.submit(web_search_advanced, **query): index
            for index, query in enumerate(normalized)
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            try:
                responses[index] = future.result()
            except Exception as error:
                responses[index] = make_error_result(f"搜索执行失败: {error}")

    sections = []
    for index, (query, response) in enumerate(zip(normalized, responses), 1):
        sections.append(f"# {index}. {query['keyword']}\n\n{response['content']}")

    return make_tool_result(
        "\n\n---\n\n".join(sections),
        results=[{"query": query, "response": response} for query, response in zip(normalized, responses)],
        meta={"query_count": len(normalized), "parallel": True},
    )
