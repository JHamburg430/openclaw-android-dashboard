import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../app/src/main/java/ai/openclaw/dashboard/MainActivity.java", import.meta.url), "utf8");
const methodStart = source.indexOf("private void openLiveConversation()");
const methodEnd = source.indexOf("private void openNativeToolsPage()", methodStart);
const method = source.slice(methodStart, methodEnd);

assert.ok(source.includes("LIVE_CONVERSATION_PORT = 8790"));
assert.ok(method.includes("openLocalApp(LIVE_CONVERSATION_PORT)"));
assert.equal(method.includes("buildLiveConversationHtml()"), false);
assert.ok(source.includes("public void prepareAgentResponsePlayback()"));
assert.ok(source.includes('prepareSpeakerPlaybackRoute(audioManager, "response_prepare")'));
assert.ok(source.includes("public void interruptAgentResponsePlayback()"));
assert.ok(source.includes('recordDiagnostic("native_audio_output.cleared", "speech_started")'));

console.log("Live Conversation opens the dedicated Pipecat service");
