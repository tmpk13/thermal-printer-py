package tprint.app;

import java.io.Closeable;
import java.io.IOException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.function.Predicate;

/** Bluetooth Classic RFCOMM print flow: 16-byte little-endian 7E 5A frames, variable replies. */
final class BtPrinter implements Closeable {
    static final int CMD_BUF_FULL = 0x10, CMD_STATUS = 0x11, CMD_CHECK = 0x12, CMD_START = 0x13,
            CMD_STOP = 0x14, CMD_MATERIAL = 0x30, CMD_BULK = 0x5C;
    private static final int ST = 14, MAT = 40; // reply offsets of the status bytes and label size
    private static final long READ_TIMEOUT_MS = 2000;
    private static final byte[] EMPTY = new byte[0];
    // Status register bits: byte within the status block, mask, error.
    private static final Object[][] ERRORS = {
        {0, 0x02, "label read/write error"}, {0, 0x04, "label roll end"},
        {0, 0x08, "label mode mismatch"}, {0, 0x10, "ribbon read/write error"},
        {0, 0x20, "ribbon end"}, {1, 0x08, "printhead too hot"},
        {2, 0x08, "cover open"}, {3, 0x01, "no label installed"}};

    /** Byte stream to the printer. */
    interface Link extends Closeable {
        void write(byte[] data) throws IOException;

        /** Bytes already received, without blocking; empty when there are none. */
        byte[] poll() throws IOException;
    }

    static final class Status {
        final boolean bufFull, busy, printing;

        Status(boolean bufFull, boolean busy, boolean printing) {
            this.bufFull = bufFull;
            this.busy = busy;
            this.printing = printing;
        }
    }

    private final Link link;
    private byte[] buf = EMPTY; // received bytes not yet framed

    BtPrinter(Link link) {
        this.link = link;
    }

    @Override
    public void close() throws IOException {
        link.close();
    }

    /** 16-byte command: magic, length 12, proto, marker, cmd, checksum, LE params. */
    static byte[] frame(int cmd, int p, int p2) {
        byte[] f = {0x7E, 0x5A, 12, 0, 0x10, 1, (byte) 0xAA, (byte) cmd, 0, 0, 0, 1, 0, 0, 0, 0};
        Raster.le16(f, 12, p);
        Raster.le16(f, 14, p2);
        int sum = 0;
        for (int i = 10; i < 16; i++) {
            sum += f[i] & 0xFF;
        }
        Raster.le16(f, 8, sum);
        return f;
    }

