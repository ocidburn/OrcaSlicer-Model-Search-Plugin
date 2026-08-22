"""The guards that stand between a remote response and the user's disk.

Every one of these could be deleted with the rest of the suite still green:
the tests that touch the download path either replace `_download_and_import`
with a Mock or drive it through fixtures that are all well-formed, so nothing
depended on a filename being stripped, an archive member staying inside its
directory, a size cap being enforced, or the SSRF guard running at all.
"""

import os
import sys
import tempfile
import types
import unittest
import zipfile
from unittest import mock

from tests._module_loader import load_plugin

mod = load_plugin("search_engine_download_pipeline")


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
        return load_plugin("search_engine_download_pipeline_script")
    finally:
        if previous is None:
            sys.modules.pop("orca", None)
        else:
            sys.modules["orca"] = previous


class SafeFilenameTests(unittest.TestCase):
    def test_a_path_never_survives_a_filename(self):
        for hostile in (
            "../../../etc/passwd",
            r"..\..\Windows\System32\drivers\etc\hosts",
            "/etc/passwd",
            r"C:\Windows\system.ini",
            "....//....//escape.stl",
        ):
            cleaned = mod._safe_filename(hostile)
            self.assertNotIn("/", cleaned, hostile)
            self.assertNotIn("\\", cleaned, hostile)
            self.assertFalse(cleaned.startswith("."), hostile)
            self.assertFalse(os.path.isabs(cleaned), hostile)

    def test_characters_a_filesystem_reads_specially_are_scrubbed(self):
        # basename alone leaves these: ":" opens an NTFS alternate data
        # stream, and the wildcards and control characters are either
        # illegal or surprising on one platform or another.
        cleaned = mod._safe_filename('part:evil.exe')
        self.assertNotIn(":", cleaned)
        for hostile, forbidden in (
            ("wild*card.stl", "*"),
            ("who?.stl", "?"),
            ('quote".stl', '"'),
            ("pipe|.stl", "|"),
            ("less<more>.stl", "<"),
            (chr(7) + "bell.stl", chr(7)),
        ):
            self.assertNotIn(forbidden, mod._safe_filename(hostile), hostile)

    def test_an_ordinary_name_survives_intact(self):
        self.assertEqual(
            mod._safe_filename("Bracket v2 (rev 3) [final].stl"),
            "Bracket v2 (rev 3) [final].stl",
        )

    def test_an_empty_or_hostile_name_falls_back(self):
        self.assertEqual(mod._safe_filename("", "fallback.3mf"), "fallback.3mf")
        self.assertEqual(mod._safe_filename("../", "fallback.3mf"), "fallback.3mf")

    def test_a_collision_does_not_overwrite_the_earlier_download(self):
        with tempfile.TemporaryDirectory() as td:
            first = mod._unique_path(td, "part.stl")
            with open(first, "wb") as handle:
                handle.write(b"first")
            second = mod._unique_path(td, "part.stl")
            self.assertNotEqual(first, second)
            with open(first, "rb") as handle:
                self.assertEqual(handle.read(), b"first")


class Response:
    def __init__(self, chunks, headers=None):
        self.headers = headers or {"content-type": "application/octet-stream"}
        self._chunks = chunks

    def iter_content(self, chunk_size=1):
        yield from self._chunks


class WriteResponseTests(unittest.TestCase):
    def _write(self, response):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "out.bin")
            mod._write_download_response(response, path)
            with open(path, "rb") as handle:
                return handle.read()

    def test_a_login_page_is_not_saved_as_a_model(self):
        with self.assertRaises(RuntimeError) as raised:
            self._write(Response([b"<html>sign in</html>"], {"content-type": "text/html"}))
        self.assertIn("HTML", str(raised.exception))

    def test_a_declared_size_over_the_cap_is_refused(self):
        with self.assertRaises(RuntimeError):
            self._write(
                Response(
                    [b"x"],
                    {
                        "content-type": "application/octet-stream",
                        "content-length": str(mod._MAX_DOWNLOAD_BYTES + 1),
                    },
                )
            )

    def test_a_lying_content_length_does_not_get_past_the_cap(self):
        # The header says one byte; the body keeps coming.
        oversize = mod._MAX_DOWNLOAD_BYTES // len(b"x" * 65536) + 2
        response = Response(
            (b"x" * 65536 for _ in range(oversize)),
            {"content-type": "application/octet-stream", "content-length": "1"},
        )
        with self.assertRaises(RuntimeError) as raised:
            self._write(response)
        self.assertIn("500 MB", str(raised.exception))

    def test_an_ordinary_body_is_written_verbatim(self):
        self.assertEqual(self._write(Response([b"solid tiny\n", b"endsolid\n"])), b"solid tiny\nendsolid\n")


