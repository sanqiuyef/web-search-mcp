# -*- coding: utf-8 -*-
"""
健康检查逻辑。
"""

from src.config import SERVER_PORT
from src.cache import cache_stats


def get_health_data() -> dict:
    """返回服务器健康状态数据。"""
    return {
        "status": "healthy",
        "server": "web-search-server",
        "port": SERVER_PORT,
        "cache": cache_stats(),
    }
