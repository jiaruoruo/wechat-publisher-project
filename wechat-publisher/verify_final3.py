# -*- coding: utf-8 -*-
from playwright.sync_api import sync_playwright

p = sync_playwright().start()
b = p.chromium.launch(headless=True)
ctx = b.new_context(storage_state="storage/browser_session.json", viewport={"width": 1280, "height": 800})
page = ctx.new_page()

page.goto("https://mp.weixin.qq.com", wait_until="networkidle", timeout=45000)
page.wait_for_timeout(3000)
home_url = page.url
token = home_url.split("token=")[1].split("&")[0] if "token=" in home_url else ""

page.goto(f"https://mp.weixin.qq.com/cgi-bin/appmsg?begin=0&count=10&type=77&action=list_card&token={token}&lang=zh_CN", wait_until="networkidle", timeout=45000)
page.wait_for_timeout(5000)
text = page.evaluate("() => document.body.innerText")
lines = [l.strip() for l in text.split("\n") if l.strip()]
print("=== DRAFT BOX (FINAL) ===", flush=True)
# Print lines around 草稿箱 section
for i, l in enumerate(lines):
    if l in ("草稿箱", "文章模板", "全部草稿") or i > 34 and i < 55:
        print(repr(l), flush=True)
ctx.close()
b.close()
p.stop()
print("DONE", flush=True)
