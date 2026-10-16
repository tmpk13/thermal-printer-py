# tprint

Prints text or images to a Supvan T50 Pro thermal label printer from Python, over USB (`1820:207f`)
or Bluetooth Classic.

## Setup

```sh
mise run udev      # USB, once: allows non-root access to /dev/hidrawN
bluetoothctl --timeout 10 scan on   # Bluetooth: printer advertises as its serial, e.g. T0205C...
```

Requires Python 3.10+, Pillow, and the system `liblzma.so.5` (>= 5.4).

## Usage

```sh
python3 tprint.py "Hello\nWorld"          # label size read from the printer
python3 tprint.py -i logo.png -d 6        # image, darker burn
echo "Box 12" | python3 tprint.py -r      # stdin, text along the feed
python3 tprint.py -s 40x30 -p out.png Hi  # preview only, no printer needed
python3 tprint.py -b A4:93:40:05:6A:13 Hi # over Bluetooth (TPRINT_BT=addr also works)
mise r print-bt -- "Hi"                   # Bluetooth, printer found automatically
python3 tprint.py --status
TPRINT_DEBUG=1 python3 tprint.py ...      # trace frames on stderr
mise run test
```

## Architecture

```mermaid
classDiagram
    class main { argparse CLI }
    class render { render_text() fit_image() }
    class raster { to_head() print_buffers() }
    class compress { lzma_block() pack_blocks() }
    class Printer { status() label_mm() print_image() }
    class UsbPrinter { cmd() send_block() }
    class BtPrinter { cmd() send_block() }
    main --> render : label-sized 1-bit image
    main --> Printer
    Printer <|-- UsbPrinter
    Printer <|-- BtPrinter
    Printer --> raster : 4096 B buffers
    Printer --> compress : LZMA blocks
    UsbPrinter --> hidraw : 64 B reports
    BtPrinter --> rfcomm : 7E 5A frames, ch 1
```

Protocol reference: [heeen/supvan-cups](https://github.com/heeen/supvan-cups) (MIT).
