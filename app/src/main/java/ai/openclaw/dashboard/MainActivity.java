package ai.openclaw.dashboard;

import android.Manifest;
import android.app.Activity;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Insets;
import android.graphics.drawable.GradientDrawable;
import android.media.AudioManager;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.media.audiofx.AcousticEchoCanceler;
import android.media.audiofx.AutomaticGainControl;
import android.media.audiofx.NoiseSuppressor;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.util.Log;
import android.util.Base64;
import android.webkit.ConsoleMessage;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.view.WindowInsets;
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
import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.FileReader;
import java.io.IOException;
import java.io.InputStream;
import java.net.InetAddress;
import java.net.UnknownHostException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.ArrayDeque;
import java.util.Collections;
import java.util.Date;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.text.SimpleDateFormat;
import java.util.Locale;
import java.util.concurrent.atomic.AtomicBoolean;

import okhttp3.Dns;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;

public final class MainActivity extends Activity {
    private static final String PREFS = "openclaw_dashboard";
    private static final int REQUEST_RECORD_AUDIO = 2001;
    private static final int REQUEST_POST_NOTIFICATIONS = 2002;
    private static final int REQUEST_CAMERA = 2003;
    private static final int REQUEST_FILE_CHOOSER = 2004;
    private static final String TAG = "OpenClawDashboard";
    private static final int MAX_DIAGNOSTIC_LINES = 120;
    private static final int TALK_FRAME_MS = 10;
    private static final String NOTIFICATION_CHANNEL_ID = "openclaw_updates";
    private static final int NOTIFICATION_ID = 41001;
    private static final String PREF_NOTIFICATION_COUNT = "notification_count";
    private static final int COLOR_APP_CHROME = Color.rgb(6, 10, 16);
    private static final int COLOR_PANEL = Color.rgb(20, 26, 34);
    private static final int COLOR_CONTROL = Color.rgb(30, 38, 48);
    private static final int COLOR_TEXT_PRIMARY = Color.rgb(244, 248, 252);
    private static final int COLOR_TEXT_SECONDARY = Color.rgb(198, 211, 224);
    private static final int COLOR_TEXT_MUTED = Color.rgb(154, 169, 184);
    private static final int COLOR_ACCENT = Color.rgb(0, 171, 126);

    private final OkHttpClient httpClient = new OkHttpClient.Builder()
            .dns(new HostsFileDns())
            .build();
    private final ArrayDeque<String> diagnosticsLines = new ArrayDeque<>();
    private final SimpleDateFormat diagnosticsTimeFormat = new SimpleDateFormat("HH:mm:ss.SSS", Locale.US);
    private final NativeAudioBridge nativeAudioBridge = new NativeAudioBridge();
    private SharedPreferences prefs;
    private PermissionRequest pendingPermissionRequest;
    private ValueCallback<Uri[]> pendingFilePathCallback;

