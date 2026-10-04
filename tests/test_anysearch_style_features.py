# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools.batch_search import batch_web_search
from src.tools.web_fetch import _extract_readable_content


class AnySearchStyleFeatureTests(unittest.TestCase):
    def test_batch_search_keeps_query_order(self):
        def fake_search(keyword, **_):
            return {"content": f"result:{keyword}", "results": [], "meta": {}}

        with patch("src.tools.batch_search.web_search_advanced", side_effect=fake_search):
            result = batch_web_search([{"keyword": "first"}, {"keyword": "second"}])

        self.assertEqual(result["meta"], {"query_count": 2, "parallel": True})
        self.assertEqual([item["query"]["keyword"] for item in result["results"]], ["first", "second"])
        self.assertLess(result["content"].index("result:first"), result["content"].index("result:second"))

    def test_batch_search_rejects_unknown_category(self):
        result = batch_web_search([{"keyword": "test", "category": "academic"}])
        self.assertTrue(result["meta"]["error"])

    def test_html_content_can_be_rendered_as_markdown(self):
        content = _extract_readable_content("<html><head><title>Example</title></head><body><main><h1>Heading</h1><p>A useful paragraph.</p><a href='https://example.com'>Link</a></main></body></html>")
        self.assertIn("# Heading", content["markdown"])
        self.assertIn("[Link](https://example.com)", content["markdown"])


if __name__ == "__main__":
    unittest.main()
