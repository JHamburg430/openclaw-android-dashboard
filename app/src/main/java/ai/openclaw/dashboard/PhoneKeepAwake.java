package ai.openclaw.dashboard;

import android.annotation.SuppressLint;
import android.content.Context;
import android.os.PowerManager;

/** Owned by the user-enabled foreground phone node, never by an Activity. */
final class PhoneKeepAwake {
    static final String PREF_ENABLED = "keep_screen_awake";
    private final PowerManager power;
    private PowerManager.WakeLock cpu;
    private PowerManager.WakeLock screen;
    private String status = "off";

    PhoneKeepAwake(Context context) {
        power = context.getSystemService(PowerManager.class);
    }

    // A window flag cannot keep other apps awake. This legacy screen level remains
    // supported on Android phones; no ACQUIRE_CAUSES_WAKEUP or lock-screen bypass.
    // Locks intentionally last until opt-out or service stop. Android also releases
    // them on process death. The foreground notification exposes an opt-out action.
    @SuppressWarnings("deprecation")
    @SuppressLint("WakelockTimeout")
    void update(boolean enabled) {
        if (!enabled) {
            release();
            return;
        }
        if (power == null) {
            status = "unavailable";
            return;
        }
        try {
            if (cpu == null) {
                cpu = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "OpenClaw:PhoneNodeCpu");
                cpu.setReferenceCounted(false);
            }
            if (!cpu.isHeld()) cpu.acquire();
            if (!power.isWakeLockLevelSupported(PowerManager.SCREEN_DIM_WAKE_LOCK)) {
                status = "CPU awake; screen keep-awake unsupported";
                return;
            }
            if (!power.isInteractive()) {
                releaseScreen();
                status = "CPU awake; screen manually off";
                return;
            }
            if (screen == null) {
                screen = power.newWakeLock(PowerManager.SCREEN_DIM_WAKE_LOCK, "OpenClaw:PhoneControlScreen");
                screen.setReferenceCounted(false);
            }
            if (!screen.isHeld()) screen.acquire();
            status = "screen and CPU awake across apps";
        } catch (RuntimeException error) {
            release();
            status = "unavailable: " + error.getClass().getSimpleName();
        }
    }

    String status() { return status; }

    private void releaseScreen() {
        if (screen != null && screen.isHeld()) screen.release();
    }

    void release() {
        releaseScreen();
        if (cpu != null && cpu.isHeld()) cpu.release();
        status = "off";
    }
}
