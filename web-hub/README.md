# OpenClaw Web Hub

A dependency-free, loopback-only launcher for the web-capable applications in
the Android Dashboard app drawer. Tailscale Serve supplies private HTTPS ingress.

## Local service

```bash
node test-server.js
systemctl --user enable --now openclaw-web-hub.service
```

The service listens on `127.0.0.1:8789` and exposes only a static launcher and
read-only service availability checks. It does not proxy application data.

## Tailscale routes

| Public HTTPS port | Loopback target | Application |
| --- | --- | --- |
| 443 | OpenClaw-managed | Control UI |
| 8443 | 8790 | Live Conversation |
| 8444 | 8789 | Web Hub |
| 8445 | 8504 | Teams Help |
| 8446 | 8503 | Contacts |
| 8447 | 8501 | Monitor |
| 8448 | 8601 | Manuals RAG |

The launcher derives all links from the browser's current MagicDNS hostname.
Do not use Tailscale Funnel for these private applications.
