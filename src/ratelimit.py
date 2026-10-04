# -*- coding: utf-8 -*-
"""
滑动窗口率限制器，防止请求过于频繁被目标服务封禁。
"""

import time
import threading
from collections import deque
from typing import Dict, Tuple


class RateLimiter:
    """
    滑动窗口率限制器。

    用法:
        limiter = RateLimiter(max_requests=30, window_seconds=60)
        if limiter.allow("search"):
            # 执行搜索
            pass
    """

    def __init__(self, max_requests: int = 30, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._buckets: Dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str = "default") -> bool:
        """
        检查当前请求是否允许通过。
        如果允许，记录该请求并返回 True；否则返回 False。
        """
        now = time.time()
        with self._lock:
            if key not in self._buckets:
                self._buckets[key] = deque()

            bucket = self._buckets[key]

            # 移除窗口外的时间戳
            while bucket and now - bucket[0] > self.window_seconds:
                bucket.popleft()

            if len(bucket) < self.max_requests:
                bucket.append(now)
                return True
            return False

    def wait_time(self, key: str = "default") -> float:
        """
        返回需要等待多少秒才能进行下一次请求。
        如果没有限制则返回 0。
        """
        now = time.time()
        with self._lock:
            bucket = self._buckets.get(key)
            if not bucket or len(bucket) < self.max_requests:
                return 0.0

            # 窗口中最旧的时间戳
            oldest = bucket[0]
            return max(0.0, oldest + self.window_seconds - now)

    def reset(self, key: str = "default") -> None:
        """重置指定键的率限状态。"""
        with self._lock:
            self._buckets.pop(key, None)

    def remaining(self, key: str = "default") -> int:
        """返回当前窗口内剩余的请求配额。"""
        now = time.time()
        with self._lock:
            bucket = self._buckets.get(key)
            if not bucket:
                return self.max_requests
            while bucket and now - bucket[0] > self.window_seconds:
                bucket.popleft()
            return self.max_requests - len(bucket)


# ── 预配置的全局率限制器 ─────────────────────────────────────
#
# 阈值按“一次调研会话”的峰值留足余量：配额是在引擎调用前扣的，缓存命中也会
# 扣一次（键在引擎内部，上层看不到是否命中）。阈值定得过低会让同一查询重复
# 调用时被静默降级成“部分引擎不可用”，表现为同一问题两次结果不一样。
# 因此这里取足够宽松的上限，只拦真正失控的调用风暴。

# 搜索请求：60次/分钟
search_limiter = RateLimiter(max_requests=60, window_seconds=60)

# 网页抓取：40次/分钟（一次 web_search_with_content 最多抓 5 页）
fetch_limiter = RateLimiter(max_requests=40, window_seconds=60)

# 单引擎子限流（防止单个引擎过载）
engine_limiters: Dict[str, RateLimiter] = {
    "duckduckgo": RateLimiter(max_requests=30, window_seconds=60),
    "bing": RateLimiter(max_requests=30, window_seconds=60),
    "google": RateLimiter(max_requests=15, window_seconds=60),
}
