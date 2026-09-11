import unittest
from datetime import datetime
import json
from pathlib import Path
import tempfile
import time
import wave
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from server import (
    AGENT_SENTINEL,
    BACKCHANNEL_DISPLAY_TEXT,
    NEW_AGENT_SENTINEL,
    IGNORE_SENTINEL,
    SAY_SENTINEL,
    SPEECH_MODEL,
    SPEECH_MODEL_CONTEXT,
    SPEECH_MODEL_KEEP_ALIVE,
    SPEECH_MODEL_URL,
    SPEECH_NUM_PREDICT,
    SPEECH_RETRY_NUM_PREDICT,
    DEFAULT_NODE_COMMAND,
    DEFAULT_OPENCLAW_MODULE,
    DEFAULT_TTS_MODEL_DIR,
    DEFAULT_TTS_SPEAKER_ID,
    DEFAULT_QWEN_INITIAL_CHUNK_FRAMES,
    LiveConversationService,
    IncrementalSpeechStream,
    OnlineTranscript,
    QwenVllmTtsClient,
    TurnDecision,
    TurnUnderstanding,
    MAX_HISTORY_MESSAGES,
    PROMPT_HISTORY_CHARS,
    confirmation_answer,
    confirmation_policy_command,
    configured_tts_backend,
    conversation_entities,
    affirmative_hum_pcm,
    direct_voice_surface_reply,
    extract_agent_acknowledgment,
    extract_agent_text,
    has_stale_identity_confusion,
    has_explicit_action_request,
    is_explicit_new_agent_request,
    is_gateway_status_question,
    is_referential_agent_question,
    is_operational_acknowledgment,
    is_silent_stop_command,
    is_wake_word,
    parse_speech_model_output,
    parse_turn_understanding,
    partial_structured_reply,
    repair_known_transcription_errors,
    normalize_spoken_text,
    latest_assistant_text,
    remove_unrequested_action_promises,
    requires_tool_backed_action,
    requires_authoritative_lookup,
    render_page,
    split_spoken_text,
    speech_model_prompt,
    summarize_gateway_status,
    summarize_tracked_agent,
    tts_speed_for,
)


def semantic_decision(
    route="direct", reply="I understand.", *, actionable=False,
    grounding=False, complete=True, confidence=0.98, speech_act="statement",
    relation="new", supersedes=False, clarification=False, assembled_text="", session_key="",
):
    return {
        "route": route,
        "reply": reply,
        "session_key": session_key,
        "complete": complete,
        "confidence": confidence,
        "speech_act": speech_act,
        "relation": relation,
        "actionable": actionable,
        "requires_grounding": grounding,
        "supersedes_previous": supersedes,
        "clarification_needed": clarification,
        "assembled_text": assembled_text or "Complete turn.",
        "reason": "Contextual semantic classification.",
    }


