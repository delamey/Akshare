"""
缓存管理模块

提供全市场行情数据的缓存、预热和自动刷新机制。
"""

import time
import threading
from typing import List

import akshare as ak
import pandas as pd

from akshare_app.config import (
    _market_cache, _market_cache_lock, _warmup_done, _warmup_started,
    _warmup_started_lock, _warmup_thread_ref, _cache_fail_times,
    _stock_name_map, _CACHE_TTL, _CACHE_FAIL_COOLDOWN, _CACHE_MAX_SIZE_MB,
)
from akshare_app.logging_utils import (
    log_info, log_success, log_warning, _safe_display,
)


def _get_cache_size_mb() -> float:
    """计算当前缓存占用的内存大小（MB）"""
    total = 0
    for item in _market_cache.values():
        if isinstance(item, tuple) and len(item) >= 1:
            df = item[0]
            if isinstance(df, pd.DataFrame):
                total += df.memory_usage(deep=True).sum() / 1024 / 1024
    return total


def _clear_oldest_cache() -> None:
    """清除最早的缓存条目以释放内存"""
    with _market_cache_lock:
        if _market_cache:
            oldest_key = next(iter(_market_cache))
            _market_cache.pop(oldest_key)


def _check_cache_size_limit() -> None:
    """检查缓存大小是否超过限制，如果超过则清除最早的缓存"""
    while _get_cache_size_mb() > _CACHE_MAX_SIZE_MB:
        _clear_oldest_cache()


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
            _check_cache_size_limit()
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
