"""
A股数据查询工具 v3.0 — 模块化版本

功能特性
--------
1. 批量数据导出（13种数据类型）
2. 单股分析查询（L1/L2数据、技术指标）
3. 股票智能筛选系统（三种策略评分）
4. Excel/CSV 双格式导出
5. 自动重试机制（3级API Fallback）
6. 备用数据源切换（Akshare → Tushare → 东方财富）
7. 控制台模式运行，支持打包为EXE
8. AI分析功能集成（DashScope/通义千问，可选）

模块结构
--------
- config: 配置常量和全局变量
- logging_utils: 日志和显示工具
- utils: 工具函数
- cache: 缓存管理
- indicators: 技术指标计算
- data_fetcher: 数据获取层
- analysis: 单股分析
- market_data: 市场数据批量获取
- ai_analysis: AI分析
- export: 导出功能
- screeners: 筛选器（L1/L2/策略/智能筛选）
- display: 显示函数
- main: 入口函数

使用方法
--------
直接运行: python -m akshare_app
或: python Akshare.py（薄包装入口）
"""

# tqdm 兼容性修复（PyInstaller 打包环境）
try:
    import tqdm
    original_del = tqdm.tqdm.__del__
    def safe_del(self):
        try:
            original_del(self)
        except AttributeError:
            pass
    tqdm.tqdm.__del__ = safe_del
except Exception:
    pass

# 配置
from akshare_app.config import *

# 日志
from akshare_app.logging_utils import *

# 工具
from akshare_app.utils import *

# 缓存
from akshare_app.cache import *

# 技术指标
from akshare_app.indicators import *

# 数据获取
from akshare_app.data_fetcher import *

# 单股分析
from akshare_app.analysis import *

# 市场数据
from akshare_app.market_data import *

# AI分析
from akshare_app.ai_analysis import *

# 导出
from akshare_app.export import *

# 筛选器
from akshare_app.screeners import *

# 显示
from akshare_app.display import *

# 入口
from akshare_app.main import *
