# -*- coding: utf-8 -*-
"""core/tasks.py 发送确认 / 发送前去重决策逻辑（纯函数）的单元测试。

覆盖「宁可漏发也绝不误发/重复发」在跨零点、证据冲突、读取失败、表情码、换行混排下的决策：
- _date_state：三态时间判定（today / nottoday / unknown）。'X分钟前/X小时前' 是相对时间
  非精确值，按向下取整区间 [n, n+1) 保守判定：整体今天=today、整体前一天=nottoday、
  跨日边界=unknown（宁漏发，绝不把边界误判成「今天」而漏发，也不放行重发）。
- visible_compact：去表情码 + 去字面转义 + 去空白，跨渲染差异可对比。
- already_present：返回 'send' | 'dedup_skip' | 'uncertain_skip'。只有「本人今天已发过
  相同内容」才构成重复；本人时间今天无条件跳过（即使列表时间自相矛盾）；本人气泡读取
  失败必须 uncertain_skip（无法排除已发）；列表时间只用于「明确非今天 -> 放行」。
- confirm_signals：多信号确认（预览匹配 / 时间戳翻转「刚刚」/ 气泡匹配），全不命中 -> False。

跨零点核心语义：北京时间 00:07 的云端运行看到会话列表「2小时前」= 前一天本人发的 🔥，
不是今天——绝不能据此跳过。列表时间是「会话最后一条消息」的时间（可能是对方今天回复），
判定「本人今天是否已发」必须看本人气泡自己的时间戳状态（own_state）。
"""
import unittest
from datetime import datetime, timedelta, timezone

from core.tasks import (
    visible_compact,
    already_present,
    confirm_signals,
    _date_state,
    list_preview_shows_same_today,
)

# 北京时区（与 tasks.CN_TZ 一致：UTC+8，无夏令时）
CN_TZ = timezone(timedelta(hours=8))
BJ = lambda *a: datetime(*a, tzinfo=CN_TZ)  # noqa: E731 构造北京时间（aware，贴近 runner 行为）


class TestDateState(unittest.TestCase):
    """_date_state 三态判定：00:48 看到 '2小时前' 必须是非今天（旧 bug 根因）。"""

    def test_0048_two_hours_ago_is_nottoday(self):
        # 云端实测场景：北京时间 08-25 00:48 列表显示「2小时前」。floor 区间 [21:48, 22:48)
        # 整体在 08-24 -> nottoday（旧代码无条件视为今天导致漏发，火花必断）。
        self.assertEqual(_date_state("2小时前", BJ(2026, 8, 25, 0, 48)), "nottoday")

    def test_0048_one_hour_ago_is_nottoday(self):
        # floor 区间 [22:48, 23:48) 整体在 08-24
        self.assertEqual(_date_state("1小时前", BJ(2026, 8, 25, 0, 48)), "nottoday")

    def test_0048_ten_min_ago_is_today(self):
        # floor 区间 [00:37, 00:38] 整体今天
        self.assertEqual(_date_state("10分钟前", BJ(2026, 8, 25, 0, 48)), "today")

    def test_0048_forty_eight_min_ago_span_is_unknown(self):
        # 「48分钟前」floor 区间 [23:59, 00:00] 跨日边界，相对时间不精确无法可靠判定 -> unknown
        self.assertEqual(_date_state("48分钟前", BJ(2026, 8, 25, 0, 48)), "unknown")

    def test_0048_forty_nine_min_ago_is_nottoday(self):
        # floor 区间 [23:58, 23:59] 整体前一天
        self.assertEqual(_date_state("49分钟前", BJ(2026, 8, 25, 0, 48)), "nottoday")

    def test_0005_five_min_ago_span_is_unknown(self):
        # 00:05 见「5分钟前」floor 区间 [23:59, 00:00] 跨日边界 -> unknown（宁漏发）
        self.assertEqual(_date_state("5分钟前", BJ(2026, 8, 25, 0, 5)), "unknown")

    def test_0200_one_hour_ago_whole_bucket_today(self):
        # 02:00 见「1小时前」floor 区间 [00:00, 01:00] 整体今天
        self.assertEqual(_date_state("1小时前", BJ(2026, 8, 25, 2, 0)), "today")

    def test_2300_hours_before_midnight_still_today(self):
        # 23:00 见「1小时前」= [21:00,22:00) 今天；「2小时前」= [20:00,21:00) 今天
        self.assertEqual(_date_state("1小时前", BJ(2026, 8, 25, 23, 0)), "today")
        self.assertEqual(_date_state("2小时前", BJ(2026, 8, 25, 23, 0)), "today")

    def test_0030_next_day_one_hour_ago_is_nottoday(self):
        # 新一天 00:30 见「1小时前」floor 区间 [22:30, 23:30) 整体前一天
        self.assertEqual(_date_state("1小时前", BJ(2026, 8, 26, 0, 30)), "nottoday")

    def test_hhmm_after_now_is_nottoday(self):
        # HH:MM 是精确展示，晚于当前时刻必是前一天（时间不会来自未来）
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
    """already_present(norm_msg, own_last_text, own_read_ok, list_state, own_state)
    -> 'send' | 'dedup_skip' | 'uncertain_skip'。

    核心：只有「本人今天已发过相同内容」才跳过；本人时间今天无条件跳过（列表自相矛盾也
    不放行）；本人气泡读取失败必须保守跳过（无法排除已发）；列表今天绝不等于本人今天已发。
    """

    def test_own_yesterday_partner_replied_today_must_send(self):
        # 本人昨天发🔥、对方今天回复 -> 列表时间 today 不能当「本人今天已发」。
        # 本人气泡时间不是今天 -> 必须 send，火花才能续上。
        self.assertEqual(
            already_present("今日火花", "今日火花", True, "today", "nottoday"), "send"
        )
        self.assertEqual(
            already_present("今日火花", "今日火花", True, "unknown", "nottoday"), "send"
        )

    def test_own_same_content_today_must_skip(self):
        # 本人今天已发过同内容 -> dedup_skip，绝不重复发
        self.assertEqual(
            already_present("今日火花", "今日火花", True, "today", "today"), "dedup_skip"
        )
        self.assertEqual(
            already_present("今日火花", "今日火花", True, "unknown", "today"), "dedup_skip"
        )

    def test_own_today_list_nottoday_conflict_is_uncertain(self):
        # 证据冲突：本人气泡今天 但 列表非今天，无法可靠判定 -> uncertain_skip，
        # 绝不因列表矛盾就放行发送（这是审查发现的放行漏洞，测试固化其关闭）
        self.assertEqual(
            already_present("今日火花", "今日火花", True, "nottoday", "today"),
            "uncertain_skip",
        )

    def test_own_unknown_list_today_is_uncertain(self):
        # 本人气泡时间读不到 + 列表今天 -> 无法证明本人今天已发，保守跳过
        self.assertEqual(
            already_present("今日火花", "今日火花", True, "today", "unknown"),
            "uncertain_skip",
        )
        self.assertEqual(
            already_present("今日火花", "今日火花", True, "unknown", "unknown"),
            "uncertain_skip",
        )

    def test_own_unknown_list_nottoday_sends(self):
        # 本人气泡时间读不到 + 列表明确非今天 -> 本人气泡（属本会话）只可能更早，send
        self.assertEqual(
            already_present("今日火花", "今日火花", True, "nottoday", "unknown"), "send"
        )

    def test_empty_own_last_text_confirmed_no_bubble_sends(self):
        # 确认读取成功（own_read_ok=True）且面板无本人气泡 -> 非重复，send
        self.assertEqual(
            already_present("今日火花", "", True, "today", "today"), "send"
        )

    def test_empty_own_last_text_read_failed_is_uncertain(self):
        # 读取失败（own_read_ok=False）无法排除已发同内容 -> 必须 uncertain_skip，
        # 即使列表时间/本人时间都显示非今天（读取失败优先于一切放行证据）
        self.assertEqual(
            already_present("今日火花", "", False, "today", "today"), "uncertain_skip"
        )
        self.assertEqual(
            already_present("今日火花", "", False, "nottoday", "nottoday"), "uncertain_skip"
        )

    def test_different_content_sends(self):
        self.assertEqual(
            already_present("今日火花", "其他消息", True, "today", "today"), "send"
        )

    def test_content_equality_after_emoji_strip(self):
        # 表情码渲染差异经 visible_compact 归一后可正确对比
        self.assertEqual(
            already_present("今日火花", "[盖瑞]今日火花[加一]", True, "today", "today"),
            "dedup_skip",
        )
        self.assertEqual(
            already_present("今日火花", "[盖瑞]今日火花[加一]", True, "today", "nottoday"),
            "send",
        )


