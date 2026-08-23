# -*- coding: utf-8 -*-
"""utils/config.py 的单元测试：Cookie 清洗、targets 新旧格式解析。"""
import json
import os
import unittest

import utils.config
from utils.config import sanitize_cookies, parse_targets, get_userData


class TestSanitizeCookies(unittest.TestCase):
    def test_keeps_valid_same_site(self):
        cookies = [
            {
                "name": "sessionid",
                "value": "abc",
                "domain": ".douyin.com",
                "path": "/",
                "sameSite": "Lax",
            }
        ]
        out = sanitize_cookies(cookies)
        self.assertEqual(out[0]["sameSite"], "Lax")

    def test_all_valid_same_site_values(self):
        for value in ("Strict", "Lax", "None"):
            out = sanitize_cookies([{"name": "n", "value": "v", "sameSite": value}])
            self.assertEqual(out[0]["sameSite"], value)

    def test_drops_invalid_same_site_value(self):
        out = sanitize_cookies([{"name": "n", "value": "v", "sameSite": "NoRestriction"}])
        self.assertNotIn("sameSite", out[0])

    def test_converts_expiration_date_to_expires(self):
        out = sanitize_cookies(
            [{"name": "n", "value": "v", "expirationDate": 1750000000.0}]
        )
        self.assertEqual(out[0]["expires"], 1750000000)

    def test_keeps_expires_as_float_when_fractional(self):
        out = sanitize_cookies([{"name": "n", "value": "v", "expires": 1750000000.5}])
        self.assertEqual(out[0]["expires"], 1750000000.5)

    def test_drops_unsupported_fields(self):
        raw = {
            "name": "n",
            "value": "v",
            "hostOnly": True,
            "storeId": "0",
            "session": True,
        }
        out = sanitize_cookies([raw])
        self.assertNotIn("hostOnly", out[0])
        self.assertNotIn("storeId", out[0])
        self.assertNotIn("session", out[0])

    def test_keeps_http_only_secure_bools(self):
        out = sanitize_cookies(
            [{"name": "n", "value": "v", "httpOnly": True, "secure": False}]
        )
        self.assertTrue(out[0]["httpOnly"])
        self.assertFalse(out[0]["secure"])

    def test_drops_items_without_name_or_value(self):
        out = sanitize_cookies([{"domain": ".douyin.com"}, "not-a-dict", 42])
        self.assertEqual(out, [])


class TestParseTargets(unittest.TestCase):
    def test_old_string_format(self):
        out = parse_targets(["好友A", "好友B"])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["id"], "好友A")
        self.assertEqual(out[0]["search_terms"], ["好友A"])
        self.assertEqual(out[0]["title_aliases"], ["好友A"])
        self.assertEqual(out[0]["search_terms_norm"], ["好友A"])
        self.assertEqual(out[0]["title_aliases_norm"], ["好友A"])

    def test_new_object_format(self):
        out = parse_targets(
            [
                {
                    "id": "好友名",
                    "search_terms": ["搜词1", "搜词2"],
                    "title_aliases": ["列表标题", "备注名"],
                }
            ]
        )
        self.assertEqual(out[0]["id"], "好友名")
        self.assertEqual(out[0]["search_terms"], ["搜词1", "搜词2"])
        self.assertEqual(out[0]["title_aliases"], ["列表标题", "备注名"])
        self.assertEqual(out[0]["search_terms_norm"], ["搜词1", "搜词2"])
        self.assertEqual(out[0]["title_aliases_norm"], ["列表标题", "备注名"])

    def test_object_without_id_uses_first_search_term(self):
        out = parse_targets([{"search_terms": ["A"], "title_aliases": ["A"]}])
        self.assertEqual(out[0]["id"], "A")

    def test_missing_search_terms_skipped(self):
        out = parse_targets([{"id": "X", "title_aliases": ["A"]}])
        self.assertEqual(out, [])

    def test_missing_title_aliases_skipped(self):
        out = parse_targets([{"id": "X", "search_terms": ["A"]}])
        self.assertEqual(out, [])

    def test_empty_search_terms_skipped(self):
        out = parse_targets([{"search_terms": [], "title_aliases": ["A"]}])
        self.assertEqual(out, [])

    def test_not_a_list_returns_empty(self):
        self.assertEqual(parse_targets("不是列表"), [])
        self.assertEqual(parse_targets(None), [])

    def test_unsupported_item_skipped(self):
        out = parse_targets([{"a": 1}, 123])
        self.assertEqual(out, [])

    def test_special_chars_normalized(self):
        # 数学粗体（NFKC 后 -> "Dream."）+ 不换行空格；PUA 苹果标保留（无兼容分解）
        out = parse_targets(["𝓓𝓻𝓮𝓪𝓶.\xa0", "Liu Beixi"])
        self.assertEqual(out[0]["title_aliases_norm"], ["Dream."])
        self.assertEqual(out[1]["title_aliases_norm"], ["Liu Beixi"])


