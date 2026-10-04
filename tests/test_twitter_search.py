# -*- coding: utf-8 -*-
"""Twitter(X) 站点搜索的解析、自愈与降级分支测试（全程 mock，不依赖网络）。"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools import twitter as tw


GRAPHQL_FIXTURE = {
    "data": {
        "search_by_raw_query": {
            "search_timeline": {
                "timeline": {
                    "instructions": [
                        {
                            "type": "TimelineAddEntries",
                            "entries": [
                                {
                                    "entryId": "tweet-1",
                                    "content": {
                                        "entryType": "TimelineTimelineItem",
                                        "itemContent": {
                                            "tweet_results": {
                                                "result": {
                                                    "__typename": "Tweet",
                                                    "rest_id": "111",
                                                    "core": {
                                                        "user_results": {
                                                            "result": {
                                                                "core": {"screen_name": "hans_ai", "name": "Hans"}
                                                            }
                                                        }
                                                    },
                                                    "legacy": {
                                                        "full_text": "short text",
                                                        "created_at": "Wed Oct 04 12:00:00 +0000 2026",
                                                        "favorite_count": 1200,
                                                        "retweet_count": 34,
                                                        "reply_count": 5,
                                                    },
                                                    "note_tweet": {
                                                        "note_tweet_results": {"result": {"text": "LONG NOTE TEXT"}}
                                                    },
                                                    "views": {"count": "4567"},
                                                }
                                            }
                                        },
                                    },
                                },
                                {
                                    "entryId": "cursor-bottom",
                                    "content": {"entryType": "TimelineTimelineCursor", "value": "cursor"},
                                },
                                {
                                    "entryId": "tweet-2",
                                    "content": {
                                        "itemContent": {
                                            "tweet_results": {
                                                "result": {
                                                    "__typename": "TweetWithVisibilityResults",
                                                    "tweet": {
                                                        "__typename": "Tweet",
                                                        "rest_id": "222",
                                                        "core": {
                                                            "user_results": {
                                                                "result": {"core": {"screen_name": "bob"}}
                                                            }
                                                        },
                                                        "legacy": {
                                                            "full_text": "visible text",
                                                            "created_at": "Thu Oct 05 01:02:03 +0000 2026",
                                                            "favorite_count": 7,
                                                            "retweet_count": 0,
                                                            "reply_count": 1,
                                                        },
                                                    },
                                                }
                                            }
                                        }
                                    },
                                },
                                {
                                    "entryId": "tweet-1-dup",
                                    "content": {
                                        "itemContent": {
                                            "tweet_results": {
                                                "result": {
                                                    "__typename": "Tweet",
                                                    "rest_id": "111",
                                                    "legacy": {"full_text": "dup", "created_at": ""},
                                                }
                                            }
                                        }
                                    },
                                },
                            ],
                        }
                    ]
                }
            }
        }
    }
}


class ExtractTests(unittest.TestCase):
    def test_extract_tweets_dedup_and_note_text(self):
        tweets = tw._extract_tweets(GRAPHQL_FIXTURE)
        self.assertEqual([t["id"] for t in tweets], ["111", "222"])
        first, second = tweets
        # note_tweet 的长文优先于 legacy.full_text
        self.assertEqual(first["text"], "LONG NOTE TEXT")
        self.assertEqual(first["handle"], "hans_ai")
        self.assertEqual(first["time"], "2026-10-04 12:00 UTC")
        self.assertEqual(first["url"], "https://x.com/hans_ai/status/111")
        self.assertIn("❤ 1,200", first["extra"])
        # TweetWithVisibilityResults 包装层要剥掉
        self.assertEqual(second["handle"], "bob")
        self.assertEqual(second["url"], "https://x.com/bob/status/222")

    def test_normalize_tolerates_legacy_only_shape(self):
        res = {
            "rest_id": "333",
            "legacy": {
                "full_text": "old shape",
                "created_at": "Fri Oct 06 00:00:00 +0000 2026",
                "favorite_count": 2,
                "retweet_count": 1,
                "reply_count": 0,
                "views": {"count": "99"},
                "screen_name": "x",
            },
            "core": {"user_results": {"result": {"legacy": {"screen_name": "old_user", "name": "Old"}}}},
        }
        item = tw._normalize_graphql_tweet(res)
        self.assertEqual(item["handle"], "old_user")
        self.assertIn("👁 99", item["extra"])

    def test_normalize_returns_none_without_id(self):
        self.assertIsNone(tw._normalize_graphql_tweet({"legacy": {"full_text": "no id"}}))


class HealFeatureTests(unittest.TestCase):
    def test_heal_features_from_x_error(self):
        body = json.dumps({
            "errors": [{
                "message": "The following features cannot be null: rweb_video_screen_enabled, "
                           "creator_subscriptions_tweet_preview_api_enabled"
            }]
        })
        healed = tw._heal_features({"existing": True}, body)
        self.assertEqual(healed["existing"], True)
        self.assertEqual(healed["rweb_video_screen_enabled"], False)
        self.assertEqual(healed["creator_subscriptions_tweet_preview_api_enabled"], False)

    def test_heal_features_returns_none_when_nothing_new(self):
        self.assertIsNone(tw._heal_features({}, "some other 400 error"))
        body = "features cannot be null: please"
        self.assertIsNone(tw._heal_features({}, body))


class CountAndTimeTests(unittest.TestCase):
    def test_parse_aria_counts_english(self):
        counts = tw._parse_aria_counts("12 replies, 34 reposts, 56 likes, 7,890 views")
        self.assertEqual((counts["replies"], counts["rts"], counts["likes"], counts["views"]),
                         (12, 34, 56, 7890))

    def test_parse_aria_counts_chinese_ui(self):
        counts = tw._parse_aria_counts("12 条回复、34 次转帖、5.6万 次查看")
        self.assertEqual(counts["replies"], 12)
        self.assertEqual(counts["rts"], 34)

    def test_fmt_count(self):
        self.assertEqual(tw._fmt_count(1200), "1,200")
        self.assertEqual(tw._fmt_count("12.3K"), "12.3K")
        self.assertEqual(tw._fmt_count("1.2M"), "1.2M")
        self.assertEqual(tw._fmt_count(None), "—")

    def test_fmt_iso_time(self):
        self.assertEqual(tw._fmt_iso_time("2026-10-04T12:00:00.000Z"), "2026-10-04 12:00 UTC")
        self.assertEqual(tw._fmt_time("Wed Oct 04 12:00:00 +0000 2026"), "2026-10-04 12:00 UTC")


class EntryPointTests(unittest.TestCase):
    def test_empty_keyword_rejected(self):
        result = tw.twitter_search("   ")
        self.assertTrue(result["meta"].get("error"))

    def test_invalid_mode_rejected(self):
        result = tw.twitter_search("ai", mode="hot")
        self.assertTrue(result["meta"].get("error"))
        self.assertIn("mode", result["content"])

    def test_no_backend_returns_setup_guide(self):
        with patch.object(tw, "X_AUTH_TOKEN", ""), patch.object(tw, "X_CT0", ""), \
                patch.object(tw, "cache_get", return_value=None), \
                patch.object(tw, "cdp_available", return_value=False):
            result = tw.twitter_search("audio llm")
        self.assertTrue(result["meta"].get("error"))
        self.assertIn("X_AUTH_TOKEN", result["content"])
        self.assertIn("9222", result["content"])

    def test_cache_hit_returns_markdown(self):
        cached_tweets = [{
            "id": "1", "handle": "hans", "name": "Hans", "url": "https://x.com/hans/status/1",
            "text": "hello", "time": "2026-10-04 12:00 UTC", "extra": "🕒 2026-10-04 12:00 UTC | ❤ 1",
        }]
        with patch.object(tw, "cache_get", return_value=cached_tweets):
            result = tw.twitter_search("hello")
        self.assertIn("@hans", result["content"])
        self.assertTrue(result["meta"].get("cached"))

    def test_cdp_login_wall_reports_clearly(self):
        with patch.object(tw, "X_AUTH_TOKEN", ""), patch.object(tw, "X_CT0", ""), \
                patch.object(tw, "cache_get", return_value=None), \
                patch.object(tw, "cache_set"), \
                patch.object(tw, "cdp_available", return_value=True), \
                patch.object(tw, "cdp_eval_new_tab",
                             return_value=(json.dumps({"url": "https://x.com/search", "loginWall": True,
                                                       "tweets": []}), "")):
            result = tw.twitter_search("audio llm")
        self.assertIn("未登录", result["content"])

    def test_cdp_tweets_are_formatted(self):
        payload = json.dumps({
            "url": "https://x.com/search?q=x",
            "loginWall": False,
            "tweets": [{
                "handle": "hans", "id": "99", "url": "https://x.com/hans/status/99",
                "text": "dom text", "name": "Hans",
                "time": "2026-10-04T12:00:00.000Z",
                "stats": "3 replies, 4 reposts, 5 likes, 600 views",
            }],
        })
        with patch.object(tw, "X_AUTH_TOKEN", "tok"), patch.object(tw, "X_CT0", "csrf"), \
                patch.object(tw, "cache_get", return_value=None), \
                patch.object(tw, "cache_set") as mock_set, \
                patch.object(tw, "_graphql_search", return_value=([], "", "")), \
                patch.object(tw, "cdp_available", return_value=True), \
                patch.object(tw, "cdp_eval_new_tab", return_value=(payload, "")):
            result = tw.twitter_search("x")
        self.assertIn("@hans", result["content"])
        self.assertIn("dom text", result["content"])
        self.assertIn("❤ 5", result["content"])
        self.assertTrue(mock_set.called)


if __name__ == "__main__":
    unittest.main()
