package ai.openclaw.dashboard;

import android.app.Notification;
import android.app.PendingIntent;
import android.app.RemoteInput;
import android.content.Intent;
import android.os.Bundle;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

import org.json.JSONArray;
import org.json.JSONObject;

public final class PhoneNotificationListenerService extends NotificationListenerService {
    private static volatile PhoneNotificationListenerService instance;

    @Override public void onListenerConnected() { instance = this; }
    @Override public void onListenerDisconnected() { if (instance == this) instance = null; }
    @Override public void onDestroy() { if (instance == this) instance = null; super.onDestroy(); }

    static boolean isConnected() { return instance != null; }

    static JSONObject list(int limit, String packageFilter) throws Exception {
        PhoneNotificationListenerService service = requireService();
        JSONArray items = new JSONArray();
        StatusBarNotification[] active = service.getActiveNotifications();
        if (active != null) {
            for (StatusBarNotification sbn : active) {
                if (packageFilter != null && !packageFilter.isEmpty() && !sbn.getPackageName().contains(packageFilter)) continue;
                Notification notification = sbn.getNotification();
                Bundle extras = notification.extras;
                JSONArray actions = new JSONArray();
                if (notification.actions != null) {
                    for (int i = 0; i < notification.actions.length; i++) {
                        Notification.Action action = notification.actions[i];
                        actions.put(new JSONObject()
                                .put("index", i)
                                .put("title", String.valueOf(action.title))
                                .put("reply", action.getRemoteInputs() != null && action.getRemoteInputs().length > 0));
                    }
                }
                items.put(new JSONObject()
                        .put("key", sbn.getKey())
                        .put("packageName", sbn.getPackageName())
                        .put("postedAt", sbn.getPostTime())
                        .put("title", stringExtra(extras, Notification.EXTRA_TITLE))
                        .put("text", stringExtra(extras, Notification.EXTRA_TEXT))
                        .put("actions", actions));
                if (items.length() >= limit) break;
            }
        }
        return new JSONObject().put("notifications", items).put("count", items.length());
    }

    static JSONObject dismiss(String key) throws Exception {
        requireService().cancelNotification(key);
        return new JSONObject().put("dismissed", true).put("key", key);
    }

    static JSONObject act(String key, int actionIndex, String replyText) throws Exception {
        PhoneNotificationListenerService service = requireService();
        StatusBarNotification target = find(service, key);
        Notification.Action[] actions = target.getNotification().actions;
        if (actions == null || actionIndex < 0 || actionIndex >= actions.length) {
            throw new IllegalArgumentException("Notification action index is unavailable.");
        }
        Notification.Action action = actions[actionIndex];
        Intent intent = new Intent();
        RemoteInput[] inputs = action.getRemoteInputs();
        if (replyText != null && !replyText.isEmpty()) {
            if (inputs == null || inputs.length == 0) throw new IllegalArgumentException("Selected action does not accept a reply.");
            Bundle replies = new Bundle();
            for (RemoteInput input : inputs) replies.putCharSequence(input.getResultKey(), replyText);
            RemoteInput.addResultsToIntent(inputs, intent, replies);
        }
        try {
            action.actionIntent.send(service, 0, intent);
        } catch (PendingIntent.CanceledException e) {
            throw new IllegalStateException("Notification action is no longer available.", e);
        }
        return new JSONObject().put("performed", true).put("key", key).put("actionIndex", actionIndex);
    }

    private static StatusBarNotification find(PhoneNotificationListenerService service, String key) {
        StatusBarNotification[] active = service.getActiveNotifications();
        if (active != null) for (StatusBarNotification item : active) if (item.getKey().equals(key)) return item;
        throw new IllegalArgumentException("Active notification not found.");
    }

    private static PhoneNotificationListenerService requireService() {
        PhoneNotificationListenerService service = instance;
        if (service == null) throw new IllegalStateException("Notification access is not enabled or connected.");
        return service;
    }

    private static String stringExtra(Bundle extras, String key) {
        CharSequence value = extras == null ? null : extras.getCharSequence(key);
        return value == null ? "" : value.toString();
    }
}
