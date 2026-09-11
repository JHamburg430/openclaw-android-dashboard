#!/bin/sh
set -eu

HEALTH_URL="http://127.0.0.1:8790/health"
for attempt in 1 2 3 4; do
    if /usr/bin/curl --fail --silent --show-error --max-time 3 "$HEALTH_URL" >/dev/null; then
        exit 0
    fi
    /usr/bin/sleep 5
done

/usr/bin/systemctl --user try-restart openclaw-live-conversation.service
for attempt in 1 2 3 4 5 6; do
    if /usr/bin/curl --fail --silent --show-error --max-time 3 "$HEALTH_URL" >/dev/null; then
        exit 0
    fi
    /usr/bin/sleep 5
done
exit 1
