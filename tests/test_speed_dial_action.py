import sys
import threading
import time
import types
import unittest
from unittest import mock

from tests._module_loader import load_plugin


class FakeWindow:
    def __init__(self):
        self.open = True
        self.posts = []
        self.closed = False

    def is_open(self):
        return self.open

    def post(self, message):
        self.posts.append(message)

    def close(self):
        self.closed = True
        self.open = False


class FakeUI:
    def __init__(self):
        self.created = []

    def create_window(self, **kwargs):
        win = FakeWindow()
        self.created.append((win, kwargs))
        return win


def load_with_fake_orca():
    fake = types.ModuleType("orca")

    class ScriptPluginCapabilityBase:
        def __init__(self, *args, **kwargs):
            pass

    class Base:
        pass

    class ExecutionResult:
        @staticmethod
        def success():
            return "success"

    ui = FakeUI()
    fake.script = types.SimpleNamespace(
        ScriptPluginCapabilityBase=ScriptPluginCapabilityBase
    )
    fake.base = Base
    fake.ExecutionResult = ExecutionResult
    fake.host = types.SimpleNamespace(ui=ui)
    fake.plugin = lambda cls: cls
    fake.register_capability = lambda capability: None

    previous = sys.modules.get("orca")
    sys.modules["orca"] = fake
    try:
        module = load_plugin("search_engine_speed_dial")
    finally:
        if previous is None:
            sys.modules.pop("orca", None)
        else:
            sys.modules["orca"] = previous
    return module, ui


