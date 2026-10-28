import lzma
import struct
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tprint as tp  # noqa: E402


def test_lzma_block_roundtrip_without_end_marker():
    data = bytes(range(256)) * 16
    blk = tp.lzma_block(data)
    assert blk[0] == 0x5D and struct.unpack("<IQ", blk[1:13]) == (8192, len(data))
    assert lzma.decompress(blk, format=lzma.FORMAT_ALONE) == data
    # An end marker would add bytes; the reference size must match a marker-free stream.
    marked = lzma.compress(data, format=lzma.FORMAT_ALONE, filters=[
        {"id": lzma.FILTER_LZMA1, "dict_size": 8192, "lc": 3, "lp": 0, "pb": 2,
         "preset": 6, "nice_len": 128}])
    assert len(blk) < len(marked)


def test_to_head_centers_mirrors_and_packs_lsb_first():
    img = Image.new("1", (8, 2), 1)
    img.putpixel((0, 0), 0)
    data = tp.to_head(img)
    assert len(data) == 2 * tp.BPL
    off = tp.HEAD_DOTS - 1 - (tp.HEAD_DOTS - 8) // 2  # image x=0 -> dot 195 (mirrored)
    assert data[off // 8] == 1 << (off % 8)
    assert sum(data) == data[off // 8]


def test_print_buffers_header_and_checksum():
    cols = tp.MAX_COLS + 10
    data = bytes([0xAA]) * (cols * tp.BPL)
    bufs = tp.print_buffers(data, density=4)
    assert len(bufs) == 2 and all(len(b) == tp.BUF_SIZE for b in bufs)
    first, last = bufs
    assert first[2] == 0x02 and last[2] == 0x0C
    assert first[3] == (4 << 2 | 1 << 6)
    assert struct.unpack_from("<HBxHHB", first, 4) == (tp.MAX_COLS, tp.BPL, 1, 1, 4)
    assert struct.unpack_from("<H", last, 4)[0] == 10
    for b in bufs:
        n = struct.unpack_from("<H", b, 4)[0]
        csum = sum(b[2:14]) + sum(b[255:14 + n * tp.BPL:256])
        assert struct.unpack_from("<H", b)[0] == csum & 0xFFFF


def test_pack_per_buffer_small_page_is_one_block():
    bufs = tp.print_buffers(bytes(tp.BPL * 240))
    blocks, speed = tp.pack_per_buffer(bufs)
    assert len(blocks) == 1 and speed == 60
    assert lzma.decompress(blocks[0], format=lzma.FORMAT_ALONE) == b"".join(bufs)


def test_render_text_fits_label():
    img = tp.render_text("Hello\nWorld", 320, 240)
    assert img.size == (320, 240)
    assert img.convert("L").getextrema() == (0, 255)  # has ink and margin


def test_bt_frame_layout():
    f = tp.bt_frame(tp.CMD_CHECK)
    assert f == bytes.fromhex("7e5a0c001001aa1201000001") + bytes(4)
    f = tp.bt_frame(tp.CMD_BULK, 512, 3)
    assert f[7] == tp.CMD_BULK and f[12:16] == bytes([0x00, 0x02, 0x03, 0x00])
    assert struct.unpack_from("<H", f, 8)[0] == sum(f[10:16])


def test_bt_data_frames():
    blk = bytes([0x42]) * 1100
    frames = tp.bt_data_frames(blk)
    assert len(frames) == 3 and all(len(f) == 512 for f in frames)
    f = frames[2]
    assert f[:6] == bytes.fromhex("7e5afc011002") and f[6:8] == b"\xaa\xbb"
    assert f[10:12] == bytes([2, 3])
    assert f[12:112] == bytes([0x42]) * 100 and f[112:] == bytes(400)
    assert struct.unpack_from("<H", f, 8)[0] == sum(f[10:]) & 0xFFFF
    assert b"".join(fr[12:] for fr in frames)[:1100] == blk


def test_pack_grouped_limits_blocks_and_picks_speed():
    import random
    rng = random.Random(1)
    dither = bytes(sum(1 << k for k in range(8) if rng.random() < 0.25) for _ in range(tp.BPL * 400))
    bufs = tp.print_buffers(dither)
    blocks, speed = tp.pack_grouped(bufs)
    assert len(blocks) > 1 and all(len(b) <= tp.BUF_SIZE for b in blocks)
    joined = b"".join(lzma.decompress(b, format=lzma.FORMAT_ALONE) for b in blocks)
    assert joined == b"".join(bufs) and speed <= 20  # dense halftone prints slowly
    blocks, speed = tp.pack_grouped(tp.print_buffers(bytes(tp.BPL * 400)))
    assert len(blocks) == 2 and speed == 60  # blank page: 4 buffers per block


class FakeLink(tp.Printer):
    """Scripted printer: idle, then printing after START, then idle once data is committed."""
    ST = 1

    def __init__(self):
        self.log, self.printing, self.blocks = [], False, []

    def cmd(self, cmd, p=0, p2=None):
        self.log.append(cmd)
        if cmd == tp.CMD_START:
            self.printing = True
        if cmd == tp.CMD_BUF_FULL and len(self.blocks) == 1:
            self.printing = False
        return bytes([0, 0, 0, 0x40 if self.printing else 0, 0])

    def send_block(self, blk):
        self.blocks.append(blk)


def test_print_flow_order():
    link = FakeLink()
    link.print_image(Image.new("1", (320, 240), 0))
    cmds = [c for c in link.log if c != tp.CMD_STATUS]
    assert cmds == [tp.CMD_CHECK, tp.CMD_START, tp.CMD_BUF_FULL]
    assert len(link.blocks) == 1


def test_render_qr_modules_match_matrix():
    import segno
    img = tp.render_qr("https://example.com", 224)
    qr = segno.make("https://example.com", error="m")
    m = qr.symbol_size(border=0)[0]
    scale = img.width // (m + 4)
    assert img.width == img.height == (m + 4) * scale and scale >= 2
    for y, row in enumerate(qr.matrix):
        for x, v in enumerate(row):
            px = img.getpixel(((x + 2) * scale + scale // 2, (y + 2) * scale + scale // 2))
            assert (px == 0) == bool(v)


def test_render_qr_rejects_data_too_long():
    import pytest
    with pytest.raises(ValueError):
        tp.render_qr("x" * 500, 100)


def test_qr_label_layouts():
    wide = tp.render_qr_label("hello", "Box 12", 320, 240)
    assert wide.size == (320, 240)
    left, right = wide.crop((0, 0, 160, 240)), wide.crop((240, 0, 320, 240))
    assert left.convert("L").getextrema()[0] == 0  # QR on the left
    assert right.convert("L").getextrema()[0] == 0  # text on the right
    tall = tp.render_qr_label("hello", "Box 12", 240, 400)
    assert tall.crop((0, 300, 240, 400)).convert("L").getextrema()[0] == 0  # text below
    alone = tp.render_qr_label("hello", "", 320, 240)
    bbox = Image.eval(alone.convert("L"), lambda v: 255 - v).getbbox()
    assert bbox is not None and abs((bbox[0] + bbox[2]) / 2 - 160) <= 8  # centered


def test_render_text_wraps_in_narrow_area():
    narrow = tp.render_text("three short words", 100, 240)
    ink = Image.eval(narrow.convert("L"), lambda v: 255 - v).getbbox()
    assert ink is not None and ink[3] - ink[1] > 60  # wrapped over several lines


def test_render_text_never_splits_words(monkeypatch):
    seen = []
    real = tp.ImageDraw.ImageDraw.multiline_text
    monkeypatch.setattr(tp.ImageDraw.ImageDraw, "multiline_text",
                        lambda self, xy, text, **kw: (seen.append(text), real(self, xy, text, **kw)))
    tp.render_text("Supvan driver", 90, 240)
    assert seen == ["Supvan\ndriver"]


def test_load_config_first_found(tmp_path):
    missing, a, b = tmp_path / "none.toml", tmp_path / "a.toml", tmp_path / "b.toml"
    a.write_text('bt_address = "A4:93:40:05:6A:13"\n')
    b.write_text('bt_address = "00:00:00:00:00:00"\n')
    assert tp.load_config([missing, a, b]) == {"bt_address": "A4:93:40:05:6A:13"}
    assert tp.load_config([missing]) == {}


def test_connect_prefers_configured_bt_and_falls_back_to_usb(monkeypatch):
    made = []

    class Bt:
        def __init__(self, addr):
            made.append(("bt", addr))
            if addr == "down":
                raise OSError("host is down")

    monkeypatch.setattr(tp, "BtPrinter", Bt)
    monkeypatch.setattr(tp, "UsbPrinter", lambda: made.append(("usb",)) or "usb")
    assert isinstance(tp.connect(config={"bt_address": "up"}), Bt)
    assert tp.connect(config={"bt_address": "down"}) == "usb"
    assert tp.connect(config={}) == "usb"
    tp.connect("explicit", config={"bt_address": "up"})
    assert made == [("bt", "up"), ("bt", "down"), ("usb",), ("usb",), ("bt", "explicit")]
