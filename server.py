# -*- coding: utf-8 -*-
"""MCP Web Search Server"""
import sys, os
import logging
import threading
import concurrent.futures
from typing import Optional
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import ToolAnnotations
sys.path.insert(0, os.path.dirname(__file__))

# 日志配置
logging.basicConfig(level=logging.WARNING, format="%(name)s [%(levelname)s] %(message)s")

from src.searchengines.duckduckgo import search_duckduckgo_items
from src.searchengines.bing import search_bing_items
from src.searchengines.google import search_google_items
from src.tools.advanced_search import web_search_advanced
from src.tools.batch_search import batch_web_search
from src.tools.site_specific import github_repo_search, npm_package_search, pypi_package_search
from src.tools.site_specific import gitlab_repo_search, crates_package_search, maven_package_search
from src.tools.site_specific import nuget_package_search, docker_image_search, huggingface_model_search
from src.tools.twitter import twitter_search
from src.tools.web_fetch import web_fetch, download_file
from src.config import (
    SEARCH_TIMEOUT,
    RELEVANCE_MIN_STRONG,
    RELEVANCE_REPORT_DROPPED,
)

mcp = FastMCP("Web Search", port=8011)

# 模块级常驻线程池：避免每次调用都新建/销毁线程池导致线程堆积。
# 按用途分池：长任务（抓正文、批量聚合）与短任务（单次搜索）分开，
# 否则一个 75s 的 web_search_with_content 就能把搜索池占满，
# 让后续任意搜索调用在队列里排队直到超时。
_SEARCH_EXECUTORS = {}
_SEARCH_EXECUTOR_LOCK = threading.Lock()
_POOL_SIZES = {"search": 6, "content": 3, "batch": 2}
_MAX_RESULTS_LIMIT = 20  # max_results 硬上限


def _get_search_executor(kind: str = "search") -> concurrent.futures.ThreadPoolExecutor:
    """懒初始化并返回指定用途的常驻线程池。"""
    pool = _SEARCH_EXECUTORS.get(kind)
    if pool is None:
        with _SEARCH_EXECUTOR_LOCK:
            pool = _SEARCH_EXECUTORS.get(kind)
            if pool is None:
                pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=_POOL_SIZES.get(kind, 4),
                    thread_name_prefix=f"web-{kind}",
                )
                _SEARCH_EXECUTORS[kind] = pool
    return pool


def _discard_future(future: concurrent.futures.Future) -> None:
    """丢弃超时 future：取消未启动的任务，并消费已完成任务的异常避免警告。"""
    future.cancel()

    def _consume(f):
        try:
            f.exception()
        except Exception:
            pass

    future.add_done_callback(_consume)


