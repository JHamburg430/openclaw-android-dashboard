package ai.openclaw.dashboard;

import android.app.Activity;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Bundle;
import android.text.InputType;
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

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;

public final class MainActivity extends Activity {
    private static final String PREFS = "openclaw_dashboard";

    private SharedPreferences prefs;

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
        setupCodeInput = input("Paste setup code from openclaw qr --json", false);
        controls.addView(label("Setup code"));
        controls.addView(setupCodeInput);
        Button decode = button("Decode setup code");
        controls.addView(decode);

        urlInput = input("http://100.76.133.101:18789/ or ws://...", false);
        tokenInput = input("gateway token", false);
        passwordInput = input("gateway password", true);
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
        webView.setWebChromeClient(new WebChromeClient());
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                statusText.setText("Loaded Control UI");
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
            urlInput.setText(toDashboardUrl(setup.url));
            statusText.setText("Setup code decoded. Add a gateway token or password, then open the UI.");
            savePrefs();
        } catch (Exception e) {
            statusText.setText("Setup decode failed: " + e.getMessage());
        }
    }

    private void openDashboard() {
        savePrefs();
        try {
            String dashboardUrl = buildDashboardUrl();
            webView.loadUrl(dashboardUrl);
            statusText.setText("Opening Control UI");
        } catch (Exception e) {
            statusText.setText(e.getMessage());
        }
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
        String secret = value(tokenInput).isEmpty() ? value(passwordInput) : value(tokenInput);
        if (secret.isEmpty()) {
            return dashboardUrl;
        }
        String encoded = URLEncoder.encode(secret, StandardCharsets.UTF_8);
        String separator = dashboardUrl.contains("?") ? "&" : "?";
        return dashboardUrl + separator + "token=" + encoded + "#token=" + encoded;
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

    private EditText input(String hint, boolean password) {
        EditText input = new EditText(this);
        input.setHint(hint);
        input.setSingleLine(false);
        input.setMinLines(1);
        input.setTextSize(14);
        input.setInputType(password ? InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD : InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE);
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
