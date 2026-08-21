"""Erasing every stored authorization detail.

This control exists to be trusted, so the tests care about what is left behind
as much as what the interface says: the file goes rather than being blanked, a
sign-in still in flight cannot write a credential back moments later, and the
message reports what actually went instead of claiming success over an empty
store.
"""

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests._module_loader import load_plugin

mod = load_plugin("search_engine_forget_all")


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
        return load_plugin("search_engine_forget_all_script")
    finally:
        if previous is None:
            sys.modules.pop("orca", None)
        else:
            sys.modules["orca"] = previous


class StoreEraseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "sessions.json")

    def test_the_file_is_removed_rather_than_blanked(self):
        store = mod.AuthStore(self.path)
        store.set("cults3d", {"auth_token": "SECRETVALUE"})
        self.assertTrue(os.path.isfile(self.path))

        store.clear()

        self.assertFalse(
            os.path.exists(self.path),
            "an emptied file still shows a credential file was there",
        )
        self.assertEqual(store.load(), {})

    def test_no_trace_of_the_secret_is_left_in_the_directory(self):
        store = mod.AuthStore(self.path)
        store.set("cults3d", {"auth_token": "SECRETVALUE"})
        store.clear()
        for name in os.listdir(self._tmp.name):
            with open(os.path.join(self._tmp.name, name), "rb") as handle:
                self.assertNotIn(b"SECRETVALUE", handle.read(), name)

    def test_erasing_an_absent_file_is_not_an_error(self):
        store = mod.AuthStore(self.path)
        store.clear()
        store.clear()
        self.assertEqual(store.load(), {})

    def test_an_unremovable_file_is_at_least_emptied(self):
        store = mod.AuthStore(self.path)
        store.set("cults3d", {"auth_token": "SECRETVALUE"})
        with mock.patch.object(mod.os, "remove", side_effect=OSError("locked")):
            store.clear()
        self.assertEqual(store.load(), {})
        with open(self.path, "rb") as handle:
            self.assertNotIn(b"SECRETVALUE", handle.read())


class ForgetAllTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.auth = mod.AuthManager(
            mod.AuthStore(os.path.join(self._tmp.name, "sessions.json"))
        )

    def test_sessions_and_clearances_both_go(self):
        self.auth.save_token("cults3d", "session=a; b=c")
        self.auth.save_token("thingiverse", "tv-token")
        self.auth.clearance.save("cults3d.com", "cf_clearance=abc", "UA/1")

        summary = self.auth.forget_all()

        self.assertEqual(summary, {"portals": 2, "clearances": 1})
        self.assertFalse(self.auth.authenticated("cults3d"))
        self.assertFalse(self.auth.authenticated("thingiverse"))
        self.assertEqual(self.auth.clearance.status(), [])
        self.assertTrue(
            all(not row["authenticated"] for row in self.auth.status().values())
        )

    def test_an_empty_store_reports_nothing_removed(self):
        self.assertEqual(self.auth.forget_all(), {"portals": 0, "clearances": 0})

    def test_the_clearance_slot_is_not_counted_as_a_portal(self):
        self.auth.clearance.save("cults3d.com", "cf_clearance=abc", "UA/1")
        self.auth.clearance.save("grabcad.com", "cf_clearance=def", "UA/1")
        self.assertEqual(
            self.auth.forget_all(), {"portals": 0, "clearances": 2}
        )

    def test_a_stale_key_from_a_removed_portal_is_not_counted(self):
        """Keys left by portals that no longer exist should not inflate the
        count, but must still be erased."""
        self.auth.store.set("smithsonian", {"auth_token": "old"})
        self.auth.save_token("cults3d", "session=a")
        self.assertEqual(self.auth.forget_all(), {"portals": 1, "clearances": 0})
        self.assertEqual(self.auth.store.load(), {})


class CoordinatorForgetTests(unittest.TestCase):
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

    def test_the_message_names_what_was_removed(self):
        self.action.auth.save_token("cults3d", "session=a")
        self.action.auth.save_token("grabcad", "session=b")
        self.action.auth.clearance.save("cults3d.com", "cf_clearance=x", "UA/1")

        self.action._do_forget_all()

        message = self.posts[-1]["message"]
        self.assertEqual(self.posts[-1]["action"], "auth_changed")
        self.assertIn("2 portal sessions", message)
        self.assertIn("1 Cloudflare verification", message)
        self.assertEqual(self.posts[-1]["cloudflare"], [])

    def test_a_single_item_is_not_pluralised(self):
        self.action.auth.save_token("cults3d", "session=a")
        self.action._do_forget_all()
        self.assertIn("1 portal session ", self.posts[-1]["message"] + " ")
        self.assertNotIn("sessions", self.posts[-1]["message"])

    def test_an_empty_store_says_so_instead_of_claiming_success(self):
        self.action._do_forget_all()
        self.assertIn("no saved authorization data", self.posts[-1]["message"])

    def test_a_sign_in_still_in_flight_is_cancelled_first(self):
        """Otherwise it would write a fresh credential moments after the erase."""
        with mock.patch.object(
            self.script.webbrowser, "open", return_value=True
        ):
            self.action._do_start_login("cults3d")
        with self.action._login_lock:
            self.assertIsNotNone(self.action._login)

        self.action._do_forget_all()

        with self.action._login_lock:
            self.assertIsNone(self.action._login)

    def test_a_failure_to_erase_is_reported_not_swallowed(self):
        self.action.auth.save_token("cults3d", "session=a")
        with mock.patch.object(
            self.script.AuthStore, "clear", side_effect=OSError("device busy")
        ):
            self.action._do_forget_all()
        self.assertEqual(self.posts[-1]["action"], "error")
        self.assertIn("device busy", self.posts[-1]["message"])
        # The credential must survive a failed erase rather than appear gone.
        self.assertTrue(self.action.auth.authenticated("cults3d"))

    def test_the_router_exposes_the_action(self):
        started = []
        self.action._start = lambda target, *args: started.append(target.__name__)
        self.action.on_message({"action": "auth_forget_all"})
        self.assertEqual(started, ["_do_forget_all"])


class ForgetAllUiTests(unittest.TestCase):
    def test_the_control_is_present(self):
        for marker in (
            'id="forget-all"',
            "function forgetAllAuth()",
            "auth_forget_all",
            "Delete all authorization data",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, mod.PAGE)

    def test_erasing_needs_a_second_click(self):
        """An irreversible erase should not fire on a single stray click."""
        self.assertIn("Click again to erase everything", mod.PAGE)
        self.assertIn("forgetArmed", mod.PAGE)

    def test_no_reliance_on_a_browser_confirm_dialog(self):
        """The embedded webview is not guaranteed to present window.confirm."""
        self.assertNotIn("confirm(", mod.PAGE)

    def test_the_button_is_restored_after_the_answer_arrives(self):
        self.assertIn("resetForgetButton()", mod.PAGE)


if __name__ == "__main__":
    unittest.main()