def mock_semantic_model(*decisions):
    response = AsyncMock()
    response.raise_for_status = lambda: None
    payloads = [
        {"message": {"content": item if isinstance(item, str) else json.dumps(item)}}
        for item in decisions
    ]
    if len(payloads) == 1:
        response.json = AsyncMock(return_value=payloads[0])
    else:
        response.json = AsyncMock(side_effect=payloads)
    response_context = MagicMock()
    response_context.__aenter__.return_value = response
    session = MagicMock()
    session.post.return_value = response_context
    session_context = MagicMock()
    session_context.__aenter__.return_value = session
    return session_context, session


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 2, 12, 34, tzinfo=ZoneInfo("America/Detroit"))

    def test_speech_model_prompt_defines_structured_routing_contract(self):
        prompt = speech_model_prompt(self.now)
        self.assertIn('"route":"agent"', prompt)
        self.assertIn('"route":"direct"', prompt)
        self.assertIn('"session_key"', prompt)
        self.assertIn("12:34 PM", prompt)
        self.assertNotIn("I'll check and let you know", prompt)
        self.assertIn("Jarvis and the Live Conversation model are the same speaker", prompt)
        self.assertIn("never ask an agent to verify whether you can hear John", prompt)
        self.assertIn("Conversation history is memory for continuity", prompt)
        self.assertIn("Refresh the authoritative source", prompt)
        self.assertIn("dispatches the action while the acknowledgment is spoken", prompt)
        self.assertIn("MICRO: use one word or one short clause", prompt)
        self.assertIn("BRIEF: this is the default", prompt)
        self.assertIn("SUMMARY: for status, comparisons, or multi-part results", prompt)
        self.assertIn("DETAILED: use only when John asks for detail", prompt)
        self.assertIn("develop one idea per short", prompt)
        self.assertIn("periods separate complete", prompt)

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

    def test_degraded_router_answers_captured_test_turn_without_full_model(self):
        async def run_test():
            service = LiveConversationService("agent:main:degraded-test", 1.15)
            service.speech_model_accelerated = False
            with patch("server.aiohttp.ClientSession") as session:
                route, reply, target = await service.speech_reply(
                    "testing out the live conversation."
                )
            self.assertEqual((route, reply, target), (
                "direct", "I hear you. Go ahead with the test.", None
            ))
            session.assert_not_called()

        import asyncio
        asyncio.run(run_test())

    def test_degraded_router_preserves_explicit_action_authorization(self):
        async def run_test():
            service = LiveConversationService("agent:main:degraded-action", 1.15)
            service.speech_model_accelerated = False
            route, reply, target = await service.speech_reply(
                "Task an agent with fixing the live conversation."
            )
            self.assertEqual(route, "agent")
            self.assertIn("agent", reply)
            self.assertIsNone(target)

        import asyncio
        asyncio.run(run_test())

    def test_degraded_router_keeps_polite_knowledge_request_direct(self):
        async def run_test():
            service = LiveConversationService("agent:main:degraded-question", 1.15)
            service.speech_model_accelerated = False
            session_context, _ = mock_semantic_model("The sky appears blue.")
            with patch("server.aiohttp.ClientSession", return_value=session_context):
                route, reply, target = await service.speech_reply(
                    "Please answer briefly. What colour is a clear daytime sky?"
                )
            self.assertEqual((route, reply, target), (
                "direct", "The sky appears blue.", None
            ))

        import asyncio
        asyncio.run(run_test())

    def test_degraded_tool_action_classifier_separates_answers_from_operations(self):
        self.assertFalse(requires_tool_backed_action(
            "Please answer briefly. What is two plus two?"
        ))
        self.assertTrue(requires_tool_backed_action("Please fix the response latency."))
        self.assertTrue(requires_tool_backed_action("Can you check the service logs?"))
        self.assertTrue(requires_tool_backed_action(
            "Task an agent with fixing the live conversation."
        ))

    def test_adversarial_routing_corpus_preserves_least_powerful_route(self):
        conversational = (
            "Please answer briefly. What color is the sky?",
            "Could you explain acoustic echo cancellation?",
            "Would you define semantic endpointing?",
            "Please name the largest planet.",
            "I'm testing the agent status indicator.",
            "Don't fix anything yet.",
            "The dashboard needs no changes.",
        )
        operational = (
            "Please investigate the audio cutoff.",
            "Would you check the Android logs?",
            "Set an alarm for six tomorrow.",
            "Find the latest release notes.",
            "Task an agent with reviewing this conversation.",
        )
        for transcript in conversational:
            with self.subTest(route="direct", transcript=transcript):
                self.assertFalse(requires_tool_backed_action(transcript))
        for transcript in operational:
            with self.subTest(route="tool", transcript=transcript):
                self.assertTrue(requires_tool_backed_action(transcript))

    def test_agent_status_word_collisions_never_consume_delegations(self):
        status_questions = (
            "What agents are currently running?",
            "Give me the status of active agent sessions.",
            "Are any agent tasks still working?",
        )
        delegations = (
            "Task an agent with fixing the status indicator.",
            "Assign an agent to update the session status display.",
            "Please have an agent review the active-task progress UI.",
        )
        for transcript in status_questions:
            with self.subTest(kind="status", transcript=transcript):
                self.assertTrue(is_gateway_status_question(transcript))
        for transcript in delegations:
            with self.subTest(kind="delegation", transcript=transcript):
                self.assertFalse(is_gateway_status_question(transcript))
                self.assertTrue(has_explicit_action_request(transcript))

    def test_semantic_router_repairs_timeless_explanation_misclassified_as_action(self):
        async def run_test():
            service = LiveConversationService("agent:main:direct-explanation", 1.15)
            service.sessions_updated_at = time.monotonic()
            service.capabilities_updated_at = time.monotonic()
            decision = semantic_decision(
                "agent", "I'll have the agent explain it.", actionable=True,
                speech_act="request", assembled_text=(
                    "Please explain photosynthesis in five sentences."
                ),
            )
            client, _ = mock_semantic_model(
                decision, "Photosynthesis converts light into chemical energy."
            )
            with patch("server.aiohttp.ClientSession", return_value=client):
                route, reply, target = await service.speech_reply(
                    "Please explain photosynthesis in five sentences."
                )
            self.assertEqual((route, reply, target), (
                "direct", "Photosynthesis converts light into chemical energy.", None
            ))

        import asyncio
        asyncio.run(run_test())

    def test_online_transcript_separates_stable_and_revisable_prefixes(self):
        state = OnlineTranscript.empty()
        stable, unstable = state.update("please ask the agent to review", 32_000)
        self.assertEqual(stable, "")
        self.assertEqual(unstable, "please ask the agent to review")
        stable, unstable = state.update(
            "please ask the agent to review the voice logs", 48_000
        )
        self.assertEqual(stable, "please ask the agent")
        self.assertEqual(unstable, "to review the voice logs")
        self.assertEqual(
            state.final("to review the voice logs carefully"),
            "please ask the agent to review the voice logs carefully",
        )

    def test_final_online_decode_includes_full_onset_for_normal_turn(self):
        async def run_test():
            service = LiveConversationService("agent:main:test", 1.15)
            service.transcribe = AsyncMock(
                return_value="Please review the voice logs now."
            )
            state = OnlineTranscript(
                stable_words=["Please", "review"],
                previous_words=["the", "voice", "logs"],
                unstable_words=["the", "voice", "logs"],
                decode_from_sample=16_000,
            )
            audio = b"\0\0" * (SAMPLE_RATE * 4)

            transcript = await service.finalize_online_transcript(audio, state)

            self.assertEqual(transcript, "Please review the voice logs now.")
            service.transcribe.assert_awaited_once_with(audio, purpose="final")

        import asyncio
        from server import SAMPLE_RATE
        asyncio.run(run_test())

    def test_final_online_decode_includes_full_onset_for_long_slow_turn(self):
        async def run_test():
            service = LiveConversationService("agent:main:test", 1.15)
            service.transcribe = AsyncMock(return_value="The complete slow sentence.")
            state = OnlineTranscript(
                stable_words=["The", "complete"], previous_words=["slow"],
                unstable_words=["slow"], decode_from_sample=160_000,
            )
            from server import SAMPLE_RATE
            audio = b"\0\0" * (SAMPLE_RATE * 22)
            transcript = await service.finalize_online_transcript(audio, state)
            self.assertEqual(transcript, "The complete slow sentence.")
            service.transcribe.assert_awaited_once_with(audio, purpose="final")

        import asyncio
        asyncio.run(run_test())

    def test_semantic_endpoint_vetoes_complete_acoustic_prediction(self):
        async def run_test():
            service = LiveConversationService("agent:main:test", 1.15)
            service.semantic_turn = MagicMock()
            service.semantic_turn.predict.return_value = TurnDecision(
                True, 0.97, "smart-turn-v3.2"
            )
            service.semantic_endpoint_decision = AsyncMock(return_value=(False, 0.99))

            decision = await service.endpoint_decision(
                b"speech", "Do not answer yet since I still need to ask"
            )

            self.assertFalse(decision.complete)
            self.assertEqual(decision.source, "smart-turn-v3.2+semantic")
            service.semantic_endpoint_decision.assert_awaited_once()

        import asyncio
        asyncio.run(run_test())

    def test_semantic_endpoint_failure_waits_for_hard_endpoint(self):
        async def run_test():
            service = LiveConversationService("agent:main:test", 1.15)
            service.semantic_turn = MagicMock()
            service.semantic_turn.predict.return_value = TurnDecision(
                True, 0.94, "smart-turn-v3.2"
            )
            service.semantic_endpoint_decision = AsyncMock(return_value=None)

            decision = await service.endpoint_decision(b"speech", "A possible turn")

            self.assertFalse(decision.complete)
            self.assertEqual(decision.source, "smart-turn-v3.2+semantic-error")

        import asyncio
        asyncio.run(run_test())

    def test_semantic_endpoint_reuses_warm_supervisor_context(self):
        import inspect
        import server

        source = inspect.getsource(server.LiveConversationService.semantic_endpoint_decision)
        self.assertIn('"num_ctx": SPEECH_MODEL_CONTEXT', source)
        self.assertNotIn('"num_ctx": 1024', source)

    def test_semantic_endpoint_uses_minimal_decision_contract(self):
        import server

        self.assertEqual(
            set(server.SEMANTIC_ENDPOINT_SCHEMA["required"]),
            {"complete", "confidence"},
        )
        self.assertNotIn("reason", server.SEMANTIC_ENDPOINT_SCHEMA["properties"])

    def test_all_speech_model_paths_reuse_one_context_size(self):
        import inspect
        import server

        for method in (
            server.LiveConversationService.semantic_endpoint_decision,
            server.LiveConversationService.semantic_fragment_resolution,
            server.LiveConversationService.generate_direct_answer,
        ):
            source = inspect.getsource(method)
            self.assertIn('"num_ctx": SPEECH_MODEL_CONTEXT', source)
            self.assertNotRegex(source, r'"num_ctx":\s*\d')

    def test_semantic_controller_requests_repeat_for_unclear_committed_speech(self):
        async def run_test():
            service = LiveConversationService("agent:main:test", 1.15)
            service.sessions_updated_at = time.monotonic()
            service.capabilities_updated_at = time.monotonic()
            decision = semantic_decision(
                "direct", "I didn't catch that clearly. Could you say it again?",
                complete=False, confidence=0.92, speech_act="correction",
                relation="correction", clarification=True,
                assembled_text="instead of saying the same thing say the same thing",
            )
            client, _ = mock_semantic_model(decision)
            with patch("server.aiohttp.ClientSession", return_value=client):
                result = await service.speech_reply(
                    "instead of saying the same thing say the same thing"
                )
            self.assertEqual(result, (
                "direct", "I didn't catch that clearly. Could you say it again?", None
            ))
            self.assertEqual(service.pending_fragment, "")

        import asyncio
        asyncio.run(run_test())

    def test_controller_diagnosis_repairs_inconsistent_clarification_flag(self):
        decision = semantic_decision(
            "direct", "", complete=False, confidence=0.95,
            speech_act="correction", relation="correction", clarification=False,
            assembled_text="a damaged correction",
        )
        decision["reason"] = (
            "The wording is semantically contradictory and indicates a recognition "
            "error, so it warrants clarification."
        )
        understanding = parse_turn_understanding(
            json.dumps(decision), "a damaged correction"
        )
        self.assertTrue(understanding.clarification_needed)

    def test_semantic_controller_accepts_fenced_schema_json(self):
        decision = semantic_decision(
            "direct", "The sky is blue.", speech_act="answer",
            assembled_text="What colour is the sky?",
        )
        understanding = parse_turn_understanding(
            f"```json\n{json.dumps(decision)}\n```",
            "What colour is the sky?",
        )
        self.assertTrue(understanding.complete)
        self.assertEqual(understanding.speech_act, "answer")

    def test_semantic_controller_retries_non_json_with_real_schema_request(self):
        async def run_test():
            service = LiveConversationService("agent:main:test", 1.15)
            service.sessions_updated_at = time.monotonic()
            service.capabilities_updated_at = time.monotonic()
            valid = semantic_decision(
                "direct", "The sky is blue.", speech_act="answer",
                assembled_text="What colour is the sky?",
            )
            client, session = mock_semantic_model("not json", valid)
            with patch("server.aiohttp.ClientSession", return_value=client):
                result = await service.speech_reply("What colour is the sky?")
            self.assertEqual(result, ("direct", "The sky is blue.", None))
            self.assertEqual(session.post.call_count, 2)
            repair_request = session.post.call_args_list[1].kwargs["json"]
            self.assertFalse(repair_request["stream"])
            self.assertIn("previous response was not valid JSON", repair_request["messages"][-1]["content"])

        import asyncio
        asyncio.run(run_test())

    def test_complete_direct_turn_recovers_missing_controller_reply(self):
        async def run_test():
            service = LiveConversationService("agent:main:test", 1.15)
            service.sessions_updated_at = time.monotonic()
            service.capabilities_updated_at = time.monotonic()
            decision = semantic_decision(
                "direct", "", speech_act="question",
                assembled_text="What colour is the sky?",
            )
            client, _ = mock_semantic_model(decision)
            service.generate_direct_answer = AsyncMock(return_value="The sky is blue.")
            with patch("server.aiohttp.ClientSession", return_value=client):
                result = await service.speech_reply("What colour is the sky?")
            self.assertEqual(result, ("direct", "The sky is blue.", None))
            service.generate_direct_answer.assert_awaited_once_with(
                "What colour is the sky?"
            )

        import asyncio
        asyncio.run(run_test())

    def test_backchannel_uses_generated_nonverbal_audio_without_tts_text(self):
        async def run_test():
            service = LiveConversationService("agent:main:test", 1.15)
            service.tts.synthesize = AsyncMock(return_value=b"")
            socket = AsyncMock()
            hum = affirmative_hum_pcm()
            await service.send_spoken_response(
                socket, "", "backchannel", "bc-1",
                message_type="backchannel", display_text=BACKCHANNEL_DISPLAY_TEXT,
                pcm_override=hum,
            )
            service.tts.synthesize.assert_not_awaited()
            first = socket.send_json.await_args_list[0].args[0]
            self.assertEqual(first["type"], "backchannel")
            self.assertEqual(first["text"], "")
            self.assertGreater(len(hum), 20_000)
            self.assertNotEqual(hum, bytes(len(hum)))

        import asyncio
        asyncio.run(run_test())

    def test_conversation_entity_graph_and_dynamic_cadence(self):
        entities = conversation_entities(
            "Have an agent review Manuals RAG retrieval failures"
        )
        self.assertIn("manuals", entities["topics"])
        self.assertIn("Manuals", entities["named_entities"])
        self.assertLess(
            tts_speed_for("I'm sorry, that request failed.", base=1.15), 1.15
        )
        self.assertGreater(
            tts_speed_for("Yes?", base=1.15), 1.15
        )

    def test_page_exposes_direct_confirmation_control(self):
        page = render_page()
        self.assertIn("Toggle confirmation", page)
        self.assertIn("type:'set_confirmation'", page)

    def test_stable_partial_prefetch_is_read_only(self):
        async def run_test():
            service = LiveConversationService("agent:main:test", 1.15)
            service.refresh_sessions = AsyncMock(return_value="fresh")
            service.refresh_capabilities = AsyncMock(return_value="fresh")
            self.assertEqual(
                await service.prefetch_for_partial(
                    "what is the latest status of that agent"
                ),
                "sessions",
            )
            service.refresh_sessions.assert_awaited_once_with(force=True)
            self.assertIsNone(
                await service.prefetch_for_partial("start another agent now")
            )

        import asyncio
        asyncio.run(run_test())

    def test_spoken_stop_commands_are_silent_controls(self):
        for transcript in (
            "Jarvis stop.", "Hey Jarvis, stop talking please.", "Be quiet.",
            "Stop speaking, Jarvis.", "That's enough.", "Jarvis, please stop.",
            "Could you stop talking now?", "Please be quiet, Jarvis.",
        ):
            self.assertTrue(is_silent_stop_command(transcript), transcript)
        self.assertFalse(is_silent_stop_command("Jarvis, stop the agent."))
        self.assertFalse(is_silent_stop_command("Why did you stop talking?"))

    def test_testing_statements_are_left_to_semantic_controller(self):
        self.assertIsNone(
            direct_voice_surface_reply("Testing out the latest live conversation updates.")
        )
        self.assertIsNone(
            direct_voice_surface_reply("I'm just trying out the new audio behavior.")
        )
        self.assertIsNone(
            direct_voice_surface_reply("I'm testing the update; have an agent monitor the logs.")
        )

    def test_action_confirmation_voice_commands(self):
        self.assertEqual(
            confirmation_policy_command("Always ask me before taking actions."), "confirm"
        )
        self.assertEqual(
            confirmation_policy_command("Don't ask for confirmation before actions."),
            "automatic",
        )
        self.assertEqual(
            confirmation_policy_command("What is the confirmation setting?"), "status"
        )
        self.assertEqual(confirmation_policy_command("Turn confirmation on."), "confirm")
        self.assertEqual(confirmation_policy_command("Please switch confirmation off."), "automatic")
        self.assertEqual(
            confirmation_policy_command("Task an agent with turning action confirmation on."),
            "confirm",
        )
        self.assertIsNone(confirmation_policy_command("Please confirm the meeting time."))
        self.assertTrue(confirmation_answer("Go ahead."))
        self.assertFalse(confirmation_answer("No, cancel that."))
        self.assertIsNone(confirmation_answer("Yes, but change the request first."))

    def test_event_ledger_migrates_and_persists_structured_turn_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            path.write_text(json.dumps({
                "messages": [{"role": "user", "content": "Legacy request"}],
            }))
            service = LiveConversationService(
                "agent:main:live-conversation", 1.15, history_path=str(path)
            )
            service.remember(
                "assistant", "Current reply", turn_id="turn-1",
                metadata={"route": "direct"},
            )
            payload = json.loads(path.read_text())

            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["events"][0]["provenance"], "legacy")
            self.assertEqual(payload["events"][-1]["turn_id"], "turn-1")
            self.assertEqual(payload["events"][-1]["metadata"]["route"], "direct")
            self.assertEqual(service.recent_history()[-1], {
                "role": "assistant", "content": "Current reply",
            })

    def test_semantic_assembler_routes_fragmented_action_as_one_turn(self):
        service = LiveConversationService("agent:main:live-conversation", 1.15)
        service.remember("user", "Can you task an agent with...")

        assembled = service.assemble_turn_text("checking on the audio cutoff?")

        self.assertEqual(
            assembled,
            "Can you task an agent with checking on the audio cutoff?",
        )
        self.assertTrue(has_explicit_action_request(assembled))

    def test_unrelated_complete_turn_is_not_merged(self):
        service = LiveConversationService("agent:main:live-conversation", 1.15)
        service.remember("user", "Tell me the current time.")

        self.assertEqual(
            service.assemble_turn_text("Also explain the date."),
            "Also explain the date.",
        )

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

    def test_operational_acknowledgments_are_not_reused_as_routing_examples(self):
        self.assertTrue(is_operational_acknowledgment(
            "I'll have the agent monitor the latest updates during your testing."
        ))
        self.assertFalse(is_operational_acknowledgment(
            "I hear you. Go ahead with the test."
        ))
        self.assertEqual(
            remove_unrequested_action_promises(
                "I understand. Road noise caused the mistake. "
                "I will keep the sessions active and continue monitoring the conversation."
            ),
            "I understand. Road noise caused the mistake.",
        )
        self.assertEqual(
            remove_unrequested_action_promises(
                "I understand. Road noise is interfering. "
                "I'll adjust the noise-floor threshold to improve it."
            ),
            "I understand. Road noise is interfering.",
        )
        service = LiveConversationService("agent:main:live-conversation", 1.15)
        service.remember("user", "Testing out the latest updates.")
        service.remember("assistant", "I'll have the agent monitor the updates.")
        service.remember("user", "The transcript is visible now.")
        service.remember("assistant", "I can see that too.")
        self.assertEqual(service.prompt_history(), [
            {"role": "user", "content": "Testing out the latest updates."},
            {"role": "user", "content": "The transcript is visible now."},
            {"role": "assistant", "content": "I can see that too."},
        ])

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
        self.assertFalse(is_gateway_status_question(
            "Task another agent with updating the Conversation Diagnostics status "
            "indicator to have an acknowledge button when a correction has completed, "
            "but no new install is required."
        ))
        self.assertTrue(is_referential_agent_question("How is this agent running?"))
        self.assertTrue(is_referential_agent_question("Tell that agent to check audio too."))
        self.assertEqual(
            summarize_gateway_status(sessions),
            "2 gateway sessions are currently running: Audio repair; RAG review.",
        )

    def test_failed_forced_session_refresh_is_reported_as_unverified(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.sessions = "key=old | title=Old | hasActiveRun=yes"
            service.sessions_last_success_wall = time.time() - 75
            service.openclaw_json = AsyncMock(side_effect=RuntimeError("gateway unavailable"))

            route, reply, target = await service.speech_reply("What sessions are running?")

            self.assertEqual(route, "direct")
            self.assertIsNone(target)
            self.assertIn("couldn't refresh", reply)
            self.assertIn("won't present it as current", reply)
            self.assertNotIn("Old", reply)

        import asyncio
        asyncio.run(run_test())

    def test_tracked_agent_summary_preserves_the_requested_task(self):
        tracked = {
            "session_key": "agent:main:live-conversation-cutoff-123",
            "request": "fix the issue where your messages are being cut off",
            "state": "running",
        }
        self.assertEqual(
            summarize_tracked_agent(tracked, {"hasActiveRun": True}),
            "The agent assigned to this task is still running: fix the issue where your messages are being cut off.",
        )
        self.assertEqual(
            summarize_gateway_status("No active sessions.", agent_pending=True),
            "One agent request from this Live Conversation is still running.",
        )

    def test_referent_resolution_uses_topic_across_multiple_recent_agents(self):
        service = LiveConversationService("agent:main:live-conversation", 1.15)
        service.register_agent_session("agent:main:audio", "Fix the audio cutoff and echo")
        service.register_agent_session("agent:main:rag", "Improve Manuals RAG retrieval")

        self.assertEqual(
            service.resolve_agent_session("Tell the audio agent to test echo too")["session_key"],
            "agent:main:audio",
        )
        self.assertEqual(
            service.resolve_agent_session("How is that agent doing?")["session_key"],
            "agent:main:rag",
        )

    def test_complete_explicit_agent_commands_bypass_model_routing(self):
        self.assertTrue(is_explicit_new_agent_request(
            "Spawn another agent to work on the transcription problem."
        ))
        self.assertTrue(is_explicit_new_agent_request(
            "Start an agent to update the Android application."
        ))
        self.assertFalse(is_explicit_new_agent_request("Start another agent that..."))
        self.assertFalse(is_explicit_new_agent_request("What agents are running?"))

    def test_tool_routes_require_explicit_action_language(self):
        self.assertFalse(has_explicit_action_request(
            "The response time still seems about the same."
        ))
        self.assertFalse(has_explicit_action_request(
            "Testing out the latest live conversation updates."
        ))
        self.assertTrue(has_explicit_action_request("Please fix the response latency."))
        self.assertTrue(has_explicit_action_request("Can you monitor the logs?"))
        self.assertTrue(has_explicit_action_request("The routing needs to be fixed."))
        self.assertTrue(has_explicit_action_request(
            "Assign an agent to review this conversation and make improvements."
        ))
        self.assertTrue(has_explicit_action_request("Set an alarm for six tomorrow."))

    def test_changeable_facts_require_authoritative_grounding(self):
        for transcript in (
            "What is the latest OpenClaw release?",
            "What's the weather today?",
            "Where is Harvey Depp?",
            "Is that service currently available?",
            "Tell me two facts about the moon.",
            "Can you still hear me when the app is closed?",
            "Does the microphone listen while the phone is locked?",
        ):
            self.assertTrue(requires_authoritative_lookup(transcript), transcript)
        for transcript in (
            "Who was Ada Lovelace?",
            "Explain acoustic echo cancellation.",
            "I'm testing the latest update.",
        ):
            self.assertFalse(requires_authoritative_lookup(transcript), transcript)

    def test_semantic_controller_vetoes_incomplete_freshness_question(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.sessions_updated_at = time.monotonic()
            service.capabilities_updated_at = time.monotonic()
            decision = semantic_decision(
                "agent", "I'll verify that.", complete=False,
                speech_act="question", actionable=False, grounding=False,
                assembled_text="What are the latest updates for the",
            )
            client, _ = mock_semantic_model(decision)
            with patch("server.aiohttp.ClientSession", return_value=client):
                self.assertEqual(
                    await service.speech_reply("What are the latest updates for the"),
                    ("wait", "", None),
                )
            self.assertEqual(
                service.pending_fragment, "What are the latest updates for the"
            )

        import asyncio
        asyncio.run(run_test())

    def test_semantic_controller_normalizes_noncanonical_speech_act(self):
        decision = semantic_decision(
            "direct", "I heard that.", speech_act="acknowledgment",
            assembled_text="That was one extra test.",
        )
        understanding = parse_turn_understanding(
            json.dumps(decision), "That was one extra test."
        )
        self.assertEqual(understanding.speech_act, "answer")
        self.assertTrue(understanding.complete)

    def test_semantic_controller_derives_unknown_speech_act_from_structure(self):
        decision = semantic_decision(
            "agent", "I'll inspect it.", actionable=True,
            speech_act="task_assignment", assembled_text="Please inspect it.",
        )
        understanding = parse_turn_understanding(
            json.dumps(decision), "Please inspect it."
        )
        self.assertEqual(understanding.speech_act, "request")
        self.assertTrue(understanding.actionable)

    def test_noncanonical_speech_act_does_not_block_speech_reply(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.sessions_updated_at = time.monotonic()
            service.capabilities_updated_at = time.monotonic()
            decision = semantic_decision(
                "direct", "I heard that.", speech_act="acknowledgment",
                assembled_text="That was one extra test.",
            )
            client, _ = mock_semantic_model(decision)
            with patch("server.aiohttp.ClientSession", return_value=client):
                self.assertEqual(
                    await service.speech_reply("That was one extra test."),
                    ("direct", "I heard that.", None),
                )

        import asyncio
        asyncio.run(run_test())

    def test_semantic_controller_distinguishes_meta_verify_from_lookup(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.sessions_updated_at = time.monotonic()
            service.capabilities_updated_at = time.monotonic()
            decision = semantic_decision(
                "direct", "I was referring to your unfinished question.",
                speech_act="meta", relation="meta", actionable=False,
                grounding=False, supersedes=True,
                assembled_text="What are you going to verify?",
            )
            client, _ = mock_semantic_model(decision)
            with patch("server.aiohttp.ClientSession", return_value=client):
                self.assertEqual(
                    await service.speech_reply("What are you going to verify?"),
                    ("direct", "I was referring to your unfinished question.", None),
                )

        import asyncio
        asyncio.run(run_test())

    def test_semantic_correction_supersedes_only_its_originating_callback(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.sessions_updated_at = time.monotonic()
            service.capabilities_updated_at = time.monotonic()
            service.register_agent_session(
                "agent:main:first", "verify the first claim", "turn-first"
            )
            service.register_agent_session(
                "agent:main:second", "verify the second claim", "turn-second"
            )
            decision = semantic_decision(
                "direct", "Understood; I won't use that second plan.",
                speech_act="correction", relation="correction", supersedes=True,
                assembled_text="No, I meant the first one.",
            )
            client, _ = mock_semantic_model(decision)
            with patch("server.aiohttp.ClientSession", return_value=client):
                await service.speech_reply("No, I meant the first one.")
            self.assertNotIn("turn-first", service.superseded_turn_ids)
            self.assertIn("turn-second", service.superseded_turn_ids)

        import asyncio
        asyncio.run(run_test())

    def test_exact_cutoff_exchange_starts_no_agent_and_speaks_once(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.sessions_updated_at = time.monotonic()
            service.capabilities_updated_at = time.monotonic()
            service.tts.synthesize = AsyncMock(return_value=b"")
            socket = AsyncMock()
            handoffs = []

            async def handoff(request, session_key, turn_id):
                handoffs.append((request, session_key, turn_id))

            decisions = [
                (
                    semantic_decision(
                        "agent", "I'll verify that.", complete=False,
                        speech_act="question",
                        assembled_text="What are the latest updates for the",
                    ),
                    ("wait", "", None),
                ),
                (
                    semantic_decision(
                        "direct", "I don't have a complete subject to verify yet.",
                        speech_act="meta", relation="meta", supersedes=True,
                        assembled_text=(
                            "It looks like my sentence was cut off. What are you going to verify?"
                        ),
                    ),
                    ("direct", "I don't have a complete subject to verify yet.", None),
                ),
            ]

            async def semantic_reply(*_args, **_kwargs):
                payload, result = decisions.pop(0)
                service.last_turn_understanding = parse_turn_understanding(
                    json.dumps(payload), payload["assembled_text"]
                )
                if not service.last_turn_understanding.complete:
                    service.pending_fragment = payload["assembled_text"]
                else:
                    service.pending_fragment = ""
                return result

            service.speech_reply = AsyncMock(side_effect=semantic_reply)
            await service.process_turn(
                socket, b"", transcript_override="What are the latest updates for the",
                handoff_callback=handoff,
            )
            await service.process_turn(
                socket, b"", transcript_override=(
                    "It looks like my sentence was cut off. What are you going to verify?"
                ), handoff_callback=handoff,
            )

            replies = [
                call.args[0] for call in socket.send_json.await_args_list
                if call.args[0].get("type") == "reply"
            ]
            self.assertEqual(handoffs, [])
            self.assertEqual(len(replies), 1)
            self.assertEqual(replies[0]["route"], "direct")
            self.assertEqual(
                replies[0]["text"], "I don't have a complete subject to verify yet."
            )
            fragments = [
                event for event in service.recent_events()
                if event.get("type") == "fragment"
            ]
            self.assertEqual(len(fragments), 1)
            self.assertEqual(fragments[0]["status"], "awaiting_continuation")

        import asyncio
        asyncio.run(run_test())

    def test_semantic_continuation_assembles_then_routes_once(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.sessions_updated_at = time.monotonic()
            service.capabilities_updated_at = time.monotonic()
            first = semantic_decision(
                "direct", "", complete=False, speech_act="question",
                assembled_text="What are the latest updates for the",
            )
            second = semantic_decision(
                "agent", "I'll check the latest Live Conversation updates.",
                speech_act="question", relation="continuation", grounding=True,
                assembled_text="What are the latest updates for the Live Conversation project?",
            )
            client, _ = mock_semantic_model(first, second)
            service.semantic_fragment_resolution = AsyncMock(return_value=(
                "continuation", True, 0.99,
                "What are the latest updates for the Live Conversation project?",
                "The second fragment supplies the missing subject.",
            ))
            with patch("server.aiohttp.ClientSession", return_value=client):
                self.assertEqual(
                    await service.speech_reply("What are the latest updates for the"),
                    ("wait", "", None),
                )
                self.assertEqual(
                    await service.speech_reply("Live Conversation project?"),
                    ("agent", "I'll check the latest Live Conversation updates.", None),
                )
            self.assertEqual(service.pending_fragment, "")
            self.assertEqual(
                service.last_turn_understanding.assembled_text,
                "What are the latest updates for the Live Conversation project?",
            )

        import asyncio
        asyncio.run(run_test())

    def test_fragment_resolver_semantically_assembles_two_transcriptions(self):
        async def run_test():
            service = LiveConversationService("agent:main:test", 1.15)
            resolution = {
                "relation": "continuation",
                "complete": True,
                "confidence": 0.99,
                "assembled_text": "What is the capital of the nation called Canada?",
                "reason": "The newer phrase supplies the missing object.",
            }
            client, session = mock_semantic_model(resolution)
            with patch("server.aiohttp.ClientSession", return_value=client):
                result = await service.semantic_fragment_resolution(
                    "Please answer only after both parts. What is the capital of?",
                    "The nation called Canada?",
                )
            self.assertEqual(result[0], "continuation")
            self.assertTrue(result[1])
            self.assertEqual(
                result[3], "What is the capital of the nation called Canada?"
            )
            request = session.post.call_args.kwargs["json"]
            self.assertIn("missing arguments", request["messages"][0]["content"])

        import asyncio
        asyncio.run(run_test())

    def test_fragment_resolver_preserves_unrelated_new_turn(self):
        async def run_test():
            service = LiveConversationService("agent:main:test", 1.15)
            resolution = {
                "relation": "new",
                "complete": True,
                "confidence": 0.98,
                "assembled_text": "What time is it?",
                "reason": "The newer question is self-contained and unrelated.",
            }
            client, _ = mock_semantic_model(resolution)
            with patch("server.aiohttp.ClientSession", return_value=client):
                result = await service.semantic_fragment_resolution(
                    "What is the capital of?", "What time is it?"
                )
            self.assertEqual(result[:4], (
                "new", True, 0.98, "What time is it?"
            ))

        import asyncio
        asyncio.run(run_test())

    def test_closed_app_hearing_question_does_not_use_presence_shortcut(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.sessions_updated_at = time.monotonic()
            service.capabilities_updated_at = time.monotonic()
            decision = semantic_decision(
                "agent", "I'll verify the background listening behavior.",
                speech_act="question", grounding=True,
                assembled_text="Can you hear me when the app is closed?",
            )
            client, session = mock_semantic_model(decision)
            with patch("server.aiohttp.ClientSession", return_value=client):
                result = await service.speech_reply(
                    "Can you hear me when the app is closed?"
                )
            self.assertEqual(result, (
                "agent", "I'll verify the background listening behavior.", None
            ))
            self.assertEqual(session.post.call_count, 1)

        import asyncio
        asyncio.run(run_test())

    def test_prompt_ignores_background_speech_and_exposes_capabilities(self):
        prompt = speech_model_prompt(self.now, capabilities="agent research: Research\nskill weather: Forecasts")
        self.assertIn("`ignore`", prompt)
        self.assertIn("clearly not addressed to you", prompt)
        self.assertIn("Imperfect grammar", prompt)
        self.assertIn("agent research: Research", prompt)
        self.assertIn("answer capability questions quickly", prompt)
        self.assertIn("Statements describing what he is currently doing", prompt)
        self.assertIn("does not prove that listening continues", prompt)
        self.assertIn("Testing out the latest Live Conversation updates", prompt)

    def test_confirmation_setting_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            service = LiveConversationService(
                "agent:main:live-conversation", 1.15, settings_path=str(settings_path)
            )
            service.set_confirmation_required(True)
            restarted = LiveConversationService(
                "agent:main:live-conversation", 1.15, settings_path=str(settings_path)
            )
            self.assertTrue(restarted.confirmation_required)
            self.assertEqual(
                json.loads(settings_path.read_text())["action_confirmation"], "confirm"
            )

    def test_audio_capture_is_opt_in_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            service = LiveConversationService(
                "agent:main:live-conversation", 1.15,
                settings_path=str(settings_path),
                recordings_path=str(Path(directory) / "recordings"),
            )
            self.assertFalse(service.audio_capture_enabled)
            self.assertIsNone(service.begin_audio_capture(b"\0\0" * 320, 1))

            service.set_audio_capture_enabled(True)
            restarted = LiveConversationService(
                "agent:main:live-conversation", 1.15,
                settings_path=str(settings_path),
                recordings_path=str(Path(directory) / "recordings"),
            )
            self.assertTrue(restarted.audio_capture_enabled)
            self.assertTrue(restarted.settings_payload()["audio_capture"])

    def test_audio_capture_writes_lossless_wav_and_test_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            recordings = Path(directory) / "recordings"
            service = LiveConversationService(
                "agent:main:live-conversation", 1.15,
                recordings_path=str(recordings),
            )
            service.audio_capture_enabled = True
            pcm = (b"\x00\x10\x00\xf0" * 8_000)

            capture_id = service.begin_audio_capture(pcm, 7)
            self.assertIsNotNone(capture_id)
            service.update_audio_capture(
                capture_id, status="complete", transcript="Test the real recording.",
                route="direct", reply="Recorded.", turn_id="turn-7",
            )

            day = recordings / capture_id[:8]
            wav_path = day / f"{capture_id}-user.wav"
            manifest_path = day / f"{capture_id}.json"
            with wave.open(str(wav_path), "rb") as recording:
                self.assertEqual(recording.getnchannels(), 1)
                self.assertEqual(recording.getsampwidth(), 2)
                self.assertEqual(recording.getframerate(), 16_000)
                self.assertEqual(recording.readframes(recording.getnframes()), pcm)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["consent"], "explicit_in_app_opt_in")
            self.assertEqual(manifest["duration_ms"], 1000)
            self.assertEqual(manifest["sequence"], 7)
            self.assertEqual(manifest["transcript"], "Test the real recording.")
            self.assertEqual(manifest["reply"], "Recorded.")
            self.assertEqual(manifest["user_audio"], wav_path.name)
            self.assertEqual(manifest["capture_source"], "automated_or_non_android_client")

    def test_android_user_agent_marks_capture_as_real_phone_microphone(self):
        with tempfile.TemporaryDirectory() as directory:
            service = LiveConversationService(
                "agent:main:live-conversation", 1.15,
                recordings_path=str(Path(directory) / "recordings"),
            )
            service.audio_capture_enabled = True
            capture_id = service.begin_audio_capture(
                b"\0\0" * 320, 2, {"user_agent": "Android WebView; Pixel"}
            )
            manifest = json.loads(
                service._capture_manifest_path(capture_id).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["capture_source"], "android_live_microphone")

    def test_debug_status_persists_and_diagnostics_redact_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            service = LiveConversationService(
                "agent:main:live-conversation", 1.15,
                settings_path=str(settings_path),
            )
            service.set_debug_status(
                "release_available", message="New release v1.0.74 is available.",
                release_tag="v1.0.74",
            )
            restarted = LiveConversationService(
                "agent:main:live-conversation", 1.15,
                settings_path=str(settings_path),
            )
            self.assertEqual(restarted.debug_status["state"], "release_available")
            self.assertEqual(restarted.settings_payload()["debug_status"]["release_tag"], "v1.0.74")
            redacted = service.redact_diagnostics(
                'Authorization: Bearer private-value token=another-secret "password":"hidden"'
            )
            self.assertNotIn("private-value", redacted)
            self.assertNotIn("another-secret", redacted)
            self.assertNotIn("hidden", redacted)
            self.assertEqual(redacted.count("[REDACTED]"), 3)

    def test_in_flight_debug_status_becomes_interrupted_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            service = LiveConversationService(
                "agent:main:live-conversation", 1.15,
                settings_path=str(settings_path),
            )
            service.set_debug_status(
                "working", request_id="debug-123", bundle_path="/tmp/debug.json",
                message="Correction agent is diagnosing the captured conversation.",
            )

            restarted = LiveConversationService(
                "agent:main:live-conversation", 1.15,
                settings_path=str(settings_path),
            )

            self.assertEqual(restarted.debug_status["state"], "failed")
            self.assertEqual(restarted.debug_status["request_id"], "debug-123")
            self.assertEqual(restarted.debug_status["bundle_path"], "/tmp/debug.json")
            self.assertIn("interrupted", restarted.debug_status["message"])
            self.assertIn("completed_at", restarted.debug_status)
            self.assertEqual(
                json.loads(settings_path.read_text(encoding="utf-8"))["debug_status"]["state"],
                "failed",
            )

    def test_debug_bundle_contains_conversation_audio_and_system_context(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                service = LiveConversationService(
                    "agent:main:live-conversation", 1.15,
                    recordings_path=str(root / "recordings"),
                    debug_path=str(root / "debug"),
                )
                service.audio_capture_enabled = True
                capture_id = service.begin_audio_capture(b"\0\0" * 16_000, 4)
                service.update_audio_capture(
                    capture_id, status="complete", transcript="This is a real phone turn."
                )
                service.remember("user", "This is a real phone turn.")
                service.remember("assistant", "I heard the turn.")
                service.system_update_snapshot = AsyncMock(return_value={
                    "dashboard_head": "abc", "dashboard_tag": "v1.0.73",
                    "gateway_version": "OpenClaw 1", "gateway_service": "ActiveState=active",
                })
                service._diagnostic_command = AsyncMock(
                    return_value="token=private-value diagnostic output"
                )

                path, baseline = await service.create_debug_bundle(
                    "debug-test", {"voice_processing": "aec=true"}
                )

                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(baseline["dashboard_tag"], "v1.0.73")
                self.assertTrue(payload["authorization"]["correct"])
                self.assertTrue(payload["authorization"]["publish_release_if_needed"])
                self.assertEqual(payload["client_context"]["voice_processing"], "aec=true")
                self.assertEqual(payload["conversation"]["messages"][-1]["role"], "assistant")
                self.assertEqual(payload["recent_audio_captures"][0]["capture_id"], capture_id)
                self.assertNotIn("private-value", path.read_text(encoding="utf-8"))
                self.assertIn("[REDACTED]", path.read_text(encoding="utf-8"))

        import asyncio
        asyncio.run(run_test())

    def test_voice_launched_session_tracking_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            first = LiveConversationService(
                "agent:main:live-conversation", 1.15, settings_path=str(settings_path)
            )
            key = first.allocate_agent_session_key(
                "spawn a subagent to fix messages being cut off"
            )
            first.register_agent_session(
                key, "spawn a subagent to fix messages being cut off"
            )
            second = LiveConversationService(
                "agent:main:live-conversation", 1.15, settings_path=str(settings_path)
            )
            self.assertEqual(second.latest_agent_session()["session_key"], key)
            self.assertIn("fix messages being cut off", second.voice_session_summary())

    def test_voice_session_keys_do_not_use_long_numeric_suffixes(self):
        service = LiveConversationService("agent:main:live-conversation", 1.15)

        first = service.allocate_agent_session_key("launch an agent to inspect speech")
        second = service.allocate_agent_session_key("launch an agent to inspect speech")

        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^agent:main:live-conversation-inspect-speech-[a-f0-9]{12}$")
        self.assertNotRegex(first, r"\d{10,}")

    def test_legacy_voice_session_is_recovered_from_history_and_gateway(self):
        service = LiveConversationService("agent:main:live-conversation", 1.15)
        service.remember("user", "Spawn a subagent to fix message cutoff.")
        service.remember("assistant", "I'll start a separate agent for that.")
        key = "agent:main:live-conversation-legacy-123"
        service.session_records = {
            key: {"key": key, "hasActiveRun": False, "status": "done", "updatedAt": 20}
        }

        service.recover_recent_agent_session()

        self.assertEqual(service.latest_agent_session(), {
            "session_key": key,
            "request": "Spawn a subagent to fix message cutoff.",
            "state": "complete",
            "started_at": service.latest_agent_session()["started_at"],
        })

    def test_page_displays_and_updates_the_rolling_history(self):
        page = render_page()
        self.assertIn("Recent messages · newest first · last 120", page)
        self.assertIn("m.type==='history'", page)
        self.assertIn("historyMessages.slice(-120).reverse()", page)
        self.assertIn("addHistory('user',m.text)", page)
        self.assertIn("addHistory('assistant',m.text)", page)
        self.assertIn("get('autostart')==='1'", page)
        self.assertIn("liveConversationStopped", page)
        self.assertIn("Action confirmation: loading", page)
        self.assertIn("m.type==='settings'", page)

    def test_action_handoff_starts_before_acknowledgment_delivery(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.transcribe = AsyncMock(return_value="Please fix the routing bug.")
            service.speech_reply = AsyncMock(
                return_value=("agent", "I'll inspect and fix the routing bug.", None)
            )
            events = []

            async def send_spoken(*_args, **_kwargs):
                events.append("acknowledgment_delivered")
                return 12

            async def start_handoff(request, target):
                events.append(("action_started", request, target))

            service.send_spoken_response = AsyncMock(side_effect=send_spoken)
            socket = AsyncMock()

            handoff = await service.process_turn(
                socket, b"audio", handoff_callback=start_handoff
            )

            self.assertEqual(events[0][0:2], (
                "action_started", "Please fix the routing bug."
            ))
            self.assertTrue(events[0][2].startswith(
                "agent:main:live-conversation-please-fix-routing-bug-"
            ))
            self.assertEqual(events[1], "acknowledgment_delivered")
            self.assertIsNone(handoff)
            messages = [call.args[0] for call in socket.send_json.await_args_list]
            action_status = next(
                index for index, message in enumerate(messages)
                if message.get("type") == "action_status"
            )
            metrics = next(
                index for index, message in enumerate(messages)
                if message.get("type") == "metrics"
            )
            self.assertLess(action_status, metrics)

        import asyncio
        asyncio.run(run_test())

    def test_ignore_token_produces_no_spoken_text(self):
        self.assertEqual(parse_speech_model_output(IGNORE_SENTINEL), ("ignore", "", None))

    def test_jarvis_wake_word_matches_as_a_word(self):
        self.assertTrue(is_wake_word("Jarvis"))
        self.assertTrue(is_wake_word("Hey, Jarvis!"))
        self.assertFalse(is_wake_word("Chavez"))
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
        self.assertEqual(SPEECH_MODEL, "openclaw-live-conversation:4b")
        self.assertEqual(SPEECH_MODEL_CONTEXT, 8192)
        self.assertEqual(SPEECH_MODEL_URL, "http://127.0.0.1:11439/api/chat")
        self.assertEqual(SPEECH_MODEL_KEEP_ALIVE, "30m")
        self.assertEqual(SPEECH_NUM_PREDICT, 384)
        self.assertEqual(SPEECH_RETRY_NUM_PREDICT, 640)

    def test_partial_structured_reply_decodes_streamed_json_safely(self):
        self.assertEqual(
            partial_structured_reply(
                '{"route":"direct","complete":true,"reply":"Hello\\nJohn. Next'
            ),
            ("direct", "Hello\nJohn. Next"),
        )
        self.assertEqual(
            partial_structured_reply('{"route":"agent","complete":true,"reply":"I will'),
            ("agent", "I will"),
        )
        self.assertEqual(
            partial_structured_reply('{"route":"direct","reply":"Premature'),
            ("direct", ""),
        )

    def test_incremental_direct_reply_starts_audio_before_final_text(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.tts.synthesize = AsyncMock(return_value=b"\0" * 960)
            socket = AsyncMock()
            stream = IncrementalSpeechStream(service, socket, "response-stream", 0)

            await stream.feed(
                '{"route":"direct","complete":true,"reply":"This first sentence is ready. The next'
            )
            await asyncio.sleep(0)
            self.assertTrue(stream.started)
            self.assertEqual(service.tts.synthesize.await_count, 1)
            await stream.finish(
                "This first sentence is ready. The next sentence is complete.", "direct"
            )

            self.assertEqual(service.tts.synthesize.await_count, 2)
            payloads = [call.args[0] for call in socket.send_json.await_args_list]
            self.assertEqual(
                sum(item.get("type") == "output_audio_buffer.started" for item in payloads),
                1,
            )
            self.assertEqual(
                sum(item.get("type") == "response.output_audio.done" for item in payloads),
                1,
            )
            self.assertTrue(any(item.get("type") == "reply" for item in payloads))

        import asyncio
        asyncio.run(run_test())

    def test_incremental_direct_reply_streams_qwen_chunks_without_full_synthesis(self):
        async def run_test():
            class StreamingTts:
                speed = 1.0

                async def stream_synthesize(self, _text, _speed):
                    yield b"\0" * 7_000
                    yield b"\0" * 12_200

                async def synthesize(self, _text, _speed):
                    raise AssertionError("streaming backend must not use full synthesis")

            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.tts = StreamingTts()
            socket = AsyncMock()
            stream = IncrementalSpeechStream(service, socket, "response-stream", 0)

            with patch("server.asyncio.sleep", AsyncMock()):
                await stream.feed(
                    '{"route":"direct","complete":true,"reply":"This streamed sentence is ready. The next'
                )
                await asyncio.sleep(0)
                await stream.finish(
                    "This streamed sentence is ready. The next sentence is complete.",
                    "direct",
                )

            payloads = [call.args[0] for call in socket.send_json.await_args_list]
            self.assertEqual(
                sum(item.get("type") == "output_audio_buffer.started" for item in payloads),
                1,
            )
            self.assertGreaterEqual(
                sum(item.get("type") == "response.output_audio.delta" for item in payloads),
                4,
            )
            self.assertEqual(payloads[-1]["type"], "response.output_audio.done")

        import asyncio
        asyncio.run(run_test())

    def test_speech_model_warmup_uses_the_live_context_size(self):
        import inspect
        source = inspect.getsource(LiveConversationService.warm_speech_model)
        self.assertIn('"num_ctx": SPEECH_MODEL_CONTEXT', source)
        self.assertNotIn('"num_ctx": 512', source)

    def test_speech_supervisor_retries_instead_of_showing_a_token_limited_reply(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            response = AsyncMock()
            response.raise_for_status = lambda: None
            response.json = AsyncMock(side_effect=[
                {
                    "done_reason": "length",
                    "eval_count": SPEECH_NUM_PREDICT,
                    "message": {"content": json.dumps({
                        "route": "direct",
                        "reply": "This reply was cut off and I",
                        "session_key": "",
                    })},
                },
                {
                    "done_reason": "stop",
                    "eval_count": 96,
                    "message": {"content": json.dumps({
                        "route": "direct",
                        "reply": "This reply now reaches a complete ending.",
                        "session_key": "",
                    })},
                },
            ])
            context = MagicMock()
            context.__aenter__.return_value = response
            session = MagicMock()
            session.post.return_value = context
            session_context = MagicMock()
            session_context.__aenter__.return_value = session

            with patch("server.aiohttp.ClientSession", return_value=session_context):
                result = await service.speech_reply("Give me a detailed answer.")

            self.assertEqual(
                result,
                ("direct", "This reply now reaches a complete ending.", None),
            )
            self.assertEqual(session.post.call_count, 2)
            limits = [
                call.kwargs["json"]["options"]["num_predict"]
                for call in session.post.call_args_list
            ]
            self.assertEqual(limits, [SPEECH_NUM_PREDICT, SPEECH_RETRY_NUM_PREDICT])

        import asyncio
        asyncio.run(run_test())

    def test_pending_agent_prompt_keeps_the_supervisor_conversational(self):
        prompt = speech_model_prompt(
            self.now,
            agent_pending=True,
            sessions="key=agent:main:job | title=Audio repair | hasActiveRun=yes",
        )
        self.assertIn("still awaiting a final response", prompt)
        self.assertIn("Multiple agents may work concurrently", prompt)
        self.assertIn("hasActiveRun=yes", prompt)
        self.assertIn("`new_agent`", prompt)

    def test_prompt_exposes_voice_session_referents(self):
        prompt = speech_model_prompt(
            self.now,
            sessions="key=agent:main:live-conversation-cutoff-123 | hasActiveRun=yes",
            voice_sessions=(
                "key=agent:main:live-conversation-cutoff-123 | state=running | "
                "request=fix message cutoff"
            ),
        )
        self.assertIn("Sessions launched from this Live Conversation", prompt)
        self.assertIn("request=fix message cutoff", prompt)
        self.assertIn("Resolve “this agent,”", prompt)

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

    def test_speech_model_output_parses_structured_route(self):
        self.assertEqual(
            parse_speech_model_output(json.dumps({
                "route": "agent",
                "reply": "I'll handle that.",
                "session_key": "",
            })),
            ("agent", "I'll handle that.", None),
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
        delegated = {"messages": [{"role": "assistant", "content": [{
            "type": "toolCall",
            "name": "sessions_yield",
            "arguments": {"acknowledgment": "The child is working."},
        }]}]}
        self.assertEqual(extract_agent_acknowledgment(delegated), "The child is working.")
        self.assertEqual(latest_assistant_text(delegated), "")
        with_prior_reply = {"messages": [{
            "role": "assistant", "content": "An older reply."
        }, *delegated["messages"]]}
        self.assertEqual(latest_assistant_text(with_prior_reply), "")
        completed = {"messages": [*with_prior_reply["messages"], {
            "role": "assistant", "content": [{"type": "text", "text": "Finished."}]
        }]}
        self.assertEqual(latest_assistant_text(completed), "Finished.")
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

    def test_agent_waits_for_final_after_sessions_yield(self):
        async def run_test():
            delegated = {"messages": [{"role": "assistant", "content": [{
                "type": "toolCall",
                "name": "sessions_yield",
                "arguments": {"acknowledgment": "The child is working."},
            }]}]}
            completed = {"messages": [*delegated["messages"], {
                "role": "assistant", "content": "The noise-rejection work is complete."
            }]}
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.openclaw_json = AsyncMock(side_effect=[{}, delegated, completed])
            with patch("server.asyncio.sleep", AsyncMock()) as sleep:
                reply = await service.agent_reply("Improve noise rejection.", "agent:test")
            self.assertEqual(reply, "The noise-rejection work is complete.")
            sleep.assert_awaited_once()
            self.assertEqual(service.openclaw_json.await_count, 3)

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
            self.assertNotIn("Atlas", messages[1]["content"])
            self.assertIn("Recent dialogue: []", messages[1]["content"])
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
            service.openclaw_json = AsyncMock(return_value={"sessions": [{
                "key": "one", "displayName": "Audio repair", "hasActiveRun": True,
            }]})
            with patch("server.aiohttp.ClientSession") as client_session:
                self.assertEqual(
                    await service.speech_reply("What sessions are running?"),
                    ("direct", "1 gateway session is currently running: Audio repair.", None),
                )
                client_session.assert_not_called()
            service.openclaw_json.assert_awaited_once()

        import asyncio
        asyncio.run(run_test())

    def test_speech_reply_semantically_routes_explicit_new_agent(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.sessions_updated_at = time.monotonic()
            service.capabilities_updated_at = time.monotonic()
            response = AsyncMock()
            response.raise_for_status = lambda: None
            response.json = AsyncMock(return_value={"message": {"content": json.dumps(
                semantic_decision(
                    "new_agent", "I'll start a separate agent for that.",
                    actionable=True, speech_act="request",
                    assembled_text="Spawn another agent to inspect transcription.",
                )
            )}})
            context = MagicMock()
            context.__aenter__.return_value = response
            session = MagicMock()
            session.post.return_value = context
            session_context = MagicMock()
            session_context.__aenter__.return_value = session
            with patch("server.aiohttp.ClientSession", return_value=session_context):
                self.assertEqual(
                    await service.speech_reply("Spawn another agent to inspect transcription."),
                    ("new_agent", "I'll start a separate agent for that.", None),
                )

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
        self.assertNotEqual(prompt_history[0]["role"], "assistant")

    def test_one_oversized_recent_message_is_truncated_to_prompt_budget(self):
        service = LiveConversationService("agent:main:live-conversation", 1.15)
        service.remember("user", "beginning-marker " + ("x" * 20_000) + " ending-marker")

        prompt_history = service.prompt_history()

        self.assertEqual(len(prompt_history), 1)
        self.assertLessEqual(len(prompt_history[0]["content"]), PROMPT_HISTORY_CHARS)
        self.assertTrue(prompt_history[0]["content"].startswith("…"))
        self.assertTrue(prompt_history[0]["content"].endswith("ending-marker"))

    def test_adjacent_duplicate_transport_events_are_not_remembered_twice(self):
        service = LiveConversationService("agent:main:live-conversation", 1.15)
        service.remember("assistant", "  The agent completed.\n")
        service.remember("assistant", "The  agent completed.")

        self.assertEqual(service.recent_history(), [
            {"role": "assistant", "content": "The agent completed."},
        ])

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

    def test_referential_status_force_refreshes_and_answers_for_exact_agent(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            key = "agent:main:live-conversation-fix-cutoff-123"
            service.register_agent_session(key, "fix the issue where messages are being cut off")
            service.sessions = "stale catalog"
            service.sessions_updated_at = time.monotonic()
            service.openclaw_json = AsyncMock(return_value={"sessions": [{
                "key": key,
                "displayName": "Fix message cutoff",
                "hasActiveRun": True,
                "status": "running",
                "updatedAt": 1788828000000,
            }]})

            result = await service.speech_reply("Can you tell me how this agent is running?")

            self.assertEqual(result, (
                "direct",
                "The agent assigned to this task is still running: fix the issue where messages are being cut off.",
                None,
            ))
            service.openclaw_json.assert_awaited_once()
            self.assertIn(key, service.session_records)

        import asyncio
        asyncio.run(run_test())

    def test_referential_followup_targets_exact_latest_voice_session(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            key = "agent:main:live-conversation-fix-cutoff-123"
            service.register_agent_session(key, "fix message cutoff")
            service.sessions_updated_at = time.monotonic()
            service.capabilities_updated_at = time.monotonic()
            response = AsyncMock()
            response.raise_for_status = lambda: None
            response.json = AsyncMock(return_value={"message": {"content": json.dumps(
                semantic_decision(
                    "session", "I'll add that to the same agent.",
                    actionable=True, speech_act="request",
                    assembled_text="Tell that agent to also check long replies.",
                    session_key=key,
                )
            )}})
            context = MagicMock()
            context.__aenter__.return_value = response
            session = MagicMock()
            session.post.return_value = context
            session_context = MagicMock()
            session_context.__aenter__.return_value = session
            with patch("server.aiohttp.ClientSession", return_value=session_context):
                self.assertEqual(
                    await service.speech_reply("Tell that agent to also check long replies."),
                    ("session", "I'll add that to the same agent.", key),
                )

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

    def test_speech_reply_enforces_action_authorization_postconditions(self):
        async def decide(transcript, model_payload):
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.sessions_updated_at = time.monotonic()
            service.capabilities_updated_at = time.monotonic()
            response = AsyncMock()
            response.raise_for_status = lambda: None
            response.json = AsyncMock(return_value={
                "message": {"content": json.dumps(model_payload)}
            })
            context = MagicMock()
            context.__aenter__.return_value = response
            session = MagicMock()
            session.post.return_value = context
            session_context = MagicMock()
            session_context.__aenter__.return_value = session
            with patch("server.aiohttp.ClientSession", return_value=session_context):
                return await service.speech_reply(transcript)

        import asyncio
        invented = semantic_decision(
            "agent", "I'll have the agent monitor that.",
            actionable=False, assembled_text="The response feels the same.",
        )
        missing_route = semantic_decision(
            "direct", "I'll have the agent fix that.", actionable=True,
            speech_act="request", assembled_text="Please fix the response latency.",
        )
        empty_ack = semantic_decision(
            "direct", "I understand.", actionable=True, speech_act="request",
            assembled_text="Assign an agent to review this conversation and make improvements.",
        )
        self.assertEqual(
            asyncio.run(decide("The response feels the same.", invented)),
            ("direct", "I understand.", None),
        )
        self.assertEqual(
            asyncio.run(decide("Please fix the response latency.", missing_route)),
            ("agent", "I'll have the agent fix that.", None),
        )
        self.assertEqual(
            asyncio.run(decide(
                "Assign an agent to review this conversation and make improvements.",
                empty_ack,
            )),
            ("agent", "I'll have the agent handle that.", None),
        )

    def test_start_uses_the_more_accurate_cached_whisper_model(self):
        import inspect
        source = inspect.getsource(LiveConversationService.start)
        self.assertIn('model="small.en"', source)
        self.assertIn('device="cuda"', source)
        self.assertIn('compute_type="float16"', source)
        self.assertIn("falling_back_to_cpu", source)
        self.assertNotIn('model="tiny.en"', source)

    def test_lazy_whisper_segments_are_consumed_off_the_event_loop(self):
        async def run_test():
            class Segment:
                text = " heard clearly"
                no_speech_prob = 0.0

            class Model:
                def transcribe(self, *_args, **_kwargs):
                    def lazy_segments():
                        time.sleep(0.15)
                        yield Segment()

                    return lazy_segments(), object()

            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.stt = MagicMock(_model=Model())
            started = time.perf_counter()
            transcription = asyncio.create_task(service.transcribe(b"\0\0" * 1600))

            # This timer must fire while lazy segment inference is still running.
            await asyncio.sleep(0.02)
            self.assertLess(time.perf_counter() - started, 0.10)
            self.assertFalse(transcription.done())
            self.assertEqual(await transcription, "heard clearly")

        import asyncio
        asyncio.run(run_test())

    def test_transcription_uses_silero_noise_filtering(self):
        import inspect
        source = inspect.getsource(LiveConversationService.transcribe)
        self.assertIn("vad_filter=True", source)
        self.assertIn('vad_threshold = 0.35 if purpose == "final"', source)
        self.assertIn('0.5 if purpose == "wake" else 0.6', source)
        self.assertIn('"threshold": vad_threshold', source)
        self.assertIn('"speech_pad_ms": 300 if purpose == "final" else 200', source)
        self.assertIn('"min_speech_duration_ms": 250', source)
        self.assertIn("beam_size=5", source)
        self.assertIn("best_of=5", source)
        self.assertIn("hotwords=", source)
        self.assertIn("OpenClaw, Live Conversation, live agent", source)

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
            self.assertEqual(handoff[:2], (
                "Check the RAG app and report progress.",
                "I’ll ask the agent to check that.",
            ))
            self.assertTrue(handoff[2].startswith(
                "agent:main:live-conversation-check-rag-app-report-progress-"
            ))

        import asyncio
        asyncio.run(run_test())

    def test_deterministic_reply_does_not_reuse_previous_assembled_text(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.last_turn_understanding = TurnUnderstanding(
                complete=True,
                confidence=1.0,
                speech_act="request",
                relation="new",
                actionable=True,
                requires_grounding=False,
                supersedes_previous=False,
                assembled_text=(
                    "Can you task an agent with identifying why the response is taking so long?"
                ),
                reason="Prior turn.",
            )
            service.refresh_sessions = AsyncMock()
            service.sessions = "No active sessions."
            service.sessions_last_success_wall = time.time()
            service.transcribe = AsyncMock(
                return_value="Can you tell me which sessions are running currently?"
            )
            service.tts.synthesize = AsyncMock(return_value=b"")
            socket = AsyncMock()

            await service.process_turn(socket, b"audio")

            user_event = service.events[-2]
            self.assertEqual(
                user_event["metadata"]["assembled_text"],
                "Can you tell me which sessions are running currently?",
            )
            self.assertIsNone(service.last_turn_understanding)

        import asyncio
        asyncio.run(run_test())

    def test_confirmation_mode_blocks_action_until_explicit_yes(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.confirmation_required = True
            service.transcribe = AsyncMock(side_effect=["Fix the routing bug.", "Go ahead."])
            service.speech_reply = AsyncMock(
                return_value=("agent", "I'll have the agent fix the routing bug.", None)
            )
            service.tts.synthesize = AsyncMock(return_value=b"")
            socket = AsyncMock()

            self.assertIsNone(await service.process_turn(socket, b"first"))
            self.assertIsNotNone(service.pending_confirmation)
            replies = [
                call.args[0] for call in socket.send_json.await_args_list
                if call.args[0].get("type") == "reply"
            ]
            self.assertEqual(replies[-1]["route"], "confirmation")
            self.assertIn("should I proceed", replies[-1]["text"])

            pending_key = service.pending_confirmation[1]
            handoff = await service.process_turn(socket, b"second")
            self.assertEqual(
                handoff,
                ("Fix the routing bug.", "Okay, proceeding.", pending_key),
            )
            self.assertIsNone(service.pending_confirmation)
            self.assertEqual(service.speech_reply.await_count, 1)

        import asyncio
        asyncio.run(run_test())

    def test_confirmation_preserves_new_agent_session_identity(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.confirmation_required = True
            service.transcribe = AsyncMock(side_effect=[
                "Spawn a subagent to fix message cutoff.", "Go ahead."
            ])
            service.speech_reply = AsyncMock(
                return_value=("new_agent", "I'll start a separate agent for that.", None)
            )
            service.tts.synthesize = AsyncMock(return_value=b"")
            socket = AsyncMock()

            self.assertIsNone(await service.process_turn(socket, b"first"))
            pending_key = service.pending_confirmation[1]
            self.assertTrue(pending_key.startswith("agent:main:live-conversation-fix-message-cutoff-"))
            handoff = await service.process_turn(socket, b"second")
            self.assertEqual(handoff[2], pending_key)

        import asyncio
        asyncio.run(run_test())

    def test_stop_command_interrupts_without_reply_or_model_call(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.transcribe = AsyncMock(return_value="Jarvis, stop talking.")
            service.speech_reply = AsyncMock()
            service.tts.synthesize = AsyncMock()
            service.speech_generation = 4
            socket = AsyncMock()

            self.assertIsNone(await service.process_turn(socket, b"audio"))
            self.assertEqual(service.speech_generation, 5)
            service.speech_reply.assert_not_awaited()
            service.tts.synthesize.assert_not_awaited()
            messages = [call.args[0] for call in socket.send_json.await_args_list]
            self.assertIn(
                {"type": "state", "state": "listening", "detail": "Silent stop command."},
                messages,
            )
            self.assertFalse(any(message.get("type") == "reply" for message in messages))

        import asyncio
        asyncio.run(run_test())

    def test_multiple_back_and_forth_turns_are_all_heard_and_remembered(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.transcribe = AsyncMock(side_effect=[
                "Can you hear the first question?",
                "Here is my second question.",
                "And this is the third one.",
            ])
            service.speech_reply = AsyncMock(side_effect=[
                ("direct", "I heard the first question.", None),
                ("direct", "I heard the second question.", None),
                ("direct", "I heard the third question.", None),
            ])
            service.tts.synthesize = AsyncMock(return_value=b"")
            socket = AsyncMock()

            for turn in (b"one", b"two", b"three"):
                self.assertIsNone(await service.process_turn(socket, turn))

            self.assertEqual(service.recent_history(), [
                {"role": "user", "content": "Can you hear the first question?"},
                {"role": "assistant", "content": "I heard the first question."},
                {"role": "user", "content": "Here is my second question."},
                {"role": "assistant", "content": "I heard the second question."},
                {"role": "user", "content": "And this is the third one."},
                {"role": "assistant", "content": "I heard the third question."},
            ])
            transcripts = [
                call.args[0]["text"] for call in socket.send_json.await_args_list
                if call.args[0].get("type") == "transcript"
            ]
            self.assertEqual(transcripts, [
                "Can you hear the first question?",
                "Here is my second question.",
                "And this is the third one.",
            ])

        import asyncio
        asyncio.run(run_test())

    def test_new_user_speech_suppresses_stale_reply_but_preserves_both_inputs(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.transcribe = AsyncMock(side_effect=[
                "Start with the first part.", "Actually, add this clarification."
            ])
            first_routing_started = asyncio.Event()
            release_first_routing = asyncio.Event()

            async def speech_reply(text, agent_pending=False, stream_callback=None):
                if text.startswith("Start"):
                    first_routing_started.set()
                    await release_first_routing.wait()
                    return "direct", "Here is the now-stale first reply.", None
                return "direct", "I heard the clarification too.", None

            service.speech_reply = AsyncMock(side_effect=speech_reply)
            service.tts.synthesize = AsyncMock(return_value=b"")
            socket = AsyncMock()

            first = asyncio.create_task(service.process_turn(socket, b"first"))
            await first_routing_started.wait()
            service.interrupt_speech()
            release_first_routing.set()
            self.assertIsNone(await first)
            self.assertIsNone(await service.process_turn(socket, b"second"))

            self.assertEqual(service.recent_history(), [
                {"role": "user", "content": "Start with the first part."},
                {"role": "user", "content": "Actually, add this clarification."},
                {"role": "assistant", "content": "I heard the clarification too."},
            ])
            messages = [call.args[0] for call in socket.send_json.await_args_list]
            self.assertTrue(any(item.get("type") == "turn_superseded" for item in messages))
            replies = [item["text"] for item in messages if item.get("type") == "reply"]
            self.assertEqual(replies, ["I heard the clarification too."])

        import asyncio
        asyncio.run(run_test())

    def test_new_speech_cancels_inflight_model_routing_before_fifo_advances(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.transcribe = AsyncMock(return_value="Explain the first topic.")
            routing_started = asyncio.Event()
            routing_cancelled = asyncio.Event()

            async def slow_routing(*_args, **_kwargs):
                routing_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    routing_cancelled.set()
                    raise

            service.speech_reply = AsyncMock(side_effect=slow_routing)
            socket = AsyncMock()
            task = asyncio.create_task(
                service.process_turn(socket, b"first", turn_generation=0)
            )
            await routing_started.wait()
            service.interrupt_speech()
            await asyncio.wait_for(task, timeout=0.5)

            self.assertTrue(routing_cancelled.is_set())
            self.assertEqual(service.recent_history(), [
                {"role": "user", "content": "Explain the first topic."},
            ])
            self.assertEqual(service.recent_events()[-1]["type"], "interruption")
            self.assertEqual(
                service.recent_events()[-1]["status"], "model_generation_cancelled"
            )

        import asyncio
        asyncio.run(run_test())

    def test_websocket_queues_rapid_commits_in_fifo_order_without_cancellation(self):
        async def run_test():
            import base64
            from collections import deque
            from aiohttp import web
            from aiohttp.test_utils import TestClient, TestServer
            from server import websocket

            class FakeService:
                def __init__(self):
                    self.pending_spoken_replies = deque()
                    self.started = asyncio.Event()
                    self.release = asyncio.Event()
                    self.completed = []
                    self.generations = []
                    self.interruptions = 0
                    self.speech_generation = 0

                def recent_history(self):
                    return []

                def settings_payload(self):
                    return {"type": "settings", "action_confirmation": "automatic"}

                def interrupt_speech(self):
                    self.interruptions += 1
                    self.speech_generation += 1

                async def process_turn(
                    self, socket, audio, agent_pending=False, turn_generation=None,
                    handoff_callback=None,
                ):
                    if not self.completed:
                        self.started.set()
                        await self.release.wait()
                    self.completed.append(audio)
                    self.generations.append(turn_generation)
                    return None

            service = FakeService()
            app = web.Application()
            app["service"] = service
            app.router.add_get("/ws", websocket)
            client = TestClient(TestServer(app))
            await client.start_server()
            socket = await client.ws_connect("/ws")
            await socket.receive_json()
            await socket.receive_json()

            async def send_turn(content):
                await socket.send_json({"type": "input_audio_buffer.speech_started"})
                await socket.send_json({"type": "start"})
                await socket.send_json({
                    "type": "audio", "audioBase64": base64.b64encode(content).decode()
                })
                await socket.send_json({"type": "commit"})

            await send_turn(b"first")
            await service.started.wait()
            await send_turn(b"second")
            await asyncio.sleep(0.02)
            self.assertEqual(service.completed, [])
            service.release.set()
            for _ in range(50):
                if len(service.completed) == 2:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(service.completed, [b"first", b"second"])
            self.assertEqual(service.generations, [1, 2])
            self.assertEqual(service.interruptions, 2)

            await socket.close()
            await client.close()

        import asyncio
        asyncio.run(run_test())

    def test_websocket_send_for_debug_launches_unique_correction_session(self):
        async def run_test():
            from collections import deque
            from aiohttp import web
            from aiohttp.test_utils import TestClient, TestServer
            from server import websocket

            class FakeService:
                def __init__(self):
                    self.pending_spoken_replies = deque()
                    self.debug_status = {"state": "idle"}
                    self.speech_generation = 0
                    self.called = asyncio.Event()
                    self.registered = []

                def recent_history(self):
                    return [{"role": "user", "content": "The previous turn failed."}]

                def settings_payload(self):
                    return {
                        "type": "settings", "action_confirmation": "automatic",
                        "audio_capture": True, "debug_status": dict(self.debug_status),
                    }

                def set_debug_status(self, state, **fields):
                    self.debug_status = {"state": state, **fields}
                    return dict(self.debug_status)

                def allocate_agent_session_key(self, request):
                    return "agent:main:live-conversation-debug-test-abcdef"

                async def create_debug_bundle(self, request_id, client_context):
                    self.bundle_context = client_context
                    return Path("/tmp/debug-test.json"), {
                        "dashboard_tag": "v1.0.73", "gateway_version": "1",
                        "gateway_service": "active-1",
                    }

                def register_agent_session(self, session_key, request, origin_turn_id):
                    self.registered.append((session_key, request, origin_turn_id))

                async def agent_reply(self, prompt, session_key):
                    self.prompt = prompt
                    self.called.set()
                    return "Correction completed."

                def update_agent_session(self, session_key, state, result=""):
                    self.updated = (session_key, state, result)

                async def system_update_snapshot(self):
                    return {
                        "dashboard_tag": "v1.0.74", "gateway_version": "1",
                        "gateway_service": "active-1",
                    }

                @staticmethod
                def gateway_is_transient(error):
                    return False

            service = FakeService()
            app = web.Application()
            app["service"] = service
            app.router.add_get("/ws", websocket)
            client = TestClient(TestServer(app))
            await client.start_server()
            socket = await client.ws_connect("/ws")
            await socket.receive_json()
            await socket.receive_json()
            await socket.send_json({"type": "send_for_debug"})

            messages = []
            for _ in range(5):
                message = await asyncio.wait_for(socket.receive_json(), timeout=1)
                messages.append(message)
                if message.get("debug_status", {}).get("state") == "release_available":
                    break

            await asyncio.wait_for(service.called.wait(), timeout=1)
            self.assertEqual(len(service.registered), 1)
            self.assertIn("dedicated Live Conversation correction agent", service.prompt)
            self.assertIn("/tmp/debug-test.json", service.prompt)
            states = [item.get("debug_status", {}).get("state") for item in messages]
            self.assertIn("collecting", states)
            self.assertIn("working", states)
            self.assertIn("release_available", states)
            self.assertEqual(service.debug_status["release_tag"], "v1.0.74")

            await socket.close()
            await client.close()

        import asyncio
        asyncio.run(run_test())

    def test_background_noise_turn_returns_to_listening_without_history_or_reply(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 1.15)
            service.transcribe = AsyncMock(return_value="")
            service.speech_reply = AsyncMock()
            service.tts.synthesize = AsyncMock()
            socket = AsyncMock()

            self.assertIsNone(await service.process_turn(socket, b"road-noise"))
            service.speech_reply.assert_not_awaited()
            service.tts.synthesize.assert_not_awaited()
            self.assertEqual(service.recent_history(), [])
            self.assertIn(
                {"type": "state", "state": "listening", "detail": "No clear speech detected."},
                [call.args[0] for call in socket.send_json.await_args_list],
            )

        import asyncio
        asyncio.run(run_test())

    def test_voice_command_changes_confirmation_mode_without_an_agent(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as directory:
                service = LiveConversationService(
                    "agent:main:live-conversation",
                    1.15,
                    settings_path=str(Path(directory) / "settings.json"),
                )
                service.transcribe = AsyncMock(
                    return_value="Always ask me before taking actions."
                )
                service.speech_reply = AsyncMock()
                service.tts.synthesize = AsyncMock(return_value=b"")
                socket = AsyncMock()

                self.assertIsNone(await service.process_turn(socket, b"audio"))
                self.assertTrue(service.confirmation_required)
                service.speech_reply.assert_not_awaited()
                messages = [call.args[0] for call in socket.send_json.await_args_list]
                settings = [message for message in messages if message.get("type") == "settings"]
                self.assertEqual(settings[-1]["action_confirmation"], "confirm")
                self.assertFalse(settings[-1]["audio_capture"])

        import asyncio
        asyncio.run(run_test())

    def test_page_uses_native_audio_and_same_origin_websocket(self):
        page = render_page()
        self.assertIn("OpenClawNativeAudio.startCapture", page)
        self.assertIn("location.host+'/ws'", page)
        self.assertNotIn("silenceMs>=600", page)
        self.assertIn("OpenClawNativeAudio.prepareAgentResponsePlayback()", page)
        self.assertIn("OpenClawNativeAudio.interruptAgentResponsePlayback()", page)
        self.assertIn("response.output_audio.delta", page)
        self.assertIn("if(recording)return", page)
        self.assertNotIn("if(awaitingResponse){candidateSpeechMs=0;return}", page)
        self.assertIn("interruptedWait?'while_awaiting_response':'ready'", page)
        self.assertIn("candidateSpeechMs>=speechRequiredMs", page)
        self.assertIn("ONSET_PREROLL_MS=2500", page)
        self.assertIn("PREBUFFER_FRAMES=Math.ceil((START_CONFIRM_MS+ONSET_PREROLL_MS)/AUDIO_FRAME_MS)", page)
        self.assertIn("while(pre.length>PREBUFFER_FRAMES)pre.shift()", page)
        self.assertIn("MAX_DRAIN_FRAMES=24", page)
        self.assertIn("processChunk(chunk)", page)
        self.assertIn("responseActive=true", page)
        self.assertIn("first_pcm_enqueued", page)
        self.assertIn("pcm_delivery_done", page)
        self.assertIn("if(m.state==='listening'&&!recording)", page)
        self.assertIn("m.type==='partial_transcript'", page)
        self.assertIn("addHistory('user',m.text)", page)

    def test_websocket_generates_incremental_partials_without_a_turn_cap(self):
        import inspect
        import server

        source = inspect.getsource(server.websocket)
        self.assertIn('purpose="partial"', source)
        self.assertIn('"type": "partial_transcript"', source)
        self.assertIn("silent_stop_detected", source)
        self.assertIn("turn_queue.put_nowait", source)
        self.assertIn("finalize_online_transcript", source)
        self.assertIn("async def run_turn_queue", source)
        self.assertIn("await conversation_idle.wait()", source)
        self.assertIn("if turn_queue.empty() and not user_input_active", source)
        self.assertIn("len(online_transcript.stable_words) >= 3", source)
        self.assertIn("pcm_override=affirmative_hum_pcm()", source)
        self.assertNotIn("turn_task.cancel()", source)
        self.assertNotIn("len(audio) < SAMPLE_RATE * 2 * 30", source)

    def test_default_tts_speed_is_conversational(self):
        import inspect
        from server import PersistentTtsWorker

        default = inspect.signature(PersistentTtsWorker).parameters["speed"].default
        self.assertEqual(default, 1.08)

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

    def test_machine_identifiers_are_not_read_as_long_numbers(self):
        text = (
            "Session agent:main:live-conversation-audio-459321222249681 finished at "
            "timestamp 1788986623228 with run 39262386-1bb6-4571-98e1-13a30047ddb8."
        )

        self.assertEqual(
            normalize_spoken_text(text),
            "Session the agent session finished at timestamp the numeric identifier "
            "with run the identifier.",
        )

    def test_normal_sized_numbers_remain_exact_in_speech(self):
        self.assertEqual(
            normalize_spoken_text("Version 2026.8.2 took 300 ms and passed 294/299 checks."),
            "Version 2026 point 8 point 2 took 300 milliseconds and passed 294 out of 299 checks.",
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

    def test_default_tts_units_are_short_enough_for_fast_first_audio(self):
        text = (
            "Two gateway sessions are currently running: live conversation session; "
            "improve voice recognition in road noise."
        )
        parts = split_spoken_text(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 90 for part in parts))
        self.assertEqual(" ".join(parts), text)

    def test_playback_vad_rejects_echo_and_covers_native_tail(self):
        page = render_page()
        self.assertIn("const speechThreshold=responseActive?.025:.012", page)
        self.assertIn("START_CONFIRM_MS=300,BARGE_IN_CONFIRM_MS=300", page)
        self.assertIn(
            "const speechRequiredMs=responseActive?BARGE_IN_CONFIRM_MS:START_CONFIRM_MS",
            page,
        )
        self.assertIn("SEMANTIC_CHECK_MS=750", page)
        self.assertIn("HARD_ENDPOINT_MS=3500", page)
        self.assertIn("send({type:'endpoint_candidate'})", page)
        self.assertIn("m.type==='endpoint_decision'", page)
        self.assertIn("responseTailTimer=setTimeout(()=>{responseActive=false;responseTailTimer=null},1500)", page)
        self.assertIn("report('barge_in','confirmed_user_speech')", page)
        self.assertIn("}send({type:'input_audio_buffer.speech_started'});send({type:'start'})", page)

    def test_spoken_audio_prefills_before_realtime_pacing(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 0.72)
            socket = AsyncMock()
            service.tts.synthesize = AsyncMock(return_value=b"\0" * 19_200)
            with patch("server.asyncio.sleep", AsyncMock()) as sleep:
                await service.send_spoken_response(socket, "A short response.", "agent", "response-1")
            self.assertEqual(sleep.await_count, 1)
            self.assertEqual(sleep.await_args_list[0].args[0], 0.1)
            payloads = [call.args[0] for call in socket.send_json.await_args_list]
            self.assertEqual(
                sum(payload.get("type") == "response.output_audio.delta" for payload in payloads),
                4,
            )

        import asyncio
        asyncio.run(run_test())

    def test_confirmed_barge_in_stops_a_long_server_stream(self):
        async def run_test():
            service = LiveConversationService("agent:main:live-conversation", 0.72)
            socket = AsyncMock()
            service.tts.synthesize = AsyncMock(return_value=b"\0" * 24_000)

            async def interrupt_after_prefill(_):
                service.interrupt_speech()

            with patch("server.asyncio.sleep", AsyncMock(side_effect=interrupt_after_prefill)) as sleep:
                await service.send_spoken_response(socket, "A long response.", "agent", "response-1")

            self.assertEqual(sleep.await_count, 1)
            payloads = [call.args[0] for call in socket.send_json.await_args_list]
            self.assertEqual(
                sum(payload.get("type") == "response.output_audio.delta" for payload in payloads),
                4,
            )
            self.assertEqual(payloads[-1]["type"], "response.output_audio.done")

        import asyncio
        asyncio.run(run_test())

    def test_qwen_backend_builds_raw_chunked_pcm_request(self):
        client = QwenVllmTtsClient()
        payload = client.request_payload("Hello from Jarvis.")
        self.assertEqual(payload["response_format"], "pcm")
        self.assertEqual(payload["stream_format"], "audio")
        self.assertTrue(payload["stream"])
        self.assertEqual(
            payload["initial_codec_chunk_frames"], DEFAULT_QWEN_INITIAL_CHUNK_FRAMES
        )
        self.assertEqual(payload["voice"], "aiden")

    def test_tts_backend_switch_is_explicit_and_reversible(self):
        with patch.dict("server.os.environ", {"LIVE_CONVERSATION_TTS_BACKEND": "qwen"}):
            self.assertIsInstance(configured_tts_backend(1.08), QwenVllmTtsClient)
        with patch.dict("server.os.environ", {"LIVE_CONVERSATION_TTS_BACKEND": "kokoro"}):
            self.assertNotIsInstance(configured_tts_backend(1.08), QwenVllmTtsClient)

    def test_qwen_pcm_stream_is_reframed_and_prefilled_for_android(self):
        async def run_test():
            class StreamingTts:
                speed = 1.0

                async def stream_synthesize(self, _text, _speed):
                    yield b"\0" * 7_000
                    yield b"\0" * 12_200

            service = LiveConversationService("agent:main:live-conversation", 0.72)
            service.tts = StreamingTts()
            socket = AsyncMock()
            with patch("server.asyncio.sleep", AsyncMock()) as sleep:
                latency = await service.send_spoken_response(
                    socket, "A streamed Qwen response.", "direct", "response-qwen"
                )
            self.assertGreaterEqual(latency, 0)
            self.assertEqual(sleep.await_count, 1)
            payloads = [call.args[0] for call in socket.send_json.await_args_list]
            deltas = [item for item in payloads if item.get("type") == "response.output_audio.delta"]
            self.assertEqual(len(deltas), 4)
            self.assertEqual(payloads[-1]["type"], "response.output_audio.done")

        import asyncio
        asyncio.run(run_test())

    def test_barge_in_closes_qwen_stream_without_a_spoken_acknowledgment(self):
        async def run_test():
            closed = False

            class StreamingTts:
                speed = 1.0

                async def stream_synthesize(self, _text, _speed):
                    nonlocal closed
                    try:
                        yield b"\0" * 48_000
                        yield b"\0" * 48_000
                    finally:
                        closed = True

            service = LiveConversationService("agent:main:live-conversation", 0.72)
            service.tts = StreamingTts()
            socket = AsyncMock()

            async def interrupt_after_prefill(_):
                service.interrupt_speech()

            with patch("server.asyncio.sleep", AsyncMock(side_effect=interrupt_after_prefill)):
                await service.send_spoken_response(
                    socket, "A response that will be interrupted.", "direct", "response-qwen"
                )
            self.assertTrue(closed)
            payloads = [call.args[0] for call in socket.send_json.await_args_list]
            self.assertEqual(payloads[-1]["type"], "response.output_audio.done")
            self.assertFalse(any(item.get("text") == "Stopped." for item in payloads))

        import asyncio
        asyncio.run(run_test())

if __name__ == "__main__":
    unittest.main()
