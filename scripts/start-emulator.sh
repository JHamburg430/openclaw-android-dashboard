#!/usr/bin/env bash
set -euo pipefail

ANDROID_HOME="${ANDROID_HOME:-/home/john/.android-build/android-sdk}"
JAVA_HOME="${JAVA_HOME:-/home/john/.android-build/jdk-17.0.19+10}"
ANDROID_AVD_HOME="${ANDROID_AVD_HOME:-/home/john/.config/.android/avd}"
ADB="${ADB:-$ANDROID_HOME/platform-tools/adb}"
EMULATOR="${EMULATOR:-$ANDROID_HOME/emulator/emulator}"
AVD_NAME="${AVD_NAME:-openclaw-talk-test}"
GRADLE="${GRADLE:-/home/john/.android-build/gradle-8.10.2/bin/gradle}"
APK="app/build/outputs/apk/debug/app-debug.apk"

export ANDROID_HOME JAVA_HOME ANDROID_AVD_HOME ADB

serial=""
while read -r candidate state _; do
  [[ "$candidate" == emulator-* && "$state" == device ]] || continue
  if [[ "$($ADB -s "$candidate" emu avd name 2>/dev/null | head -1 | tr -d '\r')" == "$AVD_NAME" ]]; then
    serial="$candidate"
    break
  fi
done < <($ADB devices -l | tail -n +2)

if [[ -z "$serial" ]]; then
  log_file="${XDG_RUNTIME_DIR:-/tmp}/${AVD_NAME}.log"
  nohup "$EMULATOR" -avd "$AVD_NAME" -no-snapshot-load -no-boot-anim \
    -gpu swiftshader_indirect -netdelay none -netspeed full >"$log_file" 2>&1 </dev/null &
  for _ in $(seq 1 60); do
    serial="$($ADB devices | awk '/^emulator-/{print $1; exit}')"
    if [[ -n "$serial" && "$($ADB -s "$serial" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" == "1" ]]; then
      break
    fi
    serial=""
    sleep 1
  done
fi

if [[ -z "$serial" ]]; then
  echo "Emulator failed to boot within 60 seconds." >&2
  exit 1
fi

$ADB -s "$serial" reverse tcp:18789 tcp:18789 >/dev/null
"$GRADLE" --no-daemon test lint assembleDebug
$ADB -s "$serial" install -r "$APK"
$ADB -s "$serial" shell pm grant ai.openclaw.dashboard android.permission.RECORD_AUDIO || true
$ADB -s "$serial" shell pm grant ai.openclaw.dashboard android.permission.POST_NOTIFICATIONS || true
node scripts/connect-emulator-dashboard.mjs "$serial"

devtools_socket=""
for _ in $(seq 1 30); do
  devtools_socket="$($ADB -s "$serial" shell cat /proc/net/unix 2>/dev/null \
    | awk '/@webview_devtools_remote_/ {sub(/^@/, "", $NF); print $NF; exit}' \
    | tr -d '\r')"
  [[ -n "$devtools_socket" ]] && break
  sleep 1
done
if [[ -n "$devtools_socket" ]]; then
  $ADB -s "$serial" forward tcp:9223 "localabstract:$devtools_socket" >/dev/null
else
  echo "Warning: WebView DevTools socket was not available; automated Talk acceptance is unavailable." >&2
fi

echo "Android Dashboard is ready on $serial ($AVD_NAME)."
