# -*- coding: utf-8 -*-
"""
网页内容获取和文件下载工具。
"""

import os
import re
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify

from src.config import FETCH_TIMEOUT, DOWNLOAD_TIMEOUT
from src.ratelimit import fetch_limiter
from src.utils import (
    make_session,
    is_anti_scraping,
    fetch_with_playwright,
    make_tool_result,
    make_error_result,
)
from src.firecrawl import (
    is_configured as firecrawl_configured,
    scrape_with_firecrawl,
    markdown_to_text,
)
from src.crawl4ai import (
    is_available as crawl4ai_available,
    scrape_with_crawl4ai,
)


def _markdown_content(data: dict, output_format: str) -> dict:
    """把 {title, markdown} 后端结果统一为 {title, text, markdown}。"""
    title = data["title"]
    text = data["markdown"] if output_format == "markdown" else markdown_to_text(data["markdown"])
    return {"title": title, "text": text, "markdown": data["markdown"]}


def _fetch_via_crawl4ai(url: str, output_format: str) -> Optional[dict]:
    """用 Crawl4AI 本地渲染抓取，成功返回 content dict，失败/未安装返回 None。"""
    data = scrape_with_crawl4ai(url)
    if not data:
        return None
    return _markdown_content(data, output_format)


def _fetch_via_firecrawl(url: str, output_format: str) -> Optional[dict]:
    """用 Firecrawl 云渲染抓取，成功返回 content dict，失败/未配置返回 None。"""
    data = scrape_with_firecrawl(url)
    if not data:
        return None
    return _markdown_content(data, output_format)


def _extract_readable_content(html: str) -> dict:
    """从 HTML 中提取可读正文，比简单 get_text 效果好得多。"""
    soup = BeautifulSoup(html, "html.parser")

    # 清理无用标签
    for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                      "noscript", "form", "iframe", "svg", "button", "select",
                      "input", "textarea", "label"]):
        tag.decompose()

    # 移除隐藏元素
    for tag in soup.find_all(style=re.compile(r"display:\s*none|visibility:\s*hidden", re.I)):
        tag.decompose()

    title = soup.title.get_text(strip=True) if soup.title else "无标题"

    # 尝试识别正文容器
    main_content = None
    for selector in ["article", "main", "[role='main']", ".post-content",
                     ".article-content", ".entry-content", ".content",
                     "#content", "#main-content", ".markdown-body",
                     ".documentation", ".prose"]:
        main_content = soup.select_one(selector)
        if main_content:
            break

    if not main_content:
        main_content = soup.body or soup

    # 提取文本，保留段落结构
    paragraphs = []
    for p in main_content.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6",
                                     "li", "blockquote", "pre", "td", "th",
                                     "div.paragraph"]):
        text = p.get_text(strip=True)
        if text and len(text) > 10:  # 过滤太短的片段
            # 标题加粗标记
            if p.name and p.name.startswith("h"):
                text = f"## {text}"
            elif p.name == "li":
                text = f"  • {text}"
            elif p.name == "blockquote":
                text = f"> {text}"
            paragraphs.append(text)

    # 如果没有提取到段落，降级到整体文本
    if not paragraphs:
        body = main_content.get_text(separator="\n", strip=True)
        text = "\n".join(l.strip() for l in body.split("\n") if l.strip())
    else:
        text = "\n\n".join(paragraphs)

    markdown = markdownify(str(main_content), heading_style="ATX", strip=["img"])
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    return {"title": title, "text": text, "markdown": markdown}


def _format_content(title: str, url: str, text: str, output_format: str) -> str:
    if output_format == "markdown":
        return f"# {title}\n\n来源: <{url}>\n\n{text}"
    return f"标题: {title}\n来源: {url}\n\n{text}"


# 截断时优先在这些边界切开，避免把句子/段落切一半（中文正文尤其明显）
_CUT_SEPARATORS = ("\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ")


def _cut_at_boundary(chunk: str, max_length: int) -> str:
    """在窗口后 30% 范围内找最近的段落/句子边界，找不到就原样返回。"""
    floor = int(max_length * 0.7)
    best = -1
    for sep in _CUT_SEPARATORS:
        idx = chunk.rfind(sep)
        if idx >= floor and idx + len(sep) > best:
            best = idx + len(sep)
    if best <= 0:
        return chunk.rstrip()
    return chunk[:best].rstrip()


