# -*- coding: utf-8 -*-
"""
Crawl4AI 本地渲染后端：把 URL 交给本地 Crawl4AI（Playwright 渲染）转为干净 Markdown。

定位：web_fetch 被反爬拦截时的首选本地回退（无 key、无额度、无 Docker）。

抓取质量优化（0.9.2）：
- PruningContentFilter 生成 fit_markdown（智能提取正文，去掉导航/页脚噪音）
- 真实 Chrome UA + 1920 视口 + 中文语言偏好，降低被反爬误伤概率
- 返回 title/description/links 等 metadata

未安装 crawl4ai 时本模块自动降级为空操作（返回 None），不影响原有链路。
"""

import concurrent.futures
import threading
from typing import Dict, List, Optional

from src.config import CRAWL4AI_TIMEOUT, HEADERS

# 惰性导入：server 启动时不强制依赖 crawl4ai（未装也能正常跑）
_crawl4ai = None
_crawl4ai_lock = threading.Lock()


def _get_crawl4ai():
    """惰性导入 crawl4ai 包，未安装返回 None。"""
    global _crawl4ai
    if _crawl4ai is None:
        with _crawl4ai_lock:
            if _crawl4ai is None:
                try:
                    import crawl4ai as c
                    _crawl4ai = c
                except ImportError:
                    _crawl4ai = False
    return _crawl4ai or None


def is_available() -> bool:
    """crawl4ai 是否已安装可用。"""
    return _get_crawl4ai() is not None


def _build_browser_config():
    """构建浏览器配置：真实 Chrome UA + 大视口 + 中文语言偏好 + 防检测，降低反爬误伤。"""
    from crawl4ai import BrowserConfig

    return BrowserConfig(
        headless=True,
        user_agent=HEADERS.get("User-Agent"),
        viewport_width=1920,
        viewport_height=1080,
        headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        enable_stealth=True,  # 反自动化检测（Navigator.webdriver 等）
        ignore_https_errors=True,
    )


def _build_run_config(timeout: int):
    """构建抓取配置：PruningContentFilter 生成 fit_markdown（智能正文提取）。"""
    from crawl4ai import CrawlerRunConfig
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
    from crawl4ai.content_filter_strategy import PruningContentFilter

    return CrawlerRunConfig(
        verbose=False,
        page_timeout=timeout * 1000,
        markdown_generator=DefaultMarkdownGenerator(
            content_filter=PruningContentFilter(),
            options={"ignore_links": False, "escape_html": False},
        ),
    )


def _impl(url: str, timeout: int) -> Optional[Dict]:
    """实际抓取，在独立线程中运行（crawl4ai 为 async，避免与 FastMCP 事件循环冲突）。"""
    import asyncio

    from crawl4ai import AsyncWebCrawler

    async def _run():
        async with AsyncWebCrawler(config=_build_browser_config()) as crawler:
            result = await crawler.arun(url=url, config=_build_run_config(timeout))
            if not result or not getattr(result, "success", False):
                return None

            md = getattr(result, "markdown", None) or None
            if md is None:
                return None

            # 优先 fit_markdown（智能正文）；为空时降级 raw_markdown
            fit_markdown = (md.fit_markdown or "").strip()
            raw_markdown = (md.raw_markdown or "").strip()
            markdown = fit_markdown or raw_markdown
            if len(markdown) < 50:
                return None

            metadata = result.metadata or {}
            links = []
            try:
                for link in (result.links or {}).get("internal", [])[:20]:
                    url_href = link.get("href", "")
                    if url_href:
                        links.append(url_href)
            except Exception:
                links = []

            return {
                "title": metadata.get("title") or "",
                "description": metadata.get("description") or "",
                "markdown": markdown,
                "fit_markdown": fit_markdown,
                "links": links,
            }

    try:
        return asyncio.run(_run())
    except Exception:
        return None


def scrape_with_crawl4ai(url: str, timeout: Optional[int] = None) -> Optional[Dict]:
    """本地渲染抓取 URL，返回 {title, description, markdown, fit_markdown, links}。

    不可用/失败/超时返回 None。
    """
    if not is_available():
        return None
    timeout = timeout or CRAWL4AI_TIMEOUT

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_impl, url, timeout)
            return future.result(timeout=timeout + 10)
    except concurrent.futures.TimeoutError:
        return None
    except Exception:
        return None
