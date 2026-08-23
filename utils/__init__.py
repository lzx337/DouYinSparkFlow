import re
import unicodedata


def norm(s):
    """归一化文本。

    - NFKC：全角/兼容字符折叠（如数学粗体 𝓓𝓻𝓮𝓪𝓶 -> Dream.）
    - 全角空格 / 不换行空格（　、\xa0）-> 半角空格
    - 去掉零宽字符（​、﻿）
    - 折叠连续空白
    """
    if not isinstance(s, str):
        s = str(s)
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("　", " ").replace("\xa0", " ")
    s = s.replace("​", "").replace("﻿", "")
    return re.sub(r"\s+", " ", s).strip()


def norm_tight(s):
    """norm 后再移除所有空白（含换行产生的空格）。

    搜索结果标题常把「昵称(备注)」渲染成换行/带空格，如 '期安 (路心月)'，
    而别名是 '期安(路心月)'。列表标题与别名用 norm 即可；搜索结果匹配用本函数。
    """
    return "".join(norm(s).split())


def strict_title_match(title, title_aliases):
    """发送前表头确认：norm(title) 必须与某个别名 norm 后完全相等。

    只允许逐字符相等，禁止子串 / 包含匹配（避免把 "A" 误当 "AB"）。
    """
    nt = norm(title)
    return any(nt == norm(a) for a in title_aliases)


def title_matches_aliases(title, title_aliases_norm):
    """会话列表项匹配：title norm 后与一组预归一化的别名逐字符相等。"""
    nt = norm(title)
    return any(nt == a for a in title_aliases_norm)
