# A股数据查询工具 v3.2

一款功能强大的A股数据查询与智能筛选工具，支持批量数据导出、单股深度分析、多策略智能筛选，打包为 Windows 独立可执行文件，无需安装 Python 环境。

[![Version](https://img.shields.io/badge/version-3.2-blue)](https://github.com/delamey/Akshare)
[![Python](https://img.shields.io/badge/python-3.11%2B-green)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%207%2F10%2F11-lightgrey)](https://github.com/delamey/Akshare)
[![License](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

---

## 目录

- [功能特性](#功能特性)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [使用示例](#使用示例)
- [功能说明](#功能说明)
- [常见问题 FAQ](#常见问题-faq)
- [技术栈](#技术栈)
- [环境变量](#环境变量)
- [打包说明](#打包说明)
- [Git LFS 配置](#git-lfs-配置大文件管理)
- [更新日志](#更新日志)

---

## 功能特性

| 模块 | 能力 |
|------|------|
| **批量数据导出** | 25种数据类型（实时行情、五档盘口、历史K线、资金流向、财务报表、分红送配等），Excel/CSV 双格式 |
| **单股深度分析** | L1/L2数据、MA/MACD/RSI/KDJ/布林带等技术指标、资金流向、财务报表一键查看 |
| **智能股票筛选** | 三种策略（短线强势 / 主力建仓 / 价值投资），自动评分排序推荐 |
| **高可用架构** | 4级API Fallback：腾讯K线 → 新浪财经 → Tushare → yfinance，自动重试 |
| **并发优化** | ThreadPoolExecutor 并行获取独立 API 数据，批量导出速度提升 3-4 倍 |
| **缓存预热** | 启动时后台预加载全市场行情数据，后续查询秒级响应；缓存大小上限 100MB，自动清理 |
| **AI分析集成** | 可选 DashScope / 通义千问 AI 深度分析报告（需自行配置 API Key） |
| **模块化架构** | 6300+ 行单文件重构为 18 个模块，职责清晰，易于维护和扩展 |
| **独立运行** | PyInstaller 打包为 58MB 独立 .exe，Windows 7/10/11 双击即用 |

---

## 项目结构

```
e:\pyDemo\
├── Akshare.py                  # 薄包装入口（向后兼容）
├── requirements.txt            # Python 依赖
├── Akshare.spec                # PyInstaller 打包配置
├── README.md                   # 项目说明
├── 使用说明.md                  # 用户使用手册
│
└── akshare_app/                # 核心包
    ├── __init__.py             # 包初始化 & 统一导出
    ├── config.py               # 配置常量、环境变量、全局资源
    ├── logging_utils.py        # 日志和 Rich 控制台工具
    ├── utils.py                # 通用工具函数
    ├── cache.py                # 缓存管理（预热/刷新/大小限制）
    ├── indicators.py           # 技术指标计算（RSI/MACD/KDJ/布林带）
    ├── data_fetcher.py         # 数据获取层（L1/L2/历史K线/财务）
    ├── analysis.py             # 单股分析逻辑
    ├── market_data.py          # 市场数据批量获取（并发优化）
    ├── ai_analysis.py          # AI 分析（DashScope/通义千问）
    ├── export.py               # Excel/CSV 导出
    ├── display.py              # Rich 表格显示
    ├── main.py                 # 主菜单 & 程序入口
    │
    └── screeners/              # 筛选器子包
        ├── __init__.py         # 筛选器统一导出
        ├── l1_screener.py      # L1 行情数据筛选
        ├── l2_screener.py      # L2 盘口数据筛选
        ├── strategies.py       # 三种策略评分引擎
        └── stock_screener.py   # 智能筛选调度器
```

### 模块职责说明

| 模块 | 职责 | 关键类/函数 |
|------|------|------------|
| `config.py` | 集中管理所有配置常量、环境变量、全局共享资源 | `TUSHARE_TOKEN`, `console`, `_HTTP_SESSION` |
| `logging_utils.py` | 日志输出、Rich 控制台安全打印 | `log_info()`, `log_warning()`, `_safe_console_print()` |
| `utils.py` | 通用工具函数（字典构造、股票查找等） | `make_l1_dict()`, `find_stock_in_df()` |
| `cache.py` | 缓存管理、预热、自动刷新、大小限制 | `warmup_market_cache()`, `_check_cache_size_limit()` |
| `indicators.py` | 技术指标计算（纯函数，无副作用） | `calculate_rsi()`, `calculate_macd()`, `TechnicalIndicators` |
| `data_fetcher.py` | 数据获取层，支持多源 Fallback | `DataFetcher`, `get_stock_name()` |
| `analysis.py` | 单股分析逻辑，整合 L1/L2/技术指标 | `get_single_stock_analysis()`, `display_stock_analysis()` |
| `market_data.py` | 批量市场数据获取，ThreadPoolExecutor 并发 | `get_stock_data()` |
| `ai_analysis.py` | DashScope AI 分析集成 | `analyze_single_stock_with_deepseek()` |
| `export.py` | Excel/CSV 双格式导出 | `export_to_excel()`, `export_strategy_scores_to_csv()` |
| `display.py` | Rich 表格展示筛选结果 | `display_stock_result()`, `display_recommendations()` |
| `main.py` | 主菜单循环、错误处理入口 | `main()`, `main_with_error_handling()` |
| `screeners/` | 股票筛选子包 | `StockScreener`, `L1Screener`, `L2Screener`, `ScreeningStrategies` |

---

## 快速开始

### 方式一：运行 Python 脚本

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动程序（两种方式均可）
python Akshare.py          # 薄包装入口
python -m akshare_app      # 包模块入口
```

### 方式二：直接使用打包好的 EXE（推荐）

1. 从 [Releases](https://github.com/delamey/Akshare) 页面下载 `Akshare.exe`
2. 双击 `Akshare.exe` 即可运行

> **系统要求**：Windows 7/10/11，无需安装 Python 环境，建议 2GB 以上内存

### 方式三：作为包导入使用

```python
from akshare_app import get_stock_data, StockScreener, console

# 批量获取市场数据
data = get_stock_data(['market', 'fund_flow', 'limit_up'])

# 单股筛选
screener = StockScreener()
result = screener.screen_single_stock('600519', strategy='all')
```

---

## 使用示例

### 示例1：单股筛选分析

```
+----------------------+
| A股数据查询工具 v3.2 |
+-- 基于 Akshare.py ---+

请选择操作模式：
+----------------------------------------------------+
| 序号 | 模式         | 说明                         |
|------+--------------+------------------------------|
|  1   | 批量数据导出 | 导出批量股票数据到Excel      |
|  2   | 单股分析查询 | 输入股票代码获取详细技术分析  |
|  3   | 股票智能筛选 | 基于L1/L2数据的智能股票筛选  |
|  q   | 退出         | 退出程序                     |
+----------------------------------------------------+

> 请输入模式序号（1/2/3/q）: 3
```

```
请输入选项: 1

> 请输入股票代码（如 688275）: 688275
> 请选择筛选策略:
  [1] 短线强势股策略
  [2] 主力建仓股策略
  [3] 价值投资股策略
  [all] 全部策略

> 请输入策略选项: all

  -> [688275] 实时行情获取: 2.156s (缓存命中)
  -> [688275] 历史K线获取: 0.812s (快速路径, 20条)

  ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
  ┃  股票代码: 688275               ┃
  ┃  股票名称: XX科技                ┃
  ┃  综合得分: 78.5 分              ┃
  ┃                                ┃
  ┃  [短线强势股] 得分: 65.0        ┃
  ┃  [主力建仓股] 得分: 82.0        ┃
  ┃  [价值投资股] 得分: 88.5        ┃
  ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

### 示例2：批量智能筛选 Top 5

```
请输入选项: 2

> 请选择筛选策略:
  [1] 短线强势股  [2] 主力建仓股  [3] 价值投资股  [all] 全部

> 请输入策略选项: all

  -> 缓存预热完成: 3.2s (5264支股票)
  筛选股票... ━━━━━━━━━━━━━━━━━━━━━━━━━━ 100%

  -> 批量筛选完成: 输入=20支 有效=20支 耗时=25.8s

  ┌─────────────────────────────────────────────┐
  │  推荐股票 Top 5                            │
  ├──────┬──────────┬────────┬──────────────────┤
  │ 排名 │ 代码     │ 名称   │ 综合得分         │
  ├──────┼──────────┼────────┼──────────────────┤
  │  1   │ 600519   │ 贵州茅台│ 92.3            │
  │  2   │ 000858   │ 五粮液  │ 88.7            │
  │  3   │ 300750   │ 宁德时代│ 85.2            │
  │  4   │ 002594   │ 比亚迪  │ 82.1            │
  │  5   │ 601318   │ 中国平安│ 78.9            │
  └──────┴──────────┴────────┴──────────────────┘
```

---

## 功能说明

### 1. 批量数据导出

支持 25 种数据类型一键导出到 Excel，并发获取提升速度：

| 数据类型 | 说明 |
|----------|------|
| 实时行情 (L1) | 最新价、涨跌幅、成交量、换手率等 |
| 五档盘口 (L2) | 买一~买五、卖一~卖五的价格和挂单量 |
| 历史K线 | 日K线数据，含 OHLCV |
| 资金流向 | 主力/超大单/大单/中单/小单净流入 |
| 财务报表 | 资产负债表、利润表、现金流量表 |
| 分红送配 | 分红记录、送股、转增 |
| 行业板块 | 行业分类、板块信息 |
| 概念板块 | 概念题材分类 |
| 龙虎榜 | 龙虎榜及详情 |
| 大宗交易 | 大宗交易记录 |
| 融资融券 | 两融数据 |
| 北向资金 | 沪深港通持股 |
| 宏观数据 | GDP/CPI/PPI/货币供应量/汇率/国债收益率等 |

### 2. 单股分析查询

输入股票代码即可获取全方位的技术分析：

- 实时行情与最新价格
- 历史K线走势与技术指标（MA5/MA10/MA20、MACD、RSI、KDJ、布林带、动量等）
- L2 五档盘口深度数据
- 资金流向（主力/散户）
- 财务报表摘要
- 分红送配历史
- 可选 AI 智能分析报告

### 3. 股票智能筛选

基于三种策略自动评估并推荐最佳标的：

| 策略 | 适合周期 | 核心指标 |
|------|----------|----------|
| **短线强势股** | 1-5天 | 均线金叉、放量突破、RSI强势区间、DDX大单动向 |
| **主力建仓股** | 1-4周 | 资金持续流入、底部放量、MACD金叉、DDX/DDY持续为正 |
| **价值投资股** | 3个月以上 | 低PE/PB、高ROE、稳定分红、MACD零轴上方 |

---

## 常见问题 FAQ

### Q: 杀毒软件提示有风险？

**A**: 由于程序使用 PyInstaller 打包，部分杀毒软件可能误报。请选择"允许运行"或"更多信息" → "仍要运行"。本程序不含任何恶意代码。

### Q: 程序启动很慢？

**A**: 首次启动需要解压依赖文件并预热行情缓存，约 5-15 秒。后续启动会更快。

### Q: 获取数据失败 / 显示"ConnectionError"？

**A**: 请依次排查：
1. 检查网络连接是否正常
2. 确认股票代码为 6 位纯数字（如 `688275`，不是 `68827`）
3. 等待 1-2 分钟后重试（API 可能限流）
4. 程序内置 4 级 Fallback，会自动切换数据源
5. 缓存失败后有 30 秒冷却期，期间使用上次缓存数据

### Q: Excel 文件保存在哪里？

**A**: 导出的 Excel 文件保存在 `Akshare.exe` 同级目录下，文件名包含时间和股票代码，如 `688275_分析报告.xlsx`。

### Q: 能在 Mac 或 Linux 上运行吗？

**A**: 当前发布的 `Akshare.exe` 仅支持 Windows。如需在其他平台运行，请使用方式一（Python 脚本）。

### Q: 如何配置 AI 分析功能？

**A**: 访问 [DashScope 官网](https://dashscope.aliyun.com) 注册获取免费 API Key，然后设置环境变量 `DASHSCOPE_API_KEY`。

### Q: 为什么输入 5 位代码会被拒绝？

**A**: 系统要求输入完整的 6 位股票代码（如 `688275`）。输入 5 位代码（如 `68827`）会被自动拦截并提示重新输入，避免无效的 API 请求。

### Q: 程序会收集我的数据吗？

**A**: 不会。程序仅在本地运行，只访问公开的股票数据接口，不收集或上传任何个人信息。

### Q: 如何修改缓存大小或 TTL？

**A**: 编辑 `akshare_app/config.py` 中的配置常量：
- `_CACHE_TTL`：缓存有效期（秒），默认 300
- `_CACHE_MAX_SIZE_MB`：最大缓存大小（MB），默认 100
- `_CACHE_FAIL_COOLDOWN`：缓存失败冷却时间（秒），默认 30

---

## 技术栈

| 类别 | 技术 | 版本 |
|------|------|------|
| 数据源 | akshare, tushare, yfinance | ≥1.18 / ≥1.4 / ≥1.4 |
| 数据处理 | pandas, numpy | ≥2.0 |
| 控制台UI | rich | ≥13.0 |
| HTTP | requests, curl_cffi | ≥2.31 |
| Excel | openpyxl, xlrd | ≥3.1 |
| 并发 | concurrent.futures (ThreadPoolExecutor) | 内置 |
| 打包 | PyInstaller | ≥6.0 |

---

## 环境变量

| 变量名 | 说明 | 必填 | 获取方式 |
|--------|------|------|----------|
| `TUSHARE_TOKEN` | Tushare API Token | 否 | [tushare.pro](https://tushare.pro) 注册获取 |
| `DASHSCOPE_API_KEY` | DashScope AI 分析 Key | 否 | [dashscope.aliyun.com](https://dashscope.aliyun.com) 注册获取 |

---

## 打包说明

```bash
# 安装 PyInstaller
pip install pyinstaller

# 使用 spec 文件打包
pyinstaller Akshare.spec

# 生成的 exe 位于 dist/Akshare.exe（约 58MB）
```

---

## Git LFS 配置（大文件管理）

由于 `dist/Akshare.exe` 超过 50MB，推荐使用 Git LFS（Large File Storage）管理大文件。

### 安装 Git LFS

```bash
# Windows（下载安装器: https://git-lfs.com）
winget install Git.LFS

# macOS
brew install git-lfs

# Linux
sudo apt install git-lfs
```

### 配置仓库

```bash
git lfs install
git lfs track "dist/*.exe"
git add .gitattributes
git commit -m "chore: 配置 Git LFS 追踪大文件"
git add dist/Akshare.exe
git commit -m "chore: 使用 LFS 上传可执行文件"
git push
```

> **注意**：如果已用普通方式推送过 `Akshare.exe`，需先迁移：
> ```bash
> git lfs migrate import --include="dist/*.exe" --everything
> git push --force
> ```

---

## 更新日志

### v3.2 (2026-06-01)

- 🏗️ **模块化重构**：6300+ 行单文件拆分为 18 个模块，职责清晰
  - `config.py` — 集中配置常量和全局资源
  - `cache.py` — 缓存管理（预热/刷新/大小限制）
  - `indicators.py` — 技术指标计算（纯函数，无副作用）
  - `data_fetcher.py` — 数据获取层，多源 Fallback
  - `market_data.py` — 批量市场数据并发获取
  - `screeners/` — 筛选器子包（L1/L2/策略/智能筛选）
- 🚀 **并发优化**：ThreadPoolExecutor 并行获取 13 种独立 API 数据，批量导出速度提升 3-4 倍
- 🧠 **内存优化**：缓存大小上限 100MB，LRU 式自动清理防止内存泄漏
- 🔒 **安全修复**：API Key 改为环境变量读取，不再硬编码
- 🔧 **错误处理标准化**：`_safe_console_print` + 上下文管理器统一错误处理
- 📐 **技术指标中心化**：RSI/MACD/KDJ/布林带计算集中在 `indicators.py`，消除副作用
- 🐛 修复函数名拼写错误 `expo10rt_strategy_scores_to_csv`
- 🐛 修复缓存大小检查函数未调用的问题
- 🐛 修复 `TechnicalIndicators` 隐式修改输入 DataFrame 的副作用

### v3.1 (2026-05-31)

- 🚀 策略 all 模式一次性加载数据，避免重复加载，性能提升 74.8%
- 🚀 新增名称映射字典缓存，名称查询从 O(n) 降至 O(1)
- 🚀 候选股数量上限 200 支 + 随机采样，防止全市场遍历
- 🚀 早停阈值按策略自适应（策略1→60 / 策略2→55 / 策略3→50）
- 🚀 缓存失败 30 秒冷却机制，防止重试风暴
- 🔧 新增 _make_detail 评分明细工厂函数，代码量减少 40%
- 🔧 L2 指标值提取为局部变量，减少重复字典查找
- 🔧 TechnicalIndicators rolling 计算添加 min_periods=1，消除 NaN 传播
- 🔧 L1Screener 缓存 _latest_row/_prev_row，避免重复 iloc 查询

### v3.0 (2026-05-31)

- ✨ 新增股票代码 6 位数字校验，拦截无效输入
- 🔧 优化缓存预热日志，仅输出一行摘要，避免干扰用户交互
- 🔧 预热线程异常时自动检测并重启恢复
- 🔧 `screen_batch()` 空列表和无效代码自动过滤
- 📦 完善 PyInstaller spec，补全 yfinance/colorama/cffi 等依赖
- 🚀 优化 `get_top_stocks()` 算法，减少数据请求量
- 🚀 4 级 API Fallback 架构（腾讯→新浪→Tushare→yfinance）
- 📊 完善筛选策略测试覆盖

### v2.0 (2026-05)

- ✨ 新增三种股票智能筛选策略
- ✨ 新增缓存预热机制，提升查询响应速度
- 🔧 修复 RemoteDisconnected 错误
- 📊 新增详细耗时日志输出

### v1.0 (2026-04)

- 🎉 初始版本，支持批量数据导出和单股分析

---

## 许可证

MIT License
