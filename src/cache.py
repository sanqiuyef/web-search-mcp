# -*- coding: utf-8 -*-
"""
内存缓存系统，带 TTL 过期和最大条目限制。
键格式: "{engine}|{category}|{keyword}|{max_results}"
"""

import json
import time
import threading
from typing import Dict, List, Optional, Tuple

from src.config import CACHE_ENABLED, CACHE_TTL

# 缓存最大条目数（防止内存无限增长）
_MAX_CACHE_SIZE = 1000

# {(engine, category, keyword, max_results): (timestamp, results)}
_search_cache: Dict[str, Tuple[float, List[Dict]]] = {}
_cache_lock = threading.Lock()


def make_cache_key(engine: str, keyword: str, max_results: int, category: str = "general") -> str:
    """生成缓存键。使用 JSON 序列化，避免 keyword 含 | 等分隔符导致键冲突。"""
    return json.dumps([engine, category, keyword, max_results], ensure_ascii=False)


def cache_get(key: str) -> Optional[List[Dict]]:
    if not CACHE_ENABLED:
        return None
    with _cache_lock:
        entry = _search_cache.get(key)
        if entry and (time.time() - entry[0]) < CACHE_TTL:
            return entry[1]
        if entry:
            del _search_cache[key]
    return None


def cache_set(key: str, results: List[Dict]) -> None:
    if not CACHE_ENABLED:
        return
    with _cache_lock:
        # 达到上限时清空最旧的 20% 条目
        if len(_search_cache) >= _MAX_CACHE_SIZE:
            _evict_oldest_locked(count=max(1, _MAX_CACHE_SIZE // 5))
        _search_cache[key] = (time.time(), results)


def _evict_oldest_locked(count: int = 1) -> None:
    """在持有锁的情况下驱逐最旧的 count 个条目。"""
    if not _search_cache:
        return
    items = sorted(_search_cache.items(), key=lambda x: x[1][0])
    for key, _ in items[:count]:
        _search_cache.pop(key, None)


def cache_clear() -> int:
    """清空所有缓存，返回清除的条目数。"""
    with _cache_lock:
        count = len(_search_cache)
        _search_cache.clear()
        return count


def cache_stats() -> Dict:
    """返回缓存统计信息。"""
    now = time.time()
    with _cache_lock:
        total = len(_search_cache)
        valid = sum(1 for v in _search_cache.values() if (now - v[0]) < CACHE_TTL)
        expired = total - valid
        return {
            "total": total,
            "valid": valid,
            "expired": expired,
            "ttl_seconds": CACHE_TTL,
            "enabled": CACHE_ENABLED,
            "max_size": _MAX_CACHE_SIZE,
        }
