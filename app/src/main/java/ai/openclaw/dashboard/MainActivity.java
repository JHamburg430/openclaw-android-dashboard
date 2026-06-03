package ai.openclaw.dashboard;

import android.app.Activity;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

public final class MainActivity extends Activity implements OpenClawClient.Listener {
    private static final String PREFS = "openclaw_dashboard";

    private SharedPreferences prefs;
    private IdentityStore identityStore;
    private OpenClawClient client;

    private EditText setupCodeInput;
    private EditText urlInput;
    private EditText bootstrapInput;
    private EditText tokenInput;
    private EditText passwordInput;
    private EditText displayNameInput;
    private TextView statusText;
    private TextView identityText;
    private TextView metricsText;
    private TextView dashboardText;
    private TextView logText;
    private JSONObject dashboardState = new JSONObject();

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        identityStore = new IdentityStore(this);
        client = new OpenClawClient(identityStore, this);
        buildUi();
        loadPrefs();
        showIdentity();
    }

    @Override
    protected void onDestroy() {
        client.disconnect();
        super.onDestroy();
    }

    private void buildUi() {
        ScrollView scroll = new ScrollView(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(18), dp(14), dp(18), dp(24));
        root.setBackgroundColor(Color.rgb(246, 248, 250));
        scroll.addView(root);

        TextView title = text("OpenClaw Dashboard", 26, Color.rgb(20, 28, 33), true);
        root.addView(title);
        statusText = text("Not connected", 15, Color.rgb(80, 88, 96), false);
        root.addView(statusText);

        LinearLayout controls = section(root);
        setupCodeInput = input("Paste setup code from openclaw qr --json", false);
        controls.addView(label("Setup code"));
        controls.addView(setupCodeInput);
        Button decode = button("Decode setup code");
        controls.addView(decode);

        urlInput = input("wss://gateway.example", false);
        bootstrapInput = input("bootstrap token", false);
        tokenInput = input("gateway/device token", false);
        passwordInput = input("gateway password", true);
        displayNameInput = input("Android OpenClaw Node", false);
        controls.addView(label("Gateway URL"));
        controls.addView(urlInput);
        controls.addView(label("Bootstrap token"));
        controls.addView(bootstrapInput);
        controls.addView(label("Gateway token or saved device token override"));
        controls.addView(tokenInput);
        controls.addView(label("Password"));
        controls.addView(passwordInput);
        controls.addView(label("Node display name"));
        controls.addView(displayNameInput);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        Button connect = button("Connect");
        Button refresh = button("Refresh");
        Button disconnect = button("Disconnect");
        row.addView(connect, new LinearLayout.LayoutParams(0, dp(46), 1));
        row.addView(refresh, new LinearLayout.LayoutParams(0, dp(46), 1));
        row.addView(disconnect, new LinearLayout.LayoutParams(0, dp(46), 1));
        controls.addView(row);

        identityText = card(root, "Node Identity");
        metricsText = card(root, "Dashboard");
        dashboardText = card(root, "Gateway Data");
        logText = card(root, "Log");

        decode.setOnClickListener(v -> decodeSetupCode());
        connect.setOnClickListener(v -> connect());
        refresh.setOnClickListener(v -> client.refreshDashboard());
        disconnect.setOnClickListener(v -> client.disconnect());
        setContentView(scroll);
    }

    private void loadPrefs() {
        urlInput.setText(prefs.getString("url", ""));
        bootstrapInput.setText(prefs.getString("bootstrap", ""));
        tokenInput.setText(prefs.getString("token", ""));
        passwordInput.setText(prefs.getString("password", ""));
        displayNameInput.setText(prefs.getString("displayName", "Android OpenClaw Node"));
    }

    private void savePrefs() {
        prefs.edit()
                .putString("url", value(urlInput))
                .putString("bootstrap", value(bootstrapInput))
                .putString("token", value(tokenInput))
                .putString("password", value(passwordInput))
                .putString("displayName", value(displayNameInput))
                .apply();
    }

    private void decodeSetupCode() {
        try {
            IdentityStore.Setup setup = IdentityStore.parseSetupCode(value(setupCodeInput));
            urlInput.setText(setup.url);
            bootstrapInput.setText(setup.bootstrapToken);
            appendLog("Decoded setup code.");
            savePrefs();
        } catch (Exception e) {
            onError("Setup decode failed: " + e.getMessage());
        }
    }

    private void connect() {
        savePrefs();
        dashboardState = new JSONObject();
        client.connect(new OpenClawClient.Config(
                value(urlInput),
                value(bootstrapInput),
                value(tokenInput),
                value(passwordInput),
                value(displayNameInput)));
    }

    private void showIdentity() {
        try {
            IdentityStore.Identity identity = identityStore.loadOrCreate();
            identityText.setText("Device ID\n" + identity.deviceId + "\n\nPublic key\n" + identity.publicKeyRawBase64Url);
        } catch (Exception e) {
            identityText.setText("Identity unavailable: " + e.getMessage());
        }
    }

    @Override
    public void onStatus(String status) {
        runOnUiThread(() -> statusText.setText(status));
    }

    @Override
    public void onConnected(JSONObject hello) {
        runOnUiThread(() -> {
            appendLog("Connected.");
            renderMetrics();
        });
    }

    @Override
    public void onDashboard(JSONObject dashboard) {
        runOnUiThread(() -> {
            merge(dashboardState, dashboard);
            renderMetrics();
            dashboardText.setText(pretty(dashboardState));
        });
    }

    @Override
    public void onLog(String message) {
        runOnUiThread(() -> appendLog(message));
    }

    @Override
    public void onError(String message) {
        runOnUiThread(() -> appendLog("ERROR: " + message));
    }

    private void renderMetrics() {
        int nodeCount = countArray(dashboardState.optJSONObject("nodes"), "nodes");
        int cronCount = countAnyArray(dashboardState.optJSONObject("cron"));
        JSONObject health = dashboardState.optJSONObject("health");
        String healthState = health == null ? "unknown" : health.optString("status", health.optString("healthState", "available"));
        metricsText.setText(
                "Health: " + healthState +
                        "\nNodes: " + nodeCount +
                        "\nCron entries: " + cronCount +
                        "\nSaved device token: " + (identityStore.getDeviceToken() == null ? "no" : "yes"));
    }

    private static int countArray(JSONObject object, String key) {
        if (object == null) return 0;
        JSONArray array = object.optJSONArray(key);
        return array == null ? 0 : array.length();
    }

    private static int countAnyArray(JSONObject object) {
        if (object == null) return 0;
        JSONArray jobs = object.optJSONArray("jobs");
        if (jobs != null) return jobs.length();
        JSONArray items = object.optJSONArray("items");
        if (items != null) return items.length();
        return object.length() == 0 ? 0 : 1;
    }

    private void appendLog(String message) {
        String current = logText.getText().toString();
        String next = current.isEmpty() || current.equals("Log") ? message : current + "\n" + message;
        logText.setText(next);
    }

    private static void merge(JSONObject target, JSONObject source) {
        if (source == null) return;
        JSONArray names = source.names();
        if (names == null) return;
        for (int i = 0; i < names.length(); i++) {
            String key = names.optString(i);
            try {
                target.put(key, source.opt(key));
            } catch (Exception ignored) {
            }
        }
    }

    private static String pretty(JSONObject value) {
        try {
            return value.toString(2);
        } catch (Exception e) {
            return value.toString();
        }
    }

    private LinearLayout section(LinearLayout root) {
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(dp(14), dp(14), dp(14), dp(14));
        layout.setBackgroundColor(Color.WHITE);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-1, -2);
        lp.setMargins(0, dp(14), 0, dp(10));
        root.addView(layout, lp);
        return layout;
    }

    private TextView card(LinearLayout root, String title) {
        TextView view = text(title, 14, Color.rgb(34, 40, 46), false);
        view.setPadding(dp(14), dp(14), dp(14), dp(14));
        view.setBackgroundColor(Color.WHITE);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-1, -2);
        lp.setMargins(0, dp(8), 0, dp(8));
        root.addView(view, lp);
        return view;
    }

    private TextView label(String value) {
        TextView view = text(value, 12, Color.rgb(74, 82, 90), true);
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
