"""
数据获取层模块

封装所有股票数据获取逻辑，支持多数据源和多级 API 回退机制。
"""

import time
import json
import math
import socket
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Tuple, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

import akshare as ak
import tushare as ts
import pandas as pd
import requests

from akshare_app.config import (
    API_DELAY_SHORT, API_DELAY_MEDIUM, HTTP_TIMEOUT_SECONDS,
    TUSHARE_TOKEN, DASHSCOPE_API_KEY, DASHSCOPE_API_URL,
    _HTTP_SESSION, _stock_name_map, MIN_DATA_ROWS,
    MAX_WORKERS_DEFAULT,
)
from akshare_app.cache import _get_cached_market_data, _clear_market_cache
from akshare_app.logging_utils import (
    log_info, log_success, log_warning, log_error, log_debug,
    _safe_console_print, _safe_display, _safe_print,
)
from akshare_app.utils import (
    clean_stock_code, get_market_prefix, format_tushare_code,
    format_yahoo_code, find_column, find_stock_in_df, find_volume_column,
    make_l1_dict, socket_timeout_context,
)
from akshare_app.indicators import calculate_rsi, calculate_macd, calculate_momentum
from akshare_app.config import console
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn


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
                stock_info = find_stock_in_df(spot_df, clean_code, col='代码')
                log_info(f"[{clean_code}] 方案3: 匹配结果: {len(stock_info)} 条")
                
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
                        timestamp=row.get('时间戳', 'N/A'),
                        source='新浪API(备用)',
                    )
                    log_success(f"[{clean_code}] 方案3: 新浪L1数据获取成功")
                    l1_success = True
                else:
                    log_warning(f"[{clean_code}] 方案3: 股票代码 {clean_code} 未在新浪数据中找到")
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
                        source='网易API(备用)',
                    )
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

    作为 _fetch_historical_klines_inner 的包装层，通过上下文管理器提供超时保护：
    - 使用 socket_timeout_context 临时设置 socket 超时，退出时自动恢复原始值
    - 避免全局修改 socket 超时影响程序中其他网络请求

    Args:
        stock_code: 纯数字股票代码（如 '688275'）

    Returns:
        历史K线 DataFrame，包含日期/开盘/收盘/最高/最低/成交量等列。
        所有数据源均失败时返回空 DataFrame。
    """
    clean_code = clean_stock_code(stock_code)
    
    log_info(f"[{clean_code}] 开始获取历史K线数据...")
    time.sleep(API_DELAY_MEDIUM)

    with socket_timeout_context(HTTP_TIMEOUT_SECONDS):
        return _fetch_historical_klines_inner(clean_code)


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
        
        if tx_df is not None and not tx_df.empty and len(tx_df) >= MIN_DATA_ROWS:
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
                
                if len(tx_df) >= MIN_DATA_ROWS:
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
            
            if hist_df is not None and not hist_df.empty and len(hist_df) >= MIN_DATA_ROWS:
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
                indicators['历史波动率(20日)'] = round(hist_df['Volatility'].tail(20).mean(), 2) if len(hist_df) >= MIN_DATA_ROWS else 'N/A'
                
                if len(hist_df) >= MIN_DATA_ROWS:
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
        API 调用失败或数据为空时统一返回空字典 {}。
    """
    clean_code = clean_stock_code(stock_code)

    log_info(f"[{clean_code}] 开始获取资金流向数据...")
    time.sleep(API_DELAY_SHORT)
    
    result: Dict[str, Any] = {}
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
        所有数据源均失败或数据为空时统一返回空字典 {}。
    """
    clean_code = clean_stock_code(stock_code)

    log_info(f"[{clean_code}] 开始获取财务报表数据...")
    time.sleep(API_DELAY_MEDIUM)
    
    financial_report: Dict[str, Any] = {}
    
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
        分红数据字典。无分红记录或 API 失败时统一返回空字典 {}。
    """
    clean_code = clean_stock_code(stock_code)

    log_info(f"[{clean_code}] 开始获取分红送配数据...")
    time.sleep(API_DELAY_SHORT)
    
    result: Dict[str, Any] = {}
    
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


def _fetch_industry_data(stock_code: str, l1_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
        所有数据源均失败时统一返回空字典 {}。
    """
    clean_code = clean_stock_code(stock_code)

    log_info(f"[{clean_code}] 开始获取行业信息...")
    time.sleep(API_DELAY_SHORT)
    
    industry_success = False
    result: Dict[str, Any] = {}
    
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
        log_warning("所有行业信息API均不可用")
    
    return result


def _fetch_basic_info(stock_code: str, l1_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
        所有数据源均失败时统一返回空字典 {}。
    """
    clean_code = clean_stock_code(stock_code)

    log_info(f"[{clean_code}] 开始获取基本信息...")
    time.sleep(API_DELAY_SHORT)
    
    info_success = False
    result: Dict[str, Any] = {}
    
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
                    return make_l1_dict(
                        code=stock_code,
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
                        source='东方财富API(缓存)',
                    )
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
