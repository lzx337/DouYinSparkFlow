import os, sys
from enum import Enum
import json
import logging
from utils import norm
from utils.logger import setup_logger

logger = setup_logger(level=logging.DEBUG)

"""
是否启用调试模式
更详细的日志打印，浏览器操作可视化等
"""
DEBUG = True
config = None
userData = None


class Environment(Enum):
    GITHUBACTION = "GITHUB_ACTION"  # GitHub Action 运行
    LOCAL = "LOCAL"  # 本地代码运行
    PACKED = "PACKED"  # PyInstaller 打包运行

    def __str__(self):
        return self.value


def get_environment():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Environment.PACKED
    elif os.getenv("GITHUB_ACTIONS") == "true":
        return Environment.GITHUBACTION
    else:
        return Environment.LOCAL


def get_config():
    """
    获取配置信息
    :return: 配置字典
    """
    global config

    if config:
        return config

    config = {
        "proxyAddress": os.getenv("PROXY_ADDRESS", ""),
        "messageTemplate": os.getenv("MESSAGE_TEMPLATE", "[盖瑞]今日火花[加一]\\n—— [右边] 每日一言 [左边] ——\\n[API]"),
        "hitokotoTypes": json.loads(
            os.getenv("HITOKOTO_TYPES", '["文学","影视","诗词","哲学"]')
        ),
        "matchMode": os.getenv("MATCH_MODE", "nickname"),  # 是否使用短 ID 进行好友匹配
        "browserTimeout": int(os.getenv("BROWSER_TIMEOUT", "120000")),  # 浏览器操作超时时间，单位毫秒
        "friendListTimeout": int(os.getenv("FRIEND_LIST_WAIT_TIME", "2000")),  # 好友列表加载超时时间，单位毫秒
        "taskRetryTimes": int(os.getenv("TASK_RETRY_TIMES", "3")),  # 任务重试次数
        "logLevel": os.getenv("LOG_LEVEL", "DEBUG"),  # 日志级别
        # 发送确认用的气泡选择器：真实登录页已核实为 .MessageItemTextisFromMe
        # （仅命中本人文本气泡，方向性稳定；空串注入时也回退到该默认值，保证确认开启）
        "outgoingBubbleSelector": os.getenv("OUTGOING_BUBBLE_SELECTOR") or ".MessageItemTextisFromMe",
        # 发送失败标记选择器：尚未在真实 DOM 观察到稳定的失败标记类，缺省不启用
        # （不猜测，避免误判发送失败）
        "failedBubbleSelector": os.getenv("FAILED_BUBBLE_SELECTOR", ""),
        # 会话表头标题选择器：真实页面核实后填入；不填则用推断列表，读不到表头时宁可跳过也不发送
        "chatHeaderTitleSelector": os.getenv("CHAT_HEADER_TITLE_SELECTOR", ""),
        # 只读诊断模式：搜索/点击/读表头与列表状态，但发送前停止、不按 Enter（绝不误发）
        # 用于定位「云端找不到目标」等问题，只输出结构诊断，不记录完整页面内容
        "dryRun": os.getenv("DRY_RUN", "").strip().lower() in ("1", "true", "yes"),
    }

    return config


# Playwright add_cookies 只接受这些字段；值为布尔/枚举的字段单独校验
_PLAYWRIGHT_COOKIE_FIELDS = (
    "name", "value", "domain", "path", "url", "expires",
    "httpOnly", "secure", "sameSite",
)
_VALID_SAME_SITE = ("Strict", "Lax", "None")


def sanitize_cookies(cookies):
    """清洗 cookie 列表，使其符合 Playwright context.add_cookies 的字段要求。

    - 保留合法字段（含 sameSite；Playwright 支持 Strict / Lax / None）
    - expirationDate（部分导出工具使用 CDP 命名）统一为 expires
    - 剔除 Playwright 不接受的字段（hostOnly / storeId / session 等）
    - sameSite 值非法时丢弃该字段；expires 只接受数字

    注意：不在此处改变任何 Cookie 的值，也不把 Cookie 写入任何文件。
    """
    cleaned = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        c = {}

        expires = cookie.get("expires", cookie.get("expirationDate"))
        if isinstance(expires, (int, float)) and not isinstance(expires, bool):
            c["expires"] = int(expires) if float(expires).is_integer() else float(expires)

        for field in ("name", "value", "domain", "path", "url"):
            if cookie.get(field) is not None:
                c[field] = cookie[field]
        for flag in ("httpOnly", "secure"):
            if isinstance(cookie.get(flag), bool):
                c[flag] = cookie[flag]

        same_site = cookie.get("sameSite")
        if same_site in _VALID_SAME_SITE:
            c["sameSite"] = same_site

        if "name" in c and "value" in c:
            cleaned.append(c)
    return cleaned


