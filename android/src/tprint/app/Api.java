package tprint.app;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.net.Uri;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;

/** The designer's api routes, answered on the phone with the printer over Bluetooth. Same
 *  requests and replies as the desktop server, so the page cannot tell them apart. */
final class Api {
    private static final int MAX_BODY = 32 << 20;

    /** HTTP status and JSON body. */
    static final class Reply {
        final int status;
        final String json;

        Reply(int status, Object json) {
            this.status = status;
            this.json = json.toString();
        }

        static Reply error(int status, String msg) {
            try {
                return new Reply(status, new JSONObject().put("error", msg));
            } catch (JSONException e) {
                throw new IllegalStateException(e);
            }
        }
    }

    private final Bluetooth bt;
    private final Job job;

    Api(Bluetooth bt) {
        this.bt = bt;
        this.job = new Job(bt);
    }

    /** path without the leading "/api/". */
    Reply get(String path, Uri url) {
        try {
            switch (path) {
                case "printer":
                    return printer();
                case "job":
                    return new Reply(200, job.snapshot());
                case "qr":
                    String data = url.getQueryParameter("data");
                    String error = url.getQueryParameter("error");
                    try {
                        return new Reply(200, new JSONObject().put("rows", new JSONArray(
                                Qr.rows(data == null ? "" : data, error == null ? "m" : error))));
                    } catch (IllegalArgumentException e) { // also data too long
                        return Reply.error(400, String.valueOf(e.getMessage()));
                    }
                default:
                    return Reply.error(404, "not found");
            }
        } catch (JSONException e) {
            return Reply.error(500, e.toString());
        }
    }

    Reply post(String path, String body) {
        switch (path) {
            case "print":
                return print(body);
            case "job/cancel":
                job.cancel = true;
                return new Reply(200, job.snapshot());
            default:
                return Reply.error(404, "not found");
        }
    }

    private Reply printer() throws JSONException {
        if (job.isRunning()) {
            return Reply.error(409, "printing");
        }
        try (BtPrinter p = bt.connect()) {
            int[] mm = p.labelMm();
            BtPrinter.Status s = p.status();
            return new Reply(200, new JSONObject()
                    .put("label_mm", mm[0] > 0 && mm[1] > 0 ? new JSONArray().put(mm[0]).put(mm[1])
                            : JSONObject.NULL)
                    .put("status", new JSONObject().put("buf_full", s.bufFull)
                            .put("busy", s.busy).put("printing", s.printing)));
        } catch (IOException e) {
            return Reply.error(503, message(e));
        }
    }

    /** Body: {"density": 0-15, "labels": [{"png": data URL, "copies": n}, ...]}. */
    private Reply print(String body) {
        int density;
        List<Raster.Mono> images = new ArrayList<>();
        List<Integer> copies = new ArrayList<>();
        try {
            if (body.length() > MAX_BODY) {
                return Reply.error(413, "request too large");
            }
            JSONObject req = new JSONObject(body);
            density = req.optInt("density", 4);
            if (density < 0 || density > 15) {
                throw new IllegalArgumentException("density must be 0-15");
            }
            JSONArray labels = req.getJSONArray("labels");
            for (int i = 0; i < labels.length(); i++) {
                JSONObject lb = labels.getJSONObject(i);
                int n = lb.optInt("copies", 1);
                Raster.Mono img = decodeLabel(lb.getString("png"));
                if (n > 0) {
                    images.add(img);
                    copies.add(n);
                }
            }
            if (images.isEmpty()) {
                throw new IllegalArgumentException("nothing to print");
            }
        } catch (JSONException | IllegalArgumentException e) {
            return Reply.error(400, "bad request: " + e.getMessage());
        }
        if (!job.start(images, copies, density)) {
            return Reply.error(409, "already printing");
        }
        return new Reply(202, job.snapshot());
    }

    /** 1-bit label from a PNG data URL: transparent counts as white, then any pixel darker
     *  than mid gray burns. */
    static Raster.Mono decodeLabel(String dataUrl) {
        byte[] png = Base64.decode(dataUrl.substring(dataUrl.indexOf(',') + 1), Base64.DEFAULT);
        Bitmap bm = BitmapFactory.decodeByteArray(png, 0, png.length);
        if (bm == null) {
            throw new IllegalArgumentException("label is not a PNG");
        }
        int w = bm.getWidth(), h = bm.getHeight();
        int[] px = new int[w * h];
        bm.getPixels(px, 0, w, 0, 0, w, h);
        bm.recycle();
        boolean[] burn = new boolean[w * h];
        for (int i = 0; i < px.length; i++) {
            int c = px[i], a = Color.alpha(c);
            // Luma as Pillow's "L" conversion computes it, over a white background.
            int l = (Color.red(c) * 299 + Color.green(c) * 587 + Color.blue(c) * 114) / 1000;
            burn[i] = (l * a + 255 * (255 - a)) / 255 < 128;
        }
        return new Raster.Mono(w, h, burn);
    }

    static String message(Exception e) {
        return e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName();
    }

    /** One print run at a time: labels printed in order, each its number of copies. */
    static final class Job {
        private final Bluetooth bt;
        private boolean running;
        private int done, total;
        private String error;
        volatile boolean cancel;

        Job(Bluetooth bt) {
            this.bt = bt;
        }

        synchronized boolean isRunning() {
            return running;
        }

        synchronized JSONObject snapshot() {
            try {
                return new JSONObject().put("running", running).put("done", done)
                        .put("total", total).put("error", error == null ? JSONObject.NULL : error);
            } catch (JSONException e) {
                throw new IllegalStateException(e);
            }
        }

        synchronized boolean start(List<Raster.Mono> images, List<Integer> copies, int density) {
            if (running) {
                return false;
            }
            running = true;
            done = 0;
            total = 0;
            error = null;
            cancel = false;
            for (int n : copies) {
                total += n;
            }
            new Thread(() -> run(images, copies, density), "print-job").start();
            return true;
        }

        private void run(List<Raster.Mono> images, List<Integer> copies, int density) {
            String err = null;
            try (BtPrinter p = bt.connect()) {
                for (int i = 0; i < images.size(); i++) {
                    for (int c = 0; c < copies.get(i); c++) {
                        if (cancel) {
                            throw new InterruptedException("cancelled");
                        }
                        p.printImage(images.get(i), density);
                        synchronized (this) {
                            done++;
                        }
                    }
                }
            } catch (Exception e) {
                err = message(e);
            }
            synchronized (this) {
                running = false;
                error = err;
            }
        }
    }
}
