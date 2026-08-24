import os
import re
import time
import traceback

from playwright.sync_api import Response, TimeoutError as PlaywrightTimeoutError

from core.browser import get_browser
from core.msg_builder import build_message
from utils import norm, norm_tight, strict_title_match
from utils.config import get_config, get_userData
from utils.logger import setup_logger


config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))

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
# 不再接受宽泛的 [role='list']：登录页 / 其他面板也可能带 role=list，会被误判为聊天列表
CONVERSATION_LIST_SELECTORS = [
    ".conversationConversationListwrapper",
    "[class*='conversation'][class*='List']",
    "[class*='Conversation'][class*='List']",
]
# 编辑器限定在聊天编辑面板内；裸 [contenteditable='true'] 只作最后兜底，避免抓到页面其他可编辑区域
CHAT_EDITOR_SELECTORS = [
    ".messageEditorimChatEditorContainer",
    "[class*='messageEditor'][contenteditable='true']",
    "[class*='Message'][contenteditable='true']",
    "[contenteditable='true']",
    "textarea",
]
# 只接受带搜索语义的输入框；不再用「会话容器内任意 input[type='text']」兜底
SEARCH_BOX_SELECTORS = [
    "input[placeholder*='搜索']",
    "input[placeholder*='搜']",
    "input[aria-label*='搜索']",
    "input[type='search']",
]
# 表头标题：真实登录页验证为 .RightPanelHeadertitle。
# 注意：旧推断 .messageChatItemTitle 在真实 DOM 不存在；[class*='Message'][class*='Title']
# 匹配到的是消息气泡里的发送者昵称（假阳性），已移除，避免「表头校验」被气泡昵称蒙混。
CHAT_HEADER_TITLE_SELECTORS = [
    ".RightPanelHeadertitle",
    "[class*='RightPanelHeader'][class*='title']",
    "[class*='message'][class*='Title']",
]
# 搜索结果容器：搜索后 .conversationConversationItemwrapper 会被 SearchPanel 覆盖成 hidden，
# 结果渲染在 SearchPanelitem 系列（真实 DOM 验证）。点击 chat_btn 才会真正进入会话。
SEARCH_PANEL_ITEM_SELECTORS = [
    ".SearchPanelitembox",
    "[class*='SearchPanel'][class*='item']",
]
SEARCH_PANEL_TITLE_SELECTORS = [
    ".SearchPanelitemtitle",
    "[class*='SearchPanel'][class*='title']",
]
SEARCH_PANEL_CHAT_BTN_SELECTORS = [
    ".SearchPanelitemchat_btn",
    "[class*='SearchPanel'][class*='chat']",
]

# 安全验证 / 登录关键词：一旦命中即视为需要人工介入，安全停止该账号
SECURITY_KEYWORDS = ("安全验证", "验证码", "短信验证", "登录")


class LoginRequired(RuntimeError):
    """登录态失效或需要人工安全验证。"""


class ChatUnavailable(RuntimeError):
    """停留在 /chat 但聊天列表未渲染/不可滚动/不可见：页面降级或慢加载，应停止该账号。

    与 LoginRequired 分开：LoginRequired 是登录/安全验证，ChatUnavailable 是页面就绪问题。
    两者都停止该账号，不继续逐目标匹配浪费超时。
    """


def make_handle_response(userIDDict):
    """返回绑定到指定账号 userIDDict 的响应处理器。

    缓存抖音 IM 用户信息接口返回值；仅作为加速索引，不依赖它匹配好友。
    账号被服务端风控降级（data: null）时接口完全不可用，直接忽略，
    匹配走聊天 UI 的搜索 / 列表标题。
    userIDDict 在账号维度创建，绝不跨账号复用。
    """

    def handle_response(response: Response):
        if "aweme/v1/web/im/user/info" not in response.url:
            return
        try:
            json_data = response.json()
        except Exception:
            return
        data = json_data.get("data")
        if not isinstance(data, list):
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

    return handle_response


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


def _body_text(page, limit=2000):
    try:
        return page.locator("body").inner_text(timeout=3000)[:limit]
    except Exception:
        return ""


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
        body_text = _body_text(page)
        with open(f"{base_path}.txt", "w", encoding="utf-8") as f:
            f.write(f"url: {url}\ntitle: {title}\n\n{body_text}")
    except Exception as e:
        logger.warning(f"保存调试文本失败: {e}")

    logger.warning(
        f"已保存调试文件: {base_path}.png / {base_path}.txt，当前 URL: {page.url}"
    )


def _compact_clean(s):
    """极致归一：norm 后再去掉字面转义序列与全部空白，只保留内容字符。

    抖音编辑器会把消息里的字面 \\n 混成「真实换行 + 字面 \\n」，气泡 inner_text 与
    列表预览 textContent 呈现各不相同；去掉一切空白与字面转义后，同一消息在不同
    渲染下都能对上。
    """
    s = norm(s)
    s = s.replace("\\n", "").replace("\\r", "").replace("\\t", "")
    return "".join(ch for ch in s if not ch.isspace())


