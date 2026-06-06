"""
导出功能模块

提供 Excel/CSV 双格式的数据导出功能。
"""

import os
from datetime import datetime
from typing import Dict, List, Any

import pandas as pd
from rich import box
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
)
from rich.table import Table

from akshare_app.config import console
from akshare_app.logging_utils import (
    log_info, log_success, log_warning, log_error,
    _safe_console_print,
)
from akshare_app.utils import clean_special_chars


def export_to_excel(data_dict: Dict[str, pd.DataFrame], filename: str = "A股综合数据.xlsx") -> bool:
    """
    将数据导出到Excel文件

    使用openpyxl引擎将多个DataFrame导出到同一个Excel文件的不同Sheet页。

    Args:
        data_dict: 包含数据的字典，key为Sheet名称，value为DataFrame
        filename: 输出文件名，默认为"A股综合数据.xlsx"

    Returns:
        导出成功返回True，失败返回False

    Example:
        >>> data = {'全市场数据': df1, '涨停股': df2}
        >>> success = export_to_excel(data, 'output.xlsx')
    """
    console.print(f"\n[cyan]> 正在导出数据到 Excel...[/cyan]")
    try:
        def _write_sheets(writer):
            for sheet_name, df in data_dict.items():
                clean_df = df.copy()
                for col in clean_df.columns:
                    if clean_df[col].dtype == object:
                        clean_df[col] = clean_df[col].apply(clean_special_chars)
                clean_df.to_excel(writer, sheet_name=sheet_name, index=False)

        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                console=console
            ) as progress:
                task = progress.add_task("[cyan]写入Sheet页", total=len(data_dict))
                with pd.ExcelWriter(filename, engine='openpyxl') as writer:
                    _write_sheets(writer)
                    progress.update(task, completed=len(data_dict))
        except Exception:
            print(f"正在导出 {len(data_dict)} 个Sheet页...")
            with pd.ExcelWriter(filename, engine='openpyxl') as writer:
                _write_sheets(writer)

        console.print(f"\n[green]* 数据已保存到：{filename}[/green]")

        table = Table(title="包含以下Sheet页", box=box.ASCII)
        table.add_column("序号", style="cyan", justify="center")
        table.add_column("Sheet页名称", style="green")
        table.add_column("数据量", style="yellow", justify="right")

        for idx, (sheet_name, df) in enumerate(data_dict.items(), 1):
            table.add_row(str(idx), sheet_name, f"{len(df)} 条")

        console.print(table)
    except PermissionError:
        console.print(f"\n[red][X] 导出失败：文件被占用，请关闭 Excel 后重试[/red]")
        return False
    except Exception as e:
        console.print(f"\n[red][X] 导出失败：{str(e)}[/red]")
        return False
    return True


def export_strategy_scores_to_csv(results: List[Dict[str, Any]], filepath: str = None) -> str:
    """
    将策略评分结果导出为 CSV 文件（适合Excel友好格式）

    包含所有股票合并在一个CSV文件中，包含：
    - 股票基本信息（名称、代码）
    - 综合得分
    - 三种策略得分
    - 每种策略的详细评分明细

    Args:
        results: 多只股票的评分结果列表
        filepath: 输出文件路径，默认自动生成含时间戳的文件名

    Returns:
        实际生成的 CSV 文件路径。
    """
    try:
        if filepath is None:
            filepath = f"策略评分明细_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

        all_rows = []

        for result in results:
            stock_code = result['stock_code']
            stock_name = result['stock_name']
            comp_score = result['comprehensive_score']

            all_rows.append({
                '股票代码': stock_code,
                '股票名称': stock_name,
                '综合得分': comp_score,
                '策略': '概述',
                '评分维度': '',
                '阈值条件': '',
                '实际值': '',
                '满分': '',
                '得分': '',
                '状态': '',
                '评分依据': ''
            })

            for strategy_name, strategy_result in result['strategies'].items():
                s_score = strategy_result.get('score', 0)
                s_max = strategy_result.get('max_score', 100)
                pct = s_score / s_max * 100 if s_max > 0 else 0
                all_rows.append({
                    '股票代码': '',
                    '股票名称': '',
                    '综合得分': '',
                    '策略': strategy_name,
                    '评分维度': '【总分】',
                    '阈值条件': '',
                    '实际值': '',
                    '满分': s_max,
                    '得分': s_score,
                    '状态': f'{pct:.0f}%',
                    '评分依据': ''
                })

                details = strategy_result.get('details', [])
                for d in details:
                    all_rows.append({
                        '股票代码': '',
                        '股票名称': '',
                        '综合得分': '',
                        '策略': strategy_name,
                        '评分维度': d['name'],
                        '阈值条件': d['threshold'],
                        '实际值': d['value'],
                        '满分': d['max_score'],
                        '得分': d['actual_score'],
                        '状态': d['status'],
                        '评分依据': d['basis']
                    })

            all_rows.append({k: '' for k in all_rows[-1]})

        df = pd.DataFrame(all_rows)
        df.to_csv(filepath, index=False, encoding='utf-8-sig')

        log_success(f"评分明细已导出到: {filepath}", prefix="")
        return filepath
    except Exception as e:
        log_warning(f"导出CSV失败: {str(e)[:50]}")
        return ""