class TestListPreviewShowsSameToday(unittest.TestCase):
    """列表预览兜底去重：预览内容 == 将发模板 且 列表时间 = 今天 -> 本人今天已发。

    独立于消息面板渲染（虚拟列表可能未渲染最新本人气泡，而列表条目是会话级权威状态）。
    只有本自动化会向这些会话写入该模板，故「预览==模板 且 今天」即可证明本人今天已发。
    """

    def test_today_same_preview_confirms(self):
        # 今天 00:07 已发 🔥、对方未回复 -> 列表预览=🔥、时间=今天 -> 本人今天已发
        self.assertTrue(list_preview_shows_same_today("🔥", "today", "🔥"))
        self.assertTrue(list_preview_shows_same_today("🔥", "today", "  🔥 "))

    def test_emoji_code_normalized(self):
        # 预览保留表情码原文，归一后与模板一致
        self.assertTrue(
            list_preview_shows_same_today("今日火花", "today", "[盖瑞]今日火花[加一]")
        )

    def test_yesterday_same_preview_does_not_confirm(self):
        # 昨天发的 🔥（列表时间=昨天）-> 今天必须照发，绝不误跳过
        self.assertFalse(list_preview_shows_same_today("🔥", "nottoday", "🔥"))

    def test_unknown_time_does_not_confirm(self):
        self.assertFalse(list_preview_shows_same_today("🔥", "unknown", "🔥"))

    def test_different_preview_does_not_confirm(self):
        # 对方今天回复了别的消息 -> 预览≠模板，不能据此判定已发（交给面板本人气泡判定）
        self.assertFalse(list_preview_shows_same_today("🔥", "today", "晚安"))
        self.assertFalse(list_preview_shows_same_today("🔥", "today", ""))

    def test_today_same_preview_wins_over_panel_inconclusive(self):
        # 即使面板本人气泡读不到（own_state=unknown），列表今天预览=模板仍应去重跳过
        # （这正是 read_newest_own_bubble 修复前同天二次运行重复发的兜底防线）
        self.assertTrue(list_preview_shows_same_today("🔥", "today", "🔥"))


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