class DownloadStreamTests(unittest.TestCase):
    def test_the_ssrf_guard_runs_before_anything_is_fetched(self):
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            with (
                mock.patch.object(auth, "request") as request,
                self.assertRaises(ValueError),
            ):
                mod._download_stream(
                    "http://127.0.0.1:8080/admin", "x.stl", td, auth, "printables"
                )
            request.assert_not_called()

    def test_a_non_http_url_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            with self.assertRaises(ValueError):
                mod._download_stream("file:///etc/passwd", "x.stl", td, auth, "printables")


def build_zip(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members:
            archive.writestr(name, payload)


class ArchiveExpansionTests(unittest.TestCase):
    def test_no_member_escapes_the_download_directory(self):
        with tempfile.TemporaryDirectory() as td:
            outside = os.path.join(td, "outside")
            os.makedirs(outside)
            dest = os.path.join(td, "dest")
            os.makedirs(dest)
            archive = os.path.join(dest, "payload.zip")
            build_zip(
                archive,
                [
                    ("../../escape.stl", "solid a\nendsolid a\n"),
                    (r"..\..\escape-win.stl", "solid b\nendsolid b\n"),
                    ("/absolute.stl", "solid c\nendsolid c\n"),
                    ("good.stl", "solid d\nendsolid d\n"),
                ],
            )

            extracted = mod._expand_archives([archive], dest)

            for path in extracted:
                self.assertTrue(
                    os.path.abspath(path).startswith(os.path.abspath(dest)),
                    f"{path} escaped {dest}",
                )
            self.assertEqual(os.listdir(outside), [])
            self.assertFalse(os.path.exists(os.path.join(td, "escape.stl")))

    def test_only_files_the_slicer_can_open_come_out(self):
        with tempfile.TemporaryDirectory() as td:
            archive = os.path.join(td, "payload.zip")
            build_zip(
                archive,
                [
                    ("model.stl", "solid a\nendsolid a\n"),
                    ("readme.txt", "hello"),
                    ("photo.png", "not really a png"),
                    ("nested.zip", "PK\x05\x06" + "\0" * 18),
                    ("run.exe", "MZ"),
                ],
            )
            names = [os.path.basename(path) for path in mod._expand_archives([archive], td)]
        self.assertEqual(names, ["model.stl"])

    def test_a_plain_model_file_passes_through_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "part.stl")
            with open(path, "wb") as handle:
                handle.write(b"solid a\nendsolid a\n")
            self.assertEqual(mod._expand_archives([path], td), [path])

    def test_a_zip_bomb_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            archive = os.path.join(td, "bomb.zip")
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
                handle.writestr("huge.stl", b"\0" * (mod._MAX_ARCHIVE_BYTES + 1))
            with self.assertRaises(RuntimeError):
                mod._expand_archives([archive], td)


class SelectedIndexTests(unittest.TestCase):
    def test_negative_and_duplicate_indices_are_dropped(self):
        self.assertEqual(mod._selected_file_indices([2, 0, 2, -1, 1]), [2, 0, 1])

    def test_a_malformed_index_list_does_not_reach_the_files(self):
        for hostile in ([-5], ["1"], [1.9]):
            # Either it is rejected outright or it is normalised to something
            # in range; what must not happen is a negative index selecting
            # from the end of the list.
            try:
                selected = mod._selected_file_indices(hostile)
            except (TypeError, ValueError):
                continue
            self.assertTrue(all(index >= 0 for index in selected), hostile)