def parse_targets(raw_targets):
    """把 TASKS 里的 targets 解析为规范结构，兼容新旧两种格式。

    旧格式（兼容）：["好友名1", "好友名2"]
    新格式：       [{"id": "好友名", "search_terms": ["搜索词", ...],
                     "title_aliases": ["标题别名", ...]}]
      - search_terms：填入搜索框用的词（可多个，逐个尝试）
      - title_aliases：用于会话列表/表头的严格标题匹配（必须逐一与真实标题一致）
      - id：仅用于日志与去重

    返回列表，每项包含 id / search_terms / search_terms_norm /
    title_aliases / title_aliases_norm。解析失败（缺字段/空列表）的项会被跳过并告警。
    """
    if not isinstance(raw_targets, list):
        logger.warning(f"targets 不是列表，已忽略: {raw_targets!r}")
        return []

    parsed = []
    for item in raw_targets:
        if isinstance(item, str):
            n = norm(item)
            if not n:
                logger.warning(f"target 为空字符串，已跳过")
                continue
            parsed.append(
                {
                    "id": n,
                    "search_terms": [item],
                    "search_terms_norm": [n],
                    "title_aliases": [item],
                    "title_aliases_norm": [n],
                }
            )
        elif isinstance(item, dict):
            search_terms = item.get("search_terms")
            title_aliases = item.get("title_aliases")
            if not isinstance(search_terms, list) or not search_terms:
                logger.warning(f"target 缺少有效 search_terms，已跳过: {item!r}")
                continue
            if not isinstance(title_aliases, list) or not title_aliases:
                logger.warning(f"target 缺少有效 title_aliases，已跳过: {item!r}")
                continue
            st = [x for x in search_terms if isinstance(x, str) and norm(x)]
            ta = [x for x in title_aliases if isinstance(x, str) and norm(x)]
            if not st or not ta:
                logger.warning(f"target 的 search_terms/title_aliases 无有效字符串，已跳过: {item!r}")
                continue
            parsed.append(
                {
                    "id": norm(str(item.get("id") or st[0])),
                    "search_terms": st,
                    "search_terms_norm": [norm(x) for x in st],
                    "title_aliases": ta,
                    "title_aliases_norm": [norm(x) for x in ta],
                }
            )
        else:
            logger.warning(f"target 格式不支持，已跳过: {item!r}")
    return parsed


def get_userData():
    """
    获取用户数据目录
    :return: 用户数据目录路径
    """
    global userData

    if userData:
        return userData

    try:
        tasks = json.loads(os.getenv("TASKS", "[]"))
    except json.JSONDecodeError:
        logger.warning("TASKS 环境变量不是合法 JSON，已按空处理")
        tasks = []

    userData = []

    for task in tasks:
        username = task.get("username", "未知用户")
        unique_id = task.get("unique_id")
        if not unique_id:
            logger.warning(f"{username} 的任务缺少 unique_id 字段，已跳过")
            continue
        cookies_key = f"cookies_{unique_id}".upper()
        cookies_str = os.getenv(cookies_key, "")
        cookies = None
        if not cookies_str:
            # 仅本地 + 指定了 DOUYIN_PROFILE_PATH 时允许无 Cookie：登录态在 profile 里
            if get_environment() == Environment.LOCAL and os.getenv("DOUYIN_PROFILE_PATH", "").strip():
                cookies = []
                logger.warning(
                    f"{username} 本地 profile 模式：未提供 {cookies_key}，以 profile 登录态为准"
                )
            else:
                logger.warning(f"{username} 的任务缺少 {cookies_key} 环境变量，已跳过")
                continue
        else:
            try:
                cookies = json.loads(cookies_str)
            except json.JSONDecodeError:
                logger.warning(f"{username} 的任务 {cookies_key} 格式不正确，已跳过")
                continue

        targets = parse_targets(task.get("targets", []))

        userData.append(
            {
                "unique_id": unique_id,
                "username": username,
                "cookies": sanitize_cookies(cookies),
                "targets": targets,
            }
        )

    return userData
