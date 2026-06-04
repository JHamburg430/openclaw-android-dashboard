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
- The app passes the shared secret in both the query string and `#token=...` fragment so the embedded Control UI and its gated runtime endpoints can authenticate.
- Cleartext `http://` gateways are allowed because many local OpenClaw setups, including this one, are not TLS-enabled.
- Realtime Talk on Android WebView requires a secure `https://` dashboard origin, or `http://localhost` during local emulator-only testing. For real devices, use your Tailscale/MagicDNS Control UI hostname rather than a raw LAN or tailnet IP.

If the gateway reports pairing is required, approve the pending request from the host:

```sh
openclaw devices list
openclaw devices approve <requestId>
```

## Build

This workspace has a local portable build toolchain under `/home/john/.android-build`.

```sh
export JAVA_HOME=/home/john/.android-build/jdk-17.0.19+10
export ANDROID_HOME=/home/john/.android-build/android-sdk
export PATH=/home/john/.android-build/jdk-17.0.19+10/bin:/home/john/.android-build/gradle-8.10.2/bin:$ANDROID_HOME/platform-tools:$PATH
gradle --no-daemon assembleDebug
```
