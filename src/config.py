# -*- coding: utf-8 -*-
"""
配置管理：从环境变量读取配置，提供默认值。
"""

import os
import sys
from typing import Dict, Optional


def _env_int(name: str, default: int) -> int:
    """读取环境变量并转换为 int，带错误处理。"""
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        print(f"⚠️  环境变量 {name} 的值无效: {val!r}，使用默认值 {default}", file=sys.stderr)
        return default


# ── 服务器配置 ──────────────────────────────────────────────

SERVER_HOST = os.environ.get("WS_HOST", "0.0.0.0")
SERVER_PORT = _env_int("WS_PORT", 8012)
SERVER_TRANSPORT = os.environ.get("WS_TRANSPORT", "stdio")  # streamable-http | stdio | sse

# ── 搜索配置 ────────────────────────────────────────────────

MAX_RESULTS_DEFAULT = 10
MAX_RESULTS_LIMIT = 20  # 硬上限

# 默认搜索语言（SearXNG :lang 语法对应），可被 web_search_advanced 的 language 参数覆盖
DEFAULT_LANGUAGE = os.environ.get("WS_LANGUAGE", "zh-CN")

# 时间范围过滤允许值（SearXNG time_range 枚举）
TIME_RANGE_VALUES = ("day", "week", "month", "year")

# ── 缓存配置 ────────────────────────────────────────────────

CACHE_TTL = _env_int("WS_CACHE_TTL", 300)  # 秒，默认 5 分钟
CACHE_ENABLED = os.environ.get("WS_CACHE_ENABLED", "true").lower() in ("true", "1", "yes")

# ── 超时配置 ────────────────────────────────────────────────

SEARCH_TIMEOUT = _env_int("WS_SEARCH_TIMEOUT", 15)  # 秒
FETCH_TIMEOUT = _env_int("WS_FETCH_TIMEOUT", 20)  # 秒
DOWNLOAD_TIMEOUT = _env_int("WS_DOWNLOAD_TIMEOUT", 60)  # 秒
BROWSER_TIMEOUT = _env_int("WS_BROWSER_TIMEOUT", 30)  # 秒

# ── Firecrawl 配置 ──────────────────────────────────────────

# 云 API Key；未配置时 web_fetch 的回退顺序跳过 Firecrawl，退回本机浏览器渲染
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")
FIRECRAWL_TIMEOUT = _env_int("WS_FIRECRAWL_TIMEOUT", 30)  # 秒
# v2 为当前版本，404 时自动回退 v1（兼容旧实例/自托管）
FIRECRAWL_ENDPOINTS = [
    os.environ.get("WS_FIRECRAWL_ENDPOINT", "https://api.firecrawl.dev/v2/scrape"),
    "https://api.firecrawl.dev/v1/scrape",
]

# ── Crawl4AI 配置 ────────────────────────────────────────────

# 本地渲染后端（首选回退）；未安装 crawl4ai 时自动禁用
CRAWL4AI_TIMEOUT = _env_int("WS_CRAWL4AI_TIMEOUT", 30)  # 秒

# ── 浏览器渲染配置 ──────────────────────────────────────────

# CDP (Chrome DevTools Protocol) — 优先使用，连接已有 Chrome/Edge
CDP_PORT = _env_int("WS_CDP_PORT", 9222)
CDP_HOST = os.environ.get("WS_CDP_HOST", "127.0.0.1")

# Playwright 回退配置（当 CDP 不可用时自动启用）
PLAYWRIGHT_HEADLESS = os.environ.get("WS_PLAYWRIGHT_HEADLESS", "true").lower() in ("true", "1", "yes")

# ── ddgs 后端配置 ────────────────────────────────────────────

# ddgs 固定后端列表。不要用 "auto"：ddgs v9 的 auto 会把 wikipedia/grokipedia
# 排在最前，而并发 worker 数由 max_results 决定（此时通常只有 2 个），
# 结果就是裸词查询只命中百科词条（词典噪声）；且 9 个后端轮转还会拖到超时。
#
# 2026-09 实测（本机网络，超时 6s，逐后端单测）：
#   yandex      1.1-1.5s  可用，质量可用   ← 唯一可用的后端
#   yahoo       8.0-8.3s  超时无结果（曾可用，现已失效）
#   bing        7-24s     可用但极慢，且与 bing.py 直连抓取重复
#   brave/duckduckgo/google/startpage  6s 超时无结果
#   mojeek      0.9s      无结果
# 失效后端排在前面会让每次搜索白等到超时（实测 12/12 次都耗满 8s），
# 因此默认只保留实测可用的 yandex。
DDGS_BACKENDS = os.environ.get("WS_DDGS_BACKENDS", "yandex")

# ddgs 搜索区域。实测 us-en 对中英文混合技术查询的相关性优于 cn-zh。
DDGS_REGION = os.environ.get("WS_DDGS_REGION", "us-en")

