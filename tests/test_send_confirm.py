# -*- coding: utf-8 -*-
"""core/tasks.py 发送确认 / 发送前去重决策逻辑（纯函数）的单元测试。

覆盖「宁可漏发也绝不误发/重复发」在跨零点、表情码、换行混排下的决策：
- _date_state：三态时间判定（today / nottoday / unknown），北京时区跨零点正确
- visible_compact：去表情码 + 去字面转义 + 去空白，跨渲染差异可对比
- already_present：新签名四参（本人气泡文本 / 列表时间状态 / 本人气泡时间状态）
- confirm_signals：多信号确认（预览匹配 / 时间戳翻转「刚刚」/ 气泡匹配），全不命中 -> False

跨零点核心语义（旧 bug 修复）：北京时间 00:07 的云端运行看到会话列表「2小时前」，
那是前一天 22:07 本人发的 🔥，不是今天——绝不能据此跳过。列表时间是「会话最后一条
消息」的时间（可能是对方今天回复），判定「本人今天是否已发」必须看本人气泡自己的
时间戳状态（own_state）。
"""
import unittest
from datetime import datetime, timedelta, timezone

from core.tasks import (
    visible_compact,
    already_present,
    confirm_signals,
    _date_state,
)

# 北京时区（与 tasks.CN_TZ 一致：UTC+8，无夏令时）
CN_TZ = timezone(timedelta(hours=8))
BJ = lambda *a: datetime(*a, tzinfo=CN_TZ)  # noqa: E731 构造北京时间（aware，贴近 runner 行为）