def visible_compact(s):
    """先去 [表情码]（气泡里渲染成图片，inner_text 会丢失），再极致归一。"""
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    return _compact_clean(re.sub(r"\[[^\[\]]{1,12}\]", "", s))


def read_conversation_item_state(page, item_selector, wanted_set):
    """只读：按标题精确匹配目标会话项，返回 (time_str, preview, preview_compact)。

    - time_str：最近消息时间，如「刚刚 / 20:42」。发送后目标条目必然翻转为「刚刚」，
      布局无关，实测可靠。
    - preview：列表预览 textContent（保留表情码原文与字面 \\n；刚发送后可能为省略号 …）
    - preview_compact：preview 去表情码并极致归一，用于跨渲染差异对比
    找不到（列表被 SearchPanel 覆盖 / 未渲染）返回 ("", "", "")，调用方自行降级。
    """
    for i in range(page.locator(item_selector).count()):
        try:
            el = page.locator(item_selector).nth(i)
            if not el.is_visible():
                continue
            title = get_item_title(el)
            if not title or norm(title) not in wanted_set:
                continue
            time_str = ""
            try:
                t_el = el.locator("[class*='timeStr']").first
                if t_el.count():
                    time_str = t_el.inner_text(timeout=1000).strip()
            except Exception:
                pass
            preview = ""
            try:
                d_el = el.locator("[class*='ConversationItemDesc']").first
                if d_el.count():
                    preview = (d_el.evaluate("(e) => (e.textContent || '')") or "").strip()
            except Exception:
                pass
            return time_str, preview, visible_compact(preview)
        except Exception:
            continue
    return "", "", ""


