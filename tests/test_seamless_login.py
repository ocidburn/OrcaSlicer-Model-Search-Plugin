"""Seamless sign-in through the loopback credential receiver.

These tests drive a real server over real HTTP. A mocked handler would prove
nothing here: the point of the feature is that a browser on this machine can
hand a portal session to the plugin, and the point of the tests is that only
a browser on this machine can.
"""

import os
import socket
import sys
import tempfile
import time
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from tests._module_loader import load_plugin

mod = load_plugin("search_engine_seamless_login")


def load_script_module():
    fake = types.ModuleType("orca")

    class ScriptPluginCapabilityBase:
        def __init__(self, *args, **kwargs):
            pass

    fake.script = types.SimpleNamespace(
        ScriptPluginCapabilityBase=ScriptPluginCapabilityBase
    )
    fake.base = type("Base", (), {})
    fake.ExecutionResult = types.SimpleNamespace(success=staticmethod(lambda: "ok"))
    fake.host = types.SimpleNamespace(
        ui=types.SimpleNamespace(create_window=lambda **kwargs: None)
    )
    fake.plugin = lambda cls: cls
    fake.register_capability = lambda capability: None
    previous = sys.modules.get("orca")
    sys.modules["orca"] = fake
    try:
        return load_plugin("search_engine_seamless_login_script")
    finally:
        if previous is None:
            sys.modules.pop("orca", None)
        else:
            sys.modules["orca"] = previous


def http(url, *, data=None, headers=None, method=None):
    """Return (status, body, headers) without raising on 4xx."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(
        url, data=body, headers=headers or {}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8"), response.headers
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read().decode("utf-8"), exc.headers


def endpoint_down(url, timeout=5):
    """True once the loopback endpoint stops answering.

    A closed listener surfaces as URLError on POSIX and as a reset or aborted
    connection on Windows; both are OSError, which is what we wait for.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            http(url)
        except OSError:
            return True
        time.sleep(0.05)
    return False


class ReceiverTest(unittest.TestCase):
    PLATFORM = "cults3d"
    CREDENTIAL = "session=abc123; other=1"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.auth = mod.AuthManager(
            mod.AuthStore(os.path.join(self._tmp.name, "sessions.json"))
        )
        self.saved = []

    def store(self, platform, value):
        self.saved.append((platform, value))
        self.auth.save_token(platform, value, label="Browser sign-in")

    def receiver(self, platform=None, timeout=30):
        rec = mod.LoginReceiver(platform or self.PLATFORM, self.store, timeout=timeout)
        rec.start()
        self.addCleanup(rec.stop)
        return rec


