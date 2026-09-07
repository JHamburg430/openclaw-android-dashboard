import unittest
from datetime import datetime
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from server import (
    AGENT_SENTINEL,
    NEW_AGENT_SENTINEL,
    IGNORE_SENTINEL,
    SAY_SENTINEL,
    SPEECH_MODEL,
    SPEECH_MODEL_KEEP_ALIVE,
    DEFAULT_NODE_COMMAND,
    DEFAULT_OPENCLAW_MODULE,
    DEFAULT_TTS_MODEL_DIR,
    DEFAULT_TTS_SPEAKER_ID,
    LiveConversationService,
    MAX_HISTORY_MESSAGES,
    PROMPT_HISTORY_CHARS,
    direct_voice_surface_reply,
    extract_agent_text,
    has_stale_identity_confusion,
    is_gateway_status_question,
    is_wake_word,
    parse_speech_model_output,
    repair_known_transcription_errors,
    normalize_spoken_text,
    render_page,
    split_spoken_text,
    speech_model_prompt,
    summarize_gateway_status,
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
        self.assertIn("Jarvis and the Live Conversation model are the same speaker", prompt)
        self.assertIn("never ask an agent to verify whether you can hear John", prompt)

    def test_hearing_and_identity_checks_are_deterministic(self):
        self.assertEqual(
            direct_voice_surface_reply("Are you able to hear me?"),
            "Yes, I can hear you clearly.",
        )
        self.assertEqual(
            direct_voice_surface_reply("Can you actually hear me now?"),
            "Yes, I can hear you clearly.",
        )
        self.assertIn("model operating Live Conversation", direct_voice_surface_reply("Who are you?"))
        self.assertIsNone(direct_voice_surface_reply("Can you check the RAG app?"))

    def test_known_false_identity_claim_is_detected(self):
        self.assertTrue(has_stale_identity_confusion(
            "I am Jarvis, not the live conversation model itself."
        ))
        self.assertTrue(has_stale_identity_confusion(
            "The live conversation model is running within an active agent session."
        ))
        self.assertFalse(has_stale_identity_confusion(
            "I'm Jarvis, the model operating Live Conversation."
        ))

    def test_gateway_status_questions_have_deterministic_summaries(self):
        sessions = (
            "key=one | title=Audio repair | hasActiveRun=yes | updated=now\n"
            "key=two | title=Completed task | hasActiveRun=no | updated=earlier\n"
            "key=three | title=RAG review | hasActiveRun=yes | updated=now"
        )
        self.assertTrue(is_gateway_status_question(
            "Can you tell me the status of the running sessions?"
        ))
        self.assertFalse(is_gateway_status_question("Start another agent."))
        self.assertEqual(
            summarize_gateway_status(sessions),
            "2 gateway sessions are currently running: Audio repair; RAG review.",
        )
        self.assertEqual(
            summarize_gateway_status("No active sessions.", agent_pending=True),
            "One agent request from this Live Conversation is still running.",
        )

    def test_prompt_ignores_background_speech_and_exposes_capabilities(self):
        prompt = speech_model_prompt(self.now, capabilities="agent research: Research\nskill weather: Forecasts")
        self.assertIn(IGNORE_SENTINEL, prompt)
        self.assertIn("clearly not addressed to you", prompt)
        self.assertIn("grammar is imperfect", prompt)
        self.assertIn("agent research: Research", prompt)
        self.assertIn("answer capability questions quickly", prompt)

    def test_page_displays_and_updates_the_rolling_history(self):
        page = render_page()
        self.assertIn("Conversation history · last 80 messages", page)
        self.assertIn("m.type==='history'", page)
        self.assertIn("historyMessages.slice(-80)", page)
        self.assertIn("addHistory('user',pendingTranscript)", page)
        self.assertIn("addHistory('assistant',m.text)", page)
        self.assertIn("get('autostart')==='1'", page)
        self.assertIn("liveConversationStopped", page)

    def test_ignore_token_produces_no_spoken_text(self):
        self.assertEqual(parse_speech_model_output(IGNORE_SENTINEL), ("ignore", "", None))

    def test_jarvis_wake_word_matches_as_a_word(self):
        self.assertTrue(is_wake_word("Jarvis"))
        self.assertTrue(is_wake_word("Hey, Jarvis!"))
        self.assertTrue(is_wake_word("Chavez"))
        self.assertFalse(is_wake_word("The jar is over there"))
        self.assertFalse(is_wake_word("JARVISON"))

    def test_known_live_agent_transcription_error_is_repaired_for_handoff(self):
        self.assertEqual(
            repair_known_transcription_errors("Make the five agent more capable."),
            "Make the live agent more capable.",
        )
        self.assertEqual(
            repair_known_transcription_errors("Improve the five conversation model."),
            "Improve the live conversation model.",
        )
        self.assertEqual(
            repair_known_transcription_errors("There are five agents running."),
            "There are five agents running.",
        )

    def test_continuation_joins_the_previous_unfinished_agent_request(self):
        service = LiveConversationService("agent:main:live-conversation", 1.15)
        service.remember("user", "Start another agent that...")
        service.remember("assistant", "Go on. What should the agent do?")
        self.assertEqual(
            service.contextualize_agent_request(
                "reviews this conversation and improves the live model."
            ),
            "Start another agent that reviews this conversation and improves the live model.",
        )
        service = LiveConversationService("agent:main:live-conversation", 1.15)
        service.remember("user", "Please check the current agents.")
        self.assertEqual(
            service.contextualize_agent_request("Now tell me a joke."),
            "Now tell me a joke.",
        )

    def test_speech_supervisor_uses_higher_quality_local_model(self):
        self.assertEqual(SPEECH_MODEL, "qwen3.5:4b")
        self.assertEqual(SPEECH_MODEL_KEEP_ALIVE, "30m")

    def test_pending_agent_prompt_keeps_the_supervisor_conversational(self):
        prompt = speech_model_prompt(
            self.now,
            agent_pending=True,
            sessions="key=agent:main:job | title=Audio repair | hasActiveRun=yes",
        )
        self.assertIn("still awaiting a final response", prompt)
        self.assertIn("Multiple agents may work concurrently", prompt)
        self.assertIn("hasActiveRun=yes", prompt)
        self.assertIn(NEW_AGENT_SENTINEL, prompt)

    def test_server_has_no_synthetic_periodic_agent_updates(self):
        import inspect
        import server

        source = inspect.getsource(server.websocket)
        self.assertNotIn("agent_progress_reply", source)
        self.assertNotIn("timeout=20", source)

    def test_speech_model_output_intercepts_escalation_token(self):
        self.assertEqual(
            parse_speech_model_output(f"{AGENT_SENTINEL} I’ll check that."),
            ("agent", "I’ll check that.", None),
        )
        self.assertEqual(
            parse_speech_model_output(f"{NEW_AGENT_SENTINEL} I’ll start another."),
            ("new_agent", "I’ll start another.", None),
        )
        self.assertEqual(
            parse_speech_model_output(
                "[[OPENCLAW_SESSION:agent:main:dashboard:abc]] I’ll add that."
            ),
            ("session", "I’ll add that.", "agent:main:dashboard:abc"),
        )
        self.assertEqual(
            parse_speech_model_output(f"{SAY_SENTINEL} It is 12:34 PM."),
            ("direct", "It is 12:34 PM.", None),
        )
        self.assertEqual(
            parse_speech_model_output("It is 12:34 PM."),
            ("direct", "It is 12:34 PM.", None),
        )

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

    def test_agent_can_target_an_exact_existing_session(self):
        async def run_test():
            target = "agent:main:dashboard:abc"
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.openclaw_json = AsyncMock(return_value={"text": "Added."})
            self.assertEqual(await service.agent_reply("Also check audio.", target), "Added.")
            arguments = service.openclaw_json.await_args.args
            self.assertEqual(arguments[arguments.index("--session-key") + 1], target)

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
                    ("direct", "Atlas is your robot.", None),
                )
            messages = session.post.call_args.kwargs["json"]["messages"]
            self.assertEqual(messages[-3], {"role": "user", "content": "My robot is named Atlas."})
            self.assertEqual(messages[-2], {"role": "assistant", "content": "I’ll remember that."})
            self.assertEqual(messages[-1], {"role": "user", "content": "What is my robot’s name?"})

        import asyncio
        asyncio.run(run_test())

    def test_false_identity_history_is_not_sent_to_supervisor(self):
        service = LiveConversationService("agent:main:live-conversation", 1.15)
        service.remember("user", "Are you able to hear me?")
        service.remember(
            "assistant",
            "I am Jarvis, not the live conversation model itself.",
        )
        service.remember("user", "What can you do?")
        self.assertEqual(
            service.prompt_history(),
            [
                {"role": "user", "content": "Are you able to hear me?"},
                {"role": "user", "content": "What can you do?"},
            ],
        )

    def test_speech_reply_bypasses_model_for_hearing_check(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            with patch("server.aiohttp.ClientSession") as client_session:
                self.assertEqual(
                    await service.speech_reply("Are you able to hear me?"),
                    ("direct", "Yes, I can hear you clearly.", None),
                )
                client_session.assert_not_called()

        import asyncio
        asyncio.run(run_test())

    def test_speech_reply_bypasses_model_for_gateway_status(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.sessions = "key=one | title=Audio repair | hasActiveRun=yes"
            service.sessions_updated_at = time.monotonic()
            with patch("server.aiohttp.ClientSession") as client_session:
                self.assertEqual(
                    await service.speech_reply("What sessions are running?"),
                    ("direct", "1 gateway session is currently running: Audio repair.", None),
                )
                client_session.assert_not_called()

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

    def test_long_history_is_retained_but_prompt_selection_is_budgeted(self):
        service = LiveConversationService("agent:main:live-conversation", 1.15)
        for index in range(MAX_HISTORY_MESSAGES):
            service.remember("user", f"turn {index} " + ("x" * 700))
        prompt_history = service.prompt_history()
        self.assertGreater(len(service.recent_history()), 24)
        self.assertLessEqual(sum(len(item["content"]) for item in prompt_history), PROMPT_HISTORY_CHARS)
        self.assertEqual(prompt_history[-1]["content"], service.recent_history()[-1]["content"])

    def test_completed_agent_reply_can_be_spoken_after_reconnect(self):
        service = LiveConversationService("agent:main:live-conversation", 1.15)
        service.queue_pending_reply("The background repair is complete.")
        self.assertEqual(
            list(service.pending_spoken_replies),
            ["The background repair is complete."],
        )

        import inspect
        import server
        source = inspect.getsource(server.websocket)
        self.assertIn("Agent tasks deliberately survive a WebView disconnect", source)
        self.assertIn("agent-reconnect-", source)

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

    def test_live_session_catalog_exposes_authoritative_status_and_keys(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.openclaw_json = AsyncMock(return_value={"sessions": [{
                "key": "agent:main:dashboard:abc",
                "displayName": "Audio repair",
                "hasActiveRun": True,
                "updatedAt": 1788819000000,
                "lastMessagePreview": "Inspecting the playback queue.",
            }]})
            catalog = await service.refresh_sessions()
            self.assertIn("key=agent:main:dashboard:abc", catalog)
            self.assertIn("hasActiveRun=yes", catalog)
            self.assertIn("Inspecting the playback queue", catalog)
            self.assertIn("agent:main:dashboard:abc", service.session_keys)

        import asyncio
        asyncio.run(run_test())

    def test_speech_reply_accepts_only_real_session_keys(self):
        async def decide(model_text):
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.sessions = "key=agent:main:dashboard:abc | hasActiveRun=yes"
            service.session_keys = {"agent:main:dashboard:abc"}
            service.sessions_updated_at = time.monotonic()
            service.capabilities_updated_at = time.monotonic()
            response = AsyncMock()
            response.raise_for_status = lambda: None
            response.json = AsyncMock(return_value={"message": {"content": model_text}})
            context = MagicMock()
            context.__aenter__.return_value = response
            session = MagicMock()
            session.post.return_value = context
            session_context = MagicMock()
            session_context.__aenter__.return_value = session
            with patch("server.aiohttp.ClientSession", return_value=session_context):
                return await service.speech_reply("Add the reconnect check.")

        import asyncio
        self.assertEqual(
            asyncio.run(decide(
                "[[OPENCLAW_SESSION:agent:main:dashboard:abc]] I’ll add that."
            )),
            ("session", "I’ll add that.", "agent:main:dashboard:abc"),
        )
        self.assertEqual(
            asyncio.run(decide(
                "[[OPENCLAW_SESSION:agent:main:invented]] I’ll add that."
            )),
            ("agent", "I’ll add that.", None),
        )

    def test_start_uses_the_more_accurate_cached_whisper_model(self):
        import inspect
        source = inspect.getsource(LiveConversationService.start)
        self.assertIn('model="small.en"', source)
        self.assertNotIn('model="tiny.en"', source)

    def test_agent_turn_intercepts_sentinel_without_speaking_it(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 0.85)
            socket = AsyncMock()
            service.transcribe = AsyncMock(return_value="Check the RAG app and report progress.")
            service.speech_reply = AsyncMock(
                return_value=("agent", "I’ll ask the agent to check that.", None)
            )
            service.tts.synthesize = AsyncMock(return_value=b"\0\0" * 2400)

            handoff = await service.process_turn(socket, b"audio")

            messages = [call.args[0] for call in socket.send_json.await_args_list]
            self.assertFalse(any(message["type"] == "acknowledgment" for message in messages))
            self.assertFalse(any(AGENT_SENTINEL in str(message) for message in messages))
            self.assertEqual(service.tts.synthesize.await_args_list[0].args[0], "I’ll ask the agent to check that.")
            self.assertEqual(
                handoff,
                (
                    "Check the RAG app and report progress.",
                    "I’ll ask the agent to check that.",
                    "agent:main:live-conversation",
                ),
            )

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
        self.assertIn("candidateSpeechMs>=speechRequiredMs", page)
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

    def test_short_sentences_are_packed_to_avoid_tts_underruns(self):
        text = "First short sentence. Second short sentence. Third short sentence."
        self.assertEqual(split_spoken_text(text), [text])

    def test_playback_vad_rejects_echo_and_covers_native_tail(self):
        page = render_page()
        self.assertIn("const speechThreshold=responseActive?.025:.006", page)
        self.assertIn("const speechRequiredMs=responseActive?600:200", page)
        self.assertIn("responseTailTimer=setTimeout(()=>{responseActive=false;responseTailTimer=null},1500)", page)
        self.assertIn("report('barge_in','confirmed_user_speech')", page)
        self.assertIn("}send({type:'input_audio_buffer.speech_started'});send({type:'start'})", page)

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

    def test_confirmed_barge_in_stops_a_long_server_stream(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 0.72)
            socket = AsyncMock()
            service.tts.synthesize = AsyncMock(return_value=b"\0" * 24_000)

            async def interrupt_after_first_chunk(_):
                service.interrupt_speech()

            with patch("server.asyncio.sleep", AsyncMock(side_effect=interrupt_after_first_chunk)) as sleep:
                await service.send_spoken_response(socket, "A long response.", "agent", "response-1")

            self.assertEqual(sleep.await_count, 1)
            payloads = [call.args[0] for call in socket.send_json.await_args_list]
            self.assertEqual(
                sum(payload.get("type") == "response.output_audio.delta" for payload in payloads),
                1,
            )
            self.assertEqual(payloads[-1]["type"], "response.output_audio.done")

        import asyncio
        asyncio.run(run_test())

if __name__ == "__main__":
    unittest.main()
