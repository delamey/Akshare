"""
集中配置模块

包含所有配置常量、全局变量和环境变量。
"""

import os
import socket
import threading
import requests
import pandas as pd
from typing import Dict, Optional

# =============================================================================
# API 配置
# =============================================================================

TUSHARE_TOKEN = os.environ.get('TUSHARE_TOKEN', '')
DASHSCOPE_API_KEY = os.environ.get('DASHSCOPE_API_KEY', '')
DASHSCOPE_API_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'

# =============================================================================
# API 延迟配置（秒）
# =============================================================================

API_DELAY_SHORT = 0.3
API_DELAY_MEDIUM = 0.5

# =============================================================================
# 缓存配置
# =============================================================================

_CACHE_TTL = 300
_CACHE_MAX_SIZE_MB = 100
_CACHE_FAIL_COOLDOWN = 30

# =============================================================================
# 技术指标阈值
# =============================================================================

RSI_OVERBOUGHT_THRESHOLD = 70
RSI_OVERSOLD_THRESHOLD = 30
VOLUME_SURGE_RATIO = 1.5
MIN_DATA_ROWS = 5

# =============================================================================
# 策略评分阈值
# =============================================================================

STRATEGY_THRESHOLD_SHORT = 60
STRATEGY_THRESHOLD_MAIN = 55
STRATEGY_THRESHOLD_VALUE = 50

# =============================================================================
# 并发配置
# =============================================================================

MAX_WORKERS_SHORT = 5
MAX_WORKERS_DEFAULT = 3

# =============================================================================
# HTTP 超时配置
# =============================================================================

HTTP_TIMEOUT_SECONDS = 10

# =============================================================================
# 涨停比例
# =============================================================================

LIMIT_UP_RATIO_MAIN = 1.1
LIMIT_UP_RATIO_GEM = 1.2

# =============================================================================
# 全局缓存和会话
# =============================================================================

_market_cache: Dict[str, tuple] = {}
_market_cache_lock = threading.Lock()
_warmup_done = threading.Event()
_warmup_started = False
_warmup_started_lock = threading.Lock()
_warmup_thread_ref: Optional[threading.Thread] = None
_cache_fail_times: Dict[str, float] = {}
_stock_name_map: Dict[str, str] = {}
_HTTP_SESSION = requests.Session()
_HTTP_SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
})

# Rich Console 实例（全局共享）
from rich.console import Console
from rich.markup import escape as rich_escape
from rich import box

console = Console()
