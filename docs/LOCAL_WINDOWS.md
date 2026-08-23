# 本机 Windows 独立运行（账号 2 专用）

> 适用场景：账号 2（手机号 189******02）在 GitHub Actions 的 datacenter IP + 无头环境下频繁被风控，
> 需要短信验证码，无法自动化解决。把账号 2 迁到本地有头浏览器 + 本人已登录的浏览器 profile 是**降级运行**，
> 不是绕过风控。遇到验证码仍需人工扫码 / 短信。

## 原则

- **账号 1 仍留在 GitHub Actions**，保持 `dev` 分支的定时任务。
- **账号 2 只在本地跑**，使用独立的、已人工登录过抖音网页版的浏览器 profile。
- **绝不把「有头浏览器 + 真实 profile」作为 Windows 服务以 `LocalSystem` 身份运行**。
  服务会话（Session 0）里无法弹出窗口、无法人工扫码，还会让浏览器以高权限系统账号运行，
  风险极高且大概率被抖音判为异常。一定要用普通用户登录后的计划任务。
- 本仓库代码**不采集/不保存** Cookie 文件、完整 HTML 或 storage state，仅保存脱敏日志与截图。

## 准备

1. 安装 Python 3.11+ 与依赖：

   ```powershell
   cd <仓库目录>
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   playwright install chromium
   ```

2. 建立独立浏览器 profile（只用一次，之后长期复用）：

   ```powershell
   & "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
     --user-data-dir="C:\Users\<你的用户名>\.douyin_spark_browser_2"
   ```

   打开 `https://www.douyin.com/chat`，**人工扫码 / 短信登录账号 2**，并勾选“记住登录”。
   验证会话列表能正常显示后再关掉。后续自动化复用它，通常几天内不需要重新验证。

3. 准备本机运行参数（本地不需要往 GitHub 写 secrets）：

   ```powershell
   $env:TASKS = '[{"username":"账号2","unique_id":"aonananidegu","targets":["好友A","好友B"]}]'
   $env:DOUYIN_PROFILE_PATH = "C:\Users\<你的用户名>\.douyin_spark_browser_2"
   $env:DOUYIN_PROFILE_EXECUTABLE = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
   $env:DOUYIN_PROFILE_UNIQUE_ID = "aonananidegu"
   ```

   代码在本地 + 设置了 `DOUYIN_PROFILE_PATH` 时会复用上面这个已登录的独立 profile，
   从而尽量不触发短信验证（见 `core/browser.py`）。未设置时回退到全新 Chromium + Cookie 环境变量。

   **profile 模式一次只服务一个账号**：必须用 `DOUYIN_PROFILE_UNIQUE_ID` 指明这个 profile
   属于哪个 `unique_id`。只有与它匹配的账号会执行，其余账号一律跳过——因为 profile 本身就是
   那个账号的登录态，绝不允许再往 profile 里注入其他账号的 Cookie。账号 2 的 `unique_id` 是
   `aonananidegu`。此时**不需要**设置 `COOKIES_AONANANIDEGU`（本地 + profile 模式允许空 Cookie，
   登录态以 profile 为准）。

   `TASKS` 支持两种格式（与 GitHub Actions 一致）：

   ```json
   "targets": ["好友名"]                                            // 旧格式：字符串
   "targets": [{"id":"好友名","search_terms":["搜索词"],"title_aliases":["列表标题"]}]  // 新格式
   ```

## 运行

有头运行（能人工看到页面、随时处理验证码）：

```powershell
$env:PYTHONIOENCODING = "utf-8"
python main.py
```

- `DEBUG=true`（默认）时浏览器 `headless=False`，页面可见。
- 遇到“安全验证 / 验证码 / 短信验证”时程序会**安全停止该账号**并保存脱敏诊断文件到 `logs/debug/`，
  不会重试、不会绕过。

## 用任务计划程序定时

不要做成系统服务，也不要勾选“不管用户是否登录都要运行”。用**当前已登录的普通用户**创建计划任务：

1. 打开「任务计划程序」→「创建任务」。
2. 「常规」：勾选“只在用户登录时运行”，不要用 `LocalSystem`。
3. 「触发器」：时间按你想要的节奏自定。注意：云端 `schedule_dev.yml` 的 cron 是 `7 16 * * *`
   （UTC），对应**北京时间每天 00:07**；本地计划任务是本机时间，与云端彼此独立，两处互不影响。
4. 「操作」：

   ```
   程序或脚本: C:\Users\<你的用户名>\<仓库>\run_account2.bat
   起始于:     C:\Users\<你的用户名>\<仓库>
   ```

5. 新建 `run_account2.bat`：

   ```bat
   @echo off
   cd /d C:\Users\<你的用户名>\<仓库>
   set PYTHONIOENCODING=utf-8
   set DOUYIN_PROFILE_PATH=C:\Users\<你的用户名>\.douyin_spark_browser_2
   set DOUYIN_PROFILE_EXECUTABLE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe
   set DOUYIN_PROFILE_UNIQUE_ID=aonananidegu
   set TASKS=[{"username":"账号2","unique_id":"aonananidegu","targets":["好友A","好友B"]}]
   call .venv\Scripts\activate.bat
   python main.py >> logs\account2_local.log 2>&1
   ```

## 需要注意

- 账号 2 的验证码是**账号级风控**（数据中心 IP / 新环境触发），本方案只在本地已登录 profile 上
  降低触发频率，不能保证永远不再验证。
- 每天第一个验证码之后，可再人工确认一次本次运行没有误发：检查 `logs\app.log` 里的
  `未找到/未发送好友` 与 `发送但未确认` 两段汇总。
- **表头确认默认是硬性要求**：程序发送前必须读到会话表头且与 `title_aliases` 严格一致，否则**跳过不发送**。
  推断的表头选择器（`.messageChatItemTitle` 等）如果在你真实页面上取不到，会导致所有目标都被跳过——这是安全的
  “宁漏发”。想要真正发送，请在真实登录页核实表头标题的选择器后设置 `CHAT_HEADER_TITLE_SELECTOR`。
- **去重（at-most-once）只覆盖一次进程**：同一「账号+目标」在当次运行内只尝试一次；但如果同一天手动再次运行
  （新进程），仍可能重复发送。跨运行无法精确去重，请避免重复手动触发。
- 若 `main.py` 读取的 Cookie 环境变量为空，程序会在「本地 + 设置了 `DOUYIN_PROFILE_PATH`」时按
  “仅 profile 登录态”运行；否则该账号会被跳过。
