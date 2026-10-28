# Completed

- USB HID transport (hidraw, report-ID prefix, per-buffer LZMA blocks)
- Bluetooth RFCOMM transport (7E 5A framing, acked 512 B data frames, grouped blocks)
- Text rendering auto-fit to label, image dithering, preview, rotate, density
- Label size read from the printer
- Mirroring fix: text verified correct on paper over Bluetooth and USB
- QR code with text on the same label; word-wrapping text fit
- `print-bt` and `scan` mise tasks
- Optional `tprint.toml` with the Bluetooth address, falling back to USB
- Web label designer: free placement of text/QR/image/box, batches with quantities, Unicode
  panel with fuzzy search and most-used bar, export/import, print progress and cancel
- Text rotation in the web designer (R / Shift+R and a Rotate button)
