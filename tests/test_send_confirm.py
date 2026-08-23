# -*- coding: utf-8 -*-
"""core/tasks.py 发送确认决策逻辑（纯函数）的单元测试。

覆盖虚拟列表/表情码/换行混排下「宁可漏发也不误发/重复发」的决策：
- visible_compact：去表情码 + 去字面转义 + 去空白，跨渲染差异可对比
- already_present：发送前预览或最后气泡已是相同内容 -> 跳过发送
- confirm_signals：多信号确认（预览匹配 / 时间戳翻转「刚刚」/ 气泡匹配），全不命中 -> False
"""
import unittest

from core.tasks import (
    visible_compact,
    already_present,
    confirm_signals,
)


class TestVisibleCompact(unittest.TestCase):
    def test_strips_emoji_codes(self):
        # 气泡里表情码渲染成图片、inner_text 会丢失 -> 对比前先去掉
        self.assertEqual(visible_compact("[盖瑞]今日火花[加一]"), visible_compact("今日火花"))

    def test_bridges_literal_escape_mangling(self):
        # 编辑器把 \\n 混成「真实换行 + 字面 \\n」，气泡/预览呈现各不相同
        msg = "阶砖不会拒绝磨蚀。\n长河渐落晓星沉。"      # 真实换行
        bubble = "阶砖不会拒绝磨蚀。\\n长河渐落晓星沉。"  # 字面 \\n
        self.assertEqual(visible_compact(msg), visible_compact(bubble))

    def test_removes_all_whitespace_and_fullwidth_space(self):
        self.assertEqual(visible_compact("期安(路心月)"), visible_compact("期安 (路心月)"))

    def test_non_string_handled(self):
        self.assertEqual(visible_compact(None), "")
        self.assertEqual(visible_compact(""), "")


class TestAlreadyPresent(unittest.TestCase):
    def test_skip_when_preview_is_same(self):
        # 预览保留表情码原文，与消息同内容 -> 判定已存在，跳过
        self.assertTrue(already_present("[盖瑞]今日火花", "[盖瑞]今日火花", ""))

    def test_skip_when_last_bubble_is_same(self):
        self.assertTrue(already_present("今日火花ABC", "", "今日火花ABC"))

    def test_no_skip_when_different(self):
        self.assertFalse(already_present("今日火花ABC", "昨日火花XYZ", ""))

    def test_no_skip_when_no_evidence(self):
        self.assertFalse(already_present("今日火花ABC", "", ""))


class TestConfirmSignals(unittest.TestCase):
    def test_sent_when_preview_matches(self):
        # 最强信号：列表预览内容 == 消息（保留表情码原文）
        self.assertTrue(
            confirm_signals(
                "[盖瑞]今日火花", "20:42", "昨日内容", "刚刚", "[盖瑞]今日火花", ""
            )
        )

    def test_sent_when_timestamp_flips_to_just_now(self):
        # 时间戳从非「刚刚」翻转为「刚刚」-> 确认发送（布局无关）
        self.assertTrue(
            confirm_signals("今日火花ABC", "20:42", "昨日内容", "刚刚", "", "不同气泡")
        )

    def test_not_sent_when_timestamp_was_already_just_now(self):
        # 发送前已是「刚刚」-> 无法用时间戳区分新旧，也不能用空预览/无关气泡宣称成功
        self.assertFalse(
            confirm_signals("今日火花ABC", "刚刚", "昨日内容", "刚刚", "", "不同气泡")
        )

    def test_not_sent_when_before_timestamp_unreadable(self):
        # 发送前时间戳读不到 -> 时间戳信号不可用（保守）
        self.assertFalse(
            confirm_signals("今日火花ABC", "", "昨日内容", "刚刚", "", "不同气泡")
        )

    def test_sent_when_bubble_matches_after_emoji_strip(self):
        # 兜底：气泡去表情码后与消息一致（气泡里表情码渲染成图片丢失）
        self.assertTrue(
            confirm_signals(
                "[盖瑞]今日火花[加一]",
                "20:42",
                "昨日内容",
                "20:45",
                "",
                "今日火花",
            )
        )

    def test_not_sent_when_nothing_matches(self):
        self.assertFalse(
            confirm_signals("今日火花ABC", "20:42", "昨日内容", "20:45", "", "其他内容")
        )

    def test_not_sent_when_bubble_unrelated_but_ts_just_now_after_unreadable_before(self):
        # 即使 now_ts=刚刚，但 before_ts 读不到且无预览/气泡匹配 -> 不确认
        self.assertFalse(
            confirm_signals("今日火花ABC", "", "", "刚刚", "", "无关内容")
        )


if __name__ == "__main__":
    unittest.main()
