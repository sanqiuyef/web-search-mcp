# -*- coding: utf-8 -*-
"""Twitter(X) 站点搜索：关键词 → 近期推文（作者、时间、正文、互动数、链接）。

为什么必须借登录态：X 没有免费公开搜索 API；未登录访问 x.com 是空白页
（2026-10-04 实测），公共 Nitter 镜像当天也全部不可用（Cloudflare / Anubis / 429）。
按本项目的回退原则，遇到反爬/登录墙应交给 **Playwright MCP 驱动用户已登录的 Edge**
完成任务；本模块是「静默、不弹浏览器」的备选，两个后端按可用性顺序回退：

1. cookie 会话（静默，推荐）：配置 X_AUTH_TOKEN / X_CT0 后直接调 x.com 网页端的
   内部接口（GraphQL SearchTimeline）。queryId 自动从前端 bundle 发现，
   features 被 X 点名缺失时自动补齐并落盘缓存 —— X 小改版无需动代码。
2. CDP 复用已登录浏览器（零密钥）：Chrome/Edge 以 --remote-debugging-port=9222
   启动且已登录 X，工具会新开一个标签页搜索、抽取 DOM、再关掉该标签页，
   全程用户可见可干预。

两条路都不可用时返回「怎么开通」的指引，而不是空结果。

凭证纪律：auth_token / ct0 只从环境变量读取，本模块不写入任何文件、不打印其值；
磁盘缓存只存 queryId / features（非凭据）。
"""

import json
import os
import re
import time
from email.utils import parsedate_to_datetime
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote

from src.cache import make_cache_key, cache_get, cache_set
from src.config import (
    X_AUTH_TOKEN,
    X_CT0,
    X_SEARCH_QUERY_ID,
    X_CACHE_FILE,
    CDP_PORT,
)
from src.utils import (
    make_tool_result,
    make_error_result,
    cdp_available,
    cdp_eval_new_tab,
)

# X 网页端公开的 OAuth2 bearer：前端 bundle 里人人可见的固定常量，不是账号凭据。
_WEB_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
_API = "https://x.com/i/api/graphql"
_OPERATION = "SearchTimeline"

_CACHE_PATH = X_CACHE_FILE or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".cache",
    "x_api.json",
)
_CACHE_TTL = 24 * 3600  # queryId / features 的发现结果复用 24h

_CACHE: Optional[Dict] = None


# ═══════════════════════════════════════════════════════════
# 发现结果缓存（queryId / features，非凭据）
# ═══════════════════════════════════════════════════════════


