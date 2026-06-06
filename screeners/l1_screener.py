"""
L1 一级行情数据筛选器

基于实时行情和历史K线的技术面筛选。
"""

import time
from typing import Dict, Any, Optional, List

import pandas as pd

from akshare_app.config import (
    console, RSI_OVERBOUGHT_THRESHOLD, RSI_OVERSOLD_THRESHOLD,
    VOLUME_SURGE_RATIO, MIN_DATA_ROWS,
)
from akshare_app.logging_utils import log_info, log_success, log_warning, log_error
from akshare_app.data_fetcher import DataFetcher
from akshare_app.indicators import TechnicalIndicators
from akshare_app.utils import clean_stock_code
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeRemainingColumn


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

        ma5 = latest.get('MA5', None)
        ma10 = latest.get('MA10', None)
        prev_ma5 = prev.get('MA5', None)
        prev_ma10 = prev.get('MA10', None)
        prev2_ma5 = prev2.get('MA5', None)

        if any(v is None or pd.isna(v) for v in [ma5, ma10, prev_ma5, prev_ma10]):
            return {'signal': False, 'description': '均线数据不完整'}

        crossover = (ma5 > ma10 and prev_ma5 <= prev_ma10)

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

        change_pct = latest.get('涨跌幅', 0)
        if pd.isna(change_pct):
            change_pct = 0

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

        recent = self.df.tail(20)

        price_lowest_idx = recent['最低'].idxmin()
        price_is_new_low = recent.iloc[-1]['最低'] <= recent.iloc[price_lowest_idx]['最低'] + 0.01

        macd_lowest_idx = recent['MACD'].idxmin()
        macd_is_new_low = recent.iloc[-1]['MACD'] <= recent.iloc[macd_lowest_idx]['MACD'] - 0.01

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

        oversold = rsi < RSI_OVERSOLD_THRESHOLD

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

        overbought = rsi > RSI_OVERBOUGHT_THRESHOLD

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
        surge = surge_ratio > VOLUME_SURGE_RATIO

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

        def get_vol(idx):
            return latest.iloc[idx].get('成交量', 0)

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

        latest_vol = latest.get('成交量', 0)
        prev_vol = prev.get('成交量', 0)

        volume_up = latest_vol > prev_vol
        price_volume_match = (price_up and volume_up)

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

        vol_col = '成交量' if '成交量' in self.df.columns else None
        if vol_col is None:
            for col in self.df.columns:
                if 'vol' in col.lower() or 'volume' in col.lower():
                    vol_col = col
                    break

        if vol_col is None:
            return {'signal': False, 'description': '成交量数据不足'}

        recent = self.df.tail(5)
        avg_volume = recent[vol_col].mean()

        volume_stable = (recent[vol_col].std() / avg_volume) < 0.3 if avg_volume > 0 else False

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

        high_col = '最高' if '最高' in self.df.columns else 'high'
        recent_30_high = self.df.tail(30)[high_col].max()

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

        recent = self.df.tail(60)

        low_col = '最低' if '最低' in recent.columns else 'low'

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
