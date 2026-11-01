package tprint.app;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * The label designer in a WebView. Its files come from the APK under a reserved https origin
 * (never resolved, so no network is involved), and its api requests are answered here instead
 * of by the desktop server.
 */
public final class MainActivity extends Activity {
    static final String HOST = "appassets.androidplatform.net";
    private static final int PICK = 1, SAVE = 2, PERMISSIONS = 3;

    private WebView web;
    private Api api;
    private ValueCallback<Uri[]> picked; // waiting for a file chooser result
    private String saving; // export text waiting for the user to choose where it goes

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        WebView.setWebContentsDebuggingEnabled(
                (getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0);
        api = new Api(new Bluetooth(this));
        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true); // the batch lives in localStorage
        s.setAllowFileAccess(false);
        // Static nested classes only: javac 21 gives an inner class constructor a nameless
        // MethodParameters entry for the outer instance, and d8 8.2 crashes on it.
        web.setWebViewClient(new Client(this));
        web.setWebChromeClient(new Chrome(this));
        web.addJavascriptInterface(new Bridge(this), "TPrintNative");
        setContentView(web);
        web.loadUrl("https://" + HOST + "/index.html");
        requestPermissionsIfNeeded();
    }

    @Override
    protected void onDestroy() {
        web.destroy();
        super.onDestroy();
    }

    private void requestPermissionsIfNeeded() {
        List<String> missing = new ArrayList<>();
        for (String p : Bluetooth.permissions()) {
            if (checkSelfPermission(p) != PackageManager.PERMISSION_GRANTED) {
                missing.add(p);
            }
        }
        if (!missing.isEmpty()) {
            requestPermissions(missing.toArray(new String[0]), PERMISSIONS);
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        // The page checked the printer at load, before the answer; its chip checks again.
        web.evaluateJavascript("document.getElementById('printer').click()", null);
    }

    @Override
    protected void onActivityResult(int code, int result, Intent data) {
        if (code == PICK && picked != null) {
            picked.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result, data));
            picked = null;
        } else if (code == SAVE && saving != null) {
            Uri uri = result == RESULT_OK && data != null ? data.getData() : null;
            if (uri != null) {
                try (OutputStream out = getContentResolver().openOutputStream(uri)) {
                    out.write(saving.getBytes(StandardCharsets.UTF_8));
                } catch (IOException | NullPointerException e) {
                    web.evaluateJavascript("console.error('export failed')", null);
                }
            }
            saving = null;
        }
    }

    private void save(String name, String mime, String text) {
        saving = text;
        Intent i = new Intent(Intent.ACTION_CREATE_DOCUMENT)
                .addCategory(Intent.CATEGORY_OPENABLE).setType(mime)
                .putExtra(Intent.EXTRA_TITLE, name);
        startActivityForResult(i, SAVE);
    }

    private WebResourceResponse serve(WebResourceRequest req) {
        Uri url = req.getUrl();
        if (!HOST.equals(url.getHost())) {
            return text(403, "text/plain", "blocked"); // the page never needs another site
        }
        String path = url.getPath() == null || url.getPath().equals("/") ? "/index.html" : url.getPath();
        if (path.equals("/api/unicode")) {
            return asset("unicode.json"); // built from Python's unicodedata with the APK
        }
        if (path.startsWith("/api/")) {
            String route = path.substring(5);
            Api.Reply r = "POST".equals(req.getMethod())
                    ? api.post(route, "{}") // bodies only arrive through the bridge
                    : api.get(route, url);
            return text(r.status, "application/json", r.json);
        }
        return asset("web" + path);
    }

    private WebResourceResponse asset(String name) {
        try {
            InputStream in = getAssets().open(name);
            WebResourceResponse r = new WebResourceResponse(mime(name), "utf-8", in);
            r.setResponseHeaders(Collections.singletonMap("Cache-Control", "no-cache"));
            return r;
        } catch (IOException e) {
            return text(404, "text/plain", "not found");
        }
    }

    private static WebResourceResponse text(int status, String mime, String body) {
        return new WebResourceResponse(mime, "utf-8", status, status < 400 ? "OK" : "Error",
                Collections.singletonMap("Cache-Control", "no-cache"),
                new ByteArrayInputStream(body.getBytes(StandardCharsets.UTF_8)));
    }

    private static String mime(String name) {
        String ext = name.substring(name.lastIndexOf('.') + 1);
        switch (ext) {
            case "html": return "text/html";
            case "js": return "text/javascript";
            case "css": return "text/css";
            case "json": return "application/json";
            case "svg": return "image/svg+xml";
            case "png": return "image/png";
            default: return "application/octet-stream";
        }
    }

    static final class Client extends WebViewClient {
        private final MainActivity a;

        Client(MainActivity a) {
            this.a = a;
        }

        @Override
        public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest req) {
            String scheme = req.getUrl().getScheme();
            return "http".equals(scheme) || "https".equals(scheme) ? a.serve(req) : null;
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
            if (HOST.equals(req.getUrl().getHost())) {
                return false;
            }
            try { // links to other sites open in the browser
                a.startActivity(new Intent(Intent.ACTION_VIEW, req.getUrl()));
            } catch (ActivityNotFoundException ignored) {
                // nothing can open it
            }
            return true;
        }
    }

    static final class Chrome extends WebChromeClient {
        private final MainActivity a;

        Chrome(MainActivity a) {
            this.a = a;
        }

        @Override
        public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                         FileChooserParams params) {
            if (a.picked != null) {
                a.picked.onReceiveValue(null);
            }
            a.picked = callback;
            Intent i = params.createIntent();
            // Pickers grey out a .json file typed as application/json by extension only.
            String[] accept = params.getAcceptTypes();
            boolean images = accept.length > 0 && accept[0].startsWith("image/");
            i.setType(images ? "image/*" : "*/*");
            try {
                a.startActivityForResult(i, PICK);
                return true;
            } catch (ActivityNotFoundException e) {
                a.picked = null;
                return false;
            }
        }
    }

    /** Calls from the page. A WebView hides request bodies from shouldInterceptRequest, so
     *  POSTs come through here instead of fetch. */
    static final class Bridge {
        private final MainActivity a;

        Bridge(MainActivity a) {
            this.a = a;
        }

        /** Returns {"status": code, "body": reply}. */
        @JavascriptInterface
        public String post(String path, String body) {
            Api.Reply r = a.api.post(path.replaceFirst("^/?api/", ""), body);
            try {
                return new JSONObject().put("status", r.status)
                        .put("body", new JSONObject(r.json)).toString();
            } catch (JSONException e) {
                return "{\"status\":500,\"body\":{\"error\":\"bad reply\"}}";
            }
        }

        @JavascriptInterface
        public void saveFile(String name, String mime, String text) {
            a.runOnUiThread(() -> a.save(name, mime, text));
        }
    }
}
