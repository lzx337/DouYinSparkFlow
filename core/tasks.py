import os
import time
import traceback

from playwright.sync_api import Response, TimeoutError as PlaywrightTimeoutError

from core.browser import get_browser
from core.msg_builder import build_message
from utils import norm
from utils.config import get_config, get_userData
from utils.logger import setup_logger


config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))
userIDDict = {}

CONVERSATION_ITEM_SELECTORS = [
    ".conversationConversationItemwrapper",
    "[class*='conversation'][class*='Item']",
    "[class*='Conversation'][class*='Item']",
]
CONVERSATION_TITLE_SELECTORS = [
    ".conversationConversationItemtitle",
    "[class*='conversation'][class*='title']",
    "[class*='Conversation'][class*='title']",
]
CONVERSATION_LIST_SELECTORS = [
    ".conversationConversationListwrapper",
    "[class*='conversation'][class*='List']",
    "[class*='Conversation'][class*='List']",
    "[role='list']",
]
CHAT_EDITOR_SELECTORS = [
    ".messageEditorimChatEditorContainer",
    "[contenteditable='true']",
    "textarea",
]


def handle_response(response: Response):
    """Cache user ids returned by Douyin IM user info API."""
    global userIDDict
    if "aweme/v1/web/im/user/info" not in response.url:
        return

    try:
        json_data = response.json()
        for item in (json_data.get("data") or []):
            short_id = item.get("short_id")
            unique_id = item.get("unique_id")
            sec_uid = item.get("sec_uid", "")
            nickname = norm(item.get("nickname"))
            remark_name = norm(item.get("remark_name", nickname))
            userIDDict[remark_name] = [
                short_id,
                unique_id,
                sec_uid,
                nickname,
                remark_name,
            ]
    except Exception as e:
        tb = traceback.extract_tb(e.__traceback__)
        last = tb[-1]
        logger.warning(
            f"解析抖音用户信息响应失败: {e} ({last.filename}:{last.lineno} {last.name})"
        )


def retry_operation(name, operation, retries=3, delay=2, *args, **kwargs):
    for attempt in range(retries):
        try:
            return operation(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"{name} 失败，正在重试第 {attempt + 1} 次，错误：{e}")
                time.sleep(delay)
            else:
                logger.error(f"{name} 失败，已达到最大重试次数，错误：{e}")
                raise


def first_visible_locator(page, selectors, timeout=5000):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=timeout)
            return selector, locator
        except PlaywrightTimeoutError:
            continue
    return None, None


