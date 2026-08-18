import importlib.util
import io
import os
import tempfile
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(__file__)
SPEC = importlib.util.spec_from_file_location("search_engine_catalog", os.path.join(HERE, "search_engine.py"))
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class CatalogSearchTests(unittest.TestCase):
    def test_thingiverse_search_parses_public_model_link(self):
        html = '<a href="/thing:7379392"><img src="/img/cube.webp" alt="Calibration cube">Calibration cube</a>'
        with mock.patch.object(mod, "_fetch_html", return_value=(html, "https://www.thingiverse.com/search?q=cube")):
            rows = mod.ThingiverseSearcher.search("cube", None)
        self.assertEqual(rows[0]["platform"], "Thingiverse")
        self.assertEqual(rows[0]["name"], "Calibration cube")
        self.assertFalse(rows[0]["requires_auth"])
        self.assertIn("thing:7379392", rows[0]["url"])

    def test_cults_search_marks_download_as_authenticated(self):
        html = '<a href="/en/3d-model/tool/3dbenchy-the-jolly-3d-printing-torture-test">3DBenchy</a>'
        with mock.patch.object(mod, "_fetch_html", return_value=(html, "https://cults3d.com/en/tags/benchy")):
            rows = mod.Cults3DSearcher.search("benchy", None)
        self.assertEqual(rows[0]["platform"], "Cults3D")
        self.assertTrue(rows[0]["requires_auth"])

    def test_myminifactory_search_parses_object(self):
        html = '<a href="/object/3d-print-yak-mount-127830">Yak Mount</a>'
        with mock.patch.object(mod, "_fetch_html", return_value=(html, "https://www.myminifactory.com/search/?query=yak")):
            rows = mod.MyMiniFactorySearcher.search("yak", None)
        self.assertEqual(rows[0]["name"], "Yak Mount")
        self.assertEqual(rows[0]["platform"], "MyMiniFactory")

    def test_thangs_search_parses_model(self):
        html = '<a href="/designer/LM3D/3d-model/Free%20Dragon-12345">Free Dragon</a>'
        with mock.patch.object(mod, "_fetch_html", return_value=(html, "https://thangs.com/search/dragon?scope=thangs")):
            rows = mod.ThangsSearcher.search("dragon", None)
        self.assertEqual(rows[0]["platform"], "Thangs")
        self.assertIn("/designer/LM3D/3d-model/", rows[0]["url"])

    def test_creality_search_parses_model(self):
        html = '<a href="/model-detail/output">Output</a>'
        with mock.patch.object(mod, "_fetch_html", return_value=(html, "https://www.crealitycloud.com/search/model?q=output")):
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
            with mock.patch.object(mod, "_fetch_html", return_value=(html, "https://grabcad.com/library?query=bracket")):
                rows = mod.GrabcadSearcher.search("bracket", auth)
        self.assertTrue(rows[0]["requires_auth"])
        self.assertEqual(rows[0]["platform"], "GrabCAD")


