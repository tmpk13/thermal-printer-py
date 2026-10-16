#!/usr/bin/env python3
"""Print text or images to a Supvan T50 Pro thermal label printer over USB HID."""
import argparse
import ctypes as C
import glob
import os
import select
import struct
import sys
import time

from PIL import Image, ImageDraw, ImageFont

HID_ID = "HID_ID=0003:00001820:0000207F"
HEAD_DOTS, BPL, DOTS_PER_MM = 384, 48, 8  # 48 mm head at 203 dpi
BUF_SIZE, HDR = 4096, 14
MAX_COLS = 4074 // BPL  # image bytes per print buffer, in whole columns
CMD_BUF_FULL, CMD_STATUS, CMD_CHECK, CMD_START, CMD_STOP = 0x10, 0x11, 0x12, 0x13, 0x14
CMD_MATERIAL, CMD_BULK = 0x30, 0x5C
# Pillow "1" rows are MSB-first with 1 = white; the head wants LSB-first with 1 = burn.
PACK = bytes(int(f"{i ^ 0xFF:08b}"[::-1], 2) for i in range(256))
ERRORS = {(1, 0x02): "label read/write error", (1, 0x04): "label roll end",
          (1, 0x08): "label mode mismatch", (1, 0x10): "ribbon read/write error",
          (1, 0x20): "ribbon end", (2, 0x08): "printhead too hot",
          (3, 0x08): "cover open", (4, 0x01): "no label installed"}


class LzmaOptions(C.Structure):
    _fields_ = [("dict_size", C.c_uint32), ("preset_dict", C.c_void_p),
                ("preset_dict_size", C.c_uint32), ("lc", C.c_uint32), ("lp", C.c_uint32),
                ("pb", C.c_uint32), ("mode", C.c_int), ("nice_len", C.c_uint32),
                ("mf", C.c_int), ("depth", C.c_uint32), ("ext_flags", C.c_uint32),
                ("ext_size_low", C.c_uint32), ("ext_size_high", C.c_uint32),
                ("reserved", C.c_uint32 * 5), ("reserved_ptr", C.c_void_p * 2)]


class LzmaFilter(C.Structure):
    _fields_ = [("id", C.c_uint64), ("options", C.c_void_p)]


def lzma_block(data: bytes) -> bytes:
    """LZMA1 "alone" stream without an end marker; the firmware drops data after one.

    Python's lzma module always writes the marker, so liblzma's LZMA1EXT raw
    encoder (ext_flags = 0 means no marker) is called directly.
    """
    lib = C.CDLL("liblzma.so.5")
    opts = LzmaOptions()
    if lib.lzma_lzma_preset(C.byref(opts), 6):
        raise RuntimeError("lzma preset failed")
    opts.dict_size, opts.lc, opts.lp, opts.pb, opts.nice_len, opts.ext_flags = 8192, 3, 0, 2, 128, 0
    filters = (LzmaFilter * 2)((0x4000000000000002, C.addressof(opts)), (2**64 - 1, None))
    out, pos = C.create_string_buffer(len(data) * 2 + 1024), C.c_size_t(0)
    lib.lzma_raw_buffer_encode.argtypes = [C.c_void_p, C.c_void_p, C.c_char_p, C.c_size_t,
                                           C.c_char_p, C.POINTER(C.c_size_t), C.c_size_t]
    rc = lib.lzma_raw_buffer_encode(filters, None, data, len(data), out, C.byref(pos), len(out))
    if rc:
        raise RuntimeError(f"lzma encode failed ({rc})")
    return struct.pack("<BIQ", 0x5D, 8192, len(data)) + out.raw[:pos.value]