def dump_debug_artifacts(page, username, reason):
    safe_reason = "".join(c if c.isalnum() or c in "-_" else "_" for c in reason)[:40]
    safe_username = "".join(c if c.isalnum() or c in "-_" else "_" for c in username)[:40]
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    debug_dir = os.path.join("logs", "debug")
    os.makedirs(debug_dir, exist_ok=True)
    base_path = os.path.join(debug_dir, f"{safe_username}-{safe_reason}-{timestamp}")

    try:
        page.screenshot(path=f"{base_path}.png", full_page=True)
    except Exception as e:
        logger.warning(f"保存页面截图失败: {e}")

    try:
        with open(f"{base_path}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception as e:
        logger.warning(f"保存页面 HTML 失败: {e}")

    logger.warning(
        f"已保存调试文件: {base_path}.png / {base_path}.html，当前 URL: {page.url}"
    )


def wait_for_chat_ready(page, username):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        logger.warning(f"账号 {username} 等待 DOMContentLoaded 超时，继续检查页面")

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        logger.debug(f"账号 {username} networkidle 超时，可能是抖音长连接导致，继续检查页面")

    list_selector, _ = first_visible_locator(page, CONVERSATION_LIST_SELECTORS, timeout=15000)
    if list_selector:
        logger.debug(f"账号 {username} 聊天列表已加载，选择器: {list_selector}")
        return list_selector

    title = ""
    body_text = ""
    try:
        title = page.title()
        body_text = page.locator("body").inner_text(timeout=3000)[:500]
    except Exception:
        pass

    dump_debug_artifacts(page, username, "chat-list-not-found")

    if "login" in page.url.lower() or "登录" in body_text or "验证码" in body_text:
        raise RuntimeError(
            f"账号 {username} 未进入聊天列表，疑似 Cookie 失效或需要验证登录。"
            "请重新获取 Cookie 后再运行。"
        )

    raise RuntimeError(
        f"账号 {username} 未找到聊天列表。页面标题: {title!r}，"
        f"页面文本片段: {body_text!r}"
    )


def checkTargetName(targetName, targets):
    target_symbol = None
    targetName = norm(targetName)
    if targetName in userIDDict:
        matched = next((v for v in userIDDict[targetName] if v and v in targets), None)
        if matched is not None:
            target_symbol = matched
    elif targetName in targets:
        target_symbol = targetName
    return target_symbol


def get_item_title(element):
    for selector in CONVERSATION_TITLE_SELECTORS:
        try:
            title = element.locator(selector).first.inner_text(timeout=2000)
            if title:
                return title
        except Exception:
            continue
    try:
        return element.inner_text(timeout=2000).splitlines()[0]
    except Exception:
        return ""


def scroll_and_select_user(page, username, targets, list_selector):
    logger.debug(f"账号 {username} 开始查找目标好友列表")
    logger.debug(f"账号 {username} 目标好友列表: {targets}")
    found_targets = set()
    remaining_targets = set(targets)
    empty_scroll_count = 0
    max_empty_scrolls = 10
    item_selector = CONVERSATION_ITEM_SELECTORS[0]

    for selector in CONVERSATION_ITEM_SELECTORS:
        try:
            if page.locator(selector).count() > 0:
                item_selector = selector
                break
        except Exception:
            continue

    while True:
        target_elements = page.locator(item_selector).all()
        if not target_elements:
            logger.warning(f"账号 {username} 当前未发现任何聊天项，选择器: {item_selector}")

        prev_found_count = len(found_targets)
        for element in target_elements:
            try:
                target_name = get_item_title(element)
                if not target_name or target_name in found_targets:
                    continue
                found_targets.add(target_name)
                logger.debug(f"账号 {username} 找到好友 {target_name}")
                target_symbol = checkTargetName(target_name, targets)
                if target_symbol:
                    element.click()
                    yield target_symbol
                    remaining_targets.discard(target_symbol)
                    if len(remaining_targets) == 0:
                        logger.debug(f"账号 {username} 所有目标好友均已找到，停止搜索")
                        return
                    break
            except Exception:
                traceback.print_exc()
        else:
            if len(found_targets) > prev_found_count:
                empty_scroll_count = 0
            else:
                empty_scroll_count += 1

            if empty_scroll_count >= max_empty_scrolls:
                logger.warning(
                    f"账号 {username} 连续 {max_empty_scrolls} 次滚动未发现新好友，判定已到达底部"
                )
                if len(remaining_targets) > 0:
                    logger.warning(
                        f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}"
                    )
                break

            try:
                scrollable_element = page.locator(list_selector).first.element_handle(timeout=3000)
            except PlaywrightTimeoutError:
                logger.error(f"账号 {username} 未找到滚动容器，退出")
                dump_debug_artifacts(page, username, "scroll-container-not-found")
                break

            if scrollable_element:
                scroll_top_before = page.evaluate(
                    "(element) => element.scrollTop", scrollable_element
                )
                page.evaluate(
                    "(element) => element.scrollTop += 800", scrollable_element
                )
                time.sleep(0.3)
                scroll_top_after = page.evaluate(
                    "(element) => element.scrollTop", scrollable_element
                )
                if scroll_top_before == scroll_top_after:
                    empty_scroll_count += 2
                    logger.debug(
                        f"账号 {username} scrollTop 未变化 ({scroll_top_before})，可能已到底 "
                        f"(空滚动计数: {empty_scroll_count}/{max_empty_scrolls})"
                    )
                else:
                    logger.debug(
                        f"账号 {username} 滚动好友列表以加载更多好友 "
                        f"(scrollTop: {scroll_top_before} -> {scroll_top_after})"
                    )
                time.sleep(1.5)


def do_user_task(browser, username, cookies, targets):
    context = browser.new_context()
    try:
        context.set_default_navigation_timeout(config["browserTimeout"])
        context.set_default_timeout(config["browserTimeout"])
        page = context.new_page()
        page.on("response", handle_response)
        context.add_cookies(cookies)

        retry_operation(
            "打开抖音网页聊天页面",
            page.goto,
            retries=config["taskRetryTimes"],
            delay=5,
            url="https://www.douyin.com/chat",
            wait_until="domcontentloaded",
            timeout=min(config["browserTimeout"], 60000),
        )

        list_selector = wait_for_chat_ready(page, username)
        logger.debug(f"账号 {username} 开始发送消息")

        for target_symbol in scroll_and_select_user(page, username, targets, list_selector):
            logger.debug(f"账号 {username} 已选中好友 {target_symbol} 发送消息")
            _, chat_input = first_visible_locator(
                page, CHAT_EDITOR_SELECTORS, timeout=config["browserTimeout"]
            )
            if chat_input is None:
                dump_debug_artifacts(page, username, "chat-editor-not-found")
                raise RuntimeError(f"账号 {username} 未找到聊天输入框")

            message = build_message()
            lines = message.replace("\\\\n", chr(10)).splitlines() or [message]
            for index, line in enumerate(lines):
                chat_input.type(line)
                if index != len(lines) - 1:
                    chat_input.press("Shift+Enter")
            logger.debug(f"账号 {username} 准备发送消息给好友 {target_symbol}：\n\t{message}")
            chat_input.press("Enter")
            logger.debug(f"账号 {username} 给好友 {target_symbol} 发送消息完成")
            time.sleep(2)
    finally:
        context.close()


def runTasks():
    playwright, browser = get_browser()
    try:
        logger.info("开始执行任务")
        logger.debug("当前配置如下：")
        logger.debug(f"消息模板: {config.get('messageTemplate', '未找到消息模板')}")
        logger.debug(f"一言类型: {config['hitokotoTypes']}")
        for user in userData:
            logger.debug(
                f"用户: {user.get('username', '未知用户')}, 目标好友: {user['targets']}"
            )
        for user in userData:
            cookies = user["cookies"]
            targets = user["targets"]
            username = user.get("username", "未知用户")
            logger.info(f"开始处理账号 {username}")
            do_user_task(browser, username, cookies, targets)
            logger.info(f"账号 {username} 任务完成")
    finally:
        browser.close()
        playwright.stop()