def _slice_content(text: str, start_index: int, max_length: int) -> tuple:
    """按字符窗口切片并生成续读指引，返回 (片段, 元信息)。

    长页面只能看到开头会让调研断在半路，所以尾部固定给出续读坐标
    （参考 modelcontextprotocol/servers 的 fetch server 与 duckduckgo-mcp-server）。
    """
    total = len(text)
    start = max(0, int(start_index or 0))
    if start >= total:
        return "", {"total_length": total, "start_index": start, "end_index": total,
                    "truncated": False, "out_of_range": True}
    end = min(start + max_length, total)
    chunk = text[start:end]
    truncated = end < total
    if truncated:
        chunk = _cut_at_boundary(chunk, max_length)
        end = start + len(chunk)
    meta = {"total_length": total, "start_index": start, "end_index": end,
            "truncated": truncated}
    return chunk, meta


def _continuation_note(meta: dict) -> str:
    """续读提示：告诉模型还有多少内容、下一次该传什么。"""
    if meta.get("out_of_range"):
        return (f"\n\n---\n[内容信息] start_index={meta['start_index']} 已超出正文长度"
                f"（共 {meta['total_length']} 字符），请从头读取。")
    if not meta.get("truncated"):
        return f"\n\n---\n[内容信息] 已显示全文（共 {meta['total_length']} 字符）。"
    return (f"\n\n---\n[内容信息] 已显示第 {meta['start_index']}-{meta['end_index']} 字符，"
            f"共 {meta['total_length']} 字符。"
            f"需要后续内容时再次调用本工具并传 start_index={meta['end_index']}。")


def _trim_content_result(
    url: str,
    content: dict,
    output_format: str,
    renderer: str,
    reason: Optional[str] = None,
    max_length: int = 5000,
    start_index: int = 0,
) -> dict:
    """把 {title, text, markdown} 结果按字符窗口截断并包装为 tool_result。

    所有渲染回退路径（firecrawl/browser）共用，保持输出格式一致。
    """
    text = content["markdown"] if output_format == "markdown" else content["text"]
    chunk, slice_meta = _slice_content(text, start_index, max_length)
    meta = {"source": url, "renderer": renderer, **slice_meta}
    if reason:
        meta["reason"] = reason
    return make_tool_result(
        _format_content(content["title"], url, chunk, output_format) + _continuation_note(slice_meta),
        meta=meta,
    )


