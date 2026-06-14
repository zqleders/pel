import os
import sys
import json
import time
from datetime import datetime, timedelta
import requests
from playwright.sync_api import sync_playwright

# 从 GitHub Secrets / 环境变量中获取配置
EMAIL = os.getenv("PELLA_EMAIL")
PASSWORD = os.getenv("PELLA_PASSWORD")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

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

    # 发送图片
    if screenshot_path and os.path.exists(screenshot_path):
        photo_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
        try:
            with open(screenshot_path, 'rb') as photo:
                requests.post(photo_url, data={"chat_id": TG_CHAT_ID}, files={"photo": photo}, timeout=15)
        except Exception as e:
            print(f"发送 TG 图片通知失败: {e}")

def parse_expiry(expiry_str):
    """解析到期时间字符串，格式: '21:23:30 14/06/2026'"""
    # 格式为: 时:分:秒 日/月/年
    return datetime.strptime(expiry_str, "%H:%M:%S %d/%m/%Y")

def run():
    if not EMAIL or not PASSWORD:
        print("错误: 未配置账号或密码环境变。")
        sys.exit(1)

    with sync_playwright() as p:
        # 启动无头浏览器
        browser = p.chromium.launch(headless=True)
        # 设置窗口大小以确保截图清晰
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        print("正在打开登录页面...")
        page.goto("https://www.pella.app/login")
        
        # 等待输入框加载
        page.wait_for_selector("#identifier-field")
        page.fill("#identifier-field", EMAIL)
        
        page.wait_for_selector("#password-field")
        page.fill("#password-field", PASSWORD)
        
        # 使用更具鲁棒性的属性选择器点击 Continue 按钮
        print("点击登录按钮...")
        page.click('button[data-localization-key="formButtonPrimary"]')
        
        # 等待页面跳转或登录完成后，确保 Cookie 已写入
        page.wait_for_timeout(5000) 

        print("正在请求服务器列表 API...")
        # 直接使用 page.request 可以共享当前的登录状态(Cookie/Tokens)
        response = page.request.get("https://api.pella.app/user/servers")
        
        if response.status != 200:
            msg = f"❌ 获取服务器列表失败，API状态码: {response.status}"
            print(msg)
            page.screenshot(path="error_login.png")
            send_telegram_notification(msg, "error_login.png")
            browser.close()
            return

        try:
            data = response.json()
        except Exception:
            msg = "❌ 解析服务器列表 JSON 失败"
            page.screenshot(path="error_json.png")
            send_telegram_notification(msg, "error_json.png")
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
        now = datetime.utcnow() # API 通常返回 UTC 时间，根据实际情况可调整
        time_left = expiry_time - now

        print(f"距离到期还剩: {time_left}")

        renewed = False
        renew_msg = ""

        # 判断是否小于等于 2 小时
        if time_left <= timedelta(hours=2):
            print("⚠️ 服务器即将到期（小于2小时），准备寻找可用的 Renew 链接...")
            
            # 寻找 claimed 为 False 的链接
            target_link = None
            for r_link in renew_links:
                if not r_link.get("claimed"):
                    target_link = r_link.get("link")
                    break
            
            if target_link:
                print(f"🔗 发现可用续期链接，正在访问: {target_link}")
                # 页面跳转去访问续期链接
                page.goto(target_link)
                page.wait_for_timeout(5000) # 等待5秒让页面完成续期加载
                renewed = True
                renew_msg = "⏰ 触发了续期操作。"
            else:
                renew_msg = "❌ 警告: 服务器即将到期，但未找到未使用的 (claimed: false) 续期链接！"
                print(renew_msg)
        else:
            renew_msg = "✅ 服务器时间充足，无需续期。"
            print(renew_msg)

        # 如果进行了续期，或者服务器当前本身就不是 running 状态，检查并尝试启动
        # 根据需求：RENEW完成后，如果 status 不是 running 则去启动
        if (renewed or status != "running"):
            # 如果刚刚续期了，重新检查一次状态（或者直接盲发 start 请求）
            if status != "running":
                print("🔄 服务器未在运行，正在发送启动指令...")
                start_res = page.request.post(
                    "https://api.pella.app/server/start",
                    data={"id": server_id}
                )
                print(f"启动指令返回状态码: {start_res.status}")
                page.wait_for_timeout(3000)

        # 最终再次访问 API 检查结果并截图
        print("正在获取最终服务器状态以进行汇报...")
        page.goto("https://www.pella.app/dashboard") # 或者去到面板首页以便截图
        page.wait_for_timeout(5000)
        
        final_res = page.request.get("https://api.pella.app/user/servers")
        final_status_text = "未知"
        final_expiry_text = "未知"
        if final_res.status == 200:
            try:
                final_server = final_res.json().get("servers", [])[0]
                final_status_text = final_server.get("status")
                final_expiry_text = final_server.get("expiry")
            except Exception:
                pass

        # 截取最终状态图
        screenshot_name = "final_status.png"
        page.screenshot(path=screenshot_name, full_page=True)

        # 组合通知消息
        report_message = (
            f"📊 【Pella 自动化运维报告】\n"
            f"---------------------------\n"
            f"ℹ️ 续期动作: {renew_msg}\n"
            f"🔄 最终运行状态: {final_status_text}\n"
            f"📅 最终到期时间: {final_expiry_text}\n"
            f"⏱️ 检查执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        print("发送最终报告到 Telegram...")
        send_telegram_notification(report_message, screenshot_name)

        browser.close()

if __name__ == "__main__":
    run()
