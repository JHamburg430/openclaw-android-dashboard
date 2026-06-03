# OpenClaw Android Dashboard

Native Android dashboard/node client for an OpenClaw Gateway.

## APK

Built debug APK:

```text
app/build/outputs/apk/debug/app-debug.apk
```

## Pairing

Generate a setup code on the gateway host:

```sh
openclaw qr --json --no-ascii
```

Paste the `setupCode` into the app and tap `Decode setup code`, then `Connect`.
If the gateway reports pairing is required, approve the pending request from the host:

```sh
openclaw nodes pending
openclaw nodes approve <requestId>
```

The app persists its Ed25519 device identity and any gateway-issued device token locally.

## Build

This workspace has a local portable build toolchain under `/home/john/.android-build`.

```sh
export JAVA_HOME=/home/john/.android-build/jdk-17.0.19+10
export ANDROID_HOME=/home/john/.android-build/android-sdk
export PATH=/home/john/.android-build/jdk-17.0.19+10/bin:/home/john/.android-build/gradle-8.10.2/bin:$ANDROID_HOME/platform-tools:$PATH
gradle --no-daemon assembleDebug
```
