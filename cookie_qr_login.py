"""
抖音 Cookie QR 码登录提取工具
在 GitHub Actions 中运行，显示二维码供用户扫码登录后自动提取 Cookie
"""

import json
import base64
import os
import sys
import time
from playwright.sync_api import sync_playwright

GITHUB_REPO = "lzx337/DouYinSparkFlow"
BRANCH = "dev"
COOKIE_FILE_PATH = ".github/cookie_data.json"
GITHUB_TOKEN = os.environ.get("GH_TOKEN", "")

def main():
    with sync_playwright() as p:
        # 启动浏览器（headless 模式）
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        
        # 桌面版用户代理
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        print("🌐 正在打开抖音登录页...")
        page.goto("https://www.douyin.com/", wait_until="networkidle", timeout=30000)
        time.sleep(3)

        # ========== 点击登录按钮 ==========
        try:
            login_btn = page.locator("span:has-text('登录')").first
            if login_btn.is_visible(timeout=5000):
                login_btn.click()
                print("✅ 已点击登录按钮")
                time.sleep(3)
            else:
                print("⚠️ 未找到登录按钮，可能已登录或页面结构变化")
        except Exception as e:
            print(f"⚠️ 点击登录按钮失败: {e}")

        # ========== 切换到扫码登录 ==========
        qr_found = False
        try:
            # 尝试找"扫码登录"或"二维码"标签
            qr_tab = page.locator("span:has-text('扫码')").first
            if qr_tab.is_visible(timeout=3000):
                qr_tab.click()
                print("✅ 已切换到扫码登录")
                time.sleep(3)
                qr_found = True
            else:
                # 也可能是"扫一扫"或二维码图标
                qr_tab = page.locator("svg").first  # 二维码图标
                print("⚠️ 未找到扫码登录标签")
        except Exception as e:
            print(f"⚠️ 切换扫码登录失败: {e}")

        # ========== 截取二维码 ==========
        time.sleep(2)
        qr_captured = False
        
        # 尝试多种方式找到二维码元素
        selectors = [
            "canvas",                           # Canvas 渲染的二维码
            "img[src*='qrcode']",               # 图片二维码
            "img[src*='qr']",                   # 含 qr 的图片
            "[class*='qrcode']",                # class 含 qrcode
            "[class*='qr'] [class*='img']",     # 二维码容器
            "canvas[class*='qr']",              # QR canvas
        ]
        
        for sel in selectors:
            try:
                qr_el = page.locator(sel).first
                if qr_el.is_visible(timeout=2000):
                    qr_el.screenshot(path="qrcode.png")
                    print(f"✅ 已截取二维码 (selector: {sel})")
                    qr_captured = True
                    break
            except:
                continue
        
        # 如果没找到特定元素，截取整个页面
        if not qr_captured:
            print("⚠️ 未找到二维码元素，截取整个页面")
            page.screenshot(path="qrcode.png", full_page=False)
        
        # ========== 在 GitHub Job Summary 中显示二维码 ==========
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open("qrcode.png", "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            
            summary = f"""## 请用手机抖音扫码登录

![抖音登录二维码](data:image/png;base64,{b64})

### 操作步骤
1. 打开手机 **抖音 App**
2. 点击右上角 **扫一扫** 图标
3. **扫描上方二维码**并确认登录
4. 脚本会自动检测登录状态并提取 Cookie

⏳ 等待扫码中...（最长 120 秒）
"""
            with open(os.environ["GITHUB_STEP_SUMMARY"], "w", encoding="utf-8") as f:
                f.write(summary)
            print("✅ 二维码已显示在 Job Summary 中")
        
        # ========== 等待扫码登录 ==========
        print("⏳ 等待扫码登录...（最长 120 秒）")
        logged_in = False
        
        for i in range(120):  # 120 秒超时
            time.sleep(1)
            
            # 检查是否有登录 Cookie
            cookies = context.cookies()
            session_cookies = [c for c in cookies if c["name"] in ("sessionid", "sid_guard", "sid_ucp")]
            
            if session_cookies:
                logged_in = True
                print(f"✅ 检测到登录成功！({i+1}秒)")
                
                # ========== 保存 Cookie ==========
                cookie_list = []
                for c in cookies:
                    entry = {
                        "domain": c.get("domain", ".douyin.com"),
                        "name": c["name"],
                        "value": c["value"],
                        "path": c.get("path", "/"),
                        "secure": c.get("secure", False),
                        "httpOnly": c.get("httpOnly", False),
                        "sameSite": c.get("sameSite", "Lax"),
                    }
                    if "expires" in c and c["expires"]:
                        entry["expirationDate"] = c["expires"]
                    cookie_list.append(entry)
                
                print(f"📦 共获取 {len(cookie_list)} 条 Cookie")
                
                # 保存到文件
                os.makedirs(os.path.dirname(COOKIE_FILE_PATH), exist_ok=True)
                with open(COOKIE_FILE_PATH, "w", encoding="utf-8") as f:
                    json.dump(cookie_list, f, indent=2, ensure_ascii=False)
                print(f"✅ Cookie 已保存到 {COOKIE_FILE_PATH}")
                
                # 上传到 GitHub（使用 API）
                if GITHUB_TOKEN:
                    upload_to_github(json.dumps(cookie_list, indent=2, ensure_ascii=False))
                else:
                    print("⚠️ 未设置 GH_TOKEN，跳过上传")
                    print("📄 Cookie 内容已保存到文件，工作流会通过 actions/upload-artifact 提供下载")
                
                # 更新 Summary
                if os.environ.get("GITHUB_STEP_SUMMARY"):
                    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                        f.write(f"\n✅ **登录成功！** 已获取 {len(cookie_list)} 条 Cookie\n")
                        f.write(f"\n📤 Cookie 已上传到仓库 `{COOKIE_FILE_PATH}`\n")
                
                break
            
            # 进度提示
            if i % 15 == 0 and i > 0:
                print(f"⏳ 等待中... 已过 {i+1} 秒")
        
        browser.close()
        
        if not logged_in:
            print("❌ 登录超时（120秒），请重新运行工作流")
            
            if os.environ.get("GITHUB_STEP_SUMMARY"):
                with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                    f.write("\n❌ **登录超时**，请重新运行工作流\n")
            
            sys.exit(1)


def upload_to_github(content):
    """通过 GitHub API 上传 Cookie 文件到仓库"""
    import urllib.request
    
    api_base = f"https://api.github.com/repos/{GITHUB_REPO}"
    url = f"{api_base}/contents/{COOKIE_FILE_PATH}?ref={BRANCH}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }
    
    # Base64 编码
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    
    # 获取已有文件 SHA
    sha = ""
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req) as res:
            data = json.loads(res.read())
            sha = data.get("sha", "")
    except:
        pass
    
    # 上传文件
    body = json.dumps({
        "message": "update cookie from qr login",
        "content": encoded,
        "branch": BRANCH,
        "sha": sha,
    })
    
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req) as res:
            print(f"✅ Cookie 已上传到 GitHub (status: {res.status})")
    except Exception as e:
        print(f"❌ 上传失败: {e}")


if __name__ == "__main__":
    main()