# ddgs 单次引擎请求超时（秒），需小于上层 SEARCH_TIMEOUT 以便返回部分结果
DDGS_TIMEOUT = _env_int("WS_DDGS_TIMEOUT", 6)

# ── 结果质量过滤 ─────────────────────────────────────────────

# 词典/百科类噪声域名：裸词查询容易只返回词条释义而非实质结果
JUNK_RESULT_DOMAINS = tuple(
    d.strip().lower()
    for d in os.environ.get(
        "WS_JUNK_RESULT_DOMAINS",
        "wikipedia.org,grokipedia.com,wiktionary.org,wikidata.org,"
        "thefreedictionary.com,dictionary.com,merriam-webster.com,"
        "collinsdictionary.com,dict.cn,zdic.net,iciba.com",
    ).split(",")
    if d.strip()
)

# 查询相关性过滤：搜索引擎偶发返回与查询词无关的结果集（实测 cn.bing.com 对
# 「储能 电池 报价」返回 10 条 QQ 邮箱登录页、对「2026 中国 储能 政策 最新」
# 返回 2026 年节假日），这类结果必须按查询词命中情况剔除，否则会挤占靠前排名。
RELEVANCE_FILTER_ENABLED = os.environ.get(
    "WS_RELEVANCE_FILTER", "true"
).lower() in ("true", "1", "yes")

# 相关性过滤后至少保留多少条“强相关”结果，低于该数则用弱相关结果补足
RELEVANCE_MIN_STRONG = _env_int("WS_RELEVANCE_MIN_STRONG", 3)

# 被判定为不相关的条数达到该值时，在结果里附上剔除说明
RELEVANCE_REPORT_DROPPED = _env_int("WS_RELEVANCE_REPORT_DROPPED", 3)

# ── SearXNG 配置 (Phase 2) ──────────────────────────────────

SEARXNG_URL = os.environ.get("WS_SEARXNG_URL", "")
SEARXNG_USERNAME = os.environ.get("WS_SEARXNG_USERNAME", "")
SEARXNG_PASSWORD = os.environ.get("WS_SEARXNG_PASSWORD", "")

# ── Twitter / X 配置（站点搜索 twitter_search）──────────────
#
# X 无免费公开搜索 API，未登录访问 x.com 是空白页（2026-10-04 实测），
# 因此只能复用「使用者自己的登录态」，两种后端按可用性回退：
#
#   方式一（静默，推荐）：浏览器 cookie 会话。
#     在已登录 X 的浏览器里 F12 → Application → Cookies → https://x.com，
#     复制 auth_token 与 ct0 两个值，配到本 MCP 的 env：
#         X_AUTH_TOKEN=<auth_token>
#         X_CT0=<ct0>
#     ⚠️ 这两个值等同于账号凭据：只留在本机 env，不要写进仓库/笔记/记忆。
#
#   方式二（零密钥）：复用已开启远程调试的 Chrome/Edge（复用下方 CDP_HOST/CDP_PORT）。
#     用 `chrome.exe --remote-debugging-port=9222` 启动并保持 X 登录即可，
#     工具会新开标签页搜索、抽取、再关闭，全程用户可见。
X_AUTH_TOKEN = os.environ.get("X_AUTH_TOKEN", "")
X_CT0 = os.environ.get("X_CT0", "")

# 可选：SearchTimeline 的 queryId 覆盖。默认自动从 x.com 前端 bundle 发现，
# 仅在自动发现失败（X 改版）时手工指定。
X_SEARCH_QUERY_ID = os.environ.get("X_SEARCH_QUERY_ID", "")

# X 接口发现结果的磁盘缓存（queryId / features 自愈结果），24 小时内复用
X_CACHE_FILE = os.environ.get("WS_X_CACHE_FILE", "")

# ── 代理配置 ────────────────────────────────────────────────

USE_PROXY = os.environ.get("WS_USE_PROXY", "").lower() in ("true", "1", "yes")
PROXY_URL = os.environ.get("WS_PROXY_URL", "")  # 显式代理 URL，优先级高于环境变量


# ── HTTP 请求头 ──────────────────────────────────────────────

HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


def get_proxies() -> Optional[Dict[str, str]]:
    """从环境变量或显式配置读取代理设置。"""
    # 优先使用显式配置的 WS_PROXY_URL
    if USE_PROXY and PROXY_URL:
        return {"http": PROXY_URL, "https": PROXY_URL}

    # 回退到环境变量
    proxies: Dict[str, str] = {}
    for var in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
        val = os.environ.get(var)
        if val:
            var_lower = var.lower().replace("_proxy", "")
            proxies[var_lower] = val
    return proxies or None


def has_proxy() -> bool:
    """检查是否配置了代理"""
    if USE_PROXY and PROXY_URL:
        return True
    return any(
        os.environ.get(v)
        for v in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]
    )
