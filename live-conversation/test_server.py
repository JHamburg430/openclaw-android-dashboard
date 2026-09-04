import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from server import (
    AGENT_SENTINEL,
    DEFAULT_NODE_COMMAND,
    DEFAULT_OPENCLAW_MODULE,
    LiveConversationService,
    extract_agent_text,
    parse_speech_model_output,
    render_page,
    speech_model_prompt,
)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 2, 12, 34, tzinfo=ZoneInfo("America/Detroit"))

    def test_speech_model_prompt_defines_hidden_escalation_contract(self):
        prompt = speech_model_prompt(self.now)
        self.assertIn(AGENT_SENTINEL, prompt)
        self.assertIn("12:34 PM", prompt)
        self.assertNotIn("I'll check and let you know", prompt)

    def test_speech_model_output_intercepts_escalation_token(self):
        self.assertIsNone(parse_speech_model_output(AGENT_SENTINEL))
        self.assertIsNone(parse_speech_model_output(f"  {AGENT_SENTINEL}  "))
        self.assertEqual(parse_speech_model_output("It is 12:34 PM."), "It is 12:34 PM.")

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

    def test_agent_turn_intercepts_sentinel_without_speaking_it(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 0.85)
            socket = AsyncMock()
            service.transcribe = AsyncMock(return_value="Check the RAG app and report progress.")
            service.speech_reply = AsyncMock(return_value=None)
            service.agent_reply = AsyncMock(return_value="The evaluation is complete.")
            service.tts.synthesize = AsyncMock(return_value=b"\0\0" * 2400)

            await service.process_turn(socket, b"audio")

            messages = [call.args[0] for call in socket.send_json.await_args_list]
            self.assertFalse(any(message["type"] == "acknowledgment" for message in messages))
            self.assertFalse(any(AGENT_SENTINEL in str(message) for message in messages))
            self.assertEqual(service.tts.synthesize.await_args_list[0].args[0], "The evaluation is complete.")

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

    def test_default_tts_speed_is_normal(self):
        import inspect
        from server import PersistentTtsWorker

        default = inspect.signature(PersistentTtsWorker).parameters["speed"].default
        self.assertEqual(default, 0.85)

if __name__ == "__main__":
    unittest.main()
