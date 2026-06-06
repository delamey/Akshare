"""
单股分析模块

提供单只股票的详细分析数据获取和展示功能。
"""

import time
from typing import Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from akshare_app.config import console, API_DELAY_SHORT, API_DELAY_MEDIUM
from akshare_app.logging_utils import (
    log_info, log_success, log_warning, log_error,
    _safe_console_print, _safe_display,
)
from akshare_app.utils import clean_stock_code, find_column, make_l1_dict
from akshare_app.cache import _clear_market_cache
from akshare_app.indicators import calculate_rsi, calculate_macd, calculate_momentum
from akshare_app.data_fetcher import (
    _fetch_l1_data, _fetch_l2_data, _fetch_fund_flow_data,
    _fetch_financial_data, _fetch_dividend_data, _fetch_industry_data,
    _fetch_basic_info, _fetch_additional_data, _fetch_tushare_data,
    _fetch_historical_klines, _calculate_technical_indicators, get_stock_name,
)
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box


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
