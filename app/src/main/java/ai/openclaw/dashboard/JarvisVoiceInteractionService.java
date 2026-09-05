package ai.openclaw.dashboard;

import android.Manifest;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.service.voice.VoiceInteractionService;
import android.util.Log;

import org.json.JSONObject;

import java.net.URI;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;
import okio.ByteString;

public final class JarvisVoiceInteractionService extends VoiceInteractionService {
    public static final String ACTION_START = "ai.openclaw.dashboard.action.START_JARVIS_WAKE";
    public static final String ACTION_PAUSE = "ai.openclaw.dashboard.action.PAUSE_JARVIS_WAKE";
    private static final String TAG = "JarvisWake";
    private static final String PREFS = "openclaw_dashboard";
    private final AtomicBoolean listening = new AtomicBoolean(false);
    private final OkHttpClient client = new OkHttpClient.Builder().pingInterval(15, TimeUnit.SECONDS).build();
    private AudioRecord recorder;
    private WebSocket socket;
    private Thread captureThread;

    @Override public void onReady() {
        super.onReady();
        if (getSharedPreferences(PREFS, MODE_PRIVATE).getBoolean("jarvis_wake_enabled", false)) startListening();
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_PAUSE.equals(intent.getAction())) stopListening();
        else if (intent != null && ACTION_START.equals(intent.getAction())) startListening();
        return START_STICKY;
    }

    private synchronized void startListening() {
        if (listening.get() || checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) return;
        String wakeUrl = buildWakeUrl();
        if (wakeUrl == null) return;
        int sampleRate = 16000;
        int minimum = AudioRecord.getMinBufferSize(sampleRate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        recorder = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, sampleRate,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, Math.max(minimum * 2, 32000));
        if (recorder.getState() != AudioRecord.STATE_INITIALIZED) { recorder.release(); recorder = null; return; }
        socket = client.newWebSocket(new Request.Builder().url(wakeUrl).build(), new WebSocketListener() {
            @Override public void onMessage(WebSocket webSocket, String text) {
                try {
                    if ("wake".equals(new JSONObject(text).optString("type"))) activateConversation();
                } catch (Exception error) { Log.w(TAG, "Invalid wake response", error); }
            }
            @Override public void onFailure(WebSocket webSocket, Throwable error, Response response) {
                Log.w(TAG, "Wake socket failed", error);
                stopListening();
            }
        });
        recorder.startRecording();
        listening.set(true);
        captureThread = new Thread(() -> {
            byte[] buffer = new byte[3200];
            while (listening.get()) {
                int count = recorder == null ? -1 : recorder.read(buffer, 0, buffer.length);
                WebSocket active = socket;
                if (count > 0 && active != null) active.send(ByteString.of(buffer, 0, count));
            }
        }, "jarvis-wake-capture");
        captureThread.start();
        Log.i(TAG, "Jarvis wake listening started");
    }

    private String buildWakeUrl() {
        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        String configured = prefs.getString("url", "");
        try {
            URI source = URI.create(configured);
            String scheme = "https".equalsIgnoreCase(source.getScheme()) ? "wss" : "ws";
            if (source.getHost() == null) return null;
            return new URI(scheme, null, source.getHost(), 8790, "/wake", null, null).toString();
        } catch (Exception error) {
            Log.w(TAG, "Invalid gateway URL for Jarvis", error);
            return null;
        }
    }

    private void activateConversation() {
        stopListening();
        Intent activity = new Intent(this, MainActivity.class)
                .setAction(MainActivity.ACTION_JARVIS_WAKE)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        startActivity(activity);
    }

    private synchronized void stopListening() {
        listening.set(false);
        if (recorder != null) {
            try { recorder.stop(); } catch (Exception ignored) { }
            recorder.release();
            recorder = null;
        }
        if (socket != null) {
            socket.close(1000, "paused");
            socket = null;
        }
        captureThread = null;
    }

    @Override public void onShutdown() { stopListening(); super.onShutdown(); }
    @Override public void onDestroy() { stopListening(); client.dispatcher().executorService().shutdown(); super.onDestroy(); }
}
