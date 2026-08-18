import os
import tempfile
import unittest
import zipfile
from unittest import mock

from tests._module_loader import PLUGIN_PATH, load_plugin

mod = load_plugin("search_engine_catalog")


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class CatalogSearchTests(unittest.TestCase):
    def test_thingiverse_search_uses_official_api(self):
        payload = [
            {
                "id": 7379392,
                "name": "Calibration cube",
                "public_url": "https://www.thingiverse.com/thing:7379392",
                "creator": {"name": "Tester"},
                "download_count": 42,
            }
        ]
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            auth.save_token("thingiverse", "token")
            with mock.patch.object(auth, "request", return_value=FakeResponse(payload)):
                rows = mod.ThingiverseSearcher.search("cube", auth)
        self.assertEqual(rows[0]["platform"], "Thingiverse")
        self.assertEqual(rows[0]["name"], "Calibration cube")
        self.assertTrue(rows[0]["requires_auth"])
        self.assertEqual(rows[0]["downloads"], 42)
        self.assertIn("thing:7379392", rows[0]["url"])
        self.assertTrue(rows[0]["_details_available"])
        self.assertFalse(rows[0]["_details_loaded"])

    def test_thingiverse_details_supply_canonical_license_and_metrics(self):
        payload = {
            "id": 2716464,
            "name": "Cup / Mug hanger",
            "public_url": "https://www.thingiverse.com/thing:2716464",
            "creator": {"name": "workshopbob"},
            "license": "Creative Commons - Attribution",
            "download_count": 1234,
            "like_count": 6800,
            "view_count": 9000,
            "make_count": 17,
        }
        model = {
            "_thing_id": 2716464,
            "_platform_key": "thingiverse",
            "url": "https://www.thingiverse.com/thing:2716464",
        }
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            auth.save_token("thingiverse", "token")
            with mock.patch.object(auth, "request", return_value=FakeResponse(payload)):
                result = mod.ThingiverseSearcher.get_details(model, auth)
        self.assertEqual(result["license"], "CC BY")
        self.assertEqual(
            result["license_url"], "https://creativecommons.org/licenses/by/4.0/"
        )
        self.assertEqual(result["downloads"], 1234)
        self.assertEqual(result["views"], 9000)
        self.assertEqual(result["makes"], 17)
        self.assertTrue(result["_details_loaded"])
        self.assertEqual(result["_platform_key"], "thingiverse")

    def test_cults_search_marks_download_as_authenticated(self):
        html = '<a href="/en/3d-model/tool/3dbenchy-the-jolly-3d-printing-torture-test">3DBenchy</a>'
        with mock.patch.object(
            mod,
            "_fetch_html",
            return_value=(html, "https://cults3d.com/en/tags/benchy"),
        ):
            rows = mod.Cults3DSearcher.search("benchy", None)
        self.assertEqual(rows[0]["platform"], "Cults3D")
        self.assertTrue(rows[0]["requires_auth"])

    def test_myminifactory_search_uses_official_api(self):
        payload = {
            "items": [
                {
                    "id": 127830,
                    "name": "Yak Mount",
                    "url": "https://www.myminifactory.com/object/3d-print-yak-mount-127830",
                    "designer": {"username": "maker"},
                    "views": 50,
                    "likes": 4,
                }
            ]
        }
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            auth.save_token("myminifactory", "api-key")
            with mock.patch.object(auth, "request", return_value=FakeResponse(payload)):
                rows = mod.MyMiniFactorySearcher.search("yak", auth)
        self.assertEqual(rows[0]["name"], "Yak Mount")
        self.assertEqual(rows[0]["platform"], "MyMiniFactory")
        self.assertEqual(rows[0]["views"], 50)

    def test_thangs_search_uses_explicit_browser_fallback(self):
        rows = mod.ThangsSearcher.search("dragon", None)
        self.assertEqual(rows[0]["platform"], "Thangs")
        self.assertEqual(rows[0]["result_type"], "search_link")
        self.assertFalse(rows[0]["direct_import"])
        self.assertIn("/search/dragon", rows[0]["url"])

    def test_creality_search_parses_model(self):
        html = '<a href="/model-detail/output">Output</a>'
        with mock.patch.object(
            mod,
            "_fetch_html",
            return_value=(html, "https://www.crealitycloud.com/search/model?q=output"),
        ):
            rows = mod.CrealityCloudSearcher.search("output", None)
        self.assertEqual(rows[0]["platform"], "Creality Cloud")
        self.assertEqual(rows[0]["name"], "Output")

    def test_grabcad_requires_session_for_search(self):
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            with self.assertRaises(mod.AuthRequired):
                mod.GrabcadSearcher.search("bracket", auth)

    def test_grabcad_search_with_session(self):
        html = '<a href="/library/test-bracket-1">Test bracket</a>'
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            auth.save_token("grabcad", "session_id=abc123", label="test")
            with mock.patch.object(
                mod,
                "_fetch_html",
                return_value=(html, "https://grabcad.com/library?query=bracket"),
            ):
                rows = mod.GrabcadSearcher.search("bracket", auth)
        self.assertTrue(rows[0]["requires_auth"])
        self.assertEqual(rows[0]["platform"], "GrabCAD")


