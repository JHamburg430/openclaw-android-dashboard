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


if __name__ == "__main__":
    unittest.main()
