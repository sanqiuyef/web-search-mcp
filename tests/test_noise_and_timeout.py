# -*- coding: utf-8 -*-
"""回归测试：词典噪声过滤、拼接标题清理、逐引擎超时、ddgs 后端配置。"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import DDGS_BACKENDS, DDGS_REGION, JUNK_RESULT_DOMAINS
from src.utils import filter_junk_results, is_junk_url, sanitize_title


class JunkFilterTests(unittest.TestCase):
    def test_wikipedia_and_grokipedia_are_junk(self):
        self.assertTrue(is_junk_url("https://zh.wikipedia.org/wiki/城市"))
        self.assertTrue(is_junk_url("https://grokipedia.com/page/x"))
        self.assertTrue(is_junk_url("https://en.wiktionary.org/wiki/x"))

    def test_normal_sites_are_not_junk(self):
        self.assertFalse(is_junk_url("https://www.python.org/doc"))
        self.assertFalse(is_junk_url("https://stackoverflow.com/q/1"))
        # 域名包含 wikipedia 字样但非同域，不应误判
        self.assertFalse(is_junk_url("https://notwikipedia.org.example.com/x"))

    def test_filter_drops_junk_but_keeps_others(self):
        results = [
            {"url": "https://zh.wikipedia.org/wiki/x", "title": "wiki"},
            {"url": "https://stackoverflow.com/q/1", "title": "so"},
        ]
        kept = filter_junk_results(results)
        self.assertEqual([r["title"] for r in kept], ["so"])

    def test_filter_keeps_original_when_all_junk(self):
        """全部为百科类结果时（裸词查询）不应清空，避免无结果可答。"""
        results = [{"url": "https://zh.wikipedia.org/wiki/x", "title": "wiki"}]
        self.assertEqual(filter_junk_results(results), results)

    def test_filter_handles_empty_and_missing_url(self):
        self.assertEqual(filter_junk_results([]), [])
        kept = filter_junk_results([{"title": "no url"}])
        self.assertEqual(len(kept), 1)


class SanitizeTitleTests(unittest.TestCase):
    def test_normal_titles_unchanged(self):
        for t in ["Welcome to Python.org", "数字孪生甬江流域防洪减灾应用研究"]:
            self.assertEqual(sanitize_title(t), t)

    def test_domain_concat_is_cut(self):
        t = "Download Python | Python.orgPython 3.14.7 documentationThe Python Tutorial"
        self.assertEqual(sanitize_title(t), "Download Python | Python.org")

    def test_marker_concat_is_cut(self):
        t = ("城市（地理学名词）_百度百科城市 - 维基百科，自由的百科全书 - "
             "zh.wikipedia.org世界城市排名_百度百科全国694个城市名单")
        self.assertEqual(sanitize_title(t), "城市（地理学名词）_百度百科")

    def test_long_title_is_bounded(self):
        long_title = "word " * 60
        self.assertLessEqual(len(sanitize_title(long_title)), 81)

    def test_empty_and_none_safe(self):
        self.assertEqual(sanitize_title(""), "")
        self.assertEqual(sanitize_title(None), "")


class DdgsConfigTests(unittest.TestCase):
    def test_backends_are_explicit_not_auto(self):
        """必须显式指定后端：auto 会优先 wikipedia/grokipedia 造成词典噪声。"""
        self.assertNotIn("auto", DDGS_BACKENDS)
        self.assertTrue(DDGS_BACKENDS.strip())

    def test_only_measured_working_backends_by_default(self):
        """默认后端必须是实测可用的：失效后端排在前面会让每次搜索白等到超时。

        2026-09 实测：yahoo 超时无结果（曾可用），brave/duckduckgo/google/
        startpage 同样超时，只有 yandex 稳定返回（1.1-1.5s）。
        """
        self.assertIn("yandex", DDGS_BACKENDS)
        self.assertNotIn("yahoo", DDGS_BACKENDS)

    def test_region_is_configured(self):
        self.assertRegex(DDGS_REGION, r"^[a-z]{2}-[a-z]{2}$")

    def test_junk_domains_cover_encyclopedia(self):
        self.assertIn("wikipedia.org", JUNK_RESULT_DOMAINS)
        self.assertIn("grokipedia.com", JUNK_RESULT_DOMAINS)


class EngineTimeoutTests(unittest.TestCase):
    def test_slow_engine_does_not_block_others(self):
        """单个引擎卡死时，其余引擎结果仍应正常返回。"""
        from src.tools import advanced_search as adv

        def _fast(keyword, max_results):
            return [{"title": f"{keyword} fast", "url": "https://fast.example.com/a",
                     "snippet": "ok", "engine": "bing"}]

        def _hangs(keyword, max_results):
            time.sleep(30)  # 远超测试用的超时阈值
            return []

        engine_map = {"bing": _fast, "duckduckgo": _hangs}
        with patch.object(adv, "_build_engine_map", return_value=engine_map), \
             patch.object(adv, "ENGINE_TIMEOUT", 2):
            t0 = time.time()
            result = adv.web_search_advanced("probe", 5)
            elapsed = time.time() - t0

        self.assertLess(elapsed, 10, "聚合调用被慢引擎拖住了")
        self.assertFalse(result["meta"].get("error"))
        self.assertIn("fast", result["content"])
        # 超时引擎应被记录在状态里，而不是静默消失
        self.assertIn("超时", result["content"])


if __name__ == "__main__":
    unittest.main()
