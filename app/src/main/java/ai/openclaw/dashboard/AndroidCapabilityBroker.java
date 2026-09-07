package ai.openclaw.dashboard;

import android.Manifest;
import android.app.Activity;
import android.app.SearchManager;
import android.content.ContentProviderOperation;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.provider.AlarmClock;
import android.provider.CalendarContract;
import android.provider.CallLog;
import android.provider.ContactsContract;
import android.provider.MediaStore;
import android.provider.Settings;
import android.provider.Telephony;
import android.telephony.SmsManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;

final class AndroidCapabilityBroker {
    private final Activity activity;

    AndroidCapabilityBroker(Activity activity) { this.activity = activity; }

    JSONObject handle(String command, JSONObject params) throws Exception {
        switch (command) {
            case "android.capabilities": return capabilities();
            case "contacts.search": return searchContacts(params);
            case "contacts.add": return addContact(params);
            case "calendar.events": return calendarEvents(params);
            case "calendar.add": return addCalendarEvent(params);
            case "callLog.search": return searchCallLog(params);
            case "sms.search": return searchSms(params);
            case "sms.send": return sendSms(params);
            case "notifications.list": return PhoneNotificationListenerService.list(limit(params, 50), params.optString("packageName", ""));
            case "notifications.dismiss": return PhoneNotificationListenerService.dismiss(required(params, "key"));
            case "notifications.act": return PhoneNotificationListenerService.act(required(params, "key"), params.optInt("actionIndex", 0), params.optString("reply", ""));
            case "media.search": return searchMedia(params);
            case "android.intent.open": return openIntent(params);
            case "android.intent.composeSms": return composeSms(params);
            case "android.intent.composeEmail": return composeEmail(params);
            case "android.intent.dial": return dial(params, false);
            case "android.call.place": return dial(params, true);
            case "android.intent.setAlarm": return setAlarm(params);
            case "android.intent.setTimer": return setTimer(params);
            case "mobile.ui.observe": return PhoneAccessibilityService.snapshot(limit(params, 200));
            case "mobile.ui.act": return PhoneAccessibilityService.perform(params);
            case "android.settings.notificationAccess": return openSettings(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS);
            case "android.settings.accessibility": return openSettings(Settings.ACTION_ACCESSIBILITY_SETTINGS);
            case "android.settings.app": return openAppSettings();
            default: return null;
        }
    }

    private JSONObject capabilities() throws Exception {
        return new JSONObject()
                .put("contactsRead", granted(Manifest.permission.READ_CONTACTS))
                .put("contactsWrite", granted(Manifest.permission.WRITE_CONTACTS))
                .put("calendarRead", granted(Manifest.permission.READ_CALENDAR))
                .put("calendarWrite", granted(Manifest.permission.WRITE_CALENDAR))
                .put("callLogRead", granted(Manifest.permission.READ_CALL_LOG))
                .put("smsRead", granted(Manifest.permission.READ_SMS))
                .put("smsSend", granted(Manifest.permission.SEND_SMS))
                .put("callPhone", granted(Manifest.permission.CALL_PHONE))
                .put("notificationAccess", PhoneNotificationListenerService.isConnected())
                .put("accessibilityControl", PhoneAccessibilityService.isConnected());
    }

    private JSONObject searchContacts(JSONObject params) throws Exception {
        requirePermission(Manifest.permission.READ_CONTACTS);
        String query = params.optString("query", "");
        String selection = query.isEmpty() ? null : ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME_PRIMARY + " LIKE ? OR " + ContactsContract.CommonDataKinds.Phone.NUMBER + " LIKE ?";
        String[] args = query.isEmpty() ? null : new String[]{"%" + query + "%", "%" + query + "%"};
        JSONArray contacts = new JSONArray();
        try (Cursor cursor = activity.getContentResolver().query(
                ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
                new String[]{ContactsContract.CommonDataKinds.Phone.CONTACT_ID, ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME_PRIMARY, ContactsContract.CommonDataKinds.Phone.NUMBER, ContactsContract.CommonDataKinds.Phone.TYPE},
                selection, args, ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME_PRIMARY + " ASC")) {
            while (cursor != null && cursor.moveToNext() && contacts.length() < limit(params, 100)) {
                contacts.put(new JSONObject()
                        .put("id", cursor.getLong(0)).put("name", cursor.getString(1))
                        .put("phone", cursor.getString(2)).put("type", cursor.getInt(3)));
            }
        }
        return new JSONObject().put("contacts", contacts).put("count", contacts.length());
    }

