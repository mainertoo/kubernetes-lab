"""Two-headless-browser e2e against a live beam: join, approve, share fake screen,
assert frames actually render on the receiver. Usage:
  python e2e_beam.py [base-url] [--relay]
"""

import asyncio
import sys

from playwright.async_api import async_playwright

BASE = next((a for a in sys.argv[1:] if not a.startswith("--")), "https://beam.mainertoo.com")
RELAY = "?relay=1" if "--relay" in sys.argv else ""


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--auto-select-desktop-capture-source=Entire screen",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        rx = await (await browser.new_context(ignore_https_errors=False)).new_page()
        tx_ctx = await browser.new_context()
        # Headless Shell has no display-capture; the fake camera exercises the
        # identical WebRTC pipeline (encode → ICE → decode → frames).
        await tx_ctx.add_init_script(
            "navigator.mediaDevices.getDisplayMedia = (c) =>"
            " navigator.mediaDevices.getUserMedia({ video: true, audio: false });"
        )
        tx = await tx_ctx.new_page()
        rx.on("console", lambda m: print(f"  [rx console] {m.type}: {m.text}") if m.type == "error" else None)
        tx.on("console", lambda m: print(f"  [tx console] {m.type}: {m.text}") if m.type == "error" else None)

        await rx.goto(f"{BASE}/screen{RELAY}")
        await rx.click("#start")
        await rx.wait_for_function("document.getElementById('code').textContent.trim().length >= 4")
        code = (await rx.text_content("#code")).strip()
        print(f"room: {code}  relay-forced: {bool(RELAY)}")

        await tx.goto(f"{BASE}/s{RELAY}")
        await tx.fill("#code", code)
        await tx.fill("#name", "e2e")
        await tx.click("form#join button")

        await rx.wait_for_selector("#approvals button:text('Allow')", timeout=15000)
        await rx.click("#approvals button:text('Allow')")

        await tx.wait_for_selector("#share:not([hidden])", timeout=15000)
        await tx.click("#share")

        # let media flow, then sample the receiver's video element twice
        await rx.wait_for_timeout(8000)
        s1 = await rx.evaluate(
            "() => { const v = document.getElementById('tv'); return {w: v.videoWidth, h: v.videoHeight, t: v.currentTime, paused: v.paused}; }"
        )
        await rx.wait_for_timeout(3000)
        s2 = await rx.evaluate(
            "() => { const v = document.getElementById('tv'); return {t: v.currentTime}; }"
        )
        sender_status = (await tx.text_content("#status") or "").strip()
        print(f"receiver video: {s1['w']}x{s1['h']} paused={s1['paused']} t {s1['t']:.1f} -> {s2['t']:.1f}")
        print(f"sender status: '{sender_status}'")

        ok = s1["w"] > 0 and s2["t"] > s1["t"] and not s1["paused"]
        path_ok = ("relayed" in sender_status) if RELAY else ("(direct)" in sender_status or "(relayed)" in sender_status)
        print(f"FRAMES FLOWING: {'YES' if ok else 'NO'}   PATH: {'OK' if path_ok else 'UNEXPECTED'}")
        await browser.close()
        sys.exit(0 if ok and path_ok else 1)


asyncio.run(main())