def to_head(img: Image.Image) -> bytes:
    """Rows become feed columns: center the image on the head, pack 48 bytes per row."""
    img = img.convert("1")
    canvas = Image.new("1", (HEAD_DOTS, img.height), 1)
    canvas.paste(img, ((HEAD_DOTS - img.width) // 2, 0))
    return canvas.tobytes().translate(PACK)


def print_buffers(data: bytes, density: int = 4) -> list[bytes]:
    """Split packed columns into 4096-byte buffers with the firmware header and checksum."""
    cols = len(data) // BPL
    starts = range(0, cols, MAX_COLS)
    out = []
    for i, c in enumerate(starts):
        n, last = min(MAX_COLS, cols - c), i == len(starts) - 1
        b = bytearray(BUF_SIZE)
        b[2] = (0x02 if i == 0 else 0) | (0x0C if last else 0)  # page start / page + job end
        b[3] = (density & 15) << 2 | 1 << 6  # black density, material = label
        struct.pack_into("<HBxHHB", b, 4, n, BPL, 1, 1, density & 15)  # cols, bpl, margins, red
        b[HDR:HDR + n * BPL] = data[c * BPL:(c + n) * BPL]
        csum = sum(b[2:HDR]) + sum(b[255:HDR + n * BPL:256])
        out.append(bytes(struct.pack("<H", csum & 0xFFFF) + b[2:]))
    return out


def pack_blocks(buffers: list[bytes]) -> tuple[list[bytes], int]:
    """Whole page in one block when it compresses small, else one block per buffer."""
    singles = [lzma_block(b) for b in buffers]
    if sum(map(len, singles)) <= 4000:
        whole = lzma_block(b"".join(buffers))
        if len(whole) <= BUF_SIZE:
            return [whole], 60
    return singles, 60 if len(singles) == 1 else 20


class Printer:
    def __init__(self, path: str | None = None):
        path = path or find_hidraw()
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def _write(self, data: bytes):
        # Leading 0x00 is the report ID; without it hidraw eats a leading zero byte.
        os.write(self.fd, b"\0" + data.ljust(64, b"\0"))

    def _read(self, timeout: float = 2.0) -> bytes | None:
        if select.select([self.fd], [], [], timeout)[0]:
            return os.read(self.fd, 64)
        return None

    def _drain(self):
        while select.select([self.fd], [], [], 0)[0]:
            os.read(self.fd, 64)

    def cmd(self, cmd: int, p: int = 0, p2: int | None = None) -> bytes:
        frame = bytes([0xC0, 0x40, p >> 8, p & 0xFF, cmd, 0, 8, 0])
        self._drain()
        self._write(frame + (struct.pack(">H", p2) if p2 is not None else b""))
        if (r := self._read()) is None:
            raise TimeoutError(f"no response to command 0x{cmd:02x}")
        return r

    def status(self) -> dict:
        r = self.cmd(CMD_STATUS)
        if errs := [msg for (i, bit), msg in ERRORS.items() if r[i] & bit]:
            raise RuntimeError(", ".join(errs))
        return {"buf_full": bool(r[1] & 1), "busy": bool(r[2] & 4), "printing": bool(r[3] & 0x40)}

    def label_mm(self) -> tuple[int, int]:
        """(width, length) of the loaded label in mm, (0, 0) if unknown."""
        r = self.cmd(CMD_MATERIAL)
        return (r[19], r[20]) if len(r) > 20 else (0, 0)

    def _wait(self, what: str, cond, timeout: float, interval: float = 0.1):
        end = time.monotonic() + timeout
        while not cond(self.status()):
            if time.monotonic() > end:
                raise TimeoutError(f"timed out waiting for {what}")
            time.sleep(interval)

    def print_image(self, img: Image.Image, density: int = 4):
        blocks, speed = pack_blocks(print_buffers(to_head(img), density))
        self.cmd(CMD_CHECK)
        self._wait("idle", lambda s: not s["busy"] and not s["printing"], 6)
        self.cmd(CMD_START, 1)
        try:
            self._wait("print start", lambda s: s["printing"], 6)
            for blk in blocks:
                self._wait("buffer space", lambda s: not s["buf_full"], 15, 0.02)
                self.cmd(CMD_BULK, len(blk))
                self._drain()
                for i in range(0, len(blk), 64):
                    self._write(blk[i:i + 64])
                self._read()
                time.sleep(0.15)
                self.cmd(CMD_BUF_FULL, len(blk), speed)
                time.sleep(0.1)
            self._wait("print end", lambda s: not s["printing"] and not s["busy"], 30)
        except BaseException:
            self.cmd(CMD_STOP)
            raise


def find_hidraw() -> str:
    for uevent in glob.glob("/sys/class/hidraw/*/device/uevent"):
        if HID_ID in open(uevent).read():
            return "/dev/" + uevent.split("/")[4]
    raise FileNotFoundError("Supvan printer (1820:207f) not found")


def render_text(text: str, w: int, h: int, margin: int = 8) -> Image.Image:
    """Largest font size whose text block fits inside the label, centered."""
    img = Image.new("1", (w, h), 1)
    draw = ImageDraw.Draw(img)

    def font(size):
        try:
            return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
        except OSError:
            return ImageFont.load_default(size)

    lo, hi = 6, h
    while lo < hi:
        mid = (lo + hi + 1) // 2
        x0, y0, x1, y1 = draw.multiline_textbbox((0, 0), text, font=font(mid), align="center")
        lo, hi = (mid, hi) if x1 - x0 <= w - 2 * margin and y1 - y0 <= h - 2 * margin else (lo, mid - 1)
    f = font(lo)
    x0, y0, x1, y1 = draw.multiline_textbbox((0, 0), text, font=f, align="center")
    draw.multiline_text(((w - x1 - x0) / 2, (h - y1 - y0) / 2), text, font=f, fill=0, align="center")
    return img


def fit_image(path: str, w: int, h: int) -> Image.Image:
    src = Image.open(path).convert("L")
    src.thumbnail((w, h))
    img = Image.new("L", (w, h), 255)
    img.paste(src, ((w - src.width) // 2, (h - src.height) // 2))
    return img.convert("1")  # Floyd-Steinberg dither


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("text", nargs="*", help="text to print (stdin if empty); '\\n' breaks lines")
    ap.add_argument("-i", "--image", help="image file to print instead of text")
    ap.add_argument("-s", "--size", help="label WxH in mm (default: read from printer)")
    ap.add_argument("-d", "--density", type=int, default=4, help="burn density 0-15 (4)")
    ap.add_argument("-r", "--rotate", action="store_true", help="run content along the feed")
    ap.add_argument("-p", "--preview", help="save the rendered label as PNG instead of printing")
    ap.add_argument("--status", action="store_true", help="show printer and label status")
    a = ap.parse_args()

    if a.status:
        p = Printer()
        print(p.status(), "label %dx%d mm" % p.label_mm())
        return
    w_mm, h_mm = map(int, a.size.lower().split("x")) if a.size else Printer().label_mm()
    w_mm, h_mm = min(w_mm or 40, HEAD_DOTS // DOTS_PER_MM), h_mm or 30
    w, h = w_mm * DOTS_PER_MM, h_mm * DOTS_PER_MM
    if a.rotate:
        w, h = h, w
    if a.image:
        img = fit_image(a.image, w, h)
    else:
        text = " ".join(a.text) if a.text else sys.stdin.read().rstrip("\n")
        img = render_text(text.replace("\\n", "\n"), w, h)
    if a.rotate:
        img = img.rotate(90, expand=True)
    if a.preview:
        img.save(a.preview)
    else:
        Printer().print_image(img, a.density)


if __name__ == "__main__":
    main()
