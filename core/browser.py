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
                launch_kwargs["args"] = ["--start-maximized"]
                launch_kwargs["no_viewport"] = True
            context = playwright.chromium.launch_persistent_context(
                profile, **launch_kwargs
            )
            return playwright, context

        # 常规路径：GitHub Actions / 本地新开浏览器
        browser = playwright.chromium.launch(headless=headless)
        return playwright, browser
    except Exception as e:
        if "Executable doesn't exist" in str(e) and env != Environment.GITHUBACTION:
            print("浏览器可执行文件不存在！")
            install_browser()
            sys.exit(1)
        else:
            traceback.print_exc()