class TestDryRunConfig(unittest.TestCase):
    """DRY_RUN 只读诊断模式：发送前停止，绝不误发。"""

    def setUp(self):
        utils.config.config = None
        utils.config.userData = None
        os.environ.pop("DRY_RUN", None)

    def tearDown(self):
        os.environ.pop("DRY_RUN", None)

    def test_false_when_env_unset(self):
        self.assertFalse(utils.config.get_config()["dryRun"])

    def test_false_when_env_empty(self):
        # GitHub Actions 里 var 未设置时 env 注入为空串 -> 必须是关闭
        os.environ["DRY_RUN"] = ""
        self.assertFalse(utils.config.get_config()["dryRun"])

    def test_true_when_env_one(self):
        os.environ["DRY_RUN"] = "1"
        self.assertTrue(utils.config.get_config()["dryRun"])

    def test_true_when_env_true(self):
        os.environ["DRY_RUN"] = "true"
        self.assertTrue(utils.config.get_config()["dryRun"])


class TestOutgoingBubbleDefault(unittest.TestCase):
    """outgoingBubbleSelector 的默认值逻辑（真实 DOM 已核实为 .MessageItemTextisFromMe）。"""

    def setUp(self):
        utils.config.config = None
        utils.config.userData = None
        os.environ.pop("OUTGOING_BUBBLE_SELECTOR", None)

    def tearDown(self):
        os.environ.pop("OUTGOING_BUBBLE_SELECTOR", None)

    def test_default_when_env_unset(self):
        # 未设置 -> 用真实 DOM 核实的默认值，保证发送确认开启
        self.assertEqual(utils.config.get_config()["outgoingBubbleSelector"], ".MessageItemTextisFromMe")

    def test_default_when_env_injected_empty(self):
        # GitHub Actions 里 var 未设置时会把 env 注入成空串 -> 仍回退默认值（or 语义）
        os.environ["OUTGOING_BUBBLE_SELECTOR"] = ""
        self.assertEqual(utils.config.get_config()["outgoingBubbleSelector"], ".MessageItemTextisFromMe")

    def test_explicit_override_wins(self):
        # 显式配置可覆盖默认（例如抖音改版后新类名）
        os.environ["OUTGOING_BUBBLE_SELECTOR"] = ".CustomNewBubble"
        self.assertEqual(utils.config.get_config()["outgoingBubbleSelector"], ".CustomNewBubble")


class TestGetUserData(unittest.TestCase):
    def setUp(self):
        # get_userData 有模块级缓存，测试间必须重置
        utils.config.userData = None
        utils.config.config = None

    def tearDown(self):
        os.environ.pop("TASKS", None)
        os.environ.pop("COOKIES_T", None)
        os.environ.pop("DOUYIN_PROFILE_PATH", None)
        os.environ.pop("GITHUB_ACTIONS", None)

    def test_skips_task_without_cookies(self):
        os.environ["TASKS"] = json.dumps([{"username": "u", "unique_id": "t", "targets": ["A"]}])
        os.environ.pop("DOUYIN_PROFILE_PATH", None)
        # 非 profile 模式：没有 COOKIES_T -> 跳过
        self.assertEqual(get_userData(), [])

    def test_allows_empty_cookies_only_in_local_profile_mode(self):
        os.environ["TASKS"] = json.dumps([{"username": "u", "unique_id": "t", "targets": ["A"]}])
        os.environ["GITHUB_ACTIONS"] = ""  # 模拟本地环境
        os.environ["DOUYIN_PROFILE_PATH"] = r"C:\profile\douyin_2"
        os.environ.pop("COOKIES_T", None)
        data = get_userData()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["cookies"], [])

    def test_skips_missing_cookies_when_profile_path_unset(self):
        os.environ["TASKS"] = json.dumps([{"username": "u", "unique_id": "t", "targets": ["A"]}])
        os.environ["GITHUB_ACTIONS"] = ""  # 本地，但没有 DOUYIN_PROFILE_PATH
        os.environ.pop("DOUYIN_PROFILE_PATH", None)
        os.environ.pop("COOKIES_T", None)
        self.assertEqual(get_userData(), [])

    def test_parses_cookies_and_targets(self):
        os.environ["TASKS"] = json.dumps(
            [{"username": "u", "unique_id": "t", "targets": [{"id": "A", "search_terms": ["A"], "title_aliases": ["A"]}]}]
        )
        os.environ["COOKIES_T"] = json.dumps(
            [{"name": "sessionid", "value": "v", "sameSite": "Lax"}]
        )
        data = get_userData()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["cookies"][0]["sameSite"], "Lax")
        self.assertEqual(data[0]["targets"][0]["id"], "A")


if __name__ == "__main__":
    unittest.main()
