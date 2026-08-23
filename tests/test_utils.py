# -*- coding: utf-8 -*-
"""utils/__init__.py 的单元测试：norm、严格标题匹配。"""
import unittest

from utils import norm, strict_title_match, title_matches_aliases


class TestNorm(unittest.TestCase):
    def test_nfkc_math_bold(self):
        # 数学粗体字母在 NFKC 后折叠成普通字母
        self.assertEqual(norm("𝓓𝓻𝓮𝓪𝓶."), "Dream.")

    def test_fullwidth_space_to_half(self):
        self.assertEqual(norm("A　B"), "A B")

    def test_nbsp_to_space(self):
        self.assertEqual(norm("A\xa0B"), "A B")

    def test_zero_width_removed(self):
        self.assertEqual(norm("A​B"), "AB")
        self.assertEqual(norm("A﻿B"), "AB")

    def test_collapses_whitespace_and_strips(self):
        self.assertEqual(norm("  A\t\tB\n\nC  "), "A B C")

    def test_non_string_coerced(self):
        self.assertEqual(norm(123), "123")

    def test_pua_apple_logo_kept(self):
        # PUA 字符无兼容分解，norm 保留它；两侧若都带该字符则相等，可精确匹配
        self.assertEqual(norm("Liu Beixi"), "Liu Beixi")


class TestStrictTitleMatch(unittest.TestCase):
    def test_exact_equal(self):
        self.assertTrue(strict_title_match("Meteor.💫(梁亚楠男友)", ["Meteor.💫(梁亚楠男友)"]))
        # 数学粗体 NFKC 后 -> "Dream.(罗致蘅)"，两侧一致 -> 精确匹配
        self.assertTrue(strict_title_match("𝓓𝓻𝓮𝓪𝓶.(罗致蘅)", ["𝓓𝓻𝓮𝓪𝓶.(罗致蘅)"]))

    def test_rejects_substring_of_alias(self):
        # 表头 "Dream." 只是别名 "Dream.(罗致蘅)" 的子串：严格匹配必须拒绝（旧代码会误判通过）
        self.assertFalse(strict_title_match("Dream.", ["𝓓𝓻𝓮𝓪𝓶.(罗致蘅)"]))

    def test_pua_apple_requires_both_sides(self):
        # 一侧带 PUA 一侧不带，不算精确匹配
        self.assertTrue(strict_title_match("Liu Beixi", ["Liu Beixi"]))
        self.assertFalse(strict_title_match("Liu Beixi", ["Liu Beixi"]))

    def test_rejects_substring(self):
        # 表头是超集/子集都不算严格匹配，防止误发
        self.assertFalse(strict_title_match("Liu Beixi abc", ["Liu Beixi"]))
        self.assertFalse(strict_title_match("Dream.", ["Dream." + "额外后缀"]))

    def test_rejects_partial_overlap(self):
        self.assertFalse(strict_title_match("晴雾", ["晴雾屿鸢(梁亚楠)"]))

    def test_whitespace_insensitive_after_norm(self):
        self.assertTrue(strict_title_match("A B", ["A\xa0B"]))


class TestTitleMatchesAliases(unittest.TestCase):
    def test_matches_norm_alias(self):
        # 列表标题与别名两侧都做 NFKC，得逐字符相等才算命中
        aliases = [norm("𝓓𝓻𝓮𝓪𝓶.(罗致蘅)")]
        self.assertTrue(title_matches_aliases("𝓓𝓻𝓮𝓪𝓶.(罗致蘅)", aliases))
        self.assertFalse(title_matches_aliases("Dream.", aliases))

    def test_no_substring_match(self):
        aliases = [norm("晴雾屿鸢(梁亚楠)")]
        self.assertFalse(title_matches_aliases("晴雾", aliases))
        self.assertFalse(title_matches_aliases("晴雾屿鸢", aliases))

    def test_multiple_aliases(self):
        aliases = [norm(a) for a in ["备注名", "列表标题"]]
        self.assertTrue(title_matches_aliases("列表标题", aliases))
        self.assertFalse(title_matches_aliases("列表", aliases))


if __name__ == "__main__":
    unittest.main()
