# -*- coding: utf-8 -*-
"""
站点定向搜索：直接调各平台公开 API，返回结构化结果。

比通用网页搜索更适合 AI：API 返回的就是结构化元数据（stars、语言、版本、描述），
无需解析 HTML、不怕反爬、结果准确。

本文件是「零凭据」的开源平台 API 集合：GitHub 仓库（主力站点）、npm、PyPI、
GitLab、crates.io、Maven Central、NuGet、Docker Hub、HuggingFace。
需要登录态的站点搜索（Twitter/X）不带 API key 可用，单独放在 twitter.py。
所有请求都带代理（与搜索引擎一致），结果走统一缓存。
"""

from typing import Dict, List, Optional

import requests

from src.cache import make_cache_key, cache_get, cache_set
from src.utils import make_session, make_tool_result, make_error_result


def _site_search(name: str, keyword: str, max_results: int, fetch_fn) -> dict:
    """站点搜索的公共包装：缓存 + 异常处理 + 统一输出格式。"""
    cache_key = make_cache_key(name, keyword, max_results, "site")
    cached = cache_get(cache_key)
    if cached:
        return cached

    try:
        results = fetch_fn(keyword, max_results)
        if not results:
            return make_tool_result(f"{name} 未找到与「{keyword}」相关的结果。")
        cache_set(cache_key, results)
    except Exception as e:
        return make_error_result(f"{name} 搜索失败: {e}", detail=str(e))

    lines = [f"🔎 **{name} 搜索结果**「{keyword}」（共 {len(results)} 条）", ""]
    for i, item in enumerate(results, 1):
        lines.append(f"{i}. **{item['name']}** — {item['url']}")
        if item.get("description"):
            lines.append(f"   {item['description'][:150]}")
        if item.get("extra"):
            lines.append(f"   {item['extra']}")
    lines.append("")
    return make_tool_result(
        "\n".join(lines),
        results=results,
        meta={"site": name, "keyword": keyword, "total": len(results)},
    )


# ═══════════════════════════════════════════════════════════
# GitHub 仓库搜索
# ═══════════════════════════════════════════════════════════


