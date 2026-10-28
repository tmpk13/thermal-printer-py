"""Browser check of the label designer against a fake printer (needs Playwright's Chromium).

Usage: e2e_web.py [OUT_DIR]  - screenshots and printed labels are written there.
"""
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
import server  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="label-e2e-")
printed = []
class Fake:
    def label_mm(self): return (40, 30)
    def status(self): return {"buf_full": False, "busy": False, "printing": False}
    def print_image(self, img, d):
        img.save(f"{OUT}/printed{len(printed)}.png"); printed.append((img.size, d))
    def close(self): pass

httpd = server.make_server("127.0.0.1", 0, Fake)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{httpd.server_address[1]}/"
errors = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1400, "height": 900})
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
    pg.goto(url)
    pg.wait_for_selector(".chip.ok")
    pg.click("#add-rect")
    pg.click("#add-qr")
    pg.fill("#props [data-key=w]", "18"); pg.fill("#props [data-key=h]", "18")
    pg.click("#add-text")
    pg.fill("#props [data-key=x]", "20"); pg.fill("#props [data-key=w]", "18.5")
    pg.fill("#props textarea", "Box 12 ")
    pg.wait_for_function("document.querySelector('#uni-search').placeholder.startsWith('Search')")
    pg.fill("#uni-search", "tick")
    pg.wait_for_selector("#uni-results .glyph")
    first = pg.inner_text("#uni-results .glyph >> nth=0")
    pg.click("#uni-results .glyph >> nth=0")
    text = pg.input_value("#props textarea")
    # drag the text box by 2 mm-ish
    box = pg.locator(".box.selected").bounding_box()
    pg.mouse.move(box["x"] + 10, box["y"] + 10); pg.mouse.down(); pg.mouse.move(box["x"] + 10, box["y"] + 30); pg.mouse.up()
    y_after = pg.input_value("#props [data-key=y]")
    pg.fill(".card.current .qty input", "3")
    pg.click("#new-label"); pg.click("#add-text"); pg.fill("#props textarea", "Second")
    pg.check("#label-rotate")
    pg.locator(".box.selected").click()
    pg.keyboard.press("r")
    rot_dims = (pg.input_value("#props [data-key=w]"), pg.input_value("#props [data-key=h]"))
    rot_label = pg.inner_text("#rotate-el")
    pg.keyboard.press("Shift+R")
    pg.keyboard.press("Shift+R")
    rot_back = pg.inner_text("#rotate-el")
    pg.click("#rotate-el")
    pg.click("#rotate-el")
    rot_final = pg.inner_text("#rotate-el")
    pg.screenshot(path=f"{OUT}/editor.png")
    pg.click("#print-batch")
    pg.wait_for_function("document.querySelector('#message').textContent.startsWith('Printed')", timeout=15000)
    msg = pg.inner_text("#message")
    pg.reload(); pg.wait_for_selector(".card")
    cards = pg.locator(".card").count()
    # Handles of an element sent behind another must still be grabbable.
    pg.click("#new-label"); pg.click("#add-rect"); pg.click("#add-text")
    pg.click("text=Back")
    w_before = float(pg.input_value("#props [data-key=w]"))
    hb = pg.locator(".handle.se").bounding_box()
    hx, hy = hb["x"] + hb["width"] / 2, hb["y"] + hb["height"] / 2
    pg.mouse.move(hx, hy); pg.mouse.down(); pg.mouse.move(hx - 60, hy); pg.mouse.up()
    covered = (pg.locator("#props h3").inner_text(), w_before, float(pg.input_value("#props [data-key=w]")))
    pg.emulate_media(color_scheme="dark"); pg.screenshot(path=f"{OUT}/dark.png")
    b.close()
assert not errors, errors
assert ord(first) == 0x2713 and text == "Box 12 \u2713", (first, text)
assert y_after != "1.5", "drag did not move the box"
assert msg == "Printed 4 labels", msg
assert printed == [((320, 240), 4)] * 4, printed  # rotated label still prints 40 mm across
assert rot_dims == ("30", "27") and rot_label == "Rotate (90)", (rot_dims, rot_label)
assert (rot_back, rot_final) == ("Rotate (270)", "Rotate (90)"), (rot_back, rot_final)
assert covered[0] == "Text" and covered[2] < covered[1], covered
assert cards == 2, "batch not restored after reload"
print(f"ok, output in {OUT}")
