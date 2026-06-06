"""
主入口模块

提供程序的主菜单循环、股票筛选交互和错误处理入口。
"""

import sys
import time
from datetime import datetime

import pandas as pd
from rich.table import Table
from rich.text import Text

from akshare_app.config import console
from akshare_app.logging_utils import _safe_console_print, log_info, log_success, log_warning, log_error
from akshare_app.cache import warmup_market_cache, _check_warmup_thread_health
from akshare_app.market_data import get_stock_data
from akshare_app.analysis import get_single_stock_analysis, display_stock_analysis
from akshare_app.ai_analysis import (
    analyze_stocks_with_deepseek,
    analyze_single_stock_with_deepseek,
    analyze_screening_results_with_deepseek,
)
from akshare_app.export import export_to_excel
from akshare_app.screeners import StockScreener
from akshare_app.display import display_stock_result, display_recommendations
from akshare_app.export import export_strategy_scores_to_csv, export_strategy_scores_to_excel
from rich.panel import Panel
from rich import box
from rich.markup import escape as rich_escape


def main() -> None:
    """
    程序主入口

    提供用户交互界面，支持两种操作模式：
    1. 批量数据导出 - 导出13种股票数据到Excel
    2. 单股分析查询 - 输入股票代码获取详细技术分析

    Returns:
        None
    """
    console.print(Panel.fit(
        Text("A股数据查询工具 v3.0", style="bold cyan", justify="center"),
        subtitle="基于 Akshare.py",
        border_style="cyan",
        box=box.ASCII
    ))

    warmup_market_cache()

    console.print("\n[cyan]请选择操作模式：[/cyan]")
    mode_table = Table(box=box.ASCII)
    mode_table.add_column("序号", style="cyan", justify="center")
    mode_table.add_column("模式", style="green")
    mode_table.add_column("说明", style="dim")

    mode_table.add_row("1", "批量数据导出", "导出批量股票数据到Excel")
    mode_table.add_row("2", "单股分析查询", "输入股票代码获取详细技术分析")
    mode_table.add_row("3", "股票智能筛选", "基于L1/L2数据的智能股票筛选")
    mode_table.add_row("q", "退出", "退出程序")

    console.print(mode_table)

    while True:
        try:
            _check_warmup_thread_health()
            mode = input("\n> 请输入模式序号（1/2/3/q）: ").strip().lower()
        except EOFError:
            console.print("\n[red][!] 输入错误，程序退出[/red]")
            return

        if mode == 'q':
            console.print("\n[blue]感谢使用，再见！[/blue]")
            return

        if mode == '2':
            console.print("\n[cyan]已进入单股分析查询模式[/cyan]")
            console.print(Panel.fit(
                Text("单只股票详细分析", style="bold yellow", justify="center"),
                border_style="yellow",
                box=box.ASCII
            ))

            while True:
                try:
                    stock_code = input("\n> 请输入股票代码（6位数字，如 600000，输入 q 返回上级菜单）: ").strip()
                except EOFError:
                    console.print("\n[red][!] 输入错误，返回上级菜单[/red]")
                    break

                if stock_code.lower() == 'q':
                    break

                if len(stock_code) != 6 or not stock_code.isdigit():
                    console.print("[red][!] 请输入有效的6位股票代码[/red]")
                    continue

                analysis_result = get_single_stock_analysis(stock_code)

                if analysis_result:
                    display_stock_analysis(analysis_result)

                    try:
                        save = input("\n> 是否保存分析报告到Excel？(Y/N): ").strip().upper()
                    except EOFError:
                        save = 'N'
                    if save == 'Y':
                        try:
                            report_data = {}

                            if analysis_result['L1数据']:
                                l1_df = pd.DataFrame([analysis_result['L1数据']])
                                report_data['L1数据'] = l1_df

                            if analysis_result['L2数据']:
                                l2_df = pd.DataFrame([analysis_result['L2数据']])
                                report_data['L2数据'] = l2_df

                            if analysis_result['技术指标']:
                                tech_df = pd.DataFrame([analysis_result['技术指标']])
                                report_data['技术指标'] = tech_df

                            if analysis_result['资金流向']:
                                fund_df = pd.DataFrame([analysis_result['资金流向']])
                                report_data['资金流向'] = fund_df

                            if analysis_result['财务报表']:
                                if '财务指标' in analysis_result['财务报表']:
                                    finance_df = pd.DataFrame([analysis_result['财务报表']['财务指标']])
                                    report_data['财务指标'] = finance_df

                            if analysis_result['分红送配']:
                                dividend_df = pd.DataFrame([analysis_result['分红送配']])
                                report_data['分红送配'] = dividend_df

                            if '历史K线' in analysis_result and not analysis_result['历史K线'].empty:
                                report_data['历史K线'] = analysis_result['历史K线']

                            if '基本信息' in analysis_result and analysis_result['基本信息']:
                                basic_df = pd.DataFrame([analysis_result['基本信息']])
                                report_data['基本信息'] = basic_df

                            if '资金流向详细' in analysis_result and analysis_result['资金流向详细']:
                                moneyflow_detail_df = pd.DataFrame(analysis_result['资金流向详细'])
                                report_data['资金流向详细'] = moneyflow_detail_df

                            if '融资融券' in analysis_result and analysis_result['融资融券']:
                                margin_df = pd.DataFrame(analysis_result['融资融券'])
                                report_data['融资融券'] = margin_df

                            if '龙虎榜' in analysis_result and analysis_result['龙虎榜']:
                                lhb_df = pd.DataFrame(analysis_result['龙虎榜'])
                                report_data['龙虎榜'] = lhb_df

                            if '大宗交易' in analysis_result and analysis_result['大宗交易']:
                                bigdeal_df = pd.DataFrame(analysis_result['大宗交易'])
                                report_data['大宗交易'] = bigdeal_df

                            if '机构持股' in analysis_result and analysis_result['机构持股']:
                                institute_df = pd.DataFrame(analysis_result['机构持股'])
                                report_data['机构持股'] = institute_df

                            if '业绩预告' in analysis_result and analysis_result['业绩预告']:
                                forecast_df = pd.DataFrame(analysis_result['业绩预告'])
                                report_data['业绩预告'] = forecast_df

                            if '限售股解禁' in analysis_result and analysis_result['限售股解禁']:
                                pledge_df = pd.DataFrame(analysis_result['限售股解禁'])
                                report_data['限售股解禁'] = pledge_df

                            if 'Tushare行情' in analysis_result and analysis_result['Tushare行情']:
                                ts_daily_df = pd.DataFrame(analysis_result['Tushare行情'])
                                report_data['Tushare行情'] = ts_daily_df

                            if 'Tushare财务指标' in analysis_result and analysis_result['Tushare财务指标']:
                                ts_fin_df = pd.DataFrame(analysis_result['Tushare财务指标'])
                                report_data['Tushare财务指标'] = ts_fin_df

                            if 'Tushare利润表' in analysis_result and analysis_result['Tushare利润表']:
                                ts_income_df = pd.DataFrame(analysis_result['Tushare利润表'])
                                report_data['Tushare利润表'] = ts_income_df

                            if 'Tushare资产负债表' in analysis_result and analysis_result['Tushare资产负债表']:
                                ts_balance_df = pd.DataFrame(analysis_result['Tushare资产负债表'])
                                report_data['Tushare资产负债表'] = ts_balance_df

                            if 'Tushare现金流量表' in analysis_result and analysis_result['Tushare现金流量表']:
                                ts_cashflow_df = pd.DataFrame(analysis_result['Tushare现金流量表'])
                                report_data['Tushare现金流量表'] = ts_cashflow_df

                            if 'Tushare前十大股东' in analysis_result and analysis_result['Tushare前十大股东']:
                                ts_holders_df = pd.DataFrame(analysis_result['Tushare前十大股东'])
                                report_data['Tushare前十大股东'] = ts_holders_df

                            if 'Tushare主力资金' in analysis_result and analysis_result['Tushare主力资金']:
                                ts_moneyflow_df = pd.DataFrame(analysis_result['Tushare主力资金'])
                                report_data['Tushare主力资金'] = ts_moneyflow_df

                            if report_data:
                                filename = f"{stock_code}_分析报告.xlsx"
                                export_to_excel(report_data, filename)
                                console.print(f"[green]  -> 已保存 {len(report_data)} 个数据表到 {filename}[/green]")
                            else:
                                console.print("[red][X] 没有可保存的数据[/red]")
                        except Exception as e:
                            console.print(f"[red][X] 保存失败：{str(e)[:50]}[/red]")

                    try:
                        ai_analysis = input("\n> 是否调用AI模型进行个股分析？(Y/N): ").strip().upper()
                    except EOFError:
                        ai_analysis = 'N'
                    if ai_analysis == 'Y':
                        console.print("\n" + "=" * 60)
                        console.print("[cyan]正在进行AI个股分析...[/cyan]")
                        console.print("=" * 60)

                        analysis_result = analyze_single_stock_with_deepseek(analysis_result)

                        console.print("\n" + "=" * 60)
                        console.print("[green]qwen-plus 个股分析结果[/green]")
                        console.print("=" * 60)
                        console.print(analysis_result)
                        console.print("\n" + "=" * 60)
                else:
                    console.print("[red][X] 获取股票分析失败[/red]")

            console.print("\n[cyan]已返回上级菜单[/cyan]\n")
            continue

        if mode == '3':
            console.print("\n[cyan]已进入股票智能筛选模式[/cyan]")
            run_stock_screener()
            console.print("\n[cyan]已返回上级菜单[/cyan]\n")
            continue

        if mode == '1':
            break
        else:
            console.print("[red][!] 请输入有效的模式序号[/red]")

    options = {
        'market': '全市场数据',
        'limit_up': '涨停股',
        'rise_top': '涨幅榜TOP20',
        'fall_top': '跌幅榜TOP20',
        'fund_flow': '资金流向',
        'industry': '行业板块',
        'hot_deal': '热点成交',
        'lhb': '龙虎榜',
        'lhb_detail': '龙虎榜详情',
        'bid_ask': '五档盘口',
        'financial': '涨停股基本面',
        'block_trade': '大宗交易',
        'trade_balance': '贸易余额',
        'cpi': 'CPI数据',
        'concept': '概念板块',
        'hk_hold': '北向资金持股',
        'margin': '融资融券',
        'new_share': '新股申购',
        'gdp': 'GDP数据',
        'ppi': 'PPI数据',
        'money_supply': '货币供应量',
        'exchange_rate': '汇率数据',
        'bond_yield': '国债收益率',
        'repurchase': '股票回购',
        'fdi': '外商投资'
    }

    console.print("\n[cyan]请选择要导出的数据类型：[/cyan]")
    console.print("-" * 60)

    console.print(f"  [0] 导出全部数据类型（共 {len(options)} 项）")
    for idx, (key, value) in enumerate(options.items(), 1):
        console.print(f"  [{idx}] {value}")

    console.print("-" * 60)

    while True:
        try:
            selected_indices = input("> 请输入选择（0=全部，或输入数字如: 1 3 5）: ").strip()
        except EOFError:
            console.print("\n[red][!] 输入错误，程序退出[/red]")
            return
        if not selected_indices:
            console.print("[red][!] 请至少选择一项[/red]")
            continue

        try:
            indices = [int(i.strip()) for i in selected_indices.split()]

            if 0 in indices:
                indices = list(range(1, len(options) + 1))
                console.print("[cyan]已选择导出全部数据类型[/cyan]")
                break

            if all(1 <= i <= len(options) for i in indices):
                break
            else:
                console.print(f"[red][!] 请输入0-{len(options)}之间的数字[/red]")
        except ValueError:
            console.print("[red][!] 请输入有效的数字[/red]")

    selected_options = [list(options.keys())[i-1] for i in indices]
    selected_names = [options[key] for key in selected_options]

    console.print("\n[cyan]你选择了以下数据类型：[/cyan]")
    for name in selected_names:
        console.print(f"  ✓ {name}")

    try:
        confirm = input("\n> 确认开始获取数据？(Y/N): ").strip().upper()
    except EOFError:
        confirm = 'N'
    if confirm != 'Y':
        console.print("\n[blue]操作已取消[/blue]")
        return

    console.print("\n" + "=" * 60)
    console.print("[cyan]开始批量获取数据...[/cyan]")
    console.print("=" * 60)

    data_dict = get_stock_data(selected_options)

    if data_dict:
        export_to_excel(data_dict)
    else:
        console.print("\n[red][X] 没有获取到任何数据[/red]")


