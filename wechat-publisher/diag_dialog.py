# -*- coding: utf-8 -*-
from playwright.sync_api import sync_playwright

p = sync_playwright().start()
b = p.chromium.launch(headless=True)
ctx = b.new_context(storage_state="storage/browser_session.json")
page = ctx.new_page()

page.goto("https://mp.weixin.qq.com", wait_until="networkidle", timeout=45000)
page.wait_for_timeout(2000)
el = page.locator('.new-creation__menu-title:has-text("文章")').first
el.click(timeout=4000)
page.wait_for_timeout(4000)

editor = None
for pg in ctx.pages:
    if "appmsg_edit" in pg.url:
        editor = pg
        break
editor.wait_for_timeout(10000)

# Find the education dialog and its close button
info = editor.evaluate("""() => {
    const r = {};
    const dialogs = Array.from(document.querySelectorAll('.weui-desktop-dialog, .education-dialog, [class*=dialog]')).filter(el => {
        const s = getComputedStyle(el);
        return s.display !== 'none' && el.offsetParent !== null;
    });
    r.visible_dialogs = dialogs.map(d => ({
        cls: d.className.toString().slice(0,80),
        buttons: Array.from(d.querySelectorAll('button, a, .weui-desktop-btn')).map(b => (b.textContent||'').trim().slice(0,20))
    }));
    // Any element with 我知道了 / 关闭 / 跳过
    r.dismiss = Array.from(document.querySelectorAll('button, a, span, i, div')).filter(el => {
        const t = (el.textContent||'').trim();
        return (t === '我知道了' || t === '关闭' || t === '跳过' || t === 'x' || t === 'X' || el.className.toString().includes('close')) && el.offsetParent !== null;
    }).map(el => el.tagName + ' | ' + (el.textContent||'').trim().slice(0,20) + ' | ' + el.className.toString().slice(0,60)).slice(0, 10);
    return r;
}""")
import json
print(json.dumps(info, ensure_ascii=False, indent=1))
ctx.close()
b.close()
p.stop()
