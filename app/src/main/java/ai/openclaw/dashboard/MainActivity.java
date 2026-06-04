package ai.openclaw.dashboard;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.text.InputType;
import android.util.Log;
import android.webkit.ConsoleMessage;
import android.webkit.PermissionRequest;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebStorage;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.io.InputStream;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;

public final class MainActivity extends Activity {
    private static final String PREFS = "openclaw_dashboard";
    private static final int REQUEST_RECORD_AUDIO = 2001;
    private static final String TAG = "OpenClawDashboard";

    private final OkHttpClient httpClient = new OkHttpClient();
    private SharedPreferences prefs;
    private PermissionRequest pendingPermissionRequest;

    private EditText setupCodeInput;
    private EditText urlInput;
    private EditText tokenInput;
    private EditText passwordInput;
    private TextView statusText;
    private TextView hintText;
    private LinearLayout settingsPanel;
    private WebView webView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        buildUi();
        loadPrefs();
        maybeAutoOpenDashboard();
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }

    private void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.rgb(14, 18, 24));

        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.VERTICAL);
        header.setPadding(dp(18), dp(18), dp(18), dp(10));
        root.addView(header, new LinearLayout.LayoutParams(-1, -2));

        TextView title = text("OpenClaw Control", 26, Color.rgb(236, 242, 248), true);
        header.addView(title);
        statusText = text("Configure the gateway URL and auth, then load the real Control UI.", 14, Color.rgb(150, 164, 178), false);
        header.addView(statusText);

        ScrollView controlsScroll = new ScrollView(this);
        settingsPanel = section();
        controlsScroll.addView(settingsPanel);
        root.addView(controlsScroll, new LinearLayout.LayoutParams(-1, -2));

        LinearLayout controls = settingsPanel;
        setupCodeInput = input("Paste setup code from openclaw qr --json", false, false);
        setupCodeInput.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO);
        controls.addView(label("Setup code"));
        controls.addView(setupCodeInput);
        Button decode = button("Decode setup code");
        controls.addView(decode);

        urlInput = input("http://100.76.133.101:18789/ or ws://...", false, true);
        tokenInput = input("gateway token", false, true);
        tokenInput.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO);
        passwordInput = input("gateway password", true, true);
        passwordInput.setAutofillHints(View.AUTOFILL_HINT_PASSWORD);
        controls.addView(label("Gateway URL"));
        controls.addView(urlInput);
        controls.addView(label("Gateway token"));
        controls.addView(tokenInput);
        controls.addView(label("Password"));
        controls.addView(passwordInput);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        Button open = button("Open UI");
        Button reload = button("Reload");
        Button toggle = button("Hide Setup");
        row.setPadding(0, dp(10), 0, 0);
        row.addView(open, new LinearLayout.LayoutParams(0, dp(46), 1));
        row.addView(reload, new LinearLayout.LayoutParams(0, dp(46), 1));
        row.addView(toggle, new LinearLayout.LayoutParams(0, dp(46), 1));
        controls.addView(row);

        hintText = text(
                "This app now embeds the actual OpenClaw Control UI. If the gateway asks for pairing, approve the pending device request from the host once, then reload.",
                13,
                Color.rgb(132, 145, 159),
                false);
        hintText.setPadding(0, dp(12), 0, 0);
        controls.addView(hintText);

        FrameLayout webContainer = new FrameLayout(this);
        LinearLayout.LayoutParams webLp = new LinearLayout.LayoutParams(-1, 0, 1);
        webLp.setMargins(0, dp(8), 0, 0);
        root.addView(webContainer, webLp);

        webView = new WebView(this);
        webView.setBackgroundColor(Color.rgb(14, 18, 24));
        WebView.setWebContentsDebuggingEnabled(true);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setUseWideViewPort(true);
        settings.setLoadWithOverviewMode(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onConsoleMessage(ConsoleMessage consoleMessage) {
                Log.d(TAG, "console " + consoleMessage.messageLevel() + " " + consoleMessage.sourceId() + ":" + consoleMessage.lineNumber() + " " + consoleMessage.message());
                return super.onConsoleMessage(consoleMessage);
            }

            @Override
            public void onPermissionRequest(PermissionRequest request) {
                runOnUiThread(() -> handleWebPermissionRequest(request));
            }

            @Override
            public void onPermissionRequestCanceled(PermissionRequest request) {
                runOnUiThread(() -> {
                    if (pendingPermissionRequest == request) {
                        pendingPermissionRequest = null;
                    }
                    statusText.setText("Microphone request was canceled.");
                });
            }
        });
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                return interceptControlUiHtml(request);
            }

            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                Log.d(TAG, "page started " + url);
                statusText.setText("Loading Control UI");
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                Log.d(TAG, "page finished " + url);
                statusText.setText("Loaded Control UI");
            }

            @Override
            public void onReceivedError(WebView view, android.webkit.WebResourceRequest request, android.webkit.WebResourceError error) {
                if (request.isForMainFrame()) {
                    String description = error == null ? "unknown error" : String.valueOf(error.getDescription());
                    Log.e(TAG, "page error " + request.getUrl() + " " + description);
                    statusText.setText("Control UI load failed: " + description);
                }
            }

            @Override
            public void onReceivedHttpError(WebView view, android.webkit.WebResourceRequest request, android.webkit.WebResourceResponse errorResponse) {
                if (request.isForMainFrame()) {
                    int statusCode = errorResponse == null ? -1 : errorResponse.getStatusCode();
                    Log.e(TAG, "http error " + request.getUrl() + " " + statusCode);
                    statusText.setText("Control UI load failed: HTTP " + statusCode);
                }
            }
        });
        webContainer.addView(webView, new FrameLayout.LayoutParams(-1, -1));

        decode.setOnClickListener(v -> decodeSetupCode());
        open.setOnClickListener(v -> openDashboard());
        reload.setOnClickListener(v -> webView.reload());
        toggle.setOnClickListener(v -> toggleSettings(toggle));
        setContentView(root);
    }

    private void loadPrefs() {
        urlInput.setText(prefs.getString("url", ""));
        tokenInput.setText(prefs.getString("token", ""));
        passwordInput.setText(prefs.getString("password", ""));
    }

    private void maybeAutoOpenDashboard() {
        String rawUrl = value(urlInput);
        if (rawUrl.isEmpty()) return;
        String dashboardUrl = toDashboardUrl(rawUrl);
        if (!isSecureDashboardUrl(dashboardUrl)) return;
        openDashboard();
    }

    private void savePrefs() {
        prefs.edit()
                .putString("url", value(urlInput))
                .putString("token", value(tokenInput))
                .putString("password", value(passwordInput))
                .apply();
    }

    private void decodeSetupCode() {
        try {
            IdentityStore.Setup setup = IdentityStore.parseSetupCode(value(setupCodeInput));
            String preferredUrl = firstNonEmpty(setup.publicUrl, setup.url);
            urlInput.setText(toDashboardUrl(preferredUrl));
            statusText.setText(isSecureDashboardUrl(toDashboardUrl(preferredUrl))
                    ? "Setup code decoded. Secure dashboard URL loaded."
                    : "Setup code decoded. For Android Talk, use a secure https:// dashboard URL before opening the UI.");
            savePrefs();
        } catch (Exception e) {
            statusText.setText("Setup decode failed: " + e.getMessage());
        }
    }

    private void openDashboard() {
        savePrefs();
        try {
            String dashboardUrl = buildDashboardUrl();
            Log.d(TAG, "opening " + dashboardUrl);
            webView.stopLoading();
            webView.clearCache(true);
            webView.clearHistory();
            WebStorage.getInstance().deleteAllData();
            webView.loadUrl(dashboardUrl);
            statusText.setText("Opening Control UI");
        } catch (Exception e) {
            statusText.setText(e.getMessage());
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != REQUEST_RECORD_AUDIO) return;
        if (hasRecordAudioPermission()) {
            grantPendingAudioCapture();
        } else {
            denyPendingPermissionRequest();
            statusText.setText("Microphone access was denied by Android. Enable it in app permissions and try again.");
        }
    }

    private void handleWebPermissionRequest(PermissionRequest request) {
        if (!requestsAudioCapture(request)) {
            request.deny();
            return;
        }
        if (hasRecordAudioPermission()) {
            grantAudioCapture(request);
            statusText.setText("Microphone access granted.");
            return;
        }
        pendingPermissionRequest = request;
        statusText.setText("OpenClaw needs microphone access for Talk. Approve the Android permission prompt.");
        requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQUEST_RECORD_AUDIO);
    }

    private boolean hasRecordAudioPermission() {
        return checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;
    }

    private boolean requestsAudioCapture(PermissionRequest request) {
        for (String resource : request.getResources()) {
            if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)) {
                return true;
            }
        }
        return false;
    }

    private void grantPendingAudioCapture() {
        PermissionRequest request = pendingPermissionRequest;
        pendingPermissionRequest = null;
        if (request != null) {
            grantAudioCapture(request);
            statusText.setText("Microphone access granted.");
        }
    }

    private void denyPendingPermissionRequest() {
        PermissionRequest request = pendingPermissionRequest;
        pendingPermissionRequest = null;
        if (request != null) {
            request.deny();
        }
    }

    private void grantAudioCapture(PermissionRequest request) {
        List<String> granted = new ArrayList<>();
        for (String resource : request.getResources()) {
            if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)) {
                granted.add(resource);
            }
        }
        if (granted.isEmpty()) {
            request.deny();
            return;
        }
        request.grant(granted.toArray(new String[0]));
    }

    private void toggleSettings(Button toggle) {
        boolean visible = settingsPanel.getVisibility() == View.VISIBLE;
        settingsPanel.setVisibility(visible ? View.GONE : View.VISIBLE);
        hintText.setVisibility(visible ? View.GONE : View.VISIBLE);
        toggle.setText(visible ? "Show Setup" : "Hide Setup");
    }

    private String buildDashboardUrl() {
        String raw = value(urlInput);
        if (raw.isEmpty()) throw new IllegalStateException("Gateway URL is required.");
        String dashboardUrl = toDashboardUrl(raw);
        if (!isSecureDashboardUrl(dashboardUrl)) {
            throw new IllegalStateException("Android Talk requires a secure https:// dashboard URL. Use your Tailscale/MagicDNS hostname instead of the raw ws:// or http:// gateway address.");
        }
        return dashboardUrl;
    }

    private WebResourceResponse interceptControlUiHtml(WebResourceRequest request) {
        try {
            if (request == null || !request.isForMainFrame()) return null;
            if (!"GET".equalsIgnoreCase(request.getMethod())) return null;
            Uri uri = request.getUrl();
            if (uri == null) return null;
            String scheme = uri.getScheme();
            if (!"https".equalsIgnoreCase(scheme) && !"http".equalsIgnoreCase(scheme)) return null;

            Request.Builder builder = new Request.Builder().url(uri.toString()).get();
            Map<String, String> requestHeaders = request.getRequestHeaders();
            if (requestHeaders != null) {
                for (Map.Entry<String, String> entry : requestHeaders.entrySet()) {
                    if (entry.getKey() != null && entry.getValue() != null) {
                        builder.header(entry.getKey(), entry.getValue());
                    }
                }
            }

            Response response = httpClient.newCall(builder.build()).execute();
            if (response.body() == null) {
                response.close();
                return null;
            }
            String contentType = response.header("Content-Type", "");
            if (!contentType.toLowerCase().contains("text/html")) {
                response.close();
                return null;
            }

            String html = response.body().string();
            response.close();
            String injected = injectNativeAuth(html);
            byte[] bytes = injected.getBytes(StandardCharsets.UTF_8);
            InputStream stream = new ByteArrayInputStream(bytes);
            Map<String, String> headers = new HashMap<>();
            headers.put("Cache-Control", "no-store");
            return new WebResourceResponse("text/html", "UTF-8", 200, "OK", headers, stream);
        } catch (Exception e) {
            Log.w(TAG, "html intercept failed: " + e.getMessage());
            return null;
        }
    }

    private String injectNativeAuth(String html) {
        String script = "<script>" + buildNativeAuthScript() + "</script>";
        int headIndex = html.indexOf("<head>");
        if (headIndex >= 0) {
            return html.substring(0, headIndex + 6) + script + html.substring(headIndex + 6);
        }
        int htmlIndex = html.indexOf(">");
        if (htmlIndex >= 0) {
            return html.substring(0, htmlIndex + 1) + script + html.substring(htmlIndex + 1);
        }
        return script + html;
    }

    private String buildNativeAuthScript() {
        String token = value(tokenInput);
        String password = value(passwordInput);
        StringBuilder auth = new StringBuilder();
        auth.append("{\"gatewayUrl\":").append(JSONObject.quote(buildDashboardUrl()));
        if (!token.isEmpty()) auth.append(",\"token\":").append(JSONObject.quote(token));
        if (!password.isEmpty()) auth.append(",\"password\":").append(JSONObject.quote(password));
        auth.append("}");
        return "window.__OPENCLAW_NATIVE_CONTROL_AUTH__=" + auth + ";";
    }

    private static String toDashboardUrl(String raw) {
        String value = raw == null ? "" : raw.trim();
        if (value.isEmpty()) return "";
        if (value.startsWith("ws://")) value = "http://" + value.substring(5);
        else if (value.startsWith("wss://")) value = "https://" + value.substring(6);
        else if (!value.startsWith("http://") && !value.startsWith("https://")) value = "https://" + value;
        if (!value.endsWith("/")) value = value + "/";
        return value;
    }

    private static boolean isSecureDashboardUrl(String raw) {
        try {
            java.net.URI uri = java.net.URI.create(raw);
            String scheme = uri.getScheme();
            String host = uri.getHost();
            if (scheme == null || host == null) return false;
            if ("https".equalsIgnoreCase(scheme)) return true;
            if (!"http".equalsIgnoreCase(scheme)) return false;
            String normalized = host.trim().toLowerCase();
            return "localhost".equals(normalized) || "127.0.0.1".equals(normalized) || "::1".equals(normalized);
        } catch (Exception ignored) {
            return false;
        }
    }

    private static String firstNonEmpty(String... values) {
        for (String value : values) {
            if (value != null && !value.trim().isEmpty()) return value;
        }
        return "";
    }

    private LinearLayout section() {
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(dp(14), dp(14), dp(14), dp(14));
        layout.setBackgroundColor(Color.rgb(20, 26, 34));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-1, -2);
        lp.setMargins(dp(14), 0, dp(14), 0);
        layout.setLayoutParams(lp);
        return layout;
    }

    private TextView label(String value) {
        TextView view = text(value, 12, Color.rgb(130, 144, 158), true);
        view.setPadding(0, dp(8), 0, dp(4));
        return view;
    }

    private EditText input(String hint, boolean password, boolean singleLine) {
        EditText input = new EditText(this);
        input.setHint(hint);
        input.setSingleLine(singleLine);
        input.setMinLines(singleLine ? 1 : 3);
        input.setMaxLines(singleLine ? 1 : 6);
        input.setTextSize(14);
        if (password) input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        else if (singleLine) input.setInputType(InputType.TYPE_CLASS_TEXT);
        else input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE);
        input.setPadding(dp(10), dp(8), dp(10), dp(8));
        input.setTextColor(Color.rgb(234, 240, 246));
        input.setHintTextColor(Color.rgb(111, 124, 137));
        input.setBackgroundColor(Color.rgb(30, 38, 48));
        return input;
    }

    private Button button(String text) {
        Button button = new Button(this);
        button.setText(text);
        button.setAllCaps(false);
        return button;
    }

    private TextView text(String value, int sp, int color, boolean bold) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(sp);
        view.setTextColor(color);
        if (bold) view.setTypeface(android.graphics.Typeface.DEFAULT_BOLD);
        return view;
    }

    private String value(EditText input) {
        return input.getText().toString().trim();
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
