import os
import sys
import json
import time
from datetime import datetime, timedelta
import requests
from playwright.sync_api import sync_playwright

# ================= 配置开关 =================
ENABLE_SCREENSHOT = True  # 截图功能开关：True 开启，False 关闭
# ============================================

# 从 GitHub Secrets / 环境变量中获取配置
EMAIL = os.getenv("PELLA_EMAIL")
PASSWORD = os.getenv("PELLA_PASSWORD")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

def format_to_pella_time(dt_obj):
    """将 datetime 对象统一格式化为 '时:分:秒 日/月/年' 格式"""
    return dt_obj.strftime("%H:%M:%S %d/%m/%Y")

def send_telegram_notification(message, screenshot_path=None):
    """发送文字消息和截图到 Telegram"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("Telegram 配置缺失，跳过发送通知。")
        return

    # 发送文字
    text_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(text_url, json={"chat_id": TG_CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print(f"发送 TG 文字通知失败: {e}")

    # 发送图片（仅在开关开启且文件存在时发送）
    if ENABLE_SCREENSHOT and screenshot_path and os.path.exists(screenshot_path):
        photo_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
        try:
            with open(screenshot_path, 'rb') as photo:
                requests.post(photo_url, data={"chat_id": TG_CHAT_ID}, files={"photo": photo}, timeout=15)
        except Exception as e:
            print(f"发送 TG 图片通知失败: {e}")

def parse_expiry(expiry_str):
    """解析到期时间字符串，格式: '21:23:30 14/06/2026'"""
    return datetime.strptime(expiry_str, "%H:%M:%S %d/%m/%Y")

def run():
    if not EMAIL or not PASSWORD:
        print("错误: 未配置账号或密码环境变量。")
        sys.exit(1)

    with sync_playwright() as p:
        # 启动无头浏览器
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        # 定义一个变量用来监听并捕获带有 Bearer Token 的请求
        jwt_token = None

        # 监听所有发出的网络请求
        def handle_request(request):
            nonlocal jwt_token
            auth_header = request.headers.get("authorization")
            if auth_header and auth_header.startswith("Bearer ") and len(auth_header) > 20:
                jwt_token = auth_header

        page.on("request", handle_request)

        print("正在打开登录页面...")
        page.goto("https://www.pella.app/login")
        
        # 等待输入框加载并填入
        page.wait_for_selector("#identifier-field")
        page.fill("#identifier-field", EMAIL)
        
        page.wait_for_selector("#password-field")
        page.fill("#password-field", PASSWORD)
        
        print("点击登录按钮...")
        page.click('button[data-localization-key="formButtonPrimary"]')
        
        print("正在等待网络身份令牌(Token)生成...")
        try:
            page.wait_for_event(
                "request", 
                lambda req: req.headers.get("authorization") is not None and "Bearer" in req.headers.get("authorization"), 
                timeout=15000
            )
        except Exception:
            print("通过事件未精准截获到 Token，使用缓冲时间并尝试 JS 兜底提取...")
            page.wait_for_timeout(5000)

        # 兜底：如果事件没触发，直接通过前端全局对象提取
        if not jwt_token:
            try:
                jwt_token = page.evaluate("window.Clerk?.session?.getToken()")
                if jwt_token and not jwt_token.startswith("Bearer "):
                    jwt_token = f"Bearer {jwt_token}"
            except Exception:
                pass

        if not jwt_token:
            msg = "❌ 错误: 登录后无法获取到身份认证的 Authorization Token！"
            print(msg)
            screenshot_name = "error_token.png" if ENABLE_SCREENSHOT else None
            if ENABLE_SCREENSHOT:
                page.screenshot(path=screenshot_name)
            send_telegram_notification(msg, screenshot_name)
            browser.close()
            return

        print("成功获取 Authorization Token，直接请求服务器列表 API...")

        # 构造统一的请求头
        api_headers = {
            "accept": "*/*",
            "authorization": jwt_token,
            "content-type": "application/json",
            "origin": "https://www.pella.app",
            "referer": "https://www.pella.app/"
        }

        # 直接请求服务器列表
        response = context.request.get("https://api.pella.app/user/servers", headers=api_headers)
        
        if response.status != 200:
            msg = f"❌ 获取服务器列表失败，API状态码: {response.status}"
            print(msg)
            screenshot_name = "error_login.png" if ENABLE_SCREENSHOT else None
            if ENABLE_SCREENSHOT:
                page.screenshot(path=screenshot_name)
            send_telegram_notification(msg, screenshot_name)
            browser.close()
            return

        try:
            data = response.json()
        except Exception:
            msg = "❌ 解析服务器列表 JSON 失败"
            print(msg)
            screenshot_name = "error_json.png" if ENABLE_SCREENSHOT else None
            if ENABLE_SCREENSHOT:
                page.screenshot(path=screenshot_name)
            send_telegram_notification(msg, screenshot_name)
            browser.close()
            return

        servers = data.get("servers", [])
        if not servers:
            print("没有找到任何服务器。")
            browser.close()
            return

        # 针对第一个服务器进行操作
        server = servers[0]
        server_id = server.get("id")
        status = server.get("status")
        expiry_str = server.get("expiry")
        renew_links = server.get("renew_links", [])

        print(f"服务器 ID: {server_id}, 当前状态: {status}, 到期时间: {expiry_str}")

        # 计算剩余时间
        expiry_time = parse_expiry(expiry_str)
        now = datetime.utcnow() 
        time_left = expiry_time - now

        print(f"距离到期还剩: {time_left}")

        renewed = False
        renew_msg = ""

        # 判断是否小于等于 2 小时
        if time_left <= timedelta(hours=2):
            print("⚠️ 服务器即将到期（小于2小时），准备寻找可用的 Renew 链接...")
            
            target_link = None
            # 尝试查找可用的链接
            for r_link in renew_links:
                if not r_link.get("claimed"):
                    target_link = r_link.get("link")
                    break
            
            # 【新增延迟重试机制】：如果没找到可用链接，多等一会儿再重新请求几次 API 刷新数据
            retry_count = 0
            while not target_link and retry_count < 3:
                retry_count += 1
                print(f"⏳ 未找到未使用的链接，可能API尚未刷新。等待 5 秒后进行第 {retry_count} 次重试...")
                page.wait_for_timeout(5000)
                
                # 重新请求服务器列表 API
                retry_res = context.request.get("https://api.pella.app/user/servers", headers=api_headers)
                if retry_res.status == 200:
                    try:
                        retry_data = retry_res.json()
                        server = retry_data.get("servers", [])[0]
                        renew_links = server.get("renew_links", [])
                        # 重新寻找
                        for r_link in renew_links:
                            if not r_link.get("claimed"):
                                target_link = r_link.get("link")
                                break
                    except Exception:
                        print("重试请求解析 JSON 失败。")
            
            if target_link:
                print(f"🔗 发现可用续期链接，正在访问: {target_link}")
                page.goto(target_link)
                page.wait_for_timeout(5000) 
                renewed = True
                renew_msg = "⏰ 触发了续期操作。"
            else:
                renew_msg = "❌ 警告: 服务器即将到期，但未找到未使用的 (claimed: false) 续期链接！"
                print(renew_msg)
        else:
            renew_msg = "✅ 服务器时间充足，无需续期。"
            print(renew_msg)

        # RENEW完成后，如果 status 不是 running 则去启动
        if (renewed or status != "running"):
            if status != "running":
                print("🔄 服务器未在运行，正在发送启动指令...")
                start_res = context.request.post(
                    "https://api.pella.app/server/start",
                    data=json.dumps({"id": server_id}), 
                    headers=api_headers
                )
                print(f"启动指令返回状态码: {start_res.status}")
                page.wait_for_timeout(3000)

        # 最终再次访问 API 检查结果并汇报
        print("正在获取最终服务器状态以进行汇报...")
        final_res = context.request.get("https://api.pella.app/user/servers", headers=api_headers)
        final_status_text = "未知"
        final_expiry_text = "未知"
        if final_res.status == 200:
            try:
                final_server = final_res.json().get("servers", [])[0]
                final_status_text = final_server.get("status")
                final_expiry_text = final_server.get("expiry")
            except Exception:
                pass

        # 根据全局开关决定是否截取最终状态图
        screenshot_name = "final_status.png" if ENABLE_SCREENSHOT else None
        if ENABLE_SCREENSHOT:
            page.screenshot(path=screenshot_name, full_page=True)

        # 格式化当前执行时间，保证和到期时间的“时:分:秒 日/月/年”格式完全一致
        current_time_str = format_to_pella_time(datetime.now())

        # 组合通知消息
        report_message = (
            f"📊 【Pella 自动化运维报告】\n"
            f"---------------------------\n"
            f"ℹ️ 续期动作: {renew_msg}\n"
            f"🔄 最终运行状态: {final_status_text}\n"
            f"📅 最终到期时间: {final_expiry_text}\n"
            f"⏱️ 检查执行时间: {current_time_str}"
        )
        
        print("发送最终报告到 Telegram...")
        send_telegram_notification(report_message, screenshot_name)

        browser.close()

if __name__ == "__main__":
    run()
