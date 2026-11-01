package tprint.app;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.util.ArrayList;
import java.util.List;

import org.tukaani.xz.LZMA2Options;
import org.tukaani.xz.LZMAOutputStream;

/** Supvan T50 Pro raster: head packing, firmware print buffers and compressed blocks. */
final class Raster {
    static final int HEAD_DOTS = 384, BPL = 48, BUF_SIZE = 4096, HDR = 14;
    static final int MAX_COLS = 4074 / BPL; // image bytes per print buffer, in whole columns
    // BUF_FULL speed (roughly mm/s) by compressed bytes per buffer, densest first.
    private static final int[][] SPEED_STEPS = {
        {3000, 10}, {2800, 15}, {2500, 20}, {2000, 25}, {1500, 40}, {1000, 45}, {500, 55}};

    private Raster() {}

    /** 1-bit label image, row-major, x across the head; true burns a dot. */
    static final class Mono {
        final int width, height;
        final boolean[] burn;

        Mono(int width, int height, boolean[] burn) {
            if (width <= 0 || height <= 0 || width > HEAD_DOTS) {
                throw new IllegalArgumentException("label " + width + "x" + height
                        + " dots does not fit the " + HEAD_DOTS + "-dot head");
            }
            this.width = width;
            this.height = height;
            this.burn = burn;
        }
    }

    /** Compressed blocks and the print speed the densest of them allows. */
    static final class Packed {
        final List<byte[]> blocks;
        final int speed;

        Packed(List<byte[]> blocks, int speed) {
            this.blocks = blocks;
            this.speed = speed;
        }
    }

    /**
     * Rows become feed columns: the image is centered on the head, 48 bytes per row, LSB-first.
     * Head dot 0 prints on the right of the label as it leaves the printer, so rows are
     * mirrored to make image x run left to right on paper.
     */
    static byte[] toHead(Mono img) {
        byte[] out = new byte[img.height * BPL];
        int left = (HEAD_DOTS - img.width) / 2;
        for (int y = 0; y < img.height; y++) {
            for (int x = 0; x < img.width; x++) {
                if (img.burn[y * img.width + x]) {
                    int p = HEAD_DOTS - 1 - (left + x);
                    out[y * BPL + p / 8] |= (byte) (1 << (p % 8));
                }
            }
        }
        return out;
    }

    /** Split packed columns into 4096-byte buffers with the firmware header and checksum. */
    static List<byte[]> printBuffers(byte[] data, int density) {
        int cols = data.length / BPL;
        List<byte[]> out = new ArrayList<>();
        for (int c = 0; c < cols; c += MAX_COLS) {
            int n = Math.min(MAX_COLS, cols - c);
            boolean last = c + MAX_COLS >= cols;
            byte[] b = new byte[BUF_SIZE];
            b[2] = (byte) ((c == 0 ? 0x02 : 0) | (last ? 0x0C : 0)); // page start / page + job end
            b[3] = (byte) ((density & 15) << 2 | 1 << 6); // black density, material = label
            le16(b, 4, n); // columns
            b[6] = BPL;
            le16(b, 8, 1); // margins
            le16(b, 10, 1);
            b[12] = (byte) (density & 15); // red density
            System.arraycopy(data, c * BPL, b, HDR, n * BPL);
            int sum = 0;
            for (int i = 2; i < HDR; i++) {
                sum += b[i] & 0xFF;
            }
            for (int i = 255; i < HDR + n * BPL; i += 256) {
                sum += b[i] & 0xFF;
            }
            le16(b, 0, sum);
            out.add(b);
        }
        return out;
    }

    /** LZMA1 "alone" stream with the size in its header and no end marker: the firmware
     *  drops data after one. */
    static byte[] lzmaBlock(byte[] data) {
        try {
            LZMA2Options o = new LZMA2Options(6);
            o.setDictSize(8192);
            o.setLcLp(3, 0);
            o.setPb(2);
            o.setNiceLen(128);
            ByteArrayOutputStream out = new ByteArrayOutputStream(data.length / 4 + 64);
            // A known input size goes in the header and suppresses the end marker.
            LZMAOutputStream z = new LZMAOutputStream(out, o, data.length);
            z.write(data);
            z.close();
            return out.toByteArray();
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    /** Bluetooth: up to 4 buffers per block, fewer until it compresses to one buffer. */
    static Packed packGrouped(List<byte[]> buffers) {
        List<byte[]> blocks = new ArrayList<>();
        int speed = 60;
        for (int at = 0; at < buffers.size(); ) {
            int take = Math.min(4, buffers.size() - at);
            byte[] z;
            while ((z = lzmaBlock(concat(buffers.subList(at, at + take)))).length > BUF_SIZE) {
                if (take == 1) {
                    throw new IllegalArgumentException("print buffer does not compress into one block");
                }
                take--;
            }
            speed = Math.min(speed, speedFor(z.length / take));
            blocks.add(z);
            at += take;
        }
        return new Packed(blocks, speed);
    }

    private static int speedFor(int perBuffer) {
        for (int[] step : SPEED_STEPS) {
            if (perBuffer > step[0]) {
                return step[1];
            }
        }
        return 60;
    }

    static byte[] concat(List<byte[]> parts) {
        int n = 0;
        for (byte[] p : parts) {
            n += p.length;
        }
        byte[] out = new byte[n];
        int at = 0;
        for (byte[] p : parts) {
            System.arraycopy(p, 0, out, at, p.length);
            at += p.length;
        }
        return out;
    }

    static void le16(byte[] b, int at, int v) {
        b[at] = (byte) v;
        b[at + 1] = (byte) (v >> 8);
    }

    static int le16(byte[] b, int at) {
        return (b[at] & 0xFF) | (b[at + 1] & 0xFF) << 8;
    }
}
