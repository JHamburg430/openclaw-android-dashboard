package ai.openclaw.dashboard;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;

/** Restores the opted-in node connection after boot or application upgrade. */
public final class PhoneNodeBootReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        String action = intent == null ? "" : intent.getAction();
        if (!Intent.ACTION_BOOT_COMPLETED.equals(action) && !Intent.ACTION_MY_PACKAGE_REPLACED.equals(action)) return;
        SharedPreferences prefs = context.getSharedPreferences(PhoneNodeService.PREFS, Context.MODE_PRIVATE);
        if (!prefs.getBoolean("nodeEnabled", false)) return;
        String url = prefs.getString("url", "");
        if (url == null || url.trim().isEmpty()) return;
        Intent service = new Intent(context, PhoneNodeService.class).setAction(PhoneNodeService.ACTION_START);
        context.startForegroundService(service);
    }
}
