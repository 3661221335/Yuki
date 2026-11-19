#!/usr/bin/env python3
"""Probe Serv00 registration form structure via Playwright (single process)."""
import asyncio, json
from playwright.async_api import async_playwright

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
URL = "https://www.serv00.com/register-account/"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=EDGE,
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="en-US",
        )
        page = await ctx.new_page()
        resp = await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        print("STATUS", resp.status if resp else None)
        await page.wait_for_timeout(3000)
        print("TITLE", await page.title())
        print("URL", page.url)

        # Dump all forms + inputs
        forms = await page.evaluate("""() => {
          return Array.from(document.forms).map(f => ({
            action: f.action, method: f.method,
            fields: Array.from(f.querySelectorAll('input,select,textarea,button')).map(el => ({
              tag: el.tagName, type: el.type||'', name: el.name||'', id: el.id||'',
              value: (el.value||'').slice(0,80), placeholder: el.placeholder||'',
              required: el.required||false,
              options: el.tagName==='SELECT' ? Array.from(el.options).slice(0,8).map(o=>({v:o.value,t:o.text})) : undefined,
              text: el.tagName==='BUTTON' ? (el.innerText||'').slice(0,80) : undefined
            }))
          }));
        }""")
        print("FORMS", json.dumps(forms, ensure_ascii=False, indent=2)[:8000])

        # Look for the anti-bot question text context
        q = await page.evaluate("""() => {
          const els = Array.from(document.querySelectorAll('*')).filter(e =>
            e.children.length===0 && (e.textContent||'').includes('hosting service'));
          return els.map(e => e.parentElement ? e.parentElement.outerHTML.slice(0,1200) : e.outerHTML.slice(0,1200));
        }""")
        print("QUESTION_CTX", json.dumps(q, ensure_ascii=False)[:3000])

        await page.screenshot(path="_reg_probe.png", full_page=True)
        print("SCREENSHOT saved")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