def _load_cache() -> Dict:
    global _CACHE
    if _CACHE is not None and (time.time() - float(_CACHE.get("ts", 0))) < _CACHE_TTL:
        return _CACHE
    try:
        with open(_CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    _CACHE = data
    return data


def _save_cache(**fields) -> None:
    global _CACHE
    data = dict(_CACHE or {})
    data.update(fields)
    data["ts"] = time.time()
    _CACHE = data
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
# 后端一：cookie 会话 → GraphQL SearchTimeline
# ═══════════════════════════════════════════════════════════


def _x_session():
    """带 cookie 会话的 HTTP 客户端。

    用 curl_cffi 模拟 Chrome 的 TLS 指纹，配合 x-csrf-token 头（= ct0），
    这是 X 网页端自身的调用形态，比裸 requests 更不容易被风控。
    """
    from curl_cffi import requests as cffi_requests

    session = cffi_requests.Session(impersonate="chrome")
    session.headers.update({
        "authorization": f"Bearer {_WEB_BEARER}",
        "x-csrf-token": X_CT0,
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "zh-cn",
        "referer": "https://x.com/",
        "origin": "https://x.com",
        "cookie": f"auth_token={X_AUTH_TOKEN}; ct0={X_CT0}",
    })
    return session


def _query_id_from_js(js: str) -> Optional[str]:
    """在（可能被压缩的）前端 JS 里找 SearchTimeline 的 queryId。"""
    if not js:
        return None
    for pat in (
        r'queryId:"([A-Za-z0-9_-]{10,})"[^{}]{0,200}?operationName:"%s"' % _OPERATION,
        r'operationName:"%s"[^{}]{0,200}?queryId:"([A-Za-z0-9_-]{10,})"' % _OPERATION,
    ):
        m = re.search(pat, js)
        if m:
            return m.group(1)
    return None


def _discover_query_id(session) -> Optional[str]:
    """从前端 bundle 里发现 SearchTimeline 的 queryId（X 改版后自愈的关键）。"""
    try:
        home = session.get("https://x.com/", timeout=20)
    except Exception:
        return None
    html = home.text or ""
    if home.status_code in (401, 403):
        return None
    scripts = re.findall(
        r"https://abs\.twimg\.com/responsive-web/client-web/[^\"'\s>]+\.js", html
    )
    for src in dict.fromkeys(scripts):  # bundle 可达数 MB，扫到即止
        try:
            js = session.get(src, timeout=30).text or ""
        except Exception:
            continue
        qid = _query_id_from_js(js)
        if qid:
            return qid
    return _query_id_from_js(html)


# X 的 400 报错会点名缺失的 feature（"...features cannot be null: a, b"），
# 按名字补成 false 再试一次即可自愈，无需跟着版本改常量。
_NULL_FEATURES_RE = re.compile(
    r"features?\s+(?:cannot be null|are missing|is missing)[:：]?\s*([^。\n\"]+)", re.I
)


def _heal_features(features: Dict, error_text: str) -> Optional[Dict]:
    m = _NULL_FEATURES_RE.search(error_text or "")
    if not m:
        return None
    names = [
        n for n in re.split(r"[,\s]+", m.group(1))
        if re.fullmatch(r"[a-z][A-Za-z0-9_]{3,}", n) and n.lower() != "please"
    ][:40]
    if not names:
        return None
    healed = dict(features)
    for n in names:
        healed.setdefault(n, False)
    return healed if healed != features else None


def _brief(text: str, limit: int = 220) -> str:
    """压缩错误响应，尽量只留 message 字段的内容。"""
    text = (text or "").strip()
    try:
        data = json.loads(text)
        errs = data.get("errors") or []
        msgs = [str(e.get("message") or "") for e in errs if isinstance(e, dict)]
        if msgs:
            text = "; ".join(m for m in msgs if m) or text
    except Exception:
        pass
    return re.sub(r"\s+", " ", text)[:limit]


def _graphql_search(keyword: str, max_results: int, mode: str) -> Tuple[List[Dict], str, str]:
    """cookie 后端：返回 (推文列表, 错误说明, 修复提示)。

    推文列表为空且错误为空 = 接口正常但确实没有结果。
    """
    session = _x_session()
    cache = _load_cache()
    qid = X_SEARCH_QUERY_ID or cache.get("query_id")
    if not qid:
        qid = _discover_query_id(session)
        if qid:
            _save_cache(query_id=qid)
    if not qid:
        return None, "未能发现 SearchTimeline 的 queryId（X 前端改版，或 cookie 会话无效导致首页取不到 bundle）", \
            "可临时手工设置 X_SEARCH_QUERY_ID"

    features = dict(cache.get("features") or {})
    variables = {
        "rawQuery": keyword,
        "count": max(1, min(max_results, 20)),
        "querySource": "typed_query",
        "product": "Top" if mode == "top" else "Latest",
    }

    last_err = ""
    for _ in range(3):
        params = {
            "variables": json.dumps(variables, ensure_ascii=False),
            "features": json.dumps(features, ensure_ascii=False),
        }
        try:
            resp = session.get(f"{_API}/{qid}/{_OPERATION}", params=params, timeout=30)
        except Exception as e:
            return None, f"请求 X 接口失败: {e}", ""

        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                return None, "X 返回了非 JSON 响应（可能被风控页拦截）", ""
            tweets = _extract_tweets(data)
            if tweets:
                return tweets, "", ""
            errs = "; ".join(
                str(e.get("message") or "")
                for e in (data.get("errors") or [])
                if isinstance(e, dict)
            )
            return [], (f"X 接口未返回推文: {_brief(errs)}" if errs else ""), ""

        body = resp.text or ""
        if resp.status_code in (401, 403):
            return None, "cookie 会话无效或已过期（auth_token / ct0 被 X 拒绝）", \
                "请重新登录 x.com 后复制这两个 cookie 再试"
        if resp.status_code == 404:
            new_qid = _discover_query_id(_x_session())
            if new_qid and new_qid != qid:
                qid = new_qid
                _save_cache(query_id=qid)
                continue
            return None, "SearchTimeline 接口 404（queryId 失效且重新发现失败）", \
                "可手工设置 X_SEARCH_QUERY_ID"
        if resp.status_code == 400:
            healed = _heal_features(features, body)
            if healed:
                features = healed
                _save_cache(features=features)
                continue
            return None, f"X 接口返回 400: {_brief(body)}", ""
        if resp.status_code == 429:
            return None, "X 接口限流（429），请降低调用频率后重试", ""
        last_err = f"X 接口返回 HTTP {resp.status_code}: {_brief(body)}"

    return None, last_err or "X 接口调用失败", ""


# ═══════════════════════════════════════════════════════════
# 后端二：CDP 复用已登录浏览器
# ═══════════════════════════════════════════════════════════

# 就绪条件：出现推文卡片，或出现登录墙（早停，避免白等到超时）。
_CDP_READY_EXPR = (
    '(document.querySelectorAll(\'article[data-testid="tweet"]\').length > 0'
    ' || !!document.querySelector(\'input[autocomplete="username"],'
    '[data-testid="loginButton"]\')) ? 1 : 0'
)

_CDP_EXTRACT_JS = r"""
(async () => {
  const NEED = __NEED__;
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const count = () => document.querySelectorAll('article[data-testid="tweet"]').length;
  let last = 0;
  for (let i = 0; i < 3 && count() < NEED; i++) {
    if (i > 0 && count() === last) break;   // 滚不动了就别浪费时间
    last = count();
    window.scrollBy(0, window.innerHeight * 2);
    await wait(1500);
  }
  const out = [];
  const seen = new Set();
  document.querySelectorAll('article[data-testid="tweet"]').forEach(a => {
    const link = a.querySelector('a[href*="/status/"]');
    if (!link) return;
    const href = link.getAttribute('href') || '';
    const m = href.match(/^\/([A-Za-z0-9_]+)\/status\/(\d+)/);
    if (!m || seen.has(m[2])) return;
    seen.add(m[2]);
    const textEl = a.querySelector('div[data-testid="tweetText"]');
    const nameEl = a.querySelector('div[data-testid="User-Name"]');
    const timeEl = a.querySelector('time');
    const group = a.querySelector('div[role="group"]');
    out.push({
      handle: m[1],
      id: m[2],
      url: 'https://x.com' + href.split('?')[0],
      text: textEl ? textEl.innerText : '',
      name: nameEl ? (nameEl.innerText || '').split('\n')[0] : '',
      time: timeEl ? (timeEl.getAttribute('datetime') || '') : '',
      stats: group ? (group.getAttribute('aria-label') || '') : ''
    });
  });
  const loginWall = !!document.querySelector('input[autocomplete="username"], [data-testid="loginButton"]');
  const bodyLen = (document.body && document.body.innerText) ? document.body.innerText.length : 0;
  return JSON.stringify({url: location.href, loginWall: loginWall, bodyLen: bodyLen, tweets: out});
})()
"""


def _search_via_cdp(keyword: str, max_results: int, mode: str) -> Tuple[List[Dict], str]:
    """浏览器后端：新开标签页跑一次 X 搜索页，抽取推文后关闭。"""
    search_url = (
        f"https://x.com/search?q={quote(keyword)}&src=typed_query"
        + ("&f=live" if mode != "top" else "")
    )
    js = _CDP_EXTRACT_JS.replace("__NEED__", str(min(max_results, 20)))
    raw, err = cdp_eval_new_tab(
        search_url, js, timeout=40, ready_expression=_CDP_READY_EXPR, ready_polls=30
    )
    if err:
        return [], err
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return [], "浏览器后端返回了无法解析的结果（页面结构可能已改版）"
    if data.get("loginWall") and not data.get("tweets"):
        return [], (
            "该浏览器未登录 X（或登录态已过期）：请先在这个 Chrome/Edge 里登录 x.com 再重试"
        )
    if not data.get("tweets") and int(data.get("bodyLen") or 0) < 200:
        return [], (
            "搜索页没有渲染出内容（页面近乎空白）：通常是未登录被 X 拦截，"
            "或该浏览器对 x.com 被风控。请确认同一浏览器里能正常打开 x.com 搜索结果页"
        )
    return _normalize_cdp_tweets(data.get("tweets") or []), ""


def _normalize_cdp_tweets(raw_tweets: List[Dict]) -> List[Dict]:
    items = []
    for t in raw_tweets:
        if not isinstance(t, dict) or not t.get("id"):
            continue
        aria = t.get("stats") or ""
        items.append({
            "id": str(t.get("id")),
            "handle": t.get("handle") or "",
            "name": t.get("name") or "",
            "url": t.get("url") or "",
            "text": _clean(t.get("text") or ""),
            "time": _fmt_iso_time(t.get("time") or ""),
            "extra": _stats_line(
                time=_fmt_iso_time(t.get("time") or ""),
                raw_stats=aria,
                **_parse_aria_counts(aria),
            ),
        })
    return items


# X 点赞/转推等计数在 role="group" 的 aria-label 里，文案随界面语言变化
# （英文 "12 replies, 34 reposts..."；中文 "12 条回复、34 次转帖..."），
# 因此按关键词子串匹配，而不是按固定英文单词。
_ARIA_COUNT_RE = re.compile(r"([\d.,]+[KkMm]?)\s*([^\d\s]{1,12})")

_COUNT_KEYWORDS = (
    ("replies", ("repl", "comment", "回复", "回覆", "评论", "評論")),
    ("rts", ("repost", "retweet", "转帖", "轉帖", "转发", "轉發", "转推", "轉推")),
    ("likes", ("like", "喜欢", "喜歡", "赞", "讚")),
    ("views", ("view", "查看", "浏览", "瀏覽", "观看", "觀看")),
)


def _parse_aria_counts(aria: str) -> Dict[str, Optional[int]]:
    """从 aria-label 里解析互动数；解析不出的项保持 None（由调用方兜底展示原文）。"""
    counts: Dict[str, Optional[int]] = {"replies": None, "rts": None, "likes": None, "views": None}
    for num, word in _ARIA_COUNT_RE.findall(aria or ""):
        w = word.lower().strip(".,;:、，。·")
        for key, prefixes in _COUNT_KEYWORDS:
            if counts[key] is None and any(p in w for p in prefixes):
                counts[key] = _parse_count(num)
                break
    return counts


# ═══════════════════════════════════════════════════════════
# 解析与格式化（GraphQL 与 CDP 两个后端共用）
# ═══════════════════════════════════════════════════════════


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\u200b", "")).strip()


def _parse_count(value) -> Optional[int]:
    """X 的计数可能是 "1,234" / "12.3K" / "1.2M" 或已经是数字。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().replace(",", "").replace("\u00a0", " ")
    m = re.match(r"^([\d.]+)\s*([KkMm]?)$", s)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2).lower()
    if unit == "k":
        num *= 1_000
    elif unit == "m":
        num *= 1_000_000
    return int(num)


def _fmt_count(value) -> str:
    n = _parse_count(value)
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.1f}K"
    return f"{n:,}"


def _fmt_time(raw) -> str:
    """GraphQL 的 "Wed Oct 04 12:00:00 +0000 2026" → "2026-10-04 12:00 UTC"。"""
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(str(raw)).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(raw)


def _fmt_iso_time(raw) -> str:
    """"2026-10-04T12:00:00.000Z" → "2026-10-04 12:00 UTC"。"""
    if not raw:
        return ""
    try:
        ts = str(raw).replace("Z", "+00:00")
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(raw)


def _stats_line(replies=None, rts=None, likes=None, views=None, time: str = "", raw_stats: str = "") -> str:
    parts = []
    if time:
        parts.append(f"🕒 {time}")
    if replies is not None:
        parts.append(f"💬 {_fmt_count(replies)}")
    if rts is not None:
        parts.append(f"🔁 {_fmt_count(rts)}")
    if likes is not None:
        parts.append(f"❤ {_fmt_count(likes)}")
    if views is not None:
        parts.append(f"👁 {_fmt_count(views)}")
    if not parts and raw_stats:
        parts.append(raw_stats)
    return " | ".join(parts)


def _iter_tweet_results(node) -> Iterator[Dict]:
    """递归找出响应里所有 tweet_results.result 节点（容忍 X 调整字段层级）。"""
    if isinstance(node, dict):
        tr = node.get("tweet_results")
        if isinstance(tr, dict) and isinstance(tr.get("result"), dict):
            yield tr["result"]
        for value in node.values():
            yield from _iter_tweet_results(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_tweet_results(value)


def _extract_tweets(data: Dict) -> List[Dict]:
    tweets: List[Dict] = []
    seen = set()
    for res in _iter_tweet_results(data):
        if res.get("__typename") == "TweetWithVisibilityResults":
            res = res.get("tweet") or {}
        if not isinstance(res, dict) or not res:
            continue
        item = _normalize_graphql_tweet(res)
        if item and item["id"] not in seen:
            seen.add(item["id"])
            tweets.append(item)
    return tweets


def _normalize_graphql_tweet(res: Dict) -> Optional[Dict]:
    legacy = res.get("legacy") or {}
    rest_id = str(res.get("rest_id") or legacy.get("id_str") or "")
    if not rest_id:
        return None
    user = (((res.get("core") or {}).get("user_results") or {}).get("result") or {})
    u_core = user.get("core") or {}
    u_legacy = user.get("legacy") or {}
    handle = u_core.get("screen_name") or u_legacy.get("screen_name") or ""
    name = u_core.get("name") or u_legacy.get("name") or ""
    note_text = (
        ((res.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
    ).get("text")
    text = note_text or legacy.get("full_text") or ""
    views = (res.get("views") or {}).get("count")
    if views is None and isinstance(legacy.get("views"), dict):
        views = legacy["views"].get("count")
    return {
        "id": rest_id,
        "handle": handle,
        "name": name,
        "url": (
            f"https://x.com/{handle}/status/{rest_id}" if handle
            else f"https://x.com/i/status/{rest_id}"
        ),
        "text": _clean(text),
        "time": _fmt_time(legacy.get("created_at")),
        "extra": _stats_line(
            replies=legacy.get("reply_count"),
            rts=legacy.get("retweet_count"),
            likes=legacy.get("favorite_count"),
            views=views,
        ),
    }


def _format_result(keyword: str, tweets: List[Dict], mode: str,
                   notes: Optional[List[str]] = None, cached: bool = False) -> Dict:
    mode_label = "热门" if mode == "top" else "最新"
    if not tweets:
        text = f"❌ Twitter(X) 未找到与「{keyword}」相关的推文（{mode_label}）。\n  提示：X 搜索只覆盖其索引范围，可换关键词或改用 mode=top。"
    else:
        lines = [f"🔎 **Twitter(X) 搜索结果**「{keyword}」（{len(tweets)} 条，{mode_label}）", ""]
        for i, item in enumerate(tweets, 1):
            who = f"@{item['handle']}" if item.get("handle") else "?"
            if item.get("name"):
                who += f" · {item['name']}"
            lines.append(f"{i}. **{who}** — {item['url']}")
            if item.get("extra"):
                lines.append(f"   {item['extra']}")
            if item.get("text"):
                text_body = item["text"]
                lines.append(f"   {text_body[:500]}{'…' if len(text_body) > 500 else ''}")
            lines.append("")
        text = "\n".join(lines).rstrip()
    if notes:
        text += "\n\n⚠️ " + "；".join(notes)
    meta = {"site": "Twitter/X", "keyword": keyword, "mode": mode, "total": len(tweets)}
    if cached:
        meta["cached"] = True
    return make_tool_result(text, results=tweets, meta=meta)


_NO_SETUP_GUIDE = (
    "Twitter(X) 搜索需要登录态：未登录直读 x.com 是空白页，公共 Nitter 镜像 "
    "2026-10-04 实测全部失效。三条通路任选一种：\n"
    "  ① 遇到反爬/登录墙，回退用 Playwright MCP（browser_* 工具）驱动已登录的 Edge\n"
    "     完成任务 —— 本机 X 登录态就在 Edge 里，零配置直接可用（推荐）。\n"
    "  ② cookie 会话（静默、不弹浏览器）：在登录了 x.com 的浏览器里复制 auth_token、ct0，\n"
    "     配到本 MCP 的 env（X_AUTH_TOKEN / X_CT0）后重启 ZCode。\n"
    f"  ③ 调试端口浏览器（进阶）：Edge/Chrome ≥136 禁止用默认配置目录开\n"
    f"     --remote-debugging-port={CDP_PORT}，需用独立 --user-data-dir 启动并在其中\n"
    "     登录一次 X，本工具的浏览器后端才能复用（实测复制配置不带走 X 登录）。\n"
    "  配置说明见 web-search-server/README.md（密钥只放本机 env，不要入仓库）。"
)


# ═══════════════════════════════════════════════════════════
# 对外入口
# ═══════════════════════════════════════════════════════════


def twitter_search(keyword: str, max_results: Optional[int] = 10,
                   mode: Optional[str] = "latest") -> dict:
    """按关键词搜索 X(Twitter) 推文，返回作者、时间、正文、互动数与链接。

    需要使用者自己的 X 登录态：优先 cookie 会话（X_AUTH_TOKEN / X_CT0），
    否则回退到已开启远程调试（9222）的浏览器。读取某人推文可用 X 搜索语法，
    如 "from:elonmusk audio" 或 "@elonmusk audio"。
    """
    if not keyword or not keyword.strip():
        return make_error_result("keyword 不能为空。")
    keyword = keyword.strip()
    max_results = min(max(1, max_results or 10), 20)
    mode = (mode or "latest").strip().lower()
    if mode not in ("latest", "top"):
        return make_error_result("mode 仅支持 latest（最新）或 top（热门）。")

    cache_key = make_cache_key("twitter", f"{mode}|{keyword}", max_results, "site")
    cached = cache_get(cache_key)
    if cached is not None:
        return _format_result(keyword, cached, mode, cached=True)

    cookies_ready = bool(X_AUTH_TOKEN and X_CT0)
    browser_ready = cdp_available()
    notes: List[str] = []

    if not cookies_ready and not browser_ready:
        return make_error_result(_NO_SETUP_GUIDE)

    tweets: List[Dict] = []

    if cookies_ready:
        tweets, err, hint = _graphql_search(keyword, max_results, mode)
        if err:
            notes.append(f"cookie 会话失败：{err}" + (f"（{hint}）" if hint else ""))
        tweets = tweets or []

    if not tweets and browser_ready:
        cdp_tweets, cdp_err = _search_via_cdp(keyword, max_results, mode)
        if cdp_err:
            notes.append(f"浏览器后端失败：{cdp_err}")
        tweets = cdp_tweets or []

    if not tweets and notes and not cookies_ready:
        # 只用浏览器后端且失败：给出开通第二种方式的指引，避免用户卡住
        notes.append("如需更稳定的静默后端，可配置 X_AUTH_TOKEN / X_CT0（见 README）")

    if tweets:
        cache_set(cache_key, tweets)
    return _format_result(keyword, tweets[:max_results], mode, notes=notes or None)