    private JSONObject addContact(JSONObject params) throws Exception {
        requirePermission(Manifest.permission.WRITE_CONTACTS);
        String name = required(params, "name");
        String phone = params.optString("phone", "");
        String email = params.optString("email", "");
        ArrayList<ContentProviderOperation> operations = new ArrayList<>();
        operations.add(ContentProviderOperation.newInsert(ContactsContract.RawContacts.CONTENT_URI)
                .withValue(ContactsContract.RawContacts.ACCOUNT_TYPE, null).withValue(ContactsContract.RawContacts.ACCOUNT_NAME, null).build());
        operations.add(ContentProviderOperation.newInsert(ContactsContract.Data.CONTENT_URI).withValueBackReference(ContactsContract.Data.RAW_CONTACT_ID, 0)
                .withValue(ContactsContract.Data.MIMETYPE, ContactsContract.CommonDataKinds.StructuredName.CONTENT_ITEM_TYPE)
                .withValue(ContactsContract.CommonDataKinds.StructuredName.DISPLAY_NAME, name).build());
        if (!phone.isEmpty()) operations.add(ContentProviderOperation.newInsert(ContactsContract.Data.CONTENT_URI).withValueBackReference(ContactsContract.Data.RAW_CONTACT_ID, 0)
                .withValue(ContactsContract.Data.MIMETYPE, ContactsContract.CommonDataKinds.Phone.CONTENT_ITEM_TYPE)
                .withValue(ContactsContract.CommonDataKinds.Phone.NUMBER, phone).withValue(ContactsContract.CommonDataKinds.Phone.TYPE, ContactsContract.CommonDataKinds.Phone.TYPE_MOBILE).build());
        if (!email.isEmpty()) operations.add(ContentProviderOperation.newInsert(ContactsContract.Data.CONTENT_URI).withValueBackReference(ContactsContract.Data.RAW_CONTACT_ID, 0)
                .withValue(ContactsContract.Data.MIMETYPE, ContactsContract.CommonDataKinds.Email.CONTENT_ITEM_TYPE)
                .withValue(ContactsContract.CommonDataKinds.Email.ADDRESS, email).withValue(ContactsContract.CommonDataKinds.Email.TYPE, ContactsContract.CommonDataKinds.Email.TYPE_HOME).build());
        activity.getContentResolver().applyBatch(ContactsContract.AUTHORITY, operations);
        return new JSONObject().put("created", true).put("name", name);
    }