def _with_timeout(func, *args, timeout=15, kind="search", **kwargs):
    """给搜索调用加超时保护，防止卡死。

    使用模块级常驻线程池（而非每次新建），超时后直接丢弃 future，
    不再因 shutdown(wait=True) 阻塞等待超时任务完成。
    """
    future = _get_search_executor(kind).submit(func, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        _discard_future(future)
        return {"content": f"⏱️ 搜索超时（>{timeout}s）", "results": [], "meta": {"error": True, "message": "timeout"}}


def _format_item_list(items) -> str:
    """把结果条目格式化为可读的编号列表。"""
    import json as _json

    from src.utils import sanitize_title

    lines = []
    for i, item in enumerate(items, 1):
        if isinstance(item, dict) and ("title" in item or "url" in item):
            title = sanitize_title(item.get("title") or item.get("url") or "")
            url = item.get("url", "")
            snippet = item.get("snippet", "")
            weak = " ⚠️相关性弱" if item.get("relevance_tier") == "B" else ""
            line = "%d. **%s**%s\n   %s" % (i, title, weak, url)
            if snippet:
                line += "\n   " + snippet
            lines.append(line)
        else:
            lines.append("%d. %s" % (i, _json.dumps(item, ensure_ascii=False)))
    return "\n\n".join(lines)


def _single_engine_search_text(label: str, keyword: str, items, extra_note: str = "") -> str:
    """单引擎搜索工具的统一输出：清洗 + 相关性标注 + 剔除说明。

    引擎偶发返回与查询词完全无关的结果集（实测 cn.bing.com 对
    「储能 电池 报价」返回 10 条 QQ 邮箱登录页），这类结果必须剔除并说明，
    而不是当成结果交给上层。
    """
    from src.utils import filter_junk_results, split_by_relevance

    if isinstance(items, dict):
        # _with_timeout 超时/失败时返回的是错误结果字典，直接透出原因
        return str(items.get("content") or f"❌ {label} 搜索失败。")

    if not items:
        text = f"❌ {label} 对「{keyword}」未返回结果。"
        return text + (f"\n  {extra_note}" if extra_note else "")

    strong, weak, dropped = split_by_relevance(keyword, filter_junk_results(items))
    weak_used = len(strong) < RELEVANCE_MIN_STRONG and bool(weak)
    picked = strong + weak if weak_used else strong

    if not picked:
        return (
            f"❌ {label} 对「{keyword}」返回的 {len(items)} 条结果与查询词均不匹配，已全部剔除。\n"
            "  可更换关键词（去掉年份、地名等泛化词，改用更具体的主题词）后重试。"
        )

    header = f"🔎 **{label} 搜索结果**「{keyword}」（{len(picked)} 条）"
    notes = []
    if dropped >= RELEVANCE_REPORT_DROPPED:
        notes.append(f"已剔除 {dropped} 条与查询词不匹配的结果")
    if weak_used:
        notes.append("强相关结果不足，已补入相关性较弱的结果（标注 ⚠️）")
    if extra_note:
        notes.append(extra_note)
    text = header + "\n\n" + _format_item_list(picked)
    if notes:
        text += "\n\n⚠️ " + "；".join(notes)
    return text


def _tool_result_text(result) -> str:
    """把工具返回结果转成干净的 markdown 文本。

    只输出 content 正文与一行元数据：fastmcp 已经把返回值整体序列化为
    structured content，这里再把 results 逐条 dump 成 JSON 会让同一条结果
    在上下文里出现两遍（token 翻倍、噪声增加）。
    """
    import json as _json

    if isinstance(result, list):
        return _format_item_list(result) if result else "未找到结果。"

    parts = [str(result.get("content", "")).strip()]
    meta = result.get("meta") or {}
    if meta:
        parts.append(f"> 元数据: {_json.dumps(meta, ensure_ascii=False)}")
    return "\n\n".join(p for p in parts if p)


# 工具显示名：MCP 客户端在工具面板里展示 title，比裸函数名可读
_TOOL_TITLES = {
    "web_search": "DuckDuckGo 网页搜索",
    "web_search_bing": "Bing 网页搜索",
    "web_search_google": "Google 网页搜索（需代理）",
    "web_search_advanced": "多引擎聚合搜索",
    "web_search_with_content": "聚合搜索（附正文）",
    "web_fetch": "抓取网页正文",
    "batch_web_search": "批量聚合搜索",
    "github_repo_search": "GitHub 仓库搜索",
    "twitter_search": "Twitter(X) 搜索",
    "npm_package_search": "npm 包搜索",
    "pypi_package_search": "PyPI 包查询",
    "gitlab_repo_search": "GitLab 仓库搜索",
    "crates_package_search": "crates.io 包搜索",
    "maven_package_search": "Maven Central 搜索",
    "nuget_package_search": "NuGet 包搜索",
    "docker_image_search": "Docker Hub 镜像搜索",
    "huggingface_model_search": "HuggingFace 模型搜索",
    "download_file": "下载文件",
}


def _readonly_tool(name: str):
    """注册只读搜索/抓取工具。

    关闭结构化输出：工具返回值本身已是可读 markdown，fastmcp 默认还会把返回值
    序列化成一份 structured content 一起发给模型，同一条结果因此在上下文里出现两遍。
    annotations 四项给全（Exa / mcp-omnisearch 的写法）：工具只读、访问开放网络、
    可重复调用且结果幂等 —— 客户端据此决定是否自动放行工具调用。
    """
    return mcp.tool(
        annotations=ToolAnnotations(
            title=_TOOL_TITLES.get(name, name),
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        name=name,
        structured_output=False,
    )


def _write_tool(name: str):
    """注册会写本地文件的工具（download_file）：非只读，其余 annotation 同上。"""
    return mcp.tool(
        annotations=ToolAnnotations(
            title=_TOOL_TITLES.get(name, name),
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        name=name,
        structured_output=False,
    )


def _single_engine_call(engine_key: str, label: str, func, keyword: str, max_results: int) -> str:
    """带熔断保护的单引擎搜索调用。

    引擎连续超时会触发熔断（见 src/utils.py 的引擎熔断说明），
    熔断期内直接返回说明，不再让每次调用都白等一个超时周期。
    """
    from src.utils import engine_available, engine_cooldown_remaining, note_engine_result

    if not engine_available(engine_key):
        return (
            f"⏸️ {label} 临时停用中（此前连续超时），约 "
            f"{engine_cooldown_remaining(engine_key)}s 后恢复。\n"
            "  可改用 web_search_advanced（多引擎聚合）继续检索。"
        )

    items = _with_timeout(func, keyword, max_results, timeout=SEARCH_TIMEOUT)
    if isinstance(items, dict):  # 超时：返回的错误字典
        note_engine_result(engine_key, False)
        text = _single_engine_search_text(label, keyword, items)
        left = engine_cooldown_remaining(engine_key)
        if left:
            text += f"\n  {label} 已连续超时，暂时停用 {left}s。"
        return text
    note_engine_result(engine_key, True)
    return _single_engine_search_text(label, keyword, items)


@_readonly_tool("web_search")
def handle_web_search(keyword: str, max_results: Optional[int] = 10) -> str:
    """DuckDuckGo 网页搜索，返回标题、摘要和链接的 markdown 文本。

    Args:
        keyword: 搜索关键词。
        max_results: 最大返回结果数（1-20，默认 10）。

    Returns:
        搜索结果的 markdown 文本（含标题、摘要与链接）。
    """
    max_results = min(max_results or 10, _MAX_RESULTS_LIMIT)
    return _single_engine_call("duckduckgo", "DuckDuckGo", search_duckduckgo_items,
                               keyword, max_results)


@_readonly_tool("web_search_bing")
def handle_web_search_bing(keyword: str, max_results: Optional[int] = 10) -> str:
    """Bing 网页搜索，返回标题、摘要和链接的 markdown 文本。

    Args:
        keyword: 搜索关键词。
        max_results: 最大返回结果数（1-20，默认 10）。

    Returns:
        搜索结果的 markdown 文本（含标题、摘要与链接）。
    """
    max_results = min(max_results or 10, _MAX_RESULTS_LIMIT)
    return _single_engine_call("bing", "Bing", search_bing_items, keyword, max_results)


@_readonly_tool("web_search_google")
def handle_web_search_google(keyword: str, max_results: Optional[int] = 10) -> str:
    """Google 网页搜索，返回标题、摘要和链接的 markdown 文本（需要代理）。

    Args:
        keyword: 搜索关键词。
        max_results: 最大返回结果数（1-20，默认 10）。

    Returns:
        搜索结果的 markdown 文本（含标题、摘要与链接）。
    """
    from src.utils import has_proxy

    max_results = min(max_results or 10, _MAX_RESULTS_LIMIT)
    if not has_proxy():
        return (
            "❌ Google 未启用：当前环境未配置代理，Google 无法访问。\n"
            "  请改用 web_search（DuckDuckGo）或 web_search_advanced（多引擎聚合）。"
        )
    return _single_engine_call("google", "Google", search_google_items, keyword, max_results)


@_readonly_tool("web_search_advanced")
def handle_web_search_advanced(keyword: str, max_results: Optional[int] = 10, category: Optional[str] = "general", engines: Optional[str] = None, time_range: Optional[str] = None, language: Optional[str] = None) -> str:
    """多引擎聚合搜索：并行查询多个引擎，合并去重后按评分排序返回。

    结果已按查询词相关性筛选：与查询词不匹配的引擎结果会被剔除并在末尾说明。

    Args:
        keyword: 搜索关键词。
        max_results: 最大返回结果数（1-20，默认 10）。
        category: 搜索类别，general（网页）或 news（新闻），默认 general。
        engines: 指定引擎，逗号分隔字符串，如 "duckduckgo,bing"；默认全部可用引擎。
        time_range: 时间范围过滤，day|week|month|year，仅返回该时间内的结果。
        language: 语言偏好，如 zh-CN / en（默认 zh-CN）。

    Returns:
        聚合结果的 markdown 文本（含来源标记、评分与链接）。
    """
    return _tool_result_text(_with_timeout(web_search_advanced, keyword, max_results or 10, category or "general", engines,
                         time_range or None, language or None,
                         timeout=SEARCH_TIMEOUT * 2))  # 聚合搜索给双倍时间


@_readonly_tool("web_search_with_content")
def handle_web_search_with_content(keyword: str, max_results: Optional[int] = 10, category: Optional[str] = "general", engines: Optional[str] = None, time_range: Optional[str] = None, language: Optional[str] = None) -> str:
    """聚合搜索并附带正文：搜索后自动抓取前 5 条结果的网页正文，供直接引用。

    与 web_search_advanced 相同，但额外抓取结果正文（每个最多 1000 字符）。
    适合需要直接引用网页内容的场景（AI 引用、RAG、深度研究）。

    Args:
        keyword: 搜索关键词。
        max_results: 最大返回结果数（1-20，默认 10）。
        category: 搜索类别，general（网页）或 news（新闻），默认 general。
        engines: 指定引擎，逗号分隔字符串，如 "duckduckgo,bing"；默认全部可用引擎。
        time_range: 时间范围过滤，day|week|month|year。
        language: 语言偏好，如 zh-CN / en。

    Returns:
        聚合结果 + 结果正文（fetch_content）的 markdown 文本。
    """
    return _tool_result_text(_with_timeout(web_search_advanced, keyword, max_results or 10, category or "general", engines,
                         time_range or None, language or None, True, kind="content",
                         timeout=SEARCH_TIMEOUT * 2 + 45))  # 含正文抓取，时间更宽裕


@_readonly_tool("web_fetch")
def handle_web_fetch(url: str, max_length: Optional[int] = 8000, output_format: Optional[str] = "text",
                     start_index: Optional[int] = 0) -> str:
    """抓取网页正文内容并转为纯文本或 markdown。

    Args:
        url: 目标网页地址。
        max_length: 返回内容的最大字符数（默认 8000）。
        output_format: 输出格式，text 或 markdown，默认 text。
        start_index: 从正文第几个字符开始返回（默认 0）。内容被截断时结果尾部会
            给出续读坐标，把它传回 start_index 即可接着读同一页。

    Returns:
        网页标题、来源与正文的文本；被反爬拦截时自动回退浏览器渲染。
        正文过长时按段落/句子边界截断，并附续读指引。
    """
    return _tool_result_text(_with_timeout(web_fetch, url, max_length or 8000,
                                           output_format or "text", start_index or 0,
                                           kind="content",
                                           timeout=SEARCH_TIMEOUT * 3))

@_readonly_tool("github_repo_search")
def handle_github_repo_search(keyword: str, max_results: Optional[int] = 10) -> str:
    """按关键词搜索 GitHub 仓库，返回名称、描述、stars、语言、fork 数。

    直接调 GitHub 公开 API（无需 key），比网页搜索更准确高效。
    适合找开源项目、评估项目热度。

    Args:
        keyword: 搜索关键词（如 "mcp server python"）。
        max_results: 最大返回结果数（1-20，默认 10）。

    Returns:
        GitHub 仓库的结构化列表（名称、stars、语言、描述）。
    """
    return _tool_result_text(github_repo_search(keyword, max_results or 10))

@_readonly_tool("twitter_search")
def handle_twitter_search(keyword: str, max_results: Optional[int] = 10, mode: Optional[str] = "latest") -> str:
    """按关键词搜索 X(Twitter) 推文，返回作者、时间、正文、互动数与链接。

    读取的是使用者本人的 X 登录态；按本项目回退原则，遇到反爬/登录墙应交给
    Playwright MCP（browser_* 工具）驱动用户已登录的 Edge 完成任务（见 README）；
    本工具是「静默、不弹浏览器」的备选：优先 env 里的
    cookie 会话（X_AUTH_TOKEN / X_CT0），未配置时回退到已开启远程调试（9222）
    且已登录 X 的浏览器。未开通时返回配置指引。
    读取某个账号的推文用 X 搜索语法，如 "from:elonmusk audio"、"@elonmusk audio"。

    Args:
        keyword: 搜索关键词，支持 X 搜索语法（from:user、@user、#tag、"精确短语"）。
        max_results: 最大返回结果数（1-20，默认 10）。
        mode: latest（最新，默认）或 top（热门）。

    Returns:
        推文列表（@handle、时间、正文、互动数、链接）的 markdown 文本。
    """
    # 走线程池：CDP 后端内部自建 asyncio 事件循环，不能在 FastMCP 的主循环线程里跑；
    # 同时给「首次发现 queryId + 浏览器渲染」留出宽裕超时。
    return _tool_result_text(_with_timeout(twitter_search, keyword, max_results or 10, mode or "latest",
                                           kind="content", timeout=90))

@_readonly_tool("npm_package_search")
def handle_npm_package_search(keyword: str, max_results: Optional[int] = 10) -> str:
    """按关键词搜索 npm 包，返回包名、版本、描述、作者。

    直接调 npm registry 公开 API（无需 key）。

    Args:
        keyword: 搜索关键词（如 "llm mcp server"）。
        max_results: 最大返回结果数（1-20，默认 10）。

    Returns:
        npm 包的结构化列表（包名、版本、描述）。
    """
    return _tool_result_text(npm_package_search(keyword, max_results or 10))

@_readonly_tool("pypi_package_search")
def handle_pypi_package_search(package_name: str) -> str:
    """查询 PyPI 包的完整信息（需精确包名）。

    返回版本、作者、Python 版本要求、简介等。PyPI 已禁用模糊搜索 API，
    所以不确定包名时先查 GitHub 找到准确包名。

    Args:
        package_name: PyPI 包名（精确，如 "crawl4ai"）。

    Returns:
        该包的版本、作者、简介、Python 版本要求、项目主页。
    """
    return _tool_result_text(pypi_package_search(package_name))

@_readonly_tool("gitlab_repo_search")
def handle_gitlab_repo_search(keyword: str, max_results: Optional[int] = 10) -> str:
    """按关键词搜索 GitLab 仓库，返回名称、描述、stars、fork 数。

    直接调 gitlab.com 公开 API（无需 key）。

    Args:
        keyword: 搜索关键词。
        max_results: 最大返回结果数（1-20，默认 10）。

    Returns:
        GitLab 仓库的结构化列表（名称、stars、fork、描述）。
    """
    return _tool_result_text(gitlab_repo_search(keyword, max_results or 10))

@_readonly_tool("crates_package_search")
def handle_crates_package_search(keyword: str, max_results: Optional[int] = 10) -> str:
    """按关键词搜索 crates.io 的 Rust 包，返回名称、版本、描述、下载量。

    crates.io 要求请求带含邮箱的 User-Agent，否则返回 403；已内置处理。

    Args:
        keyword: 搜索关键词。
        max_results: 最大返回结果数（1-20，默认 10）。

    Returns:
        crates 包的结构化列表（名称、版本、下载量、描述）。
    """
    return _tool_result_text(crates_package_search(keyword, max_results or 10))

@_readonly_tool("maven_package_search")
def handle_maven_package_search(keyword: str, max_results: Optional[int] = 10) -> str:
    """按关键词搜索 Maven Central 的 Java 包，返回 groupId:artifactId、最新版本、更新时间。"""
    return _tool_result_text(maven_package_search(keyword, max_results or 10))

@_readonly_tool("nuget_package_search")
def handle_nuget_package_search(keyword: str, max_results: Optional[int] = 10) -> str:
    """按关键词搜索 NuGet 的 .NET 包，返回名称、版本、描述、作者、下载量。"""
    return _tool_result_text(nuget_package_search(keyword, max_results or 10))

@_readonly_tool("docker_image_search")
def handle_docker_image_search(keyword: str, max_results: Optional[int] = 10) -> str:
    """按关键词搜索 Docker Hub 镜像，返回名称、描述、star、pull 数。"""
    return _tool_result_text(docker_image_search(keyword, max_results or 10))

@_readonly_tool("huggingface_model_search")
def handle_huggingface_model_search(keyword: str, max_results: Optional[int] = 10) -> str:
    """按关键词搜索 HuggingFace 模型，返回名称、下载量、likes、任务类型。"""
    return _tool_result_text(huggingface_model_search(keyword, max_results or 10))

@_readonly_tool("batch_web_search")
def handle_batch_web_search(queries: list[dict]) -> str:
    """批量聚合搜索：一次执行 1-5 条独立搜索，按输入顺序返回结果。

    Args:
        queries: 搜索请求列表，每项为
            {"keyword": str, "max_results": int(1-20), "category": "general"|"news", "engines": str}。

    Returns:
        按输入顺序排列的各条搜索结果 markdown 文本。
    """
    return _tool_result_text(_with_timeout(batch_web_search, queries, kind="batch",
                                           timeout=SEARCH_TIMEOUT * 3))

@_write_tool("download_file")
def handle_download_file(url: str, output_path: str) -> str:
    """下载文件到本地指定路径，支持大文件流式下载。

    Args:
        url: 文件下载地址。
        output_path: 本地保存路径。

    Returns:
        下载结果文本（来源、保存路径、文件大小）。
    """
    return _tool_result_text(download_file(url, output_path))

if __name__ == "__main__":
    mcp.run(transport="stdio")
