import traceback
from utils.logger import setup_logger
from utils.config import get_config, get_userData
from utils import norm
from core.msg_builder import build_message, build_message_with_openai
from core.browser import get_browser
from playwright.sync_api import Response
import time
config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))
matchMode = config.get("matchMode", "nickname")
userIDDict = {}
CONVERSATION_ITEM_SELECTOR = ".conversationConversationItemwrapper"
CONVERSATION_TITLE_SELECTOR = ".conversationConversationItemtitle"
CONVERSATION_LIST_SELECTOR = ".conversationConversationListwrapper"
CHAT_EDITOR_SELECTOR = ".messageEditorimChatEditorContainer"
def handle_response(response: Response):
    """
    只监听你要的那个接口响应
    """
    global userIDDict
    # 精准匹配目标接口 URL
    if "aweme/v1/web/im/user/info" in response.url:
        try:
            json_data = response.json()
            for item in json_data.get("data", []):
                short_id = item.get("short_id")
                unique_id = item.get("unique_id")
                sec_uid = item.get("sec_uid", "")
                nickname = norm(item.get("nickname"))
                remark_name = norm(item.get("remark_name", nickname))
                userIDDict[remark_name] = [short_id, unique_id, sec_uid, nickname, remark_name]
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)
            last = tb[-1]
            print(f"解析响应失败: {e}")
            print(f"文件: {last.filename}, 行号: {last.lineno}, 函数: {last.name}")
def retry_operation(name, operation, retries=3, delay=2, *args, **kwargs):
    """
    通用的重试逻辑
    :param name: 操作名称（用于日志记录）
    :param operation: 要执行的异步操作
    :param retries: 最大重试次数
    :param delay: 每次重试之间的延迟（秒）
    :param args: 传递给操作的参数
    :param kwargs: 传递给操作的关键字参数
    """
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
def checkTargetName(targetName, targets):
    """检查targetName是否为目标
    """
    targetSymbol = None
    targetName = norm(targetName)
    if targetName in userIDDict:
        matched = next((v for v in userIDDict[targetName] if v and v in targets), None)
        if matched is not None:
            targetSymbol = matched
    else:
        if targetName in targets:
            targetSymbol = targetName
    return targetSymbol
def scroll_and_select_user(page, username, targets):
    """尝试滚动并查找用户名"""
    # 定义目标元素和滚动容器的选择器
    target_selector = CONVERSATION_ITEM_SELECTOR
    scrollable_friends_selector = CONVERSATION_LIST_SELECTOR
    logger.debug(f"账号 {username} 开始查找目标好友列表")
    logger.debug(f"账号 {username} 目标好友列表: {targets}")
    found_targets = set()
    remaining_targets = set(targets)
    empty_scroll_count = 0
    MAX_EMPTY_SCROLLS = 10
    while True:
        target_elements = page.locator(target_selector).all()
        prev_found_count = len(found_targets)
        for element in target_elements:
            try:
                span = element.locator(CONVERSATION_TITLE_SELECTOR)
                targetName = span.inner_text()
                if targetName in found_targets:
                    continue
                found_targets.add(targetName)
                logger.debug(f"账号 {username} 找到好友 {targetName}")
                targetSymbol = checkTargetName(targetName, targets)
                if targetSymbol:
                    element.click()
                    yield targetSymbol
                    if targetSymbol in remaining_targets:
                        remaining_targets.remove(targetSymbol)
                    if len(remaining_targets) == 0:
                        logger.debug(f"账号 {username} 所有目标好友均已找到，停止搜索")
                        return
                    break
            except Exception as e:
                traceback.print_exc()
        else:
            new_found = len(found_targets) > prev_found_count
            if new_found:
                empty_scroll_count = 0
            else:
                empty_scroll_count += 1
            if empty_scroll_count >= MAX_EMPTY_SCROLLS:
                logger.warning(
                    f"账号 {username} 连续 {MAX_EMPTY_SCROLLS} 次滚动未发现新好友，判定已到达底部"
                )
                if len(remaining_targets) > 0:
                    logger.warning(
                        f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}"
                    )
                break
            scrollable_element = page.locator(
                scrollable_friends_selector
            ).element_handle()
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
                        f"账号 {username} scrollTop 未变化 ({scroll_top_before})，可能已到底 (空滚动计数: {empty_scroll_count}/{MAX_EMPTY_SCROLLS})"
                    )
                else:
                    logger.debug(
                        f"账号 {username} 滚动好友列表以加载更多好友 (scrollTop: {scroll_top_before} -> {scroll_top_after})"
                    )
                time.sleep(1.5)
            else:
                logger.error(f"账号 {username} 未找到滚动容器，退出")
                break
def do_user_task(browser, username, cookies, targets):
    context = browser.new_context()
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
    )
    time.sleep(5)
    logger.debug(f"账号 {username} 开始发送消息")
    for username in scroll_and_select_user(page, username, targets):
        logger.debug(f"账号 {username} 已选中好友 {username} 发送消息")
        chat_input_selector = CHAT_EDITOR_SELECTOR
        page.wait_for_selector(chat_input_selector, timeout=config["browserTimeout"])
        chat_input = page.locator(chat_input_selector)
        message = build_message()
        for line in message.split("\\n"):
            chat_input.type(line)
            if line != message.split("\\n")[-1]:
                chat_input.press("Shift+Enter")
        logger.debug(f"账号 {username} 准备发送消息给好友 {username}：\n\t{message}")
        logger.debug(f"账号 {username} 给好友 {username} 发送消息完成")
        chat_input.press("Enter")
        time.sleep(2)
    context.close()
def runTasks():
    playwright, browser = get_browser()
    try:
        logger.info("开始执行任务")
        logger.debug(f"当前配置如下：")
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