def export_strategy_scores_to_excel(results: List[Dict[str, Any]], filepath: str = None) -> str:
    """
    将策略评分结果导出为 Excel 文件，每只股票一个独立 Sheet

    每个 Sheet 包含：
    - 股票基本信息（名称、代码）
    - 综合得分
    - 每种策略的完整评分明细表（评分维度、阈值条件、实际值、满分、得分、状态、评分依据）

    Args:
        results: 多只股票的评分结果列表
        filepath: 输出文件路径，默认自动生成含时间戳的文件名

    Returns:
        实际生成的 Excel 文件路径。如果 openpyxl 未安装则返回空字符串。
    """
    try:
        if filepath is None:
            filepath = f"策略评分明细_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
            for result in results:
                stock_code = result['stock_code']
                stock_name = result['stock_name']
                sheet_name = str(stock_code)
                if len(sheet_name) > 31:
                    sheet_name = sheet_name[:31]

                rows = []
                rows.append({'评分维度': f'股票: {stock_name}({stock_code})', '阈值条件': '', '实际值': '', '满分': '', '得分(取整)': '', '状态': '', '评分依据': ''})
                rows.append({'评分维度': f'综合得分: {result["comprehensive_score"]:.1f}/100', '阈值条件': '', '实际值': '', '满分': '', '得分(取整)': '', '状态': '', '评分依据': ''})
                rows.append({'评分维度': '', '阈值条件': '', '实际值': '', '满分': '', '得分(取整)': '', '状态': '', '评分依据': ''})

                for strategy_name, strategy_result in result['strategies'].items():
                    score = strategy_result.get('score', 0)
                    max_s = strategy_result.get('max_score', 100)
                    pct = score / max_s * 100 if max_s > 0 else 0
                    rows.append({'评分维度': f'【{strategy_name}】总分: {score}/{max_s} ({pct:.0f}%)', '阈值条件': '', '实际值': '', '满分': '', '得分(取整)': '', '状态': '', '评分依据': ''})

                    details = strategy_result.get('details', [])
                    for d in details:
                        rows.append({
                            '评分维度': d['name'],
                            '阈值条件': d['threshold'],
                            '实际值': d['value'],
                            '满分': d['max_score'],
                            '得分(取整)': d['actual_score'],
                            '状态': d['status'],
                            '评分依据': d['basis']
                        })
                    rows.append({'评分维度': '', '阈值条件': '', '实际值': '', '满分': '', '得分(取整)': '', '状态': '', '评分依据': ''})

                df_sheet = pd.DataFrame(rows)
                df_sheet.to_excel(writer, sheet_name=sheet_name, index=False)

        log_success(f"评分明细已导出到: {filepath}", prefix="")
        return filepath
    except ImportError:
        log_warning("缺少openpyxl库，请执行: pip install openpyxl")
        return ""
    except Exception as e:
        log_warning(f"导出Excel失败: {str(e)[:50]}")
        return ""
