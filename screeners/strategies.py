"""
筛选策略模块

提供三种投资策略的评分计算：短线强势股、主力建仓股、价值投资股。
"""

import time
from typing import Dict, Any, List, Optional

from akshare_app.config import (
    console, STRATEGY_THRESHOLD_SHORT, STRATEGY_THRESHOLD_MAIN,
    STRATEGY_THRESHOLD_VALUE,
)
from akshare_app.logging_utils import log_info, log_success, log_warning, log_error
from akshare_app.screeners.l1_screener import L1Screener
from akshare_app.screeners.l2_screener import L2Screener
from akshare_app.utils import clean_stock_code
from rich.table import Table
from rich import box


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
