"""
AI分析模块

集成 DashScope/通义千问 API，提供股票数据深度分析功能。
"""

import json
from datetime import datetime
from typing import Dict, Any, Optional, List

import pandas as pd
import requests

from akshare_app.config import (
    DASHSCOPE_API_KEY, DASHSCOPE_API_URL, _HTTP_SESSION,
)
from akshare_app.logging_utils import (
    log_info, log_success, log_warning, log_error,
    _safe_console_print, _safe_display,
)
from akshare_app.utils import clean_special_chars
from akshare_app.config import console
from rich.console import Console
from rich.panel import Panel
from rich import box


_DASHSCOPE_PLACEHOLDER_MSG = (
    "[yellow]提示：请前往 https://dashscope.aliyun.com 注册获取免费API Key，并替换代码中的api_key[/yellow]\n"
    "新用户赠送100万Token，支持qwen-max、qwen-plus等模型。"
)

_DASHSCOPE_MOCK_ANALYSIS = (
    "\n【模拟分析结果】\n"
    "\n基于数据分析，以下是有潜力的股票推荐：\n"
    "\n1. 北向资金重仓股 - 关注外资持续流入的标的\n"
    "2. 涨停股中的热点板块龙头\n"
    "3. 行业板块涨幅居前的细分领域\n"
    "4. 注意风险控制，建议结合基本面分析"
)


def _call_dashscope_api(system_prompt: str, user_content: str,
                        temperature: float = 0.7, max_tokens: int = 2000,
                        timeout: int = 60) -> str:
    """调用 DashScope (通义千问) API 的公共函数

    Args:
        system_prompt: 系统提示词
        user_content: 用户消息内容
        temperature: 生成温度，默认 0.7
        max_tokens: 最大输出 token 数，默认 2000
        timeout: 请求超时时间（秒），默认 60

    Returns:
        AI 生成的文本内容。如果 API Key 为占位符则返回模拟结果；
        请求失败则返回带 [red] 标签的错误信息字符串。
    """
    if not DASHSCOPE_API_KEY or DASHSCOPE_API_KEY.startswith("sk-xxx"):
        return _DASHSCOPE_PLACEHOLDER_MSG + _DASHSCOPE_MOCK_ANALYSIS

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}"
    }

    payload = {
        "model": "qwen-plus",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        "temperature": temperature,
        "max_tokens": max_tokens
    }

    try:
        response = requests.post(DASHSCOPE_API_URL, headers=headers, json=payload, timeout=timeout)
        response.raise_for_status()
        result = response.json()
        return result['choices'][0]['message']['content']
    except requests.exceptions.RequestException as e:
        return f"[red]API调用失败: {str(e)}[/red]"
    except Exception as e:
        return f"[red]分析失败: {str(e)}[/red]"


