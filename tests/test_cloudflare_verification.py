"""Cloudflare verification hand-off.

The plugin never answers a Cloudflare challenge. A human passes the check in
their own browser and the resulting clearance is replayed here, so these tests
pin the parts that make that replay both work and stay contained: the cookie
and User-Agent travel as a pair, the cookie is scoped to the host it was earned
on, and a clearance that stops being accepted is discarded rather than retried
forever.
"""

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

import requests

from tests._module_loader import load_plugin

mod = load_plugin("search_engine_cloudflare")


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
        return load_plugin("search_engine_cloudflare_script")
    finally:
        if previous is None:
            sys.modules.pop("orca", None)
        else:
            sys.modules["orca"] = previous


class FakeResponse:
    def __init__(self, text="", status_code=200, headers=None, url=""):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url
        self.closed = False

    def close(self):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return {}


def challenge_response(url):
    return FakeResponse(
        "<title>Just a moment...</title>",
        status_code=403,
        headers={"cf-mitigated": "challenge", "server": "cloudflare"},
        url=url,
    )


class ClearanceParsingTests(unittest.TestCase):
    def test_cookie_is_accepted_in_every_shape_a_browser_offers(self):
        cases = {
            "abc123": "abc123",
            "cf_clearance=abc123": "abc123",
            "cf_clearance=abc123; other=1": "abc123",
            "session=zz; cf_clearance=abc123; more=2": "abc123",
            "Cookie: cf_clearance=abc123; other=1": "abc123",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(mod.CloudflareClearance.parse_cookie(raw), expected)

    def test_some_other_cookie_is_not_mistaken_for_a_clearance(self):
        for raw in ("session=abc123", "auth_token=abc123; other=1", "", "   "):
            with self.subTest(raw=raw):
                self.assertEqual(mod.CloudflareClearance.parse_cookie(raw), "")

    def test_host_is_normalized_from_either_a_url_or_a_bare_name(self):
        normalize = mod.CloudflareClearance.normalize_host
        self.assertEqual(normalize("https://Cults3D.com/en/tags/x"), "cults3d.com")
        self.assertEqual(normalize("Cults3D.com"), "cults3d.com")
        self.assertEqual(normalize(".cults3d.com."), "cults3d.com")
        self.assertEqual(normalize("not a host"), "")
        self.assertEqual(normalize(""), "")


class ClearanceStoreTests(unittest.TestCase):
    def clearance(self, directory):
        store = mod.AuthStore(os.path.join(directory, "sessions.json"))
        return mod.CloudflareClearance(store)

    def test_user_agent_is_mandatory_because_cloudflare_binds_to_it(self):
        with tempfile.TemporaryDirectory() as td:
            clearance = self.clearance(td)
            with self.assertRaises(mod.AuthError) as raised:
                clearance.save("cults3d.com", "cf_clearance=abc", "")
            self.assertIn("User-Agent", str(raised.exception))

    def test_missing_host_or_cookie_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            clearance = self.clearance(td)
            with self.assertRaises(mod.AuthError):
                clearance.save("", "cf_clearance=abc", "UA/1")
            with self.assertRaises(mod.AuthError):
                clearance.save("cults3d.com", "session=abc", "UA/1")

    def test_saved_clearance_covers_the_host_and_its_subdomains_only(self):
        with tempfile.TemporaryDirectory() as td:
            clearance = self.clearance(td)
            clearance.save("https://thangs.com/", "cf_clearance=abc", "UA/1")
            self.assertEqual(
                clearance.for_url("https://thangs.com/x").get("cf_clearance"), "abc"
            )
            self.assertEqual(
                clearance.for_url(
                    "https://production-api.thangs.com/search"
                ).get("cf_clearance"),
                "abc",
            )
            self.assertEqual(clearance.for_url("https://nothangs.com/x"), {})
            self.assertEqual(clearance.for_url("https://example.com/x"), {})

    def test_apply_pins_the_stored_agent_and_returns_the_cookie(self):
        with tempfile.TemporaryDirectory() as td:
            clearance = self.clearance(td)
            clearance.save("cults3d.com", "cf_clearance=abc", "Mozilla/5.0 Real")
            headers = {"User-Agent": "plugin/1.0"}
            cookies = clearance.apply("https://cults3d.com/en", headers)
            self.assertEqual(cookies, {"cf_clearance": "abc"})
            self.assertEqual(headers["User-Agent"], "Mozilla/5.0 Real")

    def test_apply_leaves_unrelated_hosts_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            clearance = self.clearance(td)
            clearance.save("cults3d.com", "cf_clearance=abc", "Mozilla/5.0 Real")
            headers = {"User-Agent": "plugin/1.0"}
            self.assertEqual(clearance.apply("https://example.com/", headers), {})
            self.assertEqual(headers["User-Agent"], "plugin/1.0")

    def test_forget_removes_the_record(self):
        with tempfile.TemporaryDirectory() as td:
            clearance = self.clearance(td)
            clearance.save("cults3d.com", "cf_clearance=abc", "UA/1")
            self.assertTrue(clearance.forget("https://cults3d.com/en"))
            self.assertEqual(clearance.for_url("https://cults3d.com/en"), {})
            self.assertFalse(clearance.forget("cults3d.com"))

    def test_status_lists_hosts_without_exposing_the_cookie(self):
        with tempfile.TemporaryDirectory() as td:
            clearance = self.clearance(td)
            clearance.save("cults3d.com", "cf_clearance=secret-value", "UA/1")
            status = clearance.status()
            self.assertEqual([row["host"] for row in status], ["cults3d.com"])
            self.assertNotIn("secret-value", repr(status))

    def test_the_clearance_slot_is_not_a_platform_credential(self):
        with tempfile.TemporaryDirectory() as td:
            store = mod.AuthStore(os.path.join(td, "sessions.json"))
            auth = mod.AuthManager(store)
            self.assertIsNone(mod._platform(mod.CloudflareClearance.STORE_KEY))
            with self.assertRaises(mod.AuthError):
                auth.save_token(mod.CloudflareClearance.STORE_KEY, "abc")


class ClearanceRequestTests(unittest.TestCase):
    def manager(self, directory):
        return mod.AuthManager(mod.AuthStore(os.path.join(directory, "sessions.json")))

    def test_request_replays_the_clearance_and_overrides_a_supplied_agent(self):
        with tempfile.TemporaryDirectory() as td:
            auth = self.manager(td)
            auth.clearance.save("cults3d.com", "cf_clearance=abc", "Mozilla/5.0 Real")
            session = requests.Session()
            captured = {}

            def fake_request(method, url, **kwargs):
                captured["headers"] = dict(kwargs.get("headers") or {})
                return FakeResponse(url=url)

            with (
                mock.patch.object(session, "request", side_effect=fake_request),
                mock.patch.object(mod, "_reject_obvious_local_target"),
            ):
                auth.request(
                    "cults3d",
                    "GET",
                    "https://cults3d.com/en/tags/benchy",
                    session=session,
                    headers={"User-Agent": "plugin/1.0"},
                )

            self.assertEqual(captured["headers"]["User-Agent"], "Mozilla/5.0 Real")
            self.assertEqual(
                session.cookies.get("cf_clearance", domain="cults3d.com"), "abc"
            )

    def test_the_clearance_cookie_is_scoped_to_the_host_that_earned_it(self):
        with tempfile.TemporaryDirectory() as td:
            auth = self.manager(td)
            auth.clearance.save("cults3d.com", "cf_clearance=abc", "Mozilla/5.0 Real")
            session = requests.Session()
            headers = {"User-Agent": "plugin/1.0"}
            auth._apply_clearance(session, "https://cults3d.com/en", headers)

            self.assertEqual(
                session.cookies.get("cf_clearance", domain="cults3d.com"), "abc"
            )
            # A redirect to somewhere else must not carry the clearance along.
            self.assertIsNone(
                session.cookies.get("cf_clearance", domain="cdn.example.com")
            )
            self.assertIsNone(
                session.cookies.get("cf_clearance", domain="attacker.example")
            )

    def test_a_stored_clearance_replaces_one_pasted_with_the_session(self):
        """A browser visit that passed the check leaves cf_clearance in the
        Cookie header the user copies for the login. Sending that copy as well
        would leave Cloudflare choosing between two values for one name, and
        only the stored one has a known User-Agent behind it."""
        with tempfile.TemporaryDirectory() as td:
            auth = self.manager(td)
            auth.save_token(
                "cults3d", "session=SESS; cf_clearance=STALE; other=1", label="x"
            )
            auth.clearance.save("cults3d.com", "cf_clearance=FRESH", "UA/Real")
            session = auth.session("cults3d")
            headers = {"User-Agent": "plugin/1.0"}
            auth._apply_clearance(session, "https://cults3d.com/en", headers)

            values = [c.value for c in session.cookies if c.name == "cf_clearance"]
            self.assertEqual(values, ["FRESH"])
            self.assertEqual(headers["User-Agent"], "UA/Real")
            # The rest of the pasted session must survive untouched.
            self.assertEqual(
                {c.name for c in session.cookies},
                {"session", "other", "cf_clearance"},
            )

    def test_no_clearance_leaves_the_request_exactly_as_before(self):
        with tempfile.TemporaryDirectory() as td:
            auth = self.manager(td)
            session = requests.Session()
            headers = {"User-Agent": "plugin/1.0"}
            auth._apply_clearance(session, "https://cults3d.com/en", headers)
            self.assertEqual(headers["User-Agent"], "plugin/1.0")
            self.assertEqual(len(session.cookies), 0)


class ChallengeReportingTests(unittest.TestCase):
    def test_challenge_names_the_blocked_host_and_the_handoff(self):
        url = "https://cults3d.com/en/tags/benchy"
        session = mock.Mock()
        session.cookies = requests.cookies.RequestsCookieJar()
        session.request.side_effect = [challenge_response(url) for _ in range(2)]
        with (
            mock.patch("requests.Session", return_value=session),
            mock.patch.object(mod, "_reject_obvious_local_target"),
            self.assertRaises(mod.CloudflareChallenge) as raised,
        ):
            mod._fetch_html(url)

        self.assertEqual(raised.exception.host, "cults3d.com")
        self.assertEqual(raised.exception.url, url)
        self.assertIn("cf_clearance", str(raised.exception))

    def test_a_clearance_that_stopped_working_is_discarded_and_explained(self):
        url = "https://cults3d.com/en/tags/benchy"
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(
                mod.AuthStore(os.path.join(td, "sessions.json"))
            )
            auth.clearance.save("cults3d.com", "cf_clearance=stale", "UA/1")
            session = mock.Mock()
            session.cookies = requests.cookies.RequestsCookieJar()
            session.request.side_effect = [challenge_response(url) for _ in range(2)]

            with (
                mock.patch.object(auth, "session", return_value=session),
                mock.patch.object(mod, "_reject_obvious_local_target"),
                self.assertRaises(mod.CloudflareChallenge) as raised,
            ):
                mod._fetch_html(url, auth=auth, platform="cults3d")

            message = str(raised.exception)
            self.assertIn("no longer accepted", message)
            self.assertIn("IP address", message)
            # The dead clearance must not linger and keep failing silently.
            self.assertEqual(auth.clearance.for_url(url), {})

    def test_thangs_search_replays_the_clearance_for_its_api_host(self):
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(
                mod.AuthStore(os.path.join(td, "sessions.json"))
            )
            auth.clearance.save("thangs.com", "cf_clearance=abc", "Mozilla/5.0 Real")
            captured = {}

            def fake_get(url, **kwargs):
                captured["headers"] = dict(kwargs.get("headers") or {})
                captured["cookies"] = dict(kwargs.get("cookies") or {})
                return FakeResponse(url=url)

            with mock.patch("requests.get", side_effect=fake_get):
                mod.ThangsSearcher._request({"searchTerm": "x"}, "x", auth)

            self.assertEqual(captured["cookies"], {"cf_clearance": "abc"})
            self.assertEqual(captured["headers"]["User-Agent"], "Mozilla/5.0 Real")

    def test_thangs_challenge_points_at_the_page_but_names_the_api_host(self):
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(
                mod.AuthStore(os.path.join(td, "sessions.json"))
            )
            api_url = mod.ThangsSearcher.SEARCH_URL
            with (
                mock.patch(
                    "requests.get",
                    side_effect=lambda url, **kwargs: challenge_response(api_url),
                ),
                self.assertRaises(mod.CloudflareChallenge) as raised,
            ):
                mod.ThangsSearcher._request({"searchTerm": "x"}, "benchy", auth)

            self.assertEqual(raised.exception.host, "production-api.thangs.com")
            self.assertIn("thangs.com/search/benchy", raised.exception.url)


class CoordinatorHandlerTests(unittest.TestCase):
    def action(self, directory):
        script = load_script_module()
        action = script.SearchEngineScript()
        action.auth = script.AuthManager(
            script.AuthStore(os.path.join(directory, "sessions.json"))
        )
        posts = []
        action._post = posts.append
        return script, action, posts

    def test_saving_a_verification_reports_it_back_to_the_ui(self):
        with tempfile.TemporaryDirectory() as td:
            _script, action, posts = self.action(td)
            action._do_cloudflare_save(
                {
                    "host": "https://cults3d.com/en",
                    "cookie": "cf_clearance=abc",
                    "user_agent": "Mozilla/5.0 Real",
                }
            )
            self.assertEqual(posts[-1]["action"], "auth_changed")
            self.assertEqual(
                [row["host"] for row in posts[-1]["cloudflare"]], ["cults3d.com"]
            )
            self.assertIn("cults3d.com", posts[-1]["message"])

    def test_saving_drops_the_inbound_cookie_copy(self):
        with tempfile.TemporaryDirectory() as td:
            _script, action, _posts = self.action(td)
            message = {
                "host": "cults3d.com",
                "cookie": "cf_clearance=abc",
                "user_agent": "Mozilla/5.0 Real",
            }
            action._do_cloudflare_save(message)
            self.assertNotIn("cookie", message)

    def test_a_rejected_verification_is_reported_as_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            _script, action, posts = self.action(td)
            action._do_cloudflare_save(
                {"host": "cults3d.com", "cookie": "cf_clearance=abc", "user_agent": ""}
            )
            self.assertEqual(posts[-1]["action"], "error")
            self.assertIn("User-Agent", posts[-1]["message"])

    def test_forgetting_a_host_removes_it(self):
        with tempfile.TemporaryDirectory() as td:
            _script, action, posts = self.action(td)
            action.auth.clearance.save("cults3d.com", "cf_clearance=abc", "UA/1")
            action._handle_cloudflare_forget({"host": "cults3d.com"})
            self.assertEqual(posts[-1]["cloudflare"], [])
            self.assertIn("removed", posts[-1]["message"])

    def test_forgetting_an_unknown_host_says_so(self):
        with tempfile.TemporaryDirectory() as td:
            _script, action, posts = self.action(td)
            action._handle_cloudflare_forget({"host": "example.com"})
            self.assertIn("No stored Cloudflare verification", posts[-1]["message"])

    def test_the_message_router_exposes_both_actions(self):
        with tempfile.TemporaryDirectory() as td:
            _script, action, _posts = self.action(td)
            started = []
            action._start = lambda target, *args: started.append(target.__name__)
            action.on_message(
                {
                    "action": "cloudflare_save",
                    "host": "cults3d.com",
                    "cookie": "cf_clearance=abc",
                    "user_agent": "UA/1",
                }
            )
            self.assertEqual(started, ["_do_cloudflare_save"])


class EmbeddedUiTests(unittest.TestCase):
    def test_the_verification_panel_is_present(self):
        for marker in (
            'id="cloudflare-modal"',
            'id="cf-host"',
            'id="cf-cookie"',
            'id="cf-agent"',
            "function submitCloudflare()",
            "function forgetCloudflare()",
            "function openCloudflare(host,url)",
            "cloudflare_save",
            "cloudflare_forget",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, mod.PAGE)

    def test_the_agent_field_is_not_prefilled_from_this_window(self):
        """The webview is not the browser that passed the check."""
        self.assertNotIn("navigator.userAgent", mod.PAGE)

    def test_a_blocked_search_source_offers_the_handoff(self):
        self.assertIn("source-verify", mod.PAGE)
        self.assertIn("cloudflare_host", mod.PAGE)
        self.assertIn("cloudflare_required", mod.PAGE)


if __name__ == "__main__":
    unittest.main()
