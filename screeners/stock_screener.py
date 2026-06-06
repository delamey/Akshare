"""
股票智能筛选器

整合 L1/L2 筛选和策略评分，提供单股和批量筛选功能。
"""

import time
import threading
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import akshare as ak
import tushare as ts
import pandas as pd

from akshare_app.config import (
    console, MAX_WORKERS_SHORT, MAX_WORKERS_DEFAULT,
    STRATEGY_THRESHOLD_SHORT, STRATEGY_THRESHOLD_MAIN, STRATEGY_THRESHOLD_VALUE,
    TUSHARE_TOKEN, _stock_name_map, _market_cache_lock, _market_cache,
)
from akshare_app.logging_utils import log_info, log_success, log_warning, log_error
from akshare_app.cache import _get_cached_market_data, warmup_market_cache
from akshare_app.data_fetcher import DataFetcher
from akshare_app.screeners.l1_screener import L1Screener
from akshare_app.screeners.l2_screener import L2Screener
from akshare_app.screeners.strategies import ScreeningStrategies
from akshare_app.utils import clean_stock_code
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn


class StockScreener:
    """
    股票智能筛选引擎

    该类实现了基于L1和L2数据的多策略股票筛选功能，支持：
    1. 单股分析筛选
    2. 批量股票筛选
    3. 三种筛选策略：
       - 短线强势股策略
       - 主力建仓股策略
       - 价值投资股策略
    4. 自定义推荐数量
    5. 多级 API fallback 机制
    """

    def __init__(self, max_recommendations: int = 5):
        """
        初始化股票筛选器

        Args:
            max_recommendations: 最大推荐股票数量，默认 5 支
        """
        self.max_recommendations = max_recommendations

    def screen_single_stock(self, stock_code: str, strategy: str = 'all',
                                stock_name: Optional[str] = None) -> Dict[str, Any]:
        """
        对单支股票进行筛选分析

        Args:
            stock_code: 股票代码
            strategy: 筛选策略，可选值: '1'(短线强势), '2'(主力建仓), '3'(价值投资), 'all'(全部)
            stock_name: 股票名称，如已预取可传入避免重复查询缓存

        Returns:
            包含筛选结果的字典，结构为:
            {
                'stock_code': 股票代码,
                'stock_name': 股票名称,
                'strategies': {
                    '策略名1': {'passed': bool, 'score': float, ...},
                    '策略名2': ...
                },
                'comprehensive_score': 综合得分,
                'passed': 是否通过筛选
            }

        Example:
            >>> screener = StockScreener()
            >>> result = screener.screen_single_stock('688275', strategy='1')
            >>> if result['passed']:
            ...     print(f"{result['stock_name']} 通过筛选")
        """

        if not isinstance(stock_code, str) or len(stock_code) != 6 or not stock_code.isdigit():
            log_warning(f"无效股票代码: {stock_code}，已跳过")
            return {
                'stock_code': str(stock_code),
                'stock_name': '无效代码',
                'strategies': {},
                'comprehensive_score': 0,
                'passed': False
            }

        _ss_t0 = time.perf_counter()

        screener = ScreeningStrategies(stock_code)

        _ss_n0 = time.perf_counter()
        if stock_name is None:
            stock_name = self._get_stock_name(stock_code)
        _ss_n1 = time.perf_counter()

        results = {
            'stock_code': stock_code,
            'stock_name': stock_name,
            'strategies': {}
        }

        if strategy != 'all':
            if strategy == '1':
                results['strategies']['短线强势股'] = screener.strategy_short_term_strong()
            elif strategy == '2':
                results['strategies']['主力建仓股'] = screener.strategy_main_accumulation()
            elif strategy == '3':
                results['strategies']['价值投资股'] = screener.strategy_value_stocks()
        else:
            if not screener.load_all_data('all'):
                results['strategies'] = {
                    '短线强势股': {'score': 0, 'max_score': 100, 'details': [], 'error': '数据加载失败'},
                    '主力建仓股': {'score': 0, 'max_score': 100, 'details': [], 'error': '数据加载失败'},
                    '价值投资股': {'score': 0, 'max_score': 100, 'details': [], 'error': '数据加载失败'},
                }
            else:
                s1 = screener.strategy_short_term_strong()
                results['strategies']['短线强势股'] = s1

                s2 = screener.strategy_main_accumulation()
                results['strategies']['主力建仓股'] = s2

                if s1.get('score', 0) < 30 and s2.get('score', 0) < 30:
                    results['strategies']['价值投资股'] = {
                        'score': 0, 'max_score': 100, 'details': [],
                        'skip_reason': '短线/中线得分过低，跳过深度分析'
                    }
                else:
                    results['strategies']['价值投资股'] = screener.strategy_value_stocks()

        total_score = 0
        strategy_count = 0
        for s_name, s_result in results['strategies'].items():
            total_score += s_result.get('score', 0)
            strategy_count += 1

        results['comprehensive_score'] = total_score / max(strategy_count, 1)

        _ss_total = time.perf_counter()
        log_info(
            f"[{stock_code}] 单股筛选完成: "
            f"名称查询={_ss_n1 - _ss_n0:.3f}s "
            f"策略执行={_ss_total - _ss_n1:.3f}s "
            f"总计={_ss_total - _ss_t0:.3f}s "
            f"综合得分={results['comprehensive_score']:.1f}"
        )

        return results

    def _get_stock_name(self, stock_code: str) -> str:
        """内部方法：获取股票名称（优先从名称映射字典读取，O(1)查询）"""
        try:
            name = _stock_name_map.get(stock_code)
            if name:
                return name
            with _market_cache_lock:
                cached = _market_cache.get("em")
            if cached is not None:
                df, _ = cached
                if df is not None and not df.empty:
                    code_col = '代码' if '代码' in df.columns else None
                    name_col = '名称' if '名称' in df.columns else None
                    if code_col and name_col:
                        row = df.loc[df[code_col] == stock_code, name_col]
                        if not row.empty:
                            return str(row.iloc[0])
        except Exception:
            pass
        return stock_code

    def screen_batch(self, stock_codes: List[str], strategy: str = 'all') -> List[Dict[str, Any]]:
        """批量筛选股票，返回推荐列表

        优化策略：
        1. 并发筛选（ThreadPoolExecutor），默认3线程
        2. 早停机制：已找到足够高分候选时提前终止
        3. 限制最大并发数避免API限流

        Args:
            stock_codes: 要筛选的股票代码列表
            strategy: 筛选策略，可选值: '1', '2', '3', 'all'

        Returns:
            排序后的推荐股票列表（按综合得分从高到低），数量不超过 max_recommendations
        """
        _sb_t0 = time.perf_counter()
        if not stock_codes:
            log_info("批量筛选完成: 输入=0支 有效=0支 耗时=0.000s")
            return []

        valid_codes = [c for c in stock_codes if isinstance(c, str) and len(c) == 6 and c.isdigit()]
        skipped = len(stock_codes) - len(valid_codes)
        if skipped > 0:
            log_warning(f"批量筛选中跳过 {skipped} 个无效代码")

        if not valid_codes:
            log_info("批量筛选完成: 输入={}支 有效=0支 耗时=0.000s".format(len(stock_codes)))
            return []

        _sb_name0 = time.perf_counter()
        name_map: Dict[str, str] = {}
        try:
            df = _get_cached_market_data("em")
            if df is not None and not df.empty:
                code_col = '代码' if '代码' in df.columns else None
                name_col = '名称' if '名称' in df.columns else None
                if code_col and name_col:
                    name_map = dict(zip(df[code_col].astype(str), df[name_col].astype(str)))
        except Exception:
            pass
        _sb_name1 = time.perf_counter()
        if name_map:
            log_info(f"名称映射预取完成: {len(name_map)}条 ({_sb_name1 - _sb_name0:.3f}s)")

        results: List[Dict[str, Any]] = []
        lock = threading.Lock()
        early_stop = threading.Event()
        strategy_thresholds = {
            '1': STRATEGY_THRESHOLD_SHORT,
            '2': STRATEGY_THRESHOLD_MAIN,
            '3': STRATEGY_THRESHOLD_VALUE,
            'all': STRATEGY_THRESHOLD_MAIN,
        }
        min_score_threshold = strategy_thresholds.get(strategy, 55)

        def process_single_stock(code: str) -> Optional[Dict[str, Any]]:
            try:
                return self.screen_single_stock(code, strategy, stock_name=name_map.get(code))
            except Exception as e:
                log_warning(f"筛选 {code} 时出错: {str(e)[:40]}")
                return None

        def worker(code: str) -> Optional[Dict[str, Any]]:
            if early_stop.is_set():
                return None
            result = process_single_stock(code)
            if result:
                with lock:
                    results.append(result)
                    if (len(results) >= self.max_recommendations
                            and all(r['comprehensive_score'] >= min_score_threshold for r in results[-self.max_recommendations:])):
                        early_stop.set()
            return result

        base_workers = 5 if strategy in ('1', '2') else 3
        max_workers = min(base_workers, len(valid_codes))

        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                console=console
            ) as progress:
                task = progress.add_task("筛选股票...", total=len(valid_codes))
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {executor.submit(worker, code): code for code in valid_codes}
                    completed = 0
                    for future in as_completed(futures):
                        completed += 1
                        if early_stop.is_set():
                            progress.update(task, completed=len(valid_codes))
                            break
                        try:
                            progress.advance(task)
                        except Exception:
                            pass
        except Exception:
            try:
                print(f"正在筛选 {len(valid_codes)} 支股票（并发模式）...")
            except Exception:
                pass
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(worker, code): code for code in valid_codes}
                for future in as_completed(futures):
                    if early_stop.is_set():
                        break
                    try:
                        future.result()
                    except Exception:
                        pass

        results.sort(key=lambda x: x['comprehensive_score'], reverse=True)
        _sb_total = time.perf_counter()
        log_info(
            f"批量筛选完成: "
            f"输入={len(stock_codes)}支 "
            f"有效={len(results)}支 "
            f"早停={'是' if early_stop.is_set() else '否'} "
            f"耗时={_sb_total - _sb_t0:.3f}s"
        )
        return results[:self.max_recommendations]

    def _safe_fetch_stock_list(self, api_func, api_name: str, max_count: int = 10) -> Optional[List[str]]:
        """
        内部方法：安全获取股票列表，包含响应验证和错误处理

        Args:
            api_func: API 函数对象
            api_name: API 名称（用于日志）
            max_count: 最大获取股票数量，默认 10 支

        Returns:
            股票代码列表或 None
        """
        try:
            df = api_func()

            if df is None:
                log_warning(f"{api_name} 返回 None", prefix="")
                return None

            if df.empty:
                log_warning(f"{api_name} 返回空数据", prefix="")
                return None

            required_columns = ['代码', '名称']
            for col in required_columns:
                if col not in df.columns:
                    log_warning(f"{api_name} 缺少必要列: {col}", prefix="")
                    return None

            df = df[~df['名称'].str.contains('ST|退市', na=False)]

            if len(df) > max_count:
                df = df.sample(n=max_count, random_state=42)

            stock_codes = df['代码'].tolist()
            log_success(f"{api_name} 成功获取 {len(stock_codes)} 支股票", prefix="")
            return stock_codes

        except ValueError as e:
            error_str = str(e)
            if '<' in error_str or 'decode' in error_str.lower():
                log_warning(f"{api_name} 返回 HTML 而非 JSON（可能被拦截）", prefix="")
            else:
                log_warning(f"{api_name} 数据解析失败: {error_str[:40]}", prefix="")
            return None
        except Exception as e:
            error_msg = str(e)
            if 'Connection aborted' in error_msg or 'RemoteDisconnected' in error_msg:
                log_warning(f"{api_name} 网络连接中断（请检查网络）", prefix="")
            elif 'decode' in error_msg.lower() and '<' in error_msg:
                log_warning(f"{api_name} 返回 HTML 而非 JSON（可能被拦截）", prefix="")
            else:
                log_warning(f"{api_name} 调用失败: {error_msg[:50]}", prefix="")
            return None

    def get_top_stocks(self, strategy: str = 'all') -> List[Dict[str, Any]]:
        """获取热门股票列表并进行筛选推荐

        优化策略：
        1. 优先复用缓存的全市场行情数据（避免重复API调用）
        2. 基于实时行情预过滤无效股票（ST/停牌/涨跌停）
        3. 按策略特征排序候选股（提高命中率）
        4. 兜底数据源自动采样限制总数，防止全市场遍历耗时过长

        Args:
            strategy: 筛选策略，可选值: '1', '2', '3', 'all'

        Returns:
            排序后的推荐股票列表
        """
        _MAX_BATCH = 200
        _FALLBACK_SAMPLE = 100

        log_info("正在获取股票列表...", prefix="")
        _gt_t0 = time.perf_counter()

        market_df = _get_cached_market_data("em")
        _gt_list_t = time.perf_counter()
        log_info(f"获取股票列表耗时: {_gt_list_t - _gt_t0:.3f}s", prefix="")
        if market_df is not None and not market_df.empty:
            stock_codes = self._prefilter_from_market_data(market_df, strategy)
            if stock_codes:
                log_success(f"从缓存行情数据中筛选出 {len(stock_codes)} 支候选股", prefix="")
                result = self.screen_batch(stock_codes, strategy)
                _gt_total = time.perf_counter()
                log_info(f"get_top_stocks 完成: 列表获取={_gt_list_t - _gt_t0:.3f}s 批量筛选={_gt_total - _gt_list_t:.3f}s 总计={_gt_total - _gt_t0:.3f}s", prefix="")
                return result

        try:
            df = ak.stock_info_a_code_name()
            if df is not None and not df.empty:
                df = df[~df['name'].str.contains('ST|退市', na=False)]
                stock_codes = df['code'].tolist()
                if len(stock_codes) > _MAX_BATCH:
                    log_warning(f"候选股过多({len(stock_codes)}支)，随机采样至{_MAX_BATCH}支", prefix="")
                    import random
                    random.seed(42)
                    stock_codes = random.sample(stock_codes, _MAX_BATCH)
                log_success(f"Akshare(stock_info) 成功获取 {len(stock_codes)} 支股票", prefix="")
                result = self.screen_batch(stock_codes, strategy)
                _gt_total = time.perf_counter()
                log_info(f"get_top_stocks 完成: 列表获取={_gt_list_t - _gt_t0:.3f}s 批量筛选={_gt_total - _gt_list_t:.3f}s 总计={_gt_total - _gt_t0:.3f}s", prefix="")
                return result
        except Exception as e:
            log_warning(f"Akshare(stock_info) 失败: {str(e)[:50]}", prefix="")

        stock_codes = self._safe_fetch_stock_list(ak.stock_zh_a_spot, "Akshare(新浪)", max_count=_FALLBACK_SAMPLE)
        if stock_codes:
            result = self.screen_batch(stock_codes, strategy)
            _gt_total = time.perf_counter()
            log_info(f"get_top_stocks 完成: 列表获取={_gt_list_t - _gt_t0:.3f}s 批量筛选={_gt_total - _gt_list_t:.3f}s 总计={_gt_total - _gt_t0:.3f}s", prefix="")
            return result

        try:
            log_info("方案3: Tushare", prefix="")
            ts.set_token(TUSHARE_TOKEN)
            pro = ts.pro_api()
            df = pro.stock_basic(exchange='', list_status='L', fields='ts_code,symbol,name')
            if df is not None and not df.empty:
                df = df[~df['name'].str.contains('ST|退市', na=False)]
                stock_codes = df['ts_code'].tolist()[:_FALLBACK_SAMPLE]
                log_success(f"Tushare 成功获取 {len(stock_codes)} 支股票", prefix="")
                result = self.screen_batch(stock_codes, strategy)
                _gt_total = time.perf_counter()
                log_info(f"get_top_stocks 完成: 列表获取={_gt_list_t - _gt_t0:.3f}s 批量筛选={_gt_total - _gt_list_t:.3f}s 总计={_gt_total - _gt_t0:.3f}s", prefix="")
                return result
        except Exception as e:
            error_msg = str(e)
            if '没有接口' in error_msg or '权限' in error_msg:
                log_warning("Tushare 失败：该 Token 没有 stock_basic 接口权限", prefix="")
            else:
                log_warning(f"Tushare 失败: {error_msg[:50]}", prefix="")

        stock_codes = self._safe_fetch_stock_list(ak.stock_zh_a_spot_em, "东方财富", max_count=_FALLBACK_SAMPLE)
        if stock_codes:
            result = self.screen_batch(stock_codes, strategy)
            _gt_total = time.perf_counter()
            log_info(f"get_top_stocks 完成: 列表获取={_gt_list_t - _gt_t0:.3f}s 批量筛选={_gt_total - _gt_list_t:.3f}s 总计={_gt_total - _gt_t0:.3f}s", prefix="")
            return result

        _gt_total = time.perf_counter()
        log_warning(f"所有数据源均获取失败 (耗时{_gt_total - _gt_t0:.3f}s)，请检查网络连接后重试", prefix="")
        return []

    def _prefilter_from_market_data(self, market_df: pd.DataFrame, strategy: str) -> List[str]:
        """从缓存的全市场行情数据中预筛选候选股票

        根据策略特征过滤无效股票并排序，提高后续评分的命中率：
        - 通用过滤：排除ST/退市/停牌/涨跌停
        - 短线强势(1)：按涨跌幅降序（强势股优先）
        - 主力建仓(2)：按换手率降序（活跃股优先）
        - 价值投资(3)：按市盈率升序（低估值优先）
        - all：按涨跌幅降序
        """
        name_col = next((c for c in market_df.columns if '名称' in str(c) or 'name' in str(c).lower()), None)
        code_col = next((c for c in market_df.columns if '代码' in str(c) or 'code' in str(c).lower()), None)
        if code_col is None:
            return []

        mask = pd.Series(True, index=market_df.index)
        if name_col:
            mask &= ~market_df[name_col].astype(str).str.contains('ST|退市', na=False)

        change_col = next((c for c in market_df.columns if '涨跌幅' in str(c) or 'changepercent' in str(c).lower()), None)
        if change_col:
            change_vals = pd.to_numeric(market_df[change_col], errors='coerce')
            mask &= change_vals.notna() & (change_vals.abs() < 9.9)

        vol_col = next((c for c in market_df.columns if '成交量' in str(c) or 'volume' in str(c).lower()), None)
        if vol_col:
            vol_vals = pd.to_numeric(market_df[vol_col], errors='coerce').fillna(0)
            mask &= vol_vals > 0

        df = market_df.loc[mask]

        sort_col = change_col
        ascending = False
        if strategy == '2':
            turnover_col = next((c for c in df.columns if '换手率' in str(c) or 'turnover' in str(c).lower()), None)
            if turnover_col:
                sort_col = turnover_col
        elif strategy == '3':
            pe_col = next((c for c in df.columns if '市盈率' in str(c) or 'pe' in str(c).lower()), None)
            if pe_col:
                sort_col = pe_col
                ascending = True
                pe_vals = pd.to_numeric(market_df[pe_col], errors='coerce')
                df = df.loc[pe_vals.loc[df.index] > 0]

        if sort_col and sort_col in df.columns:
            sort_vals = pd.to_numeric(df[sort_col], errors='coerce')
            df = df.loc[sort_vals.notna()]
            df = df.iloc[sort_vals[sort_vals.notna()].argsort()]
            if not ascending:
                df = df.iloc[::-1]

        n = min(self.max_recommendations * 5, len(df))
        return df[code_col].head(n).tolist()
