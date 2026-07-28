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
        for item in json_data.get("data", []):
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
    if not selectors:
        return None, None

    combined_selector = ', '.join(selectors)
    if combined_selector:
        locator = page.locator(combined_selector).first
        try:
            locator.wait_for(state='visible', timeout=timeout)
            return combined_selector, locator
        except PlaywrightTimeoutError:
            pass

    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state='visible', timeout=timeout)
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
        page.wait_for_load_state('domcontentloaded', timeout=30000)
    except PlaywrightTimeoutError:
        logger.warning(f'?? {username} ?? DOMContentLoaded ?????????')

    current_url = page.url.lower()
    if '/chat' not in current_url:
        dump_debug_artifacts(page, username, 'not-on-chat-page')
        raise RuntimeError(
            f'?? {username} ???????????????: {page.url}'
        )

    list_selector, _ = first_visible_locator(page, CONVERSATION_LIST_SELECTORS, timeout=10000)
    if list_selector:
        logger.debug(f'?? {username} ???????????: {list_selector}')
        return list_selector

    title = ''
    body_text = ''
    try:
        title = page.title()
        body_text = page.locator('body').inner_text(timeout=3000)[:500]
    except Exception:
        pass

    dump_debug_artifacts(page, username, 'chat-list-not-found')

    if 'login' in page.url.lower() or '??' in body_text or '???' in body_text:
        raise RuntimeError(
            f'?? {username} ?????????? Cookie ??????????'
            '????? Cookie ?????'
        )

    raise RuntimeError(
        f'?? {username} ????????????: {title!r}?'
        f'??????: {body_text!r}'
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
                context.set_default_navigation_timeout(config['browserTimeout'])
                context.set_default_timeout(config['browserTimeout'])
                page = context.new_page()
                page.on('response', handle_response)
                context.add_cookies(cookies)

                retry_operation(
                    '??????????',
                    page.goto,
                    retries=config['taskRetryTimes'],
                    delay=5,
                    url='https://www.douyin.com/chat',
                    wait_until='domcontentloaded',
                    timeout=min(config['browserTimeout'], 60000),
                )

                if '/chat' not in page.url.lower():
                    dump_debug_artifacts(page, username, 'redirected-away-from-chat')
                    raise RuntimeError(
                        f'?? {username} ???????????????: {page.url}'
                    )

                list_selector = wait_for_chat_ready(page, username)
                logger.debug(f'?? {username} ??????')

                for target_symbol in scroll_and_select_user(page, username, targets, list_selector):
                    logger.debug(f'?? {username} ????? {target_symbol} ????')
                    _, chat_input = first_visible_locator(
                        page, CHAT_EDITOR_SELECTORS, timeout=config['browserTimeout']
                    )
                    if chat_input is None:
                        dump_debug_artifacts(page, username, 'chat-editor-not-found')
                        raise RuntimeError(f'?? {username} ????????')                    message = build_message()
                    lines = message.replace(\"\n\", chr(10)).splitlines() or [message]
                    for index, line in enumerate(lines):
                        chat_input.type(line)
                        if index != len(lines) - 1:
                            chat_input.press("Shift+Enter")
                    logger.debug(f"Sending message to {target_symbol}:
	{message}")
                    chat_input.press("Enter")
                    logger.debug(f'?? {username} ??? {target_symbol} ??????')
                    time.sleep(2)
            finally:
                context.close()


def runTasks():
    global userIDDict
    if not userData:
        raise RuntimeError('??????????????? TASKS ? cookies_* ????')

    playwright, browser = get_browser()
    try:
        logger.info('??????')
        logger.debug('???????')
        logger.debug(f"????: {config.get('messageTemplate', '???????')}")
        logger.debug(f"????: {config['hitokotoTypes']}")
        for user in userData:
            logger.debug(
                f"??: {user.get('username', '????')}, ????: {user['targets']}"
            )
        for user in userData:
            userIDDict = {}
            cookies = user['cookies']
            targets = user['targets']
            username = user.get('username', '????')
            logger.info(f'?????? {username}')
            do_user_task(browser, username, cookies, targets)
            logger.info(f'?? {username} ????')
    finally:
        browser.close()
        playwright.stop()