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
- **Regular QR only**: `segno.make` silently picks Micro QR whenever the data fits one
  ("Box 12" became a 15-module M3, any URL under ~20 bytes too), and phone cameras, ZXing and
  ML Kit do not read Micro QR. Both the CLI and the server call `segno.make_qr`.
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
- **Text rotation**: `rot` (0/90/180/270) per text element. A turn swaps the box's width and
  height about its center (snapped to the dot grid, trimmed to the label), and the text is laid
  out in the unturned box then drawn through a canvas rotation, so auto-fit sizing is the same
  at every angle.
- **Shortcut focus**: canvas pointerdown calls preventDefault (no text selection while dragging),
  which also keeps focus in the last input, so R/Delete/arrows went to that input. Pointerdown
  now blurs a focused input first.
- **Save flush**: batch saves are debounced 300 ms; a pending save is flushed on `pagehide` and
  when the tab is hidden, otherwise a reload right after an edit dropped it.
- **Resize handles on top**: handles live in their own overlay layer, drawn after every element
  box, rather than inside the selected box; otherwise a later element's box covered them and a
  click on a handle selected that element instead. The selected box itself is not raised, so
  elements inside it (text in a border box) stay clickable.
- **Variables**: `{name}` placeholders in text and QR data, filled from a per-label CSV/TSV
  table (header line = names, each later line = one label) plus built-ins. There is no escape
  syntax; unknown names are left as written instead, so existing text with braces (JSON in a
  QR) is untouched. `{n}` runs over rows and copies in print order (`start + row * copies +
  copy`). A label is rendered once per row and sent with its copy count, unless a template
  uses `{n}` or `{copy}`; then every copy is its own image, since each one differs.
- **QR prefetch**: `missing()` checks every element instead of stopping at the first gap, so one
  pass starts all QR fetches for a print run; a batch of numbered QR labels then waits for one
  round of requests, not one per label.
- **Undo by snapshots**: every edit already ends in `save()`, so the history records a copy of
  the whole batch there instead of per-action inverse code that a new action could forget. The
  copy shares strings, so image data URLs are not duplicated per step; unchanged states (a
  selection, a QR arriving) add nothing, and the preview `row` is ignored. Records in the same
  group (one field, one element's arrow nudges) under 1 s apart merge into one step; a drag is
  not recorded while it moves, only on pointerup. Text boxes keep the browser's own Ctrl+Z.
  The history is in memory only and starts empty on reload.
- **Phone app shape**: the designer's own files in an Android WebView, served from the APK under
  `https://appassets.androidplatform.net/` (a reserved name, never resolved), so the page is a
  secure context with stable localStorage and needs no INTERNET permission. Plain Java built by
  `android/build.sh` with aapt2, javac, d8 and apksigner, no Gradle; the APK is about 320 KB.
  Bluetooth Classic SPP rules out iOS (no RFCOMM without MFi) and Web Bluetooth (BLE only).
- **api in the app**: GETs are answered in `shouldInterceptRequest`, but a WebView never hands
  the app a request body, so `app.js` posts through `window.TPrintNative.post()` when that
  exists and wraps the reply in a `Response`. Export goes through `saveFile()` the same way: a
  WebView cannot download a blob URL. The Unicode index is generated at build time with the
  server's own `unicode_index()`.
- **Protocol port**: `Raster.java` and `BtPrinter.java` mirror `tprint.py` and avoid Android
  classes, so `tests/test_android.py` runs them on the desktop JVM: head bytes and print buffers
  are compared byte for byte, and a fake printer on a socket receives both the Python and Java
  print flows and compares the command sequence and the decompressed data. The fake only caught
  a data-frame checksum bug once a noisy label made blocks span several frames (frame index 0
  hides an off-by-one in the summed range).
- **LZMA in Java**: xz-java's `LZMAOutputStream` with a known input size writes the alone header
  with that size and no end marker. Compressed bytes differ from liblzma's, so the test decodes
  each block with liblzma's LZMA1EXT decoder told the exact size and no marker, and requires
  every input byte consumed.
- **d8 and javac 21**: javac 21 gives every non-static inner class constructor a nameless
  `MethodParameters` entry for the outer instance, and d8 8.2 (build-tools 34) crashes on it with
  an NPE. Nested classes in the app are all static; xz-java 1.10 has the same entries, so 1.9.
- **QR in the app**: Nayuki's qrcodegen picks the same version and error level as
  `segno.make_qr` (with level boost) but a different mask and padding, so modules differ. Both
  scan; the test decodes the Java output with zxing-cpp instead of comparing modules.
- **RFCOMM on Android**: the printer listens on channel 1; the app asks for the SPP UUID first
  (public API, needs an SDP record) and falls back to the hidden `createInsecureRfcommSocket(1)`.
  `BluetoothSocket` streams have no read timeout, so the reader polls `available()` every 5 ms
  with the same 2 s timeout as the Python socket. Printer choice: last address that worked,
  then paired devices named `T0<digit>`, then a 12 s discovery (`BLUETOOTH_SCAN` with
  `neverForLocation`; location only on Android 11 and older).
