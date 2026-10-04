# -*- coding: utf-8 -*-
"""
工具函数：URL 标准化、代理、格式输出、反爬检测、Playwright 回退。
"""

import base64
import re
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, unquote

from src.config import (
    HEADERS,
    get_proxies,
    has_proxy,
    BROWSER_TIMEOUT,
    CDP_PORT,
    CDP_HOST,
    RELEVANCE_FILTER_ENABLED,
)


# ═══════════════════════════════════════════════════════════
# URL 处理
#
# 去重键的质量决定融合质量：同一页面在不同引擎下若 URL 形式不同
# （www 前缀、http/https、跳转包、跟踪参数），跨引擎共识就数不出来，
# 多源加权也就失效。参考 SearXNG 的去重键（去掉 scheme）与 4get 的
# unshiturl()（解跳转包）。
# ═══════════════════════════════════════════════════════════

# 归因类参数：只用于统计来源，不影响页面内容，可从身份键中剔除。
# 刻意保守：只列高置信度的，像 ref / source / from 这类在某些站点上
# 可能承载内容选择（论坛、分页），剔除会误合并不同页面。
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "utm_referrer", "utm_source_platform",
    "gclid", "fbclid", "msclkid", "dclid", "yclid", "twclid", "igshid",
    "mc_cid", "mc_eid", "_ga", "_gl", "spm", "ref_src",
    # eBay 系（4get 的 unshiturl 里硬编码剥掉的那批）
    "mkevt", "mkcid", "mkrid", "campid", "customid", "toolid",
    "_sop", "_dcat", "epid", "oid",
})