class SpeedDialActionTests(unittest.TestCase):
    def test_action_name_is_search_3d_models(self):
        module, _ = load_with_fake_orca()
        action = module.SearchEngineScript()
        self.assertEqual(action.get_name(), "Search 3D Models")

    def test_repeated_execute_reuses_existing_window(self):
        module, ui = load_with_fake_orca()
        action = module.SearchEngineScript()
        self.assertEqual(action.execute(), "success")
        self.assertEqual(len(ui.created), 1)
        first_window = action.win
        self.assertEqual(ui.created[0][1]["title"], "Search 3D Models")
        self.assertEqual(action.execute(), "success")
        self.assertEqual(len(ui.created), 1)
        self.assertIs(action.win, first_window)
        self.assertFalse(first_window.closed)
        self.assertEqual(first_window.posts[-1], {"action": "activate_search"})

    def test_closed_window_is_recreated(self):
        module, ui = load_with_fake_orca()
        action = module.SearchEngineScript()
        action.execute()
        first_window = action.win
        first_window.open = False
        action.execute()
        self.assertEqual(len(ui.created), 2)
        self.assertIsNot(action.win, first_window)

    def test_search_more_merges_pages_and_reports_source_progress(self):
        module, _ = load_with_fake_orca()
        action = module.SearchEngineScript()
        action.win = FakeWindow()

        def search(_query, _auth, options):
            page = options["page"]
            row = {
                "name": f"Model {page}",
                "platform": "Printables",
                "url": f"https://www.printables.com/model/{page}",
            }
            return module.SearchPage(
                [row], total=2, has_more=page == 1
            )

        with mock.patch.object(
            module.PrintablesSearcher, "search", side_effect=search
        ):
            action._search_generation = 1
            action._do_search(
                {
                    "query": "cube",
                    "platforms": ["printables"],
                    "options": {},
                },
                1,
            )
            first = action.win.posts[-1]
            self.assertFalse(first["append"])
            self.assertTrue(first["can_load_more"])
            self.assertEqual(first["sources"][0]["loaded"], 1)

            action._do_search_more(1)
            second = action.win.posts[-1]
            self.assertTrue(second["append"])
            self.assertFalse(second["can_load_more"])
            self.assertEqual(len(second["results"]), 2)
            self.assertEqual(second["sources"][0]["loaded"], 2)
            self.assertEqual(second["sources"][0]["visible"], 2)

    def test_selected_portals_search_concurrently_but_merge_in_order(self):
        module, _ = load_with_fake_orca()
        action = module.SearchEngineScript()
        action.win = FakeWindow()
        action._search_generation = 1
        lock = threading.Lock()
        started = set()
        both_started = threading.Event()

        def search_for(key, display, delay):
            def search(_query, _auth, _options):
                with lock:
                    started.add(key)
                    if len(started) == 2:
                        both_started.set()
                if not both_started.wait(1):
                    raise AssertionError("portal searches ran sequentially")
                time.sleep(delay)
                return module.SearchPage(
                    [{"name": display, "platform": display, "url": f"https://{key}.example/model"}],
                    has_more=False,
                )

            return search

        with (
            mock.patch.object(
                module.PrintablesSearcher,
                "search",
                side_effect=search_for("printables", "Printables", 0.05),
            ),
            mock.patch.object(
                module.MakeronlineSearcher,
                "search",
                side_effect=search_for("makeronline", "Makeronline", 0),
            ),
        ):
            action._do_search(
                {
                    "query": "cup",
                    "platforms": ["printables", "makeronline"],
                    "options": {},
                },
                1,
            )

        self.assertEqual(
            [row["_platform_key"] for row in action._search_results],
            ["printables", "makeronline"],
        )

    def test_auth_status_is_resolved_once_per_portal_page(self):
        module, _ = load_with_fake_orca()
        action = module.SearchEngineScript()
        rows = [
            {
                "name": f"Model {index}",
                "platform": "Makeronline",
                "requires_auth": True,
            }
            for index in range(30)
        ]
        with (
            mock.patch.object(
                module.MakeronlineSearcher, "search", return_value=rows
            ),
            mock.patch.object(
                action.auth, "authenticated", return_value=True
            ) as authenticated,
        ):
            loaded, _total, _more = action._load_search_page(
                module._PLATFORMS["makeronline"], "cup", {}, 1
            )
        self.assertEqual(len(loaded), 30)
        authenticated.assert_called_once_with("makeronline")

    def test_search_reports_cloudflare_browser_fallback_url(self):
        module, _ = load_with_fake_orca()
        action = module.SearchEngineScript()
        action.win = FakeWindow()
        action._search_generation = 1
        url = "https://cults3d.com/en/tags/benchy"

        with mock.patch.object(
            module.Cults3DSearcher,
            "search",
            side_effect=module.CloudflareChallenge("Browser verification required", url),
        ):
            action._do_search(
                {"query": "benchy", "platforms": ["cults3d"], "options": {}},
                1,
            )

        source = action.win.posts[-1]["sources"][0]
        self.assertEqual(source["error"], "Browser verification required")
        self.assertEqual(source["browser_url"], url)
        self.assertFalse(source["has_more"])

    def test_unknown_thingiverse_license_is_loaded_in_background(self):
        module, _ = load_with_fake_orca()
        action = module.SearchEngineScript()
        action.win = FakeWindow()
        action._search_generation = 3
        model = {
            "_platform_key": "thingiverse",
            "_thing_id": 42,
            "platform": "Thingiverse",
            "name": "Holder",
            "url": "https://www.thingiverse.com/thing:42",
            "license": "Unknown",
            "_details_available": True,
            "_details_loaded": False,
        }
        action._search_results = [model]
        candidates = action._prepare_thingiverse_prefetch(
            action._search_results, 3
        )
        self.assertEqual(len(candidates), 1)
        self.assertTrue(action._search_results[0]["_details_loading"])

        details = {
            **model,
            "license": "CC BY",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "_details_loaded": True,
        }
        with mock.patch.object(
            module.ThingiverseSearcher, "get_details", return_value=details
        ):
            action._prefetch_thingiverse_details(3, candidates)

        self.assertEqual(action._search_results[0]["license"], "CC BY")
        self.assertFalse(action._search_results[0]["_details_loading"])
        self.assertTrue(action.win.posts[-1]["background"])
        self.assertEqual(action.win.posts[-1]["model"]["license"], "CC BY")

    def test_known_thingiverse_license_skips_background_request(self):
        module, _ = load_with_fake_orca()
        action = module.SearchEngineScript()
        model = {
            "_platform_key": "thingiverse",
            "_thing_id": 7,
            "license": "CC0",
            "_details_loaded": False,
        }
        self.assertEqual(action._prepare_thingiverse_prefetch([model], 1), [])


if __name__ == "__main__":
    unittest.main()
