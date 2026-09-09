import assert from "node:assert/strict";
import fs from "node:fs";

const read = (path) => fs.readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
const manifest = read("app/src/main/AndroidManifest.xml");
const activity = read("app/src/main/java/ai/openclaw/dashboard/MainActivity.java");
const client = read("app/src/main/java/ai/openclaw/dashboard/OpenClawClient.java");
const service = read("app/src/main/java/ai/openclaw/dashboard/PhoneNodeService.java");
const audit = read("app/src/main/java/ai/openclaw/dashboard/ConnectionAuditLog.java");
const notificationListener = read("app/src/main/java/ai/openclaw/dashboard/PhoneNotificationListenerService.java");

assert.match(manifest, /android:name="\.PhoneNodeService"/);
assert.match(manifest, /android:foregroundServiceType="connectedDevice"/);
assert.match(manifest, /android:name="\.PhoneNodeBootReceiver"/);
assert.match(manifest, /android\.intent\.action\.BOOT_COMPLETED/);
assert.match(manifest, /android\.permission\.FOREGROUND_SERVICE_CONNECTED_DEVICE/);

assert.match(service, /return START_STICKY/);
assert.match(service, /registerDefaultNetworkCallback/);
assert.match(service, /scheduleReconnect/);
assert.match(service, /MAX_RETRY_MS/);
assert.match(service, /startForeground\(NOTIFICATION_ID/);
assert.match(service, /onInvokeStarted/);
assert.match(service, /onInvokeFinished/);

assert.doesNotMatch(activity, /OpenClawClient nodeClient/);
assert.match(activity, /bindPhoneNodeService/);
assert.match(activity, /Connection Center/);
assert.doesNotMatch(activity, /appButton\("Portals"/);
assert.match(activity, /<button onclick=\\"repairPhoneNode\(\)\\">Re-pair Phone Node<\/button>/);
assert.match(activity, /notificationAccessConnected/);
assert.match(activity, /traceSession\(message\)/);
assert.match(notificationListener, /isAccessEnabled\(Context context\)/);

assert.match(client, /CLIENT_VERSION = BuildConfig\.VERSION_NAME/);
assert.match(client, /nodeConnectionId/);
assert.match(client, /correlationId/);

for (const sensitive of ["token", "password", "secret", "body", "reply", "phone"]) {
  assert.ok(audit.includes(`lower.contains(\"${sensitive}\")`) || audit.includes(`lower.equals(\"${sensitive}\")`), `audit redacts ${sensitive}`);
}
assert.match(audit, /MAX_BYTES = 1024L \* 1024L/);

console.log("persistent phone node, native re-pair, permission diagnostics, correlation, and redacted audit are wired");
