# -*- coding: utf-8 -*-
from playwright.sync_api import sync_playwright

p = sync_playwright().start()
b = p.chromium.launch(headless=True)
ctx = b.new_context(storage_state="storage/browser_session.json", viewport={"width": 1280, "height": 800})
page = ctx.new_page()

page.goto("https://mp.weixin.qq.com", wait_until="networkidle", timeout=45000)
page.wait_for_timeout(4000)
try:
    page.locator('.weui-desktop-menu__link:has-text("草稿箱")').first.click(timeout=8000)
    page.wait_for_timeout(5000)
except Exception as e:
    print("click fail:", str(e)[:60], flush=True)

text = page.evaluate("() => document.body.innerText")
print("=== DRAFT BOX (final) ===", flush=True)
print(text[:1800], flush=True)
ctx.close()
b.close()
p.stop()
print("DONE", flush=True)
