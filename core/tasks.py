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
SEARCH_BOX_SELECTORS = [
    "input[placeholder*='搜索']",
    "input[placeholder*='搜']",
    "input[type='search']",
    "[class*='conversation'] input[type='text']",
    "[class*='Conversation'] input[type='text']",
]
CHAT_HEADER_TITLE_SELECTORS = [
    ".messageChatItemTitle",
    "[class*='message'][class*='Title']",
    "[class*='Message'][class*='Title']",
]


class LoginRequired(RuntimeError):
    """登录态失效或需要人工安全验证。"""


def handle_response(response: Response):
    """缓存抖音 IM 用户信息接口返回值；仅作为加速索引，不依赖它匹配好友。

    账号可能被服务端风控降级（data: null），此时接口完全不可用，直接忽略，
    匹配走聊天 UI 的搜索/列表标题。
    """
    if "aweme/v1/web/im/user/info" not in response.url:
        return
    try:
        json_data = response.json()
    except Exception:
        return
    data = json_data.get("data")
    if not isinstance(data, list):
        logger.warning(f"user/info 返回不可用（风控降级）: {json_data.get('data')!r}")
        return
    for item in data:
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
    """只保存脱敏文本日志和截图，不保存完整 HTML（避免泄露会话令牌/Cookie）。"""
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
        title = page.title()
        url = page.url
        body_text = ""
        try:
            body_text = page.locator("body").inner_text(timeout=3000)[:2000]
        except Exception:
            pass
        with open(f"{base_path}.txt", "w", encoding="utf-8") as f:
            f.write(f"url: {url}\ntitle: {title}\n\n{body_text}")
    except Exception as e:
        logger.warning(f"保存调试文本失败: {e}")

    logger.warning(
        f"已保存调试文件: {base_path}.png / {base_path}.txt，当前 URL: {page.url}"
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

    if "login" in page.url.lower() or "登录" in body_text or "验证码" in body_text or "短信验证" in body_text:
        raise LoginRequired(
            f"账号 {username} 未进入聊天列表，疑似 Cookie 失效或需要人工安全验证。"
        )

    raise RuntimeError(
        f"账号 {username} 未找到聊天列表。页面标题: {title!r}，"
        f"页面文本片段: {body_text!r}"
    )


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


def resolve_item_selector(page):
    for selector in CONVERSATION_ITEM_SELECTORS:
        try:
            if page.locator(selector).count() > 0:
                return selector
        except Exception:
            continue
    return CONVERSATION_ITEM_SELECTORS[0]


def find_search_box(page):
    """聊天列表顶部的搜索框；找到返回 locator，找不到返回 None。"""
    for selector in SEARCH_BOX_SELECTORS:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def exact_visible_item(page, item_selector, target):
    """遍历当前可见会话项，标题精确匹配目标（归一化后逐字符相等）。"""
    wanted = norm(target)
    for i in range(page.locator(item_selector).count()):
        try:
            el = page.locator(item_selector).nth(i)
            if not el.is_visible():
                continue
            title = get_item_title(el)
            if title and norm(title) == wanted:
                return el, title
        except Exception:
            continue
    return None, None


def find_real_scroller(page, list_selector):
    """从列表容器向上找到真正可滚动的元素（overflowY auto/scroll 且 scrollHeight>clientHeight）。"""
    try:
        wrapper = page.locator(list_selector).first
        wrapper.wait_for(state="visible", timeout=20000)
        handle = wrapper.evaluate_handle(
            """(element) => {
                let node = element;
                while (node && node !== document.body) {
                    const style = getComputedStyle(node);
                    const scrollable = /(auto|scroll)/.test(style.overflowY)
                        && node.scrollHeight > node.clientHeight + 2;
                    if (scrollable) return node;
                    node = node.parentElement;
                }
                return element;
            }"""
        )
        return handle.as_element()
    except Exception as e:
        logger.warning(f"查找滚动容器失败: {e}")
        return None


def select_by_virtual_list(page, username, target, list_selector, item_selector):
    """滚动兜底：真实滚动容器 + 鼠标滚轮（更接近真实用户触发虚拟列表加载）。"""
    scroller = find_real_scroller(page, list_selector)
    if scroller is None:
        logger.warning(f"账号 {username} 未找到可滚动容器")
        return None, None
    try:
        scroller.hover()
    except Exception:
        pass

    stale_rounds = 0
    last_state = None
    for _ in range(80):
        el, title = exact_visible_item(page, item_selector, target)
        if el is not None:
            return el, title

        try:
            page.mouse.wheel(0, 620)
        except Exception:
            pass
        time.sleep(0.35)

        try:
            state = scroller.evaluate(
                "el => ({top: el.scrollTop, height: el.scrollHeight, client: el.clientHeight})"
            )
        except Exception:
            state = None

        if state is not None:
            if state == last_state:
                stale_rounds += 1
            else:
                stale_rounds = 0
                last_state = state
            at_bottom = state["top"] >= state["height"] - state["client"] - 2
            if at_bottom or stale_rounds >= 5:
                break

    logger.warning(f"账号 {username} 滚动到底仍未找到目标 {target!r}")
    return None, None


def select_target(page, username, target, list_selector, item_selector, search):
    """主路径：搜索框精确筛选；兜底：滚动。找到并点击后返回 (True, title)，否则 (False, None)。"""
    if search is not None:
        try:
            search.fill(target)
            time.sleep(0.8)
        except Exception as e:
            logger.debug(f"账号 {username} 搜索输入失败: {e}")

        # 优先：搜索可能就地过滤会话列表
        el, title = exact_visible_item(page, item_selector, target)
        if el is not None:
            try:
                el.click()
                return True, title
            except Exception as e:
                logger.warning(f"账号 {username} 点击好友 {target} 失败: {e}")
                return False, None

        # 其次：页面上精确文本的可见元素（搜索结果面板等）
        try:
            loc = page.get_by_text(target, exact=True).filter(visible=True).first
            if loc.count() > 0:
                loc.click()
                return True, target
        except Exception as e:
            logger.debug(f"账号 {username} 搜索结果点选失败: {e}")

        # 清空搜索，回到完整列表
        try:
            search.fill("")
        except Exception:
            pass

    el, title = select_by_virtual_list(page, username, target, list_selector, item_selector)
    if el is not None:
        try:
            el.click()
            return True, title
        except Exception as e:
            logger.warning(f"账号 {username} 点击好友 {target} 失败: {e}")
            return False, None
    return False, None


def read_chat_header_title(page):
    """尽力读取当前打开会话的标题栏文本，用于发送前二次确认。"""
    for selector in CHAT_HEADER_TITLE_SELECTORS:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible():
                return loc.inner_text(timeout=2000).strip()
        except Exception:
            continue
    return ""


def send_chat_message(page, username, target):
    # 若页面已跳转到安全验证/登录页，立即失败退出该账号，不做循环重试
    try:
        body_text = page.locator("body").inner_text(timeout=2000)[:200]
    except Exception:
        body_text = ""
    if any(k in body_text for k in ("安全验证", "验证码", "短信验证")):
        raise LoginRequired(f"账号 {username} 出现安全验证/登录提示，请人工处理")

    # 点开会话后输入框应在几秒内出现；等待时间别太长，避免单个目标白等
    _, chat_input = first_visible_locator(page, CHAT_EDITOR_SELECTORS, timeout=10000)
    if chat_input is None:
        dump_debug_artifacts(page, username, "chat-editor-not-found")
        logger.warning(f"账号 {username} 未找到聊天输入框")
        return False

    message = build_message()
    lines = message.replace("\\\\n", chr(10)).splitlines() or [message]
    for index, line in enumerate(lines):
        chat_input.type(line)
        if index != len(lines) - 1:
            chat_input.press("Shift+Enter")
    logger.debug(f"账号 {username} 准备发送消息给好友 {target}：\n\t{message}")
    chat_input.press("Enter")
    logger.debug(f"账号 {username} 给好友 {target} 发送消息完成")
    time.sleep(2)
    return True


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
        item_selector = resolve_item_selector(page)
        search = find_search_box(page)
        logger.debug(f"账号 {username} 搜索框可用: {search is not None}")

        not_found = []
        for target in targets:
            target = norm(target)
            try:
                found, title = select_target(page, username, target, list_selector, item_selector, search)
            except LoginRequired:
                raise
            except Exception as e:
                logger.warning(f"账号 {username} 选择好友 {target} 出错: {e}")
                found, title = False, None

            if not found:
                not_found.append(target)
                logger.warning(f"账号 {username} 未找到好友 {target}")
                continue

            # 点开后等聊天面板渲染，再做发送前二次确认
            time.sleep(1)

            # 发送前二次确认：打开的会话标题与目标完全一致（尽力而为）
            header = read_chat_header_title(page)
            if header:
                nh = norm(header)
                nt = norm(target)
                if nh != nt and nt not in nh:
                    logger.warning(
                        f"账号 {username} 打开会话标题不匹配，跳过 {target!r} (表头 {header!r})"
                    )
                    not_found.append(target)
                    continue

            try:
                if not send_chat_message(page, username, target):
                    not_found.append(target)
            except LoginRequired:
                logger.warning(f"账号 {username} 检测到安全验证，中止该账号")
                break
            except Exception as e:
                logger.warning(f"账号 {username} 给 {target} 发消息失败: {e}")
                not_found.append(target)

        if not_found:
            logger.warning(f"账号 {username} 本次未找到/未发送好友: {not_found}")
        logger.debug(f"账号 {username} 任务完成")
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
            try:
                do_user_task(browser, username, cookies, targets)
                logger.info(f"账号 {username} 任务完成")
            except LoginRequired as e:
                logger.warning(f"账号 {username} 中止: {e}")
            except Exception as e:
                logger.error(f"账号 {username} 异常: {e}\n{traceback.format_exc()}")
    finally:
        browser.close()
        playwright.stop()
