"""
市场数据批量获取模块

提供全市场行情数据的批量获取和导出功能，支持25种数据类型。
"""

import time
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import akshare as ak
import tushare as ts
import pandas as pd

from akshare_app.config import (
    console, MAX_WORKERS_DEFAULT,
)
from akshare_app.logging_utils import (
    log_info, log_success, log_warning, log_error,
    _safe_console_print,
)
from akshare_app.cache import _get_cached_market_data
from akshare_app.utils import calc_limit_up_price
from akshare_app.data_fetcher import safe_run_with_progress
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn


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
                    _safe_console_print(f"[green][OK][/green] 成功获取 {len(stock_spot_df)} 只股票数据")
                else:
                    progress.update(task, completed=100)
                    _safe_console_print(f"[red][X][/red] 获取全市场数据失败")
                    return data_dict
        except Exception:
            print("正在获取全市场行情...")
            stock_spot_df = _fetch_market_spot()
            if stock_spot_df is not None:
                data_dict['全市场数据'] = stock_spot_df
                _safe_console_print(f"[green][OK][/green] 成功获取 {len(stock_spot_df)} 只股票数据")
            else:
                _safe_console_print(f"[red][X][/red] 获取全市场数据失败")
                return data_dict
    else:
        stock_spot_df = _fetch_market_spot() or pd.DataFrame()

    limit_up_df = pd.DataFrame()
    if not stock_spot_df.empty and '代码' in stock_spot_df.columns:
        stock_spot_df["计算涨停价"] = stock_spot_df.apply(calc_limit_up_price, axis=1)
        limit_up_df = stock_spot_df[stock_spot_df["最新价"] >= stock_spot_df["计算涨停价"]].copy()
        limit_up_df = limit_up_df.sort_values(by="成交额", ascending=False).reset_index(drop=True)

    if 'limit_up' in selected_options:
        _safe_console_print("\n[yellow]【2/25】[/yellow] [cyan]正在获取涨停股数据...[/cyan]")
        if not limit_up_df.empty:
            data_dict['涨停股'] = limit_up_df[["代码", "名称", "最新价", "涨跌幅", "成交额", "计算涨停价"]]
            _safe_console_print(f"[green][OK][/green] 成功获取 {len(limit_up_df)} 只涨停股票")
        else:
            _safe_console_print("[yellow][!][/yellow] 无涨停股数据（市场数据不可用）")

    if 'rise_top' in selected_options:
        _safe_console_print("\n[yellow]【3/25】[/yellow] [cyan]正在获取涨幅榜TOP20...[/cyan]")
        if not stock_spot_df.empty:
            rise_top_df = stock_spot_df.sort_values(by="涨跌幅", ascending=False).head(20).reset_index(drop=True)
            data_dict['涨幅榜TOP20'] = rise_top_df[["代码", "名称", "最新价", "涨跌幅", "成交量"]]
            _safe_console_print("[green][OK][/green] 成功获取涨幅榜TOP20")
        else:
            _safe_console_print("[yellow][!][/yellow] 无涨幅榜数据（市场数据不可用）")

    if 'fall_top' in selected_options:
        _safe_console_print("\n[yellow]【4/25】[/yellow] [cyan]正在获取跌幅榜TOP20...[/cyan]")
        if not stock_spot_df.empty:
            fall_top_df = stock_spot_df.sort_values(by="涨跌幅", ascending=True).head(20).reset_index(drop=True)
            data_dict['跌幅榜TOP20'] = fall_top_df[["代码", "名称", "最新价", "涨跌幅", "成交量"]]
            _safe_console_print("[green][OK][/green] 成功获取跌幅榜TOP20")
        else:
            _safe_console_print("[yellow][!][/yellow] 无跌幅榜数据（市场数据不可用）")

    if 'industry' in selected_options:
        _safe_console_print("\n[yellow]【6/25】[/yellow] [cyan]正在获取行业板块数据...[/cyan]")
        try:
            industry_info_df = pd.DataFrame()

            try:
                _safe_console_print("[cyan]  -> 方案1: stock_board_industry_spot_em (东方财富)[/cyan]")
                industry_info_df = ak.stock_board_industry_spot_em()
                if industry_info_df is None or industry_info_df.empty:
                    raise Exception("东方财富返回空数据")
            except Exception as e1:
                _safe_console_print(f"[yellow][!][/yellow] 东方财富行业板块失败: {str(e1)[:40]}")
                try:
                    _safe_console_print("[cyan]  -> 方案2: stock_sector_spot (新浪)[/cyan]")
                    industry_info_df = ak.stock_sector_spot()
                    if industry_info_df is None or industry_info_df.empty:
                        raise Exception("新浪返回空数据")
                except Exception as e2:
                    _safe_console_print(f"[yellow][!][/yellow] 新浪行业板块也失败: {str(e2)[:40]}")
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
                _safe_console_print(f"[green][OK][/green] 成功获取行业板块数据: {len(industry_info_df)} 条")
            else:
                _safe_console_print(f"[red][X][/red] 获取行业板块信息失败: 无数据返回")
        except Exception as e:
            _safe_console_print(f"[red][X][/red] 获取行业板块信息失败: {str(e)[:80]}")

    if 'bid_ask' in selected_options:
        _safe_console_print("\n[yellow]【9/25】[/yellow] [cyan]正在获取五档盘口数据...[/cyan]")
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
            _safe_console_print(f"[green][OK][/green] 成功获取五档盘口数据: {len(bid_ask_df)} 条")
        except Exception as e:
            _safe_console_print(f"[red][X][/red] 获取五档盘口数据失败: {str(e)[:80]}")

    if 'financial' in selected_options:
        _safe_console_print("\n[yellow]【10/25】[/yellow] [cyan]正在获取涨停股基本面数据...[/cyan]")
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
            _safe_console_print(f"[green][OK][/green] 成功获取涨停股基本面数据: {len(financial_df)} 条")
        except Exception as e:
            _safe_console_print(f"[red][X][/red] 获取涨停股基本面数据失败: {str(e)[:80]}")

    _simple_apis = []
    if 'fund_flow' in selected_options:
        _simple_apis.append(('fund_flow', '资金流向', lambda: ak.stock_market_fund_flow()))
    if 'hot_deal' in selected_options:
        _simple_apis.append(('hot_deal', '热点成交', lambda: ak.stock_hot_deal_xq()))
    if 'lhb' in selected_options:
        _simple_apis.append(('lhb', '龙虎榜', lambda: ak.stock_lhb_detail_em()))
    if 'trade_balance' in selected_options:
        _simple_apis.append(('trade_balance', '贸易余额', lambda: ak.macro_china_trade_balance()))
    if 'cpi' in selected_options:
        _simple_apis.append(('cpi', 'CPI数据', lambda: ak.macro_china_cpi()))
    if 'gdp' in selected_options:
        _simple_apis.append(('gdp', 'GDP数据', lambda: ak.macro_china_gdp()))
    if 'ppi' in selected_options:
        _simple_apis.append(('ppi', 'PPI数据', lambda: ak.macro_china_ppi()))
    if 'money_supply' in selected_options:
        _simple_apis.append(('money_supply', '货币供应量', lambda: ak.macro_china_money_supply()))
    if 'exchange_rate' in selected_options:
        _simple_apis.append(('exchange_rate', '汇率数据', lambda: ak.currency_boc_safe()))
    if 'bond_yield' in selected_options:
        _simple_apis.append(('bond_yield', '国债收益率', lambda: ak.bond_china_yield()))
    if 'fdi' in selected_options:
        _simple_apis.append(('fdi', '外商投资', lambda: ak.macro_china_fdi()))
    if 'margin' in selected_options:
        _simple_apis.append(('margin', '融资融券', lambda: ak.stock_margin_sse()))
    if 'repurchase' in selected_options:
        _simple_apis.append(('repurchase', '股票回购', lambda: ak.stock_repurchase_em()))

    if _simple_apis:
        _safe_console_print(f"\n[cyan]并发获取 {len(_simple_apis)} 个独立数据源...[/cyan]")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS_DEFAULT) as executor:
            futures = {}
            for key, name, api_func in _simple_apis:
                futures[executor.submit(api_func)] = (key, name)
            for future in as_completed(futures):
                key, name = futures[future]
                try:
                    df = future.result()
                    if df is not None and not isinstance(df, pd.DataFrame):
                        continue
                    if isinstance(df, pd.DataFrame) and df.empty:
                        _safe_console_print(f"[yellow][!][/yellow] {name}数据为空")
                        continue
                    data_dict[name] = df
                    _safe_console_print(f"[green][OK][/green] 成功获取{name}数据: {len(df)} 条")
                except Exception as e:
                    _safe_console_print(f"[red][X][/red] 获取{name}数据失败: {str(e)[:80]}")

    if 'block_trade' in selected_options:
        _safe_console_print("\n[yellow]【11/25】[/yellow] [cyan]正在获取大宗交易数据...[/cyan]")
        try:
            try:
                block_trade_df = ak.stock_fund_flow_big_deal()
            except Exception:
                try:
                    block_trade_df = ak.stock_lhb_detail_em()
                except Exception:
                    block_trade_df = pd.DataFrame()

            if not block_trade_df.empty:
                data_dict['大宗交易'] = block_trade_df
                _safe_console_print(f"[green][OK][/green] 成功获取大宗交易数据: {len(block_trade_df)} 条")
            else:
                _safe_console_print(f"[yellow][!][/yellow] 获取大宗交易数据为空")
        except Exception as e:
            _safe_console_print(f"[red][X][/red] 获取大宗交易数据失败: {str(e)[:80]}")

    if 'concept' in selected_options:
        _safe_console_print("\n[yellow]【14/25】[/yellow] [cyan]正在获取概念板块数据...[/cyan]")
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
                        try:
                            concept_df = ak.stock_fund_flow_concept()
                        except Exception:
                            concept_df = pd.DataFrame()

            if concept_df is not None and not concept_df.empty:
                data_dict['概念板块'] = concept_df
                _safe_console_print(f"[green][OK][/green] 成功获取概念板块数据: {len(concept_df)} 条")
            else:
                _safe_console_print(f"[yellow][!][/yellow] 获取概念板块数据为空")
        except Exception as e:
            _safe_console_print(f"[red][X][/red] 获取概念板块数据失败: {str(e)[:80]}")

    if 'hk_hold' in selected_options:
        _safe_console_print("\n[yellow]【15/25】[/yellow] [cyan]正在获取北向资金持股数据...[/cyan]")
        try:
            hk_hold_df = pd.DataFrame()

            try:
                hk_hold_df = ak.stock_hsgt_individual_em()
            except Exception:
                try:
                    hk_hold_df = ak.stock_hsgt_fund_flow_summary_em()
                except Exception:
                    hk_hold_df = pd.DataFrame()

            if hk_hold_df is not None and not hk_hold_df.empty:
                data_dict['北向资金持股'] = hk_hold_df
                _safe_console_print(f"[green][OK][/green] 成功获取北向资金持股数据: {len(hk_hold_df)} 条")
            else:
                _safe_console_print(f"[yellow][!][/yellow] 获取北向资金持股数据为空")
        except Exception as e:
            _safe_console_print(f"[red][X][/red] 获取北向资金持股数据失败: {str(e)[:80]}")

    if 'new_share' in selected_options:
        _safe_console_print("\n[yellow]【17/25】[/yellow] [cyan]正在获取新股申购数据...[/cyan]")
        try:
            new_share_df = pd.DataFrame()

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
                _safe_console_print(f"[green][OK][/green] 成功获取新股申购数据: {len(new_share_df)} 条")
            else:
                _safe_console_print(f"[yellow][!][/yellow] 获取新股申购数据为空")
        except Exception as e:
            _safe_console_print(f"[red][X][/red] 获取新股申购数据失败: {str(e)[:80]}")

    if 'lhb_detail' in selected_options:
        _safe_console_print("\n[yellow]【18/25】[/yellow] [cyan]正在获取龙虎榜详情数据...[/cyan]")
        try:
            lhb_detail_df = pd.DataFrame()
            try:
                lhb_detail_df = ak.stock_lhb_detail_em()
                if lhb_detail_df is None or lhb_detail_df.empty:
                    raise Exception("东方财富返回空")
            except Exception:
                try:
                    lhb_detail_df = ak.stock_lhb_detail_daily_sina()
                except Exception:
                    pass

            if lhb_detail_df is not None and not lhb_detail_df.empty:
                data_dict['龙虎榜详情'] = lhb_detail_df
                _safe_console_print(f"[green][OK][/green] 成功获取龙虎榜详情数据: {len(lhb_detail_df)} 条")
            else:
                _safe_console_print(f"[yellow][!][/yellow] 获取龙虎榜详情数据为空")
        except Exception as e:
            _safe_console_print(f"[red][X][/red] 获取龙虎榜详情数据失败: {str(e)[:80]}")

    return data_dict
