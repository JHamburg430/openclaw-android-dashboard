import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from server import DEFAULT_NODE_COMMAND, DEFAULT_OPENCLAW_MODULE, extract_agent_text, render_page, route_simple


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 2, 12, 34, tzinfo=ZoneInfo("America/Detroit"))

    def test_direct_conversation_controls(self):
        self.assertEqual(route_simple("Can you hear me?", self.now), "Yes. I can hear you clearly.")
        self.assertEqual(route_simple("Thanks", self.now), "You're welcome.")

    def test_direct_time_date_and_arithmetic(self):
        self.assertEqual(route_simple("What time is it?", self.now), "It is 12:34 PM.")
        self.assertEqual(route_simple("What is 12 plus 7?", self.now), "The answer is 19.")

    def test_complex_requests_escalate(self):
        self.assertIsNone(route_simple("Check my calendar and move tomorrow's meeting.", self.now))
        self.assertIsNone(route_simple("Explain the latest OpenClaw release and cite sources.", self.now))
        self.assertIsNone(route_simple("Remember that I prefer morning meetings.", self.now))

    def test_agent_json_extraction(self):
        payload = {"result": {"payloads": [{"text": "Ready."}]}}
        self.assertEqual(extract_agent_text(payload), "Ready.")
        self.assertTrue(DEFAULT_NODE_COMMAND.endswith("/node"))
        self.assertTrue(DEFAULT_OPENCLAW_MODULE.endswith("/openclaw.mjs"))

    def test_page_uses_native_audio_and_same_origin_websocket(self):
        page = render_page()
        self.assertIn("OpenClawNativeAudio.startCapture", page)
        self.assertIn("location.host+'/ws'", page)
        self.assertIn("silenceMs>=600", page)


if __name__ == "__main__":
    unittest.main()
