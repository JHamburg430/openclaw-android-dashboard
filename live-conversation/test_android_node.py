import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AndroidPhoneNodeReconnectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = (ROOT / "app/src/main/java/ai/openclaw/dashboard/PhoneNodeService.java").read_text()
        cls.client = (ROOT / "app/src/main/java/ai/openclaw/dashboard/OpenClawClient.java").read_text()
        cls.auth = (ROOT / "app/src/main/java/ai/openclaw/dashboard/GatewayConnectAuth.java").read_text()
        cls.receiver = (ROOT / "app/src/main/java/ai/openclaw/dashboard/PhoneNodeBootReceiver.java").read_text()
        cls.manifest = (ROOT / "app/src/main/AndroidManifest.xml").read_text()
        cls.activity = (ROOT / "app/src/main/java/ai/openclaw/dashboard/MainActivity.java").read_text()
        cls.wake = (ROOT / "app/src/main/java/ai/openclaw/dashboard/JarvisVoiceInteractionService.java").read_text()
        cls.network_security = (ROOT / "app/src/main/res/xml/network_security_config.xml").read_text()
        cls.gradle = (ROOT / "app/build.gradle").read_text()
        cls.ingress = (ROOT / "live-conversation/configure-private-ingress.sh").read_text()

    def test_service_is_sticky_and_independent_of_activity_task(self):
        self.assertIn("return START_STICKY;", self.service)
        self.assertIn('android:stopWithTask="false"', self.manifest)

    def test_boot_and_upgrade_restore_opted_in_node(self):
        self.assertIn("Intent.ACTION_BOOT_COMPLETED", self.receiver)
        self.assertIn("Intent.ACTION_MY_PACKAGE_REPLACED", self.receiver)
        self.assertIn("context.startForegroundService(service)", self.receiver)

    def test_watchdog_recovers_stalled_handshake_and_disconnection(self):
        self.assertIn("CONNECT_TIMEOUT_MS", self.service)
        self.assertIn("runConnectionWatchdog", self.service)
        self.assertIn('resetConnection("connect_timeout")', self.service)
        self.assertIn('audit.record("node", "watchdog_reconnect"', self.service)

    def test_network_and_gateway_changes_force_fresh_socket(self):
        self.assertIn('resetConnection("network_available")', self.service)
        self.assertIn('resetConnection("network_lost")', self.service)
        self.assertIn('resetConnection("watchdog_configuration_changed")', self.service)
        self.assertIn("hasUsableNetwork()", self.service)

    def test_stale_device_authorization_self_repairs_before_reconnect(self):
        self.assertIn("GatewayConnectAuth.shouldClearStoredDeviceToken(", self.client)
        self.assertIn("identityStore.clearDeviceToken();", self.client)
        self.assertIn("Refreshing node authorization", self.client)
        self.assertIn('normalized.contains("device signature invalid")', self.auth)
        self.assertNotIn("clearIdentity", self.client)

    def test_explicit_password_does_not_sign_with_stored_device_token(self):
        self.assertIn("token == null && normalizedPassword == null && deviceToken != null", self.auth)
        self.assertIn('auth.put("deviceToken", selectedAuth.deviceToken)', self.client)
        self.assertNotIn('auth.put("token", authToken)', self.client)

    def test_release_transport_uses_tls_and_cleartext_is_narrowly_scoped(self):
        self.assertIn('android:allowBackup="false"', self.manifest)
        self.assertIn('android:usesCleartextTraffic="false"', self.manifest)
        self.assertIn('android:networkSecurityConfig="@xml/network_security_config"', self.manifest)
        self.assertIn('<base-config cleartextTrafficPermitted="false"', self.network_security)
        self.assertIn('<domain includeSubdomains="true">ts.net</domain>', self.network_security)
        self.assertIn('return "https://" + host + ":" + LIVE_CONVERSATION_HTTPS_PORT', self.activity)
        self.assertIn('new URI(\n                        "wss"', self.wake)
        self.assertIn('if (BuildConfig.DEBUG)', self.activity)
        self.assertIn('if (BuildConfig.DEBUG)', self.wake)

    def test_live_conversation_lifecycle_recognizes_secure_and_legacy_ports(self):
        self.assertIn(
            "port == LIVE_CONVERSATION_PORT || port == LIVE_CONVERSATION_HTTPS_PORT",
            self.activity,
        )

    def test_private_ingress_preserves_installed_client_compatibility(self):
        self.assertIn("--https=8443", self.ingress)
        self.assertIn("--tcp=8790", self.ingress)
        self.assertIn("tcp://127.0.0.1:8790", self.ingress)
        self.assertNotIn("--funnel", self.ingress)

    def test_voice_playback_owns_and_releases_transient_audio_focus(self):
        self.assertIn("new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)", self.activity)
        self.assertIn(".setWillPauseWhenDucked(true)", self.activity)
        self.assertIn("AUDIOFOCUS_LOSS_TRANSIENT", self.activity)
        self.assertIn("abandonAudioFocusRequest(speechAudioFocusRequest)", self.activity)

    def test_release_signing_never_falls_back_to_debug_certificate(self):
        release_block = self.gradle.split("buildTypes", 1)[1]
        self.assertNotIn("signingConfigs.debug", release_block)
        self.assertIn('OPENCLAW_RELEASE_STORE_FILE', self.gradle)
        self.assertIn('signingConfig signingConfigs.findByName("release")', self.gradle)


if __name__ == "__main__":
    unittest.main()