def run_stock_screener():
    """
    股票智能筛选系统 — 交互式主循环

    提供三种交互功能：
    1. 单股筛选分析：输入股票代码，展示三种策略的完整评分明细表格
    2. 批量智能筛选：自动遍历全市场股票，按综合得分排序推荐
    3. 切换推荐数量：自定义批量筛选时返回的股票数量

    用户可选择对筛选结果调用 AI 模型（DashScope / 通义千问 qwen-plus）
    进行深度分析，或导出 Excel 评分表（每只股票一个独立 Sheet）。

    每只股票的数据获取耗时约 3~8 秒，批量筛选会根据股票数量相应延长。
    """
    screener = StockScreener()

    warmup_market_cache()

    _safe_console_print(Panel.fit(
        "[bold cyan]股票智能筛选系统[/bold cyan]\n\n"
        "基于L1和L2数据的自动化股票筛选工具\n"
        "综合运用多维度指标，智能筛选有价值和潜力的股票",
        box=box.ASCII
    ))

    while True:
        _check_warmup_thread_health()
        _safe_console_print("\n" + "-" * 60)
        _safe_console_print("请选择功能:")
        _safe_console_print("  [1] 单股筛选分析")
        _safe_console_print("  [2] 批量智能筛选（自动推荐最佳股票）")
        _safe_console_print(f"  [3] 切换推荐数量（当前: {screener.max_recommendations}支）")
        _safe_console_print(f"  {rich_escape('[q]')} 返回上级菜单")
        _safe_console_print("-" * 60)

        try:
            choice = input("> 请输入选项: ").strip()
        except EOFError:
            _safe_console_print("\n[yellow]返回上级菜单[/yellow]", "\n返回上级菜单")
            return

        if choice == 'q' or choice == 'Q':
            return

        elif choice == '1':
            try:
                stock_code = input("\n> 请输入股票代码（如 688275）: ").strip()
                if not stock_code:
                    _safe_console_print("[yellow]股票代码不能为空[/yellow]", "股票代码不能为空")
                    continue

                if len(stock_code) != 6 or not stock_code.isdigit():
                    _safe_console_print("[red][!] 请输入有效的6位数字股票代码[/red]", "[!] 请输入有效的6位数字股票代码")
                    continue

                _safe_console_print("\n" + "-" * 60)
                _safe_console_print("请选择筛选策略:")
                _safe_console_print("  [1] 短线强势股策略")
                _safe_console_print("  [2] 主力建仓股策略")
                _safe_console_print("  [3] 价值投资股策略")
                _safe_console_print(f"  {rich_escape('[all]')} 全部策略")

                strategy = input("> 请输入选项: ").strip()

                result = screener.screen_single_stock(stock_code, strategy)

                _safe_console_print("\n")

                display_stock_result(result)

            except Exception as e:
                _safe_console_print(f"\n[red]筛选出错: {e}[/red]", f"\n筛选出错: {e}")

        elif choice == '2':
            try:
                _safe_console_print("\n" + "-" * 60)
                _safe_console_print("请选择筛选策略:")
                _safe_console_print("  [1] 短线强势股策略")
                _safe_console_print("  [2] 主力建仓股策略")
                _safe_console_print("  [3] 价值投资股策略")
                _safe_console_print(f"  {rich_escape('[all]')} 全部策略")

                strategy = input("> 请输入选项: ").strip()

                results = screener.get_top_stocks(strategy)

                _safe_console_print("\n")

                display_recommendations(results)

                if results:
                    _safe_console_print("\n请选择导出格式:")
                    _safe_console_print("  [1] 导出为 CSV (单文件，所有股票合并)")
                    _safe_console_print("  [2] 导出为 Excel (每只股票一个Sheet)")
                    _safe_console_print("  [3] 同时导出 CSV 和 Excel")
                    _safe_console_print("  [N/n] 不导出")

                    try:
                        export_choice = input("> 请输入选项: ").strip().upper()
                        if export_choice == '1':
                            export_strategy_scores_to_csv(results)
                        elif export_choice == '2':
                            export_strategy_scores_to_excel(results)
                        elif export_choice == '3':
                            export_strategy_scores_to_csv(results)
                            export_strategy_scores_to_excel(results)
                        elif export_choice == 'N':
                            pass
                        else:
                            _safe_console_print("[yellow]无效的选项，跳过导出[/yellow]", "无效的选项，跳过导出")
                    except EOFError:
                        _safe_console_print("\n[yellow]跳过导出[/yellow]", "\n跳过导出")

                if results:
                    try:
                        ai_choice = input("\n> 是否需要AI深度分析这些股票？(Y/N): ").strip().upper()
                        if ai_choice == 'Y':
                            analyze_screening_results_with_deepseek(results)
                    except EOFError:
                        _safe_console_print("\n[yellow]跳过AI分析[/yellow]", "\n跳过AI分析")

            except Exception as e:
                _safe_console_print(f"\n[red]筛选出错: {e}[/red]", f"\n筛选出错: {e}")

        elif choice == '3':
            try:
                new_count = input(f"\n> 请输入推荐数量（1-20，当前: {screener.max_recommendations}）: ").strip()
                if new_count.isdigit():
                    count = int(new_count)
                    if 1 <= count <= 20:
                        screener.max_recommendations = count
                        _safe_console_print(f"[green]已更新推荐数量为 {count} 支[/green]", f"已更新推荐数量为 {count} 支")
                    else:
                        _safe_console_print("[yellow]数量必须在1-20之间[/yellow]", "数量必须在1-20之间")
            except Exception as e:
                _safe_console_print(f"[yellow]设置失败: {e}[/yellow]", f"设置失败: {e}")

        else:
            _safe_console_print("[yellow]无效的选项，请重新输入[/yellow]", "无效的选项，请重新输入")


