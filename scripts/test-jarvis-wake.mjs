import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const activity = readFileSync(new URL("../app/src/main/java/ai/openclaw/dashboard/MainActivity.java", import.meta.url), "utf8");
const service = readFileSync(new URL("../app/src/main/java/ai/openclaw/dashboard/JarvisVoiceInteractionService.java", import.meta.url), "utf8");
const manifest = readFileSync(new URL("../app/src/main/AndroidManifest.xml", import.meta.url), "utf8");
const metadata = readFileSync(new URL("../app/src/main/res/xml/voice_interaction_service.xml", import.meta.url), "utf8");

assert.ok(activity.includes("RoleManager.ROLE_ASSISTANT"));
assert.ok(activity.includes("Intent.ACTION_SCREEN_OFF"));
assert.ok(activity.includes("stopLiveConversationForLock"));
assert.ok(activity.includes("openLiveConversation(true)"));
assert.ok(activity.includes("boolean jarvisLaunch"));
assert.ok(activity.indexOf("if (jarvisLaunch)") < activity.indexOf("restoreWebViewState(savedInstanceState)"));
assert.ok(activity.includes("?autostart=1"));
assert.ok(service.includes('new URI("ws", null, source.getHost(), 8790, "/wake"'));
assert.ok(service.includes("handler.postDelayed"));
assert.ok(service.includes("MediaRecorder.AudioSource.VOICE_RECOGNITION"));
assert.ok(service.includes("ACTION_JARVIS_WAKE"));
assert.ok(service.includes("paused = true;\n        stopListening();"));
assert.ok(manifest.includes("android.permission.BIND_VOICE_INTERACTION"));
assert.ok(manifest.includes("@xml/voice_interaction_service"));
assert.ok(metadata.includes('android:supportsLaunchVoiceAssistFromKeyguard="true"'));

console.log("Jarvis wake-word assistant role and lock-screen flow are wired");
