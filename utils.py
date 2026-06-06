"""
工具函数模块

提供股票代码处理、列查找、DataFrame操作等通用工具。
"""

import socket
from contextlib import contextmanager
from typing import Optional

import pandas as pd

from akshare_app.config import HTTP_TIMEOUT_SECONDS


@contextmanager
def socket_timeout_context(timeout: float):
    """上下文管理器：临时设置socket超时，退出时恢复原值"""
    original = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        yield
    finally:
        socket.setdefaulttimeout(original)


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
    """在DataFrame中查找股票，先精确匹配再模糊匹配

    优化点：
    1. 避免不必要的类型转换
    2. 使用 regex=False 提升模糊匹配性能

    Args:
        df: 目标DataFrame
        code: 股票代码
        col: 列名，默认'代码'

    Returns:
        匹配结果DataFrame
    """
    if df is None or df.empty or col not in df.columns:
        return pd.DataFrame()

    if pd.api.types.is_string_dtype(df[col]):
        result = df[df[col] == code]
    else:
        result = df[df[col].astype(str) == code]

    if result.empty:
        result = df[df[col].astype(str).str.contains(code, na=False, regex=False)]
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
        stock_code: 纯数字股票代码

    Returns:
        市场前缀 'sh' 或 'sz'
    """
    code = clean_stock_code(stock_code)
    return 'sh' if code[0] in ('6', '5') else 'sz'


def format_tushare_code(stock_code: str) -> str:
    """将6位股票代码转换为Tushare格式（带市场后缀）

    Args:
        stock_code: 纯数字股票代码

    Returns:
        Tushare格式代码，如 '600000.SH' 或 '000001.SZ'
    """
    code = clean_stock_code(stock_code)
    suffix = '.SH' if code[0] in ('6', '5') else '.SZ'
    return f"{code}{suffix}"


def format_yahoo_code(stock_code: str) -> str:
    """将6位股票代码转换为Yahoo Finance格式

    Args:
        stock_code: 纯数字股票代码

    Returns:
        Yahoo格式代码，如 '600000.SS' 或 '000001.SZ'
    """
    code = clean_stock_code(stock_code)
    if code[0] in ('6', '5'):
        return f"{code}.SS"
    else:
        return f"{code}.SZ"


def clean_special_chars(text: str) -> str:
    """清理文本中的特殊字符，防止编码错误和文件名问题

    Args:
        text: 输入文本

    Returns:
        清理后的文本
    """
    if isinstance(text, str):
        invalid_chars = '<>:"/\\|?*'
        result = ''.join(char for char in text if char not in invalid_chars)
        result = ''.join(char for char in result if ord(char) < 128 or char in '，。！？、；：""''（）【】《》')
        return result
    return text


def calc_limit_up_price(row) -> float:
    """计算涨停价格（688/300开头为20%涨停，其余为10%涨停）"""
    from akshare_app.config import LIMIT_UP_RATIO_MAIN, LIMIT_UP_RATIO_GEM
    code = str(row["代码"])
    if code.startswith(("688", "300")):
        return round(row["昨收"] * LIMIT_UP_RATIO_GEM, 2)
    else:
        return round(row["昨收"] * LIMIT_UP_RATIO_MAIN, 2)
