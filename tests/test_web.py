import base64
import gzip
import io
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
import segno
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
import server  # noqa: E402


class FakePrinter:
    def __init__(self, log, fail_after=None):
        self.log, self.fail_after = log, fail_after
        self.closed = False

    def label_mm(self):
        return (40, 30)

    def status(self):
        return {"buf_full": False, "busy": False, "printing": False}

    def print_image(self, img, density):
        if self.fail_after is not None and len(self.log) >= self.fail_after:
            raise RuntimeError("label roll end")
        self.log.append((img.size, img.mode, density, img.getpixel((0, 0))))

    def close(self):
        self.closed = True


@pytest.fixture
def srv():
    s = SimpleNamespace(printed=[], printers=[], fail_after=None)

    def connect():
        s.printers.append(FakePrinter(s.printed, s.fail_after))
        return s.printers[-1]

    httpd = server.make_server("127.0.0.1", 0, connect)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    s.base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield s
    httpd.shutdown()
    httpd.server_close()


def get(base, path):
    with urllib.request.urlopen(base + path) as r:
        return r.status, r.headers, r.read()


def post(base, path, obj, headers=None):
    req = urllib.request.Request(base + path, json.dumps(obj).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def png_url(w, h, black_corner=True):
    img = Image.new("L", (w, h), 255)
    if black_corner:
        img.putpixel((0, 0), 0)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def wait_job(base):
    for _ in range(100):
        job = json.loads(get(base, "/api/job")[2])
        if not job["running"]:
            return job
        time.sleep(0.02)
    raise TimeoutError


def test_serves_editor(srv):
    status, _, body = get(srv.base, "/")
    assert status == 200 and b"Label Designer" in body
    assert get(srv.base, "/app.js")[1]["Content-Type"] in ("text/javascript", "application/javascript")


def test_printer_reports_label_and_closes(srv):
    status, _, body = get(srv.base, "/api/printer")
    assert status == 200 and json.loads(body)["label_mm"] == [40, 30]
    assert srv.printers[-1].closed


def test_qr_matches_segno(srv):
    rows = json.loads(get(srv.base, "/api/qr?data=hello&error=q")[2])["rows"]
    want = segno.make_qr("hello", error="q").matrix
    assert rows == ["".join("1" if v else "0" for v in r) for r in want]


def test_short_qr_data_is_not_micro_qr(srv):
    # segno.make would pick a 15-module Micro QR here, which phone cameras do not read.
    rows = json.loads(get(srv.base, "/api/qr?data=Box%2012&error=m")[2])["rows"]
    assert len(rows) == 21


def test_unicode_index_is_gzipped_and_filtered(srv):
    _, headers, body = get(srv.base, "/api/unicode")
    assert headers["Content-Encoding"] == "gzip"
    data = json.loads(gzip.decompress(body))
    names = dict(map(tuple, data["chars"]))
    assert names[0x2713] == "CHECK MARK" and names[0x41] == "LATIN CAPITAL LETTER A"
    assert 0x0A not in names and 0x0301 not in names and 0x4E00 not in names  # Cc, Mn, CJK
    assert 20000 < len(names) < 60000


def test_print_batch_with_copies(srv):
    status, job = post(srv.base, "/api/print", {"density": 6, "labels": [
        {"png": png_url(320, 240), "copies": 2}, {"png": png_url(160, 80, False), "copies": 1},
        {"png": png_url(8, 8), "copies": 0}]})
    assert status == 202 and job["total"] == 3
    job = wait_job(srv.base)
    assert job == {"running": False, "done": 3, "total": 3, "error": None}
    assert srv.printed == [((320, 240), "1", 6, 0)] * 2 + [((160, 80), "1", 6, 255)]
    assert srv.printers[-1].closed


def test_print_error_is_reported(srv):
    srv.fail_after = 1
    post(srv.base, "/api/print", {"labels": [{"png": png_url(16, 16), "copies": 3}]})
    job = wait_job(srv.base)
    assert job["done"] == 1 and job["error"] == "label roll end"


@pytest.mark.parametrize("body", [
    {"labels": []},
    {"labels": [{"png": png_url(400, 10), "copies": 1}]},  # wider than the head
    {"labels": [{"png": "data:image/png;base64,!!", "copies": 1}]},
    {"density": 16, "labels": [{"png": png_url(8, 8), "copies": 1}]},
])
def test_print_rejects_bad_requests(srv, body):
    assert post(srv.base, "/api/print", body)[0] == 400


def test_post_refuses_foreign_origin_and_non_json(srv):
    body = {"labels": [{"png": png_url(8, 8), "copies": 1}]}
    assert post(srv.base, "/api/print", body, {"Origin": "http://evil.example"})[0] == 403
    assert post(srv.base, "/api/print", body, {"Content-Type": "text/plain"})[0] == 403
    assert srv.printed == []


def test_decode_label_flattens_alpha_to_white():
    img = Image.new("RGBA", (4, 1), (0, 0, 0, 0))
    img.putpixel((1, 0), (0, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    out = server.decode_label("data:image/png;base64," + base64.b64encode(buf.getvalue()).decode())
    assert out.mode == "1" and [out.getpixel((x, 0)) for x in range(4)] == [255, 0, 255, 255]
