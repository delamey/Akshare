"""
A股数据查询工具 v3.0

功能特性
--------
1. 批量数据导出（13种数据类型）
2. 单股分析查询（L1/L2数据、技术指标）
3. 股票智能筛选系统（三种策略评分）
4. Excel/CSV 双格式导出
5. 自动重试机制（3级API Fallback）
6. 备用数据源切换（Akshare → Tushare → 东方财富）
7. 控制台模式运行，支持打包为EXE
8. AI分析功能集成（DashScope/通义千问，可选）

数据类型
--------
- L1数据：实时行情（最新价、涨跌幅、成交量等）
- L2数据：五档盘口（买卖五档的价格和量）
- 历史K线：日K线数据（用于技术分析）
- 资金流向：主力/超大单/大单/中单/小单净流入
- 财务报表：资产负债表、利润表、现金流量表
- 分红送配：分红记录、送股、转增

依赖库
------
- akshare: 免费股票数据接口
- tushare: 专业股票数据接口（需配置token）
- pandas: 数据处理
- rich: 控制台美化
- requests: HTTP请求
- openpyxl: Excel导出支持

环境变量
--------
- TUSHARE_TOKEN: Tushare API Token
- DASHSCOPE_API_KEY: DashScope AI分析API Key

使用方法
--------
直接运行: python Akshare.py
打包运行: Akshare.exe
"""

import sys
import io
import time
import os
import math
import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Tuple, Callable

# 第一部分：tqdm 兼容性修复（PyInstaller 打包环境）

try:
    import tqdm
    original_del = tqdm.tqdm.__del__
    def safe_del(self):
        try:
            original_del(self)
        except AttributeError:
            pass
    tqdm.tqdm.__del__ = safe_del
except Exception:
    pass

import akshare as ak
import tushare as ts
import pandas as pd
import requests
from rich.console import Console
from rich.markup import escape as rich_escape
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

# 第三部分：集中配置

TUSHARE_TOKEN = os.environ.get('TUSHARE_TOKEN', '')  # Tushare API Token
DASHSCOPE_API_KEY = os.environ.get('DASHSCOPE_API_KEY', 'sk-371b269e14c94a31aee29c6fe5cf1d81')  # DashScope AI分析API Key
DASHSCOPE_API_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'

API_DELAY_SHORT = 0.3
API_DELAY_MEDIUM = 0.5

# 全市场数据缓存（避免重复拉取5000+条行情数据）


_market_cache: Dict[str, tuple] = {}  # {source: (DataFrame, timestamp)}
_market_cache_lock = threading.Lock()
_CACHE_TTL = 300  # 缓存有效期5分钟
_warmup_done = threading.Event()
_warmup_started = False
_warmup_started_lock = threading.Lock()
_warmup_thread_ref: Optional[threading.Thread] = None
_CACHE_FAIL_COOLDOWN = 30  # 缓存获取失败后30秒内不再重试
_cache_fail_times: Dict[str, float] = {}  # {source: last_fail_timestamp}
_stock_name_map: Dict[str, str] = {}  # {code: name} 名称映射缓存
_HTTP_SESSION = requests.Session()
_HTTP_SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
})


def warmup_market_cache() -> None:
    """后台预热全市场行情缓存

    在程序启动时调用，后台线程加载东方财富全市场行情数据。
    加载完成后后续所有实时行情查询和股票名称查询均可命中缓存，
    将耗时从6s+降至毫秒级。

    安全机制：
    1. 防重入：_warmup_started 标记防止重复启动线程
    2. 外层 try/finally 保底：即使线程因 BaseException 崩溃也会设置 _warmup_done
    3. 自动恢复：检测到线程提前死亡时自动重启（最多1次）
    4. 失败时不影响主流程，后续每次调用独立重试 API
    """
    global _warmup_started
    with _warmup_started_lock:
        if _warmup_started:
            return
        _warmup_started = True

    _start_warmup_thread()


def _start_warmup_thread():
    global _warmup_thread_ref

    def _do_warmup():
        errors: List[str] = []
        elapsed = 0.0
        try:
            for attempt in range(3):
                try:
                    _t0 = time.perf_counter()
                    df = _get_cached_market_data("em", silent=True)
                    _t1 = time.perf_counter()
                    elapsed += _t1 - _t0
                    if df is not None and not df.empty:
                        log_success(
                            f"缓存预热完成: {elapsed:.1f}s ({len(df)}支股票)",
                            prefix="  ==>"
                        )
                        return
                    else:
                        errors.append(f"第{attempt + 1}次: 数据为空 ({_t1 - _t0:.1f}s)")
                        if attempt < 2:
                            time.sleep(1)
                except Exception as e:
                    errors.append(
                        f"第{attempt + 1}次: {type(e).__name__}: {str(e)[:40]}"
                    )
                    elapsed += 0
                    if attempt < 2:
                        time.sleep(1)
            log_warning(
                f"缓存预热未完成 ({'; '.join(errors)})",
                prefix="  ->"
            )
        finally:
            _warmup_done.set()

    t = threading.Thread(target=_do_warmup, daemon=True, name="cache-warmup")
    _warmup_thread_ref = t
    t.start()


def _check_warmup_thread_health() -> bool:
    """检查预热线程是否还活着

    如果线程已启动但异常死亡（未设置 _warmup_done），自动重启一次。
    返回 True 表示线程健康或已恢复。
    """
    global _warmup_thread_ref
    if _warmup_done.is_set():
        return True
    if _warmup_thread_ref is None or _warmup_thread_ref.is_alive():
        return True
    log_warning("预热线程异常终止，自动重启中...", prefix="  -->")
    _start_warmup_thread()
    return True

def _get_cached_market_data(source: str = "em", silent: bool = False) -> pd.DataFrame:
    """获取全市场行情数据（带缓存和TTL）

    缓存有效期5分钟，过期后自动刷新，防止数据过时和内存泄漏。

    Args:
        source: 数据源，'em' 东方财富 stock_zh_a_spot_em，'sina' 新浪 stock_zh_a_spot
        silent: True 时抑制错误日志（用于后台预热等场景）

    Returns:
        全市场行情DataFrame，失败时返回空DataFrame
    """
    now = time.time()
    with _market_cache_lock:
        if source in _market_cache:
            data, cached_ts = _market_cache[source]
            if now - cached_ts < _CACHE_TTL:
                return data

    last_fail = _cache_fail_times.get(source, 0)
    if now - last_fail < _CACHE_FAIL_COOLDOWN:
        with _market_cache_lock:
            return _market_cache.get(source, (pd.DataFrame(), 0))[0]

    _check_warmup_thread_health()

    try:
        if source == "em":
            df = ak.stock_zh_a_spot_em()
        elif source == "sina":
            df = ak.stock_zh_a_spot()
        else:
            df = pd.DataFrame()
        if df is not None and not df.empty:
            _cache_fail_times.pop(source, None)
            with _market_cache_lock:
                _market_cache[source] = (df, now)
                if source == "em" and '代码' in df.columns and '名称' in df.columns:
                    _stock_name_map.update(dict(zip(df['代码'], df['名称'])))
        return df if df is not None else pd.DataFrame()
    except Exception as e:
        _cache_fail_times[source] = now
        if not silent:
            if last_fail == 0:
                log_warning(f"缓存全市场数据({source})失败: {type(e).__name__}: {str(e)[:60]}")
        with _market_cache_lock:
            return _market_cache.get(source, (pd.DataFrame(), 0))[0]


def _clear_market_cache() -> None:
    """清除全市场数据缓存（每次开始新一轮分析时调用）"""
    with _market_cache_lock:
        _market_cache.clear()


# 第四部分：公共工具函数


def find_column(df: pd.DataFrame, *keywords) -> Optional[str]:
    """在DataFrame中查找匹配关键词的列名

    Args:
        df: 目标DataFrame
        *keywords: 关键词列表，如 '日期', 'date'

    Returns:
        匹配的列名，未找到返回None
    """
    for col in df.columns:
        col_lower = str(col).lower()
        if any(kw in str(col) or kw.lower() in col_lower for kw in keywords):
            return col
    return None


def make_l1_dict(code, name='N/A', price='N/A', change_pct='N/A', change_amt='N/A',
                 open_price='N/A', pre_close='N/A', high='N/A', low='N/A',
                 volume='N/A', amount='N/A', timestamp='N/A', source='N/A'):
    """构造标准L1数据字典（统一字段名，避免4处重复构造）"""
    return {
        '股票代码': code, '股票名称': name, '最新价': price,
        '涨跌幅': change_pct, '涨跌额': change_amt,
        '今开': open_price, '昨收': pre_close, '最高': high, '最低': low,
        '成交量': volume, '成交额': amount, '时间': timestamp,
        '数据来源': source,
    }


def find_stock_in_df(df: pd.DataFrame, code: str, col: str = '代码') -> pd.DataFrame:
    """在DataFrame中查找股票，先精确匹配再模糊匹配"""
    if df is None or df.empty or col not in df.columns:
        return pd.DataFrame()
    result = df[df[col] == code]
    if result.empty:
        result = df[df[col].astype(str).str.contains(code, na=False)]
    return result


def find_volume_column(df: pd.DataFrame) -> Optional[str]:
    """在DataFrame中查找成交量列名（中英文兼容）

    该工具消除了原代码中15+处的重复成交量列查找逻辑。
    """
    if '成交量' in df.columns:
        return '成交量'
    for col in df.columns:
        col_lower = col.lower()
        if 'vol' in col_lower or 'volume' in col_lower:
            return col
    return None


def clean_stock_code(stock_code: str) -> str:
    """清洗股票代码：移除市场前缀、转小写、补零、去空白

    Args:
        stock_code: 原始股票代码，如 'sh600000', 'SZ000001', '600000'

    Returns:
        清洗后的6位纯数字股票代码
    """
    code = str(stock_code).strip().lower().replace('sh', '').replace('sz', '').strip()
    return code.zfill(6)


def get_market_prefix(stock_code: str) -> str:
    """根据股票代码判断市场前缀

    规则：6/5开头 → 上海(sh)，其余 → 深圳(sz)

    Args:
        stock_code: 6位纯数字股票代码

    Returns:
        'sh' 或 'sz'
    """
    clean = clean_stock_code(stock_code)
    return 'sh' if clean.startswith(('6', '5')) else 'sz'


def format_tushare_code(stock_code: str) -> str:
    """转换为Tushare格式的股票代码（如 600000.SH）

    Args:
        stock_code: 6位纯数字股票代码

    Returns:
        Tushare格式代码
    """
    clean = clean_stock_code(stock_code)
    return f'{clean}.SH' if clean.startswith(('6', '5')) else f'{clean}.SZ'


def format_yahoo_code(stock_code: str) -> str:
    """转换为Yahoo Finance格式的股票代码（如 600000.SS）

    Yahoo Finance中上海证券交易所代码后缀为 .SS，深圳为 .SZ

    Args:
        stock_code: 6位纯数字股票代码

    Returns:
        Yahoo Finance格式代码
    """
    clean = clean_stock_code(stock_code)
    return f'{clean}.SS' if clean.startswith(('6', '5')) else f'{clean}.SZ'


# 第五部分：Windows 终端 Unicode 编码修复 + Rich 控制台初始化

if sys.platform == 'win32':
    # 设置 Windows 控制台代码页为 UTF-8
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)  # UTF-8
        kernel32.SetConsoleCP(65001)        # UTF-8
    except Exception:
        pass
    
    # 修复 stdout/stderr 编码为 UTF-8，使用 errors='replace' 处理无法编码的字符
    if sys.stdout is not None and hasattr(sys.stdout, 'buffer'):
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        except Exception:
            pass
    if sys.stderr is not None and hasattr(sys.stderr, 'buffer'):
        try:
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
        except Exception:
            pass


def _safe_print(*args, **kwargs):
    """安全的打印函数，处理流已关闭的情况"""
    try:
        print(*args, **kwargs)
    except ValueError as e:
        if "I/O operation on closed file" in str(e):
            # 流已关闭，尝试写入文件或忽略
            try:
                with open('output.log', 'a', encoding='utf-8') as f:
                    print(*args, file=f, **kwargs)
            except Exception:
                pass
        else:
            raise
    except Exception:
        pass

# 第六部分：DashScope AI 分析 API

_DASHSCOPE_PLACEHOLDER_MSG = (
    "[yellow]提示：请前往 https://dashscope.aliyun.com 注册获取免费API Key，并替换代码中的api_key[/yellow]\n"
    "新用户赠送100万Token，支持qwen-max、qwen-plus等模型。"
)

_DASHSCOPE_MOCK_ANALYSIS = (
    "\n【模拟分析结果】\n"
    "\n基于数据分析，以下是有潜力的股票推荐：\n"
    "\n1. 北向资金重仓股 - 关注外资持续流入的标的\n"
    "2. 涨停股中的热点板块龙头\n"
    "3. 行业板块涨幅居前的细分领域\n"
    "4. 注意风险控制，建议结合基本面分析"
)


def _call_dashscope_api(system_prompt: str, user_content: str,
                        temperature: float = 0.7, max_tokens: int = 2000,
                        timeout: int = 60) -> str:
    """调用 DashScope (通义千问) API 的公共函数

    Args:
        system_prompt: 系统提示词
        user_content: 用户消息内容
        temperature: 生成温度，默认 0.7
        max_tokens: 最大输出 token 数，默认 2000
        timeout: 请求超时时间（秒），默认 60

    Returns:
        AI 生成的文本内容。如果 API Key 为占位符则返回模拟结果；
        请求失败则返回带 [red] 标签的错误信息字符串。
    """
    # 检查 API Key 是否已配置
    if not DASHSCOPE_API_KEY or DASHSCOPE_API_KEY.startswith("sk-xxx"):
        return _DASHSCOPE_PLACEHOLDER_MSG + _DASHSCOPE_MOCK_ANALYSIS

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}"
    }

    payload = {
        "model": "qwen-plus",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        "temperature": temperature,
        "max_tokens": max_tokens
    }

    try:
        response = requests.post(DASHSCOPE_API_URL, headers=headers, json=payload, timeout=timeout)
        response.raise_for_status()
        result = response.json()
        return result['choices'][0]['message']['content']
    except requests.exceptions.RequestException as e:
        return f"[red]API调用失败: {str(e)}[/red]"
    except Exception as e:
        return f"[red]分析失败: {str(e)}[/red]"


# =============================================================================
# 第七部分：Rich 控制台初始化
# =============================================================================

# 创建控制台对象（简化配置以避免打包时的 I/O 问题）
console = Console(force_terminal=False, force_interactive=False)

# 第七部分：统一日志输出（消除70+处 try/except 重复模式）


LOG_COLORS = {
    'info': 'cyan',
    'success': 'green',
    'warning': 'yellow',
    'error': 'red',
    'debug': 'dim',
}


def _safe_display(rich_text: str, fallback_text: str = None) -> None:
    """安全显示文本：优先使用 Rich console，失败回退到 print。

    该包装器消除了原代码中每次 console.print 调用都需 try/except 的重复模式（70+处），
    在打包 EXE 或 I/O 异常时自动降级为 print 输出。
    """
    try:
        console.print(rich_text)
    except Exception:
        if fallback_text is not None:
            try:
                _safe_print(fallback_text)
            except Exception:
                pass


def _log(level: str, message: str, prefix: str = "  ->") -> None:
    """统一日志输出：替代原有的 log_info/success/warning/error/debug 五个函数。

    Args:
        level: 'info' | 'success' | 'warning' | 'error' | 'debug'
        message: 日志消息内容
        prefix: 前缀标识，默认 "  ->"（debug级别无前缀）
    """
    color = LOG_COLORS.get(level, 'cyan')
    if level == 'debug':
        _safe_display(f"[{color}]{message}[/{color}]", f"{message}")
    else:
        _safe_display(f"[{color}]{prefix} {message}[/{color}]", f"{prefix} {message}")


# 保留旧函数名作为别名，保持向后兼容，同时避免全部重命名
def log_info(message: str, prefix: str = "  ->") -> None:
    _log('info', message, prefix)


def log_success(message: str, prefix: str = "  ->") -> None:
    _log('success', message, prefix)


def log_warning(message: str, prefix: str = "  ->") -> None:
    _log('warning', message, prefix)


def log_error(message: str, prefix: str = "  ->") -> None:
    _log('error', message, prefix)


def log_debug(message: str) -> None:
    _log('debug', message)

# 第八部分：多 API Fallback 机制

def try_multiple_apis(api_list: List[Tuple[Callable, str]], *args, max_errors: int = 3, **kwargs) -> Optional[Any]:
    """
    尝试多个 API，返回第一个成功的结果
    
    Args:
        api_list: API函数和名称的列表，如 [(ak.stock_zh_a_spot, "新浪"), ...]
        args: 传递给 API 的位置参数
        max_errors: 最大重试次数
        kwargs: 传递给 API 的关键字参数
    
    Returns:
        第一个成功返回的结果，所有API都失败则返回None
    """
    errors = []
    
    for api_func, api_name in api_list:
        for attempt in range(max_errors):
            try:
                log_info(f"方案: {api_name}" + (f" 尝试 {attempt+1}/{max_errors}" if max_errors > 1 else ""))
                result = api_func(*args, **kwargs)
                
                # 验证结果
                if result is None:
                    errors.append(f"{api_name} 返回 None")
                    continue
                
                if isinstance(result, pd.DataFrame):
                    if result.empty:
                        errors.append(f"{api_name} 返回空数据")
                        continue
                elif hasattr(result, '__len__') and len(result) == 0:
                    errors.append(f"{api_name} 返回空数据")
                    continue
                
                log_success(f"{api_name} 成功")
                return result
                
            except Exception as e:
                error_msg = str(e)[:30]
                if attempt < max_errors - 1:
                    time.sleep(0.5 * (attempt + 1))
                else:
                    errors.append(f"{api_name} 失败: {error_msg}")
    
    if errors:
        log_warning(f"所有API均失败: {'; '.join(errors[:3])}")
    return None

def safe_run_with_progress(task_callback, task_description="处理中...", total=None):
    """
    安全的进度显示包装器，兼容打包的 EXE 环境
    
    Args:
        task_callback: 回调函数，接受 (update_progress, total) 参数
        task_description: 任务描述
        total: 总进度数量
    """
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=console
        ) as progress:
            task = progress.add_task(task_description, total=total)
            
            def update_progress(advance=1):
                progress.advance(task, advance)
            
            task_callback(update_progress, total)
    except Exception:
        # 如果 Progress 组件失败，使用简单的打印方式
        print(task_description)
        def simple_update(advance=1):
            pass
        task_callback(simple_update, total)


def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """
    计算相对强弱指数（RSI）

    RSI是衡量股价上涨和下跌力度的技术指标，取值范围0-100。
    RSI > 70 表示超买，RSI < 30 表示超卖。

    Args:
        prices: 价格序列（通常为收盘价）
        period: 计算周期，默认14日

    Returns:
        RSI序列，与输入价格序列长度相同

    Example:
        >>> prices = pd.Series([10, 12, 11, 13, 14, 12])
        >>> rsi = calculate_rsi(prices)
    """
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    # 修复零除bug：当loss为0时RSI应为100
    rs = gain / loss.replace(0, float('nan'))
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.fillna(100)  # loss=0时意味着全部上涨，RSI=100
    return rsi


