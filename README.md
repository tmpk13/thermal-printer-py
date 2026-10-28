# tprint

Prints text, QR codes or images to a Supvan T50 Pro thermal label printer from Python, over USB (`1820:207f`)
or Bluetooth Classic.

## Setup

```sh
mise run udev      # USB, once: allows non-root access to /dev/hidrawN
bluetoothctl --timeout 10 scan on   # Bluetooth: printer advertises as its serial, e.g. T0205C...
cp tprint.example.toml tprint.toml  # optional: Bluetooth address to try before USB
```

Run through [uv](https://docs.astral.sh/uv/) (`uv run tprint.py`), which installs Pillow and
segno. Needs the system Python (3.11+) and `liblzma.so.5` (>= 5.4).

## Usage

```sh
uv run tprint.py "Hello\nWorld"          # label size read from the printer
uv run tprint.py -i logo.png -d 6        # image, darker burn
uv run tprint.py -q https://x.y "Box 12"  # QR code with text beside it (alone if no text)
echo "Box 12" | uv run tprint.py -r      # stdin, text along the feed
uv run tprint.py -s 40x30 -p out.png Hi  # preview only, no printer needed
uv run tprint.py -b A4:93:40:05:6A:13 Hi # over Bluetooth (TPRINT_BT=addr also works)
mise r print-bt -- "Hi"                   # Bluetooth, printer found automatically
uv run tprint.py --status
TPRINT_DEBUG=1 uv run tprint.py ...      # trace frames on stderr
mise run test
```

## Web designer

```sh
mise run web       # http://127.0.0.1:8350/
```

Optional browser front end for designing labels: text, QR codes, images (dithered) and boxes
placed freely in mm, a batch of labels each with a quantity, and a Unicode character panel
with fuzzy search and a most-used bar. The preview is the exact 1-bit raster that prints. The
batch lives in browser storage; Export/Import saves it as JSON. `tprint.py` does not depend on it.

Text and QR data take variables: `{name}` comes from the label's variable table (CSV or rows
pasted from a spreadsheet, first line naming the columns), and each table row prints one label.
Built in: `{n}` (running number from Start), `{copy}`, `{row}`, `{total}`, `{date}`, `{time}`;
`{n:03}` pads with zeros. Unknown names print as written.

Ctrl+Z / Ctrl+Shift+Z (or the Undo/Redo buttons) undo and redo edits for the session.

## Architecture

```mermaid
classDiagram
    class main { argparse CLI }
    class render { render_text() render_qr_label() fit_image() }
    class raster { to_head() print_buffers() }
    class compress { lzma_block() pack_blocks() }
    class Printer { status() label_mm() print_image() }
    class UsbPrinter { cmd() send_block() }
    class BtPrinter { cmd() send_block() }
    class config { load_config() connect() }
    class server { web/server.py: /api/print /api/qr /api/unicode /api/printer }
    class Job { start() cancel }
    class browser { app.js render.js raster.js search.js vars.js history.js }
    main --> render : label-sized 1-bit image
    main --> config
    config --> Printer : tprint.toml bt_address, else USB
    browser --> server : 1-bit PNG per label, row or numbered copy + copies
    server --> Job
    Job --> config : tprint.connect()
    Printer <|-- UsbPrinter
    Printer <|-- BtPrinter
    Printer --> raster : 4096 B buffers
    Printer --> compress : LZMA blocks
    UsbPrinter --> hidraw : 64 B reports
    BtPrinter --> rfcomm : 7E 5A frames, ch 1
```

Protocol reference: [heeen/supvan-cups](https://github.com/heeen/supvan-cups) (MIT).
