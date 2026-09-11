#!/usr/bin/env bash
set -euo pipefail

# The HTTPS route is used by current production APKs. The tailnet-only TCP
# forward preserves loading and WebSocket support for already-installed APKs
# that still address the service directly on port 8790. Neither route is a
# public Funnel, and the application itself remains bound to loopback.
tailscale serve --bg --https=8443 http://127.0.0.1:8790
tailscale serve --bg --tcp=8790 tcp://127.0.0.1:8790

tailscale serve status
