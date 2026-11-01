"""The phone app's Java protocol code, run on the desktop JVM and held to tprint.py."""
import ctypes as C
import lzma
import shutil
import random
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import segno
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tprint as tp  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "android"
JARS = [APP / "build/deps/xz.jar", APP / "build/deps/qrcodegen.jar"]
SOURCES = [APP / "src/tprint/app" / f for f in ("Raster.java", "BtPrinter.java", "Qr.java")]

pytestmark = pytest.mark.skipif(
    not (shutil.which("javac") and all(j.exists() for j in JARS)),
    reason="needs javac and the jars from android/build.sh deps")


@pytest.fixture(scope="module")
def java(tmp_path_factory):
    out = tmp_path_factory.mktemp("classes")
    cp = ":".join(map(str, JARS))
    subprocess.run(["javac", "--release", "8", "-Xlint:-options", "-d", str(out), "-cp", cp,
                    *map(str, SOURCES), str(APP / "test/tprint/app/ProtocolCheck.java")],
                   check=True)

    def run(*args):
        r = subprocess.run(["java", "-cp", f"{out}:{cp}", "tprint.app.ProtocolCheck",
                            *map(str, args)], capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        return r.stdout.splitlines()
    return run


def label(w, h, seed=0):
    """Text-like test label: frame, slanted bars and a caption, so every buffer differs."""
    img = Image.new("1", (w, h), 1)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w - 1, h - 1], outline=0)
    for y in range(3, h, 9):
        d.line([(w // 3, y), (w - 5, y + 4 + seed)], fill=0, width=2)
    d.text((4, 4), f"Box {seed} label", fill=0)
    return img


def noisy(w, h, seed=0):
    """Label with a random-dot strip (a dithered photo, say): blocks then need several
    512-byte frames, and four buffers no longer fit one block."""
    img = label(w, h, seed)
    strip = Image.frombytes("1", (w // 2, h), random.Random(seed).randbytes(w // 2 // 8 * h))
    img.paste(strip, (w // 4, 0))
    return img


def exact_decode(blk: bytes) -> bytes:
    """Decode an alone block as the firmware must: size from the header, no end marker
    allowed, every byte consumed. liblzma's LZMA1EXT decoder is told just that."""
    assert blk[0] == 0x5D and struct.unpack_from("<I", blk, 1)[0] == 8192
    size = struct.unpack_from("<Q", blk, 5)[0]
    lib = C.CDLL("liblzma.so.5")
    opts = tp.LzmaOptions()
    assert not lib.lzma_lzma_preset(C.byref(opts), 6)
    opts.dict_size, opts.lc, opts.lp, opts.pb, opts.ext_flags = 8192, 3, 0, 2, 0
    opts.ext_size_low, opts.ext_size_high = size & 0xFFFFFFFF, size >> 32
    filters = (tp.LzmaFilter * 2)((0x4000000000000002, C.addressof(opts)), (2**64 - 1, None))
    src, out = blk[13:], C.create_string_buffer(size)
    in_pos, out_pos = C.c_size_t(0), C.c_size_t(0)
    lib.lzma_raw_buffer_decode.argtypes = [
        C.c_void_p, C.c_void_p, C.c_char_p, C.POINTER(C.c_size_t), C.c_size_t,
        C.c_char_p, C.POINTER(C.c_size_t), C.c_size_t]
    rc = lib.lzma_raw_buffer_decode(filters, None, src, C.byref(in_pos), len(src),
                                    out, C.byref(out_pos), size)
    assert rc == 0 and in_pos.value == len(src) and out_pos.value == size, (rc, in_pos.value)
    return out.raw


def test_exact_decode_rejects_an_end_marker():
    data = bytes(range(256)) * 16
    assert exact_decode(tp.lzma_block(data)) == data
    raw = lzma.compress(data, format=lzma.FORMAT_RAW, filters=[
        {"id": lzma.FILTER_LZMA1, "dict_size": 8192, "lc": 3, "lp": 0, "pb": 2}])
    with pytest.raises(AssertionError):
        exact_decode(struct.pack("<BIQ", 0x5D, 8192, len(data)) + raw)


def speed_for(blocks):
    """tprint.pack_grouped's speed rule applied to a given set of blocks."""
    speed = 60
    for blk in blocks:
        per_buf = len(blk) // (struct.unpack_from("<Q", blk, 5)[0] // tp.BUF_SIZE)
        speed = min(speed, next((s for n, s in tp.SPEED_STEPS if per_buf > n), 60))
    return speed


@pytest.mark.parametrize("make,w,h,density", [(label, 320, 240, 4), (label, 301, 83, 0),
                                              (label, 384, 85, 15), (label, 200, 1000, 6),
                                              (noisy, 384, 600, 4)])
def test_pack_matches_tprint(java, tmp_path, make, w, h, density):
    img = make(w, h)
    img.save(tmp_path / "l.pbm")
    got = {"head": [], "buffer": [], "block": [], "speed": []}
    for line in java("pack", tmp_path / "l.pbm", density):
        key, value = line.split(" ", 1)
        got[key].append(value)
    head = tp.to_head(img)
    assert bytes.fromhex(got["head"][0]) == head
    buffers = tp.print_buffers(head, density)
    assert [bytes.fromhex(b) for b in got["buffer"]] == buffers
    # Compressed bytes differ between encoders; what they decode to, and the grouping, may not.
    blocks = [bytes.fromhex(b) for b in got["block"]]
    assert all(len(b) <= tp.BUF_SIZE for b in blocks)
    assert b"".join(map(exact_decode, blocks)) == b"".join(buffers)
    assert len(blocks) == len(tp.pack_grouped(buffers)[0])
    assert int(got["speed"][0]) == speed_for(blocks)


class FakeDevice:
    """A T50 Pro on the far end of a stream socket: answers 7E 5A commands, collects blocks,
    starts printing on START and stops once the buffer flagged as job end is committed."""

    def __init__(self, sock, errors=(), silent=False):
        self.sock, self.errors, self.silent = sock, dict(errors), silent
        self.cmds, self.blocks, self.problems = [], [], []
        self.printing, self.done, self.frames, self.expect = False, False, [], 0
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        try:
            self.serve()
        except OSError:  # the test closed the socket
            pass

    def serve(self):
        buf = b""
        while chunk := self.sock.recv(4096):
            buf += chunk
            while len(buf) >= 4:
                if buf[:2] != b"\x7e\x5a":
                    self.problems.append(f"no magic: {buf[:8].hex()}")
                    return
                end = 4 + struct.unpack_from("<H", buf, 2)[0]
                if len(buf) < end:
                    break
                frame, buf = buf[:end], buf[end:]
                if len(frame) == 512:
                    self.data(frame)
                else:
                    self.command(frame)

    def reply(self, cmd):
        if self.silent:
            return
        r = bytearray(48)
        r[0:4] = b"\x7e\x5a" + struct.pack("<H", 44)
        r[4], r[7] = 0x10, cmd
        r[16] = 0x40 if self.printing else 0
        for i, bit in self.errors.items():
            r[14 + i] |= bit
        r[40:42] = bytes([40, 30])
        self.sock.sendall(bytes(r))

    def command(self, f):
        if len(f) != 16 or struct.unpack_from("<H", f, 8)[0] != sum(f[10:]) & 0xFFFF:
            self.problems.append(f"bad command {f.hex()}")
        cmd, (p, p2) = f[7], struct.unpack_from("<HH", f, 12)
        self.cmds.append((cmd, p, p2))
        if cmd == tp.CMD_START:
            self.printing = True
        elif cmd == tp.CMD_BULK:
            self.frames, self.expect = [], p2
        elif cmd == tp.CMD_BUF_FULL:
            if len(self.frames) != self.expect:
                self.problems.append(f"{len(self.frames)} frames, {self.expect} announced")
            blk = b"".join(self.frames)[:p]
            self.blocks.append(blk)
            if lzma.decompress(blk, format=lzma.FORMAT_ALONE)[-tp.BUF_SIZE + 2] & 0x0C == 0x0C:
                self.printing = False
        self.reply(cmd)

    def data(self, f):
        body = f[10:]
        if f[4:8] != b"\x10\x02\xaa\xbb" or struct.unpack_from("<H", f, 8)[0] != sum(body) & 0xFFFF:
            self.problems.append(f"bad data frame {f[:12].hex()}")
        if body[0] != len(self.frames) or body[1] != self.expect:
            self.problems.append(f"data frame {body[0]}/{body[1]}, expected {len(self.frames)}")
        self.frames.append(body[2:])
        if len(self.frames) < self.expect:
            self.reply(tp.CMD_BULK)

    def shape(self):
        """Commands with repeated status polls collapsed and encoder-dependent params dropped."""
        out = []
        for cmd, p, p2 in self.cmds:
            item = (cmd,) if cmd in (tp.CMD_BULK, tp.CMD_BUF_FULL) else (cmd, p, p2)
            if not (out and item == out[-1] == (tp.CMD_STATUS, 0, 0)):
                out.append(item)
        return out


def java_print(java, tmp_path, img, density, **fake):
    img.save(tmp_path / "l.pbm")
    with socket.create_server(("127.0.0.1", 0)) as srv:
        srv.settimeout(60)
        result = []
        t = threading.Thread(target=lambda: result.extend(
            java("print", tmp_path / "l.pbm", density, srv.getsockname()[1])))
        t.start()
        conn, _ = srv.accept()
        dev = FakeDevice(conn, **fake)
        t.join(timeout=120)
        conn.close()
    return dev, result


def test_print_flow_matches_tprint(java, tmp_path):
    img, density = noisy(320, 500, seed=2), 5
    a, b = socket.socketpair()
    py_dev = FakeDevice(b)
    py = tp.BtPrinter.__new__(tp.BtPrinter)
    py.sock, py.buf = a, b""
    assert py.label_mm() == (40, 30)
    py.print_image(img, density)
    a.close()

    dev, out = java_print(java, tmp_path, img, density)
    assert out == ["label 40 30", "done"]
    assert dev.problems == [] and py_dev.problems == []
    assert dev.shape() == py_dev.shape()
    want = b"".join(tp.print_buffers(tp.to_head(img), density))
    assert b"".join(map(exact_decode, dev.blocks)) == want
    for cmd, _, p2 in dev.cmds:
        if cmd == tp.CMD_BUF_FULL:
            assert p2 == speed_for(dev.blocks)


def test_status_error_stops_before_start(java, tmp_path):
    dev, out = java_print(java, tmp_path, label(100, 50), 4, errors={2: 0x08, 3: 0x01})
    assert out == ["label 40 30", "error cover open, no label installed"]
    assert tp.CMD_START not in [c for c, _, _ in dev.cmds]


def test_silent_printer_times_out(java, tmp_path):
    _, out = java_print(java, tmp_path, label(100, 50), 4, silent=True)
    assert out == ["error no response to command 0x30"]


@pytest.mark.parametrize("data", ["Box 12", "12345", "HELLO WORLD 123",
                                  "https://example.com/item/00042?x=1", "caf\u00e9 \u2713"])
@pytest.mark.parametrize("error", "mh")
def test_qr_scans_and_matches_segno_size(java, data, error):
    zxingcpp = pytest.importorskip("zxingcpp")
    rows = java("qr", error, data)
    # Same version as segno's regular QR; never Micro QR, which phone scanners skip.
    assert len(rows) == len(segno.make_qr(data, error=error).matrix) >= 21
    scale, quiet = 6, 4
    side = (len(rows) + 2 * quiet) * scale
    img = Image.new("L", (side, side), 255)
    for y, row in enumerate(rows):
        for x, v in enumerate(row):
            if v == "1":
                x0, y0 = (x + quiet) * scale, (y + quiet) * scale
                img.paste(0, (x0, y0, x0 + scale, y0 + scale))
    found = zxingcpp.read_barcodes(img)
    assert [(f.text, f.format.name) for f in found] == [(data, "QRCode")]


def test_qr_too_long_and_bad_level(java):
    assert java("qr", "h", "x" * 2000)[0].startswith("error ")
    assert java("qr", "z", "hi")[0] == "error unknown error level z"
