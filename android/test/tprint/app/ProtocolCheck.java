package tprint.app;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.Socket;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.Arrays;
import java.util.List;

/**
 * Desktop entry point to the phone app's protocol code, for comparing it with tprint.py:
 *   pack IMAGE.pbm DENSITY        head, buffer, block and speed lines (hex)
 *   print IMAGE.pbm DENSITY PORT  label size, then one print to a fake printer on localhost
 *   qr ERROR DATA                 QR module rows
 */
public final class ProtocolCheck {
    public static void main(String[] args) throws Exception {
        switch (args[0]) {
            case "pack": {
                byte[] head = Raster.toHead(readPbm(args[1]));
                List<byte[]> buffers = Raster.printBuffers(head, Integer.parseInt(args[2]));
                Raster.Packed packed = Raster.packGrouped(buffers);
                System.out.println("head " + hex(head));
                for (byte[] b : buffers) {
                    System.out.println("buffer " + hex(b));
                }
                for (byte[] b : packed.blocks) {
                    System.out.println("block " + hex(b));
                }
                System.out.println("speed " + packed.speed);
                break;
            }
            case "print": {
                Raster.Mono img = readPbm(args[1]);
                try (BtPrinter p = new BtPrinter(new SocketLink(
                        new Socket("127.0.0.1", Integer.parseInt(args[3]))))) {
                    int[] mm = p.labelMm();
                    System.out.println("label " + mm[0] + " " + mm[1]);
                    p.printImage(img, Integer.parseInt(args[2]));
                    System.out.println("done");
                } catch (IOException e) {
                    System.out.println("error " + e.getMessage());
                }
                break;
            }
            case "qr":
                try {
                    for (String row : Qr.rows(args[2], args[1])) {
                        System.out.println(row);
                    }
                } catch (IllegalArgumentException e) {
                    System.out.println("error " + e.getMessage());
                }
                break;
            default:
                throw new IllegalArgumentException(args[0]);
        }
    }

    /** Binary PBM (P4): rows MSB-first, 1 = black. */
    static Raster.Mono readPbm(String path) throws IOException {
        byte[] d = Files.readAllBytes(Paths.get(path));
        int[] at = {0};
        if (!"P4".equals(token(d, at))) {
            throw new IOException("not a P4 PBM");
        }
        int w = Integer.parseInt(token(d, at)), h = Integer.parseInt(token(d, at));
        int start = at[0] + 1, stride = (w + 7) / 8;
        boolean[] burn = new boolean[w * h];
        for (int y = 0; y < h; y++) {
            for (int x = 0; x < w; x++) {
                burn[y * w + x] = (d[start + y * stride + x / 8] & (0x80 >> (x % 8))) != 0;
            }
        }
        return new Raster.Mono(w, h, burn);
    }

    private static String token(byte[] d, int[] at) {
        for (;;) {
            while (Character.isWhitespace(d[at[0]])) {
                at[0]++;
            }
            if (d[at[0]] != '#') {
                break;
            }
            while (d[at[0]] != '\n') {
                at[0]++;
            }
        }
        int s = at[0];
        while (!Character.isWhitespace(d[at[0]])) {
            at[0]++;
        }
        return new String(d, s, at[0] - s);
    }

    private static String hex(byte[] b) {
        StringBuilder s = new StringBuilder(b.length * 2);
        for (byte v : b) {
            s.append(String.format("%02x", v));
        }
        return s.toString();
    }

    static final class SocketLink implements BtPrinter.Link {
        private final Socket sock;
        private final InputStream in;
        private final OutputStream out;

        SocketLink(Socket sock) throws IOException {
            this.sock = sock;
            this.in = sock.getInputStream();
            this.out = sock.getOutputStream();
        }

        @Override
        public void write(byte[] data) throws IOException {
            out.write(data);
            out.flush();
        }

        @Override
        public byte[] poll() throws IOException {
            int n = in.available();
            if (n <= 0) {
                return new byte[0];
            }
            byte[] b = new byte[n];
            int got = in.read(b);
            return got <= 0 ? new byte[0] : Arrays.copyOf(b, got);
        }

        @Override
        public void close() throws IOException {
            sock.close();
        }
    }
}
