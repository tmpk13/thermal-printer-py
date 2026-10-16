#!/usr/bin/env -S uv run -q --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow", "segno"]
# [tool.uv]
# python-preference = "only-system"  # uv-managed Pythons lack Bluetooth sockets
# ///
"""Print text, QR codes or images to a Supvan T50 Pro label printer over USB or Bluetooth."""
import argparse
import contextlib
import ctypes as C
import glob
import os
import select
import socket
import struct
import sys
import textwrap
import time

import segno
from PIL import Image, ImageDraw, ImageFont

HID_ID = "HID_ID=0003:00001820:0000207F"
HEAD_DOTS, BPL, DOTS_PER_MM = 384, 48, 8  # 48 mm head at 203 dpi
BUF_SIZE, HDR = 4096, 14
MAX_COLS = 4074 // BPL  # image bytes per print buffer, in whole columns
CMD_BUF_FULL, CMD_STATUS, CMD_CHECK, CMD_START, CMD_STOP = 0x10, 0x11, 0x12, 0x13, 0x14
CMD_MATERIAL, CMD_BULK = 0x30, 0x5C
# Pillow "1" rows are MSB-first with 1 = white; the head wants LSB-first with 1 = burn.
PACK = bytes(int(f"{i ^ 0xFF:08b}"[::-1], 2) for i in range(256))
# Status register bits: (byte within the MSTA/FSTA block, mask) -> error.
ERRORS = {(0, 0x02): "label read/write error", (0, 0x04): "label roll end",
          (0, 0x08): "label mode mismatch", (0, 0x10): "ribbon read/write error",
          (0, 0x20): "ribbon end", (1, 0x08): "printhead too hot",
          (2, 0x08): "cover open", (3, 0x01): "no label installed"}