def wait_for_chat_ready(page, username):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        logger.warning(f"账号 {username} 等待 DOMContentLoaded 超时，继续检查页面")

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        logger.debug(f"账号 {username} networkidle 超时，可能是抖音长连接导致，继续检查页面")

    # 最终必须停留在 /chat；若被重定向到登录 / 验证页，安全停止该账号
    if "/chat" not in page.url:
        body_text = _body_text(page)
        matched = next((k for k in SECURITY_KEYWORDS if k in body_text), None)
        if matched:
            dump_debug_artifacts(page, username, "login-or-verify")
            raise LoginRequired(
                f"账号 {username} 未进入聊天页（URL={page.url!r}），"
                f"疑似需要人工安全验证/登录（命中关键词 {matched!r}）。"
            )
        try:
            page.wait_for_url("**/chat**", timeout=10000)
        except PlaywrightTimeoutError:
            pass
        if "/chat" not in page.url:
            dump_debug_artifacts(page, username, "not-chat-url")
            raise LoginRequired(f"账号 {username} 未停留在聊天页（URL={page.url!r}）。")

    # 列表必须渲染出来：最多等 30s。每次轮询所有候选，防止单个选择器串行超时放大总等待
    list_selector = None
    deadline = time.time() + 30
    while time.time() < deadline:
        list_selector, _ = first_visible_locator(page, CONVERSATION_LIST_SELECTORS, timeout=4000)
        if list_selector:
            break
        time.sleep(0.5)
    if list_selector:
        logger.debug(f"账号 {username} 聊天列表已加载，选择器: {list_selector}")
        return list_selector

    title = ""
    try:
        title = page.title()
    except Exception:
        pass
    body_text = _body_text(page, 500)

    dump_debug_artifacts(page, username, "chat-list-not-found")

    diag = list_diagnostics(page)
    logger.warning(f"账号 {username} 聊天列表未渲染（诊断: {diag}）")

    # 已停留在 /chat：只有强验证关键词才判 LoginRequired；裸「登录」太泛，
    # 可能是页面文案里的普通词，不足以证明会话失效（仍会走下面的 ChatUnavailable 安全停止）
    strong = ("安全验证", "验证码", "短信验证")
    matched = next((k for k in strong if k in body_text), None)
    if matched:
        raise LoginRequired(
            f"账号 {username} 未进入聊天列表，疑似 Cookie 失效或需要人工安全验证"
            f"（命中关键词 {matched!r}）。"
        )
    weak = "登录" in body_text

    # 区别于 LoginRequired：已停留在 /chat，但列表未渲染/不可见。
    # 可能是云端 headless 降级态或慢加载。不再继续等，安全中止该账号（宁可不发也不误发）。
    raise ChatUnavailable(
        f"账号 {username} 停留在聊天页但列表未渲染/不可见"
        f"（页面标题: {title!r}, 含登录字样的弱信号: {weak}），中止该账号"
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


def find_search_box(page, username):
    """只接受带搜索语义的输入框；候选不等于 1 时记录脱敏诊断并失败（不使用搜索，仅滚动）。

    绝不猜测：输入框个数含糊时不选中任何一个，避免把消息发到错误会话。
    """
    for selector in SEARCH_BOX_SELECTORS:
        try:
            loc = page.locator(selector)
            if loc.count() == 1 and loc.first.is_visible():
                return loc.first
        except Exception:
            continue

    # 脱敏诊断：只记录 type/placeholder/aria-label，不记录页面内容/Cookie
    inputs = []
    try:
        for i in range(min(page.locator("input").count(), 10)):
            el = page.locator("input").nth(i)
            inputs.append(
                {
                    "type": el.get_attribute("type"),
                    "placeholder": el.get_attribute("placeholder"),
                    "aria": el.get_attribute("aria-label"),
                }
            )
    except Exception:
        inputs = []
    logger.warning(
        f"账号 {username} 未找到唯一搜索框（诊断: {inputs}），本次不使用搜索，仅靠滚动匹配"
    )
    return None


def exact_visible_item(page, item_selector, wanted_set):
    """遍历当前可见会话项，标题 norm 后与目标任一别名逐字符相等。

    wanted_set：{norm(别名), ...}。只有逐字符相等才算命中，不做包含匹配。
    """
    for i in range(page.locator(item_selector).count()):
        try:
            el = page.locator(item_selector).nth(i)
            if not el.is_visible():
                continue
            title = get_item_title(el)
            if title and norm(title) in wanted_set:
                return el, title
        except Exception:
            continue
    return None, None


def exact_search_panel_item(page, wanted_tight_set):
    """遍历 SearchPanel 搜索结果项，标题 norm_tight 后与目标任一别名逐字符相等。

    搜索后会话列表被 SearchPanel 覆盖为 hidden，命中项在 .SearchPanelitembox 里。
    wanted_tight_set：{norm_tight(别名), ...}。
    返回 (item_box, title)；只有逐字符相等才算命中。
    """
    for selector in SEARCH_PANEL_ITEM_SELECTORS:
        try:
            boxes = page.locator(selector)
            for i in range(boxes.count()):
                el = boxes.nth(i)
                try:
                    title_el = el.locator(SEARCH_PANEL_TITLE_SELECTORS[0]).first
                    if title_el.count() == 0:
                        continue
                    if not title_el.is_visible():
                        continue
                    title = title_el.inner_text(timeout=1500).strip()
                except Exception:
                    continue
                if title and norm_tight(title) in wanted_tight_set:
                    return el, title
        except Exception:
            continue
    return None, None


def visible_titles(page, item_selector):
    """返回当前可见会话项的 norm 后标题元组（仅用于停止条件判断）。"""
    out = []
    for i in range(page.locator(item_selector).count()):
        try:
            el = page.locator(item_selector).nth(i)
            if not el.is_visible():
                continue
            t = get_item_title(el)
            if t:
                out.append(norm(t))
        except Exception:
            continue
    return tuple(out)


def log_visible_conversation_titles(page, username, item_selector, limit=15):
    """DRY_RUN 只读诊断：记录当前可见会话项的标题（norm + 截断到 40 字符）。

    只记录用户自己的联系人显示名——这是把 unique_id 目标映射到真实显示名
    所必需的配置数据，不记录消息内容/页面全文/Cookie。上限 limit 项。
    """
    titles = []
    for i in range(min(page.locator(item_selector).count(), limit)):
        try:
            el = page.locator(item_selector).nth(i)
            if not el.is_visible():
                continue
            t = get_item_title(el)
            if t:
                titles.append(norm(t)[:40])
        except Exception:
            continue
    logger.info(f"账号 {username} [DRY_RUN] 当前可见会话标题: {titles}")


def probe_target_profile_names(page, username, targets):
    """DRY_RUN 只读诊断：尝试访问每个目标的主页，读取显示名。

    抖音网页版可能支持 /user/{抖音号} 直达主页，主页标题/og:title 含显示名。
    仅当 dryRun 时调用：只读页面标题，不发送、不点击、不填表。
    主页可能 404/要求登录/渲染不出名字，读不到就记为 None（该目标按不可达处理）。
    另开独立页面做探测，不影响 /chat 主页面。
    """
    try:
        ctx = page.context
    except Exception:
        return
    for target in targets:
        tid = target["id"]
        url = f"https://www.douyin.com/user/{tid}"
        name = None
        try:
            probe_page = ctx.new_page()
            try:
                probe_page.goto(url, wait_until="domcontentloaded", timeout=20000)
                probe_page.wait_for_timeout(2500)
                name = probe_page.evaluate(
                    """() => {
                        const og = document.querySelector('meta[property="og:title"]');
                        const t = document.title || '';
                        const raw = (og && og.content) ? og.content : t;
                        return (raw || '').trim().slice(0, 40) || null;
                    }"""
                )
            finally:
                probe_page.close()
        except Exception:
            name = None
        logger.info(f"账号 {username} [DRY_RUN] 目标 {tid!r} 主页显示名: {name}")


def list_diagnostics(page, limit=8):
    """只读：返回会话列表候选元素的结构诊断（selector/index/是否可见/位置）。

    只记录结构，不记录页面文本内容。用于判断「列表未渲染 / 被覆盖 / 定位竞态」。
    """
    result = []
    for selector in CONVERSATION_LIST_SELECTORS:
        loc = page.locator(selector)
        for i in range(min(loc.count(), limit)):
            item = loc.nth(i)
            try:
                result.append(
                    {
                        "selector": selector,
                        "index": i,
                        "visible": item.is_visible(),
                        "box": item.bounding_box(),
                    }
                )
            except Exception:
                result.append({"selector": selector, "index": i, "error": True})
    return result


_SCROLLER_WALK_JS = """(element) => {
    let node = element;
    while (node) {
        const style = getComputedStyle(node);
        if (/(auto|scroll)/.test(style.overflowY)) {
            return node;
        }
        if (node === document.documentElement) {
            break;
        }
        node = node.parentElement;
    }
    return null;
}"""


def find_real_scroller(page, timeout_ms=8000):
    """从所有列表候选里重新扫描当前可见元素，找到其可滚动祖先。

    不再把 list_selector 字符串传进来再 `.first` 查一次——虚拟列表可能替换节点、
    覆盖层可能改变 DOM 顺序。每次需要滚动时都重新扫描，取当前可见的候选。
    刻意不以 scrollHeight > clientHeight 为前提：虚拟列表不一定反映完整高度。
    先扫列表 wrapper 候选；未命中时再兜底从可见会话项向上找（wrapper 选择器
    可能对不上真实结构，但 item 选择器能命中）。找不到返回 None。
    """
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        for selector in CONVERSATION_LIST_SELECTORS:
            loc = page.locator(selector)
            for i in range(min(loc.count(), 12)):
                wrapper = loc.nth(i)
                try:
                    if not wrapper.is_visible():
                        continue
                    handle = wrapper.element_handle()
                    if handle is None:
                        continue
                    scroller = handle.evaluate_handle(_SCROLLER_WALK_JS).as_element()
                    if scroller:
                        return scroller
                except Exception:
                    continue
        # 兜底：wrapper 选择器未命中时，从可见会话项向上找滚动祖先
        for selector in CONVERSATION_ITEM_SELECTORS:
            loc = page.locator(selector)
            for i in range(min(loc.count(), 12)):
                item = loc.nth(i)
                try:
                    if not item.is_visible():
                        continue
                    handle = item.element_handle()
                    if handle is None:
                        continue
                    scroller = handle.evaluate_handle(_SCROLLER_WALK_JS).as_element()
                    if scroller:
                        return scroller
                except Exception:
                    continue
        time.sleep(0.25)
    return None


def resolve_aliases_with_userdict(target, userIDDict):
    """目标别名 + userIDDict 动态映射（仅作索引，不依赖它，找不到就原样返回）。

    若 target["id"] 是抖音号（short_id/unique_id），用被动捕获的 user/info 响应把它映射成
    「当前」备注名/昵称，追加为别名用于匹配。userIDDict 随滚动被动增长，因此本函数应在
    每次匹配时动态调用（重算 wanted_set），不要只算一次。

    对方改名也不影响：user/info 始终返回该抖音号当前最新标题，old 别名即使失效，
    新标题仍会命中。返回 (title_aliases, title_aliases_norm)。
    """
    aliases = list(target["title_aliases"])
    aliases_norm = list(target["title_aliases_norm"])
    tid = target["id"]
    if not tid:
        return aliases, aliases_norm
    for entry in userIDDict.values():
        # entry = [short_id, unique_id, sec_uid, nickname, remark_name]
        if tid not in (entry[0], entry[1]):
            continue
        for alias in (entry[4], entry[3]):
            if alias and alias not in aliases:
                aliases.append(alias)
                aliases_norm.append(norm(alias))
    return aliases, aliases_norm


def _fallback_list_scroller(page):
    """云端兜底滚动容器：find_real_scroller 未命中时，直接用列表 wrapper 元素直滚。

    find_real_scroller 要求祖先 computed overflowY 为 auto/scroll；云端虚拟列表的滚动容器
    overflowY 可能是默认值，导致返回 None（滚动回归：账号1 找不到深层目标）。旧版
    7d0ac28bfe 直接用 CONVERSATION_LIST_SELECTORS 的 wrapper 做 scrollTop += 800，
    已验证能加载更多会话。本函数不要求 overflowY，只要求可见的 wrapper 元素。找不到返回 None。
    """
    for selector in CONVERSATION_LIST_SELECTORS:
        try:
            loc = page.locator(selector)
            for i in range(min(loc.count(), 12)):
                wrapper = loc.nth(i)
                if not wrapper.is_visible():
                    continue
                handle = wrapper.element_handle()
                if handle is not None:
                    return handle
        except Exception:
            continue
    return None


def select_by_virtual_list(page, username, target, item_selector, userIDDict=None):
    """滚动兜底：真实滚动容器（或 wrapper 兜底）+ scrollTop 直滚。

    滚动机制：优先 find_real_scroller 找到的真实滚动容器；未命中时退回 _fallback_list_scroller
    （列表 wrapper，旧版 7d0ac28bfe 验证可行）。一律用 scrollTop += 620 直滚，不再依赖
    mouse.wheel 悬停捕获（云端对悬停目标敏感，回归根源）。

    匹配别名每轮动态重算：unique_id 目标在 userIDDict 被动增长后被映射成「当前」备注名/昵称
    命中（对方改名也不影响，因为 user/info 始终返回最新标题；匹配桥接在稳定 unique_id 上）。

    停止条件（组合判定，缺一不可）：
    1. 目标任一别名（含 userIDDict 动态别名）在可见窗口中精确匹配 -> 点击返回（最高优先）
    2. 连续多轮「可见标题窗口不变 且 已见集合不再增长」-> 判定不可见并停止

    找不到滚动容器/目标 -> 只跳过该目标（返回 not_found），继续下一个目标，绝不猜测点击。
    页面级不可用由 wait_for_chat_ready 抛 ChatUnavailable 负责（列表根本未渲染时才中止账号）。
    """
    userIDDict = userIDDict or {}
    scroller = find_real_scroller(page)
    if scroller is None:
        scroller = _fallback_list_scroller(page)
    if scroller is None:
        logger.warning(f"账号 {username} 未找到可滚动容器，滚动兜底跳过目标 {target['id']}")
        return None, None
    try:
        scroller.hover()  # 悬停有助于部分虚拟列表加载；scrollTop 直滚不依赖它
    except Exception:
        pass

    seen = set()
    last_window = tuple()
    stagnant = 0
    for _ in range(120):
        # 动态别名：userIDDict 随滚动被动增长，unique_id 目标映射成当前备注名/昵称后命中
        aliases, _ = resolve_aliases_with_userdict(target, userIDDict)
        wanted_set = set(norm(a) for a in aliases)
        window = visible_titles(page, item_selector)
        seen_before = len(seen)
        seen.update(window)
        grew = len(seen) > seen_before

        el, title = exact_visible_item(page, item_selector, wanted_set)
        if el is not None:
            return el, title

        try:
            scroller.evaluate("el => el.scrollTop += 620")
        except Exception:
            pass
        time.sleep(0.5)

        try:
            state = scroller.evaluate(
                "el => ({top: el.scrollTop, height: el.scrollHeight, client: el.clientHeight})"
            )
        except Exception:
            state = None
        logger.debug(
            f"账号 {username} 滚动中 scrollTop={state and state.get('top')} "
            f"可见窗口={len(window)} 累计标题={len(seen)}"
        )

        if window == last_window and not grew:
            stagnant += 1
        else:
            stagnant = 0
        last_window = window
        if stagnant >= 6:
            logger.debug(
                f"账号 {username} 连续 {stagnant} 轮可见窗口无变化且无新标题，判定 {target['id']} 不可见"
            )
            break

    logger.warning(f"账号 {username} 滚动结束仍未找到目标 {target['id']}")
    return None, None


def select_target(page, username, target, item_selector, search, userIDDict=None):
    """主路径：搜索框精确筛选；兜底：滚动。找到并点击后返回 (True, title)，否则 (False, None)。

    搜索后会话列表被 SearchPanel 覆盖为 hidden，命中项在 .SearchPanelitembox 里；
    点 .SearchPanelitemchat_btn 才会真正进入会话（真实 DOM 验证）。已删除全页 get_by_text 兜底。
    滚动兜底失败只跳过该目标（返回 not_found），不影响其他目标的搜索路径。

    userIDDict 仅作索引：把 unique_id 目标映射成「当前」备注名/昵称参与匹配（动态重算，
    对方改名也不影响；匹配桥接在稳定 unique_id 上）。搜索框只用于目标显式配置的 search_terms，
    不把名字写进搜索（名字搜索不稳定，unique_id 目标靠滚动 + 桥接命中）。
    """
    userIDDict = userIDDict or {}
    aliases, _ = resolve_aliases_with_userdict(target, userIDDict)
    wanted_set = set(norm(a) for a in aliases)
    wanted_tight_set = set(norm_tight(a) for a in aliases)
    if search is not None:
        for term in target["search_terms"]:
            try:
                search.fill(term)
                time.sleep(0.8)
            except Exception as e:
                logger.debug(f"账号 {username} 搜索输入 {term!r} 失败: {e}")
                continue
            # 搜索诊断：只记录结构/计数，不记录完整页面文本（用于区分「搜索框不存在」vs「标题不匹配」）
            try:
                logger.info(
                    f"账号 {username} 搜索诊断: target={target['id']!r}, term={term!r}, "
                    f"search_box={search is not None}, "
                    f"panel_items={page.locator(SEARCH_PANEL_ITEM_SELECTORS[0]).count()}, "
                    f"visible_items={page.locator(item_selector).count()}"
                )
            except Exception:
                pass
            # 搜索结果进 SearchPanel：在 .SearchPanelitembox 内精确匹配标题
            el, title = exact_search_panel_item(page, wanted_tight_set)
            if el is not None:
                try:
                    # 点击「去聊天」按钮才进入会话；找不到按钮则点结果项本身
                    btn = el.locator(SEARCH_PANEL_CHAT_BTN_SELECTORS[0]).first
                    if btn.count() > 0 and btn.is_visible():
                        btn.click()
                    else:
                        el.click()
                    return True, title
                except Exception as e:
                    logger.warning(f"账号 {username} 点击好友 {target['id']} 失败: {e}")
                    return False, None
            # 无精确命中：记录 SearchPanel 结果标题（限量、截断），用于诊断目标格式是否匹配
            try:
                titles = []
                boxes = page.locator(SEARCH_PANEL_ITEM_SELECTORS[0])
                for j in range(min(boxes.count(), 5)):
                    t = boxes.nth(j).locator(SEARCH_PANEL_TITLE_SELECTORS[0]).first
                    if t.count() > 0 and t.is_visible():
                        titles.append(norm(t.inner_text(timeout=800).strip())[:30])
                if titles:
                    logger.info(
                        f"账号 {username} 搜索无精确命中: target={target['id']!r}, term={term!r}, "
                        f"panel_titles={titles}"
                    )
            except Exception:
                pass
            # 兜底：若 SearchPanel 没出现（搜索无结果），尝试会话列表内精确匹配
            el, title = exact_visible_item(page, item_selector, wanted_set)
            if el is not None:
                try:
                    el.click()
                    return True, title
                except Exception as e:
                    logger.warning(f"账号 {username} 点击好友 {target['id']} 失败: {e}")
                    return False, None
            try:
                search.fill("")
            except Exception:
                pass
            time.sleep(0.3)

    el, title = select_by_virtual_list(page, username, target, item_selector, userIDDict)
    if el is not None:
        try:
            el.click()
            return True, title
        except Exception as e:
            logger.warning(f"账号 {username} 点击好友 {target['id']} 失败: {e}")
            return False, None
    return False, None


def read_chat_header_title(page, wait_seconds=5):
    """尽力读取当前打开会话的标题栏文本，用于发送前二次确认。

    点击会话后标题栏需要短暂渲染，这里对每个候选 wait_for(visible)（单个 selector
    最多 wait_seconds 秒），再读文本。优先使用配置的 CHAT_HEADER_TITLE_SELECTOR，
    否则回退到推断列表。读不到返回 ""，调用方必须据此跳过发送。
    """
    selectors = []
    override = config.get("chatHeaderTitleSelector") or ""
    if override:
        selectors.append(override)
    selectors.extend(CHAT_HEADER_TITLE_SELECTORS)
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            loc.wait_for(state="visible", timeout=wait_seconds * 1000)
            return loc.inner_text(timeout=2000).strip()
        except Exception:
            continue
    return ""


def already_present(norm_msg, before_preview, before_last_text):
    """发送前去重（纯逻辑）：预览或最后一条本人气泡已是相同内容 -> True（跳过发送）。"""
    if before_preview and visible_compact(before_preview) == visible_compact(norm_msg):
        return True
    if before_last_text and visible_compact(before_last_text) == visible_compact(norm_msg):
        return True
    return False


def confirm_signals(norm_msg, before_ts, before_preview, now_ts, now_preview, bubble_text):
    """多信号发送确认（纯逻辑）：任一命中 -> True（可确认本次已发送）。

    信号（按可靠性）：
    A. 列表预览内容 == 消息（预览保留表情码原文，跨换行差异可对比）-> 最强
    B. 目标条目时间戳翻转为「刚刚」且发送前不是「刚刚」-> 布局无关，实测可靠
    C. 消息面板本人气泡去表情码后 == 消息 -> 兜底
    全部未命中 -> False。调用方应据此返回 "unverified"，绝不宣称失败，避免人类误重发。
    """
    if now_preview and visible_compact(now_preview) == visible_compact(norm_msg):
        return True
    if before_ts and before_ts != "刚刚" and now_ts == "刚刚":
        return True
    if bubble_text and visible_compact(bubble_text) == visible_compact(norm_msg):
        return True
    return False


def send_chat_message(page, username, target, config, item_selector):
    """发送前去重 -> 输入 -> 发送 -> 多信号确认。

    返回 (状态, 详情)：
      ("sent", None)            多信号任一命中（预览匹配 / 时间戳翻转为「刚刚」/ 气泡匹配）
      ("unverified", reason)    已尝试发送但无法可靠确认——绝不自动重发，绝不宣称失败
      ("failed", reason)        明确失败（相同内容已存在 / 未配置气泡选择器 / 无输入框）
    若检测到安全验证，抛 LoginRequired 安全停止该账号。

    确认信号设计（实测）：消息面板气泡文本因表情码渲染成图片、编辑器把 \\n 混成真实
    换行+字面 \\n、列表虚拟化只渲染可视窗口，纯气泡文本匹配不可靠。改用
    「列表预览 textContent（保留表情码原文）+ 目标条目时间戳翻转为「刚刚」」为主信号，
    气泡去表情码后的极致归一对比为兜底。全部未命中 -> "unverified"（诚实，防误重发）。
    """
    body_text = _body_text(page, 2000)
    if any(k in body_text for k in ("安全验证", "验证码", "短信验证")):
        raise LoginRequired(f"账号 {username} 出现安全验证/登录提示，请人工处理")

    _, chat_input = first_visible_locator(page, CHAT_EDITOR_SELECTORS, timeout=10000)
    if chat_input is None:
        dump_debug_artifacts(page, username, "chat-editor-not-found")
        logger.warning(f"账号 {username} 未找到聊天输入框")
        return "failed", "未找到聊天输入框"

    outgoing_sel = config.get("outgoingBubbleSelector") or ""

    # 无法可靠定位本人气泡 -> 不发送（宁漏发）
    if not outgoing_sel:
        return "failed", "未配置 outgoingBubbleSelector"

    message = build_message()
    norm_msg = norm(message)
    if not norm_msg:
        logger.warning(f"账号 {username} 消息内容为空，跳过发送")
        return "failed", "消息内容为空"

    # 发送前读取目标会话项状态（时间戳 + 预览），作为去重与确认基准
    wanted_set = set(target["title_aliases_norm"])
    before_ts, before_preview, _ = read_conversation_item_state(
        page, item_selector, wanted_set
    )
    before_last_text = ""
    try:
        cnt = page.locator(outgoing_sel).count()
        if cnt > 0:
            before_last_text = norm(
                page.locator(outgoing_sel).nth(cnt - 1).inner_text(timeout=2000)
            )
    except Exception:
        logger.warning(f"账号 {username} 无法读取本人气泡（选择器 {outgoing_sel!r}）")

    # 发送前去重：预览或最后一条本人气泡已是相同内容 -> 跳过，绝不重复发
    if already_present(norm_msg, before_preview, before_last_text):
        logger.warning(f"账号 {username} 目标会话已存在相同内容，跳过发送避免重复")
        return "failed", "相同内容已存在，跳过避免重复"

    lines = message.replace("\\\\n", chr(10)).splitlines() or [message]
    for index, line in enumerate(lines):
        chat_input.type(line)
        if index != len(lines) - 1:
            chat_input.press("Shift+Enter")
    logger.debug(f"账号 {username} 发送消息：\n\t{message}")
    chat_input.press("Enter")
    logger.debug(f"账号 {username} 已按下发送")

    # 多信号确认：预览内容匹配 / 时间戳翻转为「刚刚」/ 气泡去表情码后匹配
    deadline = time.time() + 15
    while time.time() < deadline:
        time.sleep(1)
        try:
            now_ts, now_preview, _ = read_conversation_item_state(
                page, item_selector, wanted_set
            )
            bubble_last = ""
            cnt = page.locator(outgoing_sel).count()
            if cnt > 0:
                try:
                    bubble_last = norm(
                        page.locator(outgoing_sel).nth(cnt - 1).inner_text(timeout=2000)
                    )
                except Exception:
                    bubble_last = ""
            if confirm_signals(
                norm_msg, before_ts, before_preview, now_ts, now_preview, bubble_last
            ):
                logger.info(f"账号 {username} 消息发送成功并确认")
                return "sent", None
        except Exception:
            continue

    dump_debug_artifacts(page, username, "send-unverified")
    logger.warning(
        f"账号 {username} 已尝试发送但无法可靠确认（时间戳/预览/气泡均未命中），返回 unverified"
    )
    return "unverified", "发送后未能可靠确认，请人工核实后决定是否重发"


def do_user_task(browser, username, cookies, targets):
    # browser 可能是常规 browser（可 new_context()），也可能是本地已登录的 persistent_context
    owns_context = hasattr(browser, "new_context")
    if owns_context:
        # 1440×900 更接近真实桌面会话，降低 headless 下抖音渲染降级的概率（仅常规浏览器参数，非反检测）
        context = browser.new_context(viewport={"width": 1440, "height": 900})
    else:
        context = browser
    try:
        context.set_default_navigation_timeout(config["browserTimeout"])
        context.set_default_timeout(config["browserTimeout"])

        if owns_context:
            page = context.new_page()
        else:
            # 本地 profile 模式：复用 profile 里已打开的页面，登录态在 profile 中
            page = context.pages[0] if context.pages else context.new_page()

        # userIDDict 账号级隔离：绝不跨账号复用
        userIDDict = {}
        page.on("response", make_handle_response(userIDDict))
        if owns_context:
            context.add_cookies(cookies)
        else:
            # profile 模式：登录态以 profile 为准，绝不注入环境变量 Cookie，避免污染 profile
            logger.debug(
                f"账号 {username} profile 模式：不使用环境变量 Cookie，登录态以 profile 为准"
            )

        retry_operation(
            "打开抖音网页聊天页面",
            page.goto,
            retries=config["taskRetryTimes"],
            delay=5,
            url="https://www.douyin.com/chat",
            wait_until="domcontentloaded",
            timeout=min(config["browserTimeout"], 60000),
        )

        # 就绪门禁：列表未渲染/不可见时 wait_for_chat_ready 会抛 ChatUnavailable 中止该账号
        wait_for_chat_ready(page, username)
        item_selector = resolve_item_selector(page)
        search = find_search_box(page, username)
        logger.debug(f"账号 {username} 搜索框可用: {search is not None}")
        if config.get("dryRun"):
            # 只读诊断：记录可见会话标题 + 探测各目标主页显示名，用于把 unique_id 目标映射到真实显示名
            log_visible_conversation_titles(page, username, item_selector)
            probe_target_profile_names(page, username, targets)

        not_found = []
        unverified = []
        dry_matched = []
        attempted = set()  # 当次运行内 at-most-once：同一 账号+目标 只尝试一次，绝不自动重发
        for target in targets:
            target_id = target["id"]
            key = f"{username}|{target_id}"
            if key in attempted:
                logger.debug(f"账号 {username} 已尝试过 {target_id}，at-most-once 跳过")
                continue
            attempted.add(key)

            try:
                found, title = select_target(page, username, target, item_selector, search, userIDDict)
            except LoginRequired:
                raise
            except ChatUnavailable:
                # 列表不可用不是单个目标的临时失败，而是整账号不可用，立即中止
                raise
            except Exception as e:
                logger.warning(f"账号 {username} 选择好友 {target_id} 出错: {e}")
                found, title = False, None

            if not found:
                not_found.append(target_id)
                logger.warning(f"账号 {username} 未找到好友 {target_id}")
                continue

            time.sleep(1)
            # 发送前二次确认：表头必须能读到，且与目标别名严格等值，否则一律跳过（宁可漏发）
            header = read_chat_header_title(page)
            if not header:
                logger.warning(
                    f"账号 {username} 无法读取表头标题，跳过 {target_id!r}（发送前无法确认收件人）"
                )
                not_found.append(target_id)
                continue
            # 发送前二次确认：用「目标别名 + userIDDict 当前备注/昵称」做严格等值校验，
            # 表头必须能读到且严格匹配，否则一律跳过（宁可漏发）。对方改名也能过——
            # 因为 userIDDict 始终返回该抖音号当前最新标题。
            aliases, _ = resolve_aliases_with_userdict(target, userIDDict)
            if not strict_title_match(header, aliases):
                logger.warning(
                    f"账号 {username} 表头标题与目标别名不严格匹配，跳过 {target_id!r} (表头 {header!r})"
                )
                not_found.append(target_id)
                continue

            # DRY_RUN：定位诊断模式。搜索/点击/表头校验全部执行完毕，但发送前停止（绝不误发）。
            # 用于云端定位「找不到目标/表头不匹配」，只输出结果，不触碰输入框发送动作。
            if config.get("dryRun"):
                dry_matched.append(target_id)
                logger.info(
                    f"账号 {username} [DRY_RUN] 目标 {target_id!r} 表头严格匹配通过，"
                    f"不发送（表头 {header!r}）"
                )
                continue

            try:
                result, detail = send_chat_message(page, username, target, config, item_selector)
                if result == "sent":
                    logger.info(f"账号 {username} 已向 {target_id} 发送并确认")
                elif result == "unverified":
                    unverified.append(target_id)
                    logger.warning(
                        f"账号 {username} 向 {target_id} 发送但无法确认 (unverified): {detail}"
                    )
                else:
                    not_found.append(target_id)
                    logger.warning(f"账号 {username} 向 {target_id} 发送失败: {detail}")
            except LoginRequired:
                logger.warning(f"账号 {username} 检测到安全验证，中止该账号")
                break
            except Exception as e:
                logger.warning(f"账号 {username} 给 {target_id} 发消息失败: {e}")
                not_found.append(target_id)

        if not_found:
            logger.warning(f"账号 {username} 本次未找到/未发送好友: {not_found}")
        if unverified:
            logger.warning(f"账号 {username} 发送但未确认的好友: {unverified}")
        if dry_matched:
            logger.info(f"账号 {username} [DRY_RUN] 表头严格匹配通过但不发送: {dry_matched}")
        logger.debug(f"账号 {username} 任务完成")
    finally:
        if owns_context:
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
                f"用户: {user.get('username', '未知用户')}, 目标好友: "
                f"{[t['id'] for t in user['targets']]}"
            )
        # profile 模式只能服务一个账号：profile 即该账号的登录态，绝不混用两套 Cookie
        profile_mode = not hasattr(browser, "new_context")
        profile_only_id = os.getenv("DOUYIN_PROFILE_UNIQUE_ID", "").strip()
        for user in userData:
            cookies = user["cookies"]
            targets = user["targets"]
            username = user.get("username", "未知用户")
            unique_id = user.get("unique_id", "")
            if profile_mode:
                if not profile_only_id:
                    logger.warning("profile 模式必须设置 DOUYIN_PROFILE_UNIQUE_ID，本次不执行任何账号")
                    break
                if unique_id != profile_only_id:
                    logger.warning(
                        f"profile 模式只处理 DOUYIN_PROFILE_UNIQUE_ID={profile_only_id}，跳过账号 {username}"
                    )
                    continue
            logger.info(f"开始处理账号 {username}")
            try:
                do_user_task(browser, username, cookies, targets)
                logger.info(f"账号 {username} 任务完成")
            except LoginRequired as e:
                logger.warning(f"账号 {username} 中止: {e}")
            except ChatUnavailable as e:
                logger.warning(f"账号 {username} 中止: {e}")
            except Exception as e:
                logger.error(f"账号 {username} 异常: {e}\n{traceback.format_exc()}")
    finally:
        browser.close()
        playwright.stop()
