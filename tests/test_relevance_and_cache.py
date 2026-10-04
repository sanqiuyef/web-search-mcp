# -*- coding: utf-8 -*-
"""回归测试：查询相关性分级过滤、空结果缓存毒化、重试、工具输出与线程池隔离。

用例里的噪声样本来自 2026-09 实测：
  - cn.bing.com 对「储能 电池 报价」返回 10 条 QQ 邮箱登录页
  - cn.bing.com 对「2026 中国 储能 政策 最新」返回 2026 年节假日安排
  - cn.bing.com 对「最新 储能政策」只按首个词检索，返回新闻门户首页
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.cache import cache_clear, cache_get, make_cache_key
from src.utils import (
    match_query_terms,
    query_terms,
    relevance_tier,
    split_by_relevance,
)


def _item(title, url="https://example.com/a", snippet="", engine="bing"):
    return {"title": title, "url": url, "snippet": snippet, "engine": engine}


class RelevanceTierTests(unittest.TestCase):
    def test_bing_qqmail_noise_is_irrelevant(self):
        """实测噪声：查询储能报价，Bing 返回 QQ 邮箱登录页。"""
        items = [
            _item("登录QQ邮箱", "https://mail.qq.com/", "QQ邮箱 - 第一步：选择邮箱账号"),
            _item("QQ邮箱帮助系统", "https://service.mail.qq.com/", "QQ邮箱帮助"),
        ]
        for item in items:
            self.assertEqual(relevance_tier("储能 电池 报价", item), "C")

    def test_bing_holiday_noise_is_irrelevant(self):
        """实测噪声：查询储能政策，Bing 返回 2026 年节假日安排。"""
        item = _item(
            "2026年大事、要事、重要节日一览表（附放假安排）",
            "https://news.qq.com/rain/a/20251231A04Z6M00",
            "2026年是农历的丙午年（马年），下面整理了2026年大事",
        )
        self.assertEqual(relevance_tier("2026 中国 储能 政策 最新", item), "C")

    def test_ontopic_results_are_strong(self):
        strong_cases = [
            ("储能 电池 报价", _item("储能磷酸铁锂电池_最新报价", "https://m.mysteel.com/hot/1.html",
                                 "储能电池今日价格、行情走势")),
            ("2026 中国 储能 政策 最新",
             _item("36项储能政策发布：国家定调、地方发力", "https://news.bjx.com.cn/html/1.shtml",
                   "2026年储能政策全面提速")),
            ("MCP 协议 是什么", _item("MCP 协议 - 菜鸟教程", "https://www.runoob.com/np/mcp-protocol.html",
                                  "MCP（Model Context Protocol，模型上下文协议）")),
        ]
        for keyword, item in strong_cases:
            self.assertEqual(relevance_tier(keyword, item), "A", f"{keyword} 应判为强相关")

    def test_partial_match_is_weak_not_dropped(self):
        """只命中一个强词：保留但降级（避免把有效结果直接丢掉）。"""
        item = _item("中国新闻_央视网", "https://news.cctv.com/", "中国最新新闻滚动播报")
        self.assertEqual(relevance_tier("2026 中国 储能 政策 最新", item), "B")

    def test_year_only_match_is_not_strong(self):
        """年份、编号这类词不体现主题，命中不算相关。"""
        item = _item("2026年日历全年完整图", "https://rili.example.com/2026", "2026年放假安排")
        self.assertEqual(relevance_tier("2026 储能 政策", item), "C")

    def test_latin_term_uses_word_boundary(self):
        """"excel" 不应命中 "excellent"。"""
        item = _item("An excellent guide", "https://example.com/excellent", "excellent content")
        self.assertEqual(relevance_tier("excel 读取", item), "C")

    def test_url_only_match_is_not_irrelevant(self):
        """查询词只出现在链接里时不应误杀（部分结果标题是泛化的“行业动态”）。"""
        item = _item("行业动态", "https://example.com/python-excel-guide", "最新行业信息")
        self.assertNotEqual(relevance_tier("python excel", item), "C")

    def test_query_terms_split_and_stopwords(self):
        strong, weak = query_terms("2026 中国 储能政策 最新 怎么")
        self.assertIn("储能政策", strong)
        self.assertIn("中国", strong)
        self.assertNotIn("最新", strong)
        self.assertNotIn("怎么", strong)
        self.assertIn("2026", weak)

    def test_match_counts(self):
        matched_strong, _ = match_query_terms("储能 政策", "储能政策解读", "")
        self.assertEqual(matched_strong, 2)

    def test_unjudgeable_query_returns_none(self):
        """查询里只有功能词时无法判定相关性，返回 None 让上层跳过过滤。"""
        self.assertIsNone(relevance_tier("最新", _item("随便什么")))
        self.assertIsNone(relevance_tier("", _item("随便什么")))

    def test_long_chinese_term_is_windowed(self):
        """长中文查询没有空格：整串匹配会误杀标题措辞略有差异的正常页面。"""
        item = _item("大坝安全监测技术规范 SL601-2013", "https://sl.example.com/601",
                     "水库大坝安全监测技术规范 水利行业标准")
        self.assertEqual(relevance_tier("水库大坝安全监测技术规范", item), "A")
        unrelated = _item("水利工程招标公告", "https://bid.example.com/1", "某水库除险加固工程招标")
        self.assertEqual(relevance_tier("水库大坝安全监测技术规范", unrelated), "C")

    def test_windowing_does_not_weaken_short_queries(self):
        """短词查询不受滑窗影响：仍然只有真命中的结果才算相关。"""
        self.assertEqual(relevance_tier("储能 电池 报价",
                                        _item("登录QQ邮箱", "https://mail.qq.com/", "QQ邮箱")), "C")

    def test_split_by_relevance_annotates_tier(self):
        items = [
            _item("储能政策解读", "https://a.com/1", "储能 政策"),
            _item("中国新闻", "https://a.com/2", "中国最新"),
            _item("登录QQ邮箱", "https://mail.qq.com/", "QQ邮箱登录"),
        ]
        strong, weak, dropped = split_by_relevance("储能 政策 中国", items)
        self.assertEqual(len(strong), 1)
        self.assertEqual(len(weak), 1)
        self.assertEqual(dropped, 1)
        self.assertEqual(items[0]["relevance_tier"], "A")
        self.assertEqual(items[2]["relevance_tier"], "C")


class AggregateSearchTests(unittest.TestCase):
    """聚合搜索：偏题结果不得进入输出，全偏题时必须显式报错。"""

    def _run(self, keyword, engines, max_results=5):
        from src.tools import advanced_search as adv
        with patch.object(adv, "_build_engine_map", return_value=engines):
            return adv.web_search_advanced(keyword, max_results)

    def test_offtopic_engine_results_are_dropped(self):
        junk = [_item("登录QQ邮箱", "https://mail.qq.com/", "QQ邮箱登录", "bing"),
                _item("知乎 - 有问题，就会有答案", "https://www.zhihu.com/billboard", "知乎", "bing")]
        good = [_item("储能磷酸铁锂电池最新报价", "https://m.mysteel.com/hot/1.html",
                      "储能电池今日价格", "duckduckgo")]
        result = self._run("储能 电池 报价",
                           {"bing": lambda k, m: junk, "duckduckgo": lambda k, m: good})
        self.assertFalse(result["meta"].get("error"))
        self.assertIn("mysteel", result["content"])
        self.assertNotIn("QQ邮箱", result["content"])
        self.assertNotIn("zhihu.com", result["content"])
        self.assertIn("剔除", result["content"])

    def test_all_engines_offtopic_returns_explicit_error(self):
        junk = [_item("登录QQ邮箱", "https://mail.qq.com/", "QQ邮箱登录", "bing")]
        result = self._run("储能 电池 报价", {"bing": lambda k, m: junk})
        self.assertTrue(result["meta"].get("error"))
        self.assertIn("不匹配", result["content"])
        self.assertNotIn("QQ邮箱", result["content"])

    def test_weak_results_fill_only_when_strong_insufficient(self):
        weak = [_item("中国新闻_央视网", "https://news.cctv.com/", "中国最新新闻", "bing")]
        result = self._run("2026 中国 储能 政策 最新", {"bing": lambda k, m: weak})
        self.assertFalse(result["meta"].get("error"))
        self.assertTrue(result["meta"]["weak_fill"])
        self.assertIn("相关性弱", result["content"])

    def test_strong_results_suppress_weak_ones(self):
        strong = [_item("储能政策解读", "https://a.com/1", "储能 政策 中国", "duckduckgo"),
                  _item("储能政策汇总", "https://a.com/2", "储能 政策 中国", "duckduckgo"),
                  _item("储能政策问答", "https://a.com/3", "储能 政策 中国", "duckduckgo")]
        weak = [_item("中国新闻_央视网", "https://news.cctv.com/", "中国最新新闻", "bing")]
        result = self._run("储能 政策 中国", {"duckduckgo": lambda k, m: strong,
                                          "bing": lambda k, m: weak})
        self.assertFalse(result["meta"]["weak_fill"])
        self.assertNotIn("cctv.com", result["content"])

    def test_weak_results_from_offtopic_engine_are_not_used(self):
        """某引擎一条强相关都没有时，它的弱相关结果不采用（只蹭到泛化词）。

        实测样本：查询储能政策时 Bing 返回节假日安排，只命中查询里的“中国”。
        """
        strong = [_item("储能政策解读", "https://a.com/1", "储能 政策 中国", "duckduckgo"),
                  _item("储能政策汇总", "https://a.com/2", "储能 政策 中国", "duckduckgo"),
                  _item("储能政策问答", "https://a.com/3", "储能 政策 中国", "duckduckgo")]
        holiday = [_item("2026中国节假日安排｜放假、调休、补班日历",
                         "https://chinacalendar.app/2026", "2026中国节假日安排", "bing")]
        result = self._run("2026 中国 储能 政策 最新",
                           {"duckduckgo": lambda k, m: strong, "bing": lambda k, m: holiday})
        self.assertNotIn("chinacalendar", result["content"])
        self.assertIn("无强相关结果", result["content"])
        self.assertFalse(result["meta"]["weak_fill"])

    def test_weak_fill_comes_from_engine_with_strong_hits(self):
        """产出过强相关结果的引擎，其弱相关结果仍可用于补位。"""
        engine_out = [
            _item("储能政策解读", "https://a.com/1", "储能 政策 中国", "duckduckgo"),
            _item("储能电池产能扩张", "https://a.com/2", "储能电池", "duckduckgo"),
        ]
        result = self._run("2026 中国 储能 政策 最新", {"duckduckgo": lambda k, m: engine_out})
        self.assertFalse(result["meta"].get("error"))
        self.assertTrue(result["meta"]["weak_fill"])
        self.assertIn("a.com/2", result["content"])

    def test_news_category_falls_back_to_web_search(self):
        """新闻分类无可用引擎时退化为网页搜索，而不是返回空结果。"""
        from src.tools import advanced_search as adv

        calls = {"n": 0}

        def _news_engine(k, m):
            calls["n"] += 1
            return []          # 新闻引擎无结果

        def _web_engine(k, m):
            calls["n"] += 1
            return [_item("储能政策解读", "https://a.com/1", "储能 政策 电池 报价", "duckduckgo")]

        def _build(category, time_range=None, language=None):
            return {"duckduckgo": _news_engine if category == "news" else _web_engine,
                    "bing": _news_engine if category == "news" else _web_engine}

        with patch.object(adv, "_build_engine_map", side_effect=_build):
            result = adv.web_search_advanced("储能 电池 报价", 5, category="news",
                                             time_range="week")
        self.assertFalse(result["meta"].get("error"))
        self.assertEqual(result["meta"]["category"], "general")
        self.assertIn("已改用网页搜索", result["content"])

    def test_google_excluded_without_proxy(self):
        from src.tools.advanced_search import _build_engine_map
        with patch("src.tools.advanced_search.has_proxy", return_value=False):
            engine_map = _build_engine_map("general")
        self.assertNotIn("google", engine_map)
        with patch("src.tools.advanced_search.has_proxy", return_value=True):
            engine_map = _build_engine_map("general")
        self.assertIn("google", engine_map)


class BingCacheTests(unittest.TestCase):
    """空结果不得写缓存：一次抖动会让同一查询空 5 分钟。"""

    def setUp(self):
        cache_clear()

    def test_failure_is_not_cached(self):
        from src import searchengines
        with patch("src.searchengines.bing._do_search_bing",
                   side_effect=ConnectionError("boom")):
            results = searchengines.bing.search_bing_items("储能 政策", 5)
        self.assertEqual(results, [])
        key = make_cache_key("bing", "储能 政策", 5, ":")
        self.assertIsNone(cache_get(key))

    def test_empty_result_is_not_cached(self):
        from src import searchengines
        with patch("src.searchengines.bing._do_search_bing", return_value=[]):
            searchengines.bing.search_bing_items("储能 政策", 5)
        key = make_cache_key("bing", "储能 政策", 5, ":")
        self.assertIsNone(cache_get(key))

    def test_retry_is_wired_to_network_call(self):
        """重试必须挂在真正发请求的函数上（外层会吞异常，装饰在外层等于不重试）。"""
        from src.searchengines.bing import _do_search_bing
        self.assertTrue(hasattr(_do_search_bing, "__wrapped__"))

    def test_transient_error_is_retried(self):
        from src import searchengines
        html = ('<html><body><ol id="b_results">'
                '<li class="b_algo"><h2><a href="https://a.com/1">储能政策解读</a></h2>'
                '<div class="b_caption"><p>储能 政策 中国</p></div></li>'
                '</ol></body></html>')
        response = MagicMock()
        response.text = html
        response.raise_for_status = MagicMock()
        session = MagicMock()
        session.get.side_effect = [ConnectionError("first attempt fails"), response]
        with patch("src.searchengines.bing.make_session", return_value=session), \
             patch("src.retry.time.sleep", return_value=None):
            results = searchengines.bing.search_bing_items("储能 政策", 5)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://a.com/1")

    def test_pagination_and_ad_nodes_are_skipped(self):
        from src.searchengines.bing import _extract_bing_item
        from bs4 import BeautifulSoup
        html = ('<li class="b_pag"><a href="https://cn.bing.com/search?q=x&first=11">下一页</a></li>')
        self.assertIsNone(_extract_bing_item(BeautifulSoup(html, "html.parser").li))
        ad = BeautifulSoup('<li class="b_ad"><h2><a href="https://ad.com/x">广告</a></h2></li>',
                           "html.parser").li
        self.assertIsNone(_extract_bing_item(ad))


class DdgFallbackBudgetTests(unittest.TestCase):
    """DDG 库路径 + HTML 兜底的总耗时必须小于聚合层的单引擎超时。

    否则库一失败，整个 DDG 引擎会被判超时、结果全部丢弃（实测发生过：
    兜底路径的“取首页 cookie 再重试”支路累加到 15s+，撞穿 22s 超时线）。
    """

    def test_worst_case_fits_engine_timeout(self):
        from src.config import DDGS_TIMEOUT
        from src.searchengines.duckduckgo import _HTML_FALLBACK_BUDGET
        from src.tools.advanced_search import ENGINE_TIMEOUT

        self.assertLess(DDGS_TIMEOUT + _HTML_FALLBACK_BUDGET, ENGINE_TIMEOUT,
                        "库超时 + 兜底预算已超过单引擎超时，DDG 会被整批丢弃")

    def test_fallback_cools_down_after_failure(self):
        from src.searchengines import duckduckgo as ddg

        original = ddg._html_fallback_dead_until
        try:
            ddg._html_fallback_dead_until = 0.0
            with patch.object(ddg, "_search_via_library", return_value=None), \
                 patch.object(ddg, "_search_via_html", return_value=[]):
                ddg.search_duckduckgo_items("储能 电池 报价", 3)
            self.assertFalse(ddg._html_fallback_available(), "兜底失败后应进入冷却")
        finally:
            ddg._html_fallback_dead_until = original

    def test_news_library_cools_down_after_failure(self):
        """新闻端点不可达时进入冷却，避免每个新闻查询都白等一个超时周期。"""
        from src.searchengines import duckduckgo as ddg

        original = ddg._news_dead_until
        try:
            ddg._news_dead_until = 0.0
            with patch.object(ddg, "_search_via_html", return_value=[]), \
                 patch("ddgs.DDGS", side_effect=TimeoutError("endpoint unreachable")):
                ddg.search_duckduckgo_news_items("储能政策", 3)
            self.assertFalse(ddg._news_library_available(), "新闻端点失败后应进入冷却")
        finally:
            ddg._news_dead_until = original


class EngineCircuitBreakerTests(unittest.TestCase):
    """引擎连续超时后熔断：避免每次调用都白等一个超时周期、并占死工作线程。"""

    def setUp(self):
        from src.utils import reset_engine_health
        reset_engine_health()

    def tearDown(self):
        from src.utils import reset_engine_health
        reset_engine_health()

    def test_breaker_trips_after_consecutive_failures(self):
        from src.utils import (ENGINE_FAILURE_LIMIT, engine_available,
                               engine_cooldown_remaining, note_engine_result)

        for _ in range(ENGINE_FAILURE_LIMIT - 1):
            note_engine_result("duckduckgo", False)
        self.assertTrue(engine_available("duckduckgo"))
        note_engine_result("duckduckgo", False)
        self.assertFalse(engine_available("duckduckgo"))
        self.assertGreater(engine_cooldown_remaining("duckduckgo"), 0)
        # 其它引擎不受影响
        self.assertTrue(engine_available("bing"))

    def test_success_resets_failure_count(self):
        from src.utils import ENGINE_FAILURE_LIMIT, engine_available, note_engine_result

        for _ in range(ENGINE_FAILURE_LIMIT - 1):
            note_engine_result("duckduckgo", False)
        note_engine_result("duckduckgo", True)
        note_engine_result("duckduckgo", False)
        self.assertTrue(engine_available("duckduckgo"))

    def test_cooldown_expires(self):
        from src.utils import engine_available, note_engine_result
        from src import utils as utils_mod

        for _ in range(utils_mod.ENGINE_FAILURE_LIMIT):
            note_engine_result("bing", False)
        self.assertFalse(engine_available("bing"))
        utils_mod._engine_cooldown_until["bing"] = 0.0  # 模拟冷却到期
        self.assertTrue(engine_available("bing"))

    def test_aggregate_skips_cooling_engine(self):
        from src.tools import advanced_search as adv
        from src.utils import note_engine_result
        from src import utils as utils_mod

        for _ in range(utils_mod.ENGINE_FAILURE_LIMIT):
            note_engine_result("duckduckgo", False)

        good = [_item("储能政策解读", "https://a.com/1", "储能 政策 电池 报价", "bing")]
        with patch.object(adv, "_build_engine_map",
                          return_value={"duckduckgo": lambda k, m: good,
                                        "bing": lambda k, m: good}):
            result = adv.web_search_advanced("储能 电池 报价", 5)
        self.assertIn("Bing", result["meta"]["engines"])
        self.assertNotIn("DuckDuckGo", result["meta"]["engines"])

    def test_all_engines_cooling_reports_reason(self):
        from src.tools import advanced_search as adv
        from src.utils import note_engine_result
        from src import utils as utils_mod

        for _ in range(utils_mod.ENGINE_FAILURE_LIMIT):
            note_engine_result("bing", False)
        with patch.object(adv, "_build_engine_map", return_value={"bing": lambda k, m: []}):
            result = adv.web_search_advanced("储能 电池 报价", 5)
        self.assertTrue(result["meta"]["error"])
        self.assertIn("临时停用", result["content"])


class ServerOutputTests(unittest.TestCase):
    """工具输出：不重复序列化、空结果有明确说明、线程池按用途隔离。"""

    def test_structured_results_not_dumped_twice(self):
        import server
        text = server._tool_result_text({
            "content": "正文内容",
            "results": [{"index": 1, "title": "标题", "url": "https://a.com"}],
            "meta": {"keyword": "k"},
        })
        self.assertIn("正文内容", text)
        self.assertNotIn("### 结构化结果", text)
        self.assertNotIn('"title": "标题"', text)

    def test_empty_list_has_explicit_message(self):
        import server
        self.assertTrue(server._tool_result_text([]).strip())

    def test_single_engine_junk_is_reported(self):
        import server
        junk = [_item("登录QQ邮箱", "https://mail.qq.com/", "QQ邮箱登录")]
        text = server._single_engine_search_text("Bing", "储能 电池 报价", junk)
        self.assertIn("不匹配", text)
        self.assertNotIn("QQ邮箱", text)

    def test_single_engine_timeout_dict_passthrough(self):
        import server
        timeout = {"content": "⏱️ 搜索超时（>15s）", "results": [], "meta": {"error": True}}
        self.assertIn("超时", server._single_engine_search_text("Bing", "储能", timeout))

    def test_pools_are_isolated_by_purpose(self):
        import server
        self.assertIsNot(server._get_search_executor("search"),
                         server._get_search_executor("content"))
        self.assertGreaterEqual(server._POOL_SIZES["search"], 4)


if __name__ == "__main__":
    unittest.main()
