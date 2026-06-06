"""
L2 二级盘口数据筛选器

基于五档盘口数据的深度筛选。
"""

from typing import Dict, Any, Optional

import pandas as pd

from akshare_app.config import console
from akshare_app.logging_utils import log_info, log_success, log_warning, log_error
from akshare_app.data_fetcher import DataFetcher


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