# BUF_FULL speed (roughly mm/s) by compressed bytes per buffer, densest first.
SPEED_STEPS = [(3000, 10), (2800, 15), (2500, 20), (2000, 25), (1500, 40), (1000, 45), (500, 55)]


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
    """Rows become feed columns: center the image on the head, pack 48 bytes per row.

    Head dot 0 prints on the right of the label as it leaves the printer, so rows are
    mirrored to make image x run left to right on paper.
    """
    img = img.convert("1")
    canvas = Image.new("1", (HEAD_DOTS, img.height), 1)
    canvas.paste(img, ((HEAD_DOTS - img.width) // 2, 0))
    return canvas.transpose(Image.Transpose.FLIP_LEFT_RIGHT).tobytes().translate(PACK)


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


def pack_per_buffer(buffers: list[bytes]) -> tuple[list[bytes], int]:
    """USB: whole page in one block when it compresses small, else one block per buffer."""
    singles = [lzma_block(b) for b in buffers]
    if sum(map(len, singles)) <= 4000:
        whole = lzma_block(b"".join(buffers))
        if len(whole) <= BUF_SIZE:
            return [whole], 60
    return singles, 60 if len(singles) == 1 else 20


def pack_grouped(buffers: list[bytes]) -> tuple[list[bytes], int]:
    """Bluetooth: up to 4 buffers per block, fewer until it compresses to one buffer."""
    blocks, speed, rest = [], 60, buffers
    while rest:
        take = min(4, len(rest))
        while len(z := lzma_block(b"".join(rest[:take]))) > BUF_SIZE:
            if take == 1:
                raise ValueError("print buffer does not compress into one block")
            take -= 1
        per_buf = len(z) // take
        speed = min(speed, next((s for n, s in SPEED_STEPS if per_buf > n), 60))
        blocks.append(z)
        rest = rest[take:]
    return blocks, speed


def bt_frame(cmd: int, p: int = 0, p2: int = 0) -> bytes:
    """16-byte RFCOMM command: magic, length 12, proto, marker, cmd, checksum, LE params."""
    f = bytearray(struct.pack("<2sHBBBBHBBHH", b"\x7e\x5a", 12, 0x10, 1, 0xAA, cmd, 0, 0, 1, p, p2))
    struct.pack_into("<H", f, 8, sum(f[10:]) & 0xFFFF)
    return bytes(f)


def bt_data_frames(blk: bytes) -> list[bytes]:
    """Split a compressed block into 512-byte frames, each one AA BB packet of 500 bytes."""
    chunks = [blk[i:i + 500] for i in range(0, len(blk), 500)]
    out = []
    for i, chunk in enumerate(chunks):
        body = bytes([i, len(chunks)]) + chunk.ljust(500, b"\0")
        pkt = b"\xaa\xbb" + struct.pack("<H", sum(body) & 0xFFFF) + body
        out.append(b"\x7e\x5a" + struct.pack("<H", 508) + b"\x10\x02" + pkt)
    return out


class Printer:
    """Print flow shared by both links; subclasses supply framing and timing."""
    ST = MAT = START = 0  # status offset, material width offset, START_PRINT param
    PRE_COMMIT, POST_COMMIT = 0.0, 0.0
    pack = staticmethod(pack_per_buffer)

    def cmd(self, cmd: int, p: int = 0, p2: int | None = None) -> bytes:
        raise NotImplementedError

    def send_block(self, blk: bytes):
        raise NotImplementedError

    def status(self) -> dict:
        r = self.cmd(CMD_STATUS)[self.ST:self.ST + 4]
        if len(r) < 4:
            raise IOError("short status reply")
        if errs := [msg for (i, bit), msg in ERRORS.items() if r[i] & bit]:
            raise RuntimeError(", ".join(errs))
        return {"buf_full": bool(r[0] & 1), "busy": bool(r[1] & 4), "printing": bool(r[2] & 0x40)}

    def label_mm(self) -> tuple[int, int]:
        """(width, length) of the loaded label in mm, (0, 0) if unknown."""
        r = self.cmd(CMD_MATERIAL)
        return (r[self.MAT], r[self.MAT + 1]) if len(r) > self.MAT + 1 else (0, 0)

    def _wait(self, what: str, cond, timeout: float, interval: float = 0.1):
        end = time.monotonic() + timeout
        while not cond(self.status()):
            if time.monotonic() > end:
                raise TimeoutError(f"timed out waiting for {what}")
            time.sleep(interval)

    def print_image(self, img: Image.Image, density: int = 4):
        blocks, speed = self.pack(print_buffers(to_head(img), density))
        self.cmd(CMD_CHECK)
        self._wait("idle", lambda s: not s["busy"] and not s["printing"], 6)
        self.cmd(CMD_START, self.START)
        try:
            self._wait("print start", lambda s: s["printing"], 6)
            for blk in blocks:
                self._wait("buffer space", lambda s: not s["buf_full"], 15, 0.02)
                self.send_block(blk)
                time.sleep(self.PRE_COMMIT)
                self.cmd(CMD_BUF_FULL, len(blk), speed)
                time.sleep(self.POST_COMMIT)
            self._wait("print end", lambda s: not s["printing"] and not s["busy"], 30)
        except BaseException:
            with contextlib.suppress(Exception):  # keep the original error
                self.cmd(CMD_STOP)
            raise


class UsbPrinter(Printer):
    """USB HID: 8-byte big-endian commands in 64-byte reports, 64-byte replies."""
    ST, MAT, START = 1, 19, 1
    PRE_COMMIT, POST_COMMIT = 0.15, 0.1

    def __init__(self, path: str | None = None):
        self.fd = os.open(path or find_hidraw(), os.O_RDWR | os.O_NONBLOCK)

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
        debug("tx", frame)
        self._drain()
        self._write(frame + (struct.pack(">H", p2) if p2 is not None else b""))
        if (r := self._read()) is None:
            raise TimeoutError(f"no response to command 0x{cmd:02x}")
        debug("rx", r)
        return r

    def send_block(self, blk: bytes):
        self.cmd(CMD_BULK, len(blk))
        self._drain()
        for i in range(0, len(blk), 64):
            self._write(blk[i:i + 64])
        self._read()


class BtPrinter(Printer):
    """Bluetooth Classic RFCOMM: 16-byte little-endian 7E 5A frames, variable replies."""
    ST, MAT, START = 14, 40, 0
    PRE_COMMIT = 0.02
    pack = staticmethod(pack_grouped)

    def __init__(self, addr: str, channel: int = 1):
        self.sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,  # pyright: ignore
                                  socket.BTPROTO_RFCOMM)  # pyright: ignore
        self.sock.settimeout(10)
        self.sock.connect((addr, channel))
        self.buf = b""

    def _drain(self):
        self.sock.setblocking(False)
        try:
            while self.sock.recv(1024):
                pass
        except BlockingIOError:
            pass
        finally:
            self.sock.settimeout(2)
            self.buf = b""

    def _read(self) -> bytes:
        """Next reply frame, resyncing on the 7E 5A magic; empty on timeout."""
        try:
            while True:
                i = self.buf.find(b"\x7e\x5a")
                if i >= 0 and len(self.buf) >= i + 4:
                    end = i + 4 + struct.unpack_from("<H", self.buf, i + 2)[0]
                    if len(self.buf) >= end:
                        frame, self.buf = self.buf[i:end], self.buf[end:]
                        debug("rx", frame)
                        return frame
                if not (chunk := self.sock.recv(512)):
                    return b""
                self.buf += chunk
        except TimeoutError:
            return b""

    def cmd(self, cmd: int, p: int = 0, p2: int | None = None) -> bytes:
        self._drain()
        frame = bt_frame(cmd, p, p2 or 0)
        debug("tx", frame)
        self.sock.sendall(frame)
        while r := self._read():  # skip stray frames that echo another command
            if len(r) >= 8 and r[7] == cmd:
                return r
        raise TimeoutError(f"no response to command 0x{cmd:02x}")

    def send_block(self, blk: bytes):
        frames = bt_data_frames(blk)
        self.cmd(CMD_BULK, 512, len(frames))
        for i, frame in enumerate(frames):
            for j in range(0, len(frame), 128):  # the firmware wants 128-byte writes, 10 ms apart
                time.sleep(0.01)
                self._drain()
                self.sock.sendall(frame[j:j + 128])
            if i < len(frames) - 1:  # the last ack only comes once the block is digested
                self._read()


def debug(tag: str, data: bytes):
    if os.environ.get("TPRINT_DEBUG"):
        print(f"{time.monotonic():.3f} {tag} {data[:48].hex(' ')}", file=sys.stderr)


def find_hidraw() -> str:
    for uevent in glob.glob("/sys/class/hidraw/*/device/uevent"):
        if HID_ID in open(uevent).read():
            return "/dev/" + uevent.split("/")[4]
    raise FileNotFoundError("Supvan printer (1820:207f) not found on USB")


def render_text(text: str, w: int, h: int, margin: int = 8) -> Image.Image:
    """Largest font whose text block fits inside w x h, centered.

    Text without explicit line breaks is also tried wrapped at every width, so long
    text in a narrow area wraps instead of shrinking.
    """
    img = Image.new("1", (w, h), 1)
    draw = ImageDraw.Draw(img)

    def font(size):
        try:
            return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
        except OSError:
            return ImageFont.load_default(size)

    def bbox(t, size):
        return draw.multiline_textbbox((0, 0), t, font=font(size), align="center")

    def best_size(t):
        lo, hi = 6, h
        while lo < hi:
            mid = (lo + hi + 1) // 2
            x0, y0, x1, y1 = bbox(t, mid)
            fits = x1 - x0 <= w - 2 * margin and y1 - y0 <= h - 2 * margin
            lo, hi = (mid, hi) if fits else (lo, mid - 1)
        return lo

    layouts = {text} if "\n" in text else {textwrap.fill(text, c, break_long_words=False)
                                                       for c in range(1, len(text) + 1)}
    size, text = max((best_size(t), t) for t in layouts)
    x0, y0, x1, y1 = bbox(text, size)
    draw.multiline_text(((w - x1 - x0) / 2, (h - y1 - y0) / 2), text, font=font(size), fill=0,
                        align="center")
    return img


def render_qr(data: str, side: int) -> Image.Image:
    """QR code (2-module quiet zone) at a whole number of dots per module, at most side wide."""
    qr = segno.make(data, error="m")
    m = qr.symbol_size(border=0)[0]
    n = m + 4
    scale = side // n
    if scale < 2:
        raise ValueError(f"QR data too long for the label ({n} modules in {side} dots)")
    modules = Image.frombytes("L", (m, m), bytes(0 if v else 255 for row in qr.matrix for v in row))
    img = Image.new("L", (n, n), 255)
    img.paste(modules, (2, 2))
    return img.resize((n * scale, n * scale), Image.Resampling.NEAREST).convert("1")


def render_qr_label(data: str, text: str, w: int, h: int, margin: int = 8) -> Image.Image:
    """QR code alone, or beside the text (below it on labels taller than wide)."""
    img = Image.new("1", (w, h), 1)
    qr = render_qr(data, min(w, h) - 2 * margin)
    if not text:
        img.paste(qr, ((w - qr.width) // 2, (h - qr.height) // 2))
    elif w >= h:
        img.paste(qr, (margin, (h - qr.height) // 2))
        x = margin + qr.width
        img.paste(render_text(text, w - x, h, margin), (x, 0))
    else:
        img.paste(qr, ((w - qr.width) // 2, margin))
        y = margin + qr.height
        img.paste(render_text(text, w, h - y, margin), (0, y))
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
    src = ap.add_mutually_exclusive_group()
    src.add_argument("-i", "--image", help="image file to print instead of text")
    src.add_argument("-q", "--qr", metavar="DATA", help="QR code, beside the text if any is given")
    ap.add_argument("-s", "--size", help="label WxH in mm (default: read from printer)")
    ap.add_argument("-d", "--density", type=int, default=4, help="burn density 0-15 (4)")
    ap.add_argument("-r", "--rotate", action="store_true", help="run content along the feed")
    ap.add_argument("-p", "--preview", help="save the rendered label as PNG instead of printing")
    ap.add_argument("-b", "--bt", metavar="ADDR", default=os.environ.get("TPRINT_BT"),
                    help="print over Bluetooth to this address (env TPRINT_BT)")
    ap.add_argument("--status", action="store_true", help="show printer and label status")
    a = ap.parse_args()

    printer = None

    def connect() -> Printer:
        nonlocal printer
        printer = printer or (BtPrinter(a.bt) if a.bt else UsbPrinter())
        return printer

    if a.status:
        p = connect()
        print(p.status(), "label %dx%d mm" % p.label_mm())
        return
    w_mm, h_mm = map(int, a.size.lower().split("x")) if a.size else connect().label_mm()
    w_mm, h_mm = min(w_mm or 40, HEAD_DOTS // DOTS_PER_MM), h_mm or 30
    w, h = w_mm * DOTS_PER_MM, h_mm * DOTS_PER_MM
    if a.rotate:
        w, h = h, w
    if a.image:
        img = fit_image(a.image, w, h)
    elif a.qr:
        img = render_qr_label(a.qr, " ".join(a.text).replace("\\n", "\n"), w, h)
    else:
        text = " ".join(a.text) if a.text else sys.stdin.read().rstrip("\n")
        img = render_text(text.replace("\\n", "\n"), w, h)
    if a.rotate:
        img = img.rotate(90, expand=True)
    if a.preview:
        img.save(a.preview)
    else:
        connect().print_image(img, a.density)


if __name__ == "__main__":
    main()