def analyze_stocks_with_deepseek(data_dict: Dict[str, pd.DataFrame]) -> str:
    """
    调用 AI 模型分析全市场股票数据，挖掘有潜力的股票

    实际调用 DashScope API（通义千问 qwen-plus 模型）进行智能分析。
    需要配置环境变量 DASHSCOPE_API_KEY。

    Args:
        data_dict: 包含全市场数据、涨停股、北向资金、资金流向、行业板块等

    Returns:
        AI 模型生成的分析结果字符串
    """
    console.print("\n[cyan]正在调用AI模型进行股票市场分析...[/cyan]")

    try:
        summary = "【股票数据分析报告】\n\n"

        if '全市场数据' in data_dict:
            market_df = data_dict['全市场数据']
            summary += f"1. 全市场概况：共 {len(market_df)} 只股票\n"
            summary += f"   - 平均涨幅: {market_df['涨跌幅'].mean():.2f}%\n"
            summary += f"   - 上涨家数: {len(market_df[market_df['涨跌幅'] > 0])}\n"
            summary += f"   - 下跌家数: {len(market_df[market_df['涨跌幅'] < 0])}\n"

        if '涨停股' in data_dict:
            limit_up_df = data_dict['涨停股']
            summary += f"\n2. 涨停股分析：共 {len(limit_up_df)} 只涨停\n"
            top_stocks = limit_up_df.head(5)[['代码', '名称', '成交额']]
            for _, row in top_stocks.iterrows():
                summary += f"   - {row['代码']} {row['名称']} 成交额: {row['成交额']:.2f}亿\n"

        if '北向资金持股' in data_dict:
            hk_df = data_dict['北向资金持股']
            summary += f"\n3. 北向资金持股：共 {len(hk_df)} 只股票\n"
            if '持股比例' in hk_df.columns:
                top_hk = hk_df.sort_values('持股比例', ascending=False).head(3)
                for _, row in top_hk.iterrows():
                    summary += f"   - {row.get('股票代码', 'N/A')} {row.get('股票名称', 'N/A')} 持股比例: {row.get('持股比例', 'N/A')}%\n"

        if '资金流向' in data_dict:
            fund_flow = data_dict['资金流向']
            summary += f"\n4. 资金流向分析：\n"
            if not fund_flow.empty:
                latest = fund_flow.iloc[-1]
                summary += f"   - 北向资金: {latest.get('北向资金', 'N/A')}\n"
                summary += f"   - 南向资金: {latest.get('南向资金', 'N/A')}\n"

        if '行业板块' in data_dict:
            industry_df = data_dict['行业板块']
            summary += f"\n5. 行业板块表现：共 {len(industry_df)} 个行业\n"
            top_industry = industry_df.sort_values('涨跌幅', ascending=False).head(3)
            for _, row in top_industry.iterrows():
                summary += f"   - {row.get('行业名称', row.get('板块', 'N/A'))} 涨幅: {row.get('涨跌幅', 'N/A')}%\n"

        if '龙虎榜' in data_dict:
            lhb_df = data_dict['龙虎榜']
            summary += f"\n6. 龙虎榜数据：共 {len(lhb_df)} 条记录\n"

        summary += "\n【分析请求】\n请根据以上数据，分析并推荐有潜力的股票，给出具体理由。"

        return _call_dashscope_api(
            system_prompt="你是一位专业的股票分析师，请基于提供的数据进行客观分析，用中文回复",
            user_content=summary,
            max_tokens=2000,
            timeout=60
        )

    except Exception as e:
        return f"[red]分析失败: {str(e)}[/red]"


