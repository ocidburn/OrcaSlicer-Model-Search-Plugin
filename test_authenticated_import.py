import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(__file__)
SPEC = importlib.util.spec_from_file_location(
    "search_engine", os.path.join(HERE, "search_engine.py")
)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class FakeResponse:
    def __init__(
        self,
        status=200,
        data=None,
        headers=None,
        url="https://api.example.test/resource",
    ):
        self.status_code = status
        self._data = data if data is not None else {}
        self.headers = headers or {}
        self.ok = 200 <= status < 400
        self.url = url
        self.closed = False

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True


class FakeAuth:
    def __init__(self, platform, responses):
        self.platform = platform
        self.responses = list(responses)
        self.calls = []

    def authenticated(self, platform):
        return platform == self.platform

    def session(self, platform):
        return object()

    def request(self, platform, method, url, session=None, **kwargs):
        self.calls.append((platform, method, url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        return self.responses.pop(0)


class AuthStoreTests(unittest.TestCase):
    def test_password_is_never_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "sessions.json")
            store = mod.AuthStore(path)
            store.set(
                "makerworld",
                {
                    "access_token": "abc123token",
                    "password": "DO-NOT-SAVE",
                    "Password": "DO-NOT-SAVE-CASE-INSENSITIVE",
                    "secret": "DO-NOT-SAVE-EITHER",
                    "label": "user@example.com",
                },
            )
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read()
            self.assertNotIn("DO-NOT-SAVE", raw)
            data = json.loads(raw)
            self.assertEqual(data["makerworld"]["access_token"], "abc123token")

    def test_platform_tokens_are_isolated(self):
        with tempfile.TemporaryDirectory() as td:
            store = mod.AuthStore(os.path.join(td, "sessions.json"))
            auth = mod.AuthManager(store)
            auth.save_token("makerworld", "mw-token", label="bambu")
            auth.save_token("nexprint", "nx-token", label="elegoo")
            self.assertEqual(auth.token("makerworld"), "mw-token")
            self.assertEqual(auth.token("nexprint"), "nx-token")
            auth.logout("makerworld")
            self.assertFalse(auth.authenticated("makerworld"))
            self.assertTrue(auth.authenticated("nexprint"))

    def test_token_normalization_accepts_copied_headers(self):
        self.assertEqual(
            mod.AuthManager.normalize_token("makerworld", "Bearer abc"), "abc"
        )
        self.assertEqual(
            mod.AuthManager.normalize_token(
                "nexprint", "foo=1; auth_token=NX123; bar=2"
            ),
            "NX123",
        )
        self.assertEqual(
            mod.AuthManager.normalize_token("makeronline", "XX-Token: AC123"), "AC123"
        )

    def test_auth_headers_are_not_sent_to_external_cdn(self):
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            auth.save_token("makerworld", "TOP-SECRET")
            api_headers = auth._request_headers(
                "makerworld", "https://api.bambulab.com/v1/foo"
            )
            cdn_headers = auth._request_headers(
                "makerworld", "https://s3.us-west-2.amazonaws.com/bucket/file"
            )
            self.assertEqual(api_headers["Authorization"], "Bearer TOP-SECRET")
            self.assertNotIn("Authorization", cdn_headers)

    def test_auth_headers_are_rebuilt_after_cross_host_redirect(self):
        class RedirectSession:
            def __init__(self):
                self.calls = []
                self.responses = [
                    FakeResponse(
                        status=302,
                        headers={"location": "https://cdn.example.test/model.3mf"},
                        url="https://www.makeronline.com/api/download/1",
                    ),
                    FakeResponse(url="https://cdn.example.test/model.3mf"),
                ]

            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                return self.responses.pop(0)

        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            auth.save_token("makeronline", "TOP-SECRET")
            session = RedirectSession()
            with mock.patch.object(mod, "_reject_obvious_local_target"):
                response = auth.request(
                    "makeronline",
                    "GET",
                    "https://www.makeronline.com/api/download/1",
                    session=session,
                )
        first_headers = session.calls[0][2]["headers"]
        redirected_headers = session.calls[1][2]["headers"]
        self.assertEqual(response.url, "https://cdn.example.test/model.3mf")
        self.assertEqual(first_headers["XX-Token"], "TOP-SECRET")
        self.assertEqual(first_headers["Authorization"], "Bearer TOP-SECRET")
        self.assertNotIn("XX-Token", redirected_headers)
        self.assertNotIn("Authorization", redirected_headers)

    def test_dns_resolved_private_download_target_is_rejected(self):
        private_answer = [(2, 1, 6, "", ("127.0.0.1", 0))]
        with (
            mock.patch.object(mod.socket, "getaddrinfo", return_value=private_answer),
            self.assertRaisesRegex(ValueError, "private/local"),
        ):
            mod._reject_obvious_local_target("https://attacker.example/model.stl")


class ResolverTests(unittest.TestCase):
    def test_makerworld_uses_internal_model_id_and_profile_download_endpoint(self):
        design = {
            "modelId": "US2bb73b106683e5",
            "title": "Compound Bow",
            "instances": [{"profileId": 3601086, "title": "0.2mm profile"}],
        }
        signed = {
            "url": "https://makerworld.bblmw.com/path/model.3mf?at=1&exp=2&key=x",
            "name": "compound-bow.3mf",
        }
        auth = FakeAuth(
            "makerworld", [FakeResponse(data=design), FakeResponse(data=signed)]
        )
        model = {
            "platform": "MakerWorld",
            "_model_id": 3183685,
            "url": "https://makerworld.com/en/models/3183685-compound-bow#profileId-3601086",
        }
        files = mod.MakerWorldSearcher.get_files(model, auth)
        self.assertEqual(files[0]["name"], "compound-bow.3mf")
        self.assertEqual(files[0]["url"], signed["url"])
        self.assertIn("/design/3183685", auth.calls[0][2])
        self.assertIn("/user/profile/3601086", auth.calls[1][2])
        self.assertEqual(auth.calls[1][3]["params"]["model_id"], "US2bb73b106683e5")

    def test_makerworld_selects_first_instance_when_url_has_no_profile(self):
        design = {"modelId": "MID", "title": "Thing", "instances": []}
        instances = {"hits": [{"profileId": 123, "title": "Default"}]}
        signed = {"url": "https://makerworld.bblmw.com/a.3mf?x=1"}
        auth = FakeAuth(
            "makerworld",
            [
                FakeResponse(data=design),
                FakeResponse(data=instances),
                FakeResponse(data=signed),
            ],
        )
        model = {
            "platform": "MakerWorld",
            "_model_id": 10,
            "url": "https://makerworld.com/en/models/10",
        }
        files = mod.MakerWorldSearcher.get_files(model, auth)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0]["name"].endswith(".3mf"))
        self.assertIn("/design/10/instances", auth.calls[1][2])
        self.assertIn("/user/profile/123", auth.calls[2][2])

    def test_nexprint_lists_all_model_files(self):
        payload = {
            "code": 0,
            "data": {
                "modelFileInfoList": [
                    {"fileName": "a.stl", "fileUrl": "https://cdn.nexprint.com/a.stl"},
                    {"fileName": "b.3mf", "fileUrl": "https://cdn.nexprint.com/b.3mf"},
                ]
            },
        }
        auth = FakeAuth("nexprint", [FakeResponse(data=payload)])
        files = mod.NexprintSearcher.get_files(
            {"_model_id": 42, "url": "https://www.nexprint.com/models/42"}, auth
        )
        self.assertEqual([x["name"] for x in files], ["a.stl", "b.3mf"])
        self.assertIn("model-base-info/get", auth.calls[0][2])

    def test_makeronline_lists_all_model_files(self):
        payload = {
            "code": 0,
            "data": {
                "files": [
                    {
                        "file_name": "part.step",
                        "url": "https://cdn.makeronline.com/part.step",
                    },
                    {
                        "file_name": "part.stl",
                        "url": "https://cdn.makeronline.com/part.stl",
                    },
                ]
            },
        }
        auth = FakeAuth("makeronline", [FakeResponse(data=payload)])
        files = mod.MakeronlineSearcher.get_files({"_mold_id": 55}, auth)
        self.assertEqual(len(files), 2)
        self.assertEqual(files[0]["name"], "part.step")

    def test_resolvers_require_authentication(self):
        class NoneAuth:
            def authenticated(self, platform):
                return False

        for resolver, model in [
            (mod.MakerWorldSearcher.get_files, {"_model_id": 1}),
            (mod.NexprintSearcher.get_files, {"_model_id": 1}),
            (mod.MakeronlineSearcher.get_files, {"_mold_id": 1}),
        ]:
            with self.assertRaises(mod.AuthRequired):
                resolver(model, NoneAuth())


class AnycubicImportTests(unittest.TestCase):
    def test_import_token_from_slicer_config(self):
        with tempfile.TemporaryDirectory() as td:
            config = os.path.join(td, "AnycubicSlicerNext.conf")
            with open(config, "w", encoding="utf-8") as fh:
                json.dump(
                    {"anycubic_cloud": {"access_token": "1234567890abcdefTOKEN"}}, fh
                )
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            result = auth.import_anycubic_slicer_token([config])
            self.assertEqual(auth.token("makeronline"), "1234567890abcdefTOKEN")
            self.assertEqual(result["source"], config)


if __name__ == "__main__":
    unittest.main()
