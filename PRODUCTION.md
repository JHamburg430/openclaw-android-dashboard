# Production runbook

## Release gate

A release is eligible only when all of these pass:

1. `gradle --no-daemon clean test lint assembleDebug assembleRelease`
2. every `scripts/test-*.mjs` suite
3. all `live-conversation/test_*.py` suites, including the consented phone corpus
4. dependency audit with no actionable findings
5. `production_gate.py` through loopback and the tailnet HTTPS endpoint
6. APK signature, version, manifest, and SHA-256 verification
7. a physical-phone install/upgrade, wake, multi-turn, barge-in, route-change, and lock-screen test

CI enforces the portable portions. Physical-device acceptance remains a release
operator gate and must never be inferred from emulator or source tests.

## SLOs and alerts

- availability: at least 99.5%
- successful turn completion: at least 99%
- action-routing correctness: at least 99.5%
- p95 speech-end-to-first-audio: at most 2 seconds while GPU-accelerated
- dependency-restart recovery: at most 30 seconds
- false action rate: zero tolerated

Scrape `/metrics` and alert on failed turns, queue/audio-limit rejections,
repeated restarts, loss of GPU acceleration, p95 latency breaches, and disk
quota cleanup. `/health` is safe for a user-visible readiness probe.

## Canary and rollback

Keep the previous GitHub APK and Git tag as the last-known-good release. Install
new builds on one phone first, run the physical-device gate, then promote them.
If service or latency gates fail, restore the previous tag's service files,
reload the user units, restart only `openclaw-live-conversation.service`, and
re-run `production_gate.py`. Gateway restarts are unrelated and must not be used
as a Live Conversation rollback mechanism.

## Endurance matrix

Before broad use, run at least a two-hour, 100-turn soak on the target phone and
cover screen lock/unlock, process recreation, Wi-Fi/tailnet transitions, network
loss, Bluetooth and wired routes, notifications/calls/alarms, battery saver,
thermal throttling, and low storage. Confirm bounded memory, no duplicated
actions, no stale playback, and clean recovery after restarting each local
dependency during ASR, routing, TTS, delegation, and playback.

## Private data

Audio capture is explicit opt-in. Recordings and debug bundles are owner-only,
quota-bound, and expired automatically. The in-app delete control removes all
retained microphone captures after confirmation. Validate retention and deletion
tests whenever storage layout changes.