class DownloadResolverTests(unittest.TestCase):
    def test_download_candidate_extractor_accepts_direct_and_download_action(self):
        html = '''
          <a href="https://cdn.example.test/model.stl">STL</a>
          <a href="/download/123">Download 3MF</a>
          <a href="/image/model.png">Download preview</a>
        '''
        values = mod._extract_download_candidates(html, "https://site.example.test/model")
        urls = [x[0] for x in values]
        self.assertIn("https://cdn.example.test/model.stl", urls)
        self.assertIn("https://site.example.test/download/123", urls)
        self.assertNotIn("https://site.example.test/image/model.png", urls)

    def test_public_resolver_returns_validated_file(self):
        model = {"url": "https://example.test/model/1"}
        html = '<a href="https://cdn.example.test/part.3mf">Download</a>'
        with mock.patch.object(mod, "_fetch_html", return_value=(html, model["url"])), \
             mock.patch.object(mod, "_probe_download", return_value={"url": "https://cdn.example.test/part.3mf", "name": "part.3mf"}):
            files = mod._public_page_files(model)
        self.assertEqual(files, [{"url": "https://cdn.example.test/part.3mf", "name": "part.3mf"}])

    def test_myminifactory_paid_object_goes_to_browser(self):
        model = {"url": "https://www.myminifactory.com/object/3d-print-paid-123"}
        html = '<h1>$12 Paid</h1><button>Add Files To Cart $12</button>'
        with mock.patch.object(mod, "_fetch_html", return_value=(html, model["url"])):
            with self.assertRaises(mod.BrowserRequired):
                mod.MyMiniFactorySearcher.get_files(model)

    def test_thangs_member_model_goes_to_browser(self):
        model = {"url": "https://thangs.com/designer/A/3d-model/Paid-1"}
        html = '<h1>Paid</h1><div>Become a member to download</div>'
        with mock.patch.object(mod, "_fetch_html", return_value=(html, model["url"])):
            with self.assertRaises(mod.BrowserRequired):
                mod.ThangsSearcher.get_files(model)

    def test_thingiverse_prefers_public_zip(self):
        model = {"url": "https://www.thingiverse.com/thing:7379392"}
        with mock.patch.object(mod, "_probe_download", return_value={"url": model["url"] + "/zip", "name": "download"}):
            files = mod.ThingiverseSearcher.get_files(model)
        self.assertEqual(files[0]["name"], "thingiverse_7379392.zip")

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
            own = auth._request_headers("cults3d", "https://cults3d.com/en/3d-model/tool/a")
            cdn = auth._request_headers("cults3d", "https://cdn.example.test/a.stl")
            session = auth.session("cults3d")
        self.assertNotIn("Cookie", own)
        self.assertNotIn("Cookie", cdn)
        self.assertEqual(session.cookies.get("_session", domain=".cults3d.com", path="/"), "secret")

    def test_grabcad_cookie_is_host_scoped(self):
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            auth.save_token("grabcad", "Cookie: sid=secret")
            own = auth._request_headers("grabcad", "https://grabcad.com/library/a")
            cdn = auth._request_headers("grabcad", "https://cdn.example.test/a.step")
            session = auth.session("grabcad")
        self.assertNotIn("Cookie", own)
        self.assertNotIn("Cookie", cdn)
        self.assertEqual(session.cookies.get("sid", domain=".grabcad.com", path="/"), "secret")


    def test_cults_stale_session_is_reported_as_auth_error(self):
        model = {"url": "https://cults3d.com/en/3d-model/tool/free-model"}
        html = '<form action="/en/users/sign_in"><a href="/users/sign_in">Sign in</a><span>Forgot your password?</span></form>'
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            auth.save_token("cults3d", "_session=expired")
            with mock.patch.object(mod, "_fetch_html", return_value=(html, model["url"])):
                with self.assertRaises(mod.AuthRequired):
                    mod.Cults3DSearcher.get_files(model, auth)

    def test_grabcad_stale_session_is_reported_as_auth_error(self):
        model = {"url": "https://grabcad.com/library/example-1"}
        html = '<h2>Sign In or Create Account</h2><div>Sign in with email</div>'
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            auth.save_token("grabcad", "sid=expired")
            with mock.patch.object(mod, "_fetch_html", return_value=(html, model["url"])):
                with self.assertRaises(mod.AuthRequired):
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
        requested = {"thingiverse", "cults3d", "myminifactory", "thangs", "makeronline", "crealitycloud", "nexprint", "grabcad"}
        self.assertTrue(requested.issubset(mod._SEARCHERS))
        display = {"Thingiverse", "Cults3D", "MyMiniFactory", "Thangs", "Makeronline", "Creality Cloud", "Nexprint", "GrabCAD"}
        self.assertTrue(display.issubset(mod._FILE_RESOLVERS))

    def test_public_sites_do_not_have_auth_controls(self):
        for platform in ("thingiverse", "myminifactory", "thangs", "crealitycloud"):
            self.assertNotIn(f'id="auth-{platform}"', mod.PAGE)
        self.assertIn('id="auth-cults3d"', mod.PAGE)
        self.assertIn('id="auth-grabcad"', mod.PAGE)

    def test_version_is_030(self):
        with open(os.path.join(HERE, "search_engine.py"), encoding="utf-8") as fh:
            head = fh.read(500)
        self.assertIn('# version = "0.3.0"', head)


if __name__ == "__main__":
    unittest.main()
