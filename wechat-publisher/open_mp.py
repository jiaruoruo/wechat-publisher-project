# -*- coding: utf-8 -*-
import time
from playwright.sync_api import sync_playwright

p = sync_playwright().start()
b = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
ctx = b.new_context(
    storage_state="storage/browser_session.json",
    viewport={"width": 1280, "height": 800},
)
page = ctx.new_page()
page.goto("https://mp.weixin.qq.com", wait_until="networkidle", timeout=45000)
page.wait_for_timeout(3000)
print("URL:", page.url[:120])
print("Title:", page.title())

# Keep browser open so user can view it
print("BROWSER_OPEN - user is viewing the backend")
# Stay alive until killed
try:
    while True:
        time.sleep(5)
except KeyboardInterrupt:
    pass
ctx.close()
b.close()
p.stop()
