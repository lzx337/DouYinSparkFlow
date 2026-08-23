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


def get_browser():
    """
    启动浏览器实例
    :return: 浏览器实例
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
        # 启动浏览器
        playwright = sync_playwright().start()

        # 优先使用系统安装的 Google Chrome：
        # 1) browser-actions/setup-chrome 会在 Actions 环境里安装 Chrome 并设置 CHROME_PATH
        # 2) 抖音对 Playwright 自带的 headless chromium 反爬更严格，
        #    账号2 的用户信息接口会被降级为 data:null，导致好友匹配失败
        chrome_path = os.environ.get("CHROME_PATH", "")
        if chrome_path and os.path.exists(chrome_path):
            print(f"使用系统 Chrome: {chrome_path}")
            return playwright, playwright.chromium.launch(
                headless=headless, executable_path=chrome_path
            )

        try:
            return playwright, playwright.chromium.launch(headless=headless, channel="chrome")
        except Exception:
            pass

        print("使用 Playwright 自带 Chromium")
        return playwright, playwright.chromium.launch(headless=headless)
    except Exception as e:
        # 捕获浏览器启动错误
        if "Executable doesn't exist" in str(e) and env != Environment.GITHUBACTION:
            print("浏览器可执行文件不存在！")
            install_browser()
            sys.exit(1)
        else:
            traceback.print_exc()
