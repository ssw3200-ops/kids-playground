"""실제 운영 페이지 화면 캡처 (블로그용) — Actions 에서만 실행 (비밀번호는 Secrets)"""
import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright

URL = "https://ssw3200-ops.github.io/kids-playground/"
PW = os.environ["PAGE_PASSWORD"]
OUT = Path("shots")
OUT.mkdir(exist_ok=True)
GEO = {"latitude": 36.4801, "longitude": 127.2890}  # 세종시청 기준
DAY = os.environ.get("DAY_TIME") or ""


async def page(b, t=""):
    ctx = await b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3,
                              geolocation=GEO, permissions=["geolocation"], locale="ko-KR", timezone_id="Asia/Seoul")
    pg = await ctx.new_page()
    if t:
        await pg.clock.install(time=t)
    return ctx, pg


async def unlock(pg):
    await pg.goto(URL + "?shot=" + str(os.getpid()))
    await pg.wait_for_selector("#pw", state="visible")
    await pg.fill("#pw", PW)
    await pg.click("#pwBtn")
    await pg.wait_for_function("document.querySelector('#verdict .src') && document.querySelector('#verdict .src').innerText.includes('내 위치')", timeout=60000)
    await pg.wait_for_timeout(2500)


async def el(pg, sel, name):
    loc = pg.locator(sel).first
    await loc.scroll_into_view_if_needed()
    await pg.wait_for_timeout(600)
    await loc.screenshot(path=str(OUT / f"{name}.png"))
    print("saved", name)


async def places(pg, tab, name):
    await pg.click(f"#ptab button[data-k={tab}]")
    await pg.wait_for_timeout(2500)
    card = pg.locator("#ptab").locator("xpath=..")
    await card.scroll_into_view_if_needed()
    await pg.evaluate("document.querySelector('#ptab').scrollIntoView({block:'start'});window.scrollBy(0,-120)")
    await pg.wait_for_timeout(600)
    await pg.screenshot(path=str(OUT / f"{name}.png"))
    print("saved", name, await pg.inner_text("#pcount"))


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx, pg = await page(b)
        await pg.goto(URL)
        await pg.wait_for_selector("#pw", state="visible")
        await pg.wait_for_timeout(1500)
        await pg.screenshot(path=str(OUT / "01_lock.png"))
        await ctx.close()
        for tag, t in (("day", DAY), ("now", "")):
            if tag == "day" and not t:
                continue
            ctx, pg = await page(b, t)
            await unlock(pg)
            await pg.evaluate("window.scrollTo(0,0)")
            await pg.wait_for_timeout(400)
            await pg.screenshot(path=str(OUT / f"02_{tag}_top.png"))
            await el(pg, "#verdict", f"03_{tag}_verdict")
            await el(pg, "#days", f"04_{tag}_timeline")
            # 시간 칸 하나 눌러서 이유 보기
            hb = pg.locator(".hb.l1, .hb.l2, .hb.l0").nth(3)
            if await hb.count():
                await hb.click()
                await pg.wait_for_timeout(500)
                await el(pg, "#days >> xpath=..", f"05_{tag}_timeline_detail")
            await places(pg, "out", f"06_{tag}_places_out")
            await places(pg, "in", f"07_{tag}_places_in")
            await places(pg, "food", f"08_{tag}_places_food")
            await pg.screenshot(path=str(OUT / f"09_{tag}_full.png"), full_page=True)
            await ctx.close()
        await b.close()


asyncio.run(main())