    private JSONObject calendarEvents(JSONObject params) throws Exception {
        requirePermission(Manifest.permission.READ_CALENDAR);
        long from = params.optLong("from", System.currentTimeMillis() - 86400000L);
        long to = params.optLong("to", System.currentTimeMillis() + 30L * 86400000L);
        String query = params.optString("query", "");
        String selection = CalendarContract.Events.DTSTART + " >= ? AND " + CalendarContract.Events.DTSTART + " <= ?" + (query.isEmpty() ? "" : " AND " + CalendarContract.Events.TITLE + " LIKE ?");
        String[] args = query.isEmpty() ? new String[]{String.valueOf(from), String.valueOf(to)} : new String[]{String.valueOf(from), String.valueOf(to), "%" + query + "%"};
        JSONArray events = new JSONArray();
        try (Cursor cursor = activity.getContentResolver().query(CalendarContract.Events.CONTENT_URI,
                new String[]{CalendarContract.Events._ID, CalendarContract.Events.TITLE, CalendarContract.Events.DESCRIPTION, CalendarContract.Events.EVENT_LOCATION, CalendarContract.Events.DTSTART, CalendarContract.Events.DTEND, CalendarContract.Events.ALL_DAY, CalendarContract.Events.CALENDAR_ID},
                selection, args, CalendarContract.Events.DTSTART + " ASC")) {
            while (cursor != null && cursor.moveToNext() && events.length() < limit(params, 100)) {
                events.put(new JSONObject().put("id", cursor.getLong(0)).put("title", cursor.getString(1))
                        .put("description", cursor.getString(2)).put("location", cursor.getString(3))
                        .put("start", cursor.getLong(4)).put("end", cursor.getLong(5)).put("allDay", cursor.getInt(6) != 0).put("calendarId", cursor.getLong(7)));
            }
        }
        return new JSONObject().put("events", events).put("count", events.length());
    }

    private JSONObject addCalendarEvent(JSONObject params) throws Exception {
        requirePermission(Manifest.permission.WRITE_CALENDAR);
        long calendarId = params.optLong("calendarId", findWritableCalendar());
        ContentValues values = new ContentValues();
        values.put(CalendarContract.Events.CALENDAR_ID, calendarId);
        values.put(CalendarContract.Events.TITLE, required(params, "title"));
        values.put(CalendarContract.Events.DTSTART, params.getLong("start"));
        values.put(CalendarContract.Events.DTEND, params.optLong("end", params.getLong("start") + 3600000L));
        values.put(CalendarContract.Events.EVENT_TIMEZONE, params.optString("timeZone", java.util.TimeZone.getDefault().getID()));
        values.put(CalendarContract.Events.DESCRIPTION, params.optString("description", ""));
        values.put(CalendarContract.Events.EVENT_LOCATION, params.optString("location", ""));
        values.put(CalendarContract.Events.ALL_DAY, params.optBoolean("allDay", false) ? 1 : 0);
        Uri uri = activity.getContentResolver().insert(CalendarContract.Events.CONTENT_URI, values);
        if (uri == null) throw new IllegalStateException("Calendar provider rejected the event.");
        return new JSONObject().put("created", true).put("eventId", uri.getLastPathSegment()).put("calendarId", calendarId);
    }

    private long findWritableCalendar() {
        requirePermission(Manifest.permission.READ_CALENDAR);
        try (Cursor cursor = activity.getContentResolver().query(CalendarContract.Calendars.CONTENT_URI,
                new String[]{CalendarContract.Calendars._ID}, CalendarContract.Calendars.VISIBLE + "=1 AND " + CalendarContract.Calendars.CALENDAR_ACCESS_LEVEL + ">=?",
                new String[]{String.valueOf(CalendarContract.Calendars.CAL_ACCESS_CONTRIBUTOR)}, CalendarContract.Calendars.IS_PRIMARY + " DESC")) {
            if (cursor != null && cursor.moveToFirst()) return cursor.getLong(0);
        }
        throw new IllegalStateException("No writable calendar is available.");
    }

