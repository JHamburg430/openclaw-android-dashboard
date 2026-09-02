#!/usr/bin/env python3
import asyncio
import base64
import json
import sys

from aiohttp import ClientSession


async def main() -> None:
    audio = open(sys.argv[1], "rb").read()
    expected_route = sys.argv[2] if len(sys.argv) > 2 else "direct"
    events = []
    async with ClientSession() as client:
        async with client.ws_connect("http://127.0.0.1:8790/ws") as socket:
            await socket.send_json({"type": "start"})
            for offset in range(0, len(audio), 640):
                await socket.send_json({
                    "type": "audio",
                    "audioBase64": base64.b64encode(audio[offset:offset + 640]).decode("ascii"),
                })
            await socket.send_json({"type": "commit"})
            async for message in socket:
                event = json.loads(message.data)
                if event.get("type") != "audio":
                    events.append(event)
                if event.get("type") == "error":
                    raise RuntimeError(event.get("message") or "Live Conversation service error")
                if event.get("type") == "metrics":
                    break
    transcript = next(event for event in events if event.get("type") == "transcript")
    reply = next(event for event in events if event.get("type") == "reply")
    metrics = next(event for event in events if event.get("type") == "metrics")
    assert reply["route"] == expected_route
    assert metrics["route"] == expected_route
    print(json.dumps({"transcript": transcript["text"], "reply": reply["text"], "metrics": metrics}))


asyncio.run(main())
