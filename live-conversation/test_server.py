import unittest
from datetime import datetime
import json
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from server import (
    AGENT_SENTINEL,
    IGNORE_SENTINEL,
    SAY_SENTINEL,
    SPEECH_MODEL,
    SPEECH_MODEL_KEEP_ALIVE,
    DEFAULT_NODE_COMMAND,
    DEFAULT_OPENCLAW_MODULE,
    DEFAULT_TTS_MODEL_DIR,
    DEFAULT_TTS_SPEAKER_ID,
    LiveConversationService,
    extract_agent_text,
    parse_speech_model_output,
    normalize_spoken_text,
    render_page,
    split_spoken_text,
    speech_model_prompt,
)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 2, 12, 34, tzinfo=ZoneInfo("America/Detroit"))

    def test_speech_model_prompt_defines_hidden_escalation_contract(self):
        prompt = speech_model_prompt(self.now)
        self.assertIn(AGENT_SENTINEL, prompt)
        self.assertIn(SAY_SENTINEL, prompt)
        self.assertIn("12:34 PM", prompt)
        self.assertNotIn("I'll check and let you know", prompt)

    def test_prompt_ignores_background_speech_and_exposes_capabilities(self):
        prompt = speech_model_prompt(self.now, capabilities="agent research: Research\nskill weather: Forecasts")
        self.assertIn(IGNORE_SENTINEL, prompt)
        self.assertIn("likely background conversation", prompt)
        self.assertIn("agent research: Research", prompt)
        self.assertIn("answer capability questions quickly", prompt)

    def test_ignore_token_produces_no_spoken_text(self):
        self.assertEqual(parse_speech_model_output(IGNORE_SENTINEL), ("ignore", ""))

    def test_speech_supervisor_uses_higher_quality_local_model(self):
        self.assertEqual(SPEECH_MODEL, "qwen3.5:4b")
        self.assertEqual(SPEECH_MODEL_KEEP_ALIVE, "30m")

    def test_pending_agent_prompt_keeps_the_supervisor_conversational(self):
        prompt = speech_model_prompt(self.now, agent_pending=True)
        self.assertIn("still working", prompt)
        self.assertIn("Tell me a joke", prompt)
        self.assertIn("Never volunteer a timer-based or generic progress message", prompt)
        self.assertIn("never claim it has finished", prompt)
        self.assertNotIn(AGENT_SENTINEL, prompt)

    def test_server_has_no_synthetic_periodic_agent_updates(self):
        import inspect
        import server

        source = inspect.getsource(server.websocket)
        self.assertNotIn("agent_progress_reply", source)
        self.assertNotIn("timeout=20", source)

    def test_speech_model_output_intercepts_escalation_token(self):
        self.assertEqual(parse_speech_model_output(f"{AGENT_SENTINEL} I’ll check that."), ("agent", "I’ll check that."))
        self.assertEqual(parse_speech_model_output(f"{SAY_SENTINEL} It is 12:34 PM."), ("direct", "It is 12:34 PM."))
        self.assertEqual(parse_speech_model_output("It is 12:34 PM."), ("direct", "It is 12:34 PM."))

    def test_agent_json_extraction(self):
        payload = {"result": {"payloads": [{"text": "Ready."}]}}
        self.assertEqual(extract_agent_text(payload), "Ready.")
        self.assertTrue(DEFAULT_NODE_COMMAND.endswith("/node"))
        self.assertTrue(DEFAULT_OPENCLAW_MODULE.endswith("/openclaw.mjs"))

    def test_agent_receives_the_unmodified_transcript(self):
        transcript = "Check the logs, find the bottleneck, and fix it."
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b'{"text":"Done."}', b"")

        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.32)
            with patch("server.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as create:
                self.assertEqual(await service.agent_reply(transcript), "Done.")
                command = create.await_args.args
                self.assertEqual(command[command.index("--message") + 1], transcript)
                self.assertNotIn("--thinking", command)
                self.assertEqual(command[command.index("--timeout") + 1], "600")

        import asyncio
        asyncio.run(run_test())

    def test_speech_supervisor_receives_prior_conversation_history(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.remember("user", "My robot is named Atlas.")
            service.remember("assistant", "I’ll remember that.")
            response = AsyncMock()
            response.raise_for_status = lambda: None
            response.json = AsyncMock(return_value={
                "message": {"content": f"{SAY_SENTINEL} Atlas is your robot."}
            })
            context = MagicMock()
            context.__aenter__.return_value = response
            session = MagicMock()
            session.post.return_value = context
            session_context = MagicMock()
            session_context.__aenter__.return_value = session
            with patch("server.aiohttp.ClientSession", return_value=session_context):
                self.assertEqual(
                    await service.speech_reply("What is my robot’s name?"),
                    ("direct", "Atlas is your robot."),
                )
            messages = session.post.call_args.kwargs["json"]["messages"]
            self.assertEqual(messages[-3], {"role": "user", "content": "My robot is named Atlas."})
            self.assertEqual(messages[-2], {"role": "assistant", "content": "I’ll remember that."})
            self.assertEqual(messages[-1], {"role": "user", "content": "What is my robot’s name?"})

        import asyncio
        asyncio.run(run_test())

    def test_conversation_history_persists_across_service_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            first = LiveConversationService("agent:main:live-conversation", 1.15, str(path))
            first.remember("user", "Call the robot Atlas.")
            first.remember("assistant", "All right, Atlas it is.")

            second = LiveConversationService("agent:main:live-conversation", 1.15, str(path))
            self.assertEqual(second.recent_history(), [
                {"role": "user", "content": "Call the robot Atlas."},
                {"role": "assistant", "content": "All right, Atlas it is."},
            ])
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["messages"],
                second.recent_history(),
            )

    def test_agent_retries_transient_gateway_restart(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.openclaw_json = AsyncMock(side_effect=[RuntimeError("gateway connection closed"), {"text": "Done."}])
            with patch("server.asyncio.sleep", AsyncMock()) as sleep:
                self.assertEqual(await service.agent_reply("Do the work."), "Done.")
            self.assertEqual(service.openclaw_json.await_count, 2)
            sleep.assert_awaited_once_with(1)

        import asyncio
        asyncio.run(run_test())

    def test_capability_catalog_lists_only_available_model_visible_skills(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.openclaw_json = AsyncMock(side_effect=[
                [{"id": "main", "name": "Main"}, {"id": "research", "name": "Research"}],
                {"skills": [
                    {"name": "weather", "description": "Forecasts", "eligible": True, "modelVisible": True},
                    {"name": "missing", "description": "Unavailable", "eligible": False, "modelVisible": True},
                ]},
            ])
            catalog = await service.refresh_capabilities()
            self.assertIn("agent research: Research", catalog)
            self.assertIn("skill weather: Forecasts", catalog)
            self.assertNotIn("missing", catalog)

        import asyncio
        asyncio.run(run_test())

    def test_agent_turn_intercepts_sentinel_without_speaking_it(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 0.85)
            socket = AsyncMock()
            service.transcribe = AsyncMock(return_value="Check the RAG app and report progress.")
            service.speech_reply = AsyncMock(return_value=("agent", "I’ll ask the agent to check that."))
            service.tts.synthesize = AsyncMock(return_value=b"\0\0" * 2400)

            handoff = await service.process_turn(socket, b"audio")

            messages = [call.args[0] for call in socket.send_json.await_args_list]
            self.assertFalse(any(message["type"] == "acknowledgment" for message in messages))
            self.assertFalse(any(AGENT_SENTINEL in str(message) for message in messages))
            self.assertEqual(service.tts.synthesize.await_args_list[0].args[0], "I’ll ask the agent to check that.")
            self.assertEqual(handoff[0], "Check the RAG app and report progress.")

        import asyncio
        asyncio.run(run_test())

    def test_page_uses_native_audio_and_same_origin_websocket(self):
        page = render_page()
        self.assertIn("OpenClawNativeAudio.startCapture", page)
        self.assertIn("location.host+'/ws'", page)
        self.assertIn("silenceMs>=600", page)
        self.assertIn("OpenClawNativeAudio.prepareAgentResponsePlayback()", page)
        self.assertIn("OpenClawNativeAudio.interruptAgentResponsePlayback()", page)
        self.assertIn("response.output_audio.delta", page)
        self.assertIn("if(recording||awaitingResponse)return", page)
        self.assertIn("if(awaitingResponse){candidateSpeechMs=0;return}", page)
        self.assertIn("candidateSpeechMs>=200", page)
        self.assertIn("responseActive=true", page)
        self.assertIn("first_pcm_enqueued", page)
        self.assertIn("pcm_delivery_done", page)

    def test_default_tts_speed_is_faster(self):
        import inspect
        from server import PersistentTtsWorker

        default = inspect.signature(PersistentTtsWorker).parameters["speed"].default
        self.assertEqual(default, 1.15)

    def test_higher_quality_kokoro_british_voice_is_selected(self):
        self.assertTrue(DEFAULT_TTS_MODEL_DIR.endswith("/kokoro-en-v0_19"))
        self.assertEqual(DEFAULT_TTS_SPEAKER_ID, 9)

    def test_display_metrics_are_normalized_for_speech(self):
        text = "- **294/299** passed — **98.33%**. [Details](https://example.com)"
        self.assertEqual(
            normalize_spoken_text(text),
            "294 out of 299 passed. 98 point 33 percent. Details",
        )

    def test_dates_are_spoken_naturally(self):
        self.assertEqual(
            normalize_spoken_text("Updated 2026-09-04 and due 10/21/2026."),
            "Updated September fourth, twenty twenty-six and due October twenty-first, twenty twenty-six.",
        )

    def test_invalid_dates_are_left_unchanged(self):
        self.assertEqual(normalize_spoken_text("Build 2026-99-42."), "Build 2026-99-42.")

    def test_dates_with_short_years_and_iso_times_are_spoken_naturally(self):
        self.assertEqual(
            normalize_spoken_text("Runs 9/4/26; published 2026-09-04T15:07:09Z."),
            "Runs September fourth, twenty twenty-six; published September fourth, twenty twenty-six at 3 oh seven P M and nine seconds.",
        )

    def test_named_dates_and_older_years_are_spoken_naturally(self):
        self.assertEqual(
            normalize_spoken_text("Opened Sep. 4, 2026; archived 21 October 1998."),
            "Opened September fourth, twenty twenty-six; archived October twenty-first, nineteen ninety-eight.",
        )

    def test_technical_display_text_is_made_speakable(self):
        text = "## Results\n- v1.0.59: >=250 ms\n- 8/10 passed | 4 GB\nSee https://example.com/raw"
        self.assertEqual(
            normalize_spoken_text(text),
            "Results. version 1 point 0 point 59: at least 250 milliseconds. 8 out of 10 passed. 4 GB See",
        )

    def test_fenced_code_is_not_read_aloud(self):
        self.assertEqual(
            normalize_spoken_text("Use this example:\n```python\nprint('hello')\n```\nThen continue."),
            "Use this example: Then continue.",
        )

    def test_long_spoken_text_is_split_on_natural_boundaries(self):
        text = "First sentence. " + ("A longer clause with several words, " * 8) + "finished."
        parts = split_spoken_text(text, max_chars=80)
        self.assertGreater(len(parts), 2)
        self.assertEqual(" ".join(parts), text)
        self.assertTrue(all(len(part) <= 80 for part in parts))

    def test_spoken_audio_is_paced_in_realtime(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 0.72)
            socket = AsyncMock()
            service.tts.synthesize = AsyncMock(return_value=b"\0" * 9600)
            with patch("server.asyncio.sleep", AsyncMock()) as sleep:
                await service.send_spoken_response(socket, "A short response.", "agent", "response-1")
            self.assertEqual(sleep.await_count, 2)
            self.assertEqual(sleep.await_args_list[0].args[0], 0.1)

        import asyncio
        asyncio.run(run_test())

if __name__ == "__main__":
    unittest.main()
