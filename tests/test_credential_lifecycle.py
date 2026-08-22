"""Regressions for the credential store's behaviour under pressure.

Every case here reproduces a defect the plugin actually shipped: a secret that
outlived the control meant to erase it, a write that survived the erase, a lock
that only looked like one, and a refusal from a storage host being read as the
portal rejecting the account. They are grouped here rather than spread across
the adapter tests because what they have in common is the credential file, not
any one portal.
"""

import os
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from tests._module_loader import load_plugin

mod = load_plugin("search_engine_credentials")


def load_script_module():
    """Load the plugin with a stub `orca` so the coordinator class exists."""
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
        return load_plugin("search_engine_credentials_script")
    finally:
        if previous is None:
            sys.modules.pop("orca", None)
        else:
            sys.modules["orca"] = previous

BEARER = "0123456789abcdef0123456789abcdef01234567"


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "sessions.json")

    def auth(self):
        return mod.AuthManager(mod.AuthStore(self.path))

    def on_disk(self):
        if not os.path.exists(self.path):
            return ""
        with open(self.path, encoding="utf-8") as handle:
            return handle.read()


class ClearanceAdoptionTests(StoreFixture):
    def test_a_bare_token_is_never_adopted_as_a_clearance(self):
        # The panel accepts a bare value because the user knowingly pastes a
        # clearance there; adoption from a portal credential must not.
        self.assertEqual(mod.CloudflareClearance.parse_cookie(BEARER), BEARER)
        self.assertEqual(
            mod.CloudflareClearance.parse_cookie(BEARER, named_only=True), ""
        )
        self.assertEqual(
            mod.CloudflareClearance.parse_cookie(
                "cf_clearance=real-value; other=1", named_only=True
            ),
            "real-value",
        )

    def test_a_browser_sign_in_does_not_file_the_token_as_a_clearance(self):
        # Drives the real hand-off, so it fails if _adopt_clearance stops
        # asking for the cookie by name -- checking parse_cookie alone would
        # not notice the call site losing the guard.
        script = load_script_module()
        action = script.SearchEngineScript()
        action.auth = mod.AuthManager(mod.AuthStore(self.path))
        action._post = lambda message: None

        action._store_login_credential("thingiverse", BEARER, "Mozilla/5.0 Test")

        self.assertTrue(action.auth.authenticated("thingiverse"))
        self.assertEqual(
            action.auth.clearance.status(),
            [],
            "a bare API token must not be replayed as a Cloudflare cookie",
        )
        action.auth.logout("thingiverse")
        self.assertNotIn(BEARER, self.on_disk(), "the token outlived Forget session")

    def test_a_real_clearance_arriving_with_a_cookie_header_is_kept(self):
        script = load_script_module()
        action = script.SearchEngineScript()
        action.auth = mod.AuthManager(mod.AuthStore(self.path))
        action._post = lambda message: None

        action._store_login_credential(
            "cults3d", "cf_clearance=real-one; session=abc", "Mozilla/5.0 Test"
        )

        self.assertEqual(
            [record["host"] for record in action.auth.clearance.status()],
            ["cults3d.com"],
        )

    def test_forgetting_a_session_also_forgets_its_clearance(self):
        auth = self.auth()
        auth.save_token("cults3d", "cf_clearance=abc; session=xyz")
        auth.clearance.save("cults3d.com", "cf_clearance=abc", "Mozilla/5.0 Test")
        self.assertEqual([r["host"] for r in auth.clearance.status()], ["cults3d.com"])

        auth.logout("cults3d")

        self.assertEqual(auth.clearance.status(), [])
        self.assertNotIn("abc", self.on_disk())