class LoopbackRoundTripTests(ReceiverTest):
    def test_a_browser_on_this_machine_completes_the_sign_in(self):
        rec = self.receiver()
        status, page, _headers = http(rec.url)
        self.assertEqual(status, 200)
        self.assertIn("Connect Cults3D", page)
        # The page must not carry a credential; it is a drop-off, not a store.
        self.assertNotIn(self.CREDENTIAL, page)

        status, page, _headers = http(
            f"{rec.origin}/connect",
            data={"state": rec.state, "value": self.CREDENTIAL},
            headers={"Origin": rec.origin, "Referer": rec.url},
        )
        self.assertEqual(status, 200)
        self.assertIn("OrcaSlicer has the session", page)

        self.assertEqual(self.saved, [(self.PLATFORM, self.CREDENTIAL)])
        self.assertTrue(self.auth.authenticated(self.PLATFORM))
        self.assertIn("session=abc123", self.auth.token(self.PLATFORM))

    def test_a_redirect_carrying_a_token_needs_no_paste_at_all(self):
        rec = self.receiver(platform="thingiverse")
        query = urllib.parse.urlencode(
            {"state": rec.state, "access_token": "tv-token-from-redirect"}
        )
        status, page, _headers = http(f"{rec.origin}/callback?{query}")
        self.assertEqual(status, 200)
        self.assertIn("OrcaSlicer has the session", page)
        self.assertEqual(self.auth.token("thingiverse"), "tv-token-from-redirect")

    def test_a_redirect_without_a_token_is_reported_not_stored(self):
        rec = self.receiver(platform="thingiverse")
        status, _page, _headers = http(f"{rec.origin}/callback?state={rec.state}")
        self.assertEqual(status, 400)
        self.assertEqual(self.saved, [])

    def test_the_link_is_single_use(self):
        rec = self.receiver()
        origin = rec.origin
        first = http(
            f"{rec.origin}/connect",
            data={"state": rec.state, "value": self.CREDENTIAL},
        )
        self.assertEqual(first[0], 200)
        self.assertTrue(rec.finished)
        # The receiver shuts down after success; either it is already gone or
        # it refuses a second submission. Both are correct.
        try:
            status, page, _headers = http(
                f"{origin}/connect",
                data={"state": rec.state, "value": "second=value"},
            )
        except OSError:
            pass
        else:
            self.assertNotEqual(status, 200)
            self.assertNotIn("has the session", page)
        self.assertEqual(len(self.saved), 1)

    def test_a_rejected_value_does_not_burn_the_link(self):
        rec = self.receiver(platform="grabcad")
        # cookie_header mode refuses a value with no name=value pair.
        status, page, _headers = http(
            f"{rec.origin}/connect",
            data={"state": rec.state, "value": "not-a-cookie"},
        )
        self.assertEqual(status, 400)
        self.assertIn("name=value", page)
        self.assertFalse(self.auth.authenticated("grabcad"))

        status, _page, _headers = http(
            f"{rec.origin}/connect",
            data={"state": rec.state, "value": "_grabcad_session=ok"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(self.auth.authenticated("grabcad"))


class ReceiverContainmentTests(ReceiverTest):
    def test_the_listener_is_bound_to_loopback_only(self):
        rec = self.receiver()
        host, _port = rec._server.server_address[:2]
        self.assertEqual(host, "127.0.0.1")
        self.assertTrue(rec.origin.startswith("http://127.0.0.1:"))

    def test_the_listener_is_not_reachable_from_a_non_loopback_address(self):
        rec = self.receiver()
        port = rec._server.server_address[1]
        # Binding to loopback means no other local interface accepts it.
        probe = socket.socket()
        probe.settimeout(2)
        try:
            outside = socket.gethostbyname(socket.gethostname())
        except OSError:
            self.skipTest("no non-loopback address available")
        if outside.startswith("127."):
            self.skipTest("host resolves to loopback")
        with self.assertRaises(OSError):
            probe.connect((outside, port))
        probe.close()

    def test_a_wrong_state_is_refused(self):
        rec = self.receiver()
        status, _page, _headers = http(f"{rec.origin}/connect?state=wrong")
        self.assertEqual(status, 403)
        status, _page, _headers = http(
            f"{rec.origin}/connect", data={"state": "wrong", "value": self.CREDENTIAL}
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.saved, [])

    def test_a_missing_state_is_refused(self):
        rec = self.receiver()
        status, _page, _headers = http(f"{rec.origin}/connect")
        self.assertEqual(status, 403)
        self.assertEqual(self.saved, [])

    def test_a_page_on_another_site_cannot_post_to_the_receiver(self):
        rec = self.receiver()
        status, _page, _headers = http(
            f"{rec.origin}/connect",
            data={"state": rec.state, "value": self.CREDENTIAL},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.saved, [])

    def test_a_forged_host_header_is_refused(self):
        rec = self.receiver()
        status, _page, _headers = http(
            f"{rec.origin}/connect",
            data={"state": rec.state, "value": self.CREDENTIAL},
            headers={"Host": "portal.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.saved, [])

    def test_the_state_is_not_leaked_through_the_referer(self):
        rec = self.receiver()
        _status, _page, headers = http(rec.url)
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_unknown_paths_are_not_served(self):
        rec = self.receiver()
        status, _page, _headers = http(f"{rec.origin}/admin?state={rec.state}")
        self.assertEqual(status, 404)

    def test_an_oversized_submission_is_refused(self):
        rec = self.receiver()
        status, _page, _headers = http(
            f"{rec.origin}/connect",
            data={"state": rec.state, "value": "x" * 100_000},
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.saved, [])

    def test_each_receiver_gets_a_fresh_unguessable_state(self):
        states = {
            mod.LoginReceiver(self.PLATFORM, self.store).state for _ in range(20)
        }
        self.assertEqual(len(states), 20)
        for state in states:
            self.assertGreaterEqual(len(state), 24)

    def test_the_listener_closes_when_it_times_out(self):
        rec = mod.LoginReceiver(self.PLATFORM, self.store, timeout=0.3)
        rec.start()
        self.addCleanup(rec.stop)
        origin = rec.origin
        deadline = time.time() + 5
        while time.time() < deadline and rec._server is not None:
            time.sleep(0.05)
        self.assertIsNone(rec._server, "the receiver should have timed out")
        self.assertTrue(endpoint_down(f"{origin}/connect?state={rec.state}"))


class CoordinatorLoginTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.script = load_script_module()
        self.action = self.script.SearchEngineScript()
        self.action.auth = self.script.AuthManager(
            self.script.AuthStore(os.path.join(self._tmp.name, "sessions.json"))
        )
        self.posts = []
        self.action._post = self.posts.append
        self.addCleanup(self.action._stop_login)

    def start(self, platform="cults3d"):
        opened = []
        with mock.patch.object(
            self.script.webbrowser, "open", side_effect=lambda url, **k: opened.append(url) or True
        ):
            self.action._do_start_login(platform)
        return opened

    def test_starting_a_login_opens_the_portal_and_the_local_finish_page(self):
        opened = self.start()
        spec = self.script._platform("cults3d")
        self.assertIn(spec.login_url, opened)
        local = [url for url in opened if url.startswith("http://127.0.0.1:")]
        self.assertEqual(len(local), 1, opened)
        pending = [p for p in self.posts if p.get("action") == "login_pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["url"], local[0])
        self.assertEqual(pending[0]["platform"], "cults3d")

    def test_the_full_walk_connects_the_portal(self):
        opened = self.start()
        local = next(url for url in opened if url.startswith("http://127.0.0.1:"))
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(local).query)["state"][0]
        origin = local.split("/connect")[0]

        status, _page, _headers = http(
            f"{origin}/connect",
            data={"state": state, "value": "session=from-browser"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(self.action.auth.authenticated("cults3d"))
        changed = [p for p in self.posts if p.get("action") == "auth_changed"]
        self.assertTrue(changed)
        self.assertIn("Cults3D", changed[-1]["message"])
        self.assertTrue(changed[-1]["states"]["cults3d"]["authenticated"])

    def test_cancelling_stops_the_listener(self):
        opened = self.start()
        local = next(url for url in opened if url.startswith("http://127.0.0.1:"))
        self.action._handle_auth_cancel_login({})
        self.assertEqual(self.posts[-1]["action"], "login_cancelled")
        self.assertTrue(
            endpoint_down(local), "the listener stayed up after cancelling"
        )

    def test_closing_the_window_stops_the_listener(self):
        self.start()
        with self.action._login_lock:
            self.assertIsNotNone(self.action._login)
        self.action.on_close()
        with self.action._login_lock:
            self.assertIsNone(self.action._login)

    def test_starting_again_replaces_the_previous_listener(self):
        first = self.start()
        first_local = next(u for u in first if u.startswith("http://127.0.0.1:"))
        second = self.start()
        second_local = next(u for u in second if u.startswith("http://127.0.0.1:"))
        self.assertNotEqual(first_local, second_local)
        self.assertTrue(
            endpoint_down(first_local), "the first listener was left running"
        )

    def test_a_portal_without_a_session_is_refused(self):
        self.action._do_start_login("printables")
        self.assertEqual(self.posts[-1]["action"], "error")
        with self.action._login_lock:
            self.assertIsNone(self.action._login)

    def test_a_blocked_socket_falls_back_to_the_paste_flow(self):
        with mock.patch.object(
            self.script.LoginReceiver, "start", side_effect=OSError("bind refused")
        ):
            self.action._do_start_login("cults3d")
        self.assertEqual(self.posts[-1]["action"], "error")
        self.assertIn("token field", self.posts[-1]["message"])
        with self.action._login_lock:
            self.assertIsNone(self.action._login)


class SeamlessLoginUiTests(unittest.TestCase):
    def test_the_account_panel_offers_the_browser_sign_in(self):
        for marker in (
            'id="auth-seamless"',
            "function startSeamlessLogin()",
            "auth_start_login",
            "auth_cancel_login",
            "login_pending",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, mod.PAGE)

    def test_closing_the_panel_cancels_a_pending_login(self):
        self.assertIn(
            "function closeAuth(){orca.postMessage({action:'auth_cancel_login'})",
            mod.PAGE,
        )

    def test_every_authorized_portal_has_a_hand_over_hint(self):
        authorized = {
            spec.key for spec in mod._PLATFORM_SPECS if spec.requires_auth
        }
        self.assertEqual(set(mod._LOGIN_HINTS), authorized)


if __name__ == "__main__":
    unittest.main()