def web_fetch(url: str, max_length: Optional[int] = 8000, output_format: str = "text",
              start_index: int = 0) -> dict:
    """获取网页文本内容，支持 text 和 markdown 两种输出格式。

    start_index: 从正文第几个字符开始返回（默认 0）。内容被截断时，
    结果尾部会给出续读坐标，把它原样传回来即可接着读，不必重新抓取整页。
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return make_error_result(f"无效的 URL: {url}")
    if output_format not in ("text", "markdown"):
        return make_error_result("output_format 仅支持 text 或 markdown。")
    if start_index and start_index < 0:
        return make_error_result("start_index 不能为负数。")
    max_length = max(200, int(max_length or 8000))

    # 检查抓取率限
    if not fetch_limiter.allow():
        return make_error_result("请求过于频繁，请稍后再试。")

    session = make_session()

    try:
        resp = session.get(url, timeout=FETCH_TIMEOUT)
        # 编码检测：优先 HTTP 响应头 charset，其次 UTF-8；
        # 仅当按该编码解码出现替换符（乱码）时才回退 apparent_encoding。
        # （apparent_encoding 对 GBK 等站点常误判为 ISO-8859-1 导致乱码）
        content_type = resp.headers.get("Content-Type", "")
        charset_match = re.search(r"charset\s*=\s*[\"']?([\w.-]+)", content_type, re.I)
        if charset_match:
            resp.encoding = charset_match.group(1)
        else:
            resp.encoding = "utf-8"
        if "\ufffd" in resp.text:
            resp.encoding = resp.apparent_encoding

        # 检测是否被反爬虫/动态网站
        blocked, reason = is_anti_scraping(resp)
        if blocked:
            # 回退链：Crawl4AI 本地渲染 → Firecrawl 云渲染 → 本机浏览器渲染 (CDP → Playwright)
            # 仍然过不去（反爬/登录墙）→ 最终回退：交给 Playwright MCP 驱动用户已登录的 Edge 完成
            if crawl4ai_available():
                c4 = _fetch_via_crawl4ai(url, output_format)
                if c4:
                    return _trim_content_result(url, c4, output_format, "crawl4ai",
                                                reason=reason, max_length=max_length,
                                                start_index=start_index)
            if firecrawl_configured():
                fc = _fetch_via_firecrawl(url, output_format)
                if fc:
                    return _trim_content_result(url, fc, output_format, "firecrawl",
                                                reason=reason, max_length=max_length,
                                                start_index=start_index)
            # 自动回退到浏览器渲染 (CDP → Playwright)
            pw_title, pw_text = fetch_with_playwright(url)
            if pw_title or pw_text:
                chunk, slice_meta = _slice_content(pw_text, start_index, max_length)
                return make_tool_result(
                    _format_content(pw_title, url, chunk, output_format) + _continuation_note(slice_meta),
                    meta={"source": url, "renderer": "browser", "reason": reason, **slice_meta},
                )
            return make_tool_result(
                f"HTTP 请求被拦截({reason})，本地渲染也未能获取内容。\n"
                f"最终回退：改用 Playwright MCP（browser_* 工具）在用户已登录的 Edge 里打开该页面完成任务。",
                meta={"source": url, "error": True, "reason": reason},
            )

        # 正常内容 → BeautifulSoup 提取正文
        content = _extract_readable_content(resp.text)
        title = content["title"]
        text = content["markdown"] if output_format == "markdown" else content["text"]
        chunk, slice_meta = _slice_content(text, start_index, max_length)

        return make_tool_result(
            _format_content(title, url, chunk, output_format) + _continuation_note(slice_meta),
            meta={"source": url, "renderer": "bs4", "title": title, **slice_meta},
        )

    except Exception as e:
        err_str = str(e)
        # 所有网络异常都先尝试本地渲染/云渲染，再回退本机浏览器渲染
        # （DNS 失败、SSL 错误、连接拒绝、超时等，不局限于特定的 HTTP 状态码）
        try:
            if crawl4ai_available():
                c4 = _fetch_via_crawl4ai(url, output_format)
                if c4:
                    return _trim_content_result(url, c4, output_format, "crawl4ai",
                                                max_length=max_length, start_index=start_index)
        except Exception:
            pass
        try:
            if firecrawl_configured():
                fc = _fetch_via_firecrawl(url, output_format)
                if fc:
                    return _trim_content_result(url, fc, output_format, "firecrawl",
                                                max_length=max_length, start_index=start_index)
        except Exception:
            pass
        try:
            pw_title, pw_text = fetch_with_playwright(url)
            if pw_title or pw_text:
                chunk, slice_meta = _slice_content(pw_text, start_index, max_length)
                return make_tool_result(
                    _format_content(pw_title, url, chunk, output_format) + _continuation_note(slice_meta),
                    meta={"source": url, "renderer": "browser", "error_reason": err_str, **slice_meta},
                )
        except Exception:
            pass
        return make_error_result(f"获取网页失败: {e}", detail=err_str)


def download_file(url: str, output_path: str) -> dict:
    """下载文件到本地，支持大文件流式下载。"""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return make_error_result(f"无效的 URL: {url}")

    session = make_session()

    try:
        resp = session.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT)
        resp.raise_for_status()

        total = int(resp.headers.get("content-length", 0))
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        downloaded = 0
        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)

        size_mb = downloaded / (1024 * 1024)
        return make_tool_result(
            f"✅ 下载完成！\n"
            f"   来源: {url}\n"
            f"   保存到: {output_path}\n"
            f"   大小: {size_mb:.1f} MB",
            meta={
                "source": url,
                "output_path": output_path,
                "size_bytes": downloaded,
                "size_mb": round(size_mb, 1),
            },
        )

    except Exception as e:
        return make_error_result(f"下载失败: {e}")
