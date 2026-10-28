# Notes

- **Protocol**: proprietary, from the reverse engineering in heeen/supvan-cups. USB frames are
  `C0 40 <param BE> <cmd> 00 08 00 [param2 BE]`, sent as 64-byte HID reports; replies are 64-byte
  reports (status in bytes 1..4, label width/length mm at 19/20).
- **hidraw report ID**: every write must be prefixed with `0x00`, otherwise the kernel strips a
  leading zero data byte as the report ID and the compressed stream gets corrupted at random.
- **LZMA end marker**: the firmware drops data when the stream has an end-of-payload marker.
  Python's `lzma` always writes one, so `lzma_block` calls liblzma's raw `LZMA1EXT` encoder via
  ctypes (`ext_flags = 0` disables the marker) and writes the 13-byte "alone" header by hand.
- **Raster layout**: image rows are feed columns, image x is the printhead dot. 384 dots (48 B)
  per row, LSB-first, 1 = burn; the label is centered on the head. Rows are mirrored before
  packing: unmirrored text printed backwards (verified on a T50 Pro over USB and Bluetooth).
- **Blocks**: whole page in one LZMA block at speed 60 when the per-buffer compressed sizes sum
  to <= 4000 B, otherwise one block per 4096 B buffer at speed 20 (matches the vendor USB tool).
- **Bluetooth (RFCOMM channel 1)**: 16-byte `7E 5A` commands with little-endian params and a
  byte-sum checksum over bytes 10..15. Replies are length-prefixed frames; status is at byte 14,
  label width/length at 40/41. Compressed blocks go out as 512-byte frames (one `AA BB` packet,
  500 B payload) written 128 B at a time with 10 ms gaps; every packet is acked except the last,
  whose ack only arrives after the block is digested. Up to 4 buffers per block, speed by density.
- **Reply resync**: the reader frames on `7E 5A` + length and skips frames echoing another
  command; one early test print failed on a reply mismatch that did not recur.
- **No link-speed measurement**: the reference caps print speed by measured Bluetooth throughput
  so dense images do not stripe; this script uses the density rule only, fine for text.
- **Scan task**: `bluetoothctl scan on` prints only devices it has not seen before, so piping it
  to grep misses a cached printer; bluetoothctl 5.72 can also segfault on `--timeout` exit. The
  task scans silently, then greps `bluetoothctl devices`. Under dash the redirect on the command
  also hides the crash message (bash would still print it).
- **uv and system Python**: the script declares its deps inline (PEP 723) and pins
  `python-preference = "only-system"`; uv-managed Pythons are built without `AF_BLUETOOTH`.
- **QR codes**: segno module matrix with a 2-module quiet zone, scaled by a whole number of
  dots per module (nearest neighbor) so modules stay crisp; under 2 dots per module is refused.
  Placed left of the text on wide labels, above it on tall ones. Decoded with OpenCV from the
  preview, and the printed 40x30 label scans with a phone.
- **Text wrapping**: text without explicit line breaks is tried at every wrap width (never
  splitting words) and the layout with the largest font wins.
- **Config file**: optional `tprint.toml` (next to `tprint.py`, then `~/.config/tprint/`) with
  `bt_address`. Explicit `-b`/`TPRINT_BT` wins with no fallback; the configured address falls
  back to USB when the connect fails, so a USB-only session still works with the file present.
- **Web designer is a leaf**: `web/server.py` imports `tprint`; nothing in `tprint.py` knows
  about it. The browser renders each label to the final 1-bit raster (text and QR thresholded,
  images Floyd-Steinberg dithered per element) and the server prints that PNG unchanged, so
  the preview is exactly what burns. Rotation happens in the browser (quarter turn
  counterclockwise, same as `-r`), so the server only checks the width fits the 384-dot head.
- **QR in the web editor**: matrices come from the server's segno (`/api/qr`), no JS QR library,
  so the CLI and the editor encode identically. Same whole-dots-per-module rule, hatched box
  when under 2 dots per module.
- **Unicode search, no model**: Python's `unicodedata` names (29.5k after dropping controls,
  combining marks and algorithmically named CJK/Hangul/Tangut ranges), served gzipped once
  (about 300 KB). Each query word must match a name as whole word, prefix, substring or
  compact subsequence starting at a word; a small synonym table covers words Unicode spells
  differently (tick, deg, bolt, ...). A full scan takes a few ms, so a tiny LLM or embedding
  model would only add tens of MB for little gain. A plain subsequence was too loose: "tick"
  matched T..I..C..K across long names, hence the word-start and span limit.
- **Cross-site posts**: the server binds to localhost, but any page in the browser could post
  to it. POSTs need `Content-Type: application/json` (forces a CORS preflight that fails) and a
  matching `Origin` when one is sent.
- **Rotate toggle transposes boxes**: element boxes are in design-orientation mm; turning a
  label swaps each box's x/y and w/h so content stays on the label instead of being clipped.