class StoreConcurrencyTests(StoreFixture):
    def test_every_store_over_one_file_shares_a_lock(self):
        # Search workers build their own AuthManager over the same path.
        self.assertIs(mod.AuthStore(self.path)._lock, mod.AuthStore(self.path)._lock)

    def test_concurrent_clearance_saves_do_not_lose_records(self):
        def save(index):
            clearance = mod.CloudflareClearance(mod.AuthStore(self.path))
            clearance.save(
                f"host{index}.example.com", f"cf_clearance=t{index}", f"UA {index}"
            )

        threads = [threading.Thread(target=save, args=(i,)) for i in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        stored = mod.CloudflareClearance(mod.AuthStore(self.path)).status()
        self.assertEqual(len(stored), 24)

    def test_an_erase_refuses_a_write_that_was_already_in_flight(self):
        auth = self.auth()
        auth.save_token("thingiverse", "existing")
        # A sign-in captures the epoch, then blocks on the network...
        epoch = auth.store.epoch()
        summary = auth.forget_all()
        self.assertEqual(summary["portals"], 1)
        self.assertFalse(os.path.exists(self.path))

        # ...and lands after the user erased everything.
        with self.assertRaises(mod.AuthError):
            auth.save_token("thingiverse", "fresh", epoch=epoch)

        self.assertFalse(
            os.path.exists(self.path), "the erased file must not be recreated"
        )

    def test_a_write_started_after_the_erase_still_succeeds(self):
        auth = self.auth()
        auth.forget_all()
        auth.save_token("thingiverse", "fresh", epoch=auth.store.epoch())
        self.assertTrue(auth.authenticated("thingiverse"))


class LoginReceiverLifecycleTests(StoreFixture):
    def test_stopping_the_receiver_retires_it(self):
        stored = []
        receiver = mod.LoginReceiver("thingiverse", lambda *args: stored.append(args))
        receiver.stop()

        accepted, message = receiver.submit(BEARER, "UA")

        self.assertFalse(accepted)
        self.assertEqual(stored, [], "a cancelled sign-in must not store anything")
        self.assertIn("already been used", message)

    def test_a_refused_credential_leaves_the_link_usable(self):
        attempts = []

        def on_credential(platform, value, user_agent):
            attempts.append(value)
            if len(attempts) == 1:
                raise mod.AuthError("Token is empty")

        receiver = mod.LoginReceiver("thingiverse", on_credential)
        self.assertFalse(receiver.submit("", "UA")[0])
        self.assertTrue(receiver.submit(BEARER, "UA")[0])
        self.assertEqual(attempts, ["", BEARER])

    def test_one_credential_only_even_when_two_submissions_race(self):
        stored, gate = [], threading.Barrier(2)

        def on_credential(platform, value, user_agent):
            gate.wait(timeout=5)
            stored.append(value)

        receiver = mod.LoginReceiver("thingiverse", on_credential)
        results = []

        def submit():
            results.append(receiver.submit(BEARER, "UA")[0])

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        # The loser never reaches the callback, so release the barrier itself.
        gate.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(len(stored), 1)
        self.assertEqual(sorted(results), [False, True])


class DownloadRefusalTests(StoreFixture):
    class Response:
        def __init__(self, status, url):
            self.status_code = status
            self.url = url
            self.headers = {}

        def raise_for_status(self):
            raise RuntimeError(f"HTTP {self.status_code}")

        def iter_content(self, chunk_size=1):
            yield b""

        def close(self):
            pass

    def _download(self, auth, url):
        with (
            mock.patch.object(auth, "request", return_value=self.Response(403, url)),
            mock.patch.object(mod, "_reject_obvious_local_target"),
        ):
            return mod._download_stream(url, "x.zip", self._tmp.name, auth, "thangs")

    def test_a_storage_host_refusal_is_not_an_expired_session(self):
        auth = self.auth()
        auth.save_token("thangs", "valid-token")
        with self.assertRaises(RuntimeError) as raised:
            self._download(auth, "https://thangs-media.s3.amazonaws.com/x.zip")
        self.assertNotIsInstance(raised.exception, mod.AuthRequired)
        self.assertIn("expired", str(raised.exception))
        self.assertTrue(
            auth.authenticated("thangs"),
            "a stale download link must not cost the user their token",
        )

    def test_the_portal_host_refusing_is_an_expired_session(self):
        auth = self.auth()
        auth.save_token("thangs", "valid-token")
        with self.assertRaises(mod.AuthRequired):
            self._download(auth, "https://production-api.thangs.com/v2/models/1/x.zip")

    def test_probe_refusals_are_scoped_to_the_portal_host(self):
        cdn = self.Response(403, "https://files.cdn.example/model.stl")
        portal = self.Response(403, "https://cults3d.com/en/3d-model/x")
        self.assertIsNone(mod._probe_result(cdn, "cults3d"))
        with self.assertRaises(mod.AuthRequired):
            mod._probe_result(portal, "cults3d")


class FilePickerSelectionTests(unittest.TestCase):
    def test_only_files_the_slicer_can_open_are_pre_ticked(self):
        files = mod._normalize_download_files(
            [
                {"url": "https://x/1", "name": "Bracket.STL"},
                {"url": "https://x/2", "name": "Bracket.pdf"},
                {"url": "https://x/3", "name": "Assembly.SLDASM"},
                {"url": "https://x/4", "name": "bundle.zip"},
                {"url": "https://x/5", "name": "no-extension"},
            ]
        )
        self.assertEqual(
            {item["name"]: item["selected"] for item in files},
            {
                "Bracket.STL": True,
                "Bracket.pdf": False,
                "Assembly.SLDASM": False,
                "bundle.zip": True,
                "no-extension": True,
            },
        )

    def test_the_picker_honours_the_selected_flag(self):
        self.assertIn("f.selected===false?'':' checked'", mod.PAGE)


if __name__ == "__main__":
    unittest.main()