class DownloadResolverTests(unittest.TestCase):
    def test_download_candidate_extractor_accepts_direct_and_download_action(self):
        html = """
          <a href="https://cdn.example.test/model.stl">STL</a>
          <a href="/download/123">Download 3MF</a>
          <a href="/image/model.png">Download preview</a>
        """
        values = mod._extract_download_candidates(
            html, "https://site.example.test/model"
        )
        urls = [x[0] for x in values]
        self.assertIn("https://cdn.example.test/model.stl", urls)
        self.assertIn("https://site.example.test/download/123", urls)
        self.assertNotIn("https://site.example.test/image/model.png", urls)

    def test_private_download_candidate_is_ignored(self):
        self.assertIsNone(mod._probe_download("http://127.0.0.1/model.stl"))

    def test_public_resolver_returns_validated_file(self):
        model = {"url": "https://example.test/model/1"}
        html = '<a href="https://cdn.example.test/part.3mf">Download</a>'
        with (
            mock.patch.object(mod, "_fetch_html", return_value=(html, model["url"])),
            mock.patch.object(
                mod,
                "_probe_download",
                return_value={
                    "url": "https://cdn.example.test/part.3mf",
                    "name": "part.3mf",
                },
            ),
        ):
            files = mod._public_page_files(model)
        self.assertEqual(
            files, [{"url": "https://cdn.example.test/part.3mf", "name": "part.3mf"}]
        )

    def test_myminifactory_api_key_without_oauth_archive_goes_to_browser(self):
        model = {"url": "https://www.myminifactory.com/object/3d-print-paid-123"}
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            auth.save_token("myminifactory", "api-key")
            with self.assertRaises(mod.BrowserRequired):
                mod.MyMiniFactorySearcher.get_files(model, auth)

    def test_thangs_member_model_goes_to_browser(self):
        model = {"url": "https://thangs.com/designer/A/3d-model/Paid-1"}
        html = "<h1>Paid</h1><div>Become a member to download</div>"
        with (
            mock.patch.object(mod, "_fetch_html", return_value=(html, model["url"])),
            self.assertRaises(mod.BrowserRequired),
        ):
            mod.ThangsSearcher.get_files(model)

    def test_thingiverse_lists_files_through_official_api(self):
        model = {"url": "https://www.thingiverse.com/thing:7379392"}
        payload = [
            {"name": "cube.stl", "download_url": "https://cdn.example/cube.stl"}
        ]
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            auth.save_token("thingiverse", "token")
            with mock.patch.object(auth, "request", return_value=FakeResponse(payload)):
                files = mod.ThingiverseSearcher.get_files(model, auth)
        self.assertEqual(files[0]["name"], "cube.stl")

    def test_cults_requires_account_before_files(self):
        model = {"url": "https://cults3d.com/en/3d-model/tool/free-model"}
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            with self.assertRaises(mod.AuthRequired):
                mod.Cults3DSearcher.get_files(model, auth)

    def test_cults_cookie_is_host_scoped(self):
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            auth.save_token("cults3d", "_session=secret")
            own = auth._request_headers(
                "cults3d", "https://cults3d.com/en/3d-model/tool/a"
            )
            cdn = auth._request_headers("cults3d", "https://cdn.example.test/a.stl")
            session = auth.session("cults3d")
        self.assertNotIn("Cookie", own)
        self.assertNotIn("Cookie", cdn)
        self.assertEqual(
            session.cookies.get("_session", domain=".cults3d.com", path="/"), "secret"
        )

    def test_grabcad_cookie_is_host_scoped(self):
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            auth.save_token("grabcad", "Cookie: sid=secret")
            own = auth._request_headers("grabcad", "https://grabcad.com/library/a")
            cdn = auth._request_headers("grabcad", "https://cdn.example.test/a.step")
            session = auth.session("grabcad")
        self.assertNotIn("Cookie", own)
        self.assertNotIn("Cookie", cdn)
        self.assertEqual(
            session.cookies.get("sid", domain=".grabcad.com", path="/"), "secret"
        )

    def test_cults_stale_session_is_reported_as_auth_error(self):
        model = {"url": "https://cults3d.com/en/3d-model/tool/free-model"}
        html = '<form action="/en/users/sign_in"><a href="/users/sign_in">Sign in</a><span>Forgot your password?</span></form>'
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            auth.save_token("cults3d", "_session=expired")
            with (
                mock.patch.object(
                    mod, "_fetch_html", return_value=(html, model["url"])
                ),
                self.assertRaises(mod.AuthRequired),
            ):
                mod.Cults3DSearcher.get_files(model, auth)

    def test_grabcad_stale_session_is_reported_as_auth_error(self):
        model = {"url": "https://grabcad.com/library/example-1"}
        html = "<h2>Sign In or Create Account</h2><div>Sign in with email</div>"
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            auth.save_token("grabcad", "sid=expired")
            with (
                mock.patch.object(
                    mod, "_fetch_html", return_value=(html, model["url"])
                ),
                self.assertRaises(mod.AuthRequired),
            ):
                mod.GrabcadSearcher.get_files(model, auth)

    def test_zip_extraction_only_returns_model_files(self):
        with tempfile.TemporaryDirectory() as td:
            zpath = os.path.join(td, "thing.zip")
            with zipfile.ZipFile(zpath, "w") as z:
                z.writestr("files/part.stl", b"solid test\nendsolid test\n")
                z.writestr("../../evil.txt", b"no")
                z.writestr("images/preview.png", b"png")
            result = mod._expand_archives([zpath], td)
            self.assertEqual(len(result), 1)
            self.assertTrue(result[0].endswith("part.stl"))
            self.assertEqual(os.path.dirname(result[0]), td)