    /** Split a compressed block into 512-byte frames, each one AA BB packet of 500 bytes. */
    static List<byte[]> dataFrames(byte[] blk) {
        int n = (blk.length + 499) / 500;
        List<byte[]> out = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            byte[] f = new byte[512];
            f[0] = 0x7E;
            f[1] = 0x5A;
            Raster.le16(f, 2, 508);
            f[4] = 0x10;
            f[5] = 0x02;
            f[6] = (byte) 0xAA;
            f[7] = (byte) 0xBB;
            f[10] = (byte) i;
            f[11] = (byte) n;
            System.arraycopy(blk, i * 500, f, 12, Math.min(500, blk.length - i * 500));
            int sum = 0;
            for (int j = 10; j < 512; j++) {
                sum += f[j] & 0xFF;
            }
            Raster.le16(f, 8, sum);
            out.add(f);
        }
        return out;
    }

    Status status() throws IOException {
        byte[] r = cmd(CMD_STATUS, 0, 0);
        if (r.length < ST + 4) {
            throw new IOException("short status reply");
        }
        List<String> errs = new ArrayList<>();
        for (Object[] e : ERRORS) {
            if ((r[ST + (int) e[0]] & (int) e[1]) != 0) {
                errs.add((String) e[2]);
            }
        }
        if (!errs.isEmpty()) {
            throw new IOException(String.join(", ", errs));
        }
        return new Status((r[ST] & 1) != 0, (r[ST + 1] & 4) != 0, (r[ST + 2] & 0x40) != 0);
    }

    /** {width, length} of the loaded label in mm, {0, 0} if unknown. */
    int[] labelMm() throws IOException {
        byte[] r = cmd(CMD_MATERIAL, 0, 0);
        return r.length > MAT + 1 ? new int[] {r[MAT] & 0xFF, r[MAT + 1] & 0xFF} : new int[] {0, 0};
    }

    void printImage(Raster.Mono img, int density) throws IOException, InterruptedException {
        Raster.Packed packed = Raster.packGrouped(
                Raster.printBuffers(Raster.toHead(img), density));
        cmd(CMD_CHECK, 0, 0);
        await("idle", s -> !s.busy && !s.printing, 6000, 100);
        cmd(CMD_START, 0, 0);
        try {
            await("print start", s -> s.printing, 6000, 100);
            for (byte[] blk : packed.blocks) {
                await("buffer space", s -> !s.bufFull, 15000, 20);
                sendBlock(blk);
                Thread.sleep(20);
                cmd(CMD_BUF_FULL, blk.length, packed.speed);
            }
            await("print end", s -> !s.printing && !s.busy, 30000, 100);
        } catch (Exception e) {
            try {
                cmd(CMD_STOP, 0, 0);
            } catch (Exception ignored) {
                // keep the original error
            }
            throw e;
        }
    }

    private void await(String what, Predicate<Status> cond, long timeoutMs, long intervalMs)
            throws IOException, InterruptedException {
        long end = System.nanoTime() + timeoutMs * 1_000_000;
        while (!cond.test(status())) {
            if (System.nanoTime() > end) {
                throw new IOException("timed out waiting for " + what);
            }
            Thread.sleep(intervalMs);
        }
    }

    byte[] cmd(int cmd, int p, int p2) throws IOException {
        drain();
        link.write(frame(cmd, p, p2));
        for (byte[] r; (r = read()).length > 0; ) { // skip stray frames that echo another command
            if (r.length >= 8 && (r[7] & 0xFF) == cmd) {
                return r;
            }
        }
        throw new IOException(String.format("no response to command 0x%02x", cmd));
    }

    private void sendBlock(byte[] blk) throws IOException, InterruptedException {
        List<byte[]> frames = dataFrames(blk);
        cmd(CMD_BULK, 512, frames.size());
        for (int i = 0; i < frames.size(); i++) {
            byte[] f = frames.get(i);
            for (int j = 0; j < f.length; j += 128) { // the firmware wants 128-byte writes, 10 ms apart
                Thread.sleep(10);
                drain();
                link.write(Arrays.copyOfRange(f, j, Math.min(j + 128, f.length)));
            }
            if (i < frames.size() - 1) { // the last ack only comes once the block is digested
                read();
            }
        }
    }

    private void drain() throws IOException {
        while (link.poll().length > 0) {
            // discard
        }
        buf = EMPTY;
    }

    /** Next reply frame, resyncing on the 7E 5A magic; empty after 2 s without data. */
    private byte[] read() throws IOException {
        long end = System.nanoTime() + READ_TIMEOUT_MS * 1_000_000;
        for (;;) {
            int i = 0;
            while (i + 1 < buf.length && !(buf[i] == 0x7E && buf[i + 1] == 0x5A)) {
                i++;
            }
            if (i + 4 <= buf.length) {
                int stop = i + 4 + Raster.le16(buf, i + 2);
                if (stop <= buf.length) {
                    byte[] frame = Arrays.copyOfRange(buf, i, stop);
                    buf = Arrays.copyOfRange(buf, stop, buf.length);
                    return frame;
                }
            }
            byte[] chunk = link.poll();
            if (chunk.length > 0) {
                byte[] grown = Arrays.copyOf(buf, buf.length + chunk.length);
                System.arraycopy(chunk, 0, grown, buf.length, chunk.length);
                buf = grown;
                end = System.nanoTime() + READ_TIMEOUT_MS * 1_000_000;
            } else if (System.nanoTime() > end) {
                return EMPTY;
            } else {
                try {
                    Thread.sleep(5);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    throw new IOException("interrupted");
                }
            }
        }
    }
}