class ResultIdentityTests(unittest.TestCase):
    def test_two_portals_may_share_an_id_without_colliding(self):
        first = {"_platform_key": "printables", "url": "https://x/1", "name": "Cube"}
        second = {"_platform_key": "thingiverse", "url": "https://x/1", "name": "Cube"}
        self.assertNotEqual(mod._result_identity(first), mod._result_identity(second))

    def test_merging_reports_how_many_rows_were_actually_new(self):
        existing = [
            {"_platform_key": "printables", "url": "https://x/1", "name": "A"},
        ]
        incoming = [
            {"_platform_key": "printables", "url": "https://x/1", "name": "A"},
            {"_platform_key": "printables", "url": "https://x/2", "name": "B"},
        ]
        merged, added = mod._merge_unique_results(existing, incoming)
        self.assertEqual(added, 1)
        self.assertEqual(len(merged), 2)

    def test_a_page_of_nothing_new_adds_nothing(self):
        rows = [{"_platform_key": "printables", "url": "https://x/1", "name": "A"}]
        merged, added = mod._merge_unique_results(rows, list(rows))
        self.assertEqual(added, 0)
        self.assertEqual(len(merged), 1)


class ExternalUrlTests(unittest.TestCase):
    def _action(self):
        script = load_script_module()
        action = script.SearchEngineScript()
        posts = []
        action._post = posts.append
        return script, action, posts

    def test_a_non_http_url_never_reaches_the_browser(self):
        script, action, posts = self._action()
        for hostile in (
            "file:///etc/passwd",
            "javascript:alert(1)",
            r"C:\Windows\system32\calc.exe",
            "",
        ):
            with mock.patch.object(script.webbrowser, "open") as opener:
                action._open_external(hostile)
            opener.assert_not_called()
            self.assertEqual(posts[-1]["action"], "error", hostile)

    def test_an_ordinary_model_page_is_opened(self):
        script, action, posts = self._action()
        with mock.patch.object(script.webbrowser, "open", return_value=True) as opener:
            action._open_external("https://www.printables.com/model/1")
        opener.assert_called_once()
        self.assertEqual(posts[-1]["action"], "opened")


class FilterTests(unittest.TestCase):
    @staticmethod
    def rows():
        return [
            {"name": "direct", "direct_import": True, "is_free": True},
            {"name": "browser", "direct_import": False, "is_free": True},
            {"name": "search link", "direct_import": False, "result_type": "search_link"},
        ]

    def test_direct_import_only_hides_the_browser_handoff_rows(self):
        kept = mod._filter_results(
            [mod._normalize_result(row) for row in self.rows()],
            {"direct_only": True},
        )
        self.assertEqual([row["name"] for row in kept], ["direct"])

    def test_without_the_filter_every_row_is_offered(self):
        kept = mod._filter_results(
            [mod._normalize_result(row) for row in self.rows()], {}
        )
        self.assertEqual(len(kept), 3)


class NewestSortTests(unittest.TestCase):
    def test_epoch_milliseconds_and_iso_dates_are_comparable(self):
        # The old fixture used an epoch-ms value ~1000x larger than the
        # seconds one, so it sorted first whether or not the conversion ran.
        # These two describe the same instant, and the ISO row is one day
        # later, so the order depends on the division actually happening.
        rows = [
            mod._normalize_result(
                {"name": "milliseconds", "published_at": 1735689600000}
            ),
            mod._normalize_result(
                {"name": "iso", "published_at": "2025-01-02T00:00:00Z"}
            ),
        ]
        order = [row["name"] for row in mod._sort_results(rows, "newest")]
        self.assertEqual(order, ["iso", "milliseconds"])

    def test_a_missing_date_sorts_last(self):
        rows = [
            mod._normalize_result({"name": "undated", "published_at": None}),
            mod._normalize_result({"name": "dated", "published_at": 1735689600000}),
        ]
        order = [row["name"] for row in mod._sort_results(rows, "newest")]
        self.assertEqual(order, ["dated", "undated"])


if __name__ == "__main__":
    unittest.main()
