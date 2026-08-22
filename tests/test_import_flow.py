import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests._module_loader import load_plugin


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
        module = load_plugin("search_engine_import_flow")
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
            {
                "name": "part-a.stl",
                "url": "https://cdn.example/a.stl",
                "preview_url": "https://media.printables.com/a.png",
                "size": 1024,
            },
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
        self.assertEqual(
            msg["files"][0]["preview_url"], "https://media.printables.com/a.png"
        )
        self.assertEqual(msg["files"][0]["size"], 1024)
        self.assertEqual(msg["files"][1]["preview_url"], "")

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
        self.assertEqual(
            download.call_args.args[1],
            [
                {
                    "name": "only.stl",
                    "url": "https://cdn.example/only.stl",
                    "preview_url": "",
                    "size": None,
                    "selected": True,
                }
            ],
        )

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

    def test_empty_file_selection_keeps_pending_import(self):
        _mod, action = self.make_action()
        model = {"platform": "Printables", "name": "Demo"}
        files = [{"name": "a.stl", "url": "https://cdn.example/a.stl"}]
        action._pending_import_model = model
        action._pending_import_files = files

        with mock.patch.object(action, "_download_and_import") as download:
            action._import_selected([])

        download.assert_not_called()
        self.assertIs(action._pending_import_model, model)
        self.assertEqual(action._pending_import_files, files)
        self.assertEqual(action.win.posts[-1]["message"], "Select at least one file to import.")

    def test_ui_contains_multifile_checkbox_picker(self):
        mod = load_module()
        self.assertIn('id="file-modal"', mod.PAGE)
        self.assertIn("file-choice", mod.PAGE)
        self.assertIn("import_selected", mod.PAGE)
        self.assertIn("Select all", mod.PAGE)
        self.assertIn("Select none", mod.PAGE)
        self.assertIn('class="file-preview"', mod.PAGE)
        self.assertIn("f.preview_url", mod.PAGE)
        self.assertIn('loading="lazy"', mod.PAGE)
        self.assertIn('onmouseenter="showImagePreview(this)"', mod.PAGE)
        self.assertIn('onfocus="showImagePreview(this)"', mod.PAGE)

    def test_makerworld_import_opens_profile_and_format_picker(self):
        mod, action = self.make_action()
        model = {
            "platform": "MakerWorld",
            "_model_id": 10,
            "url": "https://makerworld.com/en/models/10",
            "requires_auth": True,
        }
        choices = {
            "profiles": [{"profile_id": "123", "title": "Fast"}],
            "default_profile_id": "123",
            "formats": [{"id": "3mf", "label": "3MF", "available": True}],
        }
        with (
            mock.patch.object(action.auth, "authenticated", return_value=True),
            mock.patch.object(
                mod.MakerWorldSearcher,
                "get_download_choices",
                return_value=choices,
            ),
            mock.patch.object(action, "_download_and_import") as download,
        ):
            action._resolve_import(model)
        download.assert_not_called()
        self.assertEqual(action.win.posts[-1]["action"], "profile_choices")
        self.assertEqual(action.win.posts[-1]["profiles"][0]["profile_id"], "123")

    def test_makerworld_card_selection_can_prefetch_profiles_without_login(self):
        mod, action = self.make_action()
        model = {
            "platform": "MakerWorld",
            "_platform_key": "makerworld",
            "_model_id": 10,
            "url": "https://makerworld.com/en/models/10",
            "requires_auth": True,
        }
        choices = {
            "profiles": [
                {
                    "profile_id": "123",
                    "title": "Fast",
                    "cover": "https://cdn.example/fast.webp",
                }
            ],
            "default_profile_id": "123",
            "formats": [{"id": "3mf", "label": "3MF", "available": True}],
        }
        with (
            mock.patch.object(action.auth, "authenticated", return_value=False),
            mock.patch.object(
                mod.MakerWorldSearcher,
                "get_download_choices",
                return_value=choices,
            ) as get_choices,
        ):
            action._prefetch_profile_choices(model)
        get_choices.assert_called_once_with(model, action.auth)
        message = action.win.posts[-1]
        self.assertEqual(message["action"], "profile_prefetched")
        self.assertEqual(message["model"], model)
        self.assertEqual(message["profiles"][0]["profile_id"], "123")

    def test_nexprint_import_opens_profile_picker(self):
        mod, action = self.make_action()
        model = {
            "platform": "Nexprint",
            "_platform_key": "nexprint",
            "_model_id": "G0149050",
            "url": "https://www.nexprint.com/en/models/G0149050",
            "requires_auth": True,
        }
        choices = {
            "picker_platform": "Nexprint",
            "profiles": [
                {
                    "profile_id": "1957793219610607616",
                    "title": "0.2mm profile",
                    "cover": "https://np.nexprint.com/profile.png",
                }
            ],
            "default_profile_id": "1957793219610607616",
            "formats": [{"id": "3mf", "label": "3MF", "available": True}],
        }
        with (
            mock.patch.object(action.auth, "authenticated", return_value=True),
            mock.patch.object(
                mod.NexprintSearcher,
                "get_download_choices",
                return_value=choices,
            ),
            mock.patch.object(action, "_download_and_import") as download,
        ):
            action._resolve_import(model)
        download.assert_not_called()
        message = action.win.posts[-1]
        self.assertEqual(message["action"], "profile_choices")
        self.assertEqual(message["picker_platform"], "Nexprint")
        self.assertEqual(
            message["profiles"][0]["cover"],
            "https://np.nexprint.com/profile.png",
        )

    def test_nexprint_profile_choice_is_preserved_for_download(self):
        _mod, action = self.make_action()
        model = {
            "platform": "Nexprint",
            "_platform_key": "nexprint",
            "url": "https://www.nexprint.com/en/models/G0149050",
        }
        with mock.patch.object(action, "_resolve_import") as resolve:
            action._resolve_profile_choice(
                model,
                {"profile_id": "1957793219610607616", "format": "3mf"},
            )
        selected = resolve.call_args.args[0]
        self.assertEqual(selected["_profile_id"], "1957793219610607616")
        self.assertEqual(selected["_download_format"], "3mf")

    def test_nexprint_card_prefetches_profiles_before_login(self):
        mod, action = self.make_action()
        model = {
            "platform": "Nexprint",
            "_platform_key": "nexprint",
            "_model_id": "G0149050",
        }
        choices = {
            "profiles": [
                {
                    "profile_id": "1957793219610607616",
                    "cover": "https://np.nexprint.com/profile.png",
                }
            ],
            "formats": [{"id": "3mf", "available": True}],
        }
        with mock.patch.object(
            mod.NexprintSearcher, "get_download_choices", return_value=choices
        ) as get_choices:
            action._prefetch_profile_choices(model)
        get_choices.assert_called_once_with(model, action.auth)
        message = action.win.posts[-1]
        self.assertEqual(message["action"], "profile_prefetched")
        self.assertEqual(message["picker_platform"], "Nexprint")
        self.assertEqual(
            message["profiles"][0]["cover"],
            "https://np.nexprint.com/profile.png",
        )

    def test_makerworld_3mf_choice_preserves_explicit_profile(self):
        _mod, action = self.make_action()
        model = {
            "platform": "MakerWorld",
            "url": "https://makerworld.com/en/models/10",
        }
        with mock.patch.object(action, "_resolve_import") as resolve:
            action._resolve_makerworld_choice(
                model, {"profile_id": "456", "format": "3mf"}
            )
        selected = resolve.call_args.args[0]
        self.assertEqual(selected["_profile_id"], "456")
        self.assertEqual(selected["_download_format"], "3mf")

    def test_makerworld_raw_files_use_official_browser_flow(self):
        _mod, action = self.make_action()
        action._resolve_makerworld_choice(
            {
                "platform": "MakerWorld",
                "url": "https://makerworld.com/en/models/10#profileId-111",
            },
            {"profile_id": "456", "format": "raw_browser"},
        )
        message = action.win.posts[-1]
        self.assertEqual(message["action"], "browser_required")
        self.assertEqual(
            message["url"], "https://makerworld.com/en/models/10#profileId-456"
        )

    def test_ui_contains_makerworld_profile_and_format_picker(self):
        mod = load_module()
        self.assertIn('id="makerworld-modal"', mod.PAGE)
        self.assertIn('id="mw-profiles"', mod.PAGE)
        self.assertIn('id="mw-formats"', mod.PAGE)
        self.assertIn("resolve_profile_choice", mod.PAGE)
        self.assertIn("STL/CAD files", mod.PAGE)
        self.assertIn("mw-summary", mod.PAGE)

    def test_ui_lazily_loads_search_result_images(self):
        mod = load_module()
        self.assertIn('class="result-image"', mod.PAGE)
        self.assertIn('loading="lazy"', mod.PAGE)
        self.assertIn('data-src="', mod.PAGE)
        self.assertIn("IntersectionObserver", mod.PAGE)
        self.assertIn("rootMargin:'240px 0px'", mod.PAGE)

    def test_ui_previews_every_search_result_image(self):
        mod = load_module()
        self.assertIn('class="result-image"', mod.PAGE)
        self.assertIn('tabindex="0" onmouseenter="showImagePreview(this)"', mod.PAGE)
        self.assertIn('onmouseleave="hideImagePreview(this)"', mod.PAGE)
        self.assertIn('onfocus="showImagePreview(this)"', mod.PAGE)
        self.assertIn('onblur="hideImagePreview(this)"', mod.PAGE)
        self.assertIn("source.dataset&&source.dataset.src", mod.PAGE)
        self.assertIn("loadResultImage(source)", mod.PAGE)

    def test_ui_prefetches_makerworld_profiles_and_images(self):
        mod = load_module()
        self.assertIn("prefetch_profile_choices", mod.PAGE)
        self.assertIn("profile_prefetched", mod.PAGE)
        self.assertIn("makerWorldChoicesCache", mod.PAGE)
        self.assertIn("makerWorldPreloadedImages", mod.PAGE)
        self.assertIn("new Image()", mod.PAGE)

    def test_creality_import_opens_profile_and_format_picker(self):
        mod, action = self.make_action()
        model = {
            "platform": "Creality Cloud",
            "_platform_key": "crealitycloud",
            "_model_id": "68df5251aaaa058eab3729a1",
            "url": "https://www.crealitycloud.com/model-detail/dragon-cup-3d-print",
        }
        choices = {
            "picker_platform": "Creality Cloud",
            "profiles": [
                {"profile_id": "690dd45f2904ad6ab51c9f2c", "title": "0.16mm"}
            ],
            "default_profile_id": "690dd45f2904ad6ab51c9f2c",
            "formats": [
                {"id": "3mf", "label": "3MF", "available": True},
                {"id": "raw_browser", "label": "STL/CAD", "available": True},
            ],
        }
        with mock.patch.object(
            mod.CrealityCloudSearcher,
            "get_download_choices",
            return_value=choices,
        ):
            action._resolve_import(model)
        message = action.win.posts[-1]
        self.assertEqual(message["action"], "profile_choices")
        self.assertEqual(message["picker_platform"], "Creality Cloud")
        self.assertEqual(
            message["profiles"][0]["profile_id"], "690dd45f2904ad6ab51c9f2c"
        )

    def test_creality_3mf_choice_preserves_selected_profile(self):
        _mod, action = self.make_action()
        model = {
            "platform": "Creality Cloud",
            "_platform_key": "crealitycloud",
            "url": "https://www.crealitycloud.com/model-detail/dragon-cup-3d-print",
        }
        with mock.patch.object(action, "_resolve_import") as resolve:
            action._resolve_profile_choice(
                model,
                {"profile_id": "690dd45f2904ad6ab51c9f2c", "format": "3mf"},
            )
        selected = resolve.call_args.args[0]
        self.assertEqual(selected["_profile_id"], "690dd45f2904ad6ab51c9f2c")
        self.assertEqual(selected["_download_format"], "3mf")

    def test_creality_raw_files_use_official_browser_flow(self):
        _mod, action = self.make_action()
        action._resolve_profile_choice(
            {
                "platform": "Creality Cloud",
                "_platform_key": "crealitycloud",
                "url": "https://www.crealitycloud.com/model-detail/dragon-cup-3d-print",
            },
            {
                "profile_id": "690dd45f2904ad6ab51c9f2c",
                "format": "raw_browser",
            },
        )
        message = action.win.posts[-1]
        self.assertEqual(message["action"], "browser_required")
        self.assertIn("profileId=690dd45f2904ad6ab51c9f2c", message["url"])

    def test_ui_has_enlarged_makerworld_profile_preview(self):
        mod = load_module()
        self.assertIn('id="image-preview"', mod.PAGE)
        self.assertIn("showImagePreview", mod.PAGE)
        self.assertIn("cursor:zoom-in", mod.PAGE)
        self.assertIn('onmouseenter="showImagePreview(this)"', mod.PAGE)
        self.assertIn('onfocus="showImagePreview(this)"', mod.PAGE)

    def test_detail_import_panel_closes_on_click_outside(self):
        mod = load_module()
        self.assertIn("document.addEventListener('pointerdown'", mod.PAGE)
        self.assertIn("if(d.contains(e.target))return;closeDetail()", mod.PAGE)


if __name__ == "__main__":
    unittest.main()
