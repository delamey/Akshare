"""
日志和显示工具模块

提供统一的日志记录和安全控制台打印功能。
"""

import re
from rich.console import Console
from akshare_app.config import console

LOG_COLORS = {
    'info': 'cyan',
    'success': 'green',
    'warning': 'yellow',
    'error': 'red',
    'debug': 'dim',
}


def _safe_print(*args, **kwargs):
    """安全打印：捕获所有异常，防止打印失败导致程序崩溃"""
    try:
        print(*args, **kwargs)
    except Exception:
        try:
            if args:
                print(str(args[0])[:100])
        except Exception:
            pass


def _safe_display(rich_text: str, fallback_text: str = None) -> None:
    """安全显示：优先使用 Rich console，失败时回退到普通 print"""
    try:
        console.print(rich_text)
    except Exception:
        if fallback_text is not None:
            _safe_print(fallback_text)
        elif rich_text:
            clean_text = re.sub(r'\[/?[a-z_]+\]?', '', rich_text)
            _safe_print(clean_text)


def _log(level: str, message: str, prefix: str = "  ->") -> None:
    """统一日志函数

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


def _safe_console_print(rich_text: str, fallback_text: str = None) -> None:
    """安全控制台打印：优先使用 Rich console，失败时回退到普通 print。"""
    try:
        console.print(rich_text)
    except Exception:
        if fallback_text is not None:
            try:
                print(fallback_text)
            except Exception:
                pass
        elif rich_text:
            clean_text = re.sub(r'\[/?[a-z_]+\]?', '', rich_text)
            try:
                print(clean_text)
            except Exception:
                pass


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
