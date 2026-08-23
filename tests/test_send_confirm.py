# -*- coding: utf-8 -*-
"""core/tasks.py 发送确认决策逻辑（纯函数）的单元测试。

覆盖虚拟列表下的文字匹配确认与发送前去重，保证「宁可漏发也不误发/重复发」：
- 去重：最后一条本人气泡已是相同内容 -> 跳过发送
- 确认：新气泡内容匹配消息 -> sent；匹配不到 -> failed；无法区分新旧 -> unverified
"""
import unittest

from core.tasks import should_skip_duplicate, outgoing_confirm_verdict


class TestShouldSkipDuplicate(unittest.TestCase):
    def test_skip_when_last_own_bubble_is_same(self):
        self.assertTrue(should_skip_duplicate(3, "今日火花ABC", "今日火花ABC"))

    def test_no_skip_when_different(self):
        self.assertFalse(should_skip_duplicate(3, "昨日火花XYZ", "今日火花ABC"))

    def test_no_skip_when_no_bubbles(self):
        self.assertFalse(should_skip_duplicate(0, "", "今日火花ABC"))

    def test_no_skip_when_before_last_unknown(self):
        # before_last_text 为空（读不到）时绝不去重——宁可多考虑一次也不误跳
        self.assertFalse(should_skip_duplicate(3, "", "今日火花ABC"))


class TestOutgoingConfirmVerdict(unittest.TestCase):
    def test_sent_when_new_bubble_matches(self):
        # 发送前最后一条不是该内容，发送后最后一条 == 消息 -> sent
        self.assertEqual(
            outgoing_confirm_verdict("今日火花ABC", "昨日火花XYZ", "今日火花ABC"),
            "sent",
        )

    def test_failed_when_no_match(self):
        # 发送后最后一条不是消息内容 -> failed
        self.assertEqual(
            outgoing_confirm_verdict("今日火花ABC", "昨日火花XYZ", "不同内容"),
            "failed",
        )

    def test_failed_when_last_bubble_empty(self):
        # 连气泡都读不到 -> failed（宁漏发）
        self.assertEqual(outgoing_confirm_verdict("今日火花ABC", "昨日火花XYZ", ""), "failed")

    def test_unverified_when_cannot_distinguish(self):
        # 发送前最后一条已是相同内容，无法区分新旧 -> unverified，绝不宣称成功
        self.assertEqual(
            outgoing_confirm_verdict("今日火花ABC", "今日火花ABC", "今日火花ABC"),
            "unverified",
        )

    def test_unverified_dominates_failed(self):
        # 无法区分新旧时，即使内容也没匹配到（last_text 为空）也要 unverified 而非 failed
        self.assertEqual(
            outgoing_confirm_verdict("今日火花ABC", "今日火花ABC", ""),
            "unverified",
        )

    def test_empty_message_never_sent(self):
        # 消息内容为空 -> failed
        self.assertEqual(outgoing_confirm_verdict("", "昨日", "今日"), "failed")


if __name__ == "__main__":
    unittest.main()
