package tprint.app;

import android.Manifest;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothSocket;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

/** Finds the printer (it advertises its serial, T0 and a digit first) and opens RFCOMM to it. */
final class Bluetooth {
    private static final UUID SPP = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB");
    private static final Pattern NAME = Pattern.compile("T0[0-9].*");
    private static final long DISCOVERY_MS = 12000;

    private final Context ctx;
    private final SharedPreferences prefs;

    Bluetooth(Context ctx) {
        this.ctx = ctx.getApplicationContext();
        this.prefs = ctx.getSharedPreferences("printer", Context.MODE_PRIVATE);
    }

    /** Runtime permissions the app asks for: connecting, and finding unpaired printers. */
    static String[] permissions() {
        return Build.VERSION.SDK_INT >= 31
                ? new String[] {Manifest.permission.BLUETOOTH_CONNECT, Manifest.permission.BLUETOOTH_SCAN}
                // Before Android 12 discovery needs location; fine without coarse is ignored.
                : new String[] {Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION};
    }

    /** The last printer that answered, else paired printers, else the first one discovered. */
    synchronized BtPrinter connect() throws IOException {
        BluetoothManager bm = ctx.getSystemService(BluetoothManager.class);
        BluetoothAdapter ad = bm == null ? null : bm.getAdapter();
        if (ad == null) {
            throw new IOException("this device has no Bluetooth");
        }
        if (Build.VERSION.SDK_INT >= 31 && !granted(Manifest.permission.BLUETOOTH_CONNECT)) {
            throw new IOException("Bluetooth permission not granted (allow Nearby devices)");
        }
        if (!ad.isEnabled()) {
            throw new IOException("Bluetooth is off");
        }
        List<String> tried = new ArrayList<>();
        String saved = prefs.getString("address", null);
        if (saved != null) {
            tried.add(saved);
        }
        for (BluetoothDevice d : ad.getBondedDevices()) {
            if (d.getName() != null && NAME.matcher(d.getName()).matches()
                    && !tried.contains(d.getAddress())) {
                tried.add(d.getAddress());
            }
        }
        IOException last = null;
        for (String addr : tried) {
            try {
                return open(ad, addr);
            } catch (IOException e) {
                last = e;
            }
        }
        String found = discover(ad);
        if (found != null && !tried.contains(found)) {
            return open(ad, found);
        }
        if (last != null) {
            throw new IOException("printer not reachable (" + last.getMessage() + "); is it on?");
        }
        throw new IOException(canScan() ? "printer not found; is it on?"
                : "printer not found; pair it in Bluetooth settings or allow Nearby devices");
    }

    private BtPrinter open(BluetoothAdapter ad, String addr) throws IOException {
        ad.cancelDiscovery(); // a running discovery slows a connect down badly
        BluetoothDevice dev = ad.getRemoteDevice(addr);
        BluetoothSocket sock = dev.createInsecureRfcommSocketToServiceRecord(SPP);
        try {
            sock.connect();
        } catch (IOException e) {
            sock.close();
            sock = channelOne(dev, e);
        }
        prefs.edit().putString("address", addr).apply();
        return new BtPrinter(new SocketLink(sock));
    }

    /** RFCOMM channel 1 without a service lookup, which is what the printer listens on; only
     *  reachable through a hidden method, so it is the fallback. */
    private static BluetoothSocket channelOne(BluetoothDevice dev, IOException first)
            throws IOException {
        BluetoothSocket sock;
        try {
            sock = (BluetoothSocket) dev.getClass()
                    .getMethod("createInsecureRfcommSocket", int.class).invoke(dev, 1);
        } catch (ReflectiveOperationException | RuntimeException e) {
            throw first;
        }
        try {
            sock.connect();
            return sock;
        } catch (IOException e) {
            sock.close();
            throw first;
        }
    }

    private String discover(BluetoothAdapter ad) {
        if (!canScan()) {
            return null;
        }
        Found found = new Found();
        ctx.registerReceiver(found, new IntentFilter(BluetoothDevice.ACTION_FOUND));
        try {
            if (ad.startDiscovery()) {
                found.latch.await(DISCOVERY_MS, TimeUnit.MILLISECONDS);
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        } finally {
            ad.cancelDiscovery();
            ctx.unregisterReceiver(found);
        }
        return found.address;
    }

    private boolean canScan() {
        return granted(Build.VERSION.SDK_INT >= 31
                ? Manifest.permission.BLUETOOTH_SCAN : Manifest.permission.ACCESS_FINE_LOCATION);
    }

    private boolean granted(String perm) {
        return ctx.checkSelfPermission(perm) == PackageManager.PERMISSION_GRANTED;
    }

    static final class Found extends BroadcastReceiver {
        final CountDownLatch latch = new CountDownLatch(1);
        volatile String address;

        @Override
        @SuppressWarnings("deprecation") // the typed getParcelableExtra is Android 13+
        public void onReceive(Context c, Intent intent) {
            BluetoothDevice d = intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE);
            String name = intent.getStringExtra(BluetoothDevice.EXTRA_NAME);
            if (d != null && name != null && NAME.matcher(name).matches() && address == null) {
                address = d.getAddress();
                latch.countDown();
            }
        }
    }

    static final class SocketLink implements BtPrinter.Link {
        private final BluetoothSocket sock;
        private final InputStream in;
        private final OutputStream out;

        SocketLink(BluetoothSocket sock) throws IOException {
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
