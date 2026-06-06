"""
显示功能模块

提供筛选结果的终端展示功能。
"""

from typing import Dict, Any, List

from akshare_app.config import console
from akshare_app.logging_utils import _safe_console_print
from rich.table import Table
from rich.panel import Panel
from rich import box


def display_stock_result(result: Dict[str, Any]):
    """
    以结构化表格形式在控制台展示单只股票的评分明细

    针对每种策略，依次输出评分维度表格，包含 7 列：
    评分维度（含状态图标 ✓/△/✗）、阈值条件、实际值、满分、得分、状态、评分依据。
    策略标题按得分率着色：≥80% 绿色、≥50% 黄色、<50% 红色。
    底部显示综合得分（三种策略得分的平均值）。

    Args:
        result: 股票评分结果字典，由 ScreeningStrategies 的三种策略方法返回
    """
    if not result:
        return
    console.print(f"\n[bold]股票评分明细: {result['stock_name']}({result['stock_code']})[/bold]\n")
    for strategy_name, strategy_result in result['strategies'].items():
        score = strategy_result.get('score', 0)
        max_s = strategy_result.get('max_score', 100)
        pct = score / max_s * 100 if max_s > 0 else 0
        color = 'green' if pct >= 80 else 'yellow' if pct >= 50 else 'red'
        console.print(f"[bold {color}]{strategy_name}[/bold {color}] 得分: {score}/{max_s} ({pct:.0f}%)")
        details = strategy_result.get('details', [])
        if details:
            t = Table(box=box.ASCII, show_header=True, header_style="bold cyan")
            t.add_column("评分维度", style="cyan")
            t.add_column("阈值条件", style="white")
            t.add_column("实际值", style="yellow")
            t.add_column("满分", justify="center")
            t.add_column("得分", justify="center", style="green")
            t.add_column("状态", justify="center")
            t.add_column("评分依据", style="dim")
            for d in details:
                status_icon = "[green]✓[/green]" if d['status'] == 'passed' else "[yellow]△[/yellow]" if d['status'] == 'partial' else "[red]✗[/red]"
                t.add_row(
                    f"{status_icon} {d['name']}",
                    d['threshold'],
                    d['value'],
                    str(d['max_score']),
                    str(d['actual_score']),
                    d['status'],
                    d['basis']
                )
            console.print(t)
        console.print()
    console.print(f"[bold]综合得分: {result['comprehensive_score']:.1f}/100[/bold]")


def display_recommendations(results: List[Dict[str, Any]]):
    """
    以控制台表格展示批量股票筛选排名

    按综合得分降序排列，输出「推荐股票列表」表格，包含 7 列：
    排名、代码、名称、综合得分、短线强势股得分、主力建仓股得分、价值投资股得分。
    各策略得分按阈值着色（≥80 绿 / ≥50 黄 / <50 红）。

    Args:
        results: 多只股票的评分结果列表，每项格式同 display_stock_result
    """
    if not results:
        console.print("[yellow]暂无筛选结果[/yellow]")
        return
    
    table = Table(
        box=box.ASCII,
        title=f"推荐股票列表 (共{len(results)}支)"
    )
    
    table.add_column("排名", justify="center", style="cyan")
    table.add_column("代码", style="cyan")
    table.add_column("名称", style="green")
    table.add_column("综合得分", justify="right", style="yellow")
    table.add_column("短线强势股", justify="right")
    table.add_column("主力建仓股", justify="right")
    table.add_column("价值投资股", justify="right")
    
    for i, result in enumerate(results, 1):
        score_short = result['strategies'].get('短线强势股', {}).get('score', 0)
        score_main = result['strategies'].get('主力建仓股', {}).get('score', 0)
        score_value = result['strategies'].get('价值投资股', {}).get('score', 0)
        
        def color_score(s):
            return f"[green]{s}[/green]" if s >= 80 else f"[yellow]{s}[/yellow]" if s >= 50 else f"[red]{s}[/red]"
        
        table.add_row(
            f"{i}",
            result['stock_code'],
            result['stock_name'],
            f"{result['comprehensive_score']:.1f}",
            color_score(score_short),
            color_score(score_main),
            color_score(score_value),
        )
    
    console.print(table)