class TestDateState(unittest.TestCase):
    """_date_state 三态判定：00:48 看到 '2小时前' 必须是非今天（旧 bug 根因）。"""

    def test_0048_two_hours_ago_is_nottoday(self):
        # 云端实测场景：北京时间 08-25 00:48 列表显示「2小时前」= 08-24 22:48
        self.assertEqual(_date_state("2小时前", BJ(2026, 8, 25, 0, 48)), "nottoday")

    def test_0048_one_hour_ago_is_nottoday(self):
        self.assertEqual(_date_state("1小时前", BJ(2026, 8, 25, 0, 48)), "nottoday")

    def test_0048_ten_min_ago_is_today(self):
        # 00:38 仍属今天
        self.assertEqual(_date_state("10分钟前", BJ(2026, 8, 25, 0, 48)), "today")

    def test_0048_forty_eight_min_ago_boundary_is_today(self):
        # 恰好 00:00（自然日分界点仍算今天）
        self.assertEqual(_date_state("48分钟前", BJ(2026, 8, 25, 0, 48)), "today")

    def test_0048_forty_nine_min_ago_is_nottoday(self):
        # 49 分钟前 = 23:59（前一天）-> 非今天
        self.assertEqual(_date_state("49分钟前", BJ(2026, 8, 25, 0, 48)), "nottoday")

    def test_0005_five_min_ago_is_today(self):
        # 00:00 发出，00:05 看到「5分钟前」仍是今天
        self.assertEqual(_date_state("5分钟前", BJ(2026, 8, 25, 0, 5)), "today")

    def test_2300_hours_before_midnight_still_today(self):
        # 23:00 看到「1小时前」= 22:00 今天；「2小时前」= 21:00 今天
        self.assertEqual(_date_state("1小时前", BJ(2026, 8, 25, 23, 0)), "today")
        self.assertEqual(_date_state("2小时前", BJ(2026, 8, 25, 23, 0)), "today")

    def test_0030_next_day_one_hour_ago_is_nottoday(self):
        # 新一天 00:30 看到「1小时前」= 前一天 23:30
        self.assertEqual(_date_state("1小时前", BJ(2026, 8, 26, 0, 30)), "nottoday")

    def test_hhmm_after_now_is_nottoday(self):
        # HH:MM 晚于当前时刻必是前一天（时间不会来自未来）
        self.assertEqual(_date_state("10:04", BJ(2026, 8, 25, 0, 48)), "nottoday")
        self.assertEqual(_date_state("00:59", BJ(2026, 8, 25, 0, 48)), "nottoday")

    def test_hhmm_before_now_is_today(self):
        self.assertEqual(_date_state("00:30", BJ(2026, 8, 25, 0, 48)), "today")
        self.assertEqual(_date_state("09:19", BJ(2026, 8, 25, 15, 0)), "today")

    def test_yesterday_and_before_yesterday(self):
        self.assertEqual(_date_state("昨天 19:58"), "nottoday")
        self.assertEqual(_date_state("昨天"), "nottoday")
        self.assertEqual(_date_state("前天"), "nottoday")

    def test_explicit_dates(self):
        self.assertEqual(_date_state("8月24日", BJ(2026, 8, 25, 10, 0)), "nottoday")
        self.assertEqual(_date_state("8月25日", BJ(2026, 8, 25, 10, 0)), "today")
        self.assertEqual(_date_state("2026-08-24", BJ(2026, 8, 25, 10, 0)), "nottoday")
        self.assertEqual(_date_state("2026-08-25", BJ(2026, 8, 25, 10, 0)), "today")
        self.assertEqual(_date_state("8/24", BJ(2026, 8, 25, 10, 0)), "nottoday")

    def test_just_now_and_today_markers(self):
        self.assertEqual(_date_state("刚刚"), "today")
        self.assertEqual(_date_state("今天"), "today")
        self.assertEqual(_date_state("今天 10:04"), "today")

    def test_unreadable_or_empty_is_unknown(self):
        self.assertEqual(_date_state(""), "unknown")
        self.assertEqual(_date_state(None), "unknown")
        self.assertEqual(_date_state("   "), "unknown")
        self.assertEqual(_date_state("乱七八糟"), "unknown")


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
    """新签名 already_present(norm_msg, own_last_text, list_state, own_state)。

    核心：只有「本人今天已发过相同内容」才跳过；会话列表今天（可能是对方今天回复）
    绝不等于本人今天已发；本人气泡时间读不到 -> 保守跳过（宁漏发）。
    """

    def test_own_yesterday_partner_replied_today_must_send(self):
        # 要求#3：本人昨天发🔥、对方今天回复 -> 列表时间 today 不能当「本人今天已发」。
        # 本人气泡时间不是今天 -> 必须返回 False（发送），火花才能续上。
        self.assertFalse(
            already_present("今日火花", "今日火花", "today", "nottoday")
        )
        self.assertFalse(
            already_present("今日火花", "今日火花", "unknown", "nottoday")
        )

    def test_own_same_content_today_must_skip(self):
        # 要求#4：本人今天已发过同内容 -> 跳过，绝不重复发
        self.assertTrue(
            already_present("今日火花", "今日火花", "today", "today")
        )
        self.assertTrue(
            already_present("今日火花", "今日火花", "unknown", "today")
        )

    def test_unreadable_own_date_must_skip(self):
        # 要求#5：本人气泡时间读不到/无法解析 -> 不能证明属于今天，保守跳过
        self.assertTrue(
            already_present("今日火花", "今日火花", "today", "unknown")
        )
        self.assertTrue(
            already_present("今日火花", "今日火花", "unknown", "unknown")
        )

    def test_list_nottoday_short_circuits_send(self):
        # 会话最后消息明确非今天 -> 本人气泡只可能更早，今天照发（即使 own_state 读不到）
        self.assertFalse(
            already_present("今日火花", "今日火花", "nottoday", "today")
        )
        self.assertFalse(
            already_present("今日火花", "今日火花", "nottoday", "unknown")
        )

    def test_different_content_sends(self):
        self.assertFalse(
            already_present("今日火花", "其他消息", "today", "today")
        )

    def test_empty_own_last_text_sends(self):
        # 读不到本人气泡文本 -> 无从证明重复，发送（宁漏发：不证明是重复就不跳过）
        self.assertFalse(
            already_present("今日火花", "", "today", "today")
        )

    def test_content_equality_after_emoji_strip(self):
        # 表情码渲染差异经 visible_compact 归一后可正确对比
        self.assertTrue(
            already_present("今日火花", "[盖瑞]今日火花[加一]", "today", "today")
        )
        self.assertFalse(
            already_present("今日火花", "[盖瑞]今日火花[加一]", "today", "nottoday")
        )


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
