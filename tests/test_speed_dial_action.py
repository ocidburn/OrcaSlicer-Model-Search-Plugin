import sys
import types
import unittest

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


if __name__ == "__main__":
    unittest.main()