def analyze_single_stock_with_deepseek(analysis_result: Dict[str, Any]) -> str:
    """
    调用 AI 模型对单只股票进行深度分析

    实际调用 DashScope API（通义千问 qwen-plus 模型）进行智能分析。
    需要配置环境变量 DASHSCOPE_API_KEY。

    Args:
        analysis_result: 单股分析结果字典，包含L1数据、技术指标、资金流向等

    Returns:
        AI 模型生成的分析结果字符串，包含股票潜力评估和购买建议
    """
    console.print("\n[cyan]正在调用AI模型进行个股分析...[/cyan]")

    try:
        stock_code = analysis_result.get('股票代码', '未知')

        stock_name = stock_code
        if analysis_result.get('L1数据') and analysis_result['L1数据'].get('股票名称'):
            stock_name = analysis_result['L1数据']['股票名称']

        summary = f"【个股分析报告 - {stock_code} {stock_name}】\n\n"

        if analysis_result['L1数据']:
            l1 = analysis_result['L1数据']
            summary += "一、基本行情数据\n"
            summary += f"  - 股票代码: {stock_code}\n"
            summary += f"  - 股票名称: {l1.get('股票名称', 'N/A')}\n"
            summary += f"  - 最新价: {l1.get('最新价', 'N/A')}\n"
            summary += f"  - 涨跌幅: {l1.get('涨跌幅', 'N/A')}\n"
            summary += f"  - 量比: {l1.get('量比', 'N/A')}\n"
            summary += f"  - 换手率: {l1.get('换手率', 'N/A')}\n"
            summary += f"  - 市盈率: {l1.get('市盈率-动态', 'N/A')}\n"
            summary += f"  - 市净率: {l1.get('市净率', 'N/A')}\n"
            summary += f"  - 总市值: {l1.get('总市值', 'N/A')}\n"

        if analysis_result['技术指标']:
            tech = analysis_result['技术指标']
            summary += "\n二、技术指标分析\n"
            summary += f"  - RSI(6): {tech.get('RSI(6)', 'N/A')}\n"
            summary += f"  - RSI(12): {tech.get('RSI(12)', 'N/A')}\n"
            summary += f"  - MACD: {tech.get('MACD', 'N/A')}\n"
            summary += f"  - MACD Signal: {tech.get('MACD_Signal', 'N/A')}\n"
            summary += f"  - MACD Histogram: {tech.get('MACD_Histogram', 'N/A')}\n"
            summary += f"  - 5日动量: {tech.get('5日动量', 'N/A')}%\n"
            summary += f"  - 20日动量: {tech.get('20日动量', 'N/A')}%\n"
            summary += f"  - 成交量异动率(5日): {tech.get('成交量异动率(5日)', 'N/A')}%\n"

        if analysis_result['资金流向']:
            fund = analysis_result['资金流向']
            summary += "\n三、资金流向分析\n"
            summary += f"  - 主力净流入: {fund.get('今日主力净流入', 'N/A')}\n"
            summary += f"  - 超大单净流入: {fund.get('今日超大单净流入', 'N/A')}\n"
            summary += f"  - 大单净流入: {fund.get('今日大单净流入', 'N/A')}\n"

        if analysis_result['财务报表'] and analysis_result['财务报表'].get('财务指标'):
            financial = analysis_result['财务报表']['财务指标']
            summary += "\n四、财务指标分析\n"
            summary += f"  - ROE: {financial.get('ROE', 'N/A')}%\n"
            summary += f"  - 净利润: {financial.get('净利润', 'N/A')}\n"
            summary += f"  - 营业收入: {financial.get('营业收入', 'N/A')}\n"
            summary += f"  - 毛利率: {financial.get('毛利率', 'N/A')}%\n"
            summary += f"  - 净利率: {financial.get('净利率', 'N/A')}%\n"

        if analysis_result['分红送配']:
            dividend = analysis_result['分红送配']
            summary += "\n五、分红送配信息\n"
            summary += f"  - 分红年度: {dividend.get('分红年度', 'N/A')}\n"
            summary += f"  - 每股分红: {dividend.get('每股分红', 'N/A')}\n"
            summary += f"  - 送股比例: {dividend.get('送股比例', 'N/A')}\n"
            summary += f"  - 转增比例: {dividend.get('转增比例', 'N/A')}\n"

        summary += "\n【分析请求】\n请根据以上数据，对该股票进行综合分析，包括：\n"
        summary += "1. 该股票的投资潜力评估\n"
        summary += "2. 技术面分析（趋势、买卖信号）\n"
        summary += "3. 基本面分析（估值、财务健康度）\n"
        summary += "4. 风险提示\n"
        summary += "5. 购买建议（强烈推荐/推荐/观望/谨慎/回避）\n"
        summary += "请用中文详细分析，给出具体理由和建议。"

        return _call_dashscope_api(
            system_prompt="你是一位专业的A股股票分析师，擅长技术分析和基本面分析。请基于用户提供的个股数据，给出客观、专业的投资建议，用中文回复。",
            user_content=summary,
            max_tokens=2500,
            timeout=60
        )

    except Exception as e:
        return f"[red]分析失败: {str(e)}[/red]"


