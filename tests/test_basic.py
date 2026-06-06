"""akshare_app 基础单元测试"""
import pytest
from unittest.mock import patch, MagicMock


class TestConfig:
    """配置模块测试"""

    def test_import_config(self):
        from akshare_app.config import console, TUSHARE_TOKEN
        assert console is not None

    def test_tushare_token_default_empty(self):
        from akshare_app.config import TUSHARE_TOKEN
        # 未设置环境变量时应为空字符串
        assert isinstance(TUSHARE_TOKEN, str)

    def test_cache_config_constants(self):
        from akshare_app.config import _CACHE_TTL, _CACHE_MAX_SIZE_MB, _CACHE_FAIL_COOLDOWN
        assert _CACHE_TTL > 0
        assert _CACHE_MAX_SIZE_MB > 0
        assert _CACHE_FAIL_COOLDOWN > 0


class TestUtils:
    """工具函数测试"""

    def test_make_l1_dict(self):
        from akshare_app.utils import make_l1_dict
        result = make_l1_dict(
            code="600000",
            name="测试股票",
            price="10.50",
            change_pct="2.5",
            volume="100000",
            amount="500000",
            high="11.00",
            low="10.00",
            open_price="10.20",
            pre_close="10.25",
        )
        assert result["股票代码"] == "600000"
        assert result["股票名称"] == "测试股票"
        assert result["最新价"] == "10.50"

    def test_find_stock_in_df(self):
        import pandas as pd
        from akshare_app.utils import find_stock_in_df
        df = pd.DataFrame({"代码": ["600000", "000001"], "名称": ["浦发银行", "平安银行"]})
        result = find_stock_in_df(df, "600000", col="代码")
        assert not result.empty
        assert result.iloc[0]["名称"] == "浦发银行"

    def test_find_stock_in_df_not_found(self):
        import pandas as pd
        from akshare_app.utils import find_stock_in_df
        df = pd.DataFrame({"代码": ["600000"], "名称": ["浦发银行"]})
        result = find_stock_in_df(df, "999999", col="代码")
        assert result.empty


class TestIndicators:
    """技术指标计算测试"""

    def test_calculate_rsi(self):
        import pandas as pd
        import numpy as np
        from akshare_app.indicators import calculate_rsi
        close = pd.Series(np.random.uniform(10, 20, 50))
        rsi = calculate_rsi(close, period=14)
        assert len(rsi) == len(close)
        # RSI 值应在 0-100 之间（非 NaN 部分）
        valid_rsi = rsi.dropna()
        assert (valid_rsi >= 0).all() and (valid_rsi <= 100).all()

    def test_calculate_macd(self):
        import pandas as pd
        import numpy as np
        from akshare_app.indicators import calculate_macd
        close = pd.Series(np.random.uniform(10, 20, 60))
        dif, dea, hist = calculate_macd(close)
        assert len(dif) == len(close)
        assert len(dea) == len(close)
        assert len(hist) == len(close)


class TestLoggingUtils:
    """日志工具测试"""

    def test_log_info(self):
        from akshare_app.logging_utils import log_info
        # 不应抛出异常
        log_info("测试信息")

    def test_log_warning(self):
        from akshare_app.logging_utils import log_warning
        log_warning("测试警告")

    def test_log_error(self):
        from akshare_app.logging_utils import log_error
        log_error("测试错误")

    def test_log_success(self):
        from akshare_app.logging_utils import log_success
        log_success("测试成功")


class TestCache:
    """缓存模块测试"""

    def test_cache_import(self):
        from akshare_app.cache import warmup_market_cache, _get_cached_market_data
        assert callable(warmup_market_cache)
        assert callable(_get_cached_market_data)


class TestScreeners:
    """筛选器模块测试"""

    def test_l1_screener_import(self):
        from akshare_app.screeners.l1_screener import L1Screener
        screener = L1Screener("600000")
        assert screener is not None

    def test_l2_screener_import(self):
        from akshare_app.screeners.l2_screener import L2Screener
        screener = L2Screener("600000")
        assert screener is not None

    def test_strategies_import(self):
        from akshare_app.screeners.strategies import ScreeningStrategies
        strategies = ScreeningStrategies("600000")
        assert strategies is not None

    def test_stock_screener_import(self):
        from akshare_app.screeners.stock_screener import StockScreener
        screener = StockScreener()
        assert screener is not None

    def test_strategies_have_expected_methods(self):
        from akshare_app.screeners.strategies import ScreeningStrategies
        strategies = ScreeningStrategies("600000")
        assert hasattr(strategies, 'strategy_short_term_strong')
        assert hasattr(strategies, 'strategy_main_accumulation')
        assert hasattr(strategies, 'strategy_value_stocks')


class TestExport:
    """导出模块测试"""

    def test_export_import(self):
        from akshare_app.export import export_to_excel, export_strategy_scores_to_csv
        assert callable(export_to_excel)
        assert callable(export_strategy_scores_to_csv)


class TestDisplay:
    """显示模块测试"""

    def test_display_import(self):
        from akshare_app.display import display_stock_result, display_recommendations
        assert callable(display_stock_result)
        assert callable(display_recommendations)


class TestAnalysis:
    """分析模块测试"""

    def test_analysis_import(self):
        from akshare_app.analysis import get_single_stock_analysis, display_stock_analysis
        assert callable(get_single_stock_analysis)
        assert callable(display_stock_analysis)


class TestAIAnalysis:
    """AI分析模块测试"""

    def test_ai_analysis_import(self):
        from akshare_app.ai_analysis import analyze_single_stock_with_deepseek
        assert callable(analyze_single_stock_with_deepseek)


class TestDataFetcher:
    """数据获取模块测试"""

    def test_data_fetcher_import(self):
        from akshare_app.data_fetcher import DataFetcher, get_stock_name
        assert DataFetcher is not None
        assert callable(get_stock_name)

    def test_data_fetcher_instantiation(self):
        from akshare_app.data_fetcher import DataFetcher
        fetcher = DataFetcher()
        assert fetcher is not None


class TestMarketData:
    """市场数据模块测试"""

    def test_market_data_import(self):
        from akshare_app.market_data import get_stock_data
        assert callable(get_stock_data)
