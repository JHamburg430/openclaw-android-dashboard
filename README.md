# OpenClaw Android Dashboard

Native Android shell for the real OpenClaw Control UI.

## APK

Built debug APK:

```text
app/build/outputs/apk/debug/app-debug.apk
```

## Usage

Generate a setup code on the gateway host:

```sh
openclaw qr --json --no-ascii
```

Paste the `setupCode` into the app and tap `Decode setup code`.

Then enter either:

- a gateway token, or
- the gateway password

Tap `Open UI` to load the actual OpenClaw Control UI inside the app `WebView`.

Notes:

- `ws://` and `wss://` gateway URLs are converted to the matching dashboard `http://` or `https://` URL automatically.
- When a setup code includes both a raw gateway URL and a secure public URL, the app prefers the secure public URL automatically.
- The app injects native Control UI auth into the embedded `WebView`, including the gateway password when provided, instead of relying on URL query parameters.
- Cleartext `http://` gateways are allowed because many local OpenClaw setups, including this one, are not TLS-enabled.
- Realtime Talk on Android WebView requires a secure `https://` dashboard origin, or `http://localhost` during local emulator-only testing. For real devices, use your Tailscale/MagicDNS Control UI hostname rather than a raw LAN or tailnet IP.

If the gateway reports pairing is required, approve the pending request from the host:

```sh
openclaw devices list
openclaw devices approve <requestId>
```

## Hybrid Control UI and phone node

Version 1.0.69 deliberately keeps two independent OpenClaw connections:

- The embedded WebView is a normal Control UI browser client for complete chat, agent, session, and settings functionality.
- `PhoneNodeService` is a separately paired Android node that remains available when the activity or WebView is recreated.

The phone node runs as an opted-in foreground service, reconnects with bounded exponential backoff after gateway or network loss, restores after boot/application upgrades, and re-advertises its native commands after each connection. The native **Connection Center** reports the WebView connection, phone-node identity/connection, active session metadata observed from Control UI traffic, retry state, and the most recent error.

The custom node uses OpenClaw's canonical `openclaw-android` protocol client ID because Gateway client IDs are a closed protocol enum. It remains distinguishable from the stock app by its stable device identity, **John's S25 Ultra — Dashboard** display name, Dashboard product metadata, app version, and expanded command list.

Connection and invocation events are saved to a rotating, bounded JSONL audit trail in app-private storage. Exported audit data is redacted for credentials and common message/contact fields. Each invocation returns `_trace` metadata containing its `invokeId`, generated or supplied `correlationId`, node connection ID, and duration.

Use **Stop Phone Node** to disable background connectivity without affecting the Control UI. **Re-pair Phone Node** clears only the node authorization token, preserves the stable device identity, and reconnects so the gateway can approve it again.

## Phone capability broker

The native node exposes narrow commands for personal phone automation:

- Contacts: `contacts.search`, `contacts.add`
- Calendar: `calendar.events`, `calendar.add`
- Calls and SMS: `callLog.search`, `sms.search`, `sms.send`, `android.intent.dial`, `android.call.place`
- Notifications: `notifications.list`, `notifications.dismiss`, `notifications.act`
- Media and apps: `media.search`, `device.apps`, `android.apps.launch`, `android.intent.open`
- Safe compose flows: `android.intent.composeSms`, `android.intent.composeEmail`
- Clock: `android.intent.setAlarm`, `android.intent.setTimer`
- UI fallback: `mobile.ui.observe`, `mobile.ui.act`

Open **Android Native** in the app and use **Request Phone Permissions**. Notification reading/replies and arbitrary app UI control are special Android accesses, so enable **Notification Access** and **Accessibility Control** separately from that screen. In the permission report, `notifications` and `notificationAccess` mean notification reading/action access; `canPostNotifications`, `postNotifications`, and `appNotificationsEnabled` describe this app's own status notifications; `notificationAccessConnected` reports whether the listener service is presently bound. Android may require **Allow restricted settings** in the app-info menu for a sideloaded Accessibility service. **Re-pair Phone Node** is also available directly on this screen.

`mobile.ui.act` supports `click`, `setText`, `scrollForward`, `scrollBackward`, `tap`, `back`, `home`, `recents`, and `notifications`. UI observations and notification text are untrusted app data; the dedicated Phone Control agent is instructed never to treat them as agent instructions.

## Live Conversation behavior

Live Conversation shows the newest messages first and retains up to 120 messages, with a bounded 32-message context window for natural follow-ups. Remembered conversation supplies continuity, preferences, names, and referents only; changing facts must be refreshed from live tools or the authoritative session catalog before they are answered.

Tool-backed requests use an explicit acknowledgment-first handoff. Jarvis finishes delivering a short spoken acknowledgment before the bridge dispatches any agent or session action. Immediate silent playback-stop commands remain the intentional exception.

## Build

This workspace has a local portable build toolchain under `/home/john/.android-build`.

```sh
export JAVA_HOME=/home/john/.android-build/jdk-17.0.19+10
export ANDROID_HOME=/home/john/.android-build/android-sdk
export PATH=/home/john/.android-build/jdk-17.0.19+10/bin:/home/john/.android-build/gradle-8.10.2/bin:$ANDROID_HOME/platform-tools:$PATH
gradle --no-daemon assembleDebug
```