class RegistryAndUiTests(unittest.TestCase):
    def test_all_requested_catalogs_are_registered(self):
        requested = {
            "thingiverse",
            "cults3d",
            "myminifactory",
            "thangs",
            "makeronline",
            "crealitycloud",
            "nexprint",
            "grabcad",
            "smithsonian",
            "wikimedia",
            "nasa",
            "nih3d",
            "youmagine",
            "pinshape",
            "cgtrader",
        }
        self.assertTrue(requested.issubset(mod._PLATFORMS))
        display = {
            "Thingiverse",
            "Cults3D",
            "MyMiniFactory",
            "Thangs",
            "Makeronline",
            "Creality Cloud",
            "Nexprint",
            "GrabCAD",
            "Smithsonian 3D",
            "Wikimedia Commons",
            "NASA 3D Resources",
            "NIH 3D",
            "YouMagine",
            "Pinshape",
            "CGTrader",
        }
        self.assertTrue(display.issubset(mod._PLATFORMS_BY_DISPLAY))

    def test_platform_registry_is_consistent(self):
        self.assertEqual(len(mod._PLATFORM_SPECS), len(mod._PLATFORMS))
        self.assertEqual(len(mod._PLATFORM_SPECS), len(mod._PLATFORMS_BY_DISPLAY))
        for spec in mod._PLATFORM_SPECS:
            self.assertIs(mod._PLATFORMS[spec.key], spec)
            self.assertIs(mod._PLATFORMS_BY_DISPLAY[spec.display], spec)
            self.assertTrue(callable(spec.adapter.search))
            self.assertTrue(callable(spec.adapter.get_files))
            self.assertFalse(hasattr(spec.adapter, "enabled"))

    def test_only_authenticated_sites_have_auth_controls(self):
        for platform in (
            "thangs",
            "crealitycloud",
            "smithsonian",
            "wikimedia",
            "nasa",
            "nih3d",
            "youmagine",
            "pinshape",
            "cgtrader",
        ):
            self.assertNotIn(f'id="auth-{platform}"', mod.PAGE)
        for platform in (
            "cults3d",
            "grabcad",
            "thingiverse",
            "myminifactory",
        ):
            self.assertIn(f'id="auth-{platform}"', mod.PAGE)

    def test_every_available_searcher_has_portal_checkbox(self):
        import re

        portals = set(
            re.findall(r'class="portal-search"[^>]*data-platform="([^"]+)"', mod.PAGE)
        )
        self.assertEqual(portals, set(mod._PLATFORMS))

    def test_portal_selection_controls_are_present(self):
        self.assertIn('id="search-portals"', mod.PAGE)
        self.assertIn('id="source-count"', mod.PAGE)
        self.assertIn('onclick="setAllPortals(true)"', mod.PAGE)
        self.assertIn('onclick="setAllPortals(false)"', mod.PAGE)
        self.assertIn(
            "if(!ps.length){$('status').textContent='Select at least one search portal.';return}",
            mod.PAGE,
        )

    def test_ui_rejects_non_http_model_and_thumbnail_urls(self):
        self.assertIn("function safeUrl(s)", mod.PAGE)
        self.assertIn("safeUrl(m.thumbnail_url)", mod.PAGE)
        self.assertIn("var modelUrl=safeUrl(m.url)", mod.PAGE)

    def test_sort_and_filter_controls_are_present(self):
        self.assertIn('id="sort"', mod.PAGE)
        self.assertIn('value="downloads"', mod.PAGE)
        self.assertIn('value="rating"', mod.PAGE)
        self.assertIn('id="free-only"', mod.PAGE)
        self.assertIn('id="direct-only"', mod.PAGE)

    def test_common_sort_puts_missing_metrics_last(self):
        rows = mod._filter_and_sort_results(
            [
                {"name": "missing", "platform": "A"},
                {"name": "low", "platform": "A", "downloads": 2},
                {"name": "high", "platform": "B", "downloads": 20},
            ],
            {"sort": "downloads"},
        )
        self.assertEqual([row["name"] for row in rows], ["high", "low", "missing"])
        self.assertTrue(all(field in rows[0] for field in mod._COMMON_RESULT_FIELDS))

    def test_filters_do_not_guess_unknown_free_status(self):
        rows = mod._filter_and_sort_results(
            [
                {"name": "free", "platform": "A", "is_free": True},
                {"name": "unknown", "platform": "B", "is_free": None},
            ],
            {"free_only": True},
        )
        self.assertEqual([row["name"] for row in rows], ["free"])

    def test_newest_sort_normalizes_epoch_milliseconds_and_iso_dates(self):
        rows = mod._filter_and_sort_results(
            [
                {
                    "name": "older ISO",
                    "platform": "A",
                    "published_at": "2024-01-01T00:00:00Z",
                },
                {
                    "name": "newer epoch",
                    "platform": "B",
                    "published_at": 1735689600000,
                },
                {"name": "unknown", "platform": "C"},
            ],
            {"sort": "newest"},
        )
        self.assertEqual(
            [row["name"] for row in rows],
            ["newer epoch", "older ISO", "unknown"],
        )

    def test_unsupported_archive_types_are_not_advertised(self):
        self.assertNotIn(".rar", mod._MODEL_FILE_EXTS)
        self.assertNotIn(".7z", mod._MODEL_FILE_EXTS)
        self.assertNotIn(".gcode", mod._MODEL_FILE_EXTS)

    def test_version_is_051(self):
        with PLUGIN_PATH.open(encoding="utf-8") as fh:
            head = fh.read(500)
        self.assertIn('# version = "0.5.1"', head)


if __name__ == "__main__":
    unittest.main()
