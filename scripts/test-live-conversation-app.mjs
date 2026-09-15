import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../app/src/main/java/ai/openclaw/dashboard/MainActivity.java", import.meta.url), "utf8");
const methodStart = source.indexOf("private void openLiveConversation()");
const methodEnd = source.indexOf("private void openNativeToolsPage()", methodStart);
const method = source.slice(methodStart, methodEnd);

assert.ok(source.includes("LIVE_CONVERSATION_PORT = 8790"));
assert.ok(method.includes("openLiveConversation(false)"));
assert.ok(source.includes("buildLiveConversationUrl()"));
assert.ok(source.includes('return "https://" + host + ":" + LIVE_CONVERSATION_HTTPS_PORT'));
assert.ok(source.includes("new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)"));
assert.ok(source.includes("abandonAudioFocusRequest(speechAudioFocusRequest)"));
assert.equal(method.includes("buildLiveConversationHtml()"), false);
assert.ok(source.includes("public void prepareAgentResponsePlayback()"));
assert.ok(source.includes('startPcmOutputThreadLocked(OUTPUT_SAMPLE_RATE, "response_prepare")'));
assert.ok(source.includes("private static final double OUTPUT_GAIN = 1.0"));
assert.ok(source.includes("createCommunicationAudioTrack(sampleRateHz, bufferSize)"));
assert.ok(source.includes("AudioAttributes.USAGE_VOICE_COMMUNICATION"));
const prepareStart = source.indexOf("public void prepareAgentResponsePlayback()");
const prepareEnd = source.indexOf("public void interruptAgentResponsePlayback()", prepareStart);
const prepareMethod = source.slice(prepareStart, prepareEnd);
assert.equal(prepareMethod.includes("prepareSpeakerPlaybackRoute"), true);
assert.equal(prepareMethod.includes("preferBluetoothAudioRoute"), true);
assert.equal(source.includes('"openclaw-native-output-prepare"'), false);
assert.ok(source.includes("public void interruptAgentResponsePlayback()"));
assert.ok(source.includes('recordDiagnostic("native_audio_output.cleared", "speech_started")'));

console.log("Live Conversation opens the dedicated Pipecat service");
