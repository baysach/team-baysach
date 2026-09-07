import unittest
from unittest.mock import Mock, patch

import app as poe_app


class ValidationTests(unittest.TestCase):
    def test_parses_encoded_league(self):
        target = poe_app.parse_search_url(
            "https://www.pathofexile.com/api/trade2/search/poe2/Runes%20of%20Aldur"
        )
        self.assertEqual(target.realm, "poe2")
        self.assertEqual(target.league, "Runes of Aldur")

    def test_rejects_non_poe_host(self):
        with self.assertRaises(ValueError):
            poe_app.parse_search_url(
                "https://example.com/api/trade2/search/poe2/Runes%20of%20Aldur"
            )

    def test_cookie_requires_session(self):
        with self.assertRaises(ValueError):
            poe_app.validate_cookie("cf_clearance=test")


class RouteTests(unittest.TestCase):
    def setUp(self):
        poe_app.app.config["TESTING"] = True
        self.client = poe_app.app.test_client()

    def test_index_has_security_headers(self):
        response = self.client.get("/", headers={"Host": "127.0.0.1:8765"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_start_rejects_missing_csrf(self):
        response = self.client.post(
            "/api/start",
            json={},
            headers={"Host": "127.0.0.1:8765"},
        )
        self.assertEqual(response.status_code, 403)

    def test_start_passes_valid_configuration_to_monitor(self):
        body = {
            "cookie": "POESESSID=test; POETOKEN=test",
            "search_url": (
                "https://www.pathofexile.com/api/trade2/search/poe2/"
                "Runes%20of%20Aldur"
            ),
            "payload": {"query": {"status": {"option": "securable"}}},
            "send_whispers": False,
        }
        with patch.object(poe_app.MONITOR, "start") as start:
            response = self.client.post(
                "/api/start",
                json=body,
                headers={
                    "Host": "127.0.0.1:8765",
                    "X-CSRF-Token": poe_app.CSRF_TOKEN,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        start.assert_called_once()

    def test_ready_calls_monitor(self):
        with patch.object(poe_app.MONITOR, "ready_for_next") as ready:
            response = self.client.post(
                "/api/ready",
                json={},
                headers={
                    "Host": "127.0.0.1:8765",
                    "X-CSRF-Token": poe_app.CSRF_TOKEN,
                },
            )
        self.assertEqual(response.status_code, 200)
        ready.assert_called_once()


class BusyStateTests(unittest.TestCase):
    def test_ready_for_next_clears_busy_state(self):
        monitor = poe_app.PoeMonitor(poe_app.EventBus())
        monitor._running = True
        monitor._send_whispers = True
        monitor._busy = True

        monitor.ready_for_next()

        self.assertFalse(monitor.state()["busy"])

    def test_busy_monitor_skips_another_listing(self):
        class NoPostSession:
            def post(self, *args, **kwargs):
                raise AssertionError("A whisper must not be sent while busy")

        monitor = poe_app.PoeMonitor(poe_app.EventBus())
        monitor._send_whispers = True
        monitor._busy = True
        monitor._session = NoPostSession()
        monitor._target = poe_app.SearchTarget(
            url="https://www.pathofexile.com/api/trade2/search/poe2/Test",
            realm="poe2",
            league="Test",
        )
        monitor._query_id = "query"
        monitor._handle_listing(
            {
                "id": "listing-id",
                "listing": {
                    "account": {"name": "seller"},
                    "price": {"amount": 1, "currency": "divine"},
                    "hideout_token": "token",
                },
                "item": {"typeLine": "Test Item"},
            }
        )

        self.assertIn("listing-id", monitor._processed_ids)


class TravelConfirmationTests(unittest.TestCase):
    def run_travel(self, bodies):
        events = poe_app.EventBus()
        monitor = poe_app.PoeMonitor(events)
        monitor._session = Mock()
        responses = []
        for body in bodies:
            response = poe_app.requests.Response()
            response.status_code = 200
            response._content = poe_app.json.dumps(body).encode()
            responses.append(response)
        monitor._session.post.side_effect = responses
        with patch.object(monitor, "_trade_referer", return_value="https://www.pathofexile.com/trade2"):
            monitor._send_whisper("fresh-token", "Test Item", "listing")
        return monitor, events

    def test_demand_confirmation_continues_once_with_same_token(self):
        monitor, _ = self.run_travel([{"success": False}, True])
        calls = monitor._session.post.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs["json"], {"token": "fresh-token"})
        self.assertEqual(calls[1].kwargs["json"], {"token": "fresh-token", "continue": True})
        self.assertTrue(monitor.state()["busy"])

    def test_second_false_does_not_loop_or_pause(self):
        monitor, _ = self.run_travel([False, False])
        self.assertEqual(monitor._session.post.call_count, 2)
        self.assertFalse(monitor.state()["busy"])

    def test_unavailable_item_is_not_retried(self):
        error = {"error": {"code": 1, "message": "Resource not found; Item no longer available"}}
        monitor, events = self.run_travel([error])
        self.assertEqual(monitor._session.post.call_count, 1)
        self.assertFalse(monitor.state()["busy"])
        _, history = events.subscribe()
        self.assertIn("Item no longer available", history[-1]["message"])

    def test_unavailable_after_confirmation_does_not_pause(self):
        monitor, _ = self.run_travel([
            {"success": False},
            {"error": {"code": 1, "message": "Resource not found; Item no longer available"}},
        ])
        self.assertEqual(monitor._session.post.call_count, 2)
        self.assertFalse(monitor.state()["busy"])

    def test_direct_success_does_not_retry(self):
        monitor, _ = self.run_travel([True])
        self.assertEqual(monitor._session.post.call_count, 1)
        self.assertTrue(monitor.state()["busy"])


class ConnectionResilienceTests(unittest.TestCase):
    def test_session_retries_safe_listing_fetches(self):
        monitor = poe_app.PoeMonitor(poe_app.EventBus())
        session = monitor._make_session()

        retries = session.get_adapter("https://").max_retries
        self.assertEqual(retries.total, 3)
        self.assertEqual(retries.allowed_methods, frozenset({"GET"}))
        self.assertIn(429, retries.status_forcelist)

        session.close()

    def test_websocket_close_frame_is_reported_as_status_code(self):
        events = poe_app.EventBus()
        monitor = poe_app.PoeMonitor(events)
        frame = poe_app.websocket.ABNF(
            opcode=poe_app.websocket.ABNF.OPCODE_CLOSE,
            data=b"\x03\xf0",
        )

        monitor._on_ws_error(None, frame)
        _listener, history = events.subscribe()

        self.assertEqual(
            history[-1]["message"],
            "Live connection closed by server with status 1008.",
        )


if __name__ == "__main__":
    unittest.main()
