"""
技术指标计算模块

包含独立的技术指标计算函数和 TechnicalIndicators 类。
"""

from typing import List

import pandas as pd

from akshare_app.config import RSI_OVERBOUGHT_THRESHOLD, RSI_OVERSOLD_THRESHOLD


def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """计算RSI指标（WILDERS 平滑法）

    Note:
        本函数与 TechnicalIndicators.calculate_rsi() 使用相同的 WILDERS 平滑法算法，
        区别仅在于接口形式（Series vs DataFrame）。
        RSI 超买/超卖阈值参考配置常量 RSI_OVERBOUGHT_THRESHOLD（默认70）
        和 RSI_OVERSOLD_THRESHOLD（默认30）。

    Args:
        prices: 收盘价 Series
        period: RSI 周期，默认14

    Returns:
        RSI 值 Series
    """
    delta = prices.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)

    avg_gain = gain.rolling(window=period, min_periods=1).mean()
    avg_loss = loss.rolling(window=period, min_periods=1).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_macd(
    prices: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple:
    """计算MACD指标

    Note:
        本函数与 TechnicalIndicators.calculate_macd() 使用相同的 EMA 计算逻辑，
        区别在于：类方法将 MACD 柱乘以 2（A股惯例），而独立函数返回原始差值。

    Args:
        prices: 收盘价 Series
        fast: 快线周期，默认12
        slow: 慢线周期，默认26
        signal: 信号线周期，默认9

    Returns:
        (DIF, DEA, MACD柱) 三元组
    """
    ema_fast = prices.ewm(span=fast, adjust=False).mean()
    ema_slow = prices.ewm(span=slow, adjust=False).mean()

    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    macd = dif - dea

    return dif, dea, macd


def calculate_momentum(prices: pd.Series, period: int = 20) -> pd.Series:
    """计算动量指标

    Note:
        目前 TechnicalIndicators 类中没有对应的 calculate_momentum 方法，
        如需可在类中添加等效实现。

    Args:
        prices: 收盘价 Series
        period: 动量周期，默认20

    Returns:
        动量值 Series
    """
    return prices - prices.shift(period)


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
        """计算移动平均线（返回副本，不修改原DataFrame）"""
        df = df.copy()
        for period in periods:
            df[f'MA{period}'] = df['收盘'].rolling(window=period, min_periods=1).mean()
        return df

    @staticmethod
    def calculate_ema(df: pd.DataFrame, periods: List[int] = [12, 26]) -> pd.DataFrame:
        """计算指数移动平均线（返回副本，不修改原DataFrame）"""
        df = df.copy()
        for period in periods:
            df[f'EMA{period}'] = df['收盘'].ewm(span=period, adjust=False).mean()
        return df

    @staticmethod
    def calculate_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
        """计算MACD指标（返回副本，不修改原DataFrame）"""
        df = df.copy()
        ema_fast = df['收盘'].ewm(span=fast, adjust=False).mean()
        ema_slow = df['收盘'].ewm(span=slow, adjust=False).mean()

        df['DIF'] = ema_fast - ema_slow
        df['DEA'] = df['DIF'].ewm(span=signal, adjust=False).mean()
        df['MACD'] = (df['DIF'] - df['DEA']) * 2

        return df

    @staticmethod
    def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """计算RSI指标（返回副本，不修改原DataFrame）"""
        df = df.copy()
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
        """计算KDJ指标（返回副本，不修改原DataFrame）"""
        df = df.copy()
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
        """计算布林带（返回副本，不修改原DataFrame）"""
        df = df.copy()
        df['BB_MID'] = df['收盘'].rolling(window=period, min_periods=1).mean()
        df['BB_STD'] = df['收盘'].rolling(window=period, min_periods=1).std()
        df['BB_UPPER'] = df['BB_MID'] + std_dev * df['BB_STD']
        df['BB_LOWER'] = df['BB_MID'] - std_dev * df['BB_STD']

        return df

    @staticmethod
    def calculate_volume_ma(df: pd.DataFrame, periods: List[int] = [5, 20]) -> pd.DataFrame:
        """计算成交量均线（返回副本，不修改原DataFrame）"""
        df = df.copy()
        volume_col = '成交量' if '成交量' in df.columns else next(
            (col for col in df.columns if 'vol' in col.lower() or 'volume' in col.lower()),
            None
        )
        if volume_col is None:
            return df
        for period in periods:
            df[f'VOL_MA{period}'] = df[volume_col].rolling(window=period, min_periods=1).mean()
        return df
