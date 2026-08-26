# -*- coding: utf-8 -*-
from playwright.sync_api import sync_playwright

p = sync_playwright().start()
b = p.chromium.launch(headless=True)
ctx = b.new_context(storage_state="storage/browser_session.json", viewport={"width": 1280, "height": 800})
page = ctx.new_page()

page.goto("https://mp.weixin.qq.com", wait_until="networkidle", timeout=45000)
page.wait_for_timeout(3000)
token = page.url.split("token=")[1].split("&")[0] if "token=" in page.url else ""
page.goto(f"https://mp.weixin.qq.com/cgi-bin/appmsg?begin=0&count=20&type=77&action=list_card&token={token}&lang=zh_CN", wait_until="networkidle", timeout=45000)
page.wait_for_timeout(6000)
text = page.evaluate("() => document.body.innerText")
lines = [l.strip() for l in text.split("\n") if l.strip()]
print("=== DRAFT BOX (FINAL VERIFICATION) ===", flush=True)
found = False
for l in lines:
    if len(l) > 2 and ('电脑' in l or 'AI时代' in l or '更新于' in l or '报税' in l or '科学发现' in l):
        print(repr(l), flush=True)
        if '电脑' in l: found = True
print(f"ARTICLE FOUND: {found}", flush=True)
ctx.close()
b.close()
p.stop()
print("DONE", flush=True)