def unwrap_redirect(url: str) -> str:
    """解开搜索引擎的跳转包装，返回真实 URL。

    不解包的后果很严重：同一页面在 DDG 与 Bing 下 URL 完全不同，
    跨引擎去重失效、来源数恒为 1，多源加权与共识排序直接失去意义。
    各家形式（参考 4get scraper/ddg.php::unshiturl）：
      DDG    //duckduckgo.com/l/?uddg=<urlencoded>
      Bing   /ck/a?...&u=a1<base64url>
      Yandex /redir?url=<urlencoded>
      Google /url?q=<urlencoded>
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    host = (parsed.hostname or "").lower()
    if not host:
        return url
    query = parse_qs(parsed.query)

    if host.endswith("duckduckgo.com") and query.get("uddg"):
        return unquote(query["uddg"][0])

    if host.endswith("bing.com") and parsed.path.startswith("/ck/a") and query.get("u"):
        raw = query["u"][0]
        if raw.startswith("a1"):
            raw = raw[2:]
        try:
            decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
            return decoded.decode("utf-8", "replace")
        except Exception:
            return url

    if host.endswith(("yandex.com", "yandex.ru", "yandex.com.tr")) and query.get("url"):
        return unquote(query["url"][0])

    if host.endswith("google.com") and parsed.path == "/url" and query.get("q"):
        return unquote(query["q"][0])

    return url


def url_identity_key(url: str) -> str:
    """页面身份键：用于判断两条结果是否指向同一页面。

    与展示用的 URL 分开：身份键去掉 scheme（http 与 https 视为同一页）、
    去掉 www. 前缀、主机名小写、去掉 fragment 与跟踪参数。query 其余部分
    保留 —— 剥离过多会把不同页面合并（free-search-mcp 实测否决过激进剥离）。
    """
    if not url:
        return ""
    target = unwrap_redirect(url.strip())
    try:
        parsed = urlparse(target)
    except ValueError:
        return target.lower()
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = ""
    if parsed.port and parsed.port not in (80, 443):
        port = f":{parsed.port}"
    path = parsed.path.rstrip("/") or "/"
    query = parse_qs(parsed.query, keep_blank_values=True)
    kept = {k: v for k, v in query.items() if k.lower() not in _TRACKING_PARAMS}
    query_str = urlencode(sorted(kept.items()), doseq=True) if kept else ""
    return f"{host}{port}{path}?{query_str}"


def normalize_url(url: str) -> str:
    """标准化 URL 用于展示：解跳转包、去跟踪参数与 fragment、去尾部斜杠。"""
    if not url:
        return url
    target = unwrap_redirect(url.strip())
    try:
        parsed = urlparse(target)
    except ValueError:
        return target
    if not parsed.netloc:
        return target.rstrip("/")
    query = parse_qs(parsed.query, keep_blank_values=True)
    kept = {k: v for k, v in query.items() if k.lower() not in _TRACKING_PARAMS}
    query_str = urlencode(kept, doseq=True) if kept else ""
    path = parsed.path.rstrip("/") if parsed.path != "/" else ""
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, query_str, ""))



# ═══════════════════════════════════════════════════════════
# 结果格式化
# ═══════════════════════════════════════════════════════════


def format_results(keyword: str, engine: str, items: List[str]) -> str:
    """将结果格式化为人类可读的文本。"""
    if not items:
        return f"{engine} 未搜索到「{keyword}」的相关结果"
    return f"{engine} 搜索结果「{keyword}」（共 {len(items)} 条）:\n\n" + "\n\n".join(items)


def is_junk_url(url: str, junk_domains: Optional[tuple] = None) -> bool:
    """判断 URL 是否属于词典/百科类噪声源（如 wikipedia、grokipedia）。"""
    from src.config import JUNK_RESULT_DOMAINS

    domains = junk_domains if junk_domains is not None else JUNK_RESULT_DOMAINS
    if not domains:
        return False
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


def filter_junk_results(results: List[Dict], min_keep: int = 1) -> List[Dict]:
    """过滤词典/百科类噪声结果。

    若过滤后剩余条目少于 min_keep（说明该查询确实只有百科类答案），
    则原样返回未过滤结果，避免把有效答案也清空。
    """
    if not results:
        return results
    kept = [r for r in results if not is_junk_url(r.get("url", ""))]
    return kept if len(kept) >= min_keep else results


# ═══════════════════════════════════════════════════════════
# 查询相关性判定
#
# 背景：搜索引擎会偶发返回与查询词完全无关的结果集（实测 cn.bing.com 对
# 「储能 电池 报价」返回 10 条 QQ 邮箱登录页）。这类结果如果只按“引擎排名”
# 参与合并评分，会因为位置权重被排到真结果前面。这里按查询词命中情况把每条
# 结果分成三档，供上层剔除 C 档、优先 A 档。
# ═══════════════════════════════════════════════════════════

# 功能词：命中与否不体现相关性（"最新/推荐/什么" 这类词几乎出现在任何页面上）
_QUERY_STOPWORDS = frozenset("""
的 是 了 在 和 与 及 或 有 我 你 他 她 它 这 那 之 其 并 而 就 都 也 很
什么 怎么 怎样 如何 哪些 哪个 为什么 多少 是否 可以 能否 要不要 有没有
请 帮我 一下 介绍 最新 推荐 大全 方法 教程 指南 区别 对比 排行 排名 官网
吗 呢 吧 啊 哦 呀
the a an of to for in on and or is are be by with from as at it its this that
how what why which best top vs guide tutorial docs doc
""".split())

# 拉丁词按词边界匹配，避免 "excel" 命中 "excellent"
_LATIN_TERM_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+#.\-]*")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 长中文词的滑窗长度。中文查询没有空格，整串匹配会漏掉标题措辞略有差异的页面：
# 「水库大坝安全监测技术规范」拆成 4 字窗口后，标题为「大坝安全监测技术规范」的
# 页面才能命中，否则整条查询会被判为不相关而误杀。
_CJK_WINDOW = 4


def _split_query_terms(keyword: str) -> List[str]:
    """把查询切成候选词：按空白与常见标点切分，长中文词再按 4 字滑窗展开。"""
    if not keyword:
        return []
    parts = re.split(r"[\s,，、;；|/\\()（）\[\]{}<>\"'“”‘’:：!！?？~@#$%^&*+=]+", keyword)
    terms: List[str] = []
    for part in (x.strip() for x in parts):
        if not part:
            continue
        if _CJK_RE.search(part) and len(part) > _CJK_WINDOW:
            terms.extend(part[i:i + _CJK_WINDOW]
                         for i in range(len(part) - _CJK_WINDOW + 1))
        else:
            terms.append(part)
    return terms


def _is_strong_term(term: str) -> bool:
    """判断一个查询词是否具备区分度（够长、非纯数字、非功能词）。"""
    t = term.strip().lower()
    if not t or t in _QUERY_STOPWORDS:
        return False
    if t.isdigit():  # 年份、编号这类词几乎不体现主题
        return False
    if _CJK_RE.search(t):
        return len(t) >= 2
    return len(t) >= 2


def query_terms(keyword: str) -> Tuple[List[str], List[str]]:
    """返回 (强词, 弱词)。强词用于判定相关性，弱词仅作兜底。

    功能词（"最新/怎么/推荐"…）两侧都不进：它们几乎出现在任何页面上，
    拿来判定相关性只会把无关结果也判成相关。查询里全是功能词时两侧都为空，
    上层据此跳过相关性过滤。
    """
    strong, weak = [], []
    for term in _split_query_terms(keyword):
        if term.strip().lower() in _QUERY_STOPWORDS:
            continue
        (strong if _is_strong_term(term) else weak).append(term)
    return strong, weak


def _term_in_text(term: str, text_lower: str) -> bool:
    """判断查询词是否出现在文本里（拉丁词按词边界，CJK 按子串）。"""
    t = term.lower()
    if not t:
        return False
    if _CJK_RE.search(t):
        return t in text_lower
    return re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", text_lower) is not None


def match_query_terms(keyword: str, title: str, snippet: str = "", url: str = "") -> Tuple[int, int]:
    """统计结果命中查询词的个数，返回 (命中强词数, 命中弱词数)。

    URL 也参与匹配：部分结果标题泛化（如“行业动态”），查询词只出现在链接里，
    只看标题+摘要会把这类结果误判为不相关。
    """
    strong, weak = query_terms(keyword)
    text = f"{title or ''} {snippet or ''} {url or ''}".lower()
    matched_strong = sum(1 for t in strong if _term_in_text(t, text))
    matched_weak = sum(1 for t in weak if _term_in_text(t, text))
    return matched_strong, matched_weak


def relevance_tier(keyword: str, item: Dict) -> Optional[str]:
    """把单条结果判为 "A"（强相关）/ "B"（弱相关）/ "C"（不相关）。

    查询词里没有可用强词时返回 None，表示无法判定（上层跳过相关性过滤）。
    A：命中多数强词（查询只有 1 个强词时命中 1 个即可）
    B：命中至少 1 个强词（或查询无强词时命中弱词）
    C：一个都没命中 —— 引擎返回了与查询无关的结果集
    """
    if not RELEVANCE_FILTER_ENABLED:
        return None
    strong, weak = query_terms(keyword)
    if not strong and not weak:
        return None
    matched_strong, matched_weak = match_query_terms(
        keyword, item.get("title", ""), item.get("snippet", ""), item.get("url", "")
    )
    if strong:
        need_strong = min(2, len(strong))
        if matched_strong >= need_strong:
            return "A"
        return "B" if matched_strong >= 1 else "C"
    # 查询里只有年份/功能词等弱词：命中即算强相关
    return "A" if matched_weak >= 1 else "C"


def split_by_relevance(keyword: str, results: List[Dict]) -> Tuple[List[Dict], List[Dict], int]:
    """按相关性切分结果，返回 (强相关, 弱相关, 被剔除条数)。

    相关性无法判定时（无可用强词、或过滤被关闭）全部按强相关返回。
    判定结果会写回每条结果的 relevance_tier 字段，供输出层标注。
    """
    strong_hits, weak_hits, dropped = [], [], 0
    for item in results:
        tier = relevance_tier(keyword, item)
        if tier is not None:
            item["relevance_tier"] = tier
        if tier == "A" or tier is None:
            strong_hits.append(item)
        elif tier == "B":
            weak_hits.append(item)
        else:
            dropped += 1
    return strong_hits, weak_hits, dropped


# 标题中出现的站点后缀标记：命中 2 个以上说明多条标题被拼接
_TITLE_MARKERS = (
    "_百度百科", "_百度知道", "_搜狗百科",
    "维基百科", "Wikipedia", "wikipedia",
    "- 知乎", "| 知乎",
)
_TITLE_MAX_LEN = 80
# 域名后紧跟大写字母/中文字符 = 两条标题被粘在一起（如 "Python.orgPython 3.14"）
_TITLE_CONCAT_RE = re.compile(
    r"\.(?:com|org|net|cn|gov|edu|io|co|me)(?=[A-Z\u4e00-\u9fff])"
)


def sanitize_title(title: str) -> str:
    """清理搜索引擎解析产生的拼接标题（多条标题被粘成一条）。

    ddgs 的部分后端（实测 yahoo）在特定版式下会把同容器内多个标题文本
    拼接为一条，产生上百字符的标题墙。按可信度依次尝试切分：
    站点后缀标记 → 域名粘连点，都无法切分时再按长度兜底截断。
    """
    t = (title or "").strip()
    if not t:
        return t

    # 1) 站点后缀标记：命中 2 个以上说明确实拼接，切到第一个标记结尾
    marker_ends = sorted(
        t.find(m) + len(m) for m in _TITLE_MARKERS if t.find(m) != -1
    )
    if len(marker_ends) >= 2 and marker_ends[0] >= 4:
        return t[:marker_ends[0]]

    # 2) 域名粘连点：形如 "... Python.orgPython 3.14" 时切到域名结尾
    m = _TITLE_CONCAT_RE.search(t)
    if m and m.end() >= 6:
        return t[:m.end()]

    # 3) 长度兜底：按词边界截断，避免切出半个词
    if len(t) <= _TITLE_MAX_LEN:
        return t
    head = t[:_TITLE_MAX_LEN]
    for sep in (" ", "　", "|", "-", "—", "，", "。"):
        idx = head.rfind(sep)
        if idx >= _TITLE_MAX_LEN // 2:
            return head[:idx].rstrip() + "…"
    return head.rstrip() + "…"


# ═══════════════════════════════════════════════════════════
# 引擎熔断
#
# 背景：ddgs 走 Rust 解析器，若 stderr 写入被阻塞（实测本机 hosts 文件损坏时，
# 每次查询都会写一条 ~1.1KB 的警告，stderr 管道写满后进程卡在写日志上），
# 该引擎的调用会一直挂到上层超时，且占住的工作线程永远不释放。
# 连续超时就暂时停用该引擎，避免整个搜索能力被拖死。
# ═══════════════════════════════════════════════════════════

ENGINE_FAILURE_LIMIT = 2      # 连续超时几次后熔断
ENGINE_COOLDOWN_SECONDS = 300  # 熔断冷却时长（秒）

_engine_failures: Dict[str, int] = {}
_engine_cooldown_until: Dict[str, float] = {}


def note_engine_result(name: str, ok: bool) -> None:
    """记录一次引擎调用结果：成功清零，失败累加并在到阈值时熔断。"""
    if ok:
        _engine_failures.pop(name, None)
        return
    count = _engine_failures.get(name, 0) + 1
    _engine_failures[name] = count
    if count >= ENGINE_FAILURE_LIMIT:
        _engine_cooldown_until[name] = time.time() + ENGINE_COOLDOWN_SECONDS
        _engine_failures[name] = 0


def engine_available(name: str) -> bool:
    """引擎是否可用（未在熔断冷却期）。"""
    until = _engine_cooldown_until.get(name)
    if until is None:
        return True
    if time.time() >= until:
        _engine_cooldown_until.pop(name, None)
        return True
    return False


def engine_cooldown_remaining(name: str) -> int:
    """熔断剩余秒数，未熔断返回 0。"""
    until = _engine_cooldown_until.get(name)
    if until is None:
        return 0
    return max(0, int(until - time.time()))


def reset_engine_health() -> None:
    """清空熔断状态（测试用）。"""
    _engine_failures.clear()
    _engine_cooldown_until.clear()


def make_tool_result(
    markdown: str,
    results: Optional[List[Dict]] = None,
    meta: Optional[Dict] = None,
) -> Dict:
    """
    构造结构化的工具返回结果。

    返回格式：
    {
        "content": markdown,   # 人类可读的文本
        "results": results,    # 结构化结果列表（可选）
        "meta": meta,          # 元数据（可选）
    }
    """
    return {
        "content": markdown,
        "results": results or [],
        "meta": meta or {},
    }


def make_error_result(error_msg: str, detail: Optional[str] = None) -> Dict:
    """构造错误响应。"""
    result = {
        "content": f"❌ {error_msg}",
        "results": [],
        "meta": {"error": True, "message": error_msg},
    }
    if detail:
        result["meta"]["detail"] = detail
    return result


# ═══════════════════════════════════════════════════════════
# 结果合并去重评分（借鉴 SearXNG 元搜索思路）
# ═══════════════════════════════════════════════════════════


ENGINE_ICONS = {
    "duckduckgo": "🦆",
    "bing": "🅱️",
    "google": "🔍",
    "searxng": "🔎",
}

ENGINE_LABELS = {
    "duckduckgo": "DuckDuckGo",
    "bing": "Bing",
    "google": "Google",
    "searxng": "SearXNG",
}


# ═══════════════════════════════════════════════════════════
# 近重复聚类（内容级去重）
#
# URL 身份键只能识别"同一链接"，识别不了"不同链接、同一内容"（转载/镜像）。
# 这类条目会各算一个来源，既污染来源计数，又让融合层看不见它们其实同源。
# 用字符 bigram Jaccard 聚类：中文不做分词（分词粒度不一致会让同义改写直接
# 崩掉），字符 n-gram 对改写更宽容且零依赖。
#
# 阈值 0.35 是在本机真实搜索结果上标定的（7 个查询 55 对相似配对）：
#   真重复区间 0.378-0.810（政策原文双站发布、字典页、垃圾站同模板）
#   假配对上限 0.333（知乎/掘金两篇不同 MCP 文章、autodesk 两个不同页面）
#   阈值落在 0.333~0.378 的间隙里。低于 0.30 会把"同主题不同文章"合掉。
#
# 刻意不做"仅标题相似"这条规则：实测会触发它的 5 对里 3 对是误合并
# （mcp-docs.cn 与 IBM 两篇不同文章、pythonlang.cn 首页与下载页、
# 菜鸟教程 python 与 python3 教程），只有 1 对是它独有的真重复，
# 而那一对是"同一份文件发在两个官网"——重复显示无害，误合并却会藏掉独立来源。
# ═══════════════════════════════════════════════════════════

_SHINGLE_PUNCT_RE = re.compile(r"[\s,，、;；|/\\()（）\[\]{}<>\"'“”‘’:：!！?？~@#$%^&*+=—\-…。·]+")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_TRUNCATED_TITLE_RE = re.compile(r"(\.\.\.|…)\s*$")

NEAR_DUP_THRESHOLD = 0.35        # 标题+摘要 的字符 bigram Jaccard
RRF_K = 10                       # RRF 平滑常数：见 merge_results 说明


def _char_shingles(text: str, k: int = 2) -> frozenset:
    """字符 k-gram 集合（先剔标点空白）。中文用字符级，不做分词。"""
    s = _SHINGLE_PUNCT_RE.sub("", text or "")
    if len(s) < k:
        return frozenset([s]) if s else frozenset()
    return frozenset(s[i:i + k] for i in range(len(s) - k + 1))


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def _number_guard_blocks(title_a: str, title_b: str) -> bool:
    """数字守卫：标题里的版本号/年份/编号不同时，视为不同内容。

    否则 "Python 3.13" 与 "Python 3.12"、"2026年政策" 与 "2025年政策"
    会被判为重复。已知局限：标题是 URL 形态时（解析失败回退成链接）会把
    数字当版本号，可能拦下真重复——这个方向是可接受的（少合并优于误合并）。
    """
    nums_a = set(_NUMBER_RE.findall(title_a))
    nums_b = set(_NUMBER_RE.findall(title_b))
    return bool(nums_a and nums_b and nums_a != nums_b)


def _item_title(item: Dict) -> str:
    """取条目的代表标题：兼容单数键（引擎原始结果）与复数键（合并阶段的候选列表）。"""
    if item.get("title"):
        return item["title"]
    titles = item.get("titles") or []
    return max(titles, key=len) if titles else ""


def _item_snippet(item: Dict) -> str:
    """取条目的代表摘要，兼容两种键名（见 _item_title）。"""
    if item.get("snippet"):
        return item["snippet"]
    snippets = item.get("snippets") or []
    return max(snippets, key=len) if snippets else ""


def cluster_near_duplicates(items: List[Dict]) -> List[List[int]]:
    """把内容相同的条目聚成一簇，返回下标簇列表（并查集传递闭包）。

    条目数在几十条量级，两两比较是 O(n²) 但常数极小（实测 435 对约 4ms），
    无需引入 MinHash/LSH —— 那个方案在这个规模下反而更慢且会漏判。
    """
    n = len(items)
    if n <= 1:
        return [[i] for i in range(n)]

    titles = [_SHINGLE_PUNCT_RE.sub("", _item_title(it)) for it in items]
    texts = [f"{t}{_SHINGLE_PUNCT_RE.sub('', _item_snippet(it))}"
             for t, it in zip(titles, items)]
    text_sets = [_char_shingles(t) for t in texts]

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if _number_guard_blocks(titles[i], titles[j]):
                continue
            if _jaccard(text_sets[i], text_sets[j]) >= NEAR_DUP_THRESHOLD:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _primary_member(members: List[Dict]) -> Dict:
    """从簇里挑主条目：优先标题未被引擎截断的，再取标题最长的。

    标题与 URL 必须来自同一成员 —— 分开择优会让模型拿着 A 的标题去访问
    B 的链接，标题与落地页对不上。
    """
    def _rank(m: Dict):
        title = (m.get("titles") or [""])[0] if m.get("titles") else ""
        best = max(m.get("titles") or [""], key=len)
        return (1 if _TRUNCATED_TITLE_RE.search(best) else 0, -len(best))
    return min(members, key=_rank)


def _member_title(member: Dict) -> str:
    titles = member.get("titles") or []
    if not titles:
        return ""
    full = [t for t in titles if not _TRUNCATED_TITLE_RE.search(t)]
    return max(full or titles, key=len)


def _member_url(member: Dict) -> str:
    """成员 URL：优先 https、优先不带 query，再取最短。"""
    urls = member.get("urls") or []
    if not urls:
        return ""
    def _rank(u: str):
        parsed = urlparse(u)
        return (0 if parsed.scheme == "https" else 1, 1 if parsed.query else 0, len(u))
    return sorted(urls, key=_rank)[0]


def merge_results(results_list: List[List[Dict]], max_results: int,
                  keyword: Optional[str] = None) -> List[Dict]:
    """
    多引擎结果合并：URL 身份去重 → 内容近重复聚类 → RRF 融合排序。

    融合算法用 RRF（Reciprocal Rank Fusion，Cormack 等 SIGIR 2009）：
        score(d) = Σ_引擎 1 / (k + rank_引擎(d))
    选它而不是自造加权公式的原因：
      - 只吃"名次"，不吃各引擎不可比的原始分数，天然免疫打分口径差异；
      - 逐引擎取倒数再求和，保留名次分布信息（把名次先平均再取倒数会丢信息：
        (1,9) 与 (5,5) 的平均名次都是 5，但前者说明有引擎把它排在第 1）；
      - 免调参。k=60 是论文的试点值，在 3 引擎 × top-10 的规模下会把名次差异
        压成 1.15 倍；k=10 保留 1.8 倍动态范围，同时跨引擎共识仍然占优
        （本地 200 次随机试验：k=10 的 nDCG@10 均值与最差值都最好）。

    keyword 非空时给每条结果附加 relevance_tier（A/B/C），排序仍以相关性档位为先：
    偏题结果即使排在引擎第一位，也不该压过相关结果。
    """
    # 1) URL 身份键去重：同一页面只留一条，合并各引擎给它的名次与字段候选
    entries: Dict[str, Dict] = {}
    for engine_results in results_list:
        for rank, item in enumerate(engine_results, 1):  # RRF 用 1-based 名次
            url = item.get("url", "")
            if not url:
                continue
            key = url_identity_key(url)
            engine = item.get("engine", "unknown")
            entry = entries.setdefault(
                key, {"titles": [], "snippets": [], "urls": [], "ranks": {}}
            )
            title = sanitize_title(item.get("title", ""))
            if title:
                entry["titles"].append(title)
            snippet = item.get("snippet", "")
            if snippet:
                entry["snippets"].append(snippet)
            entry["urls"].append(normalize_url(url))
            prev = entry["ranks"].get(engine)
            entry["ranks"][engine] = rank if prev is None else min(prev, rank)

    if not entries:
        return []

    # 2) 内容近重复聚类：不同 URL、同一内容的转载视为同一条
    entry_list = list(entries.values())
    clusters = cluster_near_duplicates(entry_list)

    # 3) 每簇合成一条结果：标题与 URL 取自同一主条目，摘要取全簇最长的
    merged: List[Dict] = []
    for cluster in clusters:
        members = [entry_list[i] for i in cluster]
        primary = _primary_member(members)
        snippets = [s for m in members for s in m["snippets"]]
        ranks: Dict[str, int] = {}
        for m in members:
            for engine, rank in m["ranks"].items():
                prev = ranks.get(engine)
                ranks[engine] = rank if prev is None else min(prev, rank)

        item = {
            "title": _member_title(primary),
            "url": _member_url(primary),
            "snippet": max(snippets, key=len) if snippets else "",
            "sources": sorted(ranks),
            "source_count": len(ranks),
            "ranks": ranks,
            "cluster_size": len(members),
        }
        item["score"] = round(sum(1.0 / (RRF_K + r) for r in ranks.values()), 4)
        item["best_rank"] = min(ranks.values()) if ranks else None
        if keyword:
            item["relevance_tier"] = relevance_tier(keyword, item)
        merged.append(item)

    # 4) 排序：相关性档位优先（A>B>C），同档内按 RRF 分数，再按最佳名次
    _tier_rank = {"A": 0, "B": 1, "C": 2, None: 0}
    merged.sort(key=lambda x: (_tier_rank.get(x.get("relevance_tier"), 0),
                               -x["score"], x["best_rank"] or 99))

    top = merged[:max_results]
    # 展示用强度：把 RRF 分数按本轮最高分归一到 0-10，便于模型判断相对强弱
    best_score = max((i["score"] for i in top), default=0.0)
    for i, item in enumerate(top, 1):
        item["index"] = i
        item["strength"] = round(10.0 * item["score"] / best_score, 1) if best_score else 0.0
    return top


def format_merged_results(keyword: str, merged: List[Dict], category: str, engine_names: List[str]) -> str:
    """格式化合并后的结果为人类可读文本。"""
    category_label = "新闻" if category == "news" else "网页"
    header = (
        f"🔎 **{category_label}聚合搜索结果** 「{keyword}」\n"
        f"   引擎: {', '.join(engine_names)} "
        f"| 合并后 {len(merged)} 条结果\n"
        f"{'─' * 60}"
    )

    lines = []
    for i, item in enumerate(merged, 1):
        sources = item.get("sources") or []
        # 来源标记：多源用图标组合，单源用名称
        if item.get("source_count", len(sources)) > 1 and sources:
            icon_str = " ".join(ENGINE_ICONS.get(s, s) for s in sources)
            source_info = f"[{icon_str}]"
        elif sources:
            label = ENGINE_LABELS.get(sources[0], sources[0])
            source_info = f"({label})"
        else:
            source_info = "(未知来源)"

        # 强度是本轮 RRF 分数归一化到 0-10 的相对值（榜首恒为 10）
        score_str = f"★{item.get('strength', 0):.1f}"
        weak_mark = " ⚠️相关性弱" if item.get("relevance_tier") == "B" else ""
        lines.append(
            f"{i}. **{item['title']}** {source_info} {score_str}{weak_mark}\n"
            f"   {item['snippet']}\n"
            f"   {item['url']}"
        )

    return header + "\n\n" + "\n\n".join(lines)


# ═══════════════════════════════════════════════════════════
# 反爬虫检测 & Playwright 回退
# ═══════════════════════════════════════════════════════════

# 注意：文本在检测前已转为小写，模式无需 re.I
_PLAYWRIGHT_ANTI_PATTERNS = [
    re.compile(r"cloudflare"),
    re.compile(r"cf-ray"),
    re.compile(r"checking your browser"),
    re.compile(r"just a moment"),
    re.compile(r"enable javascript"),
    re.compile(r"enable cookies"),
    re.compile(r"captcha"),
    re.compile(r"blocked"),
    re.compile(r"attention required"),
    re.compile(r"ddos protection"),
    re.compile(r"一个人机验证"),
    re.compile(r"正在检测"),
    re.compile(r"浏览器安全检查"),
]

_MIN_CONTENT_LENGTH = 200  # 低于此字节认为被拦截


def is_anti_scraping(resp) -> Tuple[bool, str]:
    """检测是否被反爬虫拦截。"""
    # 状态码检测
    if resp.status_code in (403, 429, 503):
        return True, f"HTTP {resp.status_code} 被拦截"

    # 内容检测（文本已转为小写）
    text_lower = (resp.text or "").lower()
    for pattern in _PLAYWRIGHT_ANTI_PATTERNS:
        if pattern.search(text_lower):
            return True, f"检测到反爬标记: {pattern.pattern}"

    # 空内容检测
    from bs4 import BeautifulSoup as _BS
    soup = _BS(resp.text or "", "html.parser")
    body_text = soup.get_text(separator="\n", strip=True)
    clean_lines = [l for l in body_text.split("\n") if l.strip()]
    clean_body = "\n".join(clean_lines)

    # 空内容检测：无 title 且正文极少 → 疑似 JS 渲染页；
    # 有 title 的合法短页（如错误提示页、短公告）不应触发浏览器渲染回退
    if not soup.title and len(clean_body) < _MIN_CONTENT_LENGTH:
        return True, f"无标题且内容过短({len(clean_body)} chars)，可能是 JS 动态渲染"

    return False, ""


def make_session():
    """创建标准 requests Session（带 headers 和代理）。"""
    import requests
    session = requests.Session()
    session.headers.update(HEADERS)
    proxies = get_proxies()
    if proxies:
        session.proxies.update(proxies)
    return session


def _fetch_via_cdp(url: str, timeout: int) -> Tuple[str, str]:
    """通过 Chrome DevTools Protocol (CDP) 获取渲染后的页面内容。

    连接已开启远程调试的 Chrome/Edge (http://{CDP_HOST}:{CDP_PORT})，
    在用户可见的浏览器中打开页面，用户可手动处理验证码。
    """
    import asyncio
    import websockets
    import json

    ws_url = None
    try:
        import urllib.request
        resp = urllib.request.urlopen(f"http://{CDP_HOST}:{CDP_PORT}/json/version", timeout=5)
        info = json.loads(resp.read().decode())
        ws_url = info.get("webSocketDebuggerUrl")
        if not ws_url:
            return "", "[CDP] 无法获取 WebSocket URL"
    except Exception as e:
        return "", f"[CDP] 连接失败: {e}"

    async def _impl():
        async with websockets.connect(ws_url, max_size=None, ping_timeout=None) as ws:
            # 发送请求 ID
            msg_id = 1

            async def send(cmd, params=None):
                nonlocal msg_id
                req = {"id": msg_id, "method": cmd}
                if params:
                    req["params"] = params
                msg_id += 1
                await ws.send(json.dumps(req))
                resp = await asyncio.wait_for(ws.recv(), timeout=timeout + 5)
                return json.loads(resp)

            # 获取目标列表
            targets_resp = await send("Target.getTargets")
            targets = targets_resp.get("result", {}).get("targetInfos", [])
            tab_id = None
            for t in targets:
                if t.get("type") == "page":
                    tab_id = t["targetId"]
                    break

            if not tab_id:
                # 创建新标签页
                new_resp = await send("Target.createTarget", {
                    "url": "about:blank",
                    "newWindow": False,
                })
                tab_id = new_resp.get("result", {}).get("targetId")

            if not tab_id:
                return "", "[CDP] 无法获取或创建标签页"

            # 连接到标签页
            attach_resp = await send("Target.attachToTarget", {
                "targetId": tab_id,
                "flatten": True,
            })
            session_id = attach_resp.get("result", {}).get("sessionId")
            if not session_id:
                return "", "[CDP] 无法附加到标签页"

            async def send_session(method, params=None):
                nonlocal msg_id
                req = {"id": msg_id, "sessionId": session_id, "method": method}
                if params:
                    req["params"] = params
                msg_id += 1
                await ws.send(json.dumps(req))
                resp = await asyncio.wait_for(ws.recv(), timeout=timeout + 5)
                return json.loads(resp)

            # 导航
            nav_resp = await send_session("Page.navigate", {"url": url})
            # 等待页面加载：轮询 document.readyState 直到 complete。
            # 注意 Page.loadEventFired 是事件而非命令（需 Page.enable + 事件循环），
            # 直接调用会返回错误，这里改用更简单的轮询方式。
            try:
                async def _wait_page_ready():
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        state_resp = await send_session("Runtime.evaluate", {
                            "expression": "document.readyState",
                            "returnByValue": True,
                        })
                        try:
                            state = state_resp["result"]["result"]["value"]
                        except (KeyError, TypeError):
                            state = ""
                        if state == "complete":
                            return True
                        await asyncio.sleep(0.5)
                    return False

                await asyncio.wait_for(_wait_page_ready(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

            await asyncio.sleep(2)  # 额外等待 JS 渲染

            # 获取标题
            title_resp = await send_session("Runtime.evaluate", {
                "expression": "document.title",
                "returnByValue": True,
            })
            title = ""
            try:
                title = title_resp["result"]["result"]["value"]
            except (KeyError, TypeError):
                title = ""

            # 获取页面文本
            text_resp = await send_session("Runtime.evaluate", {
                "expression": r"""
                    (() => {
                        const el = document.querySelector('article') || document.querySelector('main') || document.body;
                        return el.innerText;
                    })()
                """,
                "returnByValue": True,
            })
            body_text = ""
            try:
                body_text = text_resp["result"]["result"]["value"]
            except (KeyError, TypeError):
                body_text = ""

            # 关闭标签页
            try:
                await send_session("Page.close")
            except Exception:
                pass

            _lines = [l.strip() for l in body_text.split("\n") if l.strip()]
            clean_text = "\n".join(_lines)
            return title or "", clean_text

    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(_impl())
        loop.close()
        return result
    except Exception as e:
        return "", f"[CDP 渲染失败] {e}"


def cdp_available(timeout: float = 3.0) -> bool:
    """探测本机是否有开启远程调试的 Chrome/Edge（CDP 端口可连）。"""
    import urllib.request

    try:
        resp = urllib.request.urlopen(
            f"http://{CDP_HOST}:{CDP_PORT}/json/version", timeout=timeout
        )
        return resp.status == 200
    except Exception:
        return False


def cdp_eval_new_tab(
    url: str,
    expression: str,
    timeout: int = None,
    ready_expression: Optional[str] = None,
    ready_polls: int = 40,
    poll_interval: float = 0.5,
) -> Tuple[str, str]:
    """在已登录的浏览器里新开标签页执行 JS，返回 (结果字符串, 错误说明)。

    与 _fetch_via_cdp 的区别：
    - 用 Target.createTarget 直接新开标签页（不劫持用户当前正在看的标签页），
      执行完用 Target.closeTarget 关闭；
    - 可选 ready_expression：轮询该表达式，返回真值即认为渲染完成
      （例如 'document.querySelectorAll(\\'article\\').length'），用于 SPA 页面；
    - expression 的返回值原样带出（约定返回字符串，如 JSON.stringify 的结果）。

    本函数只连本机浏览器，不直接访问网络；供「复用用户浏览器登录态」的抓取使用。
    """
    import asyncio
    import json
    import websockets
    import urllib.request

    if timeout is None:
        timeout = BROWSER_TIMEOUT

    try:
        resp = urllib.request.urlopen(f"http://{CDP_HOST}:{CDP_PORT}/json/version", timeout=5)
        info = json.loads(resp.read().decode())
        ws_url = info.get("webSocketDebuggerUrl")
        if not ws_url:
            return "", "[CDP] 无法获取 WebSocket URL"
    except Exception as e:
        return "", f"[CDP] 连接失败: {e}（浏览器需以 --remote-debugging-port={CDP_PORT} 启动）"

    async def _impl():
        async with websockets.connect(ws_url, max_size=None, ping_timeout=None) as ws:
            msg_id = 0
            pending: Dict[int, dict] = {}

            async def call(method, params=None, session_id=None):
                """发送命令并等回执：跳过事件消息，只认 id 匹配的那条。"""
                nonlocal msg_id
                msg_id += 1
                req = {"id": msg_id, "method": method}
                if params:
                    req["params"] = params
                if session_id:
                    req["sessionId"] = session_id
                await ws.send(json.dumps(req))
                deadline = time.monotonic() + timeout + 10
                while time.monotonic() < deadline:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout + 10)
                    msg = json.loads(raw)
                    if msg.get("id") == msg_id:
                        return msg
                    if "id" in msg:
                        pending[msg["id"]] = msg  # 其它并发命令的回执，暂存（本函数内不会用到）
                return {}

            # 新开标签页（直接把目标 URL 交给浏览器，避免先导航再等待的两段式）
            created = await call("Target.createTarget", {"url": url, "newWindow": False})
            tab_id = (created.get("result") or {}).get("targetId")
            if not tab_id:
                return "", "[CDP] 无法创建标签页"

            try:
                attached = await call("Target.attachToTarget", {"targetId": tab_id, "flatten": True})
                session_id = (attached.get("result") or {}).get("sessionId")
                if not session_id:
                    return "", "[CDP] 无法附加到标签页"

                async def evaluate(expr):
                    res = await call(
                        "Runtime.evaluate",
                        {"expression": expr, "returnByValue": True, "awaitPromise": True},
                        session_id=session_id,
                    )
                    try:
                        return res["result"]["result"].get("value")
                    except (KeyError, TypeError):
                        return None

                # 等 SPA 渲染：轮询 ready_expression，真值即完成
                if ready_expression:
                    for _ in range(max(1, ready_polls)):
                        try:
                            if await evaluate(ready_expression):
                                break
                        except asyncio.TimeoutError:
                            break
                        await asyncio.sleep(poll_interval)

                value = await evaluate(expression)
                try:
                    value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
                except Exception:
                    value = str(value)
                return (value or ""), ""
            finally:
                try:
                    await call("Target.closeTarget", {"targetId": tab_id})
                except Exception:
                    pass

    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_impl())
        finally:
            loop.close()
    except Exception as e:
        return "", f"[CDP 执行失败] {e}"


def _playwright_sync_impl(url: str, timeout: int) -> Tuple[str, str]:
    """Playwright 回退实现，在独立线程中运行。"""
    from src.config import PLAYWRIGHT_HEADLESS
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser_instance = p.chromium.launch(
            headless=PLAYWRIGHT_HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
                "--no-sandbox",
            ],
        )
        try:
            context = browser_instance.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
            )
            page = context.new_page()
            page.set_default_timeout(timeout * 1000)

            try:
                page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
            except Exception:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                except Exception:
                    pass

            page.wait_for_timeout(2000)

            title = page.title()
            try:
                body_el = page.query_selector("article") or page.query_selector("main") or page.query_selector("body")
                body_text = body_el.inner_text() if body_el else page.inner_text("body")
            except Exception:
                body_text = page.inner_text("body")

            _lines = [l.strip() for l in body_text.split("\n") if l.strip()]
            clean_text = "\n".join(_lines)
            return title, clean_text
        finally:
            # 必须在 sync_playwright() 的 with 块内关闭浏览器，
            # 退出 with 后再 close 无效（driver 已停止）
            try:
                browser_instance.close()
            except Exception:
                pass


def fetch_with_playwright(url: str, timeout: int = None) -> Tuple[str, str]:
    """浏览器渲染获取页面，优先使用 CDP 连接已有 Chrome，失败则回退到 Playwright。

    在独立线程中运行以避免 FastMCP asyncio 事件循环冲突。
    """
    if timeout is None:
        timeout = BROWSER_TIMEOUT

    import concurrent.futures

    # 优先尝试 CDP（连接用户已有的 Chrome/Edge，用户可见页面）
    cdp_title, cdp_text = _fetch_via_cdp(url, timeout)
    if cdp_text and not cdp_text.startswith("[CDP"):
        return cdp_title, cdp_text

    # CDP 不可用时回退到 Playwright
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_playwright_sync_impl, url, timeout)
            return future.result(timeout=timeout + 10)
    except concurrent.futures.TimeoutError:
        return "", "[浏览器渲染失败] 页面加载超时"
    except Exception as e:
        return "", f"[浏览器渲染失败] {e}"


