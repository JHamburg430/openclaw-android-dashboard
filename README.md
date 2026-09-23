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
- Release builds require HTTPS for Live Conversation. Cleartext is denied by default and is limited to private `ts.net`/`openclaw.local` sibling tools; debug builds may still use a direct development endpoint.
- Realtime Talk on Android WebView requires a secure `https://` dashboard origin, or `http://localhost` during local emulator-only testing. For real devices, use your Tailscale/MagicDNS Control UI hostname rather than a raw LAN or tailnet IP.

If the gateway reports pairing is required, approve the pending request from the host:

```sh
openclaw devices list
openclaw devices approve <requestId>
```

## Hybrid Control UI and phone node

Version 1.0.75 deliberately keeps two independent OpenClaw connections:

- The embedded WebView is a normal Control UI browser client for complete chat, agent, session, and settings functionality.
- `PhoneNodeService` is a separately paired Android node that remains available when the activity or WebView is recreated.

The phone node runs as an opted-in foreground service, reconnects with bounded exponential backoff after gateway or network loss, restores after boot/application upgrades, and re-advertises its native commands after each connection. A service watchdog replaces stalled connection handshakes and disconnected sockets, while network and gateway changes force a fresh connection automatically. Connect authentication follows OpenClaw's native credential precedence: an explicit gateway token or password takes priority over a stored device token, and the signature uses only the selected credential. If the gateway rejects obsolete device authorization while another configured credential is available, the client preserves its stable device identity, clears only that stale authorization, and retries—the same recovery formerly available only through **Re-pair Phone Node**. The native **Connection Center** reports the WebView connection, phone-node identity/connection, active session metadata observed from Control UI traffic, retry state, and the most recent error.

Live Conversation capture uses Android's voice-communication path and reports
the active acoustic echo cancellation, noise suppression, and automatic gain
control state to its diagnostic stream. Agent speech requests transient audio
focus, stops immediately on focus loss, and releases focus after playback.

The production Live Conversation backend binds to loopback and is published to
the tailnet through Tailscale Serve on HTTPS port 8443. A tailnet-only TCP
forward on port 8790 keeps already-installed pre-HTTPS APKs working during the
upgrade window. Browser WebSockets are
same-origin checked, payloads and queues are bounded, and public health excludes
local paths, session identities, and diagnostic contents.

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

## Keep screen awake

The native controls include **Keep phone awake across apps** (open **+ → Controls / Diagnostics** if collapsed). Enabled by default, it remembers your choice across app restarts. While the phone node is running, its foreground service keeps the CPU and screen awake even on Home or in other apps, including during connection retries. The screen can dim to save power. Closing Dashboard's activity does not release these service-owned locks. Start the phone node with **Node** if it is stopped.

Turn the switch off or use **Allow sleep** in the phone-node notification to restore normal sleep without disconnecting the node. **Stop node** releases both locks and stops the connection. Your power button still turns the screen off; the CPU lock remains for the node, and screen keep-awake resumes when you wake the phone. It never wakes or unlocks the phone automatically. Android force-stop, process termination, manufacturer battery restrictions, and network outages can still interrupt connectivity; this is not a guarantee of an always-connected node. Keeping the phone awake uses more battery.

Implementation: Android `WAKE_LOCK` permission, a service-owned partial CPU lock and a capability-checked `SCREEN_DIM_WAKE_LOCK`. The legacy screen-lock level is necessary here because activity window flags do not keep other apps awake. Unsupported screen-lock capability is shown in Connection Center and the notification. See [Android PowerManager documentation](https://developer.android.com/reference/android/os/PowerManager).

The opt-in [emulator integration check](scripts/check-background-keep-awake.py) covers Home/other-app timeouts, manual lock, notification actions, toggle persistence, process restart, activity exit, and node stop/restart. It only runs against the disposable `dashboard-keep-awake-test` AVD, with a debug build and dummy gateway; setup is documented in the script. It does not substitute for physical-phone acceptance.

## Build

This workspace has a local portable build toolchain under `/home/john/.android-build`.

```sh
export JAVA_HOME=/home/john/.android-build/jdk-17.0.19+10
export ANDROID_HOME=/home/john/.android-build/android-sdk
export PATH=/home/john/.android-build/jdk-17.0.19+10/bin:/home/john/.android-build/gradle-8.10.2/bin:$ANDROID_HOME/platform-tools:$PATH
gradle --no-daemon assembleDebug
```

## Pre-release emulator

The reusable `openclaw-talk-test` Pixel 7 AVD runs Android 15 / API 35 with host
microphone and speaker support. From the repository root, start it, build and
install the current debug APK, forward the host Gateway, and bootstrap the
Control UI with:

```sh
scripts/start-emulator.sh
```

The script uses `adb reverse` for the loopback Gateway and a short-lived browser
bootstrap URL, so no Gateway password is printed or stored in the repository.
It leaves the emulator running for manual UI and Talk checks. Emulator results
are a pre-release regression gate; hardware-specific behavior still requires
the physical-phone acceptance listed in `PRODUCTION.md`.