def main_with_error_handling():
    """
    主程序入口（带错误处理）

    该函数包装了主程序，并添加了全局异常处理机制：
    1. 捕获所有未处理的异常
    2. 生成详细的错误报告
    3. 将错误日志保存到文件
    4. 显示友好的错误提示
    5. 等待用户按键退出
    """
    try:
        main()
    except Exception as e:
        import traceback
        import datetime

        error_msg = f"""错误时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
错误类型: {type(e).__name__}
错误信息: {str(e)}
错误详情:
{traceback.format_exc()}
"""

        try:
            print("\n" + "=" * 70)
            print("程序运行出错")
            print("=" * 70)
            print(f"错误类型: {type(e).__name__}")
            print(f"错误信息: {str(e)}")
            print("\n错误详情:")
            print(traceback.format_exc())
        except Exception:
            pass

        try:
            log_file = f"error_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            with open(log_file, 'w', encoding='utf-8') as f:
                f.write(error_msg)
            print(f"\n错误日志已保存到: {log_file}")
        except Exception as log_error:
            try:
                print(f"\n警告: 无法保存错误日志: {str(log_error)}")
            except Exception:
                pass

        try:
            print("\n" + "=" * 70)
            print("按任意键继续...")
        except Exception:
            pass

        try:
            import sys
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.getch()
            else:
                input()
        except Exception:
            input("按回车键继续...")
