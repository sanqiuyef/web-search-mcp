# -*- coding: utf-8 -*-
"""
指数退避重试工具，用于处理临时性网络故障。
"""

import time
import random
from functools import wraps
from typing import Callable, Optional, Type, Tuple


def retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    backoff: float = 2.0,
    jitter: bool = True,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
) -> Callable:
    """
    指数退避重试装饰器。

    Args:
        max_attempts: 最大重试次数（包括首次）
        base_delay: 初始延迟（秒）
        max_delay: 最大延迟（秒）
        backoff: 退避乘数
        jitter: 是否添加随机抖动
        exceptions: 需要重试的异常类型
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts:
                        delay = min(base_delay * (backoff ** (attempt - 1)), max_delay)
                        if jitter:
                            delay = delay * (0.5 + random.random() * 0.5)
                        time.sleep(delay)
            raise last_exception
        return wrapper
    return decorator


def retry_on_failure(
    func: Callable,
    args: tuple = (),
    kwargs: Optional[dict] = None,
    max_attempts: int = 2,
    default_return=None,
) -> tuple:
    """
    对函数调用进行简单重试，失败时返回默认值。

    Returns:
        (success, result_or_default) 元组
    """
    if kwargs is None:
        kwargs = {}
    for attempt in range(max_attempts):
        try:
            result = func(*args, **kwargs)
            return (True, result)
        except Exception:
            if attempt < max_attempts - 1:
                delay = 1.0 * (2 ** attempt)
                time.sleep(delay)
    return (False, default_return)
