# A股数据查询工具 v3.0

一款功能强大的A股数据查询与智能筛选工具，支持批量数据导出、单股深度分析、以及多策略智能筛选。

## 功能特性

- **批量数据导出** — 支持13种数据类型（实时行情、五档盘口、历史K线、资金流向、财务报表等）
- **单股深度分析** — L1/L2数据、技术指标、资金流向全面分析
- **智能股票筛选** — 三种策略评分（短线强势、主力建仓、价值投资），自动推荐最佳标的
- **多格式导出** — Excel/CSV 双格式导出
- **高可用架构** — 4级API Fallback（腾讯→新浪→Tushare→yfinance），自动重试
- **AI分析集成** — 可选DashScope/通义千问 AI智能分析
- **独立运行** — 支持打包为Windows独立可执行文件，无需安装Python

## 快速开始

### 方式一：直接运行Python脚本

```bash
pip install -r requirements.txt
python Akshare.py
```

### 方式二：使用打包好的EXE

1. 下载 `dist/Akshare.exe`
2. 双击运行即可

> **系统要求**: Windows 7/10/11，无需安装Python环境

## 功能说明

### 1. 批量数据导出

导出多只股票的多种数据到Excel文件，支持的数据类型包括：
- 实时行情（L1）、五档盘口（L2）、历史K线
- 资金流向、财务报表、分红送配
- 行业数据、基础信息

### 2. 单股分析查询

深入分析单只股票的详细数据，包括：
- 实时行情和最新价格
- 历史K线和技术指标（MA、MACD、RSI、动量等）
- L2五档盘口、资金流向
- 财务报表、分红送配记录
- 可选AI智能分析

### 3. 股票智能筛选

基于三种策略自动筛选符合条件的股票：
- **短线强势股** — 适合短期交易（均线金叉、放量突破、RSI强势）
- **主力建仓股** — 适合中期布局（资金流入、底部放量、估值合理）
- **价值投资股** — 适合长期持有（低估值、高分红、稳定增长）

## 技术栈

- **数据源**: akshare, tushare, yfinance
- **数据处理**: pandas, numpy
- **控制台界面**: rich
- **HTTP请求**: requests, curl_cffi
- **Excel导出**: openpyxl
- **打包工具**: PyInstaller

## 环境变量

| 变量名 | 说明 | 必填 |
|--------|------|------|
| `TUSHARE_TOKEN` | Tushare API Token | 否 |
| `DASHSCOPE_API_KEY` | DashScope AI分析API Key | 否 |

## 打包说明

```bash
# 安装PyInstaller
pip install pyinstaller

# 使用spec文件打包
pyinstaller Akshare.spec

# 生成的exe位于 dist/Akshare.exe
```

## 许可证

MIT License