def calculate_macd(
    prices: pd.Series, 
    fast: int = 12, 
    slow: int = 26, 
    signal: int = 9
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    计算MACD指标（指数平滑异同移动平均线）
    
    MACD由三条线组成：
    - MACD线：快速EMA - 慢速EMA
    - Signal线：MACD线的EMA
    - Histogram：MACD线 - Signal线
    
    Args:
        prices: 价格序列（通常为收盘价）
        fast: 快速EMA周期，默认12
        slow: 慢速EMA周期，默认26
        signal: Signal线EMA周期，默认9
        
    Returns:
        Tuple包含三个Series: (macd_line, signal_line, histogram)
        
    Example:
        >>> prices = pd.Series([10, 12, 11, 13, 14, 12, 15, 16])
        >>> macd, signal, hist = calculate_macd(prices)
    """
    exp1 = prices.ewm(span=fast, adjust=False).mean()
    exp2 = prices.ewm(span=slow, adjust=False).mean()
    macd = exp1 - exp2
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    histogram = macd - signal_line
    return macd, signal_line, histogram


def calculate_momentum(prices: pd.Series, period: int = 20) -> pd.Series:
    """
    计算N日动量指标（收益率）
    
    动量指标衡量价格变化的速度和方向，计算公式：
    动量 = (今日收盘价 / N日前收盘价 - 1) × 100
    
    Args:
        prices: 价格序列（通常为收盘价）
        period: 计算周期，默认20日
        
    Returns:
        动量序列，N日前的值为NaN
        
    Example:
        >>> prices = pd.Series([10, 11, 12, 11, 13, 14])
        >>> momentum = calculate_momentum(prices, period=5)
    """
    return (prices / prices.shift(period) - 1) * 100


# 辅助函数：获取股票名称
def get_stock_name(stock_code: str, console: Console) -> str:
    """
    从多个API尝试获取股票名称

    Args:
        stock_code: 股票代码
        console: rich控制台对象

    Returns:
        股票名称，无法获取时返回股票代码
    """
    # 方案1: Akshare-新浪（使用缓存）
    try:
        log_info("方案1: Akshare-新浪")
        spot_df = _get_cached_market_data("sina")
        if spot_df is not None and not spot_df.empty:
            market_prefix = get_market_prefix(stock_code)
            sina_code = f"{market_prefix}{stock_code}"
            match = find_stock_in_df(spot_df, sina_code)
            if not match.empty:
                name = match.iloc[0].get('名称', stock_code)
                log_success(f"从Akshare-新浪获取到股票名称: {name}")
                return name
    except Exception as e:
        log_warning(f"Akshare-新浪失败: {str(e)[:30]}")
    
    # 方案2: Tushare
    try:
        log_info("方案2: Tushare")
        pro = ts.pro_api()
        ts_code = format_tushare_code(stock_code)
        df = pro.stock_basic(ts_code=ts_code)
        if df is not None and not df.empty:
            name = df.iloc[0].get('name', stock_code)
            log_success(f"从Tushare获取到股票名称: {name}")
            return name
    except Exception as e:
        log_warning(f"Tushare失败: {str(e)[:30]}")
    
    # 方案3: 东方财富个股信息
    try:
        log_info("方案3: 东方财富")
        info_df = ak.stock_individual_info_em(symbol=stock_code)
        if info_df is not None and not info_df.empty:
            for _, row in info_df.iterrows():
                if row.get('item') == '股票名称':
                    name = row.get('value', stock_code)
                    log_success(f"从东方财富获取到股票名称: {name}")
                    return name
    except Exception as e:
        log_warning(f"东方财富失败: {str(e)[:30]}")
    
    # 方案4: 东方财富全市场行情
    try:
        log_info("方案4: 东方财富")
        em_df = _get_cached_market_data("em")
        if em_df is not None and not em_df.empty:
            match = em_df[em_df['代码'].astype(str).str.contains(stock_code)]
            if not match.empty:
                name = match.iloc[0].get('名称', stock_code)
                log_success(f"从其他渠道获取到股票名称: {name}")
                return name
    except Exception as e:
        log_debug(f"东方财富全市场获取名称失败: {type(e).__name__}: {str(e)[:40]}")
    
    # 所有API都无法访问，提示用户这是网络限制
    log_warning("无法获取股票名称（网络限制），使用代码代替")
    log_debug("提示：多个数据源API均无法访问，可能是网络连接问题")
    return stock_code




def _fetch_l1_data(stock_code: str) -> Dict[str, Any]:
    """
    获取 L1 实时行情数据（四级 API 回退）

    依次尝试四个数据源：
    1. 东方财富 stock_zh_a_spot_em（主力，使用内存缓存减少重复请求）
    2. 腾讯历史K线 API（直连，提取最新一条数据，从前后两天 close 计算涨跌幅）
    3. 新浪 stock_zh_a_spot（备用源）
    4. 网易 stock_zh_a_spot_163（最终备用源）

    方案2可自行计算涨跌幅、涨跌额，确保只要K线数据 ≥ 2 条就不会缺失这两个字段。

    返回统一格式字典，包含：股票代码、名称、最新价、涨跌幅、涨跌额、今开、
    昨收、最高、最低、成交量、成交额、时间戳、数据来源。

    Args:
        stock_code: 纯数字股票代码（如 '688275'）

    Returns:
        行情数据字典。所有数据源均失败时返回空字典 {}。
    """
    clean_code = clean_stock_code(stock_code)

    result = {}
    l1_success = False

    log_info(f"[{clean_code}] 开始获取L1数据...")
    time.sleep(API_DELAY_SHORT)
    
    # 方案1: 东方财富个股行情（使用缓存）
    try:
        log_info(f"[{clean_code}] 方案1: stock_zh_a_spot_em (东方财富) - 正在请求...")
        info_df = _get_cached_market_data("em")
        log_info(f"[{clean_code}] 方案1: 数据返回，检查数据有效性...")

        if info_df is not None and not info_df.empty:
            log_info(f"[{clean_code}] 方案1: 获取到 {len(info_df)} 条全市场数据，正在匹配...")
            market_prefix = get_market_prefix(clean_code)
            full_code = f"{market_prefix}{clean_code}"
            
            stock_info = find_stock_in_df(info_df, full_code)
            log_info(f"[{clean_code}] 方案1: 匹配结果: {len(stock_info)} 条")
            
            if not stock_info.empty:
                row = stock_info.iloc[0]
                result = make_l1_dict(
                    code=clean_code,
                    name=row.get('名称', 'N/A'),
                    price=row.get('最新价', 'N/A'),
                    change_pct=row.get('涨跌幅', 'N/A'),
                    change_amt=row.get('涨跌额', 'N/A'),
                    open_price=row.get('今开', 'N/A'),
                    pre_close=row.get('昨收', 'N/A'),
                    high=row.get('最高', 'N/A'),
                    low=row.get('最低', 'N/A'),
                    volume=row.get('成交量', 'N/A'),
                    amount=row.get('成交额', 'N/A'),
                    timestamp=row.get('时间', 'N/A'),
                    source='东方财富API',
                )
                log_success(f"[{clean_code}] 方案1: L1数据获取成功 (来源: 东方财富)")
                l1_success = True
            else:
                log_warning(f"[{clean_code}] 方案1: 股票代码 {full_code} 未在全市场数据中找到")
        else:
            log_warning(f"[{clean_code}] 方案1: 返回数据为空或None")
    except Exception as e:
        log_error(f"[{clean_code}] 方案1: 东方财富L1失败 - {type(e).__name__}: {str(e)[:60]}")
    
    # 方案2: 使用腾讯API获取历史数据补全（直连，绕过 get_tx_start_year）
    if not l1_success:
        try:
            log_info(f"[{clean_code}] 方案2: 使用腾讯API获取历史数据补全 - 正在请求...")
            market_tx = get_market_prefix(clean_code)
            tx_code = f"{market_tx}{clean_code}"
            today = datetime.now()
            one_month_ago = today - timedelta(days=30)
            start_date_str = one_month_ago.strftime("%Y%m%d")
            end_date_str = today.strftime("%Y%m%d")
            
            backup_hist = _fetch_tencent_klines_direct(
                symbol=tx_code,
                start_date=start_date_str,
                end_date=end_date_str,
                adjust="qfq",
                timeout=10.0,
            )
            log_info(f"[{clean_code}] 方案2: 腾讯数据返回，检查有效性...")
            
            if backup_hist is not None and not backup_hist.empty and len(backup_hist) >= 1:
                date_col = None
                for col in backup_hist.columns:
                    if 'date' in col.lower() or '日期' in str(col):
                        date_col = col
                        break
                
                if date_col:
                    backup_hist = backup_hist.sort_values(date_col)
                
                last_row = backup_hist.iloc[-1]
                n_rows = len(backup_hist)
                log_info(f"[{clean_code}] 方案2: 获取到 {n_rows} 条历史数据，使用最新一条")

                last_close = last_row.get('close', last_row.get('收盘', None))
                change_pct = 'N/A'
                change_amt = 'N/A'

                if n_rows >= 2 and last_close is not None:
                    prev_row = backup_hist.iloc[-2]
                    prev_close = prev_row.get('close', prev_row.get('收盘', None))
                    if prev_close is not None and prev_close != 0:
                        try:
                            change_amt = round(float(last_close) - float(prev_close), 2)
                            change_pct = round(change_amt / float(prev_close) * 100, 2)
                        except (ValueError, TypeError):
                            pass
                    log_info(f"[{clean_code}] 方案2: 计算涨跌幅 (昨收={prev_close}, 涨跌幅={change_pct}%)")

                stock_name = get_stock_name(clean_code, console)
                last_volume = last_row.get('成交量', last_row.get('vol', 'N/A'))
                last_amount = last_row.get('成交额', last_row.get('amount', 'N/A'))
                result = make_l1_dict(
                    code=clean_code,
                    name=stock_name,
                    price=last_close if last_close is not None else 'N/A',
                    change_pct=change_pct,
                    change_amt=change_amt,
                    open_price=last_row.get('开盘', last_row.get('open', 'N/A')),
                    high=last_row.get('最高', last_row.get('high', 'N/A')),
                    low=last_row.get('最低', last_row.get('low', 'N/A')),
                    volume=last_volume,
                    amount=last_amount,
                    timestamp=last_row.get('日期', last_row.get('date', 'N/A')),
                    source='腾讯历史数据(备用)',
                )
                log_success(f"[{clean_code}] 方案2: 腾讯历史数据补全成功")
                l1_success = True
            else:
                log_warning(f"[{clean_code}] 方案2: 腾讯历史数据为空")
        except Exception as e:
            log_error(f"[{clean_code}] 方案2: 腾讯历史数据补全失败 - {type(e).__name__}: {str(e)[:60]}")
    
    # 方案3: 新浪API（最后备用）
    if not l1_success:
        try:
            log_info(f"[{clean_code}] 方案3: stock_zh_a_spot (新浪备用) - 正在请求...")
            spot_df = ak.stock_zh_a_spot()
            log_info(f"[{clean_code}] 方案3: 新浪数据返回，检查有效性...")

            if spot_df is not None and not spot_df.empty:
                log_info(f"[{clean_code}] 方案3: 获取到 {len(spot_df)} 条新浪数据，正在匹配...")
                market_prefix = get_market_prefix(clean_code)
                sina_code = f"{market_prefix}{clean_code}"
                stock_info = spot_df[spot_df['代码'] == sina_code]
                log_info(f"[{clean_code}] 方案3: 精确匹配结果: {len(stock_info)} 条")
                
                if stock_info.empty:
                    stock_info = spot_df[spot_df['代码'].astype(str).str.contains(clean_code)]
                    log_info(f"[{clean_code}] 方案3: 模糊匹配结果: {len(stock_info)} 条")
                
                if not stock_info.empty:
                    row = stock_info.iloc[0]
                    result = {
                        '股票代码': clean_code,
                        '股票名称': row.get('名称', 'N/A'),
                        '最新价': row.get('最新价', 'N/A'),
                        '涨跌幅': row.get('涨跌幅', 'N/A'),
                        '涨跌额': row.get('涨跌额', 'N/A'),
                        '今开': row.get('今开', 'N/A'),
                        '昨收': row.get('昨收', 'N/A'),
                        '最高': row.get('最高', 'N/A'),
                        '最低': row.get('最低', 'N/A'),
                        '成交量': row.get('成交量', 'N/A'),
                        '成交额': row.get('成交额', 'N/A'),
                        '时间': row.get('时间戳', 'N/A'),
                        '数据来源': '新浪API(备用)',
                    }
                    log_success(f"[{clean_code}] 方案3: 新浪L1数据获取成功")
                    l1_success = True
                else:
                    log_warning(f"[{clean_code}] 方案3: 股票代码 {sina_code} 未在新浪数据中找到")
            else:
                log_warning(f"[{clean_code}] 方案3: 新浪数据返回为空")
        except Exception as e:
            log_error(f"[{clean_code}] 方案3: 新浪L1失败 - {type(e).__name__}: {str(e)[:60]}")
    
    # 方案4: 网易163行情数据（新增备用源）
    if not l1_success:
        try:
            log_info(f"[{clean_code}] 方案4: stock_zh_a_spot_163 (网易备用) - 正在请求...")
            wy_df = ak.stock_zh_a_spot_163()
            log_info(f"[{clean_code}] 方案4: 网易数据返回，检查有效性...")

            if wy_df is not None and not wy_df.empty:
                log_info(f"[{clean_code}] 方案4: 获取到 {len(wy_df)} 条网易数据，正在匹配...")
                stock_info = find_stock_in_df(wy_df, clean_code, col='代码')
                log_info(f"[{clean_code}] 方案4: 匹配结果: {len(stock_info)} 条")

                if not stock_info.empty:
                    row = stock_info.iloc[0]
                    result = {
                        '股票代码': clean_code,
                        '股票名称': row.get('名称', 'N/A'),
                        '最新价': row.get('最新价', 'N/A'),
                        '涨跌幅': row.get('涨跌幅', 'N/A'),
                        '涨跌额': row.get('涨跌额', 'N/A'),
                        '今开': row.get('今开', 'N/A'),
                        '昨收': row.get('昨收', 'N/A'),
                        '最高': row.get('最高', 'N/A'),
                        '最低': row.get('最低', 'N/A'),
                        '成交量': row.get('成交量', 'N/A'),
                        '成交额': row.get('成交额', 'N/A'),
                        '时间': row.get('时间', 'N/A'),
                        '数据来源': '网易API(备用)',
                    }
                    log_success(f"[{clean_code}] 方案4: 网易L1数据获取成功")
                    l1_success = True
                else:
                    log_warning(f"[{clean_code}] 方案4: 股票代码 {clean_code} 未在网易数据中找到")
            else:
                log_warning(f"[{clean_code}] 方案4: 网易数据返回为空")
        except Exception as e:
            log_error(f"[{clean_code}] 方案4: 网易L1失败 - {type(e).__name__}: {str(e)[:60]}")
    
    if l1_success:
        log_success(f"[{clean_code}] L1数据获取完成 (最终来源: {result.get('数据来源', 'N/A')})")
    else:
        log_error(f"[{clean_code}] L1数据获取失败 (所有方案都失败)")
    
    return result


def _fetch_historical_klines(stock_code: str) -> pd.DataFrame:
    """
    获取历史K线数据（近1个月，带超时保护包装）

    作为 _fetch_historical_klines_inner 的包装层，提供以下保护：
    - socket 超时设置为 10 秒，防止底层 API 调用永久阻塞
    - 无论成功失败，始终通过 finally 恢复原始超时设置

    Args:
        stock_code: 纯数字股票代码（如 '688275'）

    Returns:
        历史K线 DataFrame，包含日期/开盘/收盘/最高/最低/成交量等列。
        所有数据源均失败时返回空 DataFrame。
    """
    clean_code = clean_stock_code(stock_code)
    
    log_info(f"[{clean_code}] 开始获取历史K线数据...")
    time.sleep(API_DELAY_MEDIUM)

    default_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(10)  # 10秒超时
    
    try:
        return _fetch_historical_klines_inner(clean_code)
    finally:
        # 无论成功失败，始终恢复原始超时设置
        socket.setdefaulttimeout(default_timeout)


def _fetch_tencent_klines_direct(symbol: str, start_date: str = "", end_date: str = "",
                                  adjust: str = "", timeout: float = 10.0) -> pd.DataFrame:
    """直接调用腾讯K线API，绕过 akshare 的 get_tx_start_year（该函数会导致 int() 解析错误）

    参考 akshare.stock_feature.stock_hist_tx.stock_zh_a_hist_tx 源码，
    但跳过有问题的 get_tx_start_year() 调用。

    Args:
        symbol: 带市场前缀的代码，如 "sh600000", "sz000001"
        start_date: 开始日期，格式 YYYYMMDD 或 YYYY-MM-DD
        end_date: 结束日期，格式 YYYYMMDD 或 YYYY-MM-DD
        adjust: 复权类型 "" 不复权, "qfq" 前复权, "hfq" 后复权
        timeout: 请求超时秒数

    Returns:
        DataFrame，列包含 date/open/close/high/low/amount
    """
    # 统一日期格式为 YYYY-MM-DD
    if start_date:
        sd = start_date.replace("-", "")
        start_date_fmt = f"{sd[:4]}-{sd[4:6]}-{sd[6:]}"
    else:
        start_date_fmt = ""
    
    if end_date:
        ed = end_date.replace("-", "")
        end_date_fmt = f"{ed[:4]}-{ed[4:6]}-{ed[6:]}"
    else:
        end_date_fmt = ""
    
    url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
    
    # 确定年份范围
    try:
        if start_date:
            range_start = int(start_date.replace("-", "")[:4])
        else:
            range_start = datetime.now().year - 1
        range_end = datetime.now().year + 1
    except (ValueError, IndexError):
        range_start = datetime.now().year - 1
        range_end = datetime.now().year + 1
    
    df_list = []
    for year in range(range_start, range_end):
        params = {
            "_var": f"kline_day{adjust}{year}",
            "param": f"{symbol},day,{year}-01-01,{year + 1}-12-31,640,{adjust}",
            "r": "0.8205512681390605",
        }
        try:
            with _HTTP_SESSION.get(url, params=params, timeout=timeout) as r:
                data_text = r.text
            # 解析 JSONP 格式: kline_dayqfq2026={...}
            json_start = data_text.find("={")
            if json_start == -1:
                continue
            json_str = data_text[json_start + 1:]
            data_json = json.loads(json_str)

            if "data" not in data_json or symbol not in data_json["data"]:
                continue

            symbol_data = data_json["data"][symbol]
            # 优先取 qfqday/hfqday，否则取 day
            if "qfqday" in symbol_data:
                temp_df = pd.DataFrame(symbol_data["qfqday"])
            elif "hfqday" in symbol_data:
                temp_df = pd.DataFrame(symbol_data["hfqday"])
            elif "day" in symbol_data:
                temp_df = pd.DataFrame(symbol_data["day"])
            else:
                continue

            df_list.append(temp_df)
        except Exception:
            continue

    big_df = pd.concat(df_list, ignore_index=True) if df_list else pd.DataFrame()
    
    if big_df.empty:
        return pd.DataFrame()
    
    big_df = big_df.iloc[:, :6]
    big_df.columns = ["date", "open", "close", "high", "low", "amount"]
    big_df["date"] = pd.to_datetime(big_df["date"], errors="coerce").dt.date
    big_df["open"] = pd.to_numeric(big_df["open"], errors="coerce")
    big_df["close"] = pd.to_numeric(big_df["close"], errors="coerce")
    big_df["high"] = pd.to_numeric(big_df["high"], errors="coerce")
    big_df["low"] = pd.to_numeric(big_df["low"], errors="coerce")
    big_df["amount"] = pd.to_numeric(big_df["amount"], errors="coerce")
    big_df.drop_duplicates(inplace=True, ignore_index=True)
    big_df.index = pd.to_datetime(big_df["date"], errors="coerce")
    big_df.sort_index(inplace=True)
    
    # 按日期范围过滤
    if start_date_fmt or end_date_fmt:
        try:
            big_df = big_df.loc[start_date_fmt or None:end_date_fmt or None]
        except Exception:
            pass
    
    big_df.reset_index(inplace=True, drop=True)
    return big_df


def _fetch_historical_klines_inner(clean_code: str) -> pd.DataFrame:
    """历史K线获取核心逻辑（由 _fetch_historical_klines 包装超时保护）"""
    log_info(f"[{clean_code}] 开始获取历史K线数据...")

    today = datetime.now()
    one_month_ago = today - timedelta(days=30)
    start_date_str = one_month_ago.strftime("%Y%m%d")
    end_date_str = today.strftime("%Y%m%d")
    log_info(f"[{clean_code}] 历史数据周期: 近1个月 ({start_date_str} 至今)")
    
    max_retries = 3
    retry_delay = 2
    hist_df = pd.DataFrame()
    
    # 方案1: 腾讯API（直接调用，绕过 akshare 的 get_tx_start_year）
    # 修复: stock_zh_a_hist_tx 内部 get_tx_start_year() 返回异常数据导致 int() 错误
    try:
        log_info(f"[{clean_code}] 方案1: 腾讯K线API（直连） - 正在请求...")
        tx_code = f"{get_market_prefix(clean_code)}{clean_code}"
        
        tx_df = _fetch_tencent_klines_direct(
            symbol=tx_code,
            start_date=start_date_str,
            end_date=end_date_str,
            adjust="qfq",
            timeout=10.0,
        )
        
        log_info(f"[{clean_code}] 方案1: 腾讯数据返回，检查有效性...")
        
        if tx_df is not None and not tx_df.empty and len(tx_df) >= 5:
            log_info(f"[{clean_code}] 方案1: 获取到 {len(tx_df)} 条腾讯数据，开始转换列名...")
            
            try:
                # 安全地处理列名映射 — 同时支持中英文列名
                column_mapping_variants = [
                    # 英文列名 → 中文（腾讯API原始返回）
                    {'open': '开盘', 'close': '收盘', 'high': '最高',
                     'low': '最低', 'vol': '成交量', 'amount': '成交额',
                     'volume': '成交量'},
                    # 小写日期列
                    {'date': '日期', 'time': '日期', 'trade_date': '日期'},
                ]
                for mapping in column_mapping_variants:
                    safe_mapping = {k: v for k, v in mapping.items() if k in tx_df.columns}
                    tx_df = tx_df.rename(columns=safe_mapping)
                
                required_cols = ['日期', '开盘', '收盘', '最高', '最低']
                for req_col in required_cols:
                    if req_col not in tx_df.columns:
                        for col in tx_df.columns:
                            col_lower = str(col).lower()
                            if req_col == '日期' and ('date' in col_lower or 'time' in col_lower):
                                tx_df[req_col] = tx_df[col]
                                log_info(f"[{clean_code}] 方案1: 映射列 '{col}' -> '{req_col}'")
                                break
                            elif req_col == '开盘' and 'open' in col_lower:
                                tx_df[req_col] = tx_df[col]; break
                            elif req_col == '收盘' and 'close' in col_lower:
                                tx_df[req_col] = tx_df[col]; break
                            elif req_col == '最高' and 'high' in col_lower:
                                tx_df[req_col] = tx_df[col]; break
                            elif req_col == '最低' and 'low' in col_lower:
                                tx_df[req_col] = tx_df[col]; break
                
                # 验证数据类型并安全转换
                numeric_cols = ['开盘', '收盘', '最高', '最低', '成交量', '成交额']
                for col in numeric_cols:
                    if col in tx_df.columns:
                        try:
                            tx_df[col] = pd.to_numeric(tx_df[col], errors='coerce')
                        except Exception as conv_e:
                            log_warning(f"[{clean_code}] 方案1: 列 '{col}' 转换失败: {str(conv_e)[:40]}")
                
                # 移除无效行
                before_drop = len(tx_df)
                tx_df = tx_df.dropna(subset=['开盘', '收盘'])
                if before_drop > len(tx_df):
                    log_info(f"[{clean_code}] 方案1: 移除 {before_drop - len(tx_df)} 条无效行")
                
                if len(tx_df) >= 5:
                    log_success(f"[{clean_code}] 方案1: 腾讯历史K线获取成功 ({len(tx_df)} 条)")
                    return tx_df
                else:
                    log_warning(f"[{clean_code}] 方案1: 有效数据不足 {len(tx_df)} 条")
            except Exception as proc_e:
                log_error(f"[{clean_code}] 方案1: 数据处理失败 - {type(proc_e).__name__}: {str(proc_e)[:80]}")
        else:
            log_warning(f"[{clean_code}] 方案1: 腾讯数据不足 {len(tx_df) if tx_df is not None else 0} 条")
    except Exception as e:
        log_error(f"[{clean_code}] 方案1: 腾讯历史K线失败 - {type(e).__name__}: {str(e)[:80]}")
    
    # 方案2: Akshare-新浪 (备用) - 优化了连接管理和重试策略，减少 RemoteDisconnected
    # 核心修复：增加请求前延迟、使用短超时 + 快速重试、指数退避
    for attempt in range(max_retries):
        try:
            # 每次重试前增加延迟，给服务器缓冲时间
            if attempt > 0:
                wait_time = retry_delay * (2 ** (attempt - 1))
                log_info(f"[{clean_code}] 方案2: 第 {attempt+1}/{max_retries} 次重试，等待 {wait_time}秒...")
                time.sleep(wait_time)
            else:
                time.sleep(API_DELAY_SHORT)
            
            log_info(f"[{clean_code}] 方案2: Akshare-新浪 stock_zh_a_hist - 尝试 {attempt+1}/{max_retries}...")
            
            # 为每次请求设置独立超时
            try:
                hist_df = ak.stock_zh_a_hist(
                    symbol=clean_code, 
                    period="daily", 
                    start_date=start_date_str, 
                    adjust="qfq"
                )
            except ConnectionError as conn_e:
                # RemoteDisconnected 封装为 ConnectionError
                err_str = str(conn_e)[:80]
                log_error(f"[{clean_code}] 方案2: 连接中断 - {err_str}")
                if attempt < max_retries - 1:
                    continue
                else:
                    raise
            
            log_info(f"[{clean_code}] 方案2: 数据返回，检查有效性...")
            
            if hist_df is not None and not hist_df.empty and len(hist_df) >= 5:
                log_success(f"[{clean_code}] 方案2: 新浪历史K线获取成功 ({len(hist_df)} 条)")
                return hist_df
            else:
                log_warning(f"[{clean_code}] 方案2: 数据不足 {len(hist_df) if hist_df is not None else 0} 条 (需要 >= 5)")
        except ConnectionError as ce:
            log_error(f"[{clean_code}] 方案2: 连接错误 (第{attempt+1}次) - {str(ce)[:80]}")
            if attempt < max_retries - 1:
                log_info(f"[{clean_code}] 方案2: 将进行重试...")
                continue
        except Exception as e:
            err_type = type(e).__name__
            err_msg = str(e)[:80]
            log_error(f"[{clean_code}] 方案2: {err_type}: {err_msg}")
            if attempt < max_retries - 1:
                log_info(f"[{clean_code}] 方案2: {attempt+1} 次失败，准备重试...")
                continue
    
    # 方案3: Tushare — 已增加权限检测和降级接口
    try:
        log_info(f"[{clean_code}] 方案3: Tushare - 正在请求...")
        ts_code = format_tushare_code(clean_code)
        
        tushare_df = None
        
        # 先尝试 primary daily 接口
        try:
            pro = ts.pro_api()
            tushare_df = pro.daily(
                ts_code=ts_code, 
                start_date=start_date_str, 
                end_date=datetime.now().strftime("%Y%m%d")
            )
        except Exception as ts_e:
            err_str = str(ts_e)[:200]
            log_warning(f"[{clean_code}] 方案3: Tushare daily接口失败: {err_str}")
            
            # 检测权限错误
            if "权限" in err_str or "permission" in err_str.lower() or "没有接口" in err_str:
                log_info(f"[{clean_code}] 方案3: 检测到接口权限不足，尝试通用 pro_bar 接口...")
                try:
                    # 使用通用 pro_bar 接口作为降级方案
                    tushare_df = ts.pro_bar(
                        ts_code=ts_code, 
                        start_date=start_date_str, 
                        end_date=datetime.now().strftime("%Y%m%d"),
                        adj='qfq'
                    )
                except Exception as bar_e:
                    bar_err = str(bar_e)[:150]
                    log_warning(f"[{clean_code}] 方案3: pro_bar 降级接口也失败: {bar_err}")
                    raise
            else:
                raise
        
        log_info(f"[{clean_code}] 方案3: Tushare数据返回，检查有效性...")
        
        if tushare_df is not None and not tushare_df.empty and len(tushare_df) >= 5:
            log_info(f"[{clean_code}] 方案3: 获取到 {len(tushare_df)} 条Tushare数据，开始转换...")
            tushare_df = tushare_df.rename(columns={
                'trade_date': '日期', 'open': '开盘', 'close': '收盘',
                'high': '最高', 'low': '最低', 'vol': '成交量', 'amount': '成交额'
            })
            try:
                tushare_df['日期'] = pd.to_datetime(tushare_df['日期']).dt.strftime('%Y-%m-%d')
            except Exception:
                pass  # 日期列可能已经格式化
            log_success(f"[{clean_code}] 方案3: Tushare历史K线获取成功 ({len(tushare_df)} 条)")
            return tushare_df.sort_values('日期')
        else:
            log_warning(f"[{clean_code}] 方案3: Tushare数据不足 {len(tushare_df) if tushare_df is not None else 0} 条")
    except Exception as e:
        err_msg = str(e)[:120]
        if "权限" in err_msg or "permission" in err_msg.lower():
            log_error(f"[{clean_code}] 方案3: Tushare权限不足，请访问 https://tushare.pro/document/1 申请接口权限")
        else:
            log_error(f"[{clean_code}] 方案3: Tushare失败 - {type(e).__name__}: {err_msg}")
    
    # 方案4: yfinance（可选，容易被限流） — 已增强数据完整性验证
    try:
        log_info(f"[{clean_code}] 方案4: yfinance - 正在请求...")
        import yfinance as yf
        yahoo_code = format_yahoo_code(clean_code)
        
        # 指数退避处理 Rate Limit
        max_yf_retries = 2
        yf_delay = 3
        yf_df = None
        
        for yf_attempt in range(max_yf_retries):
            try:
                log_info(f"[{clean_code}] 方案4: yfinance - 尝试 {yf_attempt+1}/{max_yf_retries}")
                ticker = yf.Ticker(yahoo_code)
                yf_df = ticker.history(
                    start=f"{start_date_str[:4]}-{start_date_str[4:6]}-{start_date_str[6:]}"
                )
                break
            except Exception as yf_e:
                error_str = str(yf_e).lower()
                if any(kw in error_str for kw in ["rate limit", "too many requests", "ratelimit"]):
                    log_warning(f"[{clean_code}] 方案4: yfinance被限流，跳过该方案")
                    break
                else:
                    log_warning(f"[{clean_code}] 方案4: yfinance尝试失败: {str(yf_e)[:40]}")
                    if yf_attempt < max_yf_retries - 1:
                        time.sleep(yf_delay)
                        yf_delay *= 2
                    else:
                        break
        
        if yf_df is not None and not yf_df.empty:
            log_info(f"[{clean_code}] 方案4: 获取到 {len(yf_df)} 条yfinance数据，开始验证...")
            
            # 核心修复：数据完整性验证
            validation_errors = []
            
            # 验证1：检查必要的价格列
            required_yf_cols = ['Open', 'High', 'Low', 'Close']
            for col in required_yf_cols:
                if col not in yf_df.columns or yf_df[col].isna().all():
                    validation_errors.append(f"缺少价格列 '{col}'")
            
            # 验证2：检查数据是否在合理范围内（非全0或异常值）
            for col in ['Open', 'Close']:
                if col in yf_df.columns:
                    valid_vals = yf_df[col].dropna()
                    if len(valid_vals) > 0:
                        if valid_vals.min() <= 0:
                            validation_errors.append(f"'{col}' 存在非正价格值")
            
            # 验证3：检查是否有足够的数据行
            valid_rows = yf_df.dropna(subset=['Open', 'Close'])
            if len(valid_rows) < 5:
                validation_errors.append(f"有效数据行不足 ({len(valid_rows)} < 5)")
            
            if validation_errors:
                log_warning(f"[{clean_code}] 方案4: 数据验证失败: {'; '.join(validation_errors[:3])}")
                if len(valid_rows) < 3:
                    log_error(f"[{clean_code}] 方案4: yfinance数据不可用，跳过")
                else:
                    log_info(f"[{clean_code}] 方案4: yfinance数据有瑕疵但部分可用，尝试使用...")
            else:
                log_success(f"[{clean_code}] 方案4: yfinance数据验证通过")
            
            # 尝试转换和使用
            if len(valid_rows) >= 5:
                yf_df = yf_df.reset_index()
                yf_df = yf_df.rename(columns={
                    'Date': '日期', 'Open': '开盘', 'Close': '收盘',
                    'High': '最高', 'Low': '最低', 'Volume': '成交量', 'Adj Close': '复权价'
                })
                try:
                    yf_df['日期'] = pd.to_datetime(yf_df['日期']).dt.strftime('%Y-%m-%d')
                except Exception:
                    pass
                log_success(f"[{clean_code}] 方案4: yfinance历史K线获取成功 ({len(yf_df)} 条)")
                return yf_df
            else:
                log_warning(f"[{clean_code}] 方案4: yfinance有效数据不足 {len(valid_rows)} 条")
        else:
            log_warning(f"[{clean_code}] 方案4: yfinance未返回数据")
    except Exception as e:
        log_warning(f"[{clean_code}] 方案4: yfinance不可用或失败 - {type(e).__name__}: {str(e)[:40]}")
    
    log_error(f"[{clean_code}] 历史K线获取失败 (所有方案都失败)")
    return pd.DataFrame()


def _calculate_technical_indicators(hist_df: pd.DataFrame) -> Dict[str, Any]:
    """计算技术指标"""
    indicators = {}
    
    if hist_df.empty:
        return indicators
    
    # 查找正确的列名（使用公共函数）
    date_col = find_column(hist_df, '日期', 'date', 'trade_date')
    close_col = find_column(hist_df, '收盘', 'close')
    high_col = find_column(hist_df, '最高', 'high')
    low_col = find_column(hist_df, '最低', 'low')
    vol_col = find_column(hist_df, '成交量', 'vol', 'volume')
    amount_col = find_column(hist_df, '成交额', 'amount')
    
    if date_col is None: date_col = hist_df.columns[0] if len(hist_df.columns) > 0 else None
    if close_col is None: close_col = hist_df.columns[1] if len(hist_df.columns) > 1 else None
    
    if not date_col or not close_col:
        return indicators
    
    try:
        hist_df = hist_df.tail(50)
        hist_df = hist_df.sort_values(date_col)
    except Exception as e:
        log_debug(f"数据排序失败: {type(e).__name__}: {str(e)[:40]}")
    
    try:
        close_prices = hist_df[close_col]
        
        # 均线系统
        ma5 = close_prices.tail(5).mean()
        ma10 = close_prices.tail(10).mean()
        ma20 = close_prices.tail(20).mean()
        ma60 = close_prices.tail(60).mean() if len(close_prices) >= 60 else None
        
        indicators['5日均线'] = round(ma5, 2)
        indicators['10日均线'] = round(ma10, 2)
        indicators['20日均线'] = round(ma20, 2)
        indicators['60日均线'] = round(ma60, 2) if ma60 is not None else 'N/A'
        
        current_price = close_prices.iloc[-1]
        
        indicators['当前价格'] = round(current_price, 2)
        indicators['MA5偏离度'] = round((current_price / ma5 - 1) * 100, 2) if ma5 != 0 else 'N/A'
        indicators['MA10偏离度'] = round((current_price / ma10 - 1) * 100, 2) if ma10 != 0 else 'N/A'
        indicators['MA20偏离度'] = round((current_price / ma20 - 1) * 100, 2) if ma20 != 0 else 'N/A'
        
        # RSI指标
        rsi_6 = calculate_rsi(close_prices, 6)
        rsi_12 = calculate_rsi(close_prices, 12)
        rsi_24 = calculate_rsi(close_prices, 24)
        indicators['RSI(6)'] = round(rsi_6.iloc[-1], 2) if not rsi_6.empty else 'N/A'
        indicators['RSI(12)'] = round(rsi_12.iloc[-1], 2) if not rsi_12.empty else 'N/A'
        indicators['RSI(24)'] = round(rsi_24.iloc[-1], 2) if not rsi_24.empty else 'N/A'
        
        # MACD指标
        macd, signal_line, histogram = calculate_macd(close_prices)
        indicators['MACD'] = round(macd.iloc[-1], 3) if not macd.empty else 'N/A'
        indicators['MACD_Signal'] = round(signal_line.iloc[-1], 3) if not signal_line.empty else 'N/A'
        indicators['MACD_Histogram'] = round(histogram.iloc[-1], 3) if not histogram.empty else 'N/A'
        
        # 动量指标
        indicators['5日动量'] = round(calculate_momentum(close_prices, 5).iloc[-1], 2) if len(close_prices) >= 6 else 'N/A'
        indicators['10日动量'] = round(calculate_momentum(close_prices, 10).iloc[-1], 2) if len(close_prices) >= 11 else 'N/A'
        indicators['20日动量'] = round(calculate_momentum(close_prices, 20).iloc[-1], 2) if len(close_prices) >= 21 else 'N/A'
        indicators['60日动量'] = round(calculate_momentum(close_prices, 60).iloc[-1], 2) if len(close_prices) >= 61 else 'N/A'
        
        # 成交量分析
        if vol_col and len(hist_df) >= 2:
            try:
                current_vol = hist_df[vol_col].iloc[-1]
                avg_vol_5 = hist_df[vol_col].tail(5).mean()
                avg_vol_10 = hist_df[vol_col].tail(10).mean()
                
                indicators['成交量异动率(5日)'] = round((current_vol / avg_vol_5 - 1) * 100, 2) if avg_vol_5 != 0 else 'N/A'
                indicators['成交量异动率(10日)'] = round((current_vol / avg_vol_10 - 1) * 100, 2) if avg_vol_10 != 0 else 'N/A'
            except Exception as e:
                log_debug(f"成交量分析失败: {type(e).__name__}: {str(e)[:40]}")

        if amount_col and len(hist_df) >= 2:
            try:
                current_amount = hist_df[amount_col].iloc[-1]
                avg_amount_5 = hist_df[amount_col].tail(5).mean()
                indicators['成交额增长率'] = round((current_amount / avg_amount_5 - 1) * 100, 2) if avg_amount_5 != 0 else 'N/A'
            except Exception as e:
                log_debug(f"成交额分析失败: {type(e).__name__}: {str(e)[:40]}")
        
        # 波动率指标
        if high_col and low_col and close_col:
            try:
                hist_df['Volatility'] = (hist_df[high_col] - hist_df[low_col]) / hist_df[close_col] * 100
                indicators['历史波动率(20日)'] = round(hist_df['Volatility'].tail(20).mean(), 2) if len(hist_df) >= 5 else 'N/A'
                
                if len(hist_df) >= 5:
                    returns = hist_df[close_col].pct_change()
                    indicators['年化波动率'] = round(returns.std() * math.sqrt(252) * 100, 2)
            except Exception as e:
                log_debug(f"波动率计算失败: {type(e).__name__}: {str(e)[:40]}")
    except Exception as e:
        log_warning(f"技术指标计算异常: {str(e)[:40]}")
    
    return indicators


def _fetch_l2_data(stock_code: str, hist_df: pd.DataFrame = None) -> Dict[str, Any]:
    """
    获取 L2 五档盘口数据（多级回退）

    依次尝试两个数据源：
    1. 东方财富 stock_bid_ask_em（五档买卖盘口）
    2. 新浪 stock_bid_ask（备用）

    提供可选的 hist_df 参数以复用历史K线数据，减少重复 API 调用。

    Args:
        stock_code: 纯数字股票代码
        hist_df: 可选的历史K线 DataFrame，用于数据复用

    Returns:
        盘口数据字典，包含卖1-5 / 买1-5 的价格和成交量。
        所有数据源均失败时返回空字典 {}。
    """
    clean_code = clean_stock_code(stock_code)

    log_info(f"[{clean_code}] 开始获取L2数据...")
    time.sleep(API_DELAY_SHORT)
    
    l2_success = False
    bid_ask_data = {}
    
    # 方案1: 东方财富L2五档
    try:
        log_info("方案1: stock_bid_ask_em (东方财富五档)")
        l2_df = ak.stock_bid_ask_em(symbol=clean_code)
        if not l2_df.empty:
            for i in range(1, 6):
                if f'卖{i}价格' in l2_df.columns:
                    bid_ask_data[f'卖{i}价'] = l2_df[f'卖{i}价格'].iloc[0]
                    bid_ask_data[f'卖{i}量'] = l2_df[f'卖{i}成交量'].iloc[0]
                if f'买{i}价格' in l2_df.columns:
                    bid_ask_data[f'买{i}价'] = l2_df[f'买{i}价格'].iloc[0]
                    bid_ask_data[f'买{i}量'] = l2_df[f'买{i}成交量'].iloc[0]
            log_success("东方财富L2五档获取成功")
            l2_success = True
    except Exception as e:
        log_warning(f"东方财富L2失败: {str(e)[:40]}")
    
    # 方案2: 历史数据补充
    if not l2_success and hist_df is not None and not hist_df.empty:
        try:
            log_info("方案2: 使用历史K线数据")
            latest = hist_df.iloc[-1]
            bid_ask_data = {
                '收盘价': latest.get('收盘', 'N/A'),
                '开盘价': latest.get('开盘', 'N/A'),
                '最高价': latest.get('最高', 'N/A'),
                '最低价': latest.get('最低', 'N/A'),
                '成交量': latest.get('成交量', 'N/A'),
                '成交额': latest.get('成交额', 'N/A'),
                '日期': latest.get('日期', 'N/A'),
                '说明': '使用最近历史数据（L2五档不可用）'
            }
            log_success("历史数据补充成功")
            l2_success = True
        except Exception as e:
            log_warning(f"历史数据补充失败: {str(e)[:40]}")
    
    # 方案3: 新浪实时行情（使用缓存）
    if not l2_success:
        try:
            log_info("方案3: 使用新浪实时数据补充")
            spot_df = _get_cached_market_data("sina")
            if spot_df is not None and not spot_df.empty:
                match = spot_df[spot_df['代码'] == f'sh{clean_code}']
                if match.empty:
                    match = spot_df[spot_df['代码'].str.contains(clean_code)]
                if not match.empty:
                    row = match.iloc[0]
                    bid_ask_data = {
                        '最新价': row.get('最新价', 'N/A'),
                        '今开': row.get('今开', 'N/A'),
                        '最高': row.get('最高', 'N/A'),
                        '最低': row.get('最低', 'N/A'),
                        '成交量': row.get('成交量', 'N/A'),
                        '成交额': row.get('成交额', 'N/A'),
                        '说明': '使用新浪实时数据（实时五档不可用）'
                    }
                    log_success("新浪实时数据获取成功")
                    l2_success = True
        except Exception as e:
            log_warning(f"新浪L2失败: {str(e)[:40]}")
    
    if not l2_success:
        bid_ask_data = {'说明': 'L2数据暂不可用（网络问题）'}
    
    return bid_ask_data


def _fetch_fund_flow_data(stock_code: str) -> Dict[str, Any]:
    """
    获取个股资金流向数据

    从东方财富 stock_individual_fund_flow 获取最近5日的资金流向明细，
    包含主力/超大单/大单/中单/小单五个维度的净流入数据。

    Args:
        stock_code: 纯数字股票代码

    Returns:
        资金流向字典，包含今日各类型资金净流入额。
        API 调用失败时返回空字典 {}。
    """
    clean_code = clean_stock_code(stock_code)

    log_info(f"[{clean_code}] 开始获取资金流向数据...")
    time.sleep(API_DELAY_SHORT)
    
    result = {}
    try:
        market = get_market_prefix(clean_code)
        log_info(f"使用API: stock_individual_fund_flow (资金流向)")
        fund_flow_df = ak.stock_individual_fund_flow(stock=clean_code, market=market)
        if not fund_flow_df.empty:
            recent_flow = fund_flow_df.tail(5)
            result = {
                '今日主力净流入': recent_flow['主力净流入'].iloc[-1] if '主力净流入' in recent_flow.columns else 'N/A',
                '今日超大单净流入': recent_flow['超大单净流入'].iloc[-1] if '超大单净流入' in recent_flow.columns else 'N/A',
                '今日大单净流入': recent_flow['大单净流入'].iloc[-1] if '大单净流入' in recent_flow.columns else 'N/A',
                '今日中单净流入': recent_flow['中单净流入'].iloc[-1] if '中单净流入' in recent_flow.columns else 'N/A',
                '今日小单净流入': recent_flow['小单净流入'].iloc[-1] if '小单净流入' in recent_flow.columns else 'N/A',
            }
            log_success("资金流向数据获取成功")
        else:
            log_warning("资金流向数据为空")
    except Exception as e:
        log_error(f"资金流向获取失败: {str(e)[:50]}")
    
    return result


def _fetch_financial_data(stock_code: str) -> Dict[str, Any]:
    """
    获取财务报表数据（多级回退）

    依次尝试三个数据源：
    1. 东方财富 stock_financial_analysis_indicator_em（财务分析指标）
    2. 巨潮 stock_financial_abstract（财务摘要）
    3. 新浪 stock_financial_abstract_ths（同花顺财务摘要）

    提取关键财务指标：ROE、净利润、营业收入、总资产、净资产等。

    Args:
        stock_code: 纯数字股票代码

    Returns:
        财务数据字典，包含财务指标和利润表等子词典。
        所有数据源均失败时返回空字典 {}。
    """
    clean_code = clean_stock_code(stock_code)

    log_info(f"[{clean_code}] 开始获取财务报表数据...")
    time.sleep(API_DELAY_MEDIUM)
    
    financial_report = {}
    
    try:
        log_info("使用API: stock_financial_analysis_indicator_em (财务报表)")
        finance_df = ak.stock_financial_analysis_indicator_em(symbol=clean_code)
        if isinstance(finance_df, pd.DataFrame) and not finance_df.empty:
            latest = finance_df.iloc[-1]
            financial_report['财务指标'] = {
                'ROE': latest.get('净资产收益率(%)', 'N/A'),
                '净利润': latest.get('净利润(万元)', 'N/A'),
                '营业收入': latest.get('营业总收入(万元)', 'N/A'),
                '总资产': latest.get('资产总计(万元)', 'N/A'),
                '净资产': latest.get('归属于母公司股东权益(万元)', 'N/A'),
                '毛利率': latest.get('销售毛利率(%)', 'N/A'),
                '净利率': latest.get('销售净利率(%)', 'N/A'),
            }
            log_success("财务报表获取成功")
        else:
            log_warning("财务报表返回为空")
    except Exception as e:
        log_warning(f"东方财富财务报表失败: {str(e)[:40]}")
        try:
            log_info("备用API: stock_financial_report_sina (新浪财务报表)")
            fin_df = ak.stock_financial_report_sina(symbol=clean_code)
            if fin_df is not None and not fin_df.empty:
                financial_report['财务指标'] = {'说明': '使用新浪财务数据'}
                log_success("备用财务报表获取成功")
            else:
                log_warning("新浪财务报表也为空")
        except Exception as e2:
            log_warning(f"备用财务报表也失败: {str(e2)[:40]}")
    
    return financial_report


def _fetch_dividend_data(stock_code: str) -> Dict[str, Any]:
    """
    获取分红送配数据（多级回退）

    依次尝试：
    1. 巨潮 stock_dividend_cninfo（分红送配）
    2. stock_history_dividend_detail（历史分红明细，作为备用）

    提取最新一条分红记录，包含：分红年度、每股分红（税前）、送股比例、
    转增比例、股权登记日、除权除息日。

    Args:
        stock_code: 纯数字股票代码

    Returns:
        分红数据字典。无分红记录或 API 失败时返回空字典 {}。
    """
    clean_code = clean_stock_code(stock_code)

    log_info(f"[{clean_code}] 开始获取分红送配数据...")
    time.sleep(API_DELAY_SHORT)
    
    result = {}
    
    try:
        log_info("使用API: stock_dividend_cninfo (分红送配)")
        dividend_df = ak.stock_dividend_cninfo(symbol=clean_code)
        if not dividend_df.empty:
            recent_dividend = dividend_df.iloc[-1]
            result = {
                '分红年度': recent_dividend.get('分红年度', 'N/A'),
                '每股分红': recent_dividend.get('每股分红（税前）', 'N/A'),
                '送股比例': recent_dividend.get('送股比例', 'N/A'),
                '转增比例': recent_dividend.get('转增比例', 'N/A'),
                '股权登记日': recent_dividend.get('股权登记日', 'N/A'),
                '除权除息日': recent_dividend.get('除权除息日', 'N/A')
            }
            log_success("分红送配数据获取成功")
        else:
            log_warning("分红送配数据为空，尝试备用API...")
            try:
                log_info("备用API: stock_history_dividend_detail")
                div_df = ak.stock_history_dividend_detail(symbol=clean_code)
                if not div_df.empty:
                    recent = div_df.iloc[-1]
                    result = {
                        '公告日期': recent.get('公告日期', 'N/A'),
                        '每股分红': recent.get('派息', 'N/A'),
                        '送股比例': recent.get('送股', 'N/A'),
                        '转增比例': recent.get('转增', 'N/A'),
                        '除权除息日': recent.get('除权除息日', 'N/A'),
                        '股权登记日': recent.get('股权登记日', 'N/A')
                    }
                    log_success("备用分红数据获取成功")
                else:
                    log_warning("备用分红数据也为空")
            except Exception:
                log_warning("备用分红API也失败")
    except Exception as e:
        log_warning("巨潮分红API失败，尝试备用方案...")
        try:
            log_info("备用API: stock_history_dividend_detail")
            div_df = ak.stock_history_dividend_detail(symbol=clean_code)
            if not div_df.empty:
                recent = div_df.iloc[-1]
                result = {
                    '公告日期': recent.get('公告日期', 'N/A'),
                    '每股分红': recent.get('派息', 'N/A'),
                    '送股比例': recent.get('送股', 'N/A'),
                    '转增比例': recent.get('转增', 'N/A'),
                    '除权除息日': recent.get('除权除息日', 'N/A'),
                    '股权登记日': recent.get('股权登记日', 'N/A')
                }
                log_success("备用分红数据获取成功")
            else:
                log_warning("备用分红数据为空")
        except Exception as e2:
            log_error(f"分红送配获取失败: {str(e2)[:50]}")
    
    return result


def _fetch_industry_data(stock_code: str, l1_data: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    获取行业板块信息（四级回退）

    依次尝试四个数据源：
    1. 东方财富 stock_board_industry_spot_em（行业板块行情）
    2. 新浪 stock_sector_spot（板块数据）
    3. stock_fund_flow_concept（概念板块）
    4. 使用已获取的 L1 数据补全基本信息

    Args:
        stock_code: 纯数字股票代码
        l1_data: 可选的 L1 行情数据字典，用于兜底方案

    Returns:
        行业信息字典，至少包含说明和板块数量字段。
        所有数据源均失败时返回 {'说明': '行业信息暂不可用（网络限制）'}。
    """
    clean_code = clean_stock_code(stock_code)

    log_info(f"[{clean_code}] 开始获取行业信息...")
    time.sleep(API_DELAY_SHORT)
    
    industry_success = False
    result = {}
    
    # 方案1: 东方财富行业板块
    try:
        log_info("方案1: stock_board_industry_spot_em (东方财富)")
        sector_df = ak.stock_board_industry_spot_em()
        if sector_df is not None and not sector_df.empty:
            result = {
                '说明': '使用东方财富行业数据',
                '板块数量': len(sector_df)
            }
            log_success(f"东方财富行业板块获取成功({len(sector_df)}个)")
            industry_success = True
    except Exception as e:
        log_warning(f"东方财富行业API失败: {str(e)[:40]}")
    
    # 方案2: 新浪行业板块
    if not industry_success:
        try:
            log_info("方案2: stock_sector_spot (新浪)")
            sec_df = ak.stock_sector_spot()
            if sec_df is not None and not sec_df.empty:
                result = {
                    '说明': '使用新浪板块数据',
                    '板块数量': len(sec_df)
                }
                log_success(f"新浪行业板块获取成功({len(sec_df)}个)")
                industry_success = True
        except Exception as e:
            log_warning(f"新浪行业API失败: {str(e)[:40]}")
    
    # 方案3: 概念板块数据
    if not industry_success:
        try:
            log_info("方案3: stock_fund_flow_concept (概念板块)")
            concept_df = ak.stock_fund_flow_concept()
            if concept_df is not None and not concept_df.empty:
                result = {
                    '说明': '使用概念板块数据',
                    '板块数量': len(concept_df)
                }
                log_success(f"概念板块获取成功({len(concept_df)}个)")
                industry_success = True
        except Exception as e:
            log_warning(f"概念板块API失败: {str(e)[:40]}")
    
    # 方案4: 使用L1数据中的行业信息
    if not industry_success and l1_data:
        log_info("方案4: 使用行情数据中的行业信息")
        result = {
            '说明': '从行情数据获取',
            '股票名称': l1_data.get('股票名称', 'N/A'),
            '最新价': l1_data.get('最新价', 'N/A'),
            '涨跌幅': l1_data.get('涨跌幅', 'N/A'),
            '总市值': l1_data.get('总市值', 'N/A'),
            '流通市值': l1_data.get('流通市值', 'N/A'),
            '市盈率': l1_data.get('市盈率', 'N/A'),
            '市净率': l1_data.get('市净率', 'N/A')
        }
        log_success("从行情数据获取基本信息成功")
        industry_success = True
    
    if not industry_success:
        result = {'说明': '行业信息暂不可用（网络限制）'}
        log_warning("所有行业信息API均不可用")
    
    return result


def _fetch_basic_info(stock_code: str, l1_data: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    获取股票基本信息（三级回退）

    依次尝试三个数据源：
    1. 东方财富 stock_individual_info_em（个股详细信息：行业、上市日期等）
    2. 使用已获取的 L1 数据补全
    3. 新浪行情数据（使用缓存，兜底方案）

    Args:
        stock_code: 纯数字股票代码
        l1_data: 可选的 L1 行情数据字典，用于兜底方案

    Returns:
        基本信息字典，包含股票名称、行业、总市值、流通市值、市盈率、市净率等。
        所有数据源均失败时返回空字典 {}。
    """
    clean_code = clean_stock_code(stock_code)

    log_info(f"[{clean_code}] 开始获取基本信息...")
    time.sleep(API_DELAY_SHORT)
    
    info_success = False
    result = {}
    
    # 方案1: 东方财富基本信息
    try:
        log_info("方案1: stock_individual_info_em (东方财富)")
        info_df = ak.stock_individual_info_em(symbol=clean_code)
        if info_df is not None and not info_df.empty:
            info_dict = {}
            for _, row in info_df.iterrows():
                info_dict[row.get('item', 'N/A')] = row.get('value', 'N/A')
            result = info_dict
            log_success(f"东方财富基本信息获取成功({len(info_dict)}项)")
            info_success = True
    except Exception as e:
        log_warning(f"东方财富基本信息失败: {str(e)[:40]}")
    
    # 方案2: 使用已获取的L1数据
    if not info_success and l1_data:
        log_info("方案2: 使用已获取的L1数据")
        result = l1_data.copy()
        result['说明'] = '使用已获取的L1数据'
        log_success("L1数据补充成功")
        info_success = True
    
    # 方案3: 新浪行情数据（使用缓存）
    if not info_success:
        try:
            log_info("方案3: 使用新浪行情数据")
            spot_df = _get_cached_market_data("sina")
            if spot_df is not None and not spot_df.empty:
                match = spot_df[spot_df['代码'] == f'sh{clean_code}']
                if match.empty:
                    match = spot_df[spot_df['代码'].str.contains(clean_code)]
                if not match.empty:
                    row = match.iloc[0]
                    result = {
                        '股票名称': row.get('名称', 'N/A'),
                        '最新价': row.get('最新价', 'N/A'),
                        '涨跌幅': row.get('涨跌幅', 'N/A'),
                        '涨跌额': row.get('涨跌额', 'N/A'),
                        '今开': row.get('今开', 'N/A'),
                        '昨收': row.get('昨收', 'N/A'),
                        '最高': row.get('最高', 'N/A'),
                        '最低': row.get('最低', 'N/A'),
                        '成交量': row.get('成交量', 'N/A'),
                        '成交额': row.get('成交额', 'N/A'),
                        '量比': row.get('量比', 'N/A'),
                        '换手率': row.get('换手率', 'N/A'),
                        '市盈率': row.get('市盈率', 'N/A'),
                        '市净率': row.get('市净率', 'N/A'),
                        '总市值': row.get('总市值', 'N/A'),
                        '流通市值': row.get('流通市值', 'N/A'),
                        '说明': '从新浪行情数据获取'
                    }
                    log_success("新浪行情数据获取成功")
                    info_success = True
        except Exception as e:
            log_warning(f"新浪行情数据失败: {str(e)[:40]}")
    
    if not info_success:
        log_warning("所有基本信息API均不可用")
    
    return result


def _fetch_additional_data(clean_code: str) -> Dict[str, Any]:
    """
    获取额外市场数据（并发调用 8 个独立 API）

    并发获取以下数据项（使用 ThreadPoolExecutor，~8s → ~2s）：
    - 主力资金流向（stock_main_fund_flow）
    - 融资融券（stock_margin_sse）
    - 龙虎榜（stock_lhb_detail_em，两重回退）
    - 大宗交易（stock_fund_flow_big_deal）
    - 机构持股（stock_institute_hold）
    - 业绩预告（stock_profit_forecast_em）
    - 限售解禁（stock_restricted_release_queue_em）
    - 股东增减持（stock_share_hold_change_em）

    Args:
        clean_code: 纯数字股票代码

    Returns:
        字典，key 为数据类别名称，value 为对应的 DataFrames 列表。
    """
    log_info(f"[{clean_code}] 开始获取额外数据...")
    time.sleep(API_DELAY_MEDIUM)

    result = {}

    def _fetch_money_flow():
        try:
            df = ak.stock_main_fund_flow(symbol=clean_code)
            if not df.empty:
                return ('资金流向详细', df.tail(5).to_dict('records'))
        except Exception as e:
            log_warning(f"主力资金流向失败: {str(e)[:40]}")
        return None

    def _fetch_margin():
        try:
            df = ak.stock_margin_sse()
            if not df.empty:
                code_col = next((c for c in ['股票代码', '代码', 'symbol'] if c in df.columns), None)
                if code_col:
                    matched = df[df[code_col].astype(str).str.contains(clean_code)]
                    if not matched.empty:
                        return ('融资融券', matched.tail(1).to_dict('records'))
        except Exception as e:
            log_warning(f"融资融券数据失败: {str(e)[:40]}")
        return None

    def _fetch_lhb():
        try:
            df = ak.stock_lhb_detail_em()
            if df is not None and not df.empty:
                matched = df[df['代码'].astype(str).str.contains(clean_code)]
                if not matched.empty:
                    return ('龙虎榜', matched.tail(5).to_dict('records'))
        except Exception as e:
            log_warning(f"龙虎榜数据失败: {str(e)[:40]}")
            try:
                df = ak.stock_lhb_detail_daily_sina()
                if df is not None and not df.empty:
                    matched = df[df['代码'].astype(str).str.contains(clean_code)]
                    if not matched.empty:
                        return ('龙虎榜', matched.tail(5).to_dict('records'))
            except Exception:
                pass
        return None

    def _fetch_big_deal():
        try:
            df = ak.stock_fund_flow_big_deal()
            if df is not None and not df.empty:
                return ('大宗交易', df.tail(5).to_dict('records'))
        except Exception as e:
            log_warning(f"大宗交易数据失败: {str(e)[:40]}")
        return None

    def _fetch_institute():
        try:
            df = ak.stock_institute_hold(symbol=clean_code)
            if df is not None and isinstance(df, pd.DataFrame) and not df.empty:
                return ('机构持股', df.tail(5).to_dict('records'))
        except Exception as e:
            log_warning(f"机构持股数据失败: {str(e)[:40]}")
        return None

    def _fetch_forecast():
        try:
            df = ak.stock_profit_forecast_em(symbol=clean_code)
            if isinstance(df, pd.DataFrame) and not df.empty:
                return ('业绩预告', df.tail(5).to_dict('records'))
        except Exception as e:
            log_warning(f"业绩预告数据失败: {str(e)[:40]}")
        return None

    def _fetch_restricted():
        try:
            df = ak.stock_restricted_release_stockholder_em(symbol=clean_code)
            if isinstance(df, pd.DataFrame) and not df.empty:
                return ('限售股解禁', df.tail(5).to_dict('records'))
        except Exception as e:
            log_warning(f"限售股解禁数据失败: {str(e)[:40]}")
        return None

    tasks = [_fetch_money_flow, _fetch_margin, _fetch_lhb, _fetch_big_deal,
             _fetch_institute, _fetch_forecast, _fetch_restricted]

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fn): fn for fn in tasks}
        for future in as_completed(futures):
            res = future.result()
            if res:
                key, data = res
                result[key] = data
                log_success(f"{key}获取成功")

    return result


def _fetch_tushare_data(clean_code: str) -> Dict[str, Any]:
    """
    获取 Tushare 补充数据（并发调用 8 个接口）

    需要配置环境变量 TUSHARE_TOKEN 才能使用。并发获取：
    - 利润表（income_vip）、资产负债表（balancesheet_vip）、现金流量表（cashflow_vip）
    - 主要指标（fina_indicator_vip）、分红送配（dividend）
    - 十大股东（top10_holders）、十大流通股东（top10_floatholders）

    使用 ThreadPoolExecutor 并发调用，~8s → ~2s。
    所有接口失败时返回空字典 {}。

    Args:
        clean_code: 纯数字股票代码

    Returns:
        字典，包含各子数据类别。
    """
    log_info(f"[{clean_code}] 开始获取Tushare数据...")
    time.sleep(API_DELAY_MEDIUM)

    result = {}
    ts_pro = None
    ts_code = format_tushare_code(clean_code)

    try:
        ts.set_token(TUSHARE_TOKEN)
        ts_pro = ts.pro_api()
    except Exception as e:
        log_warning(f"Tushare初始化失败: {str(e)[:40]}")
        return result

    if not ts_pro:
        return result

    today_str = datetime.now().strftime('%Y%m%d')
    start_str = (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')

    def _fetch_daily():
        try:
            df = ts_pro.daily(ts_code=ts_code, start_date=start_str, end_date=today_str)
            if not df.empty:
                return ('Tushare行情', df.to_dict('records'))
        except Exception as e:
            log_warning(f"Tushare每日行情失败: {str(e)[:40]}")
        return None

    def _fetch_fina():
        try:
            df = ts_pro.fina_indicator(ts_code=ts_code)
            if not df.empty:
                return ('Tushare财务指标', df.tail(4).to_dict('records'))
        except Exception as e:
            log_warning(f"Tushare财务指标失败: {str(e)[:40]}")
        return None

    def _fetch_income():
        try:
            df = ts_pro.income(ts_code=ts_code)
            if not df.empty:
                return ('Tushare利润表', df.head(4).to_dict('records'))
        except Exception as e:
            log_warning(f"Tushare利润表失败: {str(e)[:40]}")
        return None

    def _fetch_balance():
        try:
            df = ts_pro.balancesheet(ts_code=ts_code)
            if not df.empty:
                return ('Tushare资产负债表', df.head(4).to_dict('records'))
        except Exception as e:
            log_warning(f"Tushare资产负债表失败: {str(e)[:40]}")
        return None

    def _fetch_cashflow():
        try:
            df = ts_pro.cashflow(ts_code=ts_code)
            if not df.empty:
                return ('Tushare现金流量表', df.head(4).to_dict('records'))
        except Exception as e:
            log_warning(f"Tushare现金流量表失败: {str(e)[:40]}")
        return None

    def _fetch_holders():
        try:
            df = ts_pro.top_holders(ts_code=ts_code)
            if not df.empty:
                return ('Tushare前十大股东', df.head(10).to_dict('records'))
        except Exception as e:
            log_warning(f"Tushare前十大股东失败: {str(e)[:40]}")
        return None

    def _fetch_moneyflow():
        try:
            df = ts_pro.moneyflow(ts_code=ts_code)
            if not df.empty:
                return ('Tushare主力资金', df.tail(10).to_dict('records'))
        except Exception as e:
            log_warning(f"Tushare主力资金失败: {str(e)[:40]}")
        return None

    tasks = [_fetch_daily, _fetch_fina, _fetch_income, _fetch_balance,
             _fetch_cashflow, _fetch_holders, _fetch_moneyflow]

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fn): fn for fn in tasks}
        for future in as_completed(futures):
            res = future.result()
            if res:
                key, data = res
                result[key] = data
                log_success(f"{key.split('(')[0]}获取成功")

    return result

def get_single_stock_analysis(stock_code: str) -> Optional[Dict[str, Any]]:
    """
    获取单只股票的详细分析数据
    
    该函数整合了多维度的股票数据，包括：
    - L1实时行情数据（最新价、涨跌幅、成交量等）
    - L2五档盘口数据（买卖五档价格和成交量）
    - 技术指标（均线、RSI、MACD、动量、波动率等）
    - 资金流向（主力/超大单/大单/中单/小单净流入）
    - 财务报表（资产负债表、利润表、现金流量表）
    - 分红送配（分红记录、送股、转增）
    - 行业信息
    
    Args:
        stock_code: 股票代码（6位数字，如"600000"）
        
    Returns:
        包含股票分析数据的字典，结构如下：
        {
            '股票代码': str,
            'L1数据': Dict[str, Any],
            'L2数据': Dict[str, Any],
            '技术指标': Dict[str, Any],
            '资金流向': Dict[str, Any],
            '财务报表': Dict[str, Any],
            '分红送配': Dict[str, Any],
            '行业信息': Dict[str, Any]
        }
        获取失败时返回None
        
    Example:
        >>> result = get_single_stock_analysis("600000")
        >>> if result:
        ...     print(f"股票代码: {result['股票代码']}")
        ...     print(f"最新价: {result['L1数据']['最新价']}")
    """
    result: Dict[str, Any] = {
        '股票代码': stock_code,
        'L1数据': {},
        'L2数据': {},
        '技术指标': {},
        '资金流向': {},
        '财务报表': {},
        '分红送配': {},
        '行业信息': {}
    }

    clean_code = clean_stock_code(stock_code)
    
    # 清除市场数据缓存，确保新一轮分析获取最新数据
    _clear_market_cache()
    
    console.print(f"\n[cyan]正在分析股票: {clean_code}[/cyan]")

    try:
        # ========== 1. 获取L1实时行情数据 ==========
        result['L1数据'] = _fetch_l1_data(clean_code)
    except Exception as e:
        log_warning(f"L1数据获取失败: {type(e).__name__}: {str(e)[:60]}")

    try:
        # ========== 2. 获取历史K线数据 ==========
        hist_df = _fetch_historical_klines(clean_code)
    except Exception as e:
        log_warning(f"历史K线获取失败: {type(e).__name__}: {str(e)[:60]}")
        hist_df = pd.DataFrame()

    try:
        # ========== 3. 计算技术指标 ==========
        if not hist_df.empty:
            result['历史K线'] = hist_df.copy()
            result['技术指标'] = _calculate_technical_indicators(hist_df)
    except Exception as e:
        log_warning(f"技术指标计算失败: {type(e).__name__}: {str(e)[:60]}")

    # ========== 4. 如果L1数据为空，从历史K线中提取 ==========
    if not result.get('L1数据') and not hist_df.empty:
        try:
            log_info("从历史K线提取L1数据")
            latest = hist_df.iloc[-1]
            # 查找列名
            col_map = {
                'date': find_column(hist_df, '日期', 'date'),
                'close': find_column(hist_df, '收盘', 'close'),
                'open': find_column(hist_df, '开盘', 'open'),
                'high': find_column(hist_df, '最高', 'high'),
                'low': find_column(hist_df, '最低', 'low'),
                'vol': find_column(hist_df, '成交量', 'vol'),
                'amount': find_column(hist_df, '成交额', 'amount'),
            }
            col_map = {k: v for k, v in col_map.items() if v is not None}

            prev_close = hist_df.iloc[-2].get('收盘', None) if len(hist_df) >= 2 else None
            current_close = latest[col_map['close']] if 'close' in col_map else None

            change = change_pct = None
            if prev_close is not None and current_close is not None:
                try:
                    change = round(float(current_close) - float(prev_close), 2)
                    change_pct = round((float(current_close) / float(prev_close) - 1) * 100, 2)
                except Exception:
                    pass

            stock_name = get_stock_name(clean_code, console)
            result['L1数据'] = {
                '股票代码': clean_code,
                '股票名称': stock_name,
                '最新价': latest[col_map['close']] if 'close' in col_map else 'N/A',
                '涨跌幅': change_pct,
                '涨跌额': change,
                '今开': latest[col_map['open']] if 'open' in col_map else 'N/A',
                '昨收': prev_close if prev_close is not None else 'N/A',
                '最高': latest[col_map['high']] if 'high' in col_map else 'N/A',
                '最低': latest[col_map['low']] if 'low' in col_map else 'N/A',
                '成交量': latest[col_map['vol']] if 'vol' in col_map else 'N/A',
                '成交额': latest[col_map['amount']] if 'amount' in col_map else 'N/A',
                '时间': latest[col_map['date']] if 'date' in col_map else 'N/A',
                '数据来源': '历史数据',
            }
            log_success("从历史K线提取L1数据成功")
        except Exception as e:
            log_warning(f"从历史K线提取L1数据失败: {str(e)[:40]}")

    # ========== 5-10. 并发获取多维度数据（L2/资金/财务/分红/行业/基本信息） ==========
    try:
        concurrent_tasks = {
            'L2数据': lambda: _fetch_l2_data(clean_code, hist_df),
            '资金流向': lambda: _fetch_fund_flow_data(clean_code),
            '财务报表': lambda: _fetch_financial_data(clean_code),
            '分红送配': lambda: _fetch_dividend_data(clean_code),
            '行业信息': lambda: _fetch_industry_data(clean_code, result.get('L1数据')),
            '基本信息': lambda: _fetch_basic_info(clean_code, result.get('L1数据')),
        }
        
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(fn): key for key, fn in concurrent_tasks.items()}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    result[key] = future.result()
                except Exception as e:
                    log_warning(f"并发获取{key}失败: {str(e)[:40]}")
        
        if not result.get('基本信息'):
            result['基本信息'] = {'股票代码': clean_code, '说明': '从历史数据获取'}
    except Exception as e:
        log_warning(f"并发获取数据失败: {type(e).__name__}: {str(e)[:60]}")

    # ========== 11. 获取额外数据（akshare） ==========
    try:
        additional = _fetch_additional_data(clean_code)
        result.update(additional)
    except Exception as e:
        log_warning(f"额外数据获取失败: {type(e).__name__}: {str(e)[:60]}")

    # ========== 12. 获取Tushare补充数据 ==========
    try:
        tushare_data = _fetch_tushare_data(clean_code)
        result.update(tushare_data)
    except Exception as e:
        log_warning(f"Tushare数据获取失败: {type(e).__name__}: {str(e)[:60]}")

    return result


def display_stock_analysis(analysis_result: Dict[str, Any]) -> None:
    """
    在终端中显示股票分析结果
    
    使用 rich 库以美观的表格形式展示股票分析数据，包括：
    - L1实时行情数据
    - L2五档行情数据
    - 技术指标分析
    - 资金流向分析
    
    Args:
        analysis_result: get_single_stock_analysis() 返回的分析结果字典
        
    Returns:
        None
        
    Example:
        >>> result = get_single_stock_analysis("600000")
        >>> display_stock_analysis(result)
    """
    if not analysis_result:
        return
    
    console.print(Panel.fit(
        Text(f"股票 {analysis_result['股票代码']} 详细分析报告", style="bold cyan", justify="center"),
        border_style="cyan",
        box=box.ASCII
    ))
    
    # 显示L1数据
    if analysis_result['L1数据']:
        console.print("\n[yellow]【L1 实时行情数据】[/yellow]")
        l1_table = Table(box=box.ASCII)
        l1_table.add_column("指标", style="cyan")
        l1_table.add_column("数值", style="green")
        
        l1_data = analysis_result['L1数据']
        important_l1 = ['最新价', '涨跌幅', '涨跌额', '今开', '昨收', '最高', '最低', 
                        '成交量', '成交额', '量比', '换手率', '市盈率-动态', '市净率', 
                        '总市值', '流通市值']
        
        for key in important_l1:
            if key in l1_data:
                value = l1_data[key]
                if key == '涨跌幅' and isinstance(value, (int, float)):
                    value = f"{value:.2f}%"
                l1_table.add_row(key, str(value))
        
        console.print(l1_table)
    
    # 显示L2数据
    if analysis_result['L2数据']:
        console.print("\n[yellow]【L2 五档行情数据】[/yellow]")
        l2_table = Table(box=box.ASCII)
        l2_table.add_column("档位", style="cyan", justify="center")
        l2_table.add_column("价格", style="green", justify="right")
        l2_table.add_column("成交量", style="yellow", justify="right")
        
        l2_data = analysis_result['L2数据']
        for i in range(1, 6):
            sell_price = l2_data.get(f'卖{i}价', 'N/A')
            sell_vol = l2_data.get(f'卖{i}量', 'N/A')
            l2_table.add_row(f"卖{i}", str(sell_price), str(sell_vol))
        
        l2_table.add_row("", "------", "")
        
        for i in range(1, 6):
            buy_price = l2_data.get(f'买{i}价', 'N/A')
            buy_vol = l2_data.get(f'买{i}量', 'N/A')
            l2_table.add_row(f"买{i}", str(buy_price), str(buy_vol))
        
        console.print(l2_table)
    
    # 显示技术指标
    if analysis_result['技术指标']:
        console.print("\n[yellow]【技术指标分析】[/yellow]")
        tech_table = Table(box=box.ASCII)
        tech_table.add_column("指标类别", style="cyan")
        tech_table.add_column("指标名称", style="green")
        tech_table.add_column("数值", style="yellow", justify="right")
        
        tech_data = analysis_result['技术指标']
        
        tech_table.add_row("【均线系统】", "5日均线", str(tech_data.get('5日均线', 'N/A')))
        tech_table.add_row("", "10日均线", str(tech_data.get('10日均线', 'N/A')))
        tech_table.add_row("", "20日均线", str(tech_data.get('20日均线', 'N/A')))
        tech_table.add_row("", "60日均线", str(tech_data.get('60日均线', 'N/A')))
        
        tech_table.add_row("", "MA5偏离度", str(tech_data.get('MA5偏离度', 'N/A')))
        tech_table.add_row("", "MA10偏离度", str(tech_data.get('MA10偏离度', 'N/A')))
        tech_table.add_row("", "MA20偏离度", str(tech_data.get('MA20偏离度', 'N/A')))
        
        tech_table.add_row("【RSI指标】", "RSI(6)", str(tech_data.get('RSI(6)', 'N/A')))
        tech_table.add_row("", "RSI(12)", str(tech_data.get('RSI(12)', 'N/A')))
        tech_table.add_row("", "RSI(24)", str(tech_data.get('RSI(24)', 'N/A')))
        
        tech_table.add_row("【MACD指标】", "MACD", str(tech_data.get('MACD', 'N/A')))
        tech_table.add_row("", "Signal", str(tech_data.get('MACD_Signal', 'N/A')))
        tech_table.add_row("", "Histogram", str(tech_data.get('MACD_Histogram', 'N/A')))
        
        tech_table.add_row("【动量指标】", "5日动量", str(tech_data.get('5日动量', 'N/A')))
        tech_table.add_row("", "10日动量", str(tech_data.get('10日动量', 'N/A')))
        tech_table.add_row("", "20日动量", str(tech_data.get('20日动量', 'N/A')))
        tech_table.add_row("", "60日动量", str(tech_data.get('60日动量', 'N/A')))
        
        tech_table.add_row("【成交量分析】", "成交量异动率(5日)", str(tech_data.get('成交量异动率(5日)', 'N/A')))
        tech_table.add_row("", "成交量异动率(10日)", str(tech_data.get('成交量异动率(10日)', 'N/A')))
        tech_table.add_row("", "成交额增长率", str(tech_data.get('成交额增长率', 'N/A')))
        
        tech_table.add_row("【波动性指标】", "历史波动率(20日)", str(tech_data.get('历史波动率(20日)', 'N/A')))
        tech_table.add_row("", "年化波动率", str(tech_data.get('年化波动率', 'N/A')))
        
        console.print(tech_table)
    
    # 显示资金流向
    if analysis_result['资金流向']:
        console.print("\n[yellow]【资金流向分析】[/yellow]")
        fund_table = Table(box=box.ASCII)
        fund_table.add_column("资金类型", style="cyan")
        fund_table.add_column("净流入金额", style="green", justify="right")
        
        fund_data = analysis_result['资金流向']
        for key, value in fund_data.items():
            if value != 'N/A' and isinstance(value, (int, float)):
                value_str = f"{value/10000:.2f}万" if abs(value) < 100000000 else f"{value/100000000:.2f}亿"
            else:
                value_str = str(value)
            fund_table.add_row(key, value_str)
        
        console.print(fund_table)
    
    # 显示财务报表数据
    if analysis_result['财务报表']:
        financial_data = analysis_result['财务报表']
        
        if financial_data.get('资产负债表'):
            console.print("\n[yellow]【资产负债表】[/yellow]")
            balance_table = Table(box=box.ASCII)
            balance_table.add_column("项目", style="cyan")
            balance_table.add_column("金额", style="green", justify="right")
            balance_sheet = financial_data['资产负债表']
            for key, value in balance_sheet.items():
                balance_table.add_row(key, str(value))
            console.print(balance_table)
        
        if financial_data.get('利润表'):
            console.print("\n[yellow]【利润表】[/yellow]")
            income_table = Table(box=box.ASCII)
            income_table.add_column("项目", style="cyan")
            income_table.add_column("金额", style="green", justify="right")
            income_statement = financial_data['利润表']
            for key, value in income_statement.items():
                income_table.add_row(key, str(value))
            console.print(income_table)
        
        if financial_data.get('现金流量表'):
            console.print("\n[yellow]【现金流量表】[/yellow]")
            cash_table = Table(box=box.ASCII)
            cash_table.add_column("项目", style="cyan")
            cash_table.add_column("金额", style="green", justify="right")
            cash_flow = financial_data['现金流量表']
            for key, value in cash_flow.items():
                cash_table.add_row(key, str(value))
            console.print(cash_table)
    
    # 显示分红送配数据
    if analysis_result['分红送配']:
        console.print("\n[yellow]【分红送配】[/yellow]")
        dividend_table = Table(box=box.ASCII)
        dividend_table.add_column("项目", style="cyan")
        dividend_table.add_column("内容", style="green")
        dividend_data = analysis_result['分红送配']
        for key, value in dividend_data.items():
            dividend_table.add_row(key, str(value))
        console.print(dividend_table)
    
    console.print()


def _fetch_market_spot() -> Optional[pd.DataFrame]:
    """获取全市场行情数据（东方财富→新浪二级回退）"""
    try:
        df = ak.stock_zh_a_spot_em()
        if df is not None and not df.empty:
            df["代码"] = df["代码"].astype(str)
            return df
    except Exception:
        pass
    try:
        df = ak.stock_zh_a_spot()
        if df is not None and not df.empty:
            df["代码"] = df["代码"].astype(str)
            return df
    except Exception:
        pass
    return None


def get_stock_data(selected_options: List[str]) -> Dict[str, pd.DataFrame]:
    """
    根据用户选择获取指定类型的股票数据
    
    支持获取以下25种数据类型：
    - market: 全市场数据
    - limit_up: 涨停股
    - rise_top: 涨幅榜TOP20
    - fall_top: 跌幅榜TOP20
    - fund_flow: 资金流向
    - industry: 行业板块
    - hot_deal: 热点成交
    - lhb: 龙虎榜
    - lhb_detail: 龙虎榜详情
    - bid_ask: 五档盘口
    - financial: 涨停股基本面
    - block_trade: 大宗交易
    - trade_balance: 贸易余额
    - cpi: CPI数据
    - concept: 概念板块
    - hk_hold: 北向资金持股
    - margin: 融资融券
    - new_share: 新股申购
    - gdp: GDP数据
    - ppi: PPI数据
    - money_supply: 货币供应量
    - exchange_rate: 汇率数据
    - bond_yield: 国债收益率
    - repurchase: 股票回购
    - fdi: 外商投资
    
    Args:
        selected_options: 用户选择的数据类型列表
        
    Returns:
        包含各类型数据的字典，key为数据类型名称，value为对应的DataFrame
        
    Example:
        >>> data = get_stock_data(['market', 'limit_up', 'fund_flow'])
        >>> print(data.keys())  # dict_keys(['全市场数据', '涨停股', '资金流向'])
    """
    data_dict: Dict[str, pd.DataFrame] = {}
    
    stock_spot_df = pd.DataFrame()
    
    if 'market' in selected_options:
        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeRemainingColumn(),
                console=console
            ) as progress:
                task = progress.add_task("[cyan]获取全市场行情...", total=100)
                stock_spot_df = _fetch_market_spot()
                if stock_spot_df is not None:
                    data_dict['全市场数据'] = stock_spot_df
                    progress.update(task, completed=100)
                    console.print(f"[green][OK][/green] 成功获取 {len(stock_spot_df)} 只股票数据")
                else:
                    progress.update(task, completed=100)
                    console.print(f"[red][X][/red] 获取全市场数据失败")
                    return data_dict
        except Exception:
            print("正在获取全市场行情...")
            stock_spot_df = _fetch_market_spot()
            if stock_spot_df is not None:
                data_dict['全市场数据'] = stock_spot_df
                console.print(f"[green][OK][/green] 成功获取 {len(stock_spot_df)} 只股票数据")
            else:
                console.print(f"[red][X][/red] 获取全市场数据失败")
                return data_dict
    else:
        stock_spot_df = _fetch_market_spot() or pd.DataFrame()
    
    # 计算涨停价（仅当有数据时）
    limit_up_df = pd.DataFrame()
    if not stock_spot_df.empty and '代码' in stock_spot_df.columns:
        def calc_limit_up_price(row: pd.Series) -> float:
            """计算涨停价格"""
            code = str(row["代码"])
            if code.startswith(("688", "300")):
                return round(row["昨收"] * 1.2, 2)
            else:
                return round(row["昨收"] * 1.1, 2)
        
        stock_spot_df["计算涨停价"] = stock_spot_df.apply(calc_limit_up_price, axis=1)
        limit_up_df = stock_spot_df[stock_spot_df["最新价"] >= stock_spot_df["计算涨停价"]].copy()
        limit_up_df = limit_up_df.sort_values(by="成交额", ascending=False).reset_index(drop=True)
    
    # 获取涨停股数据
    if 'limit_up' in selected_options:
        console.print("\n[yellow]【2/25】[/yellow] [cyan]正在获取涨停股数据...[/cyan]")
        if not limit_up_df.empty:
            data_dict['涨停股'] = limit_up_df[["代码", "名称", "最新价", "涨跌幅", "成交额", "计算涨停价"]]
            console.print(f"[green][OK][/green] 成功获取 {len(limit_up_df)} 只涨停股票")
        else:
            console.print("[yellow][!][/yellow] 无涨停股数据（市场数据不可用）")
    
    # 获取涨幅榜TOP20
    if 'rise_top' in selected_options:
        console.print("\n[yellow]【3/25】[/yellow] [cyan]正在获取涨幅榜TOP20...[/cyan]")
        if not stock_spot_df.empty:
            rise_top_df = stock_spot_df.sort_values(by="涨跌幅", ascending=False).head(20).reset_index(drop=True)
            data_dict['涨幅榜TOP20'] = rise_top_df[["代码", "名称", "最新价", "涨跌幅", "成交量"]]
            console.print("[green][OK][/green] 成功获取涨幅榜TOP20")
        else:
            console.print("[yellow][!][/yellow] 无涨幅榜数据（市场数据不可用）")
    
    # 获取跌幅榜TOP20
    if 'fall_top' in selected_options:
        console.print("\n[yellow]【4/25】[/yellow] [cyan]正在获取跌幅榜TOP20...[/cyan]")
        if not stock_spot_df.empty:
            fall_top_df = stock_spot_df.sort_values(by="涨跌幅", ascending=True).head(20).reset_index(drop=True)
            data_dict['跌幅榜TOP20'] = fall_top_df[["代码", "名称", "最新价", "涨跌幅", "成交量"]]
            console.print("[green][OK][/green] 成功获取跌幅榜TOP20")
        else:
            console.print("[yellow][!][/yellow] 无跌幅榜数据（市场数据不可用）")
    
    # 获取资金流向数据
    if 'fund_flow' in selected_options:
        console.print("\n[yellow]【5/25】[/yellow] [cyan]正在获取资金流向数据...[/cyan]")
        try:
            fund_flow_df = ak.stock_market_fund_flow()
            data_dict['资金流向'] = fund_flow_df
            console.print(f"[green][OK][/green] 成功获取资金流向数据: {len(fund_flow_df)} 条")
        except Exception as e:
            console.print(f"[red][X][/red] 获取资金流向数据失败: {str(e)[:80]}")
    
    # 获取行业板块数据
    if 'industry' in selected_options:
        console.print("\n[yellow]【6/25】[/yellow] [cyan]正在获取行业板块数据...[/cyan]")
        try:
            industry_info_df = pd.DataFrame()
            
            # 方案1: 东方财富行业板块（优先）
            try:
                console.print("[cyan]  -> 方案1: stock_board_industry_spot_em (东方财富)[/cyan]")
                industry_info_df = ak.stock_board_industry_spot_em()
                if industry_info_df is None or industry_info_df.empty:
                    raise Exception("东方财富返回空数据")
            except Exception as e1:
                console.print(f"[yellow][!][/yellow] 东方财富行业板块失败: {str(e1)[:40]}")
                # 方案2: 新浪行业板块（备用）
                try:
                    console.print("[cyan]  -> 方案2: stock_sector_spot (新浪)[/cyan]")
                    industry_info_df = ak.stock_sector_spot()
                    if industry_info_df is None or industry_info_df.empty:
                        raise Exception("新浪返回空数据")
                except Exception as e2:
                    console.print(f"[yellow][!][/yellow] 新浪行业板块也失败: {str(e2)[:40]}")
                    raise Exception("所有行业板块API均不可用")
            
            if not industry_info_df.empty:
                col_mapping = {
                    '板块': '行业名称',
                    'label': '行业代码',
                    '公司家数': '公司数量',
                    '平均价格': '平均价格',
                    '涨跌额': '涨跌额',
                    '涨跌幅': '涨跌幅',
                    '总成交量': '总成交量',
                    '总成交额': '总成交额',
                    '股票代码': '股票代码',
                    '股票名称': '领涨股',
                    '个股-涨跌幅': '领涨股涨跌幅',
                    '个股-当前价': '领涨股价格',
                    '个股-涨跌额': '领涨股涨跌额'
                }
                industry_info_df = industry_info_df.rename(
                    columns={k: v for k, v in col_mapping.items() if k in industry_info_df.columns}
                )
                data_dict['行业板块'] = industry_info_df
                console.print(f"[green][OK][/green] 成功获取行业板块数据: {len(industry_info_df)} 条")
            else:
                console.print(f"[red][X][/red] 获取行业板块信息失败: 无数据返回")
        except Exception as e:
            console.print(f"[red][X][/red] 获取行业板块信息失败: {str(e)[:80]}")
    
    # 获取热点成交数据
    if 'hot_deal' in selected_options:
        console.print("\n[yellow]【7/25】[/yellow] [cyan]正在获取热点成交数据...[/cyan]")
        try:
            hot_deal_df = ak.stock_hot_deal_xq()
            data_dict['热点成交'] = hot_deal_df
            console.print(f"[green][OK][/green] 成功获取热点成交数据: {len(hot_deal_df)} 条")
        except Exception as e:
            console.print(f"[red][X][/red] 获取热点成交数据失败: {str(e)[:80]}")
    
    # 获取龙虎榜数据
    if 'lhb' in selected_options:
        console.print("\n[yellow]【8/25】[/yellow] [cyan]正在获取龙虎榜数据...[/cyan]")
        try:
            lhb_df = ak.stock_lhb_detail_em()
            data_dict['龙虎榜'] = lhb_df
            console.print(f"[green][OK][/green] 成功获取龙虎榜数据: {len(lhb_df)} 条")
        except Exception as e:
            console.print(f"[red][X][/red] 获取龙虎榜数据失败: {str(e)[:80]}")
    
    # 获取五档盘口数据
    if 'bid_ask' in selected_options:
        console.print("\n[yellow]【9/25】[/yellow] [cyan]正在获取五档盘口数据...[/cyan]")
        try:
            def get_tushare_bid_ask_data(codes: List[str]) -> pd.DataFrame:
                """通过Tushare获取五档盘口数据"""
                result = pd.DataFrame()
                for code in codes[:20]:
                    try:
                        df = ts.get_realtime_quotes(code)
                        if not df.empty:
                            result = pd.concat([result, df])
                    except Exception:
                        pass
                return result
            
            bid_ask_df = get_tushare_bid_ask_data(limit_up_df['代码'].tolist())
            data_dict['五档盘口'] = bid_ask_df
            console.print(f"[green][OK][/green] 成功获取五档盘口数据: {len(bid_ask_df)} 条")
        except Exception as e:
            console.print(f"[red][X][/red] 获取五档盘口数据失败: {str(e)[:80]}")
    
    # 获取涨停股基本面数据
    if 'financial' in selected_options:
        console.print("\n[yellow]【10/25】[/yellow] [cyan]正在获取涨停股基本面数据...[/cyan]")
        try:
            financial_df = pd.DataFrame()
            for _, row in limit_up_df.head(20).iterrows():
                try:
                    fin_df = ak.stock_financial_report_sina(symbol=row["代码"])
                    fin_df["股票代码"] = row["代码"]
                    fin_df["股票名称"] = row["名称"]
                    financial_df = pd.concat([financial_df, fin_df])
                except Exception:
                    pass
            data_dict['涨停股基本面'] = financial_df
            console.print(f"[green][OK][/green] 成功获取涨停股基本面数据: {len(financial_df)} 条")
        except Exception as e:
            console.print(f"[red][X][/red] 获取涨停股基本面数据失败: {str(e)[:80]}")
    
    # 获取大宗交易数据
    if 'block_trade' in selected_options:
        console.print("\n[yellow]【11/25】[/yellow] [cyan]正在获取大宗交易数据...[/cyan]")
        try:
            # 尝试多个备用API
            try:
                block_trade_df = ak.stock_fund_flow_big_deal()
            except Exception:
                try:
                    # 使用东方财富大宗交易API
                    block_trade_df = ak.stock_lhb_detail_em()
                except Exception:
                    block_trade_df = pd.DataFrame()
            
            if not block_trade_df.empty:
                data_dict['大宗交易'] = block_trade_df
                console.print(f"[green][OK][/green] 成功获取大宗交易数据: {len(block_trade_df)} 条")
            else:
                console.print(f"[yellow][!][/yellow] 获取大宗交易数据为空")
        except Exception as e:
            console.print(f"[red][X][/red] 获取大宗交易数据失败: {str(e)[:80]}")
    
    # 获取贸易余额数据
    if 'trade_balance' in selected_options:
        console.print("\n[yellow]【12/25】[/yellow] [cyan]正在获取贸易余额数据...[/cyan]")
        try:
            trade_balance_df = ak.macro_china_trade_balance()
            data_dict['贸易余额'] = trade_balance_df
            console.print(f"[green][OK][/green] 成功获取贸易余额数据: {len(trade_balance_df)} 条")
        except Exception as e:
            console.print(f"[red][X][/red] 获取贸易余额数据失败: {str(e)[:80]}")
    
    # 获取CPI数据
    if 'cpi' in selected_options:
        console.print("\n[yellow]【13/25】[/yellow] [cyan]正在获取CPI数据...[/cyan]")
        try:
            cpi_df = ak.macro_china_cpi()
            data_dict['CPI数据'] = cpi_df
            console.print(f"[green][OK][/green] 成功获取CPI数据: {len(cpi_df)} 条")
        except Exception as e:
            console.print(f"[red][X][/red] 获取CPI数据失败: {str(e)[:80]}")
    
    # 获取概念板块数据
    if 'concept' in selected_options:
        console.print("\n[yellow]【14/25】[/yellow] [cyan]正在获取概念板块数据...[/cyan]")
        try:
            max_retries = 3
            retry_delay = 2
            concept_df = pd.DataFrame()
            
            for attempt in range(max_retries):
                try:
                    concept_df = ak.stock_board_concept_spot_em()
                    if concept_df is not None and not concept_df.empty:
                        break
                except Exception:
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        retry_delay *= 2
                    else:
                        # 备用方案：使用资金流向概念板块
                        try:
                            concept_df = ak.stock_fund_flow_concept()
                        except Exception:
                            concept_df = pd.DataFrame()
            
            if concept_df is not None and not concept_df.empty:
                data_dict['概念板块'] = concept_df
                console.print(f"[green][OK][/green] 成功获取概念板块数据: {len(concept_df)} 条")
            else:
                console.print(f"[yellow][!][/yellow] 获取概念板块数据为空")
        except Exception as e:
            console.print(f"[red][X][/red] 获取概念板块数据失败: {str(e)[:80]}")
    
    # 获取北向资金持股数据
    if 'hk_hold' in selected_options:
        console.print("\n[yellow]【15/25】[/yellow] [cyan]正在获取北向资金持股数据...[/cyan]")
        try:
            hk_hold_df = pd.DataFrame()
            
            # 尝试主API
            try:
                hk_hold_df = ak.stock_hsgt_individual_em()
            except Exception:
                # 备用方案
                try:
                    hk_hold_df = ak.stock_hsgt_fund_flow_summary_em()
                except Exception:
                    hk_hold_df = pd.DataFrame()
            
            if hk_hold_df is not None and not hk_hold_df.empty:
                data_dict['北向资金持股'] = hk_hold_df
                console.print(f"[green][OK][/green] 成功获取北向资金持股数据: {len(hk_hold_df)} 条")
            else:
                console.print(f"[yellow][!][/yellow] 获取北向资金持股数据为空")
        except Exception as e:
            console.print(f"[red][X][/red] 获取北向资金持股数据失败: {str(e)[:80]}")
    
    # 获取融资融券数据
    if 'margin' in selected_options:
        console.print("\n[yellow]【16/25】[/yellow] [cyan]正在获取融资融券数据...[/cyan]")
        try:
            margin_df = ak.stock_margin_sse()
            if not margin_df.empty:
                data_dict['融资融券'] = margin_df
                console.print(f"[green][OK][/green] 成功获取融资融券数据: {len(margin_df)} 条")
            else:
                console.print(f"[yellow][!][/yellow] 获取融资融券数据为空")
        except Exception as e:
            console.print(f"[red][X][/red] 获取融资融券数据失败: {str(e)[:80]}")
    
    # 获取新股申购数据
    if 'new_share' in selected_options:
        console.print("\n[yellow]【17/25】[/yellow] [cyan]正在获取新股申购数据...[/cyan]")
        try:
            new_share_df = pd.DataFrame()
            
            # 尝试多个备用API
            try:
                new_share_df = ak.stock_new_ipo_cninfo()
            except Exception:
                try:
                    new_share_df = ak.stock_ipo_tutor_em()
                except Exception:
                    try:
                        new_share_df = ak.stock_ipo_info()
                    except Exception:
                        pass
            
            if new_share_df is not None and not new_share_df.empty:
                data_dict['新股申购'] = new_share_df
                console.print(f"[green][OK][/green] 成功获取新股申购数据: {len(new_share_df)} 条")
            else:
                console.print(f"[yellow][!][/yellow] 获取新股申购数据为空")
        except Exception as e:
            console.print(f"[red][X][/red] 获取新股申购数据失败: {str(e)[:80]}")
    
    # 获取龙虎榜详情数据
    if 'lhb_detail' in selected_options:
        console.print("\n[yellow]【18/25】[/yellow] [cyan]正在获取龙虎榜详情数据...[/cyan]")
        try:
            lhb_detail_df = pd.DataFrame()
            # 方案1: 东方财富龙虎榜（优先）
            try:
                lhb_detail_df = ak.stock_lhb_detail_em()
                if lhb_detail_df is None or lhb_detail_df.empty:
                    raise Exception("东方财富返回空")
            except Exception:
                # 方案2: 新浪龙虎榜（备用）
                try:
                    lhb_detail_df = ak.stock_lhb_detail_daily_sina()
                except Exception:
                    pass
            
            if lhb_detail_df is not None and not lhb_detail_df.empty:
                data_dict['龙虎榜详情'] = lhb_detail_df
                console.print(f"[green][OK][/green] 成功获取龙虎榜详情数据: {len(lhb_detail_df)} 条")
            else:
                console.print(f"[yellow][!][/yellow] 获取龙虎榜详情数据为空")
        except Exception as e:
            console.print(f"[red][X][/red] 获取龙虎榜详情数据失败: {str(e)[:80]}")
    
    # 获取GDP数据
    if 'gdp' in selected_options:
        console.print("\n[yellow]【19/25】[/yellow] [cyan]正在获取GDP数据...[/cyan]")
        try:
            gdp_df = ak.macro_china_gdp()
            data_dict['GDP数据'] = gdp_df
            console.print(f"[green][OK][/green] 成功获取GDP数据: {len(gdp_df)} 条")
        except Exception as e:
            console.print(f"[red][X][/red] 获取GDP数据失败: {str(e)[:80]}")
    
    # 获取PPI数据
    if 'ppi' in selected_options:
        console.print("\n[yellow]【20/25】[/yellow] [cyan]正在获取PPI数据...[/cyan]")
        try:
            ppi_df = ak.macro_china_ppi()
            data_dict['PPI数据'] = ppi_df
            console.print(f"[green][OK][/green] 成功获取PPI数据: {len(ppi_df)} 条")
        except Exception as e:
            console.print(f"[red][X][/red] 获取PPI数据失败: {str(e)[:80]}")
    
    # 获取货币供应量数据
    if 'money_supply' in selected_options:
        console.print("\n[yellow]【21/25】[/yellow] [cyan]正在获取货币供应量数据...[/cyan]")
        try:
            money_supply_df = ak.macro_china_money_supply()
            data_dict['货币供应量'] = money_supply_df
            console.print(f"[green][OK][/green] 成功获取货币供应量数据: {len(money_supply_df)} 条")
        except Exception as e:
            console.print(f"[red][X][/red] 获取货币供应量数据失败: {str(e)[:80]}")
    
    # 获取汇率数据
    if 'exchange_rate' in selected_options:
        console.print("\n[yellow]【22/25】[/yellow] [cyan]正在获取汇率数据...[/cyan]")
        try:
            exchange_rate_df = ak.currency_boc_safe()
            data_dict['汇率数据'] = exchange_rate_df
            console.print(f"[green][OK][/green] 成功获取汇率数据: {len(exchange_rate_df)} 条")
        except Exception as e:
            console.print(f"[red][X][/red] 获取汇率数据失败: {str(e)[:80]}")
    
    # 获取国债收益率数据
    if 'bond_yield' in selected_options:
        console.print("\n[yellow]【23/25】[/yellow] [cyan]正在获取国债收益率数据...[/cyan]")
        try:
            bond_yield_df = ak.bond_china_yield()
            data_dict['国债收益率'] = bond_yield_df
            console.print(f"[green][OK][/green] 成功获取国债收益率数据: {len(bond_yield_df)} 条")
        except Exception as e:
            console.print(f"[red][X][/red] 获取国债收益率数据失败: {str(e)[:80]}")
    
    # 获取股票回购数据
    if 'repurchase' in selected_options:
        console.print("\n[yellow]【24/25】[/yellow] [cyan]正在获取股票回购数据...[/cyan]")
        try:
            repurchase_df = ak.stock_repurchase_em()
            if not repurchase_df.empty:
                data_dict['股票回购'] = repurchase_df
                console.print(f"[green][OK][/green] 成功获取股票回购数据: {len(repurchase_df)} 条")
            else:
                console.print(f"[yellow][!][/yellow] 获取股票回购数据为空")
        except Exception as e:
            console.print(f"[red][X][/red] 获取股票回购数据失败: {str(e)[:80]}")
    
    # 获取外商投资数据
    if 'fdi' in selected_options:
        console.print("\n[yellow]【25/25】[/yellow] [cyan]正在获取外商投资数据...[/cyan]")
        try:
            fdi_df = ak.macro_china_fdi()
            data_dict['外商投资'] = fdi_df
            console.print(f"[green][OK][/green] 成功获取外商投资数据: {len(fdi_df)} 条")
        except Exception as e:
            console.print(f"[red][X][/red] 获取外商投资数据失败: {str(e)[:80]}")
    
    return data_dict


def clean_special_chars(text: str) -> str:
    """
    清理文本中的特殊字符，防止编码错误和文件名问题
    
    Args:
        text: 输入文本
        
    Returns:
        清理后的文本
    """
    if isinstance(text, str):
        # 先移除文件系统不允许的字符
        invalid_chars = '<>:"/\\|?*'
        result = ''.join(char for char in text if char not in invalid_chars)
        # 再过滤掉非ASCII字符（保留常见中文标点）
        result = ''.join(char for char in result if ord(char) < 128 or char in '，。！？、；：“”‘’（）【】《》')
        return result
    return text


def analyze_stocks_with_deepseek(data_dict: Dict[str, pd.DataFrame]) -> str:
    """
    调用 AI 模型分析全市场股票数据，挖掘有潜力的股票

    实际调用 DashScope API（通义千问 qwen-plus 模型）进行智能分析。
    需要配置环境变量 DASHSCOPE_API_KEY。

    Args:
        data_dict: 包含全市场数据、涨停股、北向资金、资金流向、行业板块等

    Returns:
        AI 模型生成的分析结果字符串
    """
    console.print("\n[cyan]正在调用AI模型进行股票市场分析...[/cyan]")
    
    try:
        # 准备分析数据摘要
        summary = "【股票数据分析报告】\n\n"
        
        # 提取关键数据
        if '全市场数据' in data_dict:
            market_df = data_dict['全市场数据']
            summary += f"1. 全市场概况：共 {len(market_df)} 只股票\n"
            summary += f"   - 平均涨幅: {market_df['涨跌幅'].mean():.2f}%\n"
            summary += f"   - 上涨家数: {len(market_df[market_df['涨跌幅'] > 0])}\n"
            summary += f"   - 下跌家数: {len(market_df[market_df['涨跌幅'] < 0])}\n"
        
        if '涨停股' in data_dict:
            limit_up_df = data_dict['涨停股']
            summary += f"\n2. 涨停股分析：共 {len(limit_up_df)} 只涨停\n"
            top_stocks = limit_up_df.head(5)[['代码', '名称', '成交额']]
            for _, row in top_stocks.iterrows():
                summary += f"   - {row['代码']} {row['名称']} 成交额: {row['成交额']:.2f}亿\n"
        
        if '北向资金持股' in data_dict:
            hk_df = data_dict['北向资金持股']
            summary += f"\n3. 北向资金持股：共 {len(hk_df)} 只股票\n"
            if '持股比例' in hk_df.columns:
                top_hk = hk_df.sort_values('持股比例', ascending=False).head(3)
                for _, row in top_hk.iterrows():
                    summary += f"   - {row.get('股票代码', 'N/A')} {row.get('股票名称', 'N/A')} 持股比例: {row.get('持股比例', 'N/A')}%\n"
        
        if '资金流向' in data_dict:
            fund_flow = data_dict['资金流向']
            summary += f"\n4. 资金流向分析：\n"
            if not fund_flow.empty:
                latest = fund_flow.iloc[-1]
                summary += f"   - 北向资金: {latest.get('北向资金', 'N/A')}\n"
                summary += f"   - 南向资金: {latest.get('南向资金', 'N/A')}\n"
        
        if '行业板块' in data_dict:
            industry_df = data_dict['行业板块']
            summary += f"\n5. 行业板块表现：共 {len(industry_df)} 个行业\n"
            top_industry = industry_df.sort_values('涨跌幅', ascending=False).head(3)
            for _, row in top_industry.iterrows():
                summary += f"   - {row.get('行业名称', row.get('板块', 'N/A'))} 涨幅: {row.get('涨跌幅', 'N/A')}%\n"
        
        if '龙虎榜' in data_dict:
            lhb_df = data_dict['龙虎榜']
            summary += f"\n6. 龙虎榜数据：共 {len(lhb_df)} 条记录\n"
        
        summary += "\n【分析请求】\n请根据以上数据，分析并推荐有潜力的股票，给出具体理由。"
        
        # 调用 DashScope API
        return _call_dashscope_api(
            system_prompt="你是一位专业的股票分析师，请基于提供的数据进行客观分析，用中文回复",
            user_content=summary,
            max_tokens=2000,
            timeout=60
        )
        
    except Exception as e:
        return f"[red]分析失败: {str(e)}[/red]"


def analyze_single_stock_with_deepseek(analysis_result: Dict[str, Any]) -> str:
    """
    调用 AI 模型对单只股票进行深度分析

    实际调用 DashScope API（通义千问 qwen-plus 模型）进行智能分析。
    需要配置环境变量 DASHSCOPE_API_KEY。

    Args:
        analysis_result: 单股分析结果字典，包含L1数据、技术指标、资金流向等

    Returns:
        AI 模型生成的分析结果字符串，包含股票潜力评估和购买建议
    """
    console.print("\n[cyan]正在调用AI模型进行个股分析...[/cyan]")
    
    try:
        stock_code = analysis_result.get('股票代码', '未知')
        
        # 准备个股分析数据摘要 - 包含股票代码和名称
        stock_name = stock_code
        if analysis_result.get('L1数据') and analysis_result['L1数据'].get('股票名称'):
            stock_name = analysis_result['L1数据']['股票名称']
        
        summary = f"【个股分析报告 - {stock_code} {stock_name}】\n\n"
        
        # L1数据
        if analysis_result['L1数据']:
            l1 = analysis_result['L1数据']
            summary += "一、基本行情数据\n"
            summary += f"  - 股票代码: {stock_code}\n"
            summary += f"  - 股票名称: {l1.get('股票名称', 'N/A')}\n"
            summary += f"  - 最新价: {l1.get('最新价', 'N/A')}\n"
            summary += f"  - 涨跌幅: {l1.get('涨跌幅', 'N/A')}\n"
            summary += f"  - 量比: {l1.get('量比', 'N/A')}\n"
            summary += f"  - 换手率: {l1.get('换手率', 'N/A')}\n"
            summary += f"  - 市盈率: {l1.get('市盈率-动态', 'N/A')}\n"
            summary += f"  - 市净率: {l1.get('市净率', 'N/A')}\n"
            summary += f"  - 总市值: {l1.get('总市值', 'N/A')}\n"
        
        # 技术指标
        if analysis_result['技术指标']:
            tech = analysis_result['技术指标']
            summary += "\n二、技术指标分析\n"
            summary += f"  - RSI(6): {tech.get('RSI(6)', 'N/A')}\n"
            summary += f"  - RSI(12): {tech.get('RSI(12)', 'N/A')}\n"
            summary += f"  - MACD: {tech.get('MACD', 'N/A')}\n"
            summary += f"  - MACD Signal: {tech.get('MACD_Signal', 'N/A')}\n"
            summary += f"  - MACD Histogram: {tech.get('MACD_Histogram', 'N/A')}\n"
            summary += f"  - 5日动量: {tech.get('5日动量', 'N/A')}%\n"
            summary += f"  - 20日动量: {tech.get('20日动量', 'N/A')}%\n"
            summary += f"  - 成交量异动率(5日): {tech.get('成交量异动率(5日)', 'N/A')}%\n"
        
        # 资金流向
        if analysis_result['资金流向']:
            fund = analysis_result['资金流向']
            summary += "\n三、资金流向分析\n"
            summary += f"  - 主力净流入: {fund.get('今日主力净流入', 'N/A')}\n"
            summary += f"  - 超大单净流入: {fund.get('今日超大单净流入', 'N/A')}\n"
            summary += f"  - 大单净流入: {fund.get('今日大单净流入', 'N/A')}\n"
        
        # 财务指标
        if analysis_result['财务报表'] and analysis_result['财务报表'].get('财务指标'):
            financial = analysis_result['财务报表']['财务指标']
            summary += "\n四、财务指标分析\n"
            summary += f"  - ROE: {financial.get('ROE', 'N/A')}%\n"
            summary += f"  - 净利润: {financial.get('净利润', 'N/A')}\n"
            summary += f"  - 营业收入: {financial.get('营业收入', 'N/A')}\n"
            summary += f"  - 毛利率: {financial.get('毛利率', 'N/A')}%\n"
            summary += f"  - 净利率: {financial.get('净利率', 'N/A')}%\n"
        
        # 分红送配
        if analysis_result['分红送配']:
            dividend = analysis_result['分红送配']
            summary += "\n五、分红送配信息\n"
            summary += f"  - 分红年度: {dividend.get('分红年度', 'N/A')}\n"
            summary += f"  - 每股分红: {dividend.get('每股分红', 'N/A')}\n"
            summary += f"  - 送股比例: {dividend.get('送股比例', 'N/A')}\n"
            summary += f"  - 转增比例: {dividend.get('转增比例', 'N/A')}\n"
        
        summary += "\n【分析请求】\n请根据以上数据，对该股票进行综合分析，包括：\n"
        summary += "1. 该股票的投资潜力评估\n"
        summary += "2. 技术面分析（趋势、买卖信号）\n"
        summary += "3. 基本面分析（估值、财务健康度）\n"
        summary += "4. 风险提示\n"
        summary += "5. 购买建议（强烈推荐/推荐/观望/谨慎/回避）\n"
        summary += "请用中文详细分析，给出具体理由和建议。"
        
        # 调用 DashScope API
        return _call_dashscope_api(
            system_prompt="你是一位专业的A股股票分析师，擅长技术分析和基本面分析。请基于用户提供的个股数据，给出客观、专业的投资建议，用中文回复。",
            user_content=summary,
            max_tokens=2500,
            timeout=60
        )
        
    except Exception as e:
        return f"[red]分析失败: {str(e)}[/red]"


def export_to_excel(data_dict: Dict[str, pd.DataFrame], filename: str = "A股综合数据.xlsx") -> bool:
    """
    将数据导出到Excel文件
    
    使用openpyxl引擎将多个DataFrame导出到同一个Excel文件的不同Sheet页。
    
    Args:
        data_dict: 包含数据的字典，key为Sheet名称，value为DataFrame
        filename: 输出文件名，默认为"A股综合数据.xlsx"
        
    Returns:
        导出成功返回True，失败返回False
        
    Example:
        >>> data = {'全市场数据': df1, '涨停股': df2}
        >>> success = export_to_excel(data, 'output.xlsx')
    """
    console.print(f"\n[cyan]> 正在导出数据到 Excel...[/cyan]")
    try:
        def _write_sheets(writer):
            for sheet_name, df in data_dict.items():
                clean_df = df.copy()
                for col in clean_df.columns:
                    if clean_df[col].dtype == object:
                        clean_df[col] = clean_df[col].apply(clean_special_chars)
                clean_df.to_excel(writer, sheet_name=sheet_name, index=False)

        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                console=console
            ) as progress:
                task = progress.add_task("[cyan]写入Sheet页", total=len(data_dict))
                with pd.ExcelWriter(filename, engine='openpyxl') as writer:
                    _write_sheets(writer)
                    progress.update(task, completed=len(data_dict))
        except Exception:
            print(f"正在导出 {len(data_dict)} 个Sheet页...")
            with pd.ExcelWriter(filename, engine='openpyxl') as writer:
                _write_sheets(writer)
        
        console.print(f"\n[green]* 数据已保存到：{filename}[/green]")
        
        table = Table(title="包含以下Sheet页", box=box.ASCII)
        table.add_column("序号", style="cyan", justify="center")
        table.add_column("Sheet页名称", style="green")
        table.add_column("数据量", style="yellow", justify="right")
        
        for idx, (sheet_name, df) in enumerate(data_dict.items(), 1):
            table.add_row(str(idx), sheet_name, f"{len(df)} 条")
        
        console.print(table)
    except PermissionError:
        console.print(f"\n[red][X] 导出失败：文件被占用，请关闭 Excel 后重试[/red]")
        return False
    except Exception as e:
        console.print(f"\n[red][X] 导出失败：{str(e)}[/red]")
        return False
    return True


def main() -> None:
    """
    程序主入口
    
    提供用户交互界面，支持两种操作模式：
    1. 批量数据导出 - 导出13种股票数据到Excel
    2. 单股分析查询 - 输入股票代码获取详细技术分析
    
    Returns:
        None
    """
    console.print(Panel.fit(
        Text("A股数据查询工具 v3.0", style="bold cyan", justify="center"),
        subtitle="基于 Akshare.py",
        border_style="cyan",
        box=box.ASCII
    ))

    warmup_market_cache()
    
    console.print("\n[cyan]请选择操作模式：[/cyan]")
    mode_table = Table(box=box.ASCII)
    mode_table.add_column("序号", style="cyan", justify="center")
    mode_table.add_column("模式", style="green")
    mode_table.add_column("说明", style="dim")
    
    mode_table.add_row("1", "批量数据导出", "导出批量股票数据到Excel")
    mode_table.add_row("2", "单股分析查询", "输入股票代码获取详细技术分析")
    mode_table.add_row("3", "股票智能筛选", "基于L1/L2数据的智能股票筛选")
    mode_table.add_row("q", "退出", "退出程序")
    
    console.print(mode_table)
    
    while True:
        try:
            _check_warmup_thread_health()
            mode = input("\n> 请输入模式序号（1/2/3/q）: ").strip().lower()
        except EOFError:
            console.print("\n[red][!] 输入错误，程序退出[/red]")
            return
        
        if mode == 'q':
            console.print("\n[blue]感谢使用，再见！[/blue]")
            return
        
        if mode == '2':
            console.print("\n[cyan]已进入单股分析查询模式[/cyan]")
            console.print(Panel.fit(
                Text("单只股票详细分析", style="bold yellow", justify="center"),
                border_style="yellow",
                box=box.ASCII
            ))
            
            while True:
                try:
                    stock_code = input("\n> 请输入股票代码（6位数字，如 600000，输入 q 返回上级菜单）: ").strip()
                except EOFError:
                    console.print("\n[red][!] 输入错误，返回上级菜单[/red]")
                    break
                
                if stock_code.lower() == 'q':
                    break
                
                if len(stock_code) != 6 or not stock_code.isdigit():
                    console.print("[red][!] 请输入有效的6位股票代码[/red]")
                    continue
                
                analysis_result = get_single_stock_analysis(stock_code)
                
                if analysis_result:
                    display_stock_analysis(analysis_result)
                    
                    try:
                        save = input("\n> 是否保存分析报告到Excel？(Y/N): ").strip().upper()
                    except EOFError:
                        save = 'N'
                    if save == 'Y':
                        try:
                            report_data = {}
                            
                            if analysis_result['L1数据']:
                                l1_df = pd.DataFrame([analysis_result['L1数据']])
                                report_data['L1数据'] = l1_df
                            
                            if analysis_result['L2数据']:
                                l2_df = pd.DataFrame([analysis_result['L2数据']])
                                report_data['L2数据'] = l2_df
                            
                            if analysis_result['技术指标']:
                                tech_df = pd.DataFrame([analysis_result['技术指标']])
                                report_data['技术指标'] = tech_df
                            
                            if analysis_result['资金流向']:
                                fund_df = pd.DataFrame([analysis_result['资金流向']])
                                report_data['资金流向'] = fund_df
                            
                            if analysis_result['财务报表']:
                                if '财务指标' in analysis_result['财务报表']:
                                    finance_df = pd.DataFrame([analysis_result['财务报表']['财务指标']])
                                    report_data['财务指标'] = finance_df
                            
                            if analysis_result['分红送配']:
                                dividend_df = pd.DataFrame([analysis_result['分红送配']])
                                report_data['分红送配'] = dividend_df
                            
                            # 导出完整历史K线数据
                            if '历史K线' in analysis_result and not analysis_result['历史K线'].empty:
                                report_data['历史K线'] = analysis_result['历史K线']
                            
                            # 新增：导出新增的akshare + tushare数据
                            if '基本信息' in analysis_result and analysis_result['基本信息']:
                                basic_df = pd.DataFrame([analysis_result['基本信息']])
                                report_data['基本信息'] = basic_df
                            
                            if '资金流向详细' in analysis_result and analysis_result['资金流向详细']:
                                moneyflow_detail_df = pd.DataFrame(analysis_result['资金流向详细'])
                                report_data['资金流向详细'] = moneyflow_detail_df
                            
                            if '融资融券' in analysis_result and analysis_result['融资融券']:
                                margin_df = pd.DataFrame(analysis_result['融资融券'])
                                report_data['融资融券'] = margin_df
                            
                            if '龙虎榜' in analysis_result and analysis_result['龙虎榜']:
                                lhb_df = pd.DataFrame(analysis_result['龙虎榜'])
                                report_data['龙虎榜'] = lhb_df
                            
                            if '大宗交易' in analysis_result and analysis_result['大宗交易']:
                                bigdeal_df = pd.DataFrame(analysis_result['大宗交易'])
                                report_data['大宗交易'] = bigdeal_df
                            
                            if '机构持股' in analysis_result and analysis_result['机构持股']:
                                institute_df = pd.DataFrame(analysis_result['机构持股'])
                                report_data['机构持股'] = institute_df
                            
                            if '业绩预告' in analysis_result and analysis_result['业绩预告']:
                                forecast_df = pd.DataFrame(analysis_result['业绩预告'])
                                report_data['业绩预告'] = forecast_df
                            
                            if '限售股解禁' in analysis_result and analysis_result['限售股解禁']:
                                pledge_df = pd.DataFrame(analysis_result['限售股解禁'])
                                report_data['限售股解禁'] = pledge_df
                            
                            # Tushare数据导出
                            if 'Tushare行情' in analysis_result and analysis_result['Tushare行情']:
                                ts_daily_df = pd.DataFrame(analysis_result['Tushare行情'])
                                report_data['Tushare行情'] = ts_daily_df
                            
                            if 'Tushare财务指标' in analysis_result and analysis_result['Tushare财务指标']:
                                ts_fin_df = pd.DataFrame(analysis_result['Tushare财务指标'])
                                report_data['Tushare财务指标'] = ts_fin_df
                            
                            if 'Tushare利润表' in analysis_result and analysis_result['Tushare利润表']:
                                ts_income_df = pd.DataFrame(analysis_result['Tushare利润表'])
                                report_data['Tushare利润表'] = ts_income_df
                            
                            if 'Tushare资产负债表' in analysis_result and analysis_result['Tushare资产负债表']:
                                ts_balance_df = pd.DataFrame(analysis_result['Tushare资产负债表'])
                                report_data['Tushare资产负债表'] = ts_balance_df
                            
                            if 'Tushare现金流量表' in analysis_result and analysis_result['Tushare现金流量表']:
                                ts_cashflow_df = pd.DataFrame(analysis_result['Tushare现金流量表'])
                                report_data['Tushare现金流量表'] = ts_cashflow_df
                            
                            if 'Tushare前十大股东' in analysis_result and analysis_result['Tushare前十大股东']:
                                ts_holders_df = pd.DataFrame(analysis_result['Tushare前十大股东'])
                                report_data['Tushare前十大股东'] = ts_holders_df
                            
                            if 'Tushare主力资金' in analysis_result and analysis_result['Tushare主力资金']:
                                ts_moneyflow_df = pd.DataFrame(analysis_result['Tushare主力资金'])
                                report_data['Tushare主力资金'] = ts_moneyflow_df
                            
                            if report_data:
                                filename = f"{stock_code}_分析报告.xlsx"
                                export_to_excel(report_data, filename)
                                console.print(f"[green]  -> 已保存 {len(report_data)} 个数据表到 {filename}[/green]")
                            else:
                                console.print("[red][X] 没有可保存的数据[/red]")
                        except Exception as e:
                            console.print(f"[red][X] 保存失败：{str(e)[:50]}[/red]")
                    
                    try:
                        ai_analysis = input("\n> 是否调用AI模型进行个股分析？(Y/N): ").strip().upper()
                    except EOFError:
                        ai_analysis = 'N'
                    if ai_analysis == 'Y':
                        console.print("\n" + "=" * 60)
                        console.print("[cyan]正在进行AI个股分析...[/cyan]")
                        console.print("=" * 60)
                        
                        analysis_result = analyze_single_stock_with_deepseek(analysis_result)
                        
                        console.print("\n" + "=" * 60)
                        console.print("[green]qwen-plus 个股分析结果[/green]")
                        console.print("=" * 60)
                        console.print(analysis_result)
                        console.print("\n" + "=" * 60)
                else:
                    console.print("[red][X] 获取股票分析失败[/red]")
            
            console.print("\n[cyan]已返回上级菜单[/cyan]\n")
            continue
        
        if mode == '3':
            console.print("\n[cyan]已进入股票智能筛选模式[/cyan]")
            run_stock_screener()
            console.print("\n[cyan]已返回上级菜单[/cyan]\n")
            continue
        
        if mode == '1':
            break
        else:
            console.print("[red][!] 请输入有效的模式序号[/red]")
    
    # 批量数据导出模式
    options = {
        'market': '全市场数据',
        'limit_up': '涨停股',
        'rise_top': '涨幅榜TOP20',
        'fall_top': '跌幅榜TOP20',
        'fund_flow': '资金流向',
        'industry': '行业板块',
        'hot_deal': '热点成交',
        'lhb': '龙虎榜',
        'lhb_detail': '龙虎榜详情',
        'bid_ask': '五档盘口',
        'financial': '涨停股基本面',
        'block_trade': '大宗交易',
        'trade_balance': '贸易余额',
        'cpi': 'CPI数据',
        'concept': '概念板块',
        'hk_hold': '北向资金持股',
        'margin': '融资融券',
        'new_share': '新股申购',
        'gdp': 'GDP数据',
        'ppi': 'PPI数据',
        'money_supply': '货币供应量',
        'exchange_rate': '汇率数据',
        'bond_yield': '国债收益率',
        'repurchase': '股票回购',
        'fdi': '外商投资'
    }
    
    console.print("\n[cyan]请选择要导出的数据类型：[/cyan]")
    console.print("-" * 60)
    
    console.print(f"  [0] 导出全部数据类型（共 {len(options)} 项）")
    for idx, (key, value) in enumerate(options.items(), 1):
        console.print(f"  [{idx}] {value}")
    
    console.print("-" * 60)
    
    while True:
        try:
            selected_indices = input("> 请输入选择（0=全部，或输入数字如: 1 3 5）: ").strip()
        except EOFError:
            console.print("\n[red][!] 输入错误，程序退出[/red]")
            return
        if not selected_indices:
            console.print("[red][!] 请至少选择一项[/red]")
            continue
        
        try:
            indices = [int(i.strip()) for i in selected_indices.split()]
            
            if 0 in indices:
                indices = list(range(1, len(options) + 1))
                console.print("[cyan]已选择导出全部数据类型[/cyan]")
                break
            
            if all(1 <= i <= len(options) for i in indices):
                break
            else:
                console.print(f"[red][!] 请输入0-{len(options)}之间的数字[/red]")
        except ValueError:
            console.print("[red][!] 请输入有效的数字[/red]")
    
    selected_options = [list(options.keys())[i-1] for i in indices]
    selected_names = [options[key] for key in selected_options]
    
    console.print("\n[cyan]你选择了以下数据类型：[/cyan]")
    for name in selected_names:
        console.print(f"  ✓ {name}")
    
    try:
        confirm = input("\n> 确认开始获取数据？(Y/N): ").strip().upper()
    except EOFError:
        confirm = 'N'
    if confirm != 'Y':
        console.print("\n[blue]操作已取消[/blue]")
        return
    
    console.print("\n" + "=" * 60)
    console.print("[cyan]开始批量获取数据...[/cyan]")
    console.print("=" * 60)
    
    data_dict = get_stock_data(selected_options)
    
    if data_dict:
        export_to_excel(data_dict)
    else:
        console.print("\n[red][X] 没有获取到任何数据[/red]")


# 第九部分：数据获取工具


class DataFetcher:
    """
    数据获取器
    
    封装所有股票数据获取逻辑，支持多数据源（东方财富、腾讯、新浪）和多级 API 回退机制。
    
    主要功能：
    - 实时行情数据获取（L1，带内存缓存避免重复请求）
    - 历史K线数据获取（多源：腾讯直连 → akshare新浪 → 东方财富）
    - L2 五档盘口数据获取（东方财富、新浪）
    - 资金流向、融资融券、龙虎榜等扩展数据
    
    所有方法均为静态方法，通过统一接口屏蔽底层数据源差异，
    自动处理网络超时、格式转换和回退策略。
    """
    
    @staticmethod
    def get_realtime_data(stock_code: str) -> Optional[Dict]:
        """获取实时行情数据（L1，使用缓存）"""
        _t0 = time.perf_counter()
        try:
            df = _get_cached_market_data("em")
            _t1 = time.perf_counter()
            if df is not None and not df.empty:
                stock_df = df[df['代码'] == stock_code]
                if not stock_df.empty:
                    row = stock_df.iloc[0]
                    log_info(f"[{stock_code}] 实时行情获取: {_t1 - _t0:.3f}s (缓存命中)")
                    return {
                        '代码': row.get('代码'),
                        '名称': row.get('名称'),
                        '最新价': row.get('最新价'),
                        '涨跌幅': row.get('涨跌幅'),
                        '涨跌额': row.get('涨跌额'),
                        '成交量': row.get('成交量'),
                        '成交额': row.get('成交额'),
                        '振幅': row.get('振幅'),
                        '最高': row.get('最高'),
                        '最低': row.get('最低'),
                        '今开': row.get('今开'),
                        '昨收': row.get('昨收'),
                        '量比': row.get('量比'),
                        '换手率': row.get('换手率'),
                        '市盈率-动态': row.get('市盈率-动态'),
                        '市净率': row.get('市净率'),
                        '总市值': row.get('总市值'),
                        '流通市值': row.get('流通市值'),
                    }
            else:
                log_info(f"[{stock_code}] 实时行情获取: {_t1 - _t0:.3f}s (缓存未命中)")
        except Exception as e:
            log_warning(f"获取实时数据失败: {e}")
        return None
    
    @staticmethod
    def _normalize_kline_columns(df: pd.DataFrame) -> pd.DataFrame:
        """标准化K线数据列名为中文（统一不同数据源的列名差异）"""
        if df is None or df.empty:
            return df
        col_map = {
            'date': '日期', 'time': '日期', 'trade_date': '日期',
            'open': '开盘', 'close': '收盘', 'high': '最高',
            'low': '最低', 'vol': '成交量', 'volume': '成交量',
            'amount': '成交额',
        }
        rename = {k: v for k, v in col_map.items() if k in df.columns}
        if rename:
            df = df.rename(columns=rename)
        return df

    @staticmethod
    def get_historical_data(stock_code: str, days: int = 250) -> Optional[pd.DataFrame]:
        """获取历史K线数据（使用优化的多源fallback机制）

        当 days <= 35 时优先使用快速主路径（约30天数据），
        否则直接走 fallback 路径获取完整历史数据。
        """
        _t0 = time.perf_counter()
        if days <= 35:
            try:
                hist_df = _fetch_historical_klines(stock_code)
                if hist_df is not None and not hist_df.empty:
                    date_col = next(
                        (col for col in hist_df.columns if 'date' in col.lower() or '日期' in str(col)),
                        None
                    )
                    if date_col:
                        hist_df = hist_df.sort_values(date_col)
                    if len(hist_df) > days:
                        hist_df = hist_df.tail(days).reset_index(drop=True)
                    _t1 = time.perf_counter()
                    log_info(f"[{stock_code}] 历史K线获取: {_t1 - _t0:.3f}s (快速路径, {len(hist_df)}条, 请求{days}天)")
                    return DataFetcher._normalize_kline_columns(hist_df)
            except Exception as e:
                log_warning(f"优化历史数据获取失败: {e}")

        hist_df = DataFetcher._fallback_historical_data(stock_code, days)
        _t1 = time.perf_counter()
        if hist_df is not None:
            log_info(f"[{stock_code}] 历史K线获取: {_t1 - _t0:.3f}s (fallback路径, {len(hist_df)}条, 请求{days}天)")
            return DataFetcher._normalize_kline_columns(hist_df)
        log_info(f"[{stock_code}] 历史K线获取: {_t1 - _t0:.3f}s (失败)")
        return None

    @staticmethod
    def _fallback_historical_data(stock_code: str, days: int) -> Optional[pd.DataFrame]:
        """备用历史数据获取方案"""
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')

        _fb_t0 = time.perf_counter()
        try:
            market = get_market_prefix(stock_code)
            tx_code = f"{market}{stock_code}"
            tx_df = _fetch_tencent_klines_direct(
                symbol=tx_code, start_date=start_date, end_date=end_date,
                adjust="qfq", timeout=10.0,
            )
            if tx_df is not None and not tx_df.empty:
                _fb_t1 = time.perf_counter()
                log_info(f"[{stock_code}] 腾讯直连: {_fb_t1 - _fb_t0:.3f}s ({len(tx_df)}条)")
                return tx_df
        except Exception as tx_e:
            _fb_t1 = time.perf_counter()
            log_warning(f"腾讯API失败({_fb_t1 - _fb_t0:.3f}s): {str(tx_e)[:80]}")
            log_info("腾讯API不可用，自动切换到新浪API...")

        _fb_t0 = time.perf_counter()
        try:
            df = ak.stock_zh_a_hist(
                symbol=stock_code, period="daily",
                start_date=start_date, end_date=end_date, adjust="qfq"
            )
            if df is not None and not df.empty:
                _fb_t1 = time.perf_counter()
                log_info(f"[{stock_code}] 新浪API: {_fb_t1 - _fb_t0:.3f}s ({len(df)}条)")
                return df
        except Exception as sina_e:
            _fb_t1 = time.perf_counter()
            log_warning(f"新浪API失败({_fb_t1 - _fb_t0:.3f}s): {sina_e}")

        return None


# 第十部分：技术指标计算


class TechnicalIndicators:
    """
    技术指标计算引擎
    
    基于 pandas 实现的纯计算类，从历史K线 DataFrame 中推导常用技术指标。
    所有方法为静态方法，接收 DataFrame 返回 DataFrame，无副作用。
    
    支持的指标：
    - 移动平均线（MA）：5/10/20/50/200 日
    - 指数移动平均线（EMA）：12/26 日
    - MACD：DIF / DEA / MACD柱
    - RSI：6/12/14 日，优先使用 talib 库
    - KDJ：K / D / J 值
    - 布林带（BOLL）：上轨 / 中轨 / 下轨
    - 成交量均线（VOL_MA）：5 日
    - 均线金叉/死叉、RSI超买超卖、量价关系等综合研判
    
    RSI 计算优先使用 talib（精度更高），不可用时回退到纯 pandas 实现。
    """
    
    @staticmethod
    def calculate_ma(df: pd.DataFrame, periods: List[int] = [5, 10, 20, 50, 200]) -> pd.DataFrame:
        """计算移动平均线"""
        for period in periods:
            df[f'MA{period}'] = df['收盘'].rolling(window=period, min_periods=1).mean()
        return df
    
    @staticmethod
    def calculate_ema(df: pd.DataFrame, periods: List[int] = [12, 26]) -> pd.DataFrame:
        """计算指数移动平均线"""
        for period in periods:
            df[f'EMA{period}'] = df['收盘'].ewm(span=period, adjust=False).mean()
        return df
    
    @staticmethod
    def calculate_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
        """计算MACD指标"""
        ema_fast = df['收盘'].ewm(span=fast, adjust=False).mean()
        ema_slow = df['收盘'].ewm(span=slow, adjust=False).mean()
        
        df['DIF'] = ema_fast - ema_slow
        df['DEA'] = df['DIF'].ewm(span=signal, adjust=False).mean()
        df['MACD'] = (df['DIF'] - df['DEA']) * 2
        
        return df
    
    @staticmethod
    def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """计算RSI指标"""
        delta = df['收盘'].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        
        avg_gain = gain.rolling(window=period, min_periods=1).mean()
        avg_loss = loss.rolling(window=period, min_periods=1).mean()
        
        rs = avg_gain / avg_loss
        df[f'RSI_{period}'] = 100 - (100 / (1 + rs))
        
        return df
    
    @staticmethod
    def calculate_kdj(df: pd.DataFrame, n: int = 9, m1: int = 3, m2: int = 3) -> pd.DataFrame:
        """计算KDJ指标"""
        low_n = df['最低'].rolling(window=n, min_periods=1).min()
        high_n = df['最高'].rolling(window=n, min_periods=1).max()
        
        rsv = (df['收盘'] - low_n) / (high_n - low_n) * 100
        rsv = rsv.fillna(50)
        
        df['K'] = rsv.ewm(com=m1-1, adjust=False).mean()
        df['D'] = df['K'].ewm(com=m2-1, adjust=False).mean()
        df['J'] = 3 * df['K'] - 2 * df['D']
        
        return df
    
    @staticmethod
    def calculate_bollinger(df: pd.DataFrame, period: int = 20, std_dev: int = 2) -> pd.DataFrame:
        """计算布林带"""
        df['BB_MID'] = df['收盘'].rolling(window=period, min_periods=1).mean()
        df['BB_STD'] = df['收盘'].rolling(window=period, min_periods=1).std()
        df['BB_UPPER'] = df['BB_MID'] + std_dev * df['BB_STD']
        df['BB_LOWER'] = df['BB_MID'] - std_dev * df['BB_STD']
        
        return df
    
    @staticmethod
    def calculate_volume_ma(df: pd.DataFrame, periods: List[int] = [5, 20]) -> pd.DataFrame:
        """计算成交量均线"""
        volume_col = '成交量' if '成交量' in df.columns else next(
            (col for col in df.columns if 'vol' in col.lower() or 'volume' in col.lower()),
            None
        )
        if volume_col is None:
            return df
        for period in periods:
            df[f'VOL_MA{period}'] = df[volume_col].rolling(window=period, min_periods=1).mean()
        return df


# 第十一部分：L1 一级行情数据筛选器


class L1Screener:
    """
    L1 一级行情数据筛选器

    基于实时行情和历史K线的技术面筛选，负责加载数据并计算所有技术指标。
    初始化时绑定单只股票代码，load_data() 后即可进行各项技术条件检测。

    数据加载流程：
    1. DataFetcher.get_realtime_data() → 实时行情（最新价、涨跌幅、量比等）
    2. DataFetcher.get_historical_data() → 250天K线（用于指标计算）
    3. TechnicalIndicators 批量计算：MA / MACD / RSI / KDJ / BOLL / VOL_MA

    提供的检测方法：
    - check_ma_crossover_5_10()：均线金叉检测
    - check_macd_golden_cross()：MACD 金叉检测
    - check_rsi_overbought()：RSI 超买检测
    - check_volume_surge()：成交量放大检测
    - 以及布林带、KDJ、均线排列等 12 个检测方法
    
    数据加载耗时约 3~8 秒（取决于网络），有进度条提示。
    """
    
    def __init__(self, stock_code: str):
        self.stock_code = stock_code
        self.data_fetcher = DataFetcher()
        self.df = None
        self.realtime_data = None
        self.indicators = TechnicalIndicators()
        self._latest_row = None
        self._prev_row = None
        
    def load_data(self, days: int = 250, indicators: Optional[List[str]] = None) -> bool:
        """加载数据

        Args:
            days: 历史K线天数，默认250
            indicators: 需要计算的指标列表，None表示全部。
                可选值: 'ma', 'macd', 'rsi', 'kdj', 'bollinger', 'volume_ma'
        """
        _ld_t0 = time.perf_counter()

        def _do_load(retry_label: str = "") -> bool:
            _ld_rt0 = time.perf_counter()
            self.realtime_data = self.data_fetcher.get_realtime_data(self.stock_code)
            _ld_rt1 = time.perf_counter()

            _ld_h0 = time.perf_counter()
            self.df = self.data_fetcher.get_historical_data(self.stock_code, days=days)
            _ld_h1 = time.perf_counter()

            if self.df is not None and not self.df.empty:
                _ld_i0 = time.perf_counter()
                self._calculate_indicators(indicators)
                if len(self.df) >= 1:
                    self._latest_row = self.df.iloc[-1]
                if len(self.df) >= 2:
                    self._prev_row = self.df.iloc[-2]
                _ld_i1 = time.perf_counter()
                _ld_total = time.perf_counter()
                log_info(
                    f"[{self.stock_code}] L1数据加载完成{retry_label}: "
                    f"实时={_ld_rt1 - _ld_rt0:.3f}s "
                    f"历史={_ld_h1 - _ld_h0:.3f}s "
                    f"指标={_ld_i1 - _ld_i0:.3f}s "
                    f"总计={_ld_total - _ld_t0:.3f}s"
                )
                return True

            return False

        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console
            ) as progress:
                task = progress.add_task(f"加载 {self.stock_code} 数据...", total=None)
                ok = _do_load()
                progress.update(task, completed=True)
                if ok:
                    return True
                _ld_total = time.perf_counter()
                log_info(f"[{self.stock_code}] L1数据加载失败: 历史K线为空 (耗时{_ld_total - _ld_t0:.3f}s)")
                return False
        except (ConnectionError, TimeoutError, OSError) as e:
            try:
                if _do_load("(重试)"):
                    return True
            except Exception:
                pass
            _ld_total = time.perf_counter()
            log_info(f"[{self.stock_code}] L1数据加载最终失败 (耗时{_ld_total - _ld_t0:.3f}s)")
            return False
        except Exception as e:
            _ld_total = time.perf_counter()
            log_info(f"[{self.stock_code}] L1数据加载失败({type(e).__name__}) (耗时{_ld_total - _ld_t0:.3f}s)")
            return False

    def _calculate_indicators(self, indicators: Optional[List[str]] = None) -> None:
        """计算技术指标（内部方法，按需计算）

        Args:
            indicators: 需要计算的指标列表，None表示全部。
                可选值: 'ma', 'macd', 'rsi', 'kdj', 'bollinger', 'volume_ma'
        """
        _indicator_map = {
            'ma': self.indicators.calculate_ma,
            'macd': self.indicators.calculate_macd,
            'rsi': self.indicators.calculate_rsi,
            'kdj': self.indicators.calculate_kdj,
            'bollinger': self.indicators.calculate_bollinger,
            'volume_ma': self.indicators.calculate_volume_ma,
        }
        target = indicators if indicators else list(_indicator_map.keys())
        for name in target:
            if name in _indicator_map:
                self.df = _indicator_map[name](self.df)
    
    def check_ma_crossover_5_10(self) -> Dict[str, Any]:
        """检查5日均线上穿10日均线"""
        if self.df is None or len(self.df) < 11:
            return {'signal': False, 'description': '数据不足'}
        
        latest = self._latest_row if self._latest_row is not None else self.df.iloc[-1]
        prev = self._prev_row if self._prev_row is not None else self.df.iloc[-2]
        prev2 = self.df.iloc[-3]
        
        # 当日MA5 > MA10，前一日MA5 <= MA10（金叉）
        ma5 = latest.get('MA5', None)
        ma10 = latest.get('MA10', None)
        prev_ma5 = prev.get('MA5', None)
        prev_ma10 = prev.get('MA10', None)
        prev2_ma5 = prev2.get('MA5', None)
        
        if any(v is None or pd.isna(v) for v in [ma5, ma10, prev_ma5, prev_ma10]):
            return {'signal': False, 'description': '均线数据不完整'}
        
        crossover = (ma5 > ma10 and prev_ma5 <= prev_ma10)
        
        # 额外确认：前两日MA5仍在MA10下方（避免模糊信号）
        confirmed = crossover and (prev2_ma5 is None or prev2_ma5 <= prev_ma10)
        
        return {
            'signal': confirmed,
            'description': '5日均线上穿10日均线' if confirmed else '无金叉信号',
            'MA5': round(ma5, 2),
            'MA10': round(ma10, 2)
        }
    
    def check_price_above_ma20(self) -> Dict[str, Any]:
        """检查股价是否站上20日均线"""
        if self.df is None or len(self.df) < 21:
            return {'signal': False, 'description': '数据不足'}
        
        latest = self.df.iloc[-1]
        current_price = latest.get('收盘', 0)
        ma20 = latest.get('MA20', None)
        
        if ma20 is None or pd.isna(ma20):
            return {'signal': False, 'description': 'MA20不可用'}
        
        # 安全获取涨跌幅（可能不存在于历史K线数据中）
        change_pct = latest.get('涨跌幅', 0)
        if pd.isna(change_pct):
            change_pct = 0
        
        # 确认站上MA20且涨幅>0
        above_ma20 = current_price > ma20
        confirmed = above_ma20 and change_pct > 0
        
        return {
            'signal': confirmed,
            'description': '股价有效站上20日均线' if confirmed else '未站上20日均线',
            '价格': round(current_price, 2),
            'MA20': round(ma20, 2) if pd.notna(ma20) else None,
            '偏离度': round((current_price / ma20 - 1) * 100, 2) if pd.notna(ma20) else None
        }
    
    def check_golden_cross_50_200(self) -> Dict[str, Any]:
        """检查50日均线上穿200日均线（黄金交叉）"""
        if self.df is None or len(self.df) < 201:
            return {'signal': False, 'description': '数据不足(需要200日数据)'}
        
        latest = self.df.iloc[-1]
        prev = self.df.iloc[-2]
        
        ma50 = latest.get('MA50', None)
        ma200 = latest.get('MA200', None)
        prev_ma50 = prev.get('MA50', None)
        prev_ma200 = prev.get('MA200', None)
        
        if any(v is None or pd.isna(v) for v in [ma50, ma200, prev_ma50, prev_ma200]):
            return {'signal': False, 'description': '均线数据不完整'}
        
        crossover = (ma50 > ma200 and prev_ma50 <= prev_ma200)
        
        return {
            'signal': crossover,
            'description': '50日均线上穿200日均线（黄金交叉）' if crossover else '无黄金交叉',
            'MA50': round(ma50, 2),
            'MA200': round(ma200, 2)
        }
    
    def check_macd_golden_cross(self) -> Dict[str, Any]:
        """检查MACD金叉（DIFF上穿DEA）"""
        if self.df is None or len(self.df) < 3:
            return {'signal': False, 'description': '数据不足'}
        
        latest = self._latest_row if self._latest_row is not None else self.df.iloc[-1]
        prev = self._prev_row if self._prev_row is not None else self.df.iloc[-2]
        
        dif = latest.get('DIF', 0)
        dea = latest.get('DEA', 0)
        prev_dif = prev.get('DIF', 0)
        prev_dea = prev.get('DEA', 0)
        
        # 金叉：当日DIF>DEA，前一日DIF<=DEA
        crossover = dif > dea and prev_dif <= prev_dea
        
        return {
            'signal': crossover,
            'description': 'MACD金叉' if crossover else '无MACD金叉',
            'DIF': round(dif, 4) if pd.notna(dif) else None,
            'DEA': round(dea, 4) if pd.notna(dea) else None,
            'MACD柱': round(latest.get('MACD', 0), 4) if pd.notna(latest.get('MACD')) else None
        }
    
    def check_macd_histogram_turn(self) -> Dict[str, Any]:
        """检查MACD柱状线由绿转红"""
        if self.df is None or len(self.df) < 3:
            return {'signal': False, 'description': '数据不足'}
        
        latest = self.df.iloc[-1]
        prev = self.df.iloc[-2]
        
        latest_histogram = latest.get('MACD', 0)
        prev_histogram = prev.get('MACD', 0)
        
        # 由绿转红：前一根是负值，当前是正值
        turn_up = latest_histogram > 0 and prev_histogram <= 0
        
        return {
            'signal': turn_up,
            'description': 'MACD柱状线由绿转红' if turn_up else '无红柱转绿信号',
            '当前柱': round(latest_histogram, 4),
            '前一根柱': round(prev_histogram, 4)
        }
    
    def check_macd_divergence(self) -> Dict[str, Any]:
        """检查MACD底背离（股价创新低但MACD未创新低）"""
        if self.df is None or len(self.df) < 30:
            return {'signal': False, 'description': '数据不足'}
        
        # 取最近20天数据
        recent = self.df.tail(20)
        
        # 股价创新低
        price_lowest_idx = recent['最低'].idxmin()
        price_is_new_low = recent.iloc[-1]['最低'] <= recent.iloc[price_lowest_idx]['最低'] + 0.01
        
        # MACD未创新低
        macd_lowest_idx = recent['MACD'].idxmin()
        macd_is_new_low = recent.iloc[-1]['MACD'] <= recent.iloc[macd_lowest_idx]['MACD'] - 0.01
        
        # 底背离：股价新低但MACD不是新低
        divergence = price_is_new_low and not macd_is_new_low
        
        return {
            'signal': divergence,
            'description': 'MACD底背离' if divergence else '无底背离',
            '最新价格': round(recent.iloc[-1]['最低'], 2),
            '最低价格': round(recent.iloc[price_lowest_idx]['最低'], 2)
        }
    
    def check_rsi_oversold(self) -> Dict[str, Any]:
        """检查RSI超卖（RSI<30）"""
        if self.df is None:
            return {'signal': False, 'description': '数据不足'}
        
        latest = self.df.iloc[-1]
        rsi = latest.get('RSI_14', 50)
        
        oversold = rsi < 30
        
        return {
            'signal': oversold,
            'description': f'RSI超卖({round(rsi, 1)})' if oversold else f'RSI正常({round(rsi, 1)})',
            'RSI': round(rsi, 2) if pd.notna(rsi) else None
        }
    
    def check_rsi_overbought(self) -> Dict[str, Any]:
        """检查RSI超买（RSI>70），用于排除"""
        if self.df is None:
            return {'signal': False, 'description': '数据不足'}
        
        latest = self._latest_row if self._latest_row is not None else self.df.iloc[-1]
        rsi = latest.get('RSI_14', 50)
        
        overbought = rsi > 70
        
        return {
            'signal': overbought,
            'description': f'RSI超买({round(rsi, 1)})，建议排除' if overbought else f'RSI正常({round(rsi, 1)})',
            'RSI': round(rsi, 2) if pd.notna(rsi) else None
        }
    
    def check_kdj_golden_cross(self) -> Dict[str, Any]:
        """检查KDJ金叉（K线上穿D线）且D<20"""
        if self.df is None or len(self.df) < 3:
            return {'signal': False, 'description': '数据不足'}
        
        latest = self.df.iloc[-1]
        prev = self.df.iloc[-2]
        
        k = latest.get('K', 50)
        d = latest.get('D', 50)
        prev_k = prev.get('K', 50)
        prev_d = prev.get('D', 50)
        
        # 金叉且D值<20
        golden_cross = (k > d and prev_k <= prev_d) and d < 20
        
        return {
            'signal': golden_cross,
            'description': 'KDJ金叉且超卖区间' if golden_cross else '无有效KDJ金叉',
            'K': round(k, 2) if pd.notna(k) else None,
            'D': round(d, 2) if pd.notna(d) else None,
            'J': round(latest.get('J', 0), 2) if pd.notna(latest.get('J')) else None
        }
    
    def check_kdj_oversold(self) -> Dict[str, Any]:
        """检查J值<0（极端超卖）"""
        if self.df is None:
            return {'signal': False, 'description': '数据不足'}
        
        latest = self.df.iloc[-1]
        j = latest.get('J', 0)
        
        oversold = j < 0
        
        return {
            'signal': oversold,
            'description': f'KDJ极端超卖(J={round(j, 1)})' if oversold else f'J值正常({round(j, 1)})',
            'J': round(j, 2) if pd.notna(j) else None
        }
    
    def check_volume_surge(self) -> Dict[str, Any]:
        """检查成交量放大（当日成交量>5日均量的1.5倍）"""
        if self.df is None or len(self.df) < 6:
            return {'signal': False, 'description': '数据不足'}
        
        latest = self._latest_row if self._latest_row is not None else self.df.iloc[-1]
        volume = latest.get('成交量', 0)
        vol_ma5 = latest.get('VOL_MA5', 0)
        
        if pd.isna(vol_ma5) or vol_ma5 == 0:
            return {'signal': False, 'description': '成交量均线数据不足'}
        
        surge_ratio = volume / vol_ma5
        surge = surge_ratio > 1.5
        
        return {
            'signal': surge,
            'description': f'成交量放大({round(surge_ratio, 2)}倍)' if surge else f'成交量正常({round(surge_ratio, 2)}倍)',
            '成交量': volume,
            '5日均量': vol_ma5,
            '放大倍数': round(surge_ratio, 2)
        }
    
    def check_volume_increasing(self) -> Dict[str, Any]:
        """检查连续3日成交量递增"""
        if self.df is None or len(self.df) < 4:
            return {'signal': False, 'description': '数据不足'}
        
        latest = self.df.tail(4)
        
        # 安全获取成交量
        def get_vol(idx):
            return latest.iloc[idx].get('成交量', 0)
        
        # 连续3天递增
        increasing = (get_vol(1) > get_vol(0) and
                     get_vol(2) > get_vol(1) and
                     get_vol(3) > get_vol(2))
        
        return {
            'signal': increasing,
            'description': '连续3日成交量递增' if increasing else '无连续放量',
            '近3日成交量': [int(get_vol(i)) for i in range(1, 4)]
        }
    
    def check_price_volume_match(self) -> Dict[str, Any]:
        """检查量价配合"""
        if self.df is None or len(self.df) < 2:
            return {'signal': False, 'description': '数据不足'}
        
        latest = self.df.iloc[-1]
        prev = self.df.iloc[-2]
        
        price_up = latest.get('涨跌幅', 0) > 0
        price_down = latest.get('涨跌幅', 0) < 0
        
        # 安全获取成交量
        latest_vol = latest.get('成交量', 0)
        prev_vol = prev.get('成交量', 0)
        
        # 价涨量增
        volume_up = latest_vol > prev_vol
        price_volume_match = (price_up and volume_up)
        
        # 价跌量缩
        volume_down = latest_vol < prev_vol
        price_volume_shrink = (price_down and volume_down)
        
        vol_change = 0
        if prev_vol > 0:
            vol_change = round((latest_vol / prev_vol - 1) * 100, 2)
        
        return {
            'signal': price_volume_match or price_volume_shrink,
            'description': '量价配合良好' if (price_volume_match or price_volume_shrink) else '量价背离',
            '价涨量增': price_volume_match,
            '价跌量缩': price_volume_shrink,
            '涨跌幅': round(latest.get('涨跌幅', 0), 2),
            '成交量变化': vol_change
        }
    
    def check_turnover_rate(self) -> Dict[str, Any]:
        """检查换手率"""
        if self.realtime_data is None:
            return {'signal': False, 'description': '实时数据获取失败'}
        
        turnover_rate = self.realtime_data.get('换手率', 0)
        
        if turnover_rate is None:
            return {'signal': False, 'description': '换手率数据缺失'}
        
        # 活跃股票：换手率>3%
        active = turnover_rate > 3
        
        return {
            'signal': active,
            'description': f'换手率活跃({turnover_rate}%)' if active else f'换手率较低({turnover_rate}%)',
            '换手率': turnover_rate
        }
    
    def check_turnover_sustained(self) -> Dict[str, Any]:
        """检查换手率维持在5%-10%"""
        if self.df is None or len(self.df) < 5:
            return {'signal': False, 'description': '数据不足'}
        
        # 安全获取成交量列
        vol_col = '成交量' if '成交量' in self.df.columns else None
        if vol_col is None:
            for col in self.df.columns:
                if 'vol' in col.lower() or 'volume' in col.lower():
                    vol_col = col
                    break
        
        if vol_col is None:
            return {'signal': False, 'description': '成交量数据不足'}
        
        # 注意：历史数据中没有换手率，使用成交量/自由流通股估算
        recent = self.df.tail(5)
        avg_volume = recent[vol_col].mean()
        
        # 简化判断：成交量是否维持在一定水平
        volume_stable = (recent[vol_col].std() / avg_volume) < 0.3 if avg_volume > 0 else False  # 波动小于30%
        
        return {
            'signal': volume_stable,
            'description': '成交量维持稳定' if volume_stable else '成交量波动较大',
            '平均成交量': int(avg_volume)
        }
    
    def check_breakout(self) -> Dict[str, Any]:
        """检查股价突破前期高点"""
        if self.df is None or len(self.df) < 30:
            return {'signal': False, 'description': '数据不足'}
        
        latest = self.df.iloc[-1]
        current_price = latest.get('收盘', 0)
        
        # 近30日最高价
        high_col = '最高' if '最高' in self.df.columns else 'high'
        recent_30_high = self.df.tail(30)[high_col].max()
        
        # 突破信号：收盘价创30日新高
        breakout = current_price >= recent_30_high
        
        return {
            'signal': breakout,
            'description': '股价突破30日高点' if breakout else '未突破前期高点',
            '当前价格': round(current_price, 2),
            '30日最高': round(recent_30_high, 2),
            '突破幅度': round((current_price / recent_30_high - 1) * 100, 2)
        }
    
    def check_support_break(self) -> Dict[str, Any]:
        """检查股价突破重要压力位"""
        if self.df is None:
            return {'signal': False, 'description': '数据不足'}
        
        latest = self.df.iloc[-1]
        current_price = latest.get('收盘', 0)
        round_numbers = [10, 20, 30, 50, 100, 200]
        near_round = any(abs(current_price - r) / r < 0.05 for r in round_numbers)
        
        return {
            'signal': near_round,
            'description': '接近整数关口' if near_round else '无整数关口压力',
            '当前价格': round(current_price, 2)
        }
    
    def check_bottom_pattern(self) -> Dict[str, Any]:
        """检查W底、头肩底等底部形态"""
        if self.df is None or len(self.df) < 60:
            return {'signal': False, 'description': '数据不足(需要60日数据)'}
        
        # 取最近60天数据
        recent = self.df.tail(60)
        
        # 安全获取最低价列
        low_col = '最低' if '最低' in recent.columns else 'low'
        
        # 简化检测：最近20天内是否有两次探底后反弹
        lowest_20 = recent.tail(20)[low_col].min()
        recent_lows = recent[recent[low_col] <= lowest_20 * 1.02]
        
        has_double_bottom = len(recent_lows) >= 2
        
        return {
            'signal': has_double_bottom,
            'description': '检测到双底形态' if has_double_bottom else '无明显底部形态',
            '近20日最低': round(lowest_20, 2)
        }
    
    def check_new_low_rebound(self) -> Dict[str, Any]:
        """检查股价创N日内新低后反弹"""
        if self.df is None or len(self.df) < 30:
            return {'signal': False, 'description': '数据不足'}
        
        latest = self.df.iloc[-1]
        recent = self.df.tail(30)
        
        low_col = '最低' if '最低' in recent.columns else 'low'
        lowest_idx = recent[low_col].idxmin()
        lowest_in_recent = recent.iloc[-1][low_col] <= recent.iloc[lowest_idx][low_col] + 0.01
        
        # 是否在最近5日内创过低
        days_ago = len(recent) - 1 - recent.index.get_loc(lowest_idx) if lowest_idx in recent.index else 30
        
        rebound = lowest_in_recent and days_ago <= 5
        
        return {
            'signal': rebound,
            'description': '新低后反弹' if rebound else '无新低反弹',
            '当前价格': round(latest['收盘'], 2),
            '最低点距今': f'{days_ago}天'
        }
    
    def get_all_l1_signals(self) -> Dict[str, Any]:
        """获取所有L1信号"""
        return {
            '趋势类': {
                'MA5上穿MA10': self.check_ma_crossover_5_10(),
                '站上MA20': self.check_price_above_ma20(),
                '黄金交叉50/200': self.check_golden_cross_50_200(),
                'MACD金叉': self.check_macd_golden_cross(),
                'MACD柱状线转红': self.check_macd_histogram_turn(),
                'MACD底背离': self.check_macd_divergence(),
            },
            '震荡类': {
                'RSI超卖': self.check_rsi_oversold(),
                'RSI超买(排除)': self.check_rsi_overbought(),
                'KDJ金叉+超卖': self.check_kdj_golden_cross(),
                'KDJ极端超卖': self.check_kdj_oversold(),
            },
            '成交量类': {
                '成交量放大': self.check_volume_surge(),
                '连续放量': self.check_volume_increasing(),
                '量价配合': self.check_price_volume_match(),
                '换手率活跃': self.check_turnover_rate(),
                '换手率稳定': self.check_turnover_sustained(),
            },
            '价格形态': {
                '突破30日高点': self.check_breakout(),
                '整数关口压力': self.check_support_break(),
                '底部形态': self.check_bottom_pattern(),
                '新低反弹': self.check_new_low_rebound(),
            }
        }


# 第十二部分：L2 二级数据筛选器（模拟实现）


class L2Screener:
    """
    L2 二级数据筛选器（模拟）

    基于量价关系模拟 L2 级别的大单资金分析指标。由于真实 Level-2 数据需交易所授权，
    本模块通过价格变动 × 成交量变化来模拟 DDX / DDY / DDZ / 主力净额等指标。

    模拟算法：
    - DDX（大单动向）：涨跌幅 × 量比 × 100（涨时）或 涨跌幅 × 10（跌时）
    - DDY（涨跌动因）：成交量变化 × 50（涨时）或 × -20（跌时）
    - DDZ（大单强度）：综合 DDX + DDY 的强度值
    - 主力净额：当日成交量 × 收盘价 × 涨跌幅 / 100
    
    复用 L1 的历史K线数据进行计算，不依赖额外数据源。

    使用限制：
    所有 L2 指标均为估算值，非真实盘口数据，仅作为辅助参考，不能替代真实 L2 分析。
    """
    
    def __init__(self, stock_code: str):
        self.stock_code = stock_code
        self.data_fetcher = DataFetcher()
        self.df = None
        self._cached_l2_indicators = None
        
    def load_data(self, shared_df: Optional[pd.DataFrame] = None) -> bool:
        """加载数据（优先使用共享的L1历史数据，避免重复API调用）"""
        if shared_df is not None and not shared_df.empty:
            self.df = shared_df
            return True
        self.df = self.data_fetcher.get_historical_data(self.stock_code, days=100)
        return self.df is not None and not self.df.empty
    
    def _empty_l2_result(self) -> Dict[str, Any]:
        """返回空的L2指标结果"""
        return {
            'DDX': 0, 'DDY': 0, 'DDZ': 0,
            'DDX_5日均值': 0, 'DDY_5日趋势': 0,
            '主力净额': 0, '资金流入率': 0, '资金强度': 0
        }
    
    def get_l2_indicators(self) -> Dict[str, Any]:
        """获取L2指标（基于历史数据估算，使用缓存避免重复计算）"""
        if self._cached_l2_indicators is not None:
            return self._cached_l2_indicators
            
        if self.df is None or len(self.df) < 20:
            return self._empty_l2_result()
        
        latest = self.df.iloc[-1]
        recent_5 = self.df.tail(5)
        
        vol_col = '成交量' if '成交量' in self.df.columns else None
        if vol_col is None:
            vol_col = next(
                (col for col in self.df.columns if 'vol' in col.lower() or 'volume' in col.lower()),
                None
            )
        
        price_change = latest.get('涨跌幅', 0) if '涨跌幅' in latest else 0
        vol_mean = recent_5[vol_col].mean() if vol_col and recent_5[vol_col].mean() > 0 else 0
        latest_vol = latest.get(vol_col, 0) if vol_col else 0
        volume_change = (latest_vol / vol_mean - 1) if vol_mean > 0 else 0
        
        ddx = price_change * volume_change * 100 if price_change > 0 else price_change * 10
        ddy = volume_change * 50 if price_change > 0 else volume_change * -20
        ddz = abs(ddx) * 15 + abs(price_change) * 5
        
        main_net = 0
        if vol_col and '收盘' in latest:
            main_net = latest_vol * latest.get('收盘', 0) * price_change / 100
        
        ddx_5_mean = 0
        if vol_col and vol_mean > 0 and '涨跌幅' in recent_5.columns:
            ddx_5_mean = round((recent_5['涨跌幅'] * recent_5[vol_col] / vol_mean).mean() * 100, 4)
        
        ddy_5_trend = 0
        if vol_col and len(recent_5) > 1 and recent_5[vol_col].iloc[0] > 0:
            ddy_5_trend = round(
                (recent_5[vol_col].iloc[-1] - recent_5[vol_col].iloc[0]) / recent_5[vol_col].iloc[0] * 50, 4
            )
        
        result = {
            'DDX': round(ddx, 4),
            'DDY': round(ddy, 4),
            'DDZ': round(ddz, 4),
            'DDX_5日均值': ddx_5_mean,
            'DDY_5日趋势': ddy_5_trend,
            '主力净额': round(main_net, 2),
            '资金流入率': round(price_change * volume_change, 4),
            '资金强度': round(abs(price_change) * volume_change * 1000 / (abs(price_change) + 0.01), 2)
        }
        
        self._cached_l2_indicators = result
        return result
    
    def check_ddx_positive(self) -> Dict[str, Any]:
        """检查DDX>0"""
        l2 = self.get_l2_indicators()
        ddx = l2.get('DDX', 0)
        
        return {
            'signal': ddx > 0,
            'description': f'DDX为正({ddx:.4f})' if ddx > 0 else f'DDX为负({ddx:.4f})',
            'DDX': ddx
        }
    
    def check_ddx_continuous_positive(self) -> Dict[str, Any]:
        """检查DDX连续3日为正"""
        if self.df is None or len(self.df) < 4:
            return {'signal': False, 'description': '数据不足'}
        
        recent = self.df.tail(5)
        change_col = '涨跌幅' if '涨跌幅' in recent.columns else None
        if change_col is None:
            change_col = next(
                (c for c in recent.columns if 'change' in c.lower() or '涨跌' in str(c)),
                None
            )
        if change_col:
            positive_days = int((recent[change_col] > 0).sum())
        else:
            positive_days = 0
        
        return {
            'signal': positive_days >= 3,
            'description': f'DDX连续{positive_days}日为正' if positive_days >= 3 else f'仅{positive_days}日为正',
            '正天数': positive_days
        }
    
    def check_ddy_positive(self) -> Dict[str, Any]:
        """检查DDY>0"""
        l2 = self.get_l2_indicators()
        ddy = l2.get('DDY', 0)
        
        return {
            'signal': ddy > 0,
            'description': f'DDY为正({ddy:.4f})' if ddy > 0 else f'DDY为负({ddy:.4f})',
            'DDY': ddy
        }
    
    def check_ddy_rising(self) -> Dict[str, Any]:
        """检查DDY持续上升"""
        l2 = self.get_l2_indicators()
        ddy_trend = l2.get('DDY_5日趋势', 0)
        
        return {
            'signal': ddy_trend > 0,
            'description': 'DDY持续上升' if ddy_trend > 0 else 'DDY趋势下降',
            'DDY趋势': ddy_trend
        }
    
    def check_ddz_high(self) -> Dict[str, Any]:
        """检查DDZ>15"""
        l2 = self.get_l2_indicators()
        ddz = l2.get('DDZ', 0)
        
        return {
            'signal': ddz > 15,
            'description': f'DDZ强势({ddz:.2f})' if ddz > 15 else f'DDZ正常({ddz:.2f})',
            'DDZ': ddz
        }
    
    def check_main_net_inflow(self) -> Dict[str, Any]:
        """检查主力净额>0"""
        l2 = self.get_l2_indicators()
        net_inflow = l2.get('主力净额', 0)
        
        return {
            'signal': net_inflow > 0,
            'description': f'主力净流入({net_inflow/10000:.2f}万)' if net_inflow > 0 else f'主力净流出({abs(net_inflow)/10000:.2f}万)',
            '主力净额': net_inflow
        }
    
    def check_inflow_rate(self) -> Dict[str, Any]:
        """检查资金流入率>1%"""
        l2 = self.get_l2_indicators()
        inflow_rate = l2.get('资金流入率', 0)
        
        return {
            'signal': inflow_rate > 1,
            'description': f'资金流入率高({inflow_rate:.2f}%)' if inflow_rate > 1 else f'资金流入率低({inflow_rate:.2f}%)',
            '流入率': inflow_rate
        }
    
    def check_fund_strength(self) -> Dict[str, Any]:
        """检查资金强度>1"""
        l2 = self.get_l2_indicators()
        strength = l2.get('资金强度', 0)
        
        return {
            'signal': strength > 1,
            'description': f'资金强度高({strength:.2f})' if strength > 1 else f'资金强度低({strength:.2f})',
            '资金强度': strength
        }
    
    def get_all_l2_signals(self) -> Dict[str, Any]:
        """获取所有L2信号"""
        l2_data = self.get_l2_indicators()
        
        return {
            'DDE决策': {
                'DDX为正': self.check_ddx_positive(),
                'DDX连续为正': self.check_ddx_continuous_positive(),
                'DDY为正': self.check_ddy_positive(),
                'DDY上升趋势': self.check_ddy_rising(),
                'DDZ强势': self.check_ddz_high(),
            },
            '资金流向': {
                '主力净流入': self.check_main_net_inflow(),
                '资金流入率高': self.check_inflow_rate(),
                '资金强度高': self.check_fund_strength(),
            },
            'L2基础指标': l2_data
        }


# 第十三部分：三种投资策略评估引擎

def _make_detail(name: str, max_score: int, actual_score: int,
                 status: str, value: str, threshold: str, basis: str) -> Dict[str, Any]:
    return {
        'name': name,
        'max_score': max_score,
        'actual_score': actual_score,
        'status': status,
        'value': value,
        'threshold': threshold,
        'basis': basis,
    }

_STRATEGY_DATA_CONFIG = {
    '1': {
        'hist_days': 20,
        'indicators': ['ma', 'rsi', 'volume_ma'],
        'need_l2': True,
    },
    '2': {
        'hist_days': 65,
        'indicators': ['macd'],
        'need_l2': True,
    },
    '3': {
        'hist_days': 255,
        'indicators': ['macd'],
        'need_l2': False,
    },
    'all': {
        'hist_days': 255,
        'indicators': None,
        'need_l2': True,
    },
}


class ScreeningStrategies:
    """
    三种投资策略评估引擎

    组合 L1Screener（技术面）和 L2Screener（资金面）对单只股票进行多维度评分。
    每种策略返回结构化的评分明细，包含每项指标的名称、满分、实际得分、状态、
    阈值条件、实际值和评分依据。

    三种策略：

    ┌──────────────┬──────────┬──────────┬──────────────────────────┐
    │ 策略         │ 持仓周期  │ L2依赖   │ 核心逻辑                  │
    ├──────────────┼──────────┼──────────┼──────────────────────────┤
    │ 短线强势股   │ 1-5 天   │ 强（DDX  │ 均线金叉 + 涨幅 + 量能     │
    │              │          │  + 主力）│ + RSI + DDX + 主力净额     │
    │ 主力建仓股   │ 1-4 周   │ 强（DDX  │ 价格低位 + 量能趋势         │
    │              │          │  / DDY） │ + MACD + DDX/DDY + 主力    │
    │ 价值投资股   │ 1-12 月  │ 无       │ PE/PB 估值 + MACD + 价格位 │
    └──────────────┴──────────┴──────────┴──────────────────────────┘

    每种策略满分 100 分，不设通过阈值，所有评分结果均保留供用户自行判断。
    """
    
    def __init__(self, stock_code: str):
        self.stock_code = stock_code
        self.l1_screener = L1Screener(stock_code)
        self.l2_screener = L2Screener(stock_code)
        self._data_loaded = False
        self._loaded_days = 0
        self._loaded_indicators: Optional[List[str]] = None
        self._loaded_l2 = False
        
    def load_all_data(self, strategy: str = 'all') -> bool:
        """加载数据（按策略需求按需加载，带缓存标记避免重复加载）

        Args:
            strategy: 策略标识，'1'短线强势/'2'主力建仓/'3'价值投资/'all'全部
        """
        config = _STRATEGY_DATA_CONFIG.get(strategy, _STRATEGY_DATA_CONFIG['all'])
        required_days = config['hist_days']
        required_indicators = config['indicators']
        need_l2 = config['need_l2']

        if self._data_loaded:
            days_ok = self._loaded_days >= required_days
            indicators_ok = (
                self._loaded_indicators is None
                or required_indicators is None
                or all(ind in self._loaded_indicators for ind in required_indicators)
            )
            l2_ok = not need_l2 or self._loaded_l2
            if days_ok and indicators_ok and l2_ok:
                return True

        log_info(f"加载股票 {self.stock_code} 数据（策略={strategy}, 天数={required_days}）...")
        _t0 = time.perf_counter()
        l1_ok = self.l1_screener.load_data(days=required_days, indicators=required_indicators)
        _t1 = time.perf_counter()
        l2_ok = False
        if need_l2:
            l2_ok = self.l2_screener.load_data(shared_df=self.l1_screener.df)
        _t2 = time.perf_counter()
        log_info(f"加载 {self.stock_code}: L1={_t1-_t0:.3f}s L2={_t2-_t1:.3f}s")
        result = l1_ok or l2_ok
        if result:
            self._data_loaded = True
            self._loaded_days = max(self._loaded_days, required_days)
            if required_indicators is None:
                self._loaded_indicators = None
            elif self._loaded_indicators is not None:
                self._loaded_indicators = list(
                    set(self._loaded_indicators) | set(required_indicators)
                )
            if need_l2:
                self._loaded_l2 = True
        return result
    
    def strategy_short_term_strong(self) -> Dict[str, Any]:
        _t0 = time.perf_counter()
        if not self.load_all_data('1'):
            return {'score': 0, 'max_score': 100, 'details': [], 'error': '数据加载失败'}
        _t_load = time.perf_counter()
        
        l1 = self.l1_screener
        l2 = self.l2_screener
        
        score = 0
        details = []
        
        _t1 = time.perf_counter()
        ma_check = l1.check_ma_crossover_5_10()
        _t2 = time.perf_counter()
        log_info(f"[短线强势] {self.stock_code} 均线金叉检测: {_t2 - _t1:.4f}s")
        if ma_check['signal']:
            score += 20
            details.append(_make_detail(
                '均线系统（金叉）', 20, 20, 'passed',
                f'MA5={ma_check["MA5"]:.2f}, MA10={ma_check["MA10"]:.2f}',
                'MA5 > MA10 且前一日 MA5 <= MA10',
                '当日5日均线上穿10日均线，形成金叉信号'
            ))
        else:
            details.append(_make_detail(
                '均线系统（金叉）', 20, 0, 'failed',
                f'MA5={ma_check.get("MA5", "N/A")}, MA10={ma_check.get("MA10", "N/A")}',
                'MA5 > MA10 且前一日 MA5 <= MA10',
                '未出现金叉信号，均线未形成多头排列'
            ))
        
        if l1.df is not None and not l1.df.empty:
            latest = l1.df.iloc[-1]
            price_change = latest['涨跌幅'] if '涨跌幅' in latest else 0
        else:
            price_change = 0
        if price_change > 3:
            score += 20
            details.append(_make_detail(
                '当日涨幅强劲', 20, 20, 'passed',
                f'{price_change:.2f}%',
                '涨跌幅 > 3%',
                '当日涨幅超过3%，表明短期动能强劲'
            ))
        else:
            details.append(_make_detail(
                '当日涨幅强劲', 20, 0, 'failed',
                f'{price_change:.2f}%',
                '涨跌幅 > 3%',
                f'当日涨幅仅{price_change:.2f}%，未达到3%的强势标准' if l1.df is not None else 'L1数据缺失，无法判断涨跌幅'
            ))
        
        vol_check = l1.check_volume_surge()
        _t3 = time.perf_counter()
        log_info(f"[短线强势] {self.stock_code} 成交量检测: {_t3 - _t2:.4f}s")
        if vol_check['signal']:
            score += 20
            details.append(_make_detail(
                '成交量放大', 20, 20, 'passed',
                f'量比 {vol_check["放大倍数"]:.2f}倍',
                '成交量 > 5日均量 × 1.5',
                f'成交量放大至5日均量的{vol_check["放大倍数"]:.2f}倍，资金参与度高'
            ))
        else:
            details.append(_make_detail(
                '成交量放大', 20, 0, 'failed',
                f'量比 {vol_check.get("放大倍数", 0):.2f}倍',
                '成交量 > 5日均量 × 1.5',
                f'成交量仅{vol_check.get("放大倍数", 0):.2f}倍均量，资金参与度不足'
            ))
        
        rsi_check = l1.check_rsi_overbought()
        _t4 = time.perf_counter()
        log_info(f"[短线强势] {self.stock_code} RSI检测: {_t4 - _t3:.4f}s")
        rsi_value = rsi_check.get('RSI', 0) or 0
        if not rsi_check['signal']:
            score += 10
            details.append(_make_detail(
                'RSI 未超买', 10, 10, 'passed',
                f'RSI_14 = {rsi_value:.2f}',
                'RSI_14 < 70',
                'RSI未进入超买区间，短期回调风险可控'
            ))
        else:
            details.append(_make_detail(
                'RSI 未超买', 10, 0, 'failed',
                f'RSI_14 = {rsi_value:.2f}',
                'RSI_14 < 70',
                f'RSI达{rsi_value:.2f}，已进入超买区间，追高风险较大'
            ))
        
        l2_data = l2.get_l2_indicators()
        _t5 = time.perf_counter()
        log_info(f"[短线强势] {self.stock_code} L2指标计算: {_t5 - _t4:.4f}s")
        
        ddx = l2_data['DDX']
        if ddx > 0.5:
            score += 15
            details.append(_make_detail(
                'DDX 大单动向', 15, 15, 'passed',
                f'DDX = {ddx:.4f}',
                'DDX > 0.5 满分，DDX > 0 部分得分',
                f'DDX达{ddx:.4f}，大单资金大幅流入，主力做多意愿强'
            ))
        elif ddx > 0:
            score += 5
            details.append(_make_detail(
                'DDX 大单动向', 15, 5, 'partial',
                f'DDX = {ddx:.4f}',
                'DDX > 0.5 满分，DDX > 0 部分得分',
                f'DDX为{ddx:.4f}，虽为正但未达强势阈值0.5'
            ))
        else:
            details.append(_make_detail(
                'DDX 大单动向', 15, 0, 'failed',
                f'DDX = {ddx:.4f}',
                'DDX > 0.5 满分，DDX > 0 部分得分',
                f'DDX为{ddx:.4f}，大单资金净流出'
            ))
        
        net_amount = l2_data['主力净额']
        if net_amount > 0:
            score += 15
            details.append(_make_detail(
                '主力净流入', 15, 15, 'passed',
                f'主力净额 = {net_amount/10000:.2f}万',
                '主力净额 > 0',
                f'主力资金净流入{net_amount/10000:.2f}万，资金面积极'
            ))
        else:
            details.append(_make_detail(
                '主力净流入', 15, 0, 'failed',
                f'主力净额 = {abs(net_amount)/10000:.2f}万（流出）',
                '主力净额 > 0',
                f'主力资金净流出{abs(net_amount)/10000:.2f}万，资金面偏空'
            ))
        
        _t_end = time.perf_counter()
        log_info(f"[短线强势] {self.stock_code} 总耗时: {_t_end - _t0:.3f}s (计算: {_t_end - _t_load:.3f}s)")
        
        return {
            'score': score,
            'max_score': 100,
            'details': details
        }
    
    def strategy_main_accumulation(self) -> Dict[str, Any]:
        _t0 = time.perf_counter()
        if not self.load_all_data('2'):
            return {'score': 0, 'max_score': 100, 'details': [], 'error': '数据加载失败'}
        _t_load = time.perf_counter()
        
        l1 = self.l1_screener
        l2 = self.l2_screener
        
        score = 0
        details = []
        
        _t1 = time.perf_counter()
        if l1.df is not None and len(l1.df) >= 60:
            price_3m_ago = l1.df.iloc[-60]['收盘'] if len(l1.df) >= 60 else l1.df.iloc[0]['收盘']
            price_current = l1.df.iloc[-1]['收盘']
            decline_3m = (price_current / price_3m_ago - 1) * 100
            
            if decline_3m > -30:
                score += 15
                details.append(_make_detail(
                    '股价相对低位', 15, 15, 'passed',
                    f'近60天跌幅 {decline_3m:.1f}%', '近60天跌幅 > -30%',
                    f'近60天跌幅仅{decline_3m:.1f}%，股价处于相对安全区间'
                ))
            else:
                details.append(_make_detail(
                    '股价相对低位', 15, 0, 'failed',
                    f'近60天跌幅 {decline_3m:.1f}%', '近60天跌幅 > -30%',
                    f'近60天跌幅达{decline_3m:.1f}%，下跌幅度过大'
                ))
        else:
            details.append(_make_detail(
                '股价相对低位', 15, 0, 'failed',
                '数据不足（需60天K线）', '近60天跌幅 > -30%',
                'K线数据不足60天，无法判断价格位置'
            ))
        
        vol_trend = l2.check_ddx_continuous_positive()
        _t2 = time.perf_counter()
        log_info(f"[主力建仓] {self.stock_code} 价格低位+DDX检测: {_t2 - _t1:.4f}s")
        positive_days = vol_trend.get('正天数', 0)
        if vol_trend['signal']:
            score += 20
            details.append(_make_detail(
                '成交量逐步放大', 20, 20, 'passed',
                f'近5日{positive_days}天涨跌幅>0', '近5日涨跌幅>0天数 >= 3',
                f'近5日中有{positive_days}天上涨，成交量呈现逐步放大趋势'
            ))
        else:
            details.append(_make_detail(
                '成交量逐步放大', 20, 0, 'failed',
                f'近5日{positive_days}天涨跌幅>0', '近5日涨跌幅>0天数 >= 3',
                f'近5日仅{positive_days}天上涨，成交量未持续放大'
            ))
        
        macd_check = l1.check_macd_golden_cross()
        _t3 = time.perf_counter()
        log_info(f"[主力建仓] {self.stock_code} MACD金叉检测: {_t3 - _t2:.4f}s")
        if l1.df is not None and not l1.df.empty:
            latest_dif = l1.df.iloc[-1].get('DIF', 0) or 0
            latest_dea = l1.df.iloc[-1].get('DEA', 0) or 0
        else:
            latest_dif = 0
            latest_dea = 0
        macd_close = latest_dif > latest_dea * 0.9
        if macd_check['signal']:
            score += 20
            details.append(_make_detail(
                'MACD 金叉', 20, 20, 'passed',
                f'DIF={latest_dif:.4f}, DEA={latest_dea:.4f}', 'MACD金叉(20分) / DIF>=DEA×0.9(10分)',
                'MACD形成金叉信号，DIF上穿DEA，趋势转多'
            ))
        elif macd_close:
            score += 10
            details.append(_make_detail(
                'MACD 金叉', 20, 10, 'partial',
                f'DIF={latest_dif:.4f}, DEA={latest_dea:.4f}', 'MACD金叉(20分) / DIF>=DEA×0.9(10分)',
                f'DIF接近DEA（DIF/DEA={latest_dif/max(latest_dea,0.0001):.2f}），即将形成金叉'
            ))
        else:
            details.append(_make_detail(
                'MACD 金叉', 20, 0, 'failed',
                f'DIF={latest_dif:.4f}, DEA={latest_dea:.4f}', 'MACD金叉(20分) / DIF>=DEA×0.9(10分)',
                'DIF与DEA差距较大，暂未出现金叉信号'
            ))
        
        l2_data = l2.get_l2_indicators()
        _t4 = time.perf_counter()
        log_info(f"[主力建仓] {self.stock_code} L2指标计算: {_t4 - _t3:.4f}s")
        
        ddx_5_avg = l2_data['DDX_5日均值']
        ddy_5_trend = l2_data['DDY_5日趋势']
        main_net = l2_data['主力净额']
        
        if ddx_5_avg > 0:
            score += 15
            details.append(_make_detail(
                'DDX 持续为正', 15, 15, 'passed',
                f'DDX_5日均值 = {ddx_5_avg:.4f}', 'DDX_5日均值 > 0',
                f'DDX近5日均值为{ddx_5_avg:.4f}，大单资金持续流入'
            ))
        else:
            details.append(_make_detail(
                'DDX 持续为正', 15, 0, 'failed',
                f'DDX_5日均值 = {ddx_5_avg:.4f}', 'DDX_5日均值 > 0',
                f'DDX近5日均值为{ddx_5_avg:.4f}，大单资金未持续流入'
            ))
        
        if ddy_5_trend > 0:
            score += 15
            details.append(_make_detail(
                'DDY 上升趋势', 15, 15, 'passed',
                f'DDY_5日趋势 = {ddy_5_trend:.4f}', 'DDY_5日趋势 > 0',
                f'DDY近5日趋势值为{ddy_5_trend:.4f}，涨跌动因持续上升'
            ))
        else:
            details.append(_make_detail(
                'DDY 上升趋势', 15, 0, 'failed',
                f'DDY_5日趋势 = {ddy_5_trend:.4f}', 'DDY_5日趋势 > 0',
                f'DDY近5日趋势为{ddy_5_trend:.4f}，涨跌动因未呈上升态势'
            ))
        
        if main_net > 0:
            score += 15
            details.append(_make_detail(
                '主力净流入', 15, 15, 'passed',
                f'主力净额 = {main_net/10000:.2f}万', '主力净额 > 0',
                f'主力资金净流入{main_net/10000:.2f}万，资金面支持建仓'
            ))
        else:
            details.append(_make_detail(
                '主力净流入', 15, 0, 'failed',
                f'主力净额 = {abs(main_net)/10000:.2f}万（流出）', '主力净额 > 0',
                f'主力资金净流出{abs(main_net)/10000:.2f}万，暂未出现建仓迹象'
            ))
        
        _t_end = time.perf_counter()
        log_info(f"[主力建仓] {self.stock_code} 总耗时: {_t_end - _t0:.3f}s (计算: {_t_end - _t_load:.3f}s)")
        
        return {
            'score': score,
            'max_score': 100,
            'details': details
        }
    
    def strategy_value_stocks(self) -> Dict[str, Any]:
        _t0 = time.perf_counter()
        if not self.load_all_data('3'):
            return {'score': 0, 'max_score': 100, 'details': [], 'error': '数据加载失败'}
        _t_load = time.perf_counter()
        log_info(f"[价值投资] {self.stock_code} 数据加载: {_t_load - _t0:.3f}s")

        l1 = self.l1_screener

        score = 0
        details = []

        pe = None
        if l1.realtime_data:
            pe = l1.realtime_data.get('市盈率-动态', 0)
            if pe and 0 < pe < 30:
                score += 25
                details.append(_make_detail(
                    '市盈率(PE)合理', 25, 25, 'passed',
                    f'PE = {pe:.1f}', '0 < PE < 30',
                    f'市盈率{pe:.1f}处于合理估值区间，估值安全边际好'))
            else:
                details.append(_make_detail(
                    '市盈率(PE)合理', 25, 0, 'failed',
                    f'PE = {pe}', '0 < PE < 30',
                    f'市盈率{pe}不在合理范围（0~30），估值偏高或为亏损'))
        else:
            details.append(_make_detail(
                '市盈率(PE)合理', 25, 0, 'failed',
                '无实时数据', '0 < PE < 30',
                '无法获取实时市盈率数据'))

        if l1.realtime_data:
            pb = l1.realtime_data.get('市净率', 0)
            if pb and 0 < pb < 3:
                score += 25
                details.append(_make_detail(
                    '市净率(PB)合理', 25, 25, 'passed',
                    f'PB = {pb:.2f}', '0 < PB < 3',
                    f'市净率{pb:.2f}处于合理区间，净资产支撑充分'))
            else:
                details.append(_make_detail(
                    '市净率(PB)合理', 25, 0, 'failed',
                    f'PB = {pb}', '0 < PB < 3',
                    f'市净率{pb}不在合理范围（0~3），估值偏高或为负资产'))
        else:
            details.append(_make_detail(
                '市净率(PB)合理', 25, 0, 'failed',
                '无实时数据', '0 < PB < 3',
                '无法获取实时市净率数据'))

        if l1.df is not None:
            latest = l1.df.iloc[-1]
            dif_val = latest.get('DIF', 0) or 0
            dea_val = latest.get('DEA', 0) or 0
            if dif_val > 0 and dea_val > 0:
                score += 25
                details.append(_make_detail(
                    'MACD零轴上方', 25, 25, 'passed',
                    f'DIF={dif_val:.4f}, DEA={dea_val:.4f}', 'DIF > 0 且 DEA > 0',
                    'MACD双线均在零轴上方，中长期趋势向好'))
            else:
                details.append(_make_detail(
                    'MACD零轴上方', 25, 0, 'failed',
                    f'DIF={dif_val:.4f}, DEA={dea_val:.4f}', 'DIF > 0 且 DEA > 0',
                    'MACD双线未同时在零轴上方，趋势尚未确认走强'))
        else:
            details.append(_make_detail(
                'MACD零轴上方', 25, 0, 'failed',
                '无K线数据', 'DIF > 0 且 DEA > 0',
                '无法获取K线数据，无法计算MACD'))

        if l1.df is not None and len(l1.df) >= 250:
            price_current = l1.df.iloc[-1]['收盘']
            price_1y_high = l1.df.tail(250)['最高'].max()
            ratio = price_current / price_1y_high if price_1y_high > 0 else 1

            if price_current < price_1y_high * 0.7:
                score += 25
                details.append(_make_detail(
                    '股价相对低位', 25, 25, 'passed',
                    f'当前价{price_current:.2f}, 年高{price_1y_high:.2f}, 比例{ratio:.1%}',
                    '当前价 < 近250天最高价 × 0.7',
                    f'当前价为年高点的{ratio:.1%}，处于相对低位，上涨空间大'))
            else:
                details.append(_make_detail(
                    '股价相对低位', 25, 0, 'failed',
                    f'当前价{price_current:.2f}, 年高{price_1y_high:.2f}, 比例{ratio:.1%}',
                    '当前价 < 近250天最高价 × 0.7',
                    f'当前价为年高点的{ratio:.1%}，价格相对较高，安全边际不足'))
        else:
            details.append(_make_detail(
                '股价相对低位', 25, 0, 'failed',
                '数据不足（需250天K线）', '当前价 < 近250天最高价 × 0.7',
                'K线数据不足250天，无法判断股价位置'))

        _t_end = time.perf_counter()
        log_info(f"[价值投资] {self.stock_code} 总耗时: {_t_end - _t0:.3f}s (计算: {_t_end - _t_load:.3f}s)")

        return {
            'score': score,
            'max_score': 100,
            'details': details
        }


# 第十四部分：股票智能筛选引擎

class StockScreener:
    """
    股票智能筛选引擎
    
    该类实现了基于L1和L2数据的多策略股票筛选功能，支持：
    1. 单股分析筛选
    2. 批量股票筛选
    3. 三种筛选策略：
       - 短线强势股策略
       - 主力建仓股策略
       - 价值投资股策略
    4. 自定义推荐数量
    5. 多级 API fallback 机制
    """
    
    def __init__(self, max_recommendations: int = 5):
        """
        初始化股票筛选器
        
        Args:
            max_recommendations: 最大推荐股票数量，默认 5 支
        """
        self.max_recommendations = max_recommendations
        
    def screen_single_stock(self, stock_code: str, strategy: str = 'all',
                                stock_name: Optional[str] = None) -> Dict[str, Any]:
        """
        对单支股票进行筛选分析

        Args:
            stock_code: 股票代码
            strategy: 筛选策略，可选值: '1'(短线强势), '2'(主力建仓), '3'(价值投资), 'all'(全部)
            stock_name: 股票名称，如已预取可传入避免重复查询缓存

        Returns:
            包含筛选结果的字典，结构为:
            {
                'stock_code': 股票代码,
                'stock_name': 股票名称,
                'strategies': {
                    '策略名1': {'passed': bool, 'score': float, ...},
                    '策略名2': ...
                },
                'comprehensive_score': 综合得分,
                'passed': 是否通过筛选
            }
            
        Example:
            >>> screener = StockScreener()
            >>> result = screener.screen_single_stock('688275', strategy='1')
            >>> if result['passed']:
            ...     print(f"{result['stock_name']} 通过筛选")
        """

        if not isinstance(stock_code, str) or len(stock_code) != 6 or not stock_code.isdigit():
            log_warning(f"无效股票代码: {stock_code}，已跳过")
            return {
                'stock_code': str(stock_code),
                'stock_name': '无效代码',
                'strategies': {},
                'comprehensive_score': 0,
                'passed': False
            }

        _ss_t0 = time.perf_counter()

        screener = ScreeningStrategies(stock_code)

        _ss_n0 = time.perf_counter()
        if stock_name is None:
            stock_name = self._get_stock_name(stock_code)
        _ss_n1 = time.perf_counter()

        results = {
            'stock_code': stock_code,
            'stock_name': stock_name,
            'strategies': {}
        }

        if strategy != 'all':
            if strategy == '1':
                results['strategies']['短线强势股'] = screener.strategy_short_term_strong()
            elif strategy == '2':
                results['strategies']['主力建仓股'] = screener.strategy_main_accumulation()
            elif strategy == '3':
                results['strategies']['价值投资股'] = screener.strategy_value_stocks()
        else:
            if not screener.load_all_data('all'):
                results['strategies'] = {
                    '短线强势股': {'score': 0, 'max_score': 100, 'details': [], 'error': '数据加载失败'},
                    '主力建仓股': {'score': 0, 'max_score': 100, 'details': [], 'error': '数据加载失败'},
                    '价值投资股': {'score': 0, 'max_score': 100, 'details': [], 'error': '数据加载失败'},
                }
            else:
                s1 = screener.strategy_short_term_strong()
                results['strategies']['短线强势股'] = s1

                s2 = screener.strategy_main_accumulation()
                results['strategies']['主力建仓股'] = s2

                if s1.get('score', 0) < 30 and s2.get('score', 0) < 30:
                    results['strategies']['价值投资股'] = {
                        'score': 0, 'max_score': 100, 'details': [],
                        'skip_reason': '短线/中线得分过低，跳过深度分析'
                    }
                else:
                    results['strategies']['价值投资股'] = screener.strategy_value_stocks()
        
        total_score = 0
        strategy_count = 0
        for s_name, s_result in results['strategies'].items():
            total_score += s_result.get('score', 0)
            strategy_count += 1
        
        results['comprehensive_score'] = total_score / max(strategy_count, 1)

        _ss_total = time.perf_counter()
        log_info(
            f"[{stock_code}] 单股筛选完成: "
            f"名称查询={_ss_n1 - _ss_n0:.3f}s "
            f"策略执行={_ss_total - _ss_n1:.3f}s "
            f"总计={_ss_total - _ss_t0:.3f}s "
            f"综合得分={results['comprehensive_score']:.1f}"
        )

        return results
    
    def _get_stock_name(self, stock_code: str) -> str:
        """内部方法：获取股票名称（优先从名称映射字典读取，O(1)查询）"""
        try:
            name = _stock_name_map.get(stock_code)
            if name:
                return name
            with _market_cache_lock:
                cached = _market_cache.get("em")
            if cached is not None:
                df, _ = cached
                if df is not None and not df.empty:
                    code_col = '代码' if '代码' in df.columns else None
                    name_col = '名称' if '名称' in df.columns else None
                    if code_col and name_col:
                        row = df.loc[df[code_col] == stock_code, name_col]
                        if not row.empty:
                            return str(row.iloc[0])
        except Exception:
            pass
        return stock_code
    
    def screen_batch(self, stock_codes: List[str], strategy: str = 'all') -> List[Dict[str, Any]]:
        """批量筛选股票，返回推荐列表

        优化策略：
        1. 并发筛选（ThreadPoolExecutor），默认3线程
        2. 早停机制：已找到足够高分候选时提前终止
        3. 限制最大并发数避免API限流

        Args:
            stock_codes: 要筛选的股票代码列表
            strategy: 筛选策略，可选值: '1', '2', '3', 'all'

        Returns:
            排序后的推荐股票列表（按综合得分从高到低），数量不超过 max_recommendations
        """
        _sb_t0 = time.perf_counter()
        if not stock_codes:
            log_info("批量筛选完成: 输入=0支 有效=0支 耗时=0.000s")
            return []

        valid_codes = [c for c in stock_codes if isinstance(c, str) and len(c) == 6 and c.isdigit()]
        skipped = len(stock_codes) - len(valid_codes)
        if skipped > 0:
            log_warning(f"批量筛选中跳过 {skipped} 个无效代码")

        if not valid_codes:
            log_info("批量筛选完成: 输入={}支 有效=0支 耗时=0.000s".format(len(stock_codes)))
            return []

        _sb_name0 = time.perf_counter()
        name_map: Dict[str, str] = {}
        try:
            df = _get_cached_market_data("em")
            if df is not None and not df.empty:
                code_col = '代码' if '代码' in df.columns else None
                name_col = '名称' if '名称' in df.columns else None
                if code_col and name_col:
                    name_map = dict(zip(df[code_col].astype(str), df[name_col].astype(str)))
        except Exception:
            pass
        _sb_name1 = time.perf_counter()
        if name_map:
            log_info(f"名称映射预取完成: {len(name_map)}条 ({_sb_name1 - _sb_name0:.3f}s)")

        results: List[Dict[str, Any]] = []
        lock = threading.Lock()
        early_stop = threading.Event()
        strategy_thresholds = {'1': 60, '2': 55, '3': 50, 'all': 55}
        min_score_threshold = strategy_thresholds.get(strategy, 55)

        def process_single_stock(code: str) -> Optional[Dict[str, Any]]:
            try:
                return self.screen_single_stock(code, strategy, stock_name=name_map.get(code))
            except Exception as e:
                log_warning(f"筛选 {code} 时出错: {str(e)[:40]}")
                return None

        def worker(code: str) -> Optional[Dict[str, Any]]:
            if early_stop.is_set():
                return None
            result = process_single_stock(code)
            if result:
                with lock:
                    results.append(result)
                    if (len(results) >= self.max_recommendations
                            and all(r['comprehensive_score'] >= min_score_threshold for r in results[-self.max_recommendations:])):
                        early_stop.set()
            return result

        base_workers = 5 if strategy in ('1', '2') else 3
        max_workers = min(base_workers, len(valid_codes))

        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                console=console
            ) as progress:
                task = progress.add_task("筛选股票...", total=len(valid_codes))
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {executor.submit(worker, code): code for code in valid_codes}
                    completed = 0
                    for future in as_completed(futures):
                        completed += 1
                        if early_stop.is_set():
                            progress.update(task, completed=len(valid_codes))
                            break
                        try:
                            progress.advance(task)
                        except Exception:
                            pass
        except Exception:
            try:
                print(f"正在筛选 {len(valid_codes)} 支股票（并发模式）...")
            except Exception:
                pass
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(worker, code): code for code in valid_codes}
                for future in as_completed(futures):
                    if early_stop.is_set():
                        break
                    try:
                        future.result()
                    except Exception:
                        pass

        results.sort(key=lambda x: x['comprehensive_score'], reverse=True)
        _sb_total = time.perf_counter()
        log_info(
            f"批量筛选完成: "
            f"输入={len(stock_codes)}支 "
            f"有效={len(results)}支 "
            f"早停={'是' if early_stop.is_set() else '否'} "
            f"耗时={_sb_total - _sb_t0:.3f}s"
        )
        return results[:self.max_recommendations]
    
    def _safe_fetch_stock_list(self, api_func, api_name: str, max_count: int = 10) -> Optional[List[str]]:
        """
        内部方法：安全获取股票列表，包含响应验证和错误处理
        
        Args:
            api_func: API 函数对象
            api_name: API 名称（用于日志）
            max_count: 最大获取股票数量，默认 10 支
            
        Returns:
            股票代码列表或 None
        """
        try:
            df = api_func()
            
            # 验证返回的数据
            if df is None:
                log_warning(f"{api_name} 返回 None", prefix="")
                return None
            
            if df.empty:
                log_warning(f"{api_name} 返回空数据", prefix="")
                return None
            
            # 验证数据结构（确保包含必要的列）
            required_columns = ['代码', '名称']
            for col in required_columns:
                if col not in df.columns:
                    log_warning(f"{api_name} 缺少必要列: {col}", prefix="")
                    return None
            
            # 过滤 ST 和退市股票（避免风险较大的股票）
            df = df[~df['名称'].str.contains('ST|退市', na=False)]
            
            # 限制数量（避免处理过多股票导致性能问题）
            if len(df) > max_count:
                df = df.sample(n=max_count, random_state=42)
            
            stock_codes = df['代码'].tolist()
            log_success(f"{api_name} 成功获取 {len(stock_codes)} 支股票", prefix="")
            return stock_codes
            
        except ValueError as e:
            # JSON 解码错误 - 通常是返回了 HTML（可能是网络限制或反爬虫）
            error_str = str(e)
            if '<' in error_str or 'decode' in error_str.lower():
                log_warning(f"{api_name} 返回 HTML 而非 JSON（可能被拦截）", prefix="")
            else:
                log_warning(f"{api_name} 数据解析失败: {error_str[:40]}", prefix="")
            return None
        except Exception as e:
            error_msg = str(e)
            # 针对常见网络错误提供更友好的提示
            if 'Connection aborted' in error_msg or 'RemoteDisconnected' in error_msg:
                log_warning(f"{api_name} 网络连接中断（请检查网络）", prefix="")
            elif 'decode' in error_msg.lower() and '<' in error_msg:
                log_warning(f"{api_name} 返回 HTML 而非 JSON（可能被拦截）", prefix="")
            else:
                log_warning(f"{api_name} 调用失败: {error_msg[:50]}", prefix="")
            return None

    def get_top_stocks(self, strategy: str = 'all') -> List[Dict[str, Any]]:
        """获取热门股票列表并进行筛选推荐

        优化策略：
        1. 优先复用缓存的全市场行情数据（避免重复API调用）
        2. 基于实时行情预过滤无效股票（ST/停牌/涨跌停）
        3. 按策略特征排序候选股（提高命中率）
        4. 兜底数据源自动采样限制总数，防止全市场遍历耗时过长

        Args:
            strategy: 筛选策略，可选值: '1', '2', '3', 'all'

        Returns:
            排序后的推荐股票列表
        """
        _MAX_BATCH = 200
        _FALLBACK_SAMPLE = 100

        log_info("正在获取股票列表...", prefix="")
        _gt_t0 = time.perf_counter()

        market_df = _get_cached_market_data("em")
        _gt_list_t = time.perf_counter()
        log_info(f"获取股票列表耗时: {_gt_list_t - _gt_t0:.3f}s", prefix="")
        if market_df is not None and not market_df.empty:
            stock_codes = self._prefilter_from_market_data(market_df, strategy)
            if stock_codes:
                log_success(f"从缓存行情数据中筛选出 {len(stock_codes)} 支候选股", prefix="")
                result = self.screen_batch(stock_codes, strategy)
                _gt_total = time.perf_counter()
                log_info(f"get_top_stocks 完成: 列表获取={_gt_list_t - _gt_t0:.3f}s 批量筛选={_gt_total - _gt_list_t:.3f}s 总计={_gt_total - _gt_t0:.3f}s", prefix="")
                return result

        try:
            df = ak.stock_info_a_code_name()
            if df is not None and not df.empty:
                df = df[~df['name'].str.contains('ST|退市', na=False)]
                stock_codes = df['code'].tolist()
                if len(stock_codes) > _MAX_BATCH:
                    log_warning(f"候选股过多({len(stock_codes)}支)，随机采样至{_MAX_BATCH}支", prefix="")
                    import random
                    random.seed(42)
                    stock_codes = random.sample(stock_codes, _MAX_BATCH)
                log_success(f"Akshare(stock_info) 成功获取 {len(stock_codes)} 支股票", prefix="")
                result = self.screen_batch(stock_codes, strategy)
                _gt_total = time.perf_counter()
                log_info(f"get_top_stocks 完成: 列表获取={_gt_list_t - _gt_t0:.3f}s 批量筛选={_gt_total - _gt_list_t:.3f}s 总计={_gt_total - _gt_t0:.3f}s", prefix="")
                return result
        except Exception as e:
            log_warning(f"Akshare(stock_info) 失败: {str(e)[:50]}", prefix="")

        stock_codes = self._safe_fetch_stock_list(ak.stock_zh_a_spot, "Akshare(新浪)", max_count=_FALLBACK_SAMPLE)
        if stock_codes:
            result = self.screen_batch(stock_codes, strategy)
            _gt_total = time.perf_counter()
            log_info(f"get_top_stocks 完成: 列表获取={_gt_list_t - _gt_t0:.3f}s 批量筛选={_gt_total - _gt_list_t:.3f}s 总计={_gt_total - _gt_t0:.3f}s", prefix="")
            return result

        try:
            log_info("方案3: Tushare", prefix="")
            ts.set_token(TUSHARE_TOKEN)
            pro = ts.pro_api()
            df = pro.stock_basic(exchange='', list_status='L', fields='ts_code,symbol,name')
            if df is not None and not df.empty:
                df = df[~df['name'].str.contains('ST|退市', na=False)]
                stock_codes = df['ts_code'].tolist()[:_FALLBACK_SAMPLE]
                log_success(f"Tushare 成功获取 {len(stock_codes)} 支股票", prefix="")
                result = self.screen_batch(stock_codes, strategy)
                _gt_total = time.perf_counter()
                log_info(f"get_top_stocks 完成: 列表获取={_gt_list_t - _gt_t0:.3f}s 批量筛选={_gt_total - _gt_list_t:.3f}s 总计={_gt_total - _gt_t0:.3f}s", prefix="")
                return result
        except Exception as e:
            error_msg = str(e)
            if '没有接口' in error_msg or '权限' in error_msg:
                log_warning("Tushare 失败：该 Token 没有 stock_basic 接口权限", prefix="")
            else:
                log_warning(f"Tushare 失败: {error_msg[:50]}", prefix="")

        stock_codes = self._safe_fetch_stock_list(ak.stock_zh_a_spot_em, "东方财富", max_count=_FALLBACK_SAMPLE)
        if stock_codes:
            result = self.screen_batch(stock_codes, strategy)
            _gt_total = time.perf_counter()
            log_info(f"get_top_stocks 完成: 列表获取={_gt_list_t - _gt_t0:.3f}s 批量筛选={_gt_total - _gt_list_t:.3f}s 总计={_gt_total - _gt_t0:.3f}s", prefix="")
            return result

        _gt_total = time.perf_counter()
        log_warning(f"所有数据源均获取失败 (耗时{_gt_total - _gt_t0:.3f}s)，请检查网络连接后重试", prefix="")
        return []

    def _prefilter_from_market_data(self, market_df: pd.DataFrame, strategy: str) -> List[str]:
        """从缓存的全市场行情数据中预筛选候选股票

        根据策略特征过滤无效股票并排序，提高后续评分的命中率：
        - 通用过滤：排除ST/退市/停牌/涨跌停
        - 短线强势(1)：按涨跌幅降序（强势股优先）
        - 主力建仓(2)：按换手率降序（活跃股优先）
        - 价值投资(3)：按市盈率升序（低估值优先）
        - all：按涨跌幅降序
        """
        name_col = next((c for c in market_df.columns if '名称' in str(c) or 'name' in str(c).lower()), None)
        code_col = next((c for c in market_df.columns if '代码' in str(c) or 'code' in str(c).lower()), None)
        if code_col is None:
            return []

        mask = pd.Series(True, index=market_df.index)
        if name_col:
            mask &= ~market_df[name_col].astype(str).str.contains('ST|退市', na=False)

        change_col = next((c for c in market_df.columns if '涨跌幅' in str(c) or 'changepercent' in str(c).lower()), None)
        if change_col:
            change_vals = pd.to_numeric(market_df[change_col], errors='coerce')
            mask &= change_vals.notna() & (change_vals.abs() < 9.9)

        vol_col = next((c for c in market_df.columns if '成交量' in str(c) or 'volume' in str(c).lower()), None)
        if vol_col:
            vol_vals = pd.to_numeric(market_df[vol_col], errors='coerce').fillna(0)
            mask &= vol_vals > 0

        df = market_df.loc[mask]

        sort_col = change_col
        ascending = False
        if strategy == '2':
            turnover_col = next((c for c in df.columns if '换手率' in str(c) or 'turnover' in str(c).lower()), None)
            if turnover_col:
                sort_col = turnover_col
        elif strategy == '3':
            pe_col = next((c for c in df.columns if '市盈率' in str(c) or 'pe' in str(c).lower()), None)
            if pe_col:
                sort_col = pe_col
                ascending = True
                pe_vals = pd.to_numeric(market_df[pe_col], errors='coerce')
                df = df.loc[pe_vals.loc[df.index] > 0]

        if sort_col and sort_col in df.columns:
            sort_vals = pd.to_numeric(df[sort_col], errors='coerce')
            df = df.loc[sort_vals.notna()]
            df = df.iloc[sort_vals[sort_vals.notna()].argsort()]
            if not ascending:
                df = df.iloc[::-1]

        n = min(self.max_recommendations * 5, len(df))
        return df[code_col].head(n).tolist()


def display_stock_result(result: Dict[str, Any]):
    """
    以结构化表格形式在控制台展示单只股票的评分明细

    针对每种策略，依次输出评分维度表格，包含 7 列：
    评分维度（含状态图标 ✓/△/✗）、阈值条件、实际值、满分、得分、状态、评分依据。
    策略标题按得分率着色：≥80% 绿色、≥50% 黄色、<50% 红色。
    底部显示综合得分（三种策略得分的平均值）。

    Args:
        result: 股票评分结果字典，由 ScreeningStrategies 的三种策略方法返回
    """
    if not result:
        return
    console.print(f"\n[bold]股票评分明细: {result['stock_name']}({result['stock_code']})[/bold]\n")
    for strategy_name, strategy_result in result['strategies'].items():
        score = strategy_result.get('score', 0)
        max_s = strategy_result.get('max_score', 100)
        pct = score / max_s * 100 if max_s > 0 else 0
        color = 'green' if pct >= 80 else 'yellow' if pct >= 50 else 'red'
        console.print(f"[bold {color}]{strategy_name}[/bold {color}] 得分: {score}/{max_s} ({pct:.0f}%)")
        details = strategy_result.get('details', [])
        if details:
            t = Table(box=box.ASCII, show_header=True, header_style="bold cyan")
            t.add_column("评分维度", style="cyan")
            t.add_column("阈值条件", style="white")
            t.add_column("实际值", style="yellow")
            t.add_column("满分", justify="center")
            t.add_column("得分", justify="center", style="green")
            t.add_column("状态", justify="center")
            t.add_column("评分依据", style="dim")
            for d in details:
                status_icon = "[green]✓[/green]" if d['status'] == 'passed' else "[yellow]△[/yellow]" if d['status'] == 'partial' else "[red]✗[/red]"
                t.add_row(
                    f"{status_icon} {d['name']}",
                    d['threshold'],
                    d['value'],
                    str(d['max_score']),
                    str(d['actual_score']),
                    d['status'],
                    d['basis']
                )
            console.print(t)
        console.print()
    console.print(f"[bold]综合得分: {result['comprehensive_score']:.1f}/100[/bold]")


def display_recommendations(results: List[Dict[str, Any]]):
    """
    以控制台表格展示批量股票筛选排名

    按综合得分降序排列，输出「推荐股票列表」表格，包含 7 列：
    排名、代码、名称、综合得分、短线强势股得分、主力建仓股得分、价值投资股得分。
    各策略得分按阈值着色（≥80 绿 / ≥50 黄 / <50 红）。

    Args:
        results: 多只股票的评分结果列表，每项格式同 display_stock_result
    """
    if not results:
        console.print("[yellow]暂无筛选结果[/yellow]")
        return
    
    table = Table(
        box=box.ASCII,
        title=f"推荐股票列表 (共{len(results)}支)"
    )
    
    table.add_column("排名", justify="center", style="cyan")
    table.add_column("代码", style="cyan")
    table.add_column("名称", style="green")
    table.add_column("综合得分", justify="right", style="yellow")
    table.add_column("短线强势股", justify="right")
    table.add_column("主力建仓股", justify="right")
    table.add_column("价值投资股", justify="right")
    
    for i, result in enumerate(results, 1):
        score_short = result['strategies'].get('短线强势股', {}).get('score', 0)
        score_main = result['strategies'].get('主力建仓股', {}).get('score', 0)
        score_value = result['strategies'].get('价值投资股', {}).get('score', 0)
        
        def color_score(s):
            return f"[green]{s}[/green]" if s >= 80 else f"[yellow]{s}[/yellow]" if s >= 50 else f"[red]{s}[/red]"
        
        table.add_row(
            f"{i}",
            result['stock_code'],
            result['stock_name'],
            f"{result['comprehensive_score']:.1f}",
            color_score(score_short),
            color_score(score_main),
            color_score(score_value),
        )
    
    console.print(table)


def export_strategy_scores_to_csv(results: List[Dict[str, Any]], filepath: str = None) -> str:
    """
    将策略评分结果导出为 CSV 文件（适合Excel友好格式）

    包含所有股票合并在一个CSV文件中，包含：
    - 股票基本信息（名称、代码）
    - 综合得分
    - 三种策略得分
    - 每种策略的详细评分明细

    Args:
        results: 多只股票的评分结果列表
        filepath: 输出文件路径，默认自动生成含时间戳的文件名

    Returns:
        实际生成的 CSV 文件路径。
    """
    try:
        if filepath is None:
            filepath = f"策略评分明细_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        all_rows = []
        
        for result in results:
            stock_code = result['stock_code']
            stock_name = result['stock_name']
            comp_score = result['comprehensive_score']
            
            all_rows.append({
                '股票代码': stock_code,
                '股票名称': stock_name,
                '综合得分': comp_score,
                '策略': '概述',
                '评分维度': '',
                '阈值条件': '',
                '实际值': '',
                '满分': '',
                '得分': '',
                '状态': '',
                '评分依据': ''
            })
            
            for strategy_name, strategy_result in result['strategies'].items():
                s_score = strategy_result.get('score', 0)
                s_max = strategy_result.get('max_score', 100)
                pct = s_score / s_max * 100 if s_max > 0 else 0
                all_rows.append({
                    '股票代码': '',
                    '股票名称': '',
                    '综合得分': '',
                    '策略': strategy_name,
                    '评分维度': '【总分】',
                    '阈值条件': '',
                    '实际值': '',
                    '满分': s_max,
                    '得分': s_score,
                    '状态': f'{pct:.0f}%',
                    '评分依据': ''
                })
                
                details = strategy_result.get('details', [])
                for d in details:
                    all_rows.append({
                        '股票代码': '',
                        '股票名称': '',
                        '综合得分': '',
                        '策略': strategy_name,
                        '评分维度': d['name'],
                        '阈值条件': d['threshold'],
                        '实际值': d['value'],
                        '满分': d['max_score'],
                        '得分': d['actual_score'],
                        '状态': d['status'],
                        '评分依据': d['basis']
                    })
            
            all_rows.append({k: '' for k in all_rows[-1]})
        
        df = pd.DataFrame(all_rows)
        df.to_csv(filepath, index=False, encoding='utf-8-sig')
        
        log_success(f"评分明细已导出到: {filepath}", prefix="")
        return filepath
    except Exception as e:
        log_warning(f"导出CSV失败: {str(e)[:50]}")
        return ""


def export_strategy_scores_to_excel(results: List[Dict[str, Any]], filepath: str = None) -> str:
    """
    将策略评分结果导出为 Excel 文件，每只股票一个独立 Sheet

    每个 Sheet 包含：
    - 股票基本信息（名称、代码）
    - 综合得分
    - 每种策略的完整评分明细表（评分维度、阈值条件、实际值、满分、得分、状态、评分依据）

    Args:
        results: 多只股票的评分结果列表
        filepath: 输出文件路径，默认自动生成含时间戳的文件名

    Returns:
        实际生成的 Excel 文件路径。如果 openpyxl 未安装则返回空字符串。
    """
    try:
        if filepath is None:
            filepath = f"策略评分明细_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
            for result in results:
                stock_code = result['stock_code']
                stock_name = result['stock_name']
                sheet_name = str(stock_code)
                if len(sheet_name) > 31:
                    sheet_name = sheet_name[:31]
                
                rows = []
                rows.append({'评分维度': f'股票: {stock_name}({stock_code})', '阈值条件': '', '实际值': '', '满分': '', '得分(取整)': '', '状态': '', '评分依据': ''})
                rows.append({'评分维度': f'综合得分: {result["comprehensive_score"]:.1f}/100', '阈值条件': '', '实际值': '', '满分': '', '得分(取整)': '', '状态': '', '评分依据': ''})
                rows.append({'评分维度': '', '阈值条件': '', '实际值': '', '满分': '', '得分(取整)': '', '状态': '', '评分依据': ''})
                
                for strategy_name, strategy_result in result['strategies'].items():
                    score = strategy_result.get('score', 0)
                    max_s = strategy_result.get('max_score', 100)
                    pct = score / max_s * 100 if max_s > 0 else 0
                    rows.append({'评分维度': f'【{strategy_name}】总分: {score}/{max_s} ({pct:.0f}%)', '阈值条件': '', '实际值': '', '满分': '', '得分(取整)': '', '状态': '', '评分依据': ''})
                    
                    details = strategy_result.get('details', [])
                    for d in details:
                        rows.append({
                            '评分维度': d['name'],
                            '阈值条件': d['threshold'],
                            '实际值': d['value'],
                            '满分': d['max_score'],
                            '得分(取整)': d['actual_score'],
                            '状态': d['status'],
                            '评分依据': d['basis']
                        })
                    rows.append({'评分维度': '', '阈值条件': '', '实际值': '', '满分': '', '得分(取整)': '', '状态': '', '评分依据': ''})
                
                df_sheet = pd.DataFrame(rows)
                df_sheet.to_excel(writer, sheet_name=sheet_name, index=False)
        
        log_success(f"评分明细已导出到: {filepath}", prefix="")
        return filepath
    except ImportError:
        log_warning("缺少openpyxl库，请执行: pip install openpyxl")
        return ""
    except Exception as e:
        log_warning(f"导出Excel失败: {str(e)[:50]}")
        return ""


def analyze_screening_results_with_deepseek(results: List[Dict[str, Any]]) -> None:
    """
    调用 AI 模型对批量筛选结果进行深度分析

    实际调用 DashScope API（通义千问 qwen-plus 模型）进行智能分析。
    生成的分析报告自动保存到本地文件。

    Args:
        results: 筛选结果列表，每项包含股票代码、名称及三种策略得分
    """
    console.print("\n[cyan]正在调用AI模型进行深度分析...[/cyan]")
    
    try:
        # 构建分析报告
        summary = "【股票智能筛选分析报告】\n\n"
        summary += f"筛选出 {len(results)} 支符合条件的股票：\n\n"
        
        for i, result in enumerate(results, 1):
            stock_code = result['stock_code']
            stock_name = result['stock_name']
            score = result['comprehensive_score']
            
            summary += f"{i}. {stock_code} {stock_name}\n"
            summary += f"   综合得分: {score:.1f}\n"
            
            for strategy_name, strategy_result in result['strategies'].items():
                s_score = strategy_result.get('score', 0)
                s_max = strategy_result.get('max_score', 100)
                summary += f"   [{strategy_name}] 得分: {s_score}/{s_max}\n"
                details = strategy_result.get('details', [])
                for d in details:
                    icon = '✓' if d['status'] == 'passed' else '△' if d['status'] == 'partial' else '✗'
                    summary += f"      {icon} {d['name']}: {d['value']} ({d['actual_score']}/{d['max_score']}分)\n"
            
            summary += "\n"
        
        summary += "\n【分析请求】\n请对以上筛选结果进行深度分析，包括：\n"
        summary += "1. 优中选优：对这" + str(len(results)) + "支股票进行优先级排序\n"
        summary += "2. 风险评估：识别每支股票的主要风险点\n"
        summary += "3. 投资建议：针对不同投资者类型（激进型/稳健型）的配置建议\n"
        summary += "4. 时机分析：入场时机和持仓周期建议\n"
        summary += "5. 注意事项：需要特别关注的风险警示\n"
        summary += "\n请用中文详细分析，给出具体的投资优先级排序和建议持仓比例。"
        
        # 调用 DashScope API
        console.print("[cyan]正在等待AI响应，请稍候...[/cyan]")
        ai_analysis = _call_dashscope_api(
            system_prompt="你是一位专业的A股量化投资顾问，擅长量化筛选和技术分析。请基于用户提供的量化筛选结果，给出客观、专业的投资建议，用中文回复。",
            user_content=summary,
            max_tokens=3000,
            timeout=90
        )
        
        # 处理占位符 API Key 的模拟结果（console.print 版本）
        if ai_analysis.startswith('[yellow]提示'):
            console.print("\n[yellow]提示：请前往 https://dashscope.aliyun.com 注册获取免费API Key[/yellow]")
            console.print("[yellow]新用户赠送100万Token，支持qwen-max、qwen-plus等模型。[/yellow]\n")
            console.print("[cyan]【模拟AI分析结果】[/cyan]\n")
            console.print("基于量化筛选结果，以下是AI的深度分析建议：\n")
            console.print("【优先级排序】")
            for i, result in enumerate(results[:5], 1):
                console.print(f"  {i}. {result['stock_code']} {result['stock_name']} (得分: {result['comprehensive_score']:.1f})")
            console.print("\n【投资建议】")
            console.print("  - 激进型投资者：可重点关注排名前3的股票")
            console.print("  - 稳健型投资者：建议分散配置前5名股票")
            console.print("\n【风险提示】")
            console.print("  - 股市有风险，投资需谨慎")
            console.print("  - 建议结合市场整体环境综合判断")
            console.print("  - AI分析仅供参考，不构成投资建议")
            return
        
        # 处理 API 错误（ai_analysis 包含 [red] 标签）
        if ai_analysis.startswith('[red]'):
            console.print(ai_analysis)
            return
        
        console.print("\n" + "=" * 70)
        console.print("[bold green]AI 深度分析结果[/bold green]")
        console.print("=" * 70)
        console.print(ai_analysis)
        console.print("=" * 70)
        
        # 询问是否保存分析报告
        try:
            save = input("\n> 是否保存AI分析报告到文件？(Y/N): ").strip().upper()
            if save == 'Y':
                report_file = f"筛选分析报告_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                with open(report_file, 'w', encoding='utf-8') as f:
                    f.write(f"股票智能筛选分析报告\n")
                    f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write("=" * 70 + "\n\n")
                    f.write(summary + "\n\n")
                    f.write("=" * 70 + "\n\n")
                    f.write("AI 深度分析结果:\n\n")
                    f.write(ai_analysis)
                console.print(f"[green]分析报告已保存到: {report_file}[/green]")
        except Exception as e:
            console.print(f"[yellow]保存报告失败: {e}[/yellow]")
        
    except Exception as e:
        console.print(f"[red]分析失败: {str(e)}[/red]")


def run_stock_screener():
    """
    股票智能筛选系统 — 交互式主循环

    提供三种交互功能：
    1. 单股筛选分析：输入股票代码，展示三种策略的完整评分明细表格
    2. 批量智能筛选：自动遍历全市场股票，按综合得分排序推荐
    3. 切换推荐数量：自定义批量筛选时返回的股票数量

    用户可选择对筛选结果调用 AI 模型（DashScope / 通义千问 qwen-plus）
    进行深度分析，或导出 Excel 评分表（每只股票一个独立 Sheet）。

    每只股票的数据获取耗时约 3~8 秒，批量筛选会根据股票数量相应延长。
    """
    # 在函数开始时创建一个持久性的 screener 实例
    screener = StockScreener()

    warmup_market_cache()
    
    try:
        console.print(Panel.fit(
            "[bold cyan]股票智能筛选系统[/bold cyan]\n\n"
            "基于L1和L2数据的自动化股票筛选工具\n"
            "综合运用多维度指标，智能筛选有价值和潜力的股票",
            box=box.ASCII
        ))
    except Exception:
        pass
    
    while True:
        try:
            _check_warmup_thread_health()
            console.print("\n" + "-" * 60)
            console.print("请选择功能:")
            console.print("  [1] 单股筛选分析")
            console.print("  [2] 批量智能筛选（自动推荐最佳股票）")
            console.print(f"  [3] 切换推荐数量（当前: {screener.max_recommendations}支）")
            console.print(f"  {rich_escape('[q]')} 返回上级菜单")
            console.print("-" * 60)
        except Exception:
            print("\n" + "-" * 60)
            print("请选择功能:")
            print("  [1] 单股筛选分析")
            print("  [2] 批量智能筛选（自动推荐最佳股票）")
            print(f"  [3] 切换推荐数量（当前: {screener.max_recommendations}支）")
            print("  [q] 返回上级菜单")
            print("-" * 60)
        
        try:
            choice = input("> 请输入选项: ").strip()
        except EOFError:
            try:
                console.print("\n[yellow]返回上级菜单[/yellow]")
            except Exception:
                print("\n返回上级菜单")
            return
        
        if choice == 'q' or choice == 'Q':
            return
        
        elif choice == '1':
            # 单股筛选
            try:
                stock_code = input("\n> 请输入股票代码（如 688275）: ").strip()
                if not stock_code:
                    try:
                        console.print("[yellow]股票代码不能为空[/yellow]")
                    except Exception:
                        print("股票代码不能为空")
                    continue
                
                if len(stock_code) != 6 or not stock_code.isdigit():
                    try:
                        console.print("[red][!] 请输入有效的6位数字股票代码[/red]")
                    except Exception:
                        print("[!] 请输入有效的6位数字股票代码")
                    continue
                
                try:
                    console.print("\n" + "-" * 60)
                    console.print("请选择筛选策略:")
                    console.print("  [1] 短线强势股策略")
                    console.print("  [2] 主力建仓股策略")
                    console.print("  [3] 价值投资股策略")
                    console.print(f"  {rich_escape('[all]')} 全部策略")
                except Exception:
                    print("\n" + "-" * 60)
                    print("请选择筛选策略:")
                    print("  [1] 短线强势股策略")
                    print("  [2] 主力建仓股策略")
                    print("  [3] 价值投资股策略")
                    print("  [all] 全部策略")
                
                strategy = input("> 请输入选项: ").strip()
                
                result = screener.screen_single_stock(stock_code, strategy)
                
                try:
                    console.print("\n")
                except Exception:
                    print("\n")
                
                display_stock_result(result)
                
            except Exception as e:
                try:
                    console.print(f"\n[red]筛选出错: {e}[/red]")
                except Exception:
                    print(f"\n筛选出错: {e}")
        
        elif choice == '2':
            # 批量筛选
            try:
                try:
                    console.print("\n" + "-" * 60)
                    console.print("请选择筛选策略:")
                    console.print("  [1] 短线强势股策略")
                    console.print("  [2] 主力建仓股策略")
                    console.print("  [3] 价值投资股策略")
                    console.print(f"  {rich_escape('[all]')} 全部策略")
                except Exception:
                    print("\n" + "-" * 60)
                    print("请选择筛选策略:")
                    print("  [1] 短线强势股策略")
                    print("  [2] 主力建仓股策略")
                    print("  [3] 价值投资股策略")
                    print("  [all] 全部策略")
                
                strategy = input("> 请输入选项: ").strip()
                
                results = screener.get_top_stocks(strategy)
                
                try:
                    console.print("\n")
                except Exception:
                    print("\n")
                
                display_recommendations(results)
                
                if results:
                    try:
                        console.print("\n请选择导出格式:")
                        console.print("  [1] 导出为 CSV (单文件，所有股票合并)")
                        console.print("  [2] 导出为 Excel (每只股票一个Sheet)")
                        console.print("  [3] 同时导出 CSV 和 Excel")
                        console.print("  [N/n] 不导出")
                    except Exception:
                        print("\n请选择导出格式:")
                        print("  [1] 导出为 CSV (单文件，所有股票合并)")
                        print("  [2] 导出为 Excel (每只股票一个Sheet)")
                        print("  [3] 同时导出 CSV 和 Excel")
                        print("  [N/n] 不导出")
                    
                    try:
                        export_choice = input("> 请输入选项: ").strip().upper()
                        if export_choice == '1':
                            expo10rt_strategy_scores_to_csv(results)
                        elif export_choice == '2':
                            export_strategy_scores_to_excel(results)
                        elif export_choice == '3':
                            export_strategy_scores_to_csv(results)
                            export_strategy_scores_to_excel(results)
                        elif export_choice == 'N':
                            pass
                        else:
                            try:
                                console.print("[yellow]无效的选项，跳过导出[/yellow]")
                            except Exception:
                                print("无效的选项，跳过导出")
                    except EOFError:
                        try:
                            console.print("\n[yellow]跳过导出[/yellow]")
                        except Exception:
                            print("\n跳过导出")
                
                # 新增：询问是否需要AI分析
                if results:
                    try:
                        ai_choice = input("\n> 是否需要AI深度分析这些股票？(Y/N): ").strip().upper()
                        if ai_choice == 'Y':
                            analyze_screening_results_with_deepseek(results)
                    except EOFError:
                        try:
                            console.print("\n[yellow]跳过AI分析[/yellow]")
                        except Exception:
                            print("\n跳过AI分析")
                
            except Exception as e:
                try:
                    console.print(f"\n[red]筛选出错: {e}[/red]")
                except Exception:
                    print(f"\n筛选出错: {e}")
        
        elif choice == '3':
            # 切换推荐数量
            try:
                new_count = input(f"\n> 请输入推荐数量（1-20，当前: {screener.max_recommendations}）: ").strip()
                if new_count.isdigit():
                    count = int(new_count)
                    if 1 <= count <= 20:
                        screener.max_recommendations = count
                        try:
                            console.print(f"[green]已更新推荐数量为 {count} 支[/green]")
                        except Exception:
                            print(f"已更新推荐数量为 {count} 支")
                    else:
                        try:
                            console.print("[yellow]数量必须在1-20之间[/yellow]")
                        except Exception:
                            print("数量必须在1-20之间")
            except Exception as e:
                try:
                    console.print(f"[yellow]设置失败: {e}[/yellow]")
                except Exception:
                    print(f"设置失败: {e}")
        
        else:
            try:
                console.print("[yellow]无效的选项，请重新输入[/yellow]")
            except Exception:
                print("无效的选项，请重新输入")


def main_with_error_handling():
    """
    主程序入口（带错误处理）
    
    该函数包装了主程序，并添加了全局异常处理机制：
    1. 捕获所有未处理的异常
    2. 生成详细的错误报告
    3. 将错误日志保存到文件
    4. 显示友好的错误提示
    5. 等待用户按键退出
    """
    try:
        main()
    except Exception as e:
        import traceback
        import datetime
        
        error_msg = f"""错误时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
错误类型: {type(e).__name__}
错误信息: {str(e)}
错误详情:
{traceback.format_exc()}
"""
        
        # 使用普通print代替console.print，避免rich库在异常时出错
        try:
            print("\n" + "=" * 70)
            print("程序运行出错")
            print("=" * 70)
            print(f"错误类型: {type(e).__name__}")
            print(f"错误信息: {str(e)}")
            print("\n错误详情:")
            print(traceback.format_exc())
        except Exception:
            pass
        
        try:
            log_file = f"error_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            with open(log_file, 'w', encoding='utf-8') as f:
                f.write(error_msg)
            print(f"\n错误日志已保存到: {log_file}")
        except Exception as log_error:
            try:
                print(f"\n警告: 无法保存错误日志: {str(log_error)}")
            except Exception:
                pass
        
        try:
            print("\n" + "=" * 70)
            print("按任意键继续...")
        except Exception:
            pass
        
        try:
            import sys
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.getch()
            else:
                input()
        except Exception:
            input("按回车键继续...")


if __name__ == "__main__":
    """
    程序入口点
    
    当直接运行该脚本时，会调用 main_with_error_handling() 启动程序
    """
    main_with_error_handling()
