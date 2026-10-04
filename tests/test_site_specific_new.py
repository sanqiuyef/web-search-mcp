# -*- coding: utf-8 -*-
"""site_specific 新增开源平台搜索的参数校验与错误分支测试（mock requests，不依赖网络）。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.cache import cache_clear
from src.tools.site_specific import (
    crates_package_search,
    docker_image_search,
    gitlab_repo_search,
    huggingface_model_search,
    maven_package_search,
    nuget_package_search,
)


def _mock_get(json_data=None, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data if json_data is not None else {}
    resp.raise_for_status.side_effect = None if status == 200 else (
        lambda: (_ for _ in ()).throw(RuntimeError(f"HTTP {status}"))
    )
    session = MagicMock()
    session.headers = {}
    session.get.return_value = resp
    return session


class NewSiteSearchTests(unittest.TestCase):
    def setUp(self):
        # 清除共享缓存，避免前一个测试的同 key 结果污染后续断言
        cache_clear()
    def test_gitlab_parses_repositories(self):
        with patch("src.tools.site_specific.make_session", return_value=_mock_get([
            {
                "path_with_namespace": "gitlab-org/gitlab",
                "web_url": "https://gitlab.com/gitlab-org/gitlab",
                "description": "GitLab 仓库",
                "star_count": 5000,
                "forks_count": 2000,
                "last_activity_at": "2026-08-01T00:00:00Z",
            }
        ])):
            result = gitlab_repo_search("gitlab")
        self.assertEqual(result["results"][0]["name"], "gitlab-org/gitlab")
        self.assertIn("⭐ 5000", result["results"][0]["extra"])

    def test_gitlab_rejects_empty_keyword(self):
        result = gitlab_repo_search("   ")
        self.assertTrue(result["meta"]["error"])

    def test_crates_sends_user_agent_and_parses(self):
        with patch("src.tools.site_specific.make_session", return_value=_mock_get({
            "crates": [{
                "id": "serde",
                "description": "序列化框架",
                "max_version": "1.0.200",
                "downloads": 100000000,
            }]
        })) as mocked:
            result = crates_package_search("serde")
        session = mocked.return_value
        ua = session.headers.get("User-Agent", "")
        self.assertIn("@", ua, "crates.io 要求 UA 含邮箱")
        self.assertEqual(result["results"][0]["name"], "serde")
        self.assertIn("v1.0.200", result["results"][0]["extra"])

    def test_maven_parses_docs(self):
        with patch("src.tools.site_specific.make_session", return_value=_mock_get({
            "response": {"docs": [{
                "id": "org.slf4j:slf4j-api",
                "g": "org.slf4j",
                "a": "slf4j-api",
                "latestVersion": "2.0.13",
                "timestamp": 1710000000000,
            }]}
        })):
            result = maven_package_search("slf4j")
        self.assertEqual(result["results"][0]["name"], "org.slf4j:slf4j-api")
        self.assertIn("v2.0.13", result["results"][0]["extra"])

    def test_nuget_parses_packages(self):
        with patch("src.tools.site_specific.make_session", return_value=_mock_get({
            "data": [{
                "id": "Newtonsoft.Json",
                "version": "13.0.3",
                "description": "JSON 框架",
                "authors": ["James Newton-King"],
                "totalDownloads": 3000000000,
            }]
        })):
            result = nuget_package_search("json")
        self.assertEqual(result["results"][0]["name"], "Newtonsoft.Json")
        self.assertIn("v13.0.3", result["results"][0]["extra"])

    def test_docker_parses_images(self):
        with patch("src.tools.site_specific.make_session", return_value=_mock_get({
            "results": [{
                "repo_name": "library/nginx",
                "short_description": "Nginx 服务器",
                "star_count": 20000,
                "pull_count": 1000000000,
            }]
        })):
            result = docker_image_search("nginx")
        self.assertEqual(result["results"][0]["name"], "library/nginx")
        self.assertIn("⭐ 20000", result["results"][0]["extra"])

    def test_huggingface_parses_models(self):
        with patch("src.tools.site_specific.make_session", return_value=_mock_get([
            {
                "id": "Qwen/Qwen2.5-7B-Instruct",
                "downloads": 5000000,
                "likes": 1200,
                "pipeline_tag": "text-generation",
            }
        ])):
            result = huggingface_model_search("qwen")
        self.assertEqual(result["results"][0]["name"], "Qwen/Qwen2.5-7B-Instruct")
        self.assertIn("text-generation", result["results"][0]["extra"])

    def test_http_error_returns_error_result(self):
        with patch("src.tools.site_specific.make_session", return_value=_mock_get(status=500)):
            result = docker_image_search("nginx")
        self.assertTrue(result["meta"]["error"])
        self.assertIn("搜索失败", result["content"])


if __name__ == "__main__":
    unittest.main()
