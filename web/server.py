#!/usr/bin/env -S uv run -q --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow", "segno"]
# [tool.uv]
# python-preference = "only-system"  # uv-managed Pythons lack Bluetooth sockets
# ///
"""Web label designer: serves the editor and prints its rendered labels through tprint."""
import argparse
import base64
import gzip
import io
import json
import os
import sys
import threading
import unicodedata
from collections.abc import Callable
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import segno
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tprint  # noqa: E402

STATIC = Path(__file__).with_name("static")
MAX_BODY = 32 << 20
# Unprintable or invisible on their own, and algorithmically named ranges with no useful words.
SKIP_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp", "Mn", "Me", "Mc"}
SKIP_PREFIXES = ("CJK UNIFIED", "CJK COMPATIBILITY IDEOGRAPH", "HANGUL SYLLABLE", "TANGUT",
                 "KHITAN", "NUSHU")


def unicode_index() -> list[list]:
    """[[codepoint, name], ...] for every named, visible character worth searching."""
    out = []
    for cp in range(0x110000):
        ch = chr(cp)
        if unicodedata.category(ch) in SKIP_CATEGORIES:
            continue
        if (name := unicodedata.name(ch, "")) and not name.startswith(SKIP_PREFIXES):
            out.append([cp, name])
    return out


def qr_matrix(data: str, error: str = "m") -> list[str]:
    """QR modules as rows of '0'/'1' (1 = dark), no quiet zone."""
    qr = segno.make_qr(data, error=error)  # never Micro QR: phones mostly cannot scan it
    return ["".join("1" if v else "0" for v in row) for row in qr.matrix]


def decode_label(png_data_url: str) -> Image.Image:
    """1-bit label image from a PNG data URL; any non-white pixel burns."""
    _, _, b64 = png_data_url.partition(",")
    img = Image.open(io.BytesIO(base64.b64decode(b64, validate=True)))
    img.load()
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    img = img.convert("L").point(lambda v: 255 if v >= 128 else 0)
    if img.width > tprint.HEAD_DOTS or not img.width or not img.height:
        raise ValueError(f"label {img.width}x{img.height} dots does not fit the "
                         f"{tprint.HEAD_DOTS}-dot head")
    return img.convert("1", dither=Image.Dither.NONE)


class Job:
    """One print run at a time: labels printed in order, each `copies` times."""

    def __init__(self, connect):
        self.connect = connect
        self.lock = threading.Lock()
        self.state = {"running": False, "done": 0, "total": 0, "error": None}
        self.cancel = threading.Event()

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    def start(self, labels: list[tuple[Image.Image, int]], density: int) -> bool:
        with self.lock:
            if self.state["running"]:
                return False
            total = sum(n for _, n in labels)
            self.state = {"running": True, "done": 0, "total": total, "error": None}
        self.cancel.clear()
        threading.Thread(target=self._run, args=(labels, density), daemon=True).start()
        return True

    def _run(self, labels, density):
        error, printer = None, None
        try:
            printer = self.connect()
            for img, copies in labels:
                for _ in range(copies):
                    if self.cancel.is_set():
                        raise InterruptedError("cancelled")
                    printer.print_image(img, density)
                    with self.lock:
                        self.state["done"] += 1
        except Exception as e:
            error = str(e) or type(e).__name__
        finally:
            if printer:
                printer.close()
            with self.lock:
                self.state.update(running=False, error=error)


class Handler(SimpleHTTPRequestHandler):
    job: Job
    connect: Callable[[], tprint.Printer]
    unicode_gz: bytes | None = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, format, *args):  # noqa: A002 (base class name)
        if "/api/job" not in self.path:  # polled while printing
            super().log_message(format, *args)

    def send_json(self, obj, status=HTTPStatus.OK):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        match url.path:
            case "/api/printer":
                self.get_printer()
            case "/api/job":
                self.send_json(self.job.snapshot())
            case "/api/qr":
                try:
                    self.send_json({"rows": qr_matrix(q.get("data", ""), q.get("error", "m"))})
                except (ValueError, segno.DataOverflowError) as e:
                    self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            case "/api/unicode":
                self.get_unicode()
            case _:
                super().do_GET()

    def get_printer(self):
        if self.job.snapshot()["running"]:
            self.send_json({"error": "printing"}, HTTPStatus.CONFLICT)
            return
        printer = None
        try:
            printer = type(self).connect()
            w, h = printer.label_mm()
            self.send_json({"label_mm": [w, h] if w and h else None, "status": printer.status()})
        except Exception as e:
            self.send_json({"error": str(e) or type(e).__name__}, HTTPStatus.SERVICE_UNAVAILABLE)
        finally:
            if printer:
                printer.close()

    def get_unicode(self):
        cls = type(self)
        if cls.unicode_gz is None:
            cls.unicode_gz = gzip.compress(json.dumps(
                {"version": unicodedata.unidata_version, "chars": unicode_index()},
                separators=(",", ":")).encode())
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Cache-Control", "max-age=86400")
        self.send_header("Content-Length", str(len(cls.unicode_gz)))
        self.end_headers()
        self.wfile.write(cls.unicode_gz)

    def do_POST(self):
        # Another web page in the same browser can post here too; a JSON content type forces
        # a CORS preflight it cannot pass, and a foreign Origin is refused outright.
        origin = self.headers.get("Origin")
        if (self.headers.get("Content-Type", "").split(";")[0] != "application/json"
                or origin and urlparse(origin).netloc != self.headers.get("Host")):
            self.send_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return
        match urlparse(self.path).path:
            case "/api/print":
                self.post_print()
            case "/api/job/cancel":
                self.job.cancel.set()
                self.send_json(self.job.snapshot())
            case _:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def post_print(self):
        """Body: {"density": 0-15, "labels": [{"png": data URL, "copies": n}, ...]}."""
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            self.send_json({"error": "request too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        try:
            req = json.loads(self.rfile.read(length))
            density = int(req.get("density", 4))
            if not 0 <= density <= 15:
                raise ValueError("density must be 0-15")
            labels = [(decode_label(lb["png"]), int(lb.get("copies", 1))) for lb in req["labels"]]
            labels = [(img, n) for img, n in labels if n > 0]
            if not labels:
                raise ValueError("nothing to print")
        except Exception as e:
            self.send_json({"error": f"bad request: {e}"}, HTTPStatus.BAD_REQUEST)
            return
        if not self.job.start(labels, density):
            self.send_json({"error": "already printing"}, HTTPStatus.CONFLICT)
            return
        self.send_json(self.job.snapshot(), HTTPStatus.ACCEPTED)


def make_server(host: str, port: int, connect) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"connect": staticmethod(connect),
                                                "job": Job(connect)})
    return ThreadingHTTPServer((host, port), handler)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8350)
    ap.add_argument("-b", "--bt", metavar="ADDR", default=os.environ.get("TPRINT_BT"),
                    help="printer Bluetooth address (env TPRINT_BT, else bt_address in "
                         "tprint.toml if reachable, else USB)")
    a = ap.parse_args()
    srv = make_server(a.host, a.port, lambda: tprint.connect(a.bt))
    print(f"label designer on http://{a.host}:{srv.server_address[1]}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