def analyze_screening_results_with_deepseek(results: List[Dict[str, Any]]) -> None:
    """
    调用 AI 模型对批量筛选结果进行深度分析

    实际调用 DashScope API（通义千问 qwen-plus 模型）进行智能分析。
    生成的分析报告自动保存到本地文件。

    Args:
        results: 筛选结果列表，每项包含股票代码、名称及三种策略得分
    """
    console.print("\n[cyan]正在调用AI模型进行深度分析...[/cyan]")

    try:
        summary = "【股票智能筛选分析报告】\n\n"
        summary += f"筛选出 {len(results)} 支符合条件的股票：\n\n"

        for i, result in enumerate(results, 1):
            stock_code = result['stock_code']
            stock_name = result['stock_name']
            score = result['comprehensive_score']

            summary += f"{i}. {stock_code} {stock_name}\n"
            summary += f"   综合得分: {score:.1f}\n"

            for strategy_name, strategy_result in result['strategies'].items():
                s_score = strategy_result.get('score', 0)
                s_max = strategy_result.get('max_score', 100)
                summary += f"   [{strategy_name}] 得分: {s_score}/{s_max}\n"
                details = strategy_result.get('details', [])
                for d in details:
                    icon = '✓' if d['status'] == 'passed' else '△' if d['status'] == 'partial' else '✗'
                    summary += f"      {icon} {d['name']}: {d['value']} ({d['actual_score']}/{d['max_score']}分)\n"

            summary += "\n"

        summary += "\n【分析请求】\n请对以上筛选结果进行深度分析，包括：\n"
        summary += "1. 优中选优：对这" + str(len(results)) + "支股票进行优先级排序\n"
        summary += "2. 风险评估：识别每支股票的主要风险点\n"
        summary += "3. 投资建议：针对不同投资者类型（激进型/稳健型）的配置建议\n"
        summary += "4. 时机分析：入场时机和持仓周期建议\n"
        summary += "5. 注意事项：需要特别关注的风险警示\n"
        summary += "\n请用中文详细分析，给出具体的投资优先级排序和建议持仓比例。"

        console.print("[cyan]正在等待AI响应，请稍候...[/cyan]")
        ai_analysis = _call_dashscope_api(
            system_prompt="你是一位专业的A股量化投资顾问，擅长量化筛选和技术分析。请基于用户提供的量化筛选结果，给出客观、专业的投资建议，用中文回复。",
            user_content=summary,
            max_tokens=3000,
            timeout=90
        )

        if ai_analysis.startswith('[yellow]提示'):
            console.print("\n[yellow]提示：请前往 https://dashscope.aliyun.com 注册获取免费API Key[/yellow]")
            console.print("[yellow]新用户赠送100万Token，支持qwen-max、qwen-plus等模型。[/yellow]\n")
            console.print("[cyan]【模拟AI分析结果】[/cyan]\n")
            console.print("基于量化筛选结果，以下是AI的深度分析建议：\n")
            console.print("【优先级排序】")
            for i, result in enumerate(results[:5], 1):
                console.print(f"  {i}. {result['stock_code']} {result['stock_name']} (得分: {result['comprehensive_score']:.1f})")
            console.print("\n【投资建议】")
            console.print("  - 激进型投资者：可重点关注排名前3的股票")
            console.print("  - 稳健型投资者：建议分散配置前5名股票")
            console.print("\n【风险提示】")
            console.print("  - 股市有风险，投资需谨慎")
            console.print("  - 建议结合市场整体环境综合判断")
            console.print("  - AI分析仅供参考，不构成投资建议")
            return

        if ai_analysis.startswith('[red]'):
            console.print(ai_analysis)
            return

        console.print("\n" + "=" * 70)
        console.print("[bold green]AI 深度分析结果[/bold green]")
        console.print("=" * 70)
        console.print(ai_analysis)
        console.print("=" * 70)

        try:
            save = input("\n> 是否保存AI分析报告到文件？(Y/N): ").strip().upper()
            if save == 'Y':
                report_file = f"筛选分析报告_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                with open(report_file, 'w', encoding='utf-8') as f:
                    f.write(f"股票智能筛选分析报告\n")
                    f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write("=" * 70 + "\n\n")
                    f.write(summary + "\n\n")
                    f.write("=" * 70 + "\n\n")
                    f.write("AI 深度分析结果:\n\n")
                    f.write(ai_analysis)
                console.print(f"[green]分析报告已保存到: {report_file}[/green]")
        except Exception as e:
            console.print(f"[yellow]保存报告失败: {e}[/yellow]")

    except Exception as e:
        console.print(f"[red]分析失败: {str(e)}[/red]")
