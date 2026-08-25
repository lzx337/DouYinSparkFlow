# 尝试从 .env 文件加载环境变量
import os
if os.path.exists(".env"):
    from dotenv import load_dotenv

    load_dotenv(".env")

from core.tasks import runTasks

# 退出码：0=全部完成无待处理；非 0=存在未发送/未确认/时间不确定跳过或账号异常，
# 让 CI 步骤失败并触发 failure() 制品上传，保证可观测性。
raise SystemExit(runTasks())
