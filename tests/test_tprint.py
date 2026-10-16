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


def test_to_head_centers_and_packs_lsb_first():
    img = Image.new("1", (8, 2), 1)
    img.putpixel((0, 0), 0)
    data = tp.to_head(img)
    assert len(data) == 2 * tp.BPL
    off = (tp.HEAD_DOTS - 8) // 2  # 188 -> byte 23, bit 4
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


def test_pack_blocks_small_page_is_one_block():
    bufs = tp.print_buffers(bytes(tp.BPL * 240))
    blocks, speed = tp.pack_blocks(bufs)
    assert len(blocks) == 1 and speed == 60
    assert lzma.decompress(blocks[0], format=lzma.FORMAT_ALONE) == b"".join(bufs)


def test_render_text_fits_label():
    img = tp.render_text("Hello\nWorld", 320, 240)
    assert img.size == (320, 240)
    assert img.convert("L").getextrema() == (0, 255)  # has ink and margin
