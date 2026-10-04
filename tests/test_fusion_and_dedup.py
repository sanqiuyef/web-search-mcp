# -*- coding: utf-8 -*-
"""回归测试：URL 身份键、RRF 融合、内容近重复聚类、正文续读。

阈值与规则均来自本机真实搜索结果的标定（见 src/utils.py 中的实测记录）。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.utils import (
    NEAR_DUP_THRESHOLD,
    RRF_K,
    cluster_near_duplicates,
    merge_results,
    normalize_url,
    url_identity_key,
)
from src.tools.web_fetch import _slice_content, _cut_at_boundary, _continuation_note


def _item(title, url, snippet="", engine="duckduckgo"):
    return {"title": title, "url": url, "snippet": snippet, "engine": engine}


class UrlIdentityTests(unittest.TestCase):
    def test_scheme_and_www_are_ignored(self):
        self.assertEqual(url_identity_key("http://www.example.com/a"),
                         url_identity_key("https://example.com/a/"))

    def test_fragment_and_tracking_params_ignored(self):
        base = url_identity_key("https://example.com/a")
        self.assertEqual(base, url_identity_key("https://example.com/a#section"))
        self.assertEqual(base, url_identity_key("https://example.com/a?utm_source=x&gclid=y"))
        self.assertEqual(base, url_identity_key("https://example.com/a?fbclid=z"))

    def test_meaningful_query_is_kept(self):
        """有内容意义的参数必须保留，否则会把不同页面合并。"""
        self.assertNotEqual(url_identity_key("https://x.com/p?id=1"),
                            url_identity_key("https://x.com/p?id=2"))
        # ref/source/from 这类可能承载内容选择的参数刻意不剥
        self.assertNotEqual(url_identity_key("https://x.com/p?source=a"),
                            url_identity_key("https://x.com/p?source=b"))

    def test_ddg_redirect_unwrapped(self):
        wrapped = ("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fnews.bjx.com.cn%2Fhtml%2F1.shtml"
                   "&rut=abc")
        self.assertEqual(url_identity_key(wrapped),
                         url_identity_key("https://news.bjx.com.cn/html/1.shtml"))

    def test_bing_redirect_unwrapped(self):
        import base64
        real = "https://www.gov.cn/zhengce/content/1.htm"
        token = "a1" + base64.urlsafe_b64encode(real.encode()).decode().rstrip("=")
        wrapped = f"https://www.bing.com/ck/a?!&&p=xyz&u={token}&ntb=1"
        self.assertEqual(url_identity_key(wrapped), url_identity_key(real))

    def test_yandex_redirect_unwrapped(self):
        wrapped = "https://yandex.ru/redir?url=https%3A%2F%2Fexample.com%2Fdoc"
        self.assertEqual(url_identity_key(wrapped), url_identity_key("https://example.com/doc"))

    def test_normalize_url_strips_tracking_for_display(self):
        self.assertEqual(normalize_url("https://Example.com/a/?utm_source=x#frag"),
                         "https://Example.com/a")

    def test_empty_safe(self):
        self.assertEqual(url_identity_key(""), "")
        self.assertEqual(normalize_url(""), "")


class RrfFusionTests(unittest.TestCase):
    def test_no_rank_inversion(self):
        """RRF 不应出现名次倒挂：名次更好的结果必须排在名次更差的之前。"""
        ddg = [_item(f"d{i}", f"https://d.example.com/{i}", "摘要" * (i % 3)) for i in range(1, 7)]
        bing = [_item(f"b{i}", f"https://b.example.com/{i}", "另一段摘要" * (i % 4), "bing")
                for i in range(1, 7)]
        merged = merge_results([ddg, bing], 12)
        ranks = [i["best_rank"] for i in merged]
        self.assertEqual(ranks, sorted(ranks), "出现了名次倒挂")

    def test_cross_engine_consensus_beats_single_late_rank(self):
        """两个引擎都命中（即使名次靠后）应胜过单引擎靠后命中。"""
        ddg = [_item(f"d{i}", f"https://d.example.com/{i}") for i in range(1, 11)]
        ddg.append(_item("shared", "https://shared.example.com/x"))
        bing = [_item(f"b{i}", f"https://b.example.com/{i}", engine="bing") for i in range(1, 11)]
        bing.append(_item("shared", "https://shared.example.com/x", engine="bing"))
        merged = merge_results([ddg, bing], 30)
        shared = next(i for i in merged if "shared" in i["url"])
        self.assertEqual(shared["source_count"], 2)
        # 两引擎各排第 11 位：1/(10+11)*2 = 0.0952，高于任一引擎第 1 位的 0.0909
        single_first = next(i for i in merged if i["url"].endswith("/1"))
        self.assertGreater(shared["score"], single_first["score"])

    def test_snippet_length_does_not_change_order(self):
        """旧公式的摘要长度项会让长摘要结果浮到名次更好的结果之上，这里必须不受影响。"""
        ddg = [_item("short", "https://a.example.com/1", "短"),
               _item("long", "https://a.example.com/2", "长" * 300)]
        merged = merge_results([ddg], 5)
        self.assertEqual(merged[0]["url"], "https://a.example.com/1")

    def test_best_rank_recorded_per_engine(self):
        ddg = [_item("x", "https://x.example.com/a"), _item("y", "https://y.example.com/b")]
        bing = [_item("y", "https://y.example.com/b", engine="bing")]
        merged = merge_results([ddg, bing], 5)
        y = next(i for i in merged if i["url"].endswith("/b"))
        self.assertEqual(y["ranks"], {"duckduckgo": 2, "bing": 1})
        self.assertEqual(y["best_rank"], 1)

    def test_strength_normalized_to_ten(self):
        ddg = [_item(f"d{i}", f"https://d.example.com/{i}") for i in range(1, 5)]
        merged = merge_results([ddg], 4)
        self.assertEqual(merged[0]["strength"], 10.0)
        self.assertTrue(all(0 <= i["strength"] <= 10 for i in merged))

    def test_same_page_different_url_form_merges(self):
        """同一页面在不同引擎下 URL 形式不同（www/https）时，来源数必须累加。"""
        ddg = [_item("标题", "http://www.example.com/a")]
        bing = [_item("标题", "https://example.com/a", engine="bing")]
        merged = merge_results([ddg, bing], 5)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source_count"], 2)


class NearDuplicateTests(unittest.TestCase):
    def test_repost_on_different_domain_clusters(self):
        """实测样本：同一份政策发在两个官网（标题完全相同）。"""
        items = [
            {"title": "关于印发《新型储能规模化建设专项行动方案 (2025—2027年)》的通知",
             "snippet": "各省、自治区、直辖市人民政府，新疆生产建设兵团：为贯彻落实……",
             "url": "https://www.ndrc.gov.cn/xxgk/zcfb/tz/202509/t1_ext.html"},
            {"title": "关于印发《新型储能规模化建设专项行动方案 (2025—2027年)》的通知",
             "snippet": "各省、自治区、直辖市人民政府，新疆生产建设兵团：为贯彻落实……",
             "url": "https://www.nea.gov.cn/20250912/73455a1a/c.html"},
        ]
        clusters = cluster_near_duplicates(items)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(sorted(clusters[0]), [0, 1])

    def test_different_articles_on_same_topic_not_clustered(self):
        """实测样本：两篇不同的 MCP 文章（全文相似度 0.333，低于阈值 0.35）。"""
        items = [
            {"title": "通俗易懂讲清楚：什么是MCP、MCP服务，其实没那么高深",
             "snippet": "一、MCP是什么？ MCP ，全名是 Model Context Protocol（模型上下文协议）。"
                        "它是一种开放标准，用于把 AI 模型连接到各种外部工具和数据源，"
                        "通俗来讲，它就如同 AI 应用的 USB 接口。本文用最直白的话讲清三个角色",
             "url": "https://zhuanlan.zhihu.com/p/1922641762163340954"},
            {"title": "MCP详细介绍了什么是MCP，MCP为了解决什么问题",
             "snippet": "MCP 全称是 Model Context Protocol（模型上下文协议），它解决的是大模型"
                        "与外部数据源之间的连接问题。MCP 的架构主要包括 Host、Client、Server "
                        "三个核心角色，本文从协议设计出发介绍其工作流程与实现要点",
             "url": "https://juejin.cn/post/7666064008379482112"},
        ]
        self.assertEqual(len(cluster_near_duplicates(items)), 2)

    def test_same_site_different_pages_not_clustered(self):
        """实测样本：pythonlang.cn 首页与下载页（仅标题相似，内容不同）。"""
        items = [
            {"title": "欢迎来到Python.org -Python编程语言",
             "snippet": "2025年12月15日 · 快速 & 易于学习 任何其他语言的有经验的程序员都可以非常快速地掌握 Python",
             "url": "https://pythonlang.cn"},
            {"title": "下载Python|Python.org -Python编程语言",
             "snippet": "从 python.org 下载的适用于 macOS 的 Python 安装包使用 Apple 的签名",
             "url": "https://pythonlang.cn/downloads"},
        ]
        self.assertEqual(len(cluster_near_duplicates(items)), 2)

    def test_number_guard_keeps_versions_apart(self):
        items = [
            {"title": "Python 3.13 新特性一览", "snippet": "本文介绍 Python 3.13 的新特性与改进",
             "url": "https://a.com/313"},
            {"title": "Python 3.12 新特性一览", "snippet": "本文介绍 Python 3.12 的新特性与改进",
             "url": "https://b.com/312"},
        ]
        self.assertEqual(len(cluster_near_duplicates(items)), 2)

    def test_spam_template_clusters(self):
        """实测样本：两个不同域名的同模板垃圾站页面（标题不同、正文相同）。"""
        items = [
            {"title": "安博在线_安博(中国)",
             "snippet": "安博在线 体育 电竞 真人 视讯 电子 彩票 优惠活动 立即注册",
             "url": "https://www.waldenprojectny.com/wanboguanwangmanbetx/zhengce/165.html"},
            {"title": "乐鱼网页版登录入口-乐鱼（中国）",
             "snippet": "乐鱼网页版 体育 电竞 真人 视讯 电子 彩票 优惠活动 立即注册",
             "url": "https://www.sheltonsfurniture.com/uihrOjo/zhengce/165.html"},
        ]
        self.assertEqual(len(cluster_near_duplicates(items)), 1)

    def test_cluster_keeps_title_and_url_from_same_member(self):
        """标题与 URL 必须来自同一成员，否则模型会拿 A 的标题访问 B 的链接。"""
        items = [
            {"title": "水库大坝安全监测管理办法全文", "snippet": "第一条 为加强水库大坝安全监测",
             "url": "https://gov.example.com/a"},
            {"title": "水库大坝安全监测管理办法", "snippet": "第一条 为加强水库大坝安全监测",
             "url": "https://other.example.com/b"},
        ]
        merged = merge_results([items], 5)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "水库大坝安全监测管理办法全文")
        self.assertEqual(merged[0]["url"], "https://gov.example.com/a")

    def test_threshold_in_measured_gap(self):
        """阈值必须落在实测间隙 0.333~0.378 内。"""
        self.assertGreater(NEAR_DUP_THRESHOLD, 0.333)
        self.assertLess(NEAR_DUP_THRESHOLD, 0.378)

    def test_cluster_cost_is_bounded(self):
        import time
        items = [{"title": f"标题{i}", "snippet": "内容" * 60, "url": f"https://x.com/{i}"}
                 for i in range(50)]
        t0 = time.time()
        cluster_near_duplicates(items)
        self.assertLess(time.time() - t0, 0.5, "50 条聚类耗时过长")


class FetchContinuationTests(unittest.TestCase):
    def test_slice_reports_coordinates(self):
        text = "字" * 1000
        chunk, meta = _slice_content(text, 0, 300)
        self.assertEqual(meta["total_length"], 1000)
        self.assertTrue(meta["truncated"])
        self.assertLessEqual(len(chunk), 300)
        note = _continuation_note(meta)
        self.assertIn(f"start_index={meta['end_index']}", note)

    def test_continuation_returns_next_chunk(self):
        text = "".join(f"第{i}段的内容，讲的是第{i}个要点。" for i in range(200))
        first, meta1 = _slice_content(text, 0, 300)
        second, meta2 = _slice_content(text, meta1["end_index"], 300)
        self.assertTrue(second)
        self.assertNotEqual(first[:50], second[:50])
        self.assertEqual(meta2["start_index"], meta1["end_index"])
        # 两段拼起来应与原文的对应区间一致（不丢字）
        self.assertEqual(first + second, text[:meta2["end_index"]])

    def test_full_content_reports_complete(self):
        chunk, meta = _slice_content("短文", 0, 300)
        self.assertEqual(chunk, "短文")
        self.assertFalse(meta["truncated"])
        self.assertIn("已显示全文", _continuation_note(meta))

    def test_out_of_range_start_index(self):
        chunk, meta = _slice_content("短文", 100, 300)
        self.assertEqual(chunk, "")
        self.assertTrue(meta["out_of_range"])
        self.assertIn("超出正文长度", _continuation_note(meta))

    def test_cut_at_sentence_boundary(self):
        """截断应落在句子/段落边界，不能切在句子中间。"""
        text = "第一句完整的话。" * 40
        chunk = _cut_at_boundary(text[:300], 300)
        self.assertTrue(chunk.endswith("。"), f"截断位置不在句末: {chunk[-12:]!r}")

    def test_cut_falls_back_when_no_boundary(self):
        chunk = _cut_at_boundary("a" * 300, 300)
        self.assertEqual(len(chunk), 300)


if __name__ == "__main__":
    unittest.main()
