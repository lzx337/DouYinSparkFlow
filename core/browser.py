import os, sys
import subprocess
import traceback
from playwright.sync_api import sync_playwright
from utils.config import DEBUG, get_environment, Environment

PLAYWRIGHT_BROWSERS_PATH = "../chrome"


def install_browser():
    """
    安装 Chromium 浏览器
    """
    try:
        subprocess.run(["playwright", "install", "chromium"], check=True)
        print("浏览器安装完成，请重新运行程序。")
    except subprocess.CalledProcessError as e:
        print(f"发生未知错误：{e}")


def _proxy_args():
    """返回浏览器的代理启动参数。

    本自动化只访问抖音（国内站，直连可达），不需要代理。但 Chromium 默认继承
    系统代理设置，一旦 Clash / V2Ray 之类的代理程序挂掉、却把系统代理留在指向
    死端口的状态，浏览器就会 net::ERR_PROXY_CONNECTION_FAILED，整个任务失败。

    2026-09-28 实测发生过：clash-verge-service 在跑、7897 端口却无人监听，
    系统代理仍指向 127.0.0.1:7897，账号1 和账号2 同时失败。

    因此默认绕过系统代理。确有需要时用 DOUYIN_PROXY_SERVER 显式指定，
    例如 DOUYIN_PROXY_SERVER=http://127.0.0.1:7897。
    """
    server = os.getenv("DOUYIN_PROXY_SERVER", "").strip()
    if server:
        return ["--proxy-server=" + server]
    return ["--no-proxy-server"]


def get_browser():
    """
    启动浏览器实例
    :return: (playwright, browser) 或 (playwright, persistent_context)

    本地 + 设置了 DOUYIN_PROFILE_PATH 时，复用「已人工登录的独立浏览器 profile」
    （账号 2 的降级运行路径，见 docs/LOCAL_WINDOWS.md）。此时返回的第二个值是
    persistent_context，而不是可 new_context() 的 browser 对象。
    """
    headless = True

    env = get_environment()
    if env == Environment.LOCAL:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.abspath(
            os.path.join(os.path.dirname(__file__), PLAYWRIGHT_BROWSERS_PATH)
        )
        if DEBUG:
            headless = False
    elif env == Environment.PACKED:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.abspath(
            os.path.join(os.path.dirname(sys.executable), PLAYWRIGHT_BROWSERS_PATH)
        )

    try:
        playwright = sync_playwright().start()

        # 本地降级运行：复用已人工登录的独立 profile（不绕过登录/短信，仅降低触发频率）
        profile = ""
        if env == Environment.LOCAL:
            profile = os.getenv("DOUYIN_PROFILE_PATH", "").strip()
        if profile:
            launch_kwargs = {"headless": headless}
            executable = os.getenv("DOUYIN_PROFILE_EXECUTABLE", "").strip()
            if executable and os.path.exists(executable):
                launch_kwargs["executable_path"] = executable
            if not headless:
                launch_kwargs["args"] = ["--start-maximized"] + _proxy_args()
                launch_kwargs["no_viewport"] = True
            else:
                # 无头静默模式固定桌面视口，与云端一致，降低抖音无头降级渲染概率
                launch_kwargs["args"] = _proxy_args()
                launch_kwargs["viewport"] = {"width": 1440, "height": 900}
            context = playwright.chromium.launch_persistent_context(
                profile, **launch_kwargs
            )
            return playwright, context

        # 常规路径：GitHub Actions / 本地新开浏览器
        browser = playwright.chromium.launch(headless=headless, args=_proxy_args())
        return playwright, browser
    except Exception as e:
        if "Executable doesn't exist" in str(e) and env != Environment.GITHUBACTION:
            print("浏览器可执行文件不存在！")
            install_browser()
            sys.exit(1)
        else:
            traceback.print_exc()
