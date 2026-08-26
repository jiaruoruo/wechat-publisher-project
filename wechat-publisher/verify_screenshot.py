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
print("TOKEN:", token, flush=True)

page.goto(f"https://mp.weixin.qq.com/cgi-bin/appmsg?begin=0&count=10&type=77&action=list_card&token={token}&lang=zh_CN", wait_until="networkidle", timeout=45000)
page.wait_for_timeout(6000)

# Screenshot
page.screenshot(path="storage/screenshots/draft_box_check.png", full_page=True)
print("SCREENSHOT SAVED", flush=True)

# Get detailed draft items with timestamps via evaluate
info = page.evaluate("""() => {
    const items = [];
    const nodes = document.querySelectorAll('a, div');
    for (const el of nodes) {
        const cls = el.className || '';
        if (typeof cls === 'string' && (cls.includes('appmsg') || cls.includes('draft') || cls.includes('card') || cls.includes('list-item'))) {
            const t = (el.textContent || '').trim();
            if (t && t.length > 2 && t.length < 60) items.push({cls: cls.slice(0,50), text: t.slice(0,50)});
        }
    }
    return items.slice(0, 25);
}""")
import json
print(json.dumps(info, ensure_ascii=False, indent=1), flush=True)
ctx.close()
b.close()
p.stop()
print("DONE", flush=True)