    private JSONObject searchCallLog(JSONObject params) throws Exception {
        requirePermission(Manifest.permission.READ_CALL_LOG);
        String query = params.optString("query", "");
        JSONArray calls = new JSONArray();
        String selection = query.isEmpty() ? null : CallLog.Calls.NUMBER + " LIKE ? OR " + CallLog.Calls.CACHED_NAME + " LIKE ?";
        String[] args = query.isEmpty() ? null : new String[]{"%" + query + "%", "%" + query + "%"};
        try (Cursor cursor = activity.getContentResolver().query(CallLog.Calls.CONTENT_URI,
                new String[]{CallLog.Calls._ID, CallLog.Calls.NUMBER, CallLog.Calls.CACHED_NAME, CallLog.Calls.DATE, CallLog.Calls.DURATION, CallLog.Calls.TYPE},
                selection, args, CallLog.Calls.DATE + " DESC")) {
            while (cursor != null && cursor.moveToNext() && calls.length() < limit(params, 100)) calls.put(new JSONObject()
                    .put("id", cursor.getLong(0)).put("number", cursor.getString(1)).put("name", cursor.getString(2))
                    .put("date", cursor.getLong(3)).put("durationSeconds", cursor.getLong(4)).put("type", cursor.getInt(5)));
        }
        return new JSONObject().put("calls", calls).put("count", calls.length());
    }

    private JSONObject searchSms(JSONObject params) throws Exception {
        requirePermission(Manifest.permission.READ_SMS);
        String query = params.optString("query", "");
        JSONArray messages = new JSONArray();
        String selection = query.isEmpty() ? null : Telephony.Sms.ADDRESS + " LIKE ? OR " + Telephony.Sms.BODY + " LIKE ?";
        String[] args = query.isEmpty() ? null : new String[]{"%" + query + "%", "%" + query + "%"};
        try (Cursor cursor = activity.getContentResolver().query(Telephony.Sms.CONTENT_URI,
                new String[]{Telephony.Sms._ID, Telephony.Sms.ADDRESS, Telephony.Sms.BODY, Telephony.Sms.DATE, Telephony.Sms.TYPE, Telephony.Sms.READ},
                selection, args, Telephony.Sms.DATE + " DESC")) {
            while (cursor != null && cursor.moveToNext() && messages.length() < limit(params, 100)) messages.put(new JSONObject()
                    .put("id", cursor.getLong(0)).put("address", cursor.getString(1)).put("body", cursor.getString(2))
                    .put("date", cursor.getLong(3)).put("type", cursor.getInt(4)).put("read", cursor.getInt(5) != 0));
        }
        return new JSONObject().put("messages", messages).put("count", messages.length());
    }

    @SuppressWarnings("deprecation") private JSONObject sendSms(JSONObject params) throws Exception {
        requirePermission(Manifest.permission.SEND_SMS);
        String address = required(params, "address");
        String body = required(params, "body");
        SmsManager manager = SmsManager.getDefault();
        ArrayList<String> parts = manager.divideMessage(body);
        if (parts.size() > 1) manager.sendMultipartTextMessage(address, null, parts, null, null);
        else manager.sendTextMessage(address, null, body, null, null);
        return new JSONObject().put("submitted", true).put("address", address).put("parts", parts.size());
    }

    private JSONObject searchMedia(JSONObject params) throws Exception {
        String kind = params.optString("kind", "images");
        Uri uri;
        if ("video".equals(kind) || "videos".equals(kind)) uri = MediaStore.Video.Media.EXTERNAL_CONTENT_URI;
        else if ("audio".equals(kind)) uri = MediaStore.Audio.Media.EXTERNAL_CONTENT_URI;
        else uri = MediaStore.Images.Media.EXTERNAL_CONTENT_URI;
        String query = params.optString("query", "");
        JSONArray media = new JSONArray();
        String selection = query.isEmpty() ? null : MediaStore.MediaColumns.DISPLAY_NAME + " LIKE ?";
        String[] args = query.isEmpty() ? null : new String[]{"%" + query + "%"};
        try (Cursor cursor = activity.getContentResolver().query(uri,
                new String[]{MediaStore.MediaColumns._ID, MediaStore.MediaColumns.DISPLAY_NAME, MediaStore.MediaColumns.MIME_TYPE, MediaStore.MediaColumns.SIZE, MediaStore.MediaColumns.DATE_MODIFIED},
                selection, args, MediaStore.MediaColumns.DATE_MODIFIED + " DESC")) {
            while (cursor != null && cursor.moveToNext() && media.length() < limit(params, 100)) media.put(new JSONObject()
                    .put("id", cursor.getLong(0)).put("name", cursor.getString(1)).put("mimeType", cursor.getString(2))
                    .put("size", cursor.getLong(3)).put("modified", cursor.getLong(4) * 1000L).put("uri", Uri.withAppendedPath(uri, String.valueOf(cursor.getLong(0))).toString()));
        }
        return new JSONObject().put("media", media).put("count", media.length()).put("kind", kind);
    }