def github_repo_search(keyword: str, max_results: Optional[int] = 10) -> dict:
    """按关键词搜索 GitHub 仓库，返回名称、描述、stars、语言、fork 数。

    使用 GitHub 公开 REST API（无需 key，匿名限流 60 次/小时）。
    """
    if not keyword or not keyword.strip():
        return make_error_result("keyword 不能为空。")
    max_results = min(max(1, max_results or 10), 20)

    def _fetch(kw: str, mr: int) -> List[Dict]:
        session = make_session()
        resp = session.get(
            "https://api.github.com/search/repositories",
            params={"q": kw, "sort": "stars", "order": "desc", "per_page": mr},
            headers={"Accept": "application/vnd.github+json"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        items = []
        for repo in data.get("items", []):
            items.append({
                "name": repo.get("full_name", ""),
                "url": repo.get("html_url", ""),
                "description": repo.get("description") or "",
                "extra": (
                    f"⭐ {repo.get('stargazers_count', 0)} | "
                    f"🍴 {repo.get('forks_count', 0)} | "
                    f"语言: {repo.get('language') or 'N/A'} | "
                    f"更新: {repo.get('pushed_at', '')[:10]}"
                ),
            })
        return items

    return _site_search("GitHub", keyword, max_results, _fetch)


# ═══════════════════════════════════════════════════════════
# npm 包搜索
# ═══════════════════════════════════════════════════════════


def npm_package_search(keyword: str, max_results: Optional[int] = 10) -> dict:
    """按关键词搜索 npm 包，返回包名、版本、描述、作者。

    使用 npm registry 公开 API（无需 key）。
    """
    if not keyword or not keyword.strip():
        return make_error_result("keyword 不能为空。")
    max_results = min(max(1, max_results or 10), 20)

    def _fetch(kw: str, mr: int) -> List[Dict]:
        session = make_session()
        resp = session.get(
            "https://registry.npmjs.org/-/v1/search",
            params={"text": kw, "size": mr},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        items = []
        for obj in data.get("objects", []):
            pkg = obj.get("package", {})
            author = pkg.get("author") or {}
            items.append({
                "name": pkg.get("name", ""),
                "url": f"https://www.npmjs.com/package/{pkg.get('name', '')}",
                "description": pkg.get("description") or "",
                "extra": (
                    f"v{pkg.get('version', '')} | "
                    f"作者: {(author.get('name') if isinstance(author, dict) else author) or 'N/A'}"
                ),
            })
        return items

    return _site_search("npm", keyword, max_results, _fetch)


# ═══════════════════════════════════════════════════════════
# PyPI 包搜索
# ═══════════════════════════════════════════════════════════


def pypi_package_search(package_name: str) -> dict:
    """查询 PyPI 包的完整信息（需精确包名）。

    PyPI 已禁用模糊搜索 API（XML-RPC/HTML 均被移除），但有稳定的 JSON
    精确查询：/pypi/{name}/json 返回版本、作者、Python 版本要求、简介等。
    不确定包名时，可先用 github_repo_search 或通用搜索找到包名。

    Args:
        package_name: PyPI 包名（精确，如 "crawl4ai"）。

    Returns:
        该包的版本、作者、简介、Python 版本要求、项目主页。
    """
    if not package_name or not package_name.strip():
        return make_error_result("package_name 不能为空。")
    name = package_name.strip().lower()

    cache_key = make_cache_key("pypi", name, 1, "site")
    cached = cache_get(cache_key)
    if cached:
        return cached

    try:
        session = make_session()
        resp = session.get(f"https://pypi.org/pypi/{name}/json", timeout=15)
        if resp.status_code == 404:
            return make_tool_result(
                f"PyPI 上未找到包「{name}」。\n"
                f"提示：PyPI 无公开模糊搜索 API，若不确定包名，"
                f"可先用 github_repo_search 或通用搜索找到准确包名。"
            )
        resp.raise_for_status()
        info = resp.json().get("info", {})
        result = [{
            "name": info.get("name", name),
            "url": f"https://pypi.org/project/{name}/",
            "description": info.get("summary") or "",
            "extra": (
                f"v{info.get('version', '')} | "
                f"作者: {info.get('author') or 'N/A'} | "
                f"Python: {info.get('requires_python') or '任意'} | "
                f"主页: {info.get('home_page') or info.get('project_urls', {}).get('Homepage', 'N/A')}"
            ),
        }]
        cache_set(cache_key, result)
    except Exception as e:
        return make_error_result(f"PyPI 查询失败: {e}", detail=str(e))

    return make_tool_result(
        f"🔎 **PyPI 包信息**「{result[0]['name']}」\n\n"
        f"1. **{result[0]['name']}** — {result[0]['url']}\n"
        f"   {result[0]['description'][:200]}\n"
        f"   {result[0]['extra']}",
        results=result,
        meta={"site": "PyPI", "package": name},
    )


# ═══════════════════════════════════════════════════════════
# GitLab 仓库搜索
# ═══════════════════════════════════════════════════════════


def gitlab_repo_search(keyword: str, max_results: Optional[int] = 10) -> dict:
    """按关键词搜索 GitLab 仓库，返回名称、描述、stars、fork 数。

    使用 GitLab 公开 API（gitlab.com，无需 key）。
    """
    if not keyword or not keyword.strip():
        return make_error_result("keyword 不能为空。")
    max_results = min(max(1, max_results or 10), 20)

    def _fetch(kw: str, mr: int) -> List[Dict]:
        session = make_session()
        resp = session.get(
            "https://gitlab.com/api/v4/projects",
            # order_by 仅支持 id/name/path/created_at/updated_at/last_activity_at
            params={"search": kw, "order_by": "last_activity_at", "sort": "desc", "per_page": mr},
            timeout=15,
        )
        resp.raise_for_status()
        items = []
        for project in resp.json():
            items.append({
                "name": project.get("path_with_namespace", ""),
                "url": project.get("web_url", ""),
                "description": project.get("description") or "",
                "extra": (
                    f"⭐ {project.get('star_count', 0)} | "
                    f"🍴 {project.get('forks_count', 0)} | "
                    f"更新: {project.get('last_activity_at', '')[:10]}"
                ),
            })
        return items

    return _site_search("GitLab", keyword, max_results, _fetch)


# ═══════════════════════════════════════════════════════════
# crates.io 包搜索（Rust）
# ═══════════════════════════════════════════════════════════


def crates_package_search(keyword: str, max_results: Optional[int] = 10) -> dict:
    """按关键词搜索 crates.io 的 Rust 包，返回名称、版本、描述、下载量。

    crates.io 要求请求带含邮箱的 User-Agent，否则返回 403。
    """
    if not keyword or not keyword.strip():
        return make_error_result("keyword 不能为空。")
    max_results = min(max(1, max_results or 10), 20)

    def _fetch(kw: str, mr: int) -> List[Dict]:
        session = make_session()
        session.headers.update({"User-Agent": "web-search-server (mcp; contact: search@localhost)"})
        resp = session.get(
            "https://crates.io/api/v1/crates",
            params={"q": kw, "per_page": mr},
            timeout=15,
        )
        resp.raise_for_status()
        items = []
        for crate in resp.json().get("crates", []):
            crate_id = crate.get("id", "")
            items.append({
                "name": crate_id,
                "url": f"https://crates.io/crates/{crate_id}",
                "description": crate.get("description") or "",
                "extra": (
                    f"v{crate.get('max_version', '')} | "
                    f"总下载: {crate.get('downloads', 0):,}"
                ),
            })
        return items

    return _site_search("crates.io", keyword, max_results, _fetch)


# ═══════════════════════════════════════════════════════════
# Maven Central 包搜索（Java）
# ═══════════════════════════════════════════════════════════


def maven_package_search(keyword: str, max_results: Optional[int] = 10) -> dict:
    """按关键词搜索 Maven Central 的 Java 包，返回 groupId:artifactId、最新版本、更新时间。"""
    if not keyword or not keyword.strip():
        return make_error_result("keyword 不能为空。")
    max_results = min(max(1, max_results or 10), 20)

    def _fetch(kw: str, mr: int) -> List[Dict]:
        session = make_session()
        resp = session.get(
            "https://search.maven.org/solrsearch/select",
            params={"q": kw, "rows": mr, "wt": "json"},
            timeout=15,
        )
        resp.raise_for_status()
        docs = resp.json().get("response", {}).get("docs", [])
        items = []
        for doc in docs:
            artifact = doc.get("id", "")
            g, a = doc.get("g", ""), doc.get("a", "")
            ts = doc.get("timestamp", 0)
            updated = ""
            if ts:
                import datetime
                updated = datetime.datetime.fromtimestamp(ts / 1000, datetime.UTC).strftime("%Y-%m-%d")
            items.append({
                "name": artifact,
                "url": f"https://search.maven.org/artifact/{g}/{a}",
                "description": doc.get("latestVersion", "") and f"最新版本: {doc['latestVersion']}" or "",
                "extra": f"最新: v{doc.get('latestVersion', '')} | 更新: {updated}",
            })
        return items

    return _site_search("Maven Central", keyword, max_results, _fetch)


# ═══════════════════════════════════════════════════════════
# NuGet 包搜索（.NET）
# ═══════════════════════════════════════════════════════════


def nuget_package_search(keyword: str, max_results: Optional[int] = 10) -> dict:
    """按关键词搜索 NuGet 的 .NET 包，返回名称、版本、描述、作者、下载量。"""
    if not keyword or not keyword.strip():
        return make_error_result("keyword 不能为空。")
    max_results = min(max(1, max_results or 10), 20)

    def _fetch(kw: str, mr: int) -> List[Dict]:
        session = make_session()
        resp = session.get(
            "https://azuresearch-usnc.nuget.org/query",
            params={"q": kw, "take": mr},
            timeout=15,
        )
        resp.raise_for_status()
        items = []
        for pkg in resp.json().get("data", []):
            pkg_id = pkg.get("id", "")
            authors = pkg.get("authors") or []
            items.append({
                "name": pkg_id,
                "url": f"https://www.nuget.org/packages/{pkg_id}",
                "description": pkg.get("description") or "",
                "extra": (
                    f"v{pkg.get('version', '')} | "
                    f"作者: {(', '.join(authors) if isinstance(authors, list) else authors) or 'N/A'} | "
                    f"下载: {pkg.get('totalDownloads', 0):,}"
                ),
            })
        return items

    return _site_search("NuGet", keyword, max_results, _fetch)


# ═══════════════════════════════════════════════════════════
# Docker Hub 镜像搜索
# ═══════════════════════════════════════════════════════════


def docker_image_search(keyword: str, max_results: Optional[int] = 10) -> dict:
    """按关键词搜索 Docker Hub 镜像，返回名称、描述、star、pull 数。"""
    if not keyword or not keyword.strip():
        return make_error_result("keyword 不能为空。")
    max_results = min(max(1, max_results or 10), 20)

    def _fetch(kw: str, mr: int) -> List[Dict]:
        session = make_session()
        resp = session.get(
            "https://hub.docker.com/v2/search/repositories/",
            params={"query": kw, "page_size": mr},
            timeout=15,
        )
        resp.raise_for_status()
        items = []
        for repo in resp.json().get("results", []):
            repo_name = repo.get("repo_name", "")
            items.append({
                "name": repo_name,
                "url": f"https://hub.docker.com/r/{repo_name}",
                "description": repo.get("short_description") or "",
                "extra": (
                    f"⭐ {repo.get('star_count', 0)} | "
                    f"下载: {repo.get('pull_count', 0):,}"
                ),
            })
        return items

    return _site_search("Docker Hub", keyword, max_results, _fetch)


# ═══════════════════════════════════════════════════════════
# HuggingFace 模型搜索
# ═══════════════════════════════════════════════════════════


def huggingface_model_search(keyword: str, max_results: Optional[int] = 10) -> dict:
    """按关键词搜索 HuggingFace 模型，返回名称、下载量、likes、任务类型。"""
    if not keyword or not keyword.strip():
        return make_error_result("keyword 不能为空。")
    max_results = min(max(1, max_results or 10), 20)

    def _fetch(kw: str, mr: int) -> List[Dict]:
        session = make_session()
        resp = session.get(
            "https://huggingface.co/api/models",
            params={"search": kw, "limit": mr},
            timeout=15,
        )
        resp.raise_for_status()
        items = []
        for model in resp.json():
            model_id = model.get("id", "")
            pipeline = model.get("pipeline_tag")
            items.append({
                "name": model_id,
                "url": f"https://huggingface.co/{model_id}",
                "description": "",
                "extra": (
                    f"下载: {model.get('downloads', 0):,} | "
                    f"♥ {model.get('likes', 0)} | "
                    f"任务: {pipeline or 'N/A'}"
                ),
            })
        return items

    return _site_search("HuggingFace", keyword, max_results, _fetch)
