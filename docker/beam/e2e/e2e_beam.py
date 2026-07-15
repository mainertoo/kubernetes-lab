"""Two-headless-browser e2e against a live beam: join, approve, share fake screen,
assert frames actually render on the receiver, then cast a photo over the
DataChannel and assert it displays. Usage:
  python e2e_beam.py [base-url] [--relay]
"""

import asyncio
import struct
import sys
import zlib

from playwright.async_api import async_playwright


def make_png(w: int = 8, h: int = 8) -> bytes:
    """Minimal valid RGBA PNG (solid red) — no imaging deps needed."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack("!I", len(data)) + c + struct.pack("!I", zlib.crc32(c) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\xff\x00\x00\xff" * w for _ in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )

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
        # bypass_csp: beam ships script-src 'self' (no unsafe-eval) — right for
        # users, but it blocks the harness's evaluate() string injections.
        rx = await (await browser.new_context(bypass_csp=True)).new_page()
        tx_ctx = await browser.new_context(bypass_csp=True)
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
        sender_path = (await tx.text_content("#path") or "").strip()
        print(f"receiver video: {s1['w']}x{s1['h']} paused={s1['paused']} t {s1['t']:.1f} -> {s2['t']:.1f}")
        print(f"sender path: '{sender_path}'")

        ok = s1["w"] > 0 and s2["t"] > s1["t"] and not s1["paused"]
        path_ok = ("relayed" in sender_path) if RELAY else ("direct" in sender_path or "relayed" in sender_path)

        # v1 phone mode: cast a photo over the DataChannel, assert it renders.
        await tx.set_input_files(
            "#photopick",
            files=[{"name": "e2e.png", "mimeType": "image/png", "buffer": make_png()}],
        )
        try:
            await rx.wait_for_function(
                "() => { const i = document.getElementById('photo');"
                " return !i.hidden && i.naturalWidth > 0; }",
                timeout=20000,
            )
            photo_ok = True
        except Exception:
            photo_ok = False
        print(f"photo cast: {'OK' if photo_ok else 'FAILED'}")

        print(
            f"FRAMES FLOWING: {'YES' if ok else 'NO'}   PATH: {'OK' if path_ok else 'UNEXPECTED'}"
            f"   PHOTO: {'OK' if photo_ok else 'FAILED'}"
        )
        await browser.close()
        sys.exit(0 if ok and path_ok and photo_ok else 1)


asyncio.run(main())