    private JSONObject openIntent(JSONObject params) throws Exception {
        String uri = required(params, "uri");
        Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse(uri)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        String packageName = params.optString("packageName", "");
        if (!packageName.isEmpty()) intent.setPackage(packageName);
        return launch(intent, "opened");
    }

    private JSONObject composeSms(JSONObject params) throws Exception {
        Intent intent = new Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:" + Uri.encode(params.optString("address", ""))));
        intent.putExtra("sms_body", params.optString("body", ""));
        return launch(intent, "composed");
    }

    private JSONObject composeEmail(JSONObject params) throws Exception {
        Intent intent = new Intent(Intent.ACTION_SENDTO, Uri.parse("mailto:" + Uri.encode(params.optString("to", ""))));
        intent.putExtra(Intent.EXTRA_SUBJECT, params.optString("subject", ""));
        intent.putExtra(Intent.EXTRA_TEXT, params.optString("body", ""));
        return launch(intent, "composed");
    }

    private JSONObject dial(JSONObject params, boolean place) throws Exception {
        String number = required(params, "number");
        if (place) requirePermission(Manifest.permission.CALL_PHONE);
        Intent intent = new Intent(place ? Intent.ACTION_CALL : Intent.ACTION_DIAL, Uri.parse("tel:" + Uri.encode(number)));
        return launch(intent, place ? "placed" : "opened");
    }

    private JSONObject setAlarm(JSONObject params) throws Exception {
        Intent intent = new Intent(AlarmClock.ACTION_SET_ALARM)
                .putExtra(AlarmClock.EXTRA_HOUR, params.getInt("hour")).putExtra(AlarmClock.EXTRA_MINUTES, params.optInt("minute", 0))
                .putExtra(AlarmClock.EXTRA_MESSAGE, params.optString("message", "OpenClaw alarm")).putExtra(AlarmClock.EXTRA_SKIP_UI, params.optBoolean("skipUi", false));
        return launch(intent, "submitted");
    }

    private JSONObject setTimer(JSONObject params) throws Exception {
        Intent intent = new Intent(AlarmClock.ACTION_SET_TIMER).putExtra(AlarmClock.EXTRA_LENGTH, params.getInt("seconds"))
                .putExtra(AlarmClock.EXTRA_MESSAGE, params.optString("message", "OpenClaw timer")).putExtra(AlarmClock.EXTRA_SKIP_UI, params.optBoolean("skipUi", false));
        return launch(intent, "submitted");
    }

    private JSONObject openSettings(String action) throws Exception { return launch(new Intent(action), "opened"); }
    private JSONObject openAppSettings() throws Exception { return launch(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + activity.getPackageName())), "opened"); }

    private JSONObject launch(Intent intent, String resultKey) throws Exception {
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        activity.runOnUiThread(() -> activity.startActivity(intent));
        return new JSONObject().put(resultKey, true);
    }

    private boolean granted(String permission) { return activity.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED; }
    private void requirePermission(String permission) {
        if (!granted(permission)) throw new SecurityException("Android permission is required: " + permission);
    }
    private static int limit(JSONObject params, int fallback) { return Math.max(1, Math.min(params.optInt("limit", fallback), 500)); }
    private static String required(JSONObject params, String key) throws Exception {
        String value = params.optString(key, "").trim();
        if (value.isEmpty()) throw new IllegalArgumentException(key + " is required.");
        return value;
    }
}
