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
