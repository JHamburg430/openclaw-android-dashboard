#!/usr/bin/env python3
"""Opt-in UI/power integration check on the disposable dashboard-keep-awake-test AVD.

Install the debug APK first; configure a dummy gateway (http://127.0.0.1:9/),
start the node and enable keep-awake. No real gateway credentials are needed.
Usage: python scripts/check-background-keep-awake.py /path/to/adb emulator-5554
Add --controls-only to rerun controls/lifecycle after the timeout checks pass.
The suite restores emulator power settings and leaves the test node stopped.
"""
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

adb = [sys.argv[1], "-s", sys.argv[2]]
package = "ai.openclaw.dashboard"


def shell(*args):
    return subprocess.check_output(adb + ["shell", *args], text=True, stderr=subprocess.STDOUT)


def observe():
    for _ in range(5):
        try:
            result = shell("uiautomator", "dump", "/sdcard/background-awake-ui.xml")
        except subprocess.CalledProcessError:
            time.sleep(1)
            continue
        if "dumped to:" in result:
            return ET.fromstring(shell("cat", "/sdcard/background-awake-ui.xml"))
        time.sleep(1)
    raise AssertionError("UI observation unavailable")


def tap(label):
    nodes = list(observe().iter("node"))
    node = next(n for n in nodes if label in (n.get("text"), n.get("content-desc")))
    x1, y1, x2, y2 = map(int, re.findall(r"\d+", node.get("bounds")))
    shell("input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
    time.sleep(1)


def locks(cpu, screen, label):
    power = shell("dumpsys", "power")
    # Match held locks only, not the historical acquisition/release log.
    held = "\n".join(line for line in power.splitlines() if re.match(r"\s+(PARTIAL|SCREEN_DIM)_WAKE_LOCK\s", line))
    assert ("OpenClaw:PhoneNodeCpu" in held) == cpu, label + ": CPU"
    assert ("OpenClaw:PhoneControlScreen" in held) == screen, label + ": screen"
    print("PASS:", label, flush=True)


def awake(expected):
    state = "Awake" if expected else "Asleep"
    assert "mWakefulness=" + state in shell("dumpsys", "power"), state


def launch():
    shell("am", "start", "-n", package + "/.MainActivity")
    time.sleep(3)


def wake():
    shell("input", "keyevent", "KEYCODE_WAKEUP")
    shell("wm", "dismiss-keyguard")
    time.sleep(2)


def notification_action(label):
    shell("cmd", "statusbar", "expand-notifications")
    time.sleep(1)
    tap(label)
    shell("cmd", "statusbar", "collapse")


def show_toggle():
    label = "Keep phone awake across apps"
    if not any(n.get("text") == label for n in observe().iter("node")):
        tap("Open apps")
        tap("Controls / Diagnostics")
    return next(n for n in observe().iter("node") if n.get("text") == label)


def check_timeouts():
    shell("cmd", "statusbar", "collapse")
    wake()
    shell("input", "keyevent", "KEYCODE_HOME")
    time.sleep(20)
    awake(True)
    locks(True, True, "Home stays awake beyond timeout during connection retries")
    shell("am", "start", "-a", "android.settings.SETTINGS")
    time.sleep(20)
    awake(True)
    locks(True, True, "another app stays awake beyond timeout")
    shell("input", "keyevent", "KEYCODE_SLEEP")
    time.sleep(2)
    awake(False)
    locks(True, False, "manual lock sleeps screen but retains node CPU lock")
    wake()
    locks(True, True, "screen keep-awake resumes after waking")
    notification_action("Allow sleep")
    locks(False, False, "notification Allow sleep releases both locks")
    assert "isForeground=true" in shell("dumpsys", "activity", "services", package)
    shell("input", "keyevent", "KEYCODE_HOME")
    time.sleep(20)
    awake(False)
    print("PASS: Allow sleep restores real screen timeout without stopping node", flush=True)


assert "dashboard-keep-awake-test" in subprocess.check_output(adb + ["emu", "avd", "name"], text=True), "Disposable test AVD only"
timeout = shell("settings", "get", "system", "screen_off_timeout").strip()
charging = shell("settings", "get", "global", "stay_on_while_plugged_in").strip()
try:
    shell("settings", "put", "global", "stay_on_while_plugged_in", "0")
    shell("settings", "put", "system", "screen_off_timeout", "15000")
    if "--controls-only" not in sys.argv:
        check_timeouts()
    else:
        wake()
        launch()
        notification_action("Allow sleep")
    # Leave enough time for UI snapshots while keep-awake is intentionally off.
    shell("settings", "put", "system", "screen_off_timeout", "60000")
    wake()
    launch()
    assert show_toggle().get("checked") == "false"
    shell("am", "force-stop", package)
    launch()
    assert show_toggle().get("checked") == "false"
    locks(False, False, "Allow sleep choice survives process restart and renders off")
    tap("Keep phone awake across apps")
    locks(True, True, "toggle enables service locks")
    tap("Keep phone awake across apps")
    locks(False, False, "toggle disables service locks")
    tap("Keep phone awake across apps")
    shell("am", "force-stop", package)
    locks(False, False, "process termination releases locks")
    launch()
    locks(True, True, "saved on choice restored on node restart")
    shell("input", "keyevent", "KEYCODE_BACK")
    time.sleep(2)
    locks(True, True, "finishing Dashboard activity retains service locks")
    notification_action("Stop node")
    locks(False, False, "Stop node releases both locks")
    launch()
    locks(False, False, "reopening Dashboard respects stopped node")
    assert "isForeground=true" not in shell("dumpsys", "activity", "services", package)
    show_toggle()
    tap("Node")
    time.sleep(2)
    locks(True, True, "explicit Node action restarts keep-awake")
    notification_action("Stop node")
finally:
    shell("am", "force-stop", package)
    shell("settings", "put", "system", "screen_off_timeout", timeout)
    shell("settings", "put", "global", "stay_on_while_plugged_in", charging)
