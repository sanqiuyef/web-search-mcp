# -*- coding: utf-8 -*-
"""
Firecrawl 抓取后端：把 URL 交给 Firecrawl 云 API 渲染为干净 Markdown。

定位：web_fetch 被反爬拦截时的第一优先级回退。
Firecrawl 有独立的浏览器渲染池 + 代理轮换，反爬能力远超本机直接抓取。

无需 API Key 时本模块自动降级为空操作（返回 None），不影响原有链路。
端点当前为 v2，若返回 404 自动回退 v1。
"""

import re
import time
from typing import Dict, Optional

import requests

from src.config import (
    FIRECRAWL_API_KEY,
    FIRECRAWL_TIMEOUT,
    FIRECRAWL_ENDPOINTS,
)


def is_configured() -> bool:
    """是否配置了 FIRECRAWL_API_KEY。"""
    return bool(FIRECRAWL_API_KEY)


def _scrape_once(endpoint: str, url: str, timeout: int) -> Optional[Dict]:
    """调用一次 scrape 端点，成功返回 data 字典，失败返回 None。"""
    headers = {
        "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
        "removeBase64Images": True,
        "timeout": timeout * 1000,
    }
    resp = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
    if resp.status_code == 404:
        return None  # 端点不存在，交给上层换端点
    if resp.status_code != 200:
        return None
    data = resp.json().get("data") or {}
    markdown = data.get("markdown") or ""
    if not markdown or len(markdown.strip()) < 50:
        return None
    return data


def scrape_with_firecrawl(url: str, timeout: Optional[int] = None) -> Optional[Dict]:
    """抓取 URL，返回 {title, markdown}；不可用/失败返回 None。"""
    if not is_configured():
        return None
    timeout = timeout or FIRECRAWL_TIMEOUT

    for endpoint in FIRECRAWL_ENDPOINTS:
        try:
            data = _scrape_once(endpoint, url, timeout)
            if data is not None:
                metadata = data.get("metadata") or {}
                title = metadata.get("title") or ""
                return {"title": title, "markdown": data.get("markdown", "")}
        except requests.RequestException:
            continue
        except Exception:
            continue
    return None


# ═══════════════════════════════════════════════════════════
# Markdown → 纯文本
# ═══════════════════════════════════════════════════════════


_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_EMPH_RE = re.compile(r"\*\*?([^*]+)\*\*?")
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*")
_MD_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
_MD_QUOTE_RE = re.compile(r"^\s{0,3}>\s?")
_MD_BLOCK_RE = re.compile(r"^```.*?```", re.S | re.M)
_MD_CODE_TAG_RE = re.compile(r"`{1,3}")

_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


def markdown_to_text(markdown: str) -> str:
    """把 Markdown 转为纯文本，保留段落和列表结构。"""
    text = _MD_BLOCK_RE.sub("", markdown)
    text = _MD_IMAGE_RE.sub("", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_EMPH_RE.sub(r"\1", text)
    text = _MD_CODE_TAG_RE.sub("", text)
    lines = []
    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        if _TABLE_SEP_RE.match(line):
            continue
        line = _MD_HEADING_RE.sub("", line)
        line = _MD_QUOTE_RE.sub("", line)
        line = _MD_LIST_RE.sub("", line)
        line = line.strip()
        # 跳过纯表格行（以 | 开头且含多个 |）
        if line.startswith("|") and line.count("|") >= 3:
            continue
        if line:
            lines.append(line)
    return "\n".join(lines)
