import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(__file__)


class FakeWindow:
    def __init__(self):
        self.posts = []
        self.open = True

    def is_open(self):
        return self.open

    def post(self, message):
        self.posts.append(message)

    def close(self):
        self.open = False


class FakeUI:
    def create_window(self, **kwargs):
        return FakeWindow()


def load_module():
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

    fake.script = types.SimpleNamespace(
        ScriptPluginCapabilityBase=ScriptPluginCapabilityBase
    )
    fake.base = Base
    fake.ExecutionResult = ExecutionResult
    fake.host = types.SimpleNamespace(ui=FakeUI())
    fake.plugin = lambda cls: cls
    fake.register_capability = lambda capability: None
    previous = sys.modules.get("orca")
    sys.modules["orca"] = fake
    try:
        spec = importlib.util.spec_from_file_location(
            "search_engine_import_flow", os.path.join(HERE, "search_engine.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop("orca", None)
        else:
            sys.modules["orca"] = previous
    return module


class ImportHandoffTests(unittest.TestCase):
    def test_non_windows_handoff_does_not_use_invalid_single_instance_cli_flag(self):
        mod = load_module()
        with tempfile.NamedTemporaryFile(suffix=".stl") as fh:
            completed = types.SimpleNamespace(returncode=0, stderr="")
            with (
                mock.patch.object(
                    mod, "_current_orca_executable", return_value="/opt/OrcaSlicer"
                ),
                mock.patch.object(mod.os, "name", "posix"),
                mock.patch.object(mod.subprocess, "run", return_value=completed) as run,
            ):
                ok, detail = mod._load_in_orca([fh.name])
        self.assertTrue(ok)
        self.assertEqual(detail, "")
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "/opt/OrcaSlicer")
        self.assertNotIn("--single-instance", argv)
        self.assertEqual(argv[1], os.path.abspath(fh.name))

    def test_windows_handoff_uses_native_ipc_without_spawning_orca(self):
        mod = load_module()
        with (
            tempfile.NamedTemporaryFile(suffix=".stl") as fh,
            mock.patch.object(
                mod,
                "_current_orca_executable",
                return_value=r"C:\Program Files\OrcaSlicer\OrcaSlicer.exe",
            ),
            mock.patch.object(mod.os, "name", "nt"),
            mock.patch.object(
                mod, "_send_windows_instance_message", return_value=(True, "")
            ) as send,
            mock.patch.object(mod.subprocess, "run") as run,
        ):
            ok, detail = mod._load_in_orca([fh.name])
        self.assertTrue(ok)
        self.assertEqual(detail, "")
        send.assert_called_once()
        run.assert_not_called()

    def test_escape_strings_cstyle_matches_orca_argv_format(self):
        mod = load_module()
        payload = mod._escape_strings_cstyle(
            [r"C:\Program Files\OrcaSlicer.exe", r"C:\Models\part one.stl"]
        )
        self.assertEqual(
            payload, r'"C:\\Program Files\\OrcaSlicer.exe";"C:\\Models\\part one.stl"'
        )

    def test_load_in_orca_rejects_missing_file(self):
        mod = load_module()
        ok, detail = mod._load_in_orca(
            [os.path.join(tempfile.gettempdir(), "definitely-missing-model.stl")]
        )
        self.assertFalse(ok)
        self.assertIn("no longer exists", detail)


class MultiFileSelectionTests(unittest.TestCase):
    def make_action(self):
        mod = load_module()
        action = mod.SearchEngineScript()
        action.win = FakeWindow()
        return mod, action

    def test_multiple_resolved_files_show_checkbox_choices_before_download(self):
        mod, action = self.make_action()
        model = {"platform": "Printables", "name": "Demo", "requires_auth": False}
        resolver = lambda model, auth: [
            {"name": "part-a.stl", "url": "https://cdn.example/a.stl"},
            {"name": "part-b.stl", "url": "https://cdn.example/b.stl"},
        ]
        with (
            mock.patch.object(
                mod._PLATFORMS["printables"].adapter, "get_files", side_effect=resolver
            ),
            mock.patch.object(action, "_download_and_import") as download,
        ):
            action._resolve_import(model)
        download.assert_not_called()
        msg = action.win.posts[-1]
        self.assertEqual(msg["action"], "file_choices")
        self.assertEqual(
            [x["name"] for x in msg["files"]], ["part-a.stl", "part-b.stl"]
        )
        self.assertEqual([x["index"] for x in msg["files"]], [0, 1])

    def test_single_resolved_file_imports_immediately(self):
        mod, action = self.make_action()
        model = {"platform": "Printables", "name": "Demo", "requires_auth": False}
        files = [{"name": "only.stl", "url": "https://cdn.example/only.stl"}]
        with (
            mock.patch.object(
                mod._PLATFORMS["printables"].adapter, "get_files", return_value=files
            ),
            mock.patch.object(action, "_download_and_import") as download,
        ):
            action._resolve_import(model)
        download.assert_called_once()
        self.assertEqual(download.call_args.args[1], files)

    def test_only_checked_indices_are_downloaded(self):
        _mod, action = self.make_action()
        model = {"platform": "Printables", "name": "Demo", "requires_auth": False}
        files = [
            {"name": "a.stl", "url": "https://cdn.example/a.stl"},
            {"name": "b.stl", "url": "https://cdn.example/b.stl"},
            {"name": "c.stl", "url": "https://cdn.example/c.stl"},
        ]
        action._pending_import_model = model
        action._pending_import_files = files
        with mock.patch.object(action, "_download_and_import") as download:
            action._import_selected([2, 0])
        download.assert_called_once_with(model, [files[2], files[0]])
        self.assertIsNone(action._pending_import_model)
        self.assertEqual(action._pending_import_files, [])

    def test_ui_contains_multifile_checkbox_picker(self):
        mod = load_module()
        self.assertIn('id="file-modal"', mod.PAGE)
        self.assertIn("file-choice", mod.PAGE)
        self.assertIn("import_selected", mod.PAGE)
        self.assertIn("Select all", mod.PAGE)
        self.assertIn("Select none", mod.PAGE)

    def test_detail_import_panel_closes_on_click_outside(self):
        mod = load_module()
        self.assertIn("document.addEventListener('pointerdown'", mod.PAGE)
        self.assertIn("if(d.contains(e.target))return;closeDetail()", mod.PAGE)


if __name__ == "__main__":
    unittest.main()