    private EditText setupCodeInput;
    private EditText urlInput;
    private EditText tokenInput;
    private EditText passwordInput;
    private TextView statusText;
    private TextView hintText;
    private TextView diagnosticsText;
    private LinearLayout chromeContainer;
    private LinearLayout settingsPanel;
    private WebView webView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        createNotificationChannel();
        configureSystemBars();
        buildUi();
        loadPrefs();
        ensureNotificationPermission();
        if (!restoreWebViewState(savedInstanceState)) {
            maybeAutoOpenDashboard();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        clearNativeNotifications();
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        if (webView != null) {
            webView.saveState(outState);
        }
        super.onSaveInstanceState(outState);
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
        root.setBackgroundColor(COLOR_APP_CHROME);
        applyStatusBarInset(root);

        chromeContainer = new LinearLayout(this);
        chromeContainer.setOrientation(LinearLayout.VERTICAL);
        chromeContainer.setBackgroundColor(COLOR_APP_CHROME);
        root.addView(chromeContainer, new LinearLayout.LayoutParams(-1, -2));

        statusText = text("Configure the gateway URL and auth, then load the real Control UI.", 14, COLOR_TEXT_SECONDARY, true);
        statusText.setGravity(Gravity.CENTER_VERTICAL);
        statusText.setMinHeight(dp(44));
        statusText.setPadding(dp(14), dp(8), dp(14), dp(6));
        chromeContainer.addView(statusText, new LinearLayout.LayoutParams(-1, -2));

        LinearLayout topActions = new LinearLayout(this);
        topActions.setOrientation(LinearLayout.HORIZONTAL);
        topActions.setGravity(Gravity.CENTER_VERTICAL);
        topActions.setPadding(dp(14), dp(2), dp(14), dp(10));
        topActions.setBackgroundColor(COLOR_APP_CHROME);
        Button open = button("Open UI");
        Button reload = button("Reload");
        Button toggle = button("Hide Setup");
        topActions.addView(open, new LinearLayout.LayoutParams(0, dp(38), 1));
        topActions.addView(reload, new LinearLayout.LayoutParams(0, dp(38), 1));
        topActions.addView(toggle, new LinearLayout.LayoutParams(0, dp(38), 1));
        chromeContainer.addView(topActions, new LinearLayout.LayoutParams(-1, -2));

        ScrollView controlsScroll = new ScrollView(this);
        settingsPanel = section();
        controlsScroll.addView(settingsPanel);
        chromeContainer.addView(controlsScroll, new LinearLayout.LayoutParams(-1, -2));

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

        hintText = text(
                "This app now embeds the actual OpenClaw Control UI. If the gateway asks for pairing, approve the pending device request from the host once, then reload.",
                13,
                COLOR_TEXT_MUTED,
                false);
        hintText.setPadding(0, dp(12), 0, 0);
        controls.addView(hintText);

        controls.addView(label("Diagnostics"));
        LinearLayout diagnosticsActions = new LinearLayout(this);
        diagnosticsActions.setOrientation(LinearLayout.HORIZONTAL);
        diagnosticsActions.setGravity(Gravity.CENTER_VERTICAL);
        Button copyDiagnostics = button("Copy Diagnostics");
        Button clearDiagnostics = button("Clear");
        Button probeMic = button("Probe Mic");
        diagnosticsActions.addView(copyDiagnostics, new LinearLayout.LayoutParams(0, dp(42), 1));
        diagnosticsActions.addView(clearDiagnostics, new LinearLayout.LayoutParams(0, dp(42), 1));
        diagnosticsActions.addView(probeMic, new LinearLayout.LayoutParams(0, dp(42), 1));
        controls.addView(diagnosticsActions);

        diagnosticsText = text("No diagnostics yet.", 12, COLOR_TEXT_SECONDARY, false);
        diagnosticsText.setTextIsSelectable(true);
        diagnosticsText.setPadding(dp(10), dp(8), dp(10), dp(8));
        diagnosticsText.setBackgroundColor(Color.rgb(10, 14, 20));
        controls.addView(diagnosticsText);

        FrameLayout webContainer = new FrameLayout(this);
        LinearLayout.LayoutParams webLp = new LinearLayout.LayoutParams(-1, 0, 1);
        webLp.setMargins(0, dp(4), 0, 0);
        root.addView(webContainer, webLp);

        webView = new WebView(this);
        webView.setBackgroundColor(COLOR_APP_CHROME);
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
        webView.addJavascriptInterface(new DiagnosticsBridge(), "OpenClawDiag");
        webView.addJavascriptInterface(new NativeNotificationsBridge(), "OpenClawNativeNotifications");
        webView.addJavascriptInterface(nativeAudioBridge, "OpenClawNativeAudio");
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onConsoleMessage(ConsoleMessage consoleMessage) {
                recordDiagnostic("console." + consoleMessage.messageLevel().name().toLowerCase(Locale.US),
                        consoleMessage.message());
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
                    statusText.setText("Media permission request was canceled.");
                });
            }

            @Override
            public boolean onShowFileChooser(WebView webView, ValueCallback<Uri[]> filePathCallback, FileChooserParams fileChooserParams) {
                if (pendingFilePathCallback != null) {
                    pendingFilePathCallback.onReceiveValue(null);
                }
                pendingFilePathCallback = filePathCallback;
                Intent intent;
                try {
                    intent = fileChooserParams.createIntent();
                } catch (Exception e) {
                    intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                    intent.addCategory(Intent.CATEGORY_OPENABLE);
                    intent.setType("*/*");
                }
                try {
                    startActivityForResult(intent, REQUEST_FILE_CHOOSER);
                    return true;
                } catch (Exception e) {
                    pendingFilePathCallback = null;
                    statusText.setText("File picker is unavailable on this device.");
                    recordDiagnostic("file_chooser.failed", e.getMessage());
                    return false;
                }
            }
        });
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                return interceptControlUiHtml(request);
            }

            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                recordDiagnostic("page.started", url);
                Log.d(TAG, "page started " + url);
                setConnectedUiVisible(false);
                statusText.setText("Loading Control UI");
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                recordDiagnostic("page.finished", url);
                injectRuntimeScripts(view, "page_finished");
                Log.d(TAG, "page finished " + url);
                setConnectedUiVisible(true);
            }

            @Override
            public void onReceivedError(WebView view, android.webkit.WebResourceRequest request, android.webkit.WebResourceError error) {
                if (request.isForMainFrame()) {
                    String description = error == null ? "unknown error" : String.valueOf(error.getDescription());
                    recordDiagnostic("page.error", description);
                    Log.e(TAG, "page error " + request.getUrl() + " " + description);
                    setConnectedUiVisible(false);
                    statusText.setText("Control UI load failed: " + description);
                }
            }

            @Override
            public void onReceivedHttpError(WebView view, android.webkit.WebResourceRequest request, android.webkit.WebResourceResponse errorResponse) {
                if (request.isForMainFrame()) {
                    int statusCode = errorResponse == null ? -1 : errorResponse.getStatusCode();
                    recordDiagnostic("page.http_error", String.valueOf(statusCode));
                    Log.e(TAG, "http error " + request.getUrl() + " " + statusCode);
                    setConnectedUiVisible(false);
                    statusText.setText("Control UI load failed: HTTP " + statusCode);
                }
            }
        });
        webContainer.addView(webView, new FrameLayout.LayoutParams(-1, -1));

        decode.setOnClickListener(v -> decodeSetupCode());
        open.setOnClickListener(v -> openDashboard());
        reload.setOnClickListener(v -> webView.reload());
        toggle.setOnClickListener(v -> toggleSettings(toggle));
        copyDiagnostics.setOnClickListener(v -> copyDiagnostics());
        clearDiagnostics.setOnClickListener(v -> clearDiagnostics());
        probeMic.setOnClickListener(v -> runNativeMicProbe());
        setContentView(root);
        recordDiagnostic("app.ready", "Diagnostics bridge initialized");
    }

    private void configureSystemBars() {
        Window window = getWindow();
        if (window == null) return;
        window.setStatusBarColor(COLOR_APP_CHROME);
        window.setNavigationBarColor(COLOR_APP_CHROME);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            window.setStatusBarContrastEnforced(false);
            window.setNavigationBarContrastEnforced(false);
        }
        View decorView = window.getDecorView();
        if (decorView == null) return;
        int flags = decorView.getSystemUiVisibility();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            flags &= ~View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            flags &= ~View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
        }
        decorView.setSystemUiVisibility(flags);
    }

    private boolean restoreWebViewState(Bundle savedInstanceState) {
        if (savedInstanceState == null || webView == null) return false;
        webView.restoreState(savedInstanceState);
        String restoredUrl = webView.getUrl();
        if (restoredUrl == null || restoredUrl.trim().isEmpty()) return false;
        recordDiagnostic("webview.restored", restoredUrl);
        setConnectedUiVisible(true);
        return true;
    }

    private void applyStatusBarInset(View view) {
        final int baseLeft = view.getPaddingLeft();
        final int baseTop = view.getPaddingTop();
        final int baseRight = view.getPaddingRight();
        final int baseBottom = view.getPaddingBottom();
        view.setOnApplyWindowInsetsListener((target, insets) -> {
            Insets statusBars = insets.getInsets(WindowInsets.Type.statusBars());
            target.setPadding(
                    baseLeft,
                    baseTop + statusBars.top,
                    baseRight,
                    baseBottom);
            return insets;
        });
        view.requestApplyInsets();
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
        if (!isDashboardUrl(dashboardUrl)) return;
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
            clearDiagnostics();
            recordDiagnostic("open_dashboard", dashboardUrl);
            Log.d(TAG, "opening " + dashboardUrl);
            setConnectedUiVisible(false);
            webView.stopLoading();
            webView.loadUrl(dashboardUrl);
            statusText.setText("Opening Control UI");
        } catch (Exception e) {
            setConnectedUiVisible(false);
            statusText.setText(e.getMessage());
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQUEST_RECORD_AUDIO) {
            if (hasRecordAudioPermission()) {
                grantPendingMediaCapture();
            } else {
                denyPendingPermissionRequest();
                recordDiagnostic("android.permission.denied", "RECORD_AUDIO");
                statusText.setText("Microphone access was denied by Android. Enable it in app permissions and try again.");
            }
            return;
        }
        if (requestCode == REQUEST_CAMERA) {
            if (hasCameraPermission()) {
                grantPendingMediaCapture();
            } else {
                denyPendingPermissionRequest();
                recordDiagnostic("android.permission.denied", "CAMERA");
                statusText.setText("Camera access was denied by Android. Enable it in app permissions and try again.");
            }
            return;
        }
        if (requestCode == REQUEST_POST_NOTIFICATIONS) {
            recordDiagnostic(
                    hasNotificationPermission() ? "android.permission.granted" : "android.permission.denied",
                    "POST_NOTIFICATIONS");
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQUEST_FILE_CHOOSER) {
            ValueCallback<Uri[]> callback = pendingFilePathCallback;
            pendingFilePathCallback = null;
            if (callback != null) {
                callback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data));
            }
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    private void handleWebPermissionRequest(PermissionRequest request) {
        if (!requestsSupportedMediaCapture(request)) {
            recordDiagnostic("web.permission.denied", "Unsupported permission request");
            request.deny();
            return;
        }
        if (canGrantMediaCapture(request)) {
            grantMediaCapture(request);
            recordDiagnostic("android.permission.already_granted", describeRequestedMediaPermissions(request));
            statusText.setText("Media capture access granted.");
            return;
        }
        pendingPermissionRequest = request;
        if (requestsAudioCapture(request) && !hasRecordAudioPermission()) {
            recordDiagnostic("android.permission.requested", "RECORD_AUDIO");
            statusText.setText("OpenClaw needs microphone access for Talk. Approve the Android permission prompt.");
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQUEST_RECORD_AUDIO);
            return;
        }
        if (requestsVideoCapture(request) && !hasCameraPermission()) {
            recordDiagnostic("android.permission.requested", "CAMERA");
            statusText.setText("OpenClaw needs camera access. Approve the Android permission prompt.");
            requestPermissions(new String[]{Manifest.permission.CAMERA}, REQUEST_CAMERA);
        }
    }

    private boolean hasRecordAudioPermission() {
        return checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;
    }

    private boolean hasCameraPermission() {
        return checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED;
    }

    private boolean requestsAudioCapture(PermissionRequest request) {
        for (String resource : request.getResources()) {
            if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)) {
                return true;
            }
        }
        return false;
    }

    private boolean requestsVideoCapture(PermissionRequest request) {
        for (String resource : request.getResources()) {
            if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(resource)) {
                return true;
            }
        }
        return false;
    }

    private boolean requestsSupportedMediaCapture(PermissionRequest request) {
        for (String resource : request.getResources()) {
            if (!PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)
                    && !PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(resource)) {
                return false;
            }
        }
        return request.getResources().length > 0;
    }

    private boolean canGrantMediaCapture(PermissionRequest request) {
        return (!requestsAudioCapture(request) || hasRecordAudioPermission())
                && (!requestsVideoCapture(request) || hasCameraPermission());
    }

    private void grantPendingMediaCapture() {
        PermissionRequest request = pendingPermissionRequest;
        pendingPermissionRequest = null;
        if (request != null) {
            if (canGrantMediaCapture(request)) {
                grantMediaCapture(request);
                recordDiagnostic("android.permission.granted", describeRequestedMediaPermissions(request));
                statusText.setText("Media capture access granted.");
            } else {
                pendingPermissionRequest = request;
                handleWebPermissionRequest(request);
            }
        }
    }

    private void denyPendingPermissionRequest() {
        PermissionRequest request = pendingPermissionRequest;
        pendingPermissionRequest = null;
        if (request != null) {
            request.deny();
        }
    }

    private void grantMediaCapture(PermissionRequest request) {
        List<String> granted = new ArrayList<>();
        for (String resource : request.getResources()) {
            if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)
                    || PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(resource)) {
                granted.add(resource);
            }
        }
        if (granted.isEmpty()) {
            recordDiagnostic("web.permission.denied", "Media capture resources missing");
            request.deny();
            return;
        }
        recordDiagnostic("web.permission.granted", String.join(",", granted));
        request.grant(granted.toArray(new String[0]));
    }

    private String describeRequestedMediaPermissions(PermissionRequest request) {
        List<String> permissions = new ArrayList<>();
        if (requestsAudioCapture(request)) permissions.add("RECORD_AUDIO");
        if (requestsVideoCapture(request)) permissions.add("CAMERA");
        return String.join(",", permissions);
    }

    private void toggleSettings(Button toggle) {
        boolean visible = settingsPanel.getVisibility() == View.VISIBLE;
        settingsPanel.setVisibility(visible ? View.GONE : View.VISIBLE);
        hintText.setVisibility(visible ? View.GONE : View.VISIBLE);
        toggle.setText(visible ? "Show Setup" : "Hide Setup");
    }

    private void setConnectedUiVisible(boolean connected) {
        if (chromeContainer == null || settingsPanel == null || hintText == null) return;
        chromeContainer.setVisibility(View.VISIBLE);
        settingsPanel.setVisibility(connected ? View.GONE : View.VISIBLE);
        hintText.setVisibility(connected ? View.GONE : View.VISIBLE);
        ViewGroup.MarginLayoutParams layoutParams = (ViewGroup.MarginLayoutParams) webView.getLayoutParams();
        if (layoutParams != null) {
            layoutParams.topMargin = connected ? 0 : dp(4);
            webView.setLayoutParams(layoutParams);
        }
    }

    private String buildDashboardUrl() {
        String raw = value(urlInput);
        if (raw.isEmpty()) throw new IllegalStateException("Gateway URL is required.");
        String dashboardUrl = toDashboardUrl(raw);
        if (!isDashboardUrl(dashboardUrl)) {
            throw new IllegalStateException("Gateway URL must be an http:// or https:// dashboard URL.");
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
            recordDiagnostic("html.intercept.failed", e.getMessage());
            Log.w(TAG, "html intercept failed: " + e.getMessage());
            return null;
        }
    }

    private String injectNativeAuth(String html) {
        String script = "<script>" + buildInjectedScript() + "</script>";
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

    private String buildInjectedScript() {
        return buildNativeAuthScript()
                + buildNotificationBridgeScript()
                + buildDiagnosticsScript()
                + buildNativeAudioBridgeScript()
                + buildViewportTighteningScript();
    }

    private String buildNotificationBridgeScript() {
        return "(function(){"
                + "var bridge=window.OpenClawNativeNotifications;"
                + "if(!bridge||typeof bridge.isAvailable!=='function'||!bridge.isAvailable())return;"
                + "if(window.__OPENCLAW_NATIVE_NOTIFICATIONS_PATCHED__)return;"
                + "window.__OPENCLAW_NATIVE_NOTIFICATIONS_PATCHED__=true;"
                + "function permission(){try{return String(bridge.getPermissionStatus()||'default');}catch(_){return 'default';}}"
                + "function notify(title,options){options=options||{};try{bridge.notify(String(title||''),JSON.stringify(options));}catch(_){}}"
                + "function NativeNotification(title,options){"
                + "this.title=String(title||'');"
                + "this.options=options||{};"
                + "this.data=this.options.data;"
                + "this.tag=this.options.tag||'';"
                + "this.body=this.options.body||'';"
                + "notify(this.title,this.options);"
                + "}"
                + "NativeNotification.permission=permission();"
                + "NativeNotification.requestPermission=function(callback){"
                + "var value=permission();"
                + "if(value!=='granted'){value=String(bridge.requestPermission()||value||'default');}"
                + "NativeNotification.permission=value;"
                + "if(typeof callback==='function'){try{callback(value);}catch(_){}}"
                + "return Promise.resolve(value);"
                + "};"
                + "NativeNotification.prototype.close=function(){try{bridge.clearAll();}catch(_){}};"
                + "Object.defineProperty(window,'Notification',{configurable:true,writable:true,value:NativeNotification});"
                + "if(typeof ServiceWorkerRegistration!=='undefined'&&ServiceWorkerRegistration.prototype&&typeof ServiceWorkerRegistration.prototype.showNotification==='function'){"
                + "ServiceWorkerRegistration.prototype.showNotification=function(title,options){notify(title,options);return Promise.resolve();};"
                + "}"
                + "if(navigator&&typeof navigator.setAppBadge==='undefined'){navigator.setAppBadge=function(){return Promise.resolve();};}"
                + "if(navigator&&typeof navigator.clearAppBadge==='undefined'){navigator.clearAppBadge=function(){try{bridge.clearAll();}catch(_){ }return Promise.resolve();};}"
                + "})();";
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

    private String buildDiagnosticsScript() {
        return "(function(){"
                + "var bridge=window.OpenClawDiag;"
                + "function send(kind,payload){try{if(bridge&&typeof bridge.emit==='function'){bridge.emit(String(kind||'diag'),typeof payload==='string'?payload:JSON.stringify(payload||{}));}}catch(_){}}"
                + "window.__OPENCLAW_DIAG__={emit:send};"
                + "send('bootstrap',{href:String(location.href||''),userAgent:String(navigator.userAgent||'')});"
                + "if(window.__OPENCLAW_DIAG_INSTALLED__){return;}"
                + "window.__OPENCLAW_DIAG_INSTALLED__=true;"
                + "window.addEventListener('error',function(event){send('window.error',{message:String(event.message||''),filename:String(event.filename||''),line:event.lineno||0,column:event.colno||0});});"
                + "window.addEventListener('unhandledrejection',function(event){var reason=event&&event.reason;send('window.unhandledrejection',{reason:reason&&reason.message?String(reason.message):String(reason)});});"
                + "var mediaDevices=navigator.mediaDevices;"
                + "if(mediaDevices&&typeof mediaDevices.getUserMedia==='function'){"
                + "var originalGetUserMedia=mediaDevices.getUserMedia.bind(mediaDevices);"
                + "mediaDevices.getUserMedia=function(constraints){send('gum.request',constraints||{});return originalGetUserMedia(constraints).then(function(stream){var track=stream&&stream.getAudioTracks&&stream.getAudioTracks()[0];send('gum.success',{trackState:track&&track.readyState?String(track.readyState):'unknown',sampleRate:track&&track.getSettings&&track.getSettings().sampleRate||null});return stream;}).catch(function(error){send('gum.error',{name:error&&error.name?String(error.name):'',message:error&&error.message?String(error.message):String(error)});throw error;});};"
                + "}"
                + "var AudioContextCtor=window.AudioContext||window.webkitAudioContext;"
                + "if(AudioContextCtor&& !window.__OPENCLAW_DIAG_AUDIO_PATCHED__){"
                + "window.__OPENCLAW_DIAG_AUDIO_PATCHED__=true;"
                + "function WrappedAudioContext(){var args=Array.prototype.slice.call(arguments);var ctx=new (Function.prototype.bind.apply(AudioContextCtor,[null].concat(args)))();send('audio.context.created',{state:String(ctx.state||''),sampleRate:ctx.sampleRate||null});"
                + "var originalResume=ctx.resume&&ctx.resume.bind(ctx);if(originalResume){ctx.resume=function(){send('audio.context.resume.request',{state:String(ctx.state||'')});return originalResume().then(function(result){send('audio.context.resume.success',{state:String(ctx.state||'')});return result;}).catch(function(error){send('audio.context.resume.error',{message:error&&error.message?String(error.message):String(error)});throw error;});};}"
                + "var originalCreateMediaStreamSource=ctx.createMediaStreamSource&&ctx.createMediaStreamSource.bind(ctx);if(originalCreateMediaStreamSource){ctx.createMediaStreamSource=function(stream){send('audio.media_stream_source.request',{});try{var node=originalCreateMediaStreamSource(stream);send('audio.media_stream_source.success',{});return node;}catch(error){send('audio.media_stream_source.error',{message:error&&error.message?String(error.message):String(error)});throw error;}};}"
                + "var originalCreateScriptProcessor=ctx.createScriptProcessor&&ctx.createScriptProcessor.bind(ctx);if(originalCreateScriptProcessor){ctx.createScriptProcessor=function(){var node=originalCreateScriptProcessor.apply(ctx,arguments);send('audio.script_processor.created',{bufferSize:arguments[0]||null});var seen=false;var previous=node.onaudioprocess;Object.defineProperty(node,'onaudioprocess',{configurable:true,enumerable:true,get:function(){return previous;},set:function(handler){previous=function(event){if(!seen){seen=true;send('audio.script_processor.first_frame',{inputChannels:event&&event.inputBuffer?event.inputBuffer.numberOfChannels:null,length:event&&event.inputBuffer?event.inputBuffer.length:null});}return handler&&handler.call(this,event);};}});return node;};}"
                + "return ctx;}"
                + "WrappedAudioContext.prototype=AudioContextCtor.prototype;"
                + "window.AudioContext=WrappedAudioContext;"
                + "if(window.webkitAudioContext){window.webkitAudioContext=WrappedAudioContext;}"
                + "}"
                + "var levels=['debug','log','info','warn','error'];"
                + "for(var i=0;i<levels.length;i++){(function(level){var original=console[level];if(typeof original!=='function')return;console[level]=function(){var parts=[];for(var j=0;j<arguments.length;j++){var value=arguments[j];if(typeof value==='string'){parts.push(value);}else{try{parts.push(JSON.stringify(value));}catch(_){parts.push(String(value));}}}send('console.'+level,parts.join(' '));return original.apply(console,arguments);};})(levels[i]);}"
                + "})();";
    }

    private String buildNativeAudioBridgeScript() {
        return "(function(){"
                + "var bridge=window.OpenClawNativeAudio;"
                + "var diag=window.__OPENCLAW_DIAG__&&typeof window.__OPENCLAW_DIAG__.emit==='function'?window.__OPENCLAW_DIAG__.emit:function(){};"
                + "if(!bridge||typeof bridge.isAvailable!=='function'||!bridge.isAvailable())return;"
                + "if(window.__OPENCLAW_NATIVE_AUDIO_PATCHED__)return;"
                + "window.__OPENCLAW_NATIVE_AUDIO_PATCHED__=true;"
                + "diag('native_audio.patch',{'status':'enabled'});"
                + "function decodePcm16(base64){var binary=atob(base64||'');var bytes=binary.length;var samples=new Float32Array(bytes/2);for(var i=0,j=0;i<bytes;i+=2,j++){var value=(binary.charCodeAt(i)&255)|((binary.charCodeAt(i+1)&255)<<8);if(value&32768)value-=65536;samples[j]=Math.max(-1,Math.min(1,value/32768));}return samples;}"
                + "function resampleLinear(input,fromRate,toRate){if(!input||!input.length||!fromRate||!toRate||fromRate===toRate)return input;var ratio=toRate/fromRate;var outLength=Math.max(1,Math.round(input.length*ratio));var output=new Float32Array(outLength);for(var i=0;i<outLength;i++){var position=i/ratio;var index=Math.floor(position);var next=Math.min(index+1,input.length-1);var weight=position-index;output[i]=input[index]+(input[next]-input[index])*weight;}return output;}"
                + "var mediaDevices=navigator.mediaDevices||(navigator.mediaDevices={});"
                + "var nativeStreamFactory=function(){return Promise.resolve({__openclawNativeAudio:true,getAudioTracks:function(){return[{readyState:'live',stop:function(){try{bridge.stopCapture();}catch(_){}}}];},getTracks:function(){return this.getAudioTracks();}});};"
                + "mediaDevices.getUserMedia=function(constraints){diag('native_audio.gum_override',constraints||{});return nativeStreamFactory();};"
                + "var AudioContextCtor=window.AudioContext||window.webkitAudioContext;"
                + "if(!AudioContextCtor)return;"
                + "var originalCreateMediaStreamSource=AudioContextCtor.prototype.createMediaStreamSource;"
                + "var originalCreateScriptProcessor=AudioContextCtor.prototype.createScriptProcessor;"
                + "AudioContextCtor.prototype.createMediaStreamSource=function(stream){if(!stream||!stream.__openclawNativeAudio)return originalCreateMediaStreamSource.call(this,stream);var ctx=this;return{connect:function(node){node&&typeof node.__openclawNativeAttach==='function'&&node.__openclawNativeAttach(ctx);},disconnect:function(){try{bridge.stopCapture();}catch(_){}}};};"
                + "AudioContextCtor.prototype.createScriptProcessor=function(bufferSize,inputChannels,outputChannels){var node=originalCreateScriptProcessor.call(this,bufferSize,inputChannels,outputChannels);var originalConnect=node.connect?node.connect.bind(node):null;var originalDisconnect=node.disconnect?node.disconnect.bind(node):null;node.__openclawNativeTimer=null;node.__openclawNativeCtx=null;node.__openclawNativeAttach=function(ctx){node.__openclawNativeCtx=ctx;try{bridge.startCapture(16000,10);diag('native_audio.capture.start',{'bufferSize':bufferSize||0,'ctxSampleRate':ctx&&ctx.sampleRate||null});}catch(error){diag('native_audio.capture.error',{'message':String(error)});}if(node.__openclawNativeTimer)return;node.__openclawNativeTimer=window.setInterval(function(){if(typeof node.onaudioprocess!=='function')return;var chunk='';try{chunk=bridge.readChunkBase64()||'';}catch(error){diag('native_audio.read.error',{'message':String(error)});return;}if(!chunk)return;var floats=decodePcm16(chunk);var targetRate=node.__openclawNativeCtx&&node.__openclawNativeCtx.sampleRate?node.__openclawNativeCtx.sampleRate:16000;var resampled=resampleLinear(floats,16000,targetRate);var inputBuffer={length:resampled.length,numberOfChannels:1,getChannelData:function(){return resampled;}};node.onaudioprocess({inputBuffer:inputBuffer});},10);};"
                + "node.disconnect=function(){if(node.__openclawNativeTimer){window.clearInterval(node.__openclawNativeTimer);node.__openclawNativeTimer=null;}try{bridge.stopCapture();diag('native_audio.capture.stop',{});}catch(_){ }return originalDisconnect?originalDisconnect():void 0;};"
                + "node.connect=function(){return originalConnect?originalConnect.apply(node,arguments):void 0;};"
                + "return node;};"
                + "})();";
    }

    private String buildViewportTighteningScript() {
        return "(function(){"
                + "function apply(){"
                + "try{"
                + "var root=document.documentElement;"
                + "if(root){root.style.setProperty('--safe-area-bottom','0px','important');root.style.setProperty('--safe-area-top','0px','important');}"
                + "var body=document.body;"
                + "if(body){body.style.paddingBottom='0px';body.style.marginBottom='0px';body.style.minHeight='100dvh';}"
                + "var app=document.querySelector('openclaw-app');"
                + "if(app){app.style.height='100dvh';app.style.minHeight='100dvh';app.style.paddingBottom='0px';app.style.marginBottom='0px';}"
                + "var style=document.getElementById('openclaw-android-tighten');"
                + "if(!style){style=document.createElement('style');style.id='openclaw-android-tighten';style.textContent='html,body,openclaw-app,.shell,.shell--chat{height:100dvh!important;min-height:100dvh!important;max-height:100dvh!important;}body,openclaw-app{padding-bottom:0!important;margin-bottom:0!important;}[class*=composer],[class*=chat]{padding-bottom:0!important;margin-bottom:0!important;}';document.head&&document.head.appendChild(style);}"
                + "}catch(_){}}"
                + "if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',apply,{once:true});}"
                + "apply();"
                + "})();";
    }

    private void injectRuntimeScripts(WebView view, String reason) {
        if (view == null) return;
        String script = buildInjectedScript();
        recordDiagnostic("js.inject.request", reason);
        view.post(() -> view.evaluateJavascript(script, value ->
                recordDiagnostic("js.inject.result", reason + " " + String.valueOf(value))));
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

    private static boolean isDashboardUrl(String raw) {
        try {
            java.net.URI uri = java.net.URI.create(raw);
            String scheme = uri.getScheme();
            String host = uri.getHost();
            if (scheme == null || host == null) return false;
            return "https".equalsIgnoreCase(scheme) || "http".equalsIgnoreCase(scheme);
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

    private void recordDiagnostic(String kind, String message) {
        String safeMessage = message == null ? "" : message;
        String line = diagnosticsTimeFormat.format(new Date()) + "  " + kind + "  " + safeMessage;
        Log.d(TAG, "diag " + line);
        runOnUiThread(() -> {
            diagnosticsLines.addLast(line);
            while (diagnosticsLines.size() > MAX_DIAGNOSTIC_LINES) {
                diagnosticsLines.removeFirst();
            }
            StringBuilder builder = new StringBuilder();
            for (String entry : diagnosticsLines) {
                if (builder.length() > 0) builder.append('\n');
                builder.append(entry);
            }
            diagnosticsText.setText(builder.length() == 0 ? "No diagnostics yet." : builder.toString());
        });
    }

    private void clearDiagnostics() {
        diagnosticsLines.clear();
        diagnosticsText.setText("No diagnostics yet.");
    }

    private void copyDiagnostics() {
        ClipboardManager clipboard = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
        if (clipboard == null) return;
        CharSequence text = diagnosticsText.getText();
        clipboard.setPrimaryClip(ClipData.newPlainText("OpenClaw diagnostics", text));
        statusText.setText("Diagnostics copied.");
    }

    private void runNativeMicProbe() {
        if (!hasRecordAudioPermission()) {
            recordDiagnostic("native_mic_probe.skipped", "RECORD_AUDIO permission missing");
            statusText.setText("Grant microphone permission first.");
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQUEST_RECORD_AUDIO);
            return;
        }
        statusText.setText("Running native mic probe");
        recordDiagnostic("native_mic_probe.start", "AudioRecord");
        new Thread(() -> {
            AudioRecord recorder = null;
            try {
                int sampleRate = 16000;
                int channelConfig = AudioFormat.CHANNEL_IN_MONO;
                int encoding = AudioFormat.ENCODING_PCM_16BIT;
                int minBuffer = AudioRecord.getMinBufferSize(sampleRate, channelConfig, encoding);
                if (minBuffer <= 0) {
                    throw new IllegalStateException("getMinBufferSize returned " + minBuffer);
                }
                int bufferSize = Math.max(minBuffer, sampleRate);
                recordDiagnostic("native_mic_probe.config",
                        "sampleRate=" + sampleRate + " minBuffer=" + minBuffer + " bufferSize=" + bufferSize);
                recorder = new AudioRecord(
                        MediaRecorder.AudioSource.MIC,
                        sampleRate,
                        channelConfig,
                        encoding,
                        bufferSize);
                recordDiagnostic("native_mic_probe.record_state",
                        "state=" + recorder.getState() + " sessionId=" + recorder.getAudioSessionId());
                if (recorder.getState() != AudioRecord.STATE_INITIALIZED) {
                    throw new IllegalStateException("AudioRecord failed to initialize: state=" + recorder.getState());
                }
                recorder.startRecording();
                recordDiagnostic("native_mic_probe.recording_state",
                        "state=" + recorder.getRecordingState());
                if (recorder.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) {
                    throw new IllegalStateException("AudioRecord failed to start: state=" + recorder.getRecordingState());
                }
                byte[] buffer = new byte[bufferSize];
                int bytesRead = recorder.read(buffer, 0, buffer.length);
                recordDiagnostic("native_mic_probe.read",
                        "bytesRead=" + bytesRead);
                if (bytesRead <= 0) {
                    throw new IllegalStateException("AudioRecord read failed: " + bytesRead);
                }
                runOnUiThread(() -> statusText.setText("Native mic probe passed."));
                recordDiagnostic("native_mic_probe.success", "AudioRecord captured audio");
            } catch (Exception e) {
                recordDiagnostic("native_mic_probe.error",
                        e.getClass().getSimpleName() + ": " + e.getMessage());
                runOnUiThread(() -> statusText.setText("Native mic probe failed: " + e.getMessage()));
            } finally {
                if (recorder != null) {
                    try {
                        recorder.stop();
                    } catch (Exception ignored) {
                    }
                    recorder.release();
                }
            }
        }, "openclaw-native-mic-probe").start();
    }

    private final class DiagnosticsBridge {
        @JavascriptInterface
        public void emit(String kind, String payload) {
            recordDiagnostic("js." + kind, payload);
        }
    }

    private final class NativeNotificationsBridge {
        @JavascriptInterface
        public boolean isAvailable() {
            return true;
        }

        @JavascriptInterface
        public String getPermissionStatus() {
            return hasNotificationPermission() ? "granted" : "default";
        }

        @JavascriptInterface
        public String requestPermission() {
            ensureNotificationPermission();
            return getPermissionStatus();
        }

        @JavascriptInterface
        public void notify(String title, String optionsJson) {
            runOnUiThread(() -> postNativeNotification(title, optionsJson));
        }

        @JavascriptInterface
        public void clearAll() {
            runOnUiThread(MainActivity.this::clearNativeNotifications);
        }
    }

    private final class NativeAudioBridge {
        private static final int NATIVE_SAMPLE_RATE = 16000;
        private static final int MAX_QUEUED_CHUNKS = 64;
        private final Object lock = new Object();
        private final ArrayDeque<String> chunkQueue = new ArrayDeque<>();
        private final AtomicBoolean running = new AtomicBoolean(false);
        private AudioRecord recorder;
        private Thread readerThread;
        private Integer previousAudioMode;
        private AcousticEchoCanceler acousticEchoCanceler;
        private NoiseSuppressor noiseSuppressor;
        private AutomaticGainControl automaticGainControl;

        @JavascriptInterface
        public boolean isAvailable() {
            return true;
        }

        @JavascriptInterface
        public void startCapture(int sampleRateHz, int frameMs) {
            synchronized (lock) {
                stopCaptureLocked();
                int requestedFrameMs = frameMs > 0 ? Math.max(TALK_FRAME_MS, frameMs) : TALK_FRAME_MS;
                int samplesPerChunk = Math.max(160, NATIVE_SAMPLE_RATE * requestedFrameMs / 1000);
                int minBuffer = AudioRecord.getMinBufferSize(
                        NATIVE_SAMPLE_RATE,
                        AudioFormat.CHANNEL_IN_MONO,
                        AudioFormat.ENCODING_PCM_16BIT);
                if (minBuffer <= 0) {
                    recordDiagnostic("native_audio_bridge.error", "getMinBufferSize=" + minBuffer);
                    return;
                }
                int bufferSize = Math.max(minBuffer, samplesPerChunk * 2);
                setCommunicationAudioMode();
                recorder = new AudioRecord(
                        MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                        NATIVE_SAMPLE_RATE,
                        AudioFormat.CHANNEL_IN_MONO,
                        AudioFormat.ENCODING_PCM_16BIT,
                        bufferSize);
                if (recorder.getState() != AudioRecord.STATE_INITIALIZED) {
                    recordDiagnostic("native_audio_bridge.error", "AudioRecord state=" + recorder.getState());
                    recorder.release();
                    recorder = null;
                    restoreAudioMode();
                    return;
                }
                enableVoiceProcessingEffects(recorder.getAudioSessionId());
                recorder.startRecording();
                running.set(true);
                recordDiagnostic("native_audio_bridge.start",
                        "sampleRate=" + sampleRateHz
                                + " nativeRate=" + NATIVE_SAMPLE_RATE
                                + " frameMs=" + requestedFrameMs
                                + " source=VOICE_COMMUNICATION");
                readerThread = new Thread(() -> readNativeAudioLoop(samplesPerChunk), "openclaw-native-audio");
                readerThread.start();
            }
        }

        @JavascriptInterface
        public void stopCapture() {
            synchronized (lock) {
                stopCaptureLocked();
            }
        }

        @JavascriptInterface
        public String readChunkBase64() {
            synchronized (lock) {
                return chunkQueue.pollFirst();
            }
        }

        private void readNativeAudioLoop(int samplesPerChunk) {
            byte[] buffer = new byte[samplesPerChunk * 2];
            while (running.get()) {
                AudioRecord activeRecorder;
                synchronized (lock) {
                    activeRecorder = recorder;
                }
                if (activeRecorder == null) return;
                int bytesRead = activeRecorder.read(buffer, 0, buffer.length);
                if (bytesRead <= 0) {
                    recordDiagnostic("native_audio_bridge.read_error", "bytesRead=" + bytesRead);
                    continue;
                }
                String encoded = Base64.encodeToString(copyOf(buffer, bytesRead), Base64.NO_WRAP);
                synchronized (lock) {
                    chunkQueue.addLast(encoded);
                    while (chunkQueue.size() > MAX_QUEUED_CHUNKS) {
                        chunkQueue.removeFirst();
                    }
                }
            }
        }

        private byte[] copyOf(byte[] source, int length) {
            ByteArrayOutputStream out = new ByteArrayOutputStream(length);
            out.write(source, 0, length);
            return out.toByteArray();
        }

        private void stopCaptureLocked() {
            running.set(false);
            chunkQueue.clear();
            releaseVoiceProcessingEffects();
            if (recorder != null) {
                try {
                    recorder.stop();
                } catch (Exception ignored) {
                }
                recorder.release();
                recorder = null;
            }
            if (readerThread != null) {
                try {
                    readerThread.join(100);
                } catch (InterruptedException ignored) {
                    Thread.currentThread().interrupt();
                }
                readerThread = null;
            }
            restoreAudioMode();
            recordDiagnostic("native_audio_bridge.stop", "stopped");
        }

        private void setCommunicationAudioMode() {
            AudioManager audioManager = getAudioManager();
            if (audioManager == null) return;
            if (previousAudioMode == null) {
                previousAudioMode = audioManager.getMode();
            }
            audioManager.setMode(AudioManager.MODE_IN_COMMUNICATION);
            recordDiagnostic("native_audio_bridge.audio_mode", "mode=MODE_IN_COMMUNICATION");
        }

        private void restoreAudioMode() {
            AudioManager audioManager = getAudioManager();
            if (audioManager == null || previousAudioMode == null) return;
            audioManager.setMode(previousAudioMode);
            recordDiagnostic("native_audio_bridge.audio_mode", "mode=" + previousAudioMode);
            previousAudioMode = null;
        }

        private AudioManager getAudioManager() {
            return (AudioManager) MainActivity.this.getSystemService(Context.AUDIO_SERVICE);
        }

        private void enableVoiceProcessingEffects(int audioSessionId) {
            acousticEchoCanceler = enableAcousticEchoCanceler(audioSessionId);
            noiseSuppressor = enableNoiseSuppressor(audioSessionId);
            automaticGainControl = enableAutomaticGainControl(audioSessionId);
        }

        private AcousticEchoCanceler enableAcousticEchoCanceler(int audioSessionId) {
            if (!AcousticEchoCanceler.isAvailable()) {
                recordDiagnostic("native_audio_bridge.effect", "aec=unavailable");
                return null;
            }
            AcousticEchoCanceler effect = AcousticEchoCanceler.create(audioSessionId);
            if (effect == null) {
                recordDiagnostic("native_audio_bridge.effect", "aec=create_failed");
                return null;
            }
            effect.setEnabled(true);
            recordDiagnostic("native_audio_bridge.effect", "aec=" + effect.getEnabled());
            return effect;
        }

        private NoiseSuppressor enableNoiseSuppressor(int audioSessionId) {
            if (!NoiseSuppressor.isAvailable()) {
                recordDiagnostic("native_audio_bridge.effect", "ns=unavailable");
                return null;
            }
            NoiseSuppressor effect = NoiseSuppressor.create(audioSessionId);
            if (effect == null) {
                recordDiagnostic("native_audio_bridge.effect", "ns=create_failed");
                return null;
            }
            effect.setEnabled(true);
            recordDiagnostic("native_audio_bridge.effect", "ns=" + effect.getEnabled());
            return effect;
        }

        private AutomaticGainControl enableAutomaticGainControl(int audioSessionId) {
            if (!AutomaticGainControl.isAvailable()) {
                recordDiagnostic("native_audio_bridge.effect", "agc=unavailable");
                return null;
            }
            AutomaticGainControl effect = AutomaticGainControl.create(audioSessionId);
            if (effect == null) {
                recordDiagnostic("native_audio_bridge.effect", "agc=create_failed");
                return null;
            }
            effect.setEnabled(true);
            recordDiagnostic("native_audio_bridge.effect", "agc=" + effect.getEnabled());
            return effect;
        }

        private void releaseVoiceProcessingEffects() {
            if (acousticEchoCanceler != null) {
                acousticEchoCanceler.release();
                acousticEchoCanceler = null;
            }
            if (noiseSuppressor != null) {
                noiseSuppressor.release();
                noiseSuppressor = null;
            }
            if (automaticGainControl != null) {
                automaticGainControl.release();
                automaticGainControl = null;
            }
        }
    }

    private boolean hasNotificationPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return true;
        return checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
    }

    private void ensureNotificationPermission() {
        if (hasNotificationPermission()) return;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQUEST_POST_NOTIFICATIONS);
        }
    }

    private void createNotificationChannel() {
        NotificationManager manager = getNotificationManager();
        if (manager == null || Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationChannel channel = new NotificationChannel(
                NOTIFICATION_CHANNEL_ID,
                "OpenClaw updates",
                NotificationManager.IMPORTANCE_DEFAULT);
        channel.setDescription("Notifications from the OpenClaw dashboard");
        channel.setShowBadge(true);
        manager.createNotificationChannel(channel);
    }

    private void postNativeNotification(String title, String optionsJson) {
        if (!hasNotificationPermission()) {
            ensureNotificationPermission();
            recordDiagnostic("native_notification.skipped", "POST_NOTIFICATIONS permission missing");
            return;
        }
        String safeTitle = title == null || title.trim().isEmpty() ? "OpenClaw" : title.trim();
        String body = "";
        try {
            if (optionsJson != null && !optionsJson.isEmpty()) {
                JSONObject options = new JSONObject(optionsJson);
                body = options.optString("body", "");
            }
        } catch (Exception e) {
            recordDiagnostic("native_notification.parse_error", e.getMessage());
        }
        int count = prefs.getInt(PREF_NOTIFICATION_COUNT, 0) + 1;
        prefs.edit().putInt(PREF_NOTIFICATION_COUNT, count).apply();
        NotificationManager manager = getNotificationManager();
        if (manager == null) return;
        Intent intent = new Intent(this, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent contentIntent = PendingIntent.getActivity(
                this,
                0,
                intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification notification = new Notification.Builder(this, NOTIFICATION_CHANNEL_ID)
                .setSmallIcon(android.R.drawable.stat_notify_more)
                .setContentTitle(safeTitle)
                .setContentText(body.isEmpty() ? "OpenClaw has a new update." : body)
                .setStyle(new Notification.BigTextStyle().bigText(body.isEmpty() ? safeTitle : body))
                .setAutoCancel(true)
                .setContentIntent(contentIntent)
                .setNumber(count)
                .setBadgeIconType(Notification.BADGE_ICON_SMALL)
                .build();
        manager.notify(NOTIFICATION_ID, notification);
        recordDiagnostic("native_notification.posted", safeTitle + " count=" + count);
    }

    private void clearNativeNotifications() {
        prefs.edit().putInt(PREF_NOTIFICATION_COUNT, 0).apply();
        NotificationManager manager = getNotificationManager();
        if (manager != null) {
            manager.cancel(NOTIFICATION_ID);
        }
        recordDiagnostic("native_notification.cleared", "count=0");
    }

    private NotificationManager getNotificationManager() {
        return (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
    }

    private static final class HostsFileDns implements Dns {
        @Override
        public List<InetAddress> lookup(String hostname) throws UnknownHostException {
            try {
                return Dns.SYSTEM.lookup(hostname);
            } catch (UnknownHostException firstError) {
                InetAddress mapped = lookupHostsFile(hostname);
                if (mapped != null) {
                    return Collections.singletonList(mapped);
                }
                throw firstError;
            }
        }

        private InetAddress lookupHostsFile(String hostname) {
            try (BufferedReader reader = new BufferedReader(new FileReader("/etc/hosts"))) {
                String normalizedHost = hostname == null ? "" : hostname.trim().toLowerCase();
                String line;
                while ((line = reader.readLine()) != null) {
                    String trimmed = line.trim();
                    if (trimmed.isEmpty() || trimmed.startsWith("#")) continue;
                    int commentIndex = trimmed.indexOf('#');
                    if (commentIndex >= 0) {
                        trimmed = trimmed.substring(0, commentIndex).trim();
                    }
                    String[] parts = trimmed.split("\\s+");
                    if (parts.length < 2) continue;
                    for (int i = 1; i < parts.length; i++) {
                        if (normalizedHost.equals(parts[i].trim().toLowerCase())) {
                            return InetAddress.getByName(parts[0]);
                        }
                    }
                }
            } catch (IOException ignored) {
                return null;
            }
            return null;
        }
    }

    private LinearLayout section() {
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(dp(14), dp(10), dp(14), dp(10));
        layout.setBackgroundColor(COLOR_PANEL);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-1, -2);
        lp.setMargins(dp(14), dp(4), dp(14), 0);
        layout.setLayoutParams(lp);
        return layout;
    }

    private TextView label(String value) {
        TextView view = text(value, 12, COLOR_TEXT_MUTED, true);
        view.setPadding(0, dp(6), 0, dp(3));
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
        input.setTextColor(COLOR_TEXT_PRIMARY);
        input.setHintTextColor(COLOR_TEXT_MUTED);
        input.setBackgroundColor(COLOR_CONTROL);
        return input;
    }

    private Button button(String text) {
        Button button = new Button(this);
        button.setText(text);
        button.setAllCaps(false);
        button.setTextColor(COLOR_TEXT_PRIMARY);
        button.setTextSize(13);
        button.setPadding(dp(8), 0, dp(8), 0);
        GradientDrawable background = new GradientDrawable();
        background.setColor(COLOR_CONTROL);
        background.setCornerRadius(dp(6));
        background.setStroke(dp(1), COLOR_ACCENT);
        button.setBackground(background);
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
