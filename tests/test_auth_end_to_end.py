"""End-to-end scenarios for every portal that requires authorization.

Each scenario drives the real objects -- a real `AuthStore` on disk, a real
`AuthManager`, the real adapter, and the real coordinator -- and stubs only the
network transport. That keeps the parts worth testing in the loop: where a
credential is stored, which host it is presented to, the shape it is presented
in, what happens when the portal rejects it, and whether a resolved file
actually reaches the import hand-off.

The nine authorized portals are Nexprint, Makeronline, MakerWorld, Thingiverse,
Cults3D, MyMiniFactory, Thangs, Creality Cloud, and GrabCAD.
"""

import json
import os
import sys
import tempfile
import types
import unittest
from typing import ClassVar
from unittest import mock

import requests
from requests.cookies import get_cookie_header
from requests.models import PreparedRequest

from tests._module_loader import load_plugin

mod = load_plugin("search_engine_auth_e2e")

AUTHORIZED = (
    "nexprint",
    "makeronline",
    "makerworld",
    "thingiverse",
    "cults3d",
    "myminifactory",
    "thangs",
    "crealitycloud",
    "grabcad",
)

FOREIGN_HOST = "https://cdn.attacker.example/payload.stl"


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
        return load_plugin("search_engine_auth_e2e_script")
    finally:
        if previous is None:
            sys.modules.pop("orca", None)
        else:
            sys.modules["orca"] = previous


class Response:
    """Enough of a requests.Response for the paths under test."""

    def __init__(
        self,
        *,
        json_data=None,
        text="",
        status=200,
        headers=None,
        url=None,
        body=b"",
    ):
        self._json = json_data
        self.text = text
        self.status_code = status
        self.headers = headers or {}
        # A real transport reports the URL that was actually reached. Only
        # pin it here when a fixture is modelling a redirect on purpose.
        self.url = url or ""
        self.pinned_url = url is not None
        self.closed = False
        self._body = body

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_content(self, chunk_size=1):
        yield self._body

    def close(self):
        self.closed = True


MODEL_BODY = b"solid tiny\nendsolid tiny\n"
EMPTY_ZIP = b"PK\x05\x06" + b"\0" * 18


def model_file(name="model.stl", body=b"solid tiny\nendsolid tiny\n"):
    return Response(
        headers={
            "content-type": "application/octet-stream",
            "content-disposition": f'attachment; filename="{name}"',
            "content-length": str(len(body)),
        },
        body=body,
    )


class Call:
    __slots__ = ("cookies", "headers", "json", "kwargs", "method", "params", "url")

    def __init__(self, method, url, kwargs, cookies):
        self.method = method
        self.url = url
        self.kwargs = kwargs
        self.headers = {
            str(k).lower(): v for k, v in (kwargs.get("headers") or {}).items()
        }
        self.params = kwargs.get("params") or {}
        self.json = kwargs.get("json")
        self.cookies = cookies

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<Call {self.method} {self.url}>"


class FakeNetwork:
    """Route every HTTP call the plugin makes and record what it carried."""

    def __init__(self):
        self.routes = []
        self.calls = []

    def add(self, fragment, response, method=None):
        """Serve `response` for URLs containing `fragment`.

        A list of responses is consumed in order, which is how multi-step
        flows (detail then signed download) are expressed.
        """
        self.routes.append((fragment, method, response))
        return self

    def _match(self, method, url):
        for index, (fragment, want_method, response) in enumerate(self.routes):
            if fragment not in url:
                continue
            if want_method and want_method.upper() != method:
                continue
            if isinstance(response, list):
                if not response:
                    continue
                return response.pop(0)
            return response
        raise AssertionError(f"unrouted request: {method} {url}")

    @staticmethod
    def _cookie_header(jar, url):
        if jar is None:
            return None
        prepared = PreparedRequest()
        prepared.prepare_method("GET")
        prepared.prepare_url(url, None)
        prepared.prepare_headers({})
        return get_cookie_header(jar, prepared)

    def _record(self, method, url, kwargs, jar):
        call = Call(method, url, kwargs, self._cookie_header(jar, url))
        self.calls.append(call)
        response = self._match(method, url)
        if not response.pinned_url:
            # A real transport reports the URL it actually reached.
            response.url = url
        return response

    def install(self, stack):
        network = self

        def session_request(session, method, url, **kwargs):
            return network._record(str(method).upper(), url, kwargs, session.cookies)

        def plain(method):
            def call(url, **kwargs):
                jar = kwargs.get("cookies")
                if isinstance(jar, dict):
                    jar = requests.cookies.cookiejar_from_dict(jar)
                return network._record(method, url, kwargs, jar)

            return call

        stack.enter_context(
            mock.patch.object(requests.Session, "request", session_request)
        )
        stack.enter_context(mock.patch("requests.get", plain("GET")))
        stack.enter_context(mock.patch("requests.post", plain("POST")))
        stack.enter_context(mock.patch.object(mod, "_reject_obvious_local_target"))
        return self

    # -- assertions ----------------------------------------------------------

    def calls_to(self, fragment):
        return [call for call in self.calls if fragment in call.url]

    def credential_traces(self, secret):
        """Every call whose headers, cookies, params or body carried `secret`."""
        traces = []
        for call in self.calls:
            blob = json.dumps(
                {
                    "headers": call.headers,
                    "cookies": call.cookies,
                    "params": {k: str(v) for k, v in call.params.items()},
                    "json": call.json,
                },
                default=str,
            )
            if secret in blob:
                traces.append(call)
        return traces


class ScenarioTest(unittest.TestCase):
    """Shared plumbing for one portal's end-to-end walk."""

    maxDiff = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_path = os.path.join(self._tmp.name, "sessions.json")
        self.download_dir = os.path.join(self._tmp.name, "downloads")

    def fresh_auth(self):
        return mod.AuthManager(mod.AuthStore(self.store_path))

    def connected(self, platform, credential):
        """Save a credential the way the account panel does."""
        auth = self.fresh_auth()
        auth.save_token(platform, credential, label="e2e")
        self.assertTrue(
            auth.authenticated(platform),
            f"{platform} should be connected after saving a credential",
        )
        return auth

    def network(self):
        from contextlib import ExitStack

        stack = ExitStack()
        self.addCleanup(stack.close)
        return FakeNetwork().install(stack)

    def assert_only_reached(self, net, secret, allowed_hosts):
        """The credential must appear only on its own allow-listed hosts."""
        for call in net.credential_traces(secret):
            host = mod._url_host(call.url)
            self.assertTrue(
                mod._host_matches(host, allowed_hosts),
                f"credential leaked to {host} via {call.method} {call.url}",
            )

    def assert_file_downloaded(self, name, expected):
        """The bytes the portal served must actually reach the disk."""
        path = os.path.join(self.download_dir, name)
        self.assertTrue(os.path.isfile(path), f"{name} was never written")
        with open(path, "rb") as handle:
            self.assertEqual(
                handle.read(), expected, f"{name} arrived truncated"
            )

    def download_and_import(self, platform, files, auth):
        """Run the real download + hand-off path for a resolved file list."""
        script = load_script_module()
        action = script.SearchEngineScript()
        action.auth = auth
        posts = []
        action._post = posts.append
        model = {"platform": mod._display_name(platform), "_platform_key": platform}
        with (
            mock.patch.object(script, "_download_dir", return_value=self.download_dir),
            mock.patch.object(script, "_load_in_orca", return_value=(True, "")),
            mock.patch.object(script, "_reject_obvious_local_target"),
        ):
            action._download_and_import(model, files)
        return posts


# ---------------------------------------------------------------------------
# Bearer-token portals
# ---------------------------------------------------------------------------


class ThingiverseScenario(ScenarioTest):
    TOKEN = "tv-access-token"

    def test_search_is_refused_before_a_token_is_saved(self):
        with self.assertRaises(mod.AuthRequired):
            mod.ThingiverseSearcher.search("benchy", self.fresh_auth(), {})

    def test_end_to_end_search_resolve_and_import(self):
        auth = self.connected("thingiverse", self.TOKEN)
        net = self.network()
        net.add(
            "api.thingiverse.com/search/",
            Response(
                json_data={
                    "hits": [
                        {
                            "id": 4242,
                            "name": "Calibration Cube",
                            "creator": {"name": "maker"},
                            "public_url": "https://www.thingiverse.com/thing:4242",
                            "license": "Creative Commons - Attribution",
                            "download_count": 12,
                        }
                    ],
                    "total": 1,
                }
            ),
        )
        net.add(
            "/things/4242/files",
            Response(
                json_data=[
                    {
                        "name": "cube.stl",
                        "download_url": "https://cdn.thingiverse.com/cube.stl",
                    }
                ]
            ),
        )
        net.add("cdn.thingiverse.com/cube.stl", model_file("cube.stl"))

        results = mod.ThingiverseSearcher.search("benchy", auth, {})
        self.assertEqual(results[0]["name"], "Calibration Cube")
        self.assertEqual(results[0]["_thing_id"], 4242)

        files = mod.ThingiverseSearcher.get_files(results[0], auth)
        self.assertEqual(files[0]["name"], "cube.stl")

        posts = self.download_and_import("thingiverse", files, auth)
        self.assertEqual(posts[-1]["action"], "imported")
        self.assert_file_downloaded("cube.stl", MODEL_BODY)

        search_call = net.calls_to("api.thingiverse.com/search/")[0]
        self.assertEqual(search_call.headers["authorization"], f"Bearer {self.TOKEN}")
        self.assert_only_reached(net, self.TOKEN, ("api.thingiverse.com",))

    def test_rejected_token_is_reported_as_an_auth_failure(self):
        auth = self.connected("thingiverse", self.TOKEN)
        net = self.network()
        net.add("api.thingiverse.com/search/", Response(status=401))
        with self.assertRaises(mod.AuthRequired):
            mod.ThingiverseSearcher.search("benchy", auth, {})


class MakerWorldScenario(ScenarioTest):
    TOKEN = "mw-bearer-token"

    def test_import_is_refused_before_a_token_is_saved(self):
        with self.assertRaises(mod.AuthRequired):
            mod.MakerWorldSearcher.get_files({"_model_id": 7}, self.fresh_auth())

    def test_end_to_end_search_profile_pick_and_import(self):
        auth = self.connected("makerworld", self.TOKEN)
        net = self.network()
        net.add(
            "search-service/select/design2",
            Response(
                json_data={
                    "hits": [
                        {
                            "id": 77,
                            "title": "Bracket",
                            "designCreator": {"name": "maker"},
                            "license": "BY",
                        }
                    ],
                    "total": 1,
                }
            ),
        )
        instance = {
            "id": 900,
            "title": "0.20 standard",
            "isDefault": True,
            "hasZipStl": False,
        }
        net.add(
            "design-service/design/77/instances",
            Response(json_data={"hits": [instance]}),
        )
        net.add(
            "design-service/design/77",
            Response(json_data={"modelId": 5150, "instances": [instance]}),
        )
        net.add(
            "iot-service/api/user/profile/900",
            Response(
                json_data={
                    "url": "https://cdn.bambulab.test/bracket.3mf",
                    "name": "bracket.3mf",
                }
            ),
        )
        net.add("cdn.bambulab.test/bracket.3mf", model_file("bracket.3mf"))

        results = mod.MakerWorldSearcher.search("bracket", auth, {})
        model = results[0]
        self.assertEqual(model["_model_id"], 77)

        choices = mod.MakerWorldSearcher.get_download_choices(model, auth)
        self.assertEqual(choices["default_profile_id"], "900")

        model = dict(model, _profile_id="900", _download_format="3mf")
        files = mod.MakerWorldSearcher.get_files(model, auth)
        self.assertTrue(files[0]["name"].endswith(".3mf"))

        posts = self.download_and_import("makerworld", files, auth)
        self.assertEqual(posts[-1]["action"], "imported")
        self.assert_file_downloaded("bracket.3mf", MODEL_BODY)

        design_call = net.calls_to("design-service/design/77")[0]
        self.assertEqual(design_call.headers["authorization"], f"Bearer {self.TOKEN}")
        self.assert_only_reached(net, self.TOKEN, ("api.bambulab.com", "makerworld.com"))

    def test_expired_session_is_surfaced(self):
        auth = self.connected("makerworld", self.TOKEN)
        net = self.network()
        net.add(
            "design-service/design/77",
            Response(json_data={"modelId": 5150, "instances": [{"id": 900}]}),
        )
        net.add(
            "design-service/design/77/instances",
            Response(json_data={"hits": [{"id": 900}]}),
        )
        net.add("iot-service/api/user/profile/900", Response(status=401))
        model = {"_model_id": 77, "_profile_id": "900", "_download_format": "3mf"}
        with self.assertRaises(mod.AuthRequired):
            mod.MakerWorldSearcher.get_files(model, auth)


class ThangsScenario(ScenarioTest):
    TOKEN = "thangs-bearer"
    DOWNLOAD = "https://production-api.thangs.com/v2/models/31/download-url"

    def test_import_is_refused_before_a_token_is_saved(self):
        model = {"download_url": self.DOWNLOAD, "url": "https://thangs.com/m/31"}
        with self.assertRaises(mod.AuthRequired):
            mod.ThangsSearcher.get_files(model, self.fresh_auth())

    def test_end_to_end_search_resolve_and_import(self):
        auth = self.connected("thangs", self.TOKEN)
        net = self.network()
        net.add(
            "search-by-text",
            Response(
                json_data={
                    "items": [
                        {
                            "modelId": 31,
                            "name": "Clip",
                            "ownerUsername": "maker",
                            "modelPageUrl": "https://thangs.com/m/31",
                            "downloadUrl": self.DOWNLOAD,
                        }
                    ],
                    "totalPages": 1,
                    "totalResults": 1,
                }
            ),
        )
        net.add(
            "/v2/models/31/download-url",
            Response(
                json_data={
                    "signedUrl": "https://storage.thangs.test/clip.zip?sig=1",
                    "fileName": "clip",
                }
            ),
        )
        net.add("storage.thangs.test/clip.zip", model_file("clip.zip", EMPTY_ZIP))

        results = mod.ThangsSearcher.search("clip", auth, {})
        model = results[0]
        self.assertTrue(model["direct_import"])

        files = mod.ThangsSearcher.get_files(model, auth)
        self.assertEqual(files[0]["name"], "clip.zip")

        posts = self.download_and_import("thangs", files, auth)
        # An empty archive yields no loadable model, which is still a clean end.
        self.assertIn(posts[-1]["action"], ("imported", "downloaded_only"))
        self.assert_file_downloaded("clip.zip", EMPTY_ZIP)

        resolve = net.calls_to("/v2/models/31/download-url")[0]
        self.assertEqual(resolve.headers["authorization"], f"Bearer {self.TOKEN}")
        # The signed storage URL must never receive the portal token.
        self.assert_only_reached(net, self.TOKEN, ("production-api.thangs.com",))

    def test_rejected_token_is_surfaced(self):
        auth = self.connected("thangs", self.TOKEN)
        net = self.network()
        net.add("/v2/models/31/download-url", Response(status=403))
        model = {"download_url": self.DOWNLOAD, "url": "https://thangs.com/m/31"}
        with self.assertRaises(mod.AuthRequired):
            mod.ThangsSearcher.get_files(model, auth)


# ---------------------------------------------------------------------------
# Cookie and header portals
# ---------------------------------------------------------------------------


class NexprintScenario(ScenarioTest):
    TOKEN = "nexprint-auth-token"

    def test_import_is_refused_before_a_session_is_saved(self):
        with self.assertRaises(mod.AuthRequired):
            mod.NexprintSearcher.get_files({"_model_id": "abc"}, self.fresh_auth())

    def test_end_to_end_search_profile_pick_and_import(self):
        auth = self.connected("nexprint", self.TOKEN)
        net = self.network()
        net.add(
            "model-base-info/search",
            Response(
                json_data={
                    "code": 0,
                    "data": {
                        "pageResult": {
                            "list": [
                                {
                                    "modelId": "m-1",
                                    "modelName": "Hook",
                                    "authorName": "maker",
                                    "licenseType": 1,
                                }
                            ],
                            "total": 1,
                        }
                    },
                }
            ),
        )
        detail = {
            "code": 0,
            "data": {
                "settingInfoList": [
                    {
                        "id": "5001",
                        "settingName": "0.2 draft",
                        "settingFile": {
                            "fileId": "f-1",
                            "fileName": "hook.3mf",
                            "fileSize": 2048,
                        },
                    }
                ]
            },
        }
        net.add("model-base-info/get", Response(json_data=detail))
        net.add(
            "presigned-download-url",
            Response(
                json_data={
                    "code": 0,
                    "data": {
                        "fileInfoList": [
                            {"resultUrl": "https://cdn.nexprint.test/hook.3mf"}
                        ]
                    },
                }
            ),
        )
        net.add("cdn.nexprint.test/hook.3mf", model_file("hook.3mf"))

        results = mod.NexprintSearcher.search("hook", auth, {})
        model = results[0]
        self.assertEqual(model["_model_id"], "m-1")

        choices = mod.NexprintSearcher.get_download_choices(model, auth)
        self.assertEqual(choices["default_profile_id"], "5001")

        model = dict(model, _profile_id="5001", _download_format="3mf")
        files = mod.NexprintSearcher.get_files(model, auth)
        self.assertEqual(files[0]["name"], "hook.3mf")

        posts = self.download_and_import("nexprint", files, auth)
        self.assertEqual(posts[-1]["action"], "imported")
        self.assert_file_downloaded("hook.3mf", MODEL_BODY)

        detail_call = net.calls_to("model-base-info/get")[-1]
        self.assertIn(f"auth_token={self.TOKEN}", detail_call.cookies or "")
        self.assert_only_reached(net, self.TOKEN, ("nexprint.com",))

    def test_expired_cookie_is_surfaced(self):
        auth = self.connected("nexprint", self.TOKEN)
        net = self.network()
        net.add("model-base-info/get", Response(status=401))
        with self.assertRaises(mod.AuthRequired):
            mod.NexprintSearcher.get_files({"_model_id": "m-1"}, auth)


class MakeronlineScenario(ScenarioTest):
    TOKEN = "anycubic-access-token"

    def test_import_is_refused_before_a_token_is_saved(self):
        with self.assertRaises(mod.AuthRequired):
            mod.MakeronlineSearcher.get_files({"_mold_id": 5}, self.fresh_auth())

    def test_end_to_end_search_resolve_and_import(self):
        auth = self.connected("makeronline", self.TOKEN)
        net = self.network()
        net.add(
            "makeronline.com/api/search/model",
            Response(
                json_data={
                    "code": 0,
                    "data": {
                        "data": [
                            {
                                "mold_id": 5,
                                "title": "Spool holder",
                                "show_user_name": "maker",
                                "license": 1,
                                "target_url": "https://www.makeronline.com/en/model/5",
                            }
                        ]
                    },
                }
            ),
        )
        net.add(
            "makeronline.com/api/mold/detail",
            Response(
                json_data={
                    "code": 0,
                    "data": {
                        "files": [
                            {
                                "file_name": "holder.stl",
                                "url": "https://cdn.makeronline.test/holder.stl",
                            }
                        ]
                    },
                }
            ),
        )
        net.add("cdn.makeronline.test/holder.stl", model_file("holder.stl"))

        results = mod.MakeronlineSearcher.search("holder", auth, {})
        model = results[0]
        self.assertEqual(model["_mold_id"], 5)

        files = mod.MakeronlineSearcher.get_files(model, auth)
        self.assertEqual(files[0]["name"], "holder.stl")

        posts = self.download_and_import("makeronline", files, auth)
        self.assertEqual(posts[-1]["action"], "imported")
        self.assert_file_downloaded("holder.stl", MODEL_BODY)

        detail_call = net.calls_to("api/mold/detail")[0]
        self.assertEqual(detail_call.headers["xx-token"], self.TOKEN)
        self.assertEqual(detail_call.headers["authorization"], f"Bearer {self.TOKEN}")
        self.assert_only_reached(net, self.TOKEN, ("makeronline.com", "anycubic.com"))

    def test_expired_token_is_surfaced(self):
        auth = self.connected("makeronline", self.TOKEN)
        net = self.network()
        net.add("api/mold/detail", Response(status=403))
        with self.assertRaises(mod.AuthRequired):
            mod.MakeronlineSearcher.get_files({"_mold_id": 5}, auth)


class MyMiniFactoryScenario(ScenarioTest):
    KEY = "mmf-api-key"

    def test_search_is_refused_before_a_key_is_saved(self):
        with self.assertRaises(mod.AuthRequired):
            mod.MyMiniFactorySearcher.search("dragon", self.fresh_auth(), {})

    def test_end_to_end_search_resolve_and_import(self):
        auth = self.connected("myminifactory", self.KEY)
        net = self.network()
        net.add(
            "myminifactory.com/api/v2/search",
            Response(
                json_data={
                    "items": [
                        {
                            "id": 88,
                            "name": "Dragon",
                            "designer": {"username": "maker"},
                            "url": "https://www.myminifactory.com/object/88",
                            "archive_download_url": "https://cdn.mmf.test/dragon.zip",
                        }
                    ],
                    "total_count": 1,
                }
            ),
        )
        net.add(
            "cdn.mmf.test/dragon.zip",
            model_file("dragon.zip", EMPTY_ZIP),
        )

        results = mod.MyMiniFactorySearcher.search("dragon", auth, {})
        model = results[0]
        self.assertTrue(model["direct_import"])

        files = mod.MyMiniFactorySearcher.get_files(model, auth)
        self.assertEqual(files[0]["url"], "https://cdn.mmf.test/dragon.zip")

        posts = self.download_and_import("myminifactory", files, auth)
        self.assertIn(posts[-1]["action"], ("imported", "downloaded_only"))
        self.assert_file_downloaded("myminifactory_model.zip", EMPTY_ZIP)

        search_call = net.calls_to("api/v2/search")[0]
        # The key travels as a documented query parameter, not a bearer header.
        self.assertEqual(search_call.params.get("key"), self.KEY)
        self.assertNotIn("authorization", search_call.headers)
        self.assert_only_reached(net, self.KEY, ("myminifactory.com",))

    def test_a_model_without_an_archive_falls_back_to_the_browser(self):
        auth = self.connected("myminifactory", self.KEY)
        with self.assertRaises(mod.BrowserRequired):
            mod.MyMiniFactorySearcher.get_files(
                {"url": "https://www.myminifactory.com/object/88"}, auth
            )

    def test_rejected_key_is_surfaced(self):
        auth = self.connected("myminifactory", self.KEY)
        net = self.network()
        net.add("api/v2/search", Response(status=403))
        with self.assertRaises(mod.AuthRequired):
            mod.MyMiniFactorySearcher.search("dragon", auth, {})


class CrealityCloudScenario(ScenarioTest):
    TOKEN = "model_token=creality-session; model_user_id=42"

    def test_import_is_refused_before_a_session_is_saved(self):
        model = {"_profile_id": "3001", "_download_format": "3mf"}
        with self.assertRaises(mod.AuthRequired):
            mod.CrealityCloudSearcher.get_files(model, self.fresh_auth())

    def test_end_to_end_search_profile_pick_and_import(self):
        auth = self.connected("crealitycloud", self.TOKEN)
        net = self.network()
        net.add(
            "smart_search/v1/model",
            Response(
                json_data={
                    "code": 0,
                    "result": {
                        "list": [
                            {
                                "id": "grp-1",
                                "groupName": "Vase",
                                "urlAlias": "vase",
                                "model3mfCount": 1,
                                "userInfo": {"nickName": "maker"},
                            }
                        ],
                        "count": 1,
                    },
                }
            ),
        )
        net.add(
            "v3/model/3mfList",
            Response(
                json_data={
                    "code": 0,
                    "result": {"list": [{"id": "3001", "name": "0.2 vase"}]},
                }
            ),
        )
        net.add(
            "v3/model/3mfDetail",
            Response(json_data={"code": 0, "result": {"name": "vase", "size": 1024}}),
        )
        net.add(
            "v3/model/3mfDownload",
            Response(
                json_data={
                    "code": 0,
                    "result": {"url": "https://cdn.creality.test/vase.3mf"},
                }
            ),
        )
        net.add("cdn.creality.test/vase.3mf", model_file("vase.3mf"))

        results = mod.CrealityCloudSearcher.search("vase", auth, {})
        model = results[0]
        self.assertEqual(model["_model_id"], "grp-1")

        choices = mod.CrealityCloudSearcher.get_download_choices(model, auth)
        self.assertEqual(choices["default_profile_id"], "3001")

        model = dict(model, _profile_id="3001", _download_format="3mf")
        files = mod.CrealityCloudSearcher.get_files(model, auth)
        self.assertEqual(files[0]["name"], "vase.3mf")

        posts = self.download_and_import("crealitycloud", files, auth)
        self.assertEqual(posts[-1]["action"], "imported")
        self.assert_file_downloaded("vase.3mf", MODEL_BODY)

        download_call = net.calls_to("v3/model/3mfDownload")[0]
        self.assertEqual(download_call.headers["__cxy_token_"], "creality-session")
        self.assertEqual(download_call.headers["__cxy_uid_"], "42")
        self.assert_only_reached(net, "creality-session", ("admin.crealitycloud.com",))

    def test_expired_session_is_surfaced(self):
        auth = self.connected("crealitycloud", self.TOKEN)
        net = self.network()
        net.add("v3/model/3mfDetail", Response(json_data={"code": 0, "result": {}}))
        net.add(
            "v3/model/3mfDownload",
            Response(json_data={"code": 401, "msg": "token expired"}),
        )
        model = {"_profile_id": "3001", "_download_format": "3mf"}
        with self.assertRaises(mod.AuthRequired):
            mod.CrealityCloudSearcher.get_files(model, auth)


# ---------------------------------------------------------------------------
# Browser-session portals resolved from HTML
# ---------------------------------------------------------------------------


CULTS_PAGE = """
<html><body>
<a href="/en/3d-model/tools/handy-clip">Handy clip</a>
</body></html>
"""

CULTS_MODEL_PAGE = """
<html><body>
<a href="https://files.cults3d.test/clip.stl">Download STL</a>
</body></html>
"""

GRABCAD_PAGE = """
<html><body>
<a href="/library/precision-bracket">Precision bracket</a>
</body></html>
"""

GRABCAD_MODEL_PAGE = """
<html><body>
<a href="https://files.grabcad.test/bracket.step">Download</a>
</body></html>
"""


class Cults3DScenario(ScenarioTest):
    COOKIE = "session=cults-secret; other=1"

    def test_import_is_refused_before_a_session_is_saved(self):
        model = {"url": "https://cults3d.com/en/3d-model/tools/handy-clip"}
        with self.assertRaises(mod.AuthRequired):
            mod.Cults3DSearcher.get_files(model, self.fresh_auth())

    def test_end_to_end_search_resolve_and_import(self):
        auth = self.connected("cults3d", self.COOKIE)
        net = self.network()
        net.add("cults3d.com/en/tags/", Response(text=CULTS_PAGE, url="https://cults3d.com/en/tags/clip"))
        net.add(
            "cults3d.com/en/3d-model/tools/handy-clip",
            Response(
                text=CULTS_MODEL_PAGE,
                url="https://cults3d.com/en/3d-model/tools/handy-clip",
            ),
        )
        net.add("files.cults3d.test/clip.stl", model_file("clip.stl"))

        results = mod.Cults3DSearcher.search("clip", auth, {})
        model = results[0]
        self.assertTrue(model["requires_auth"])

        files = mod.Cults3DSearcher.get_files(model, auth)
        self.assertEqual(files[0]["name"], "clip.stl")

        posts = self.download_and_import("cults3d", files, auth)
        self.assertEqual(posts[-1]["action"], "imported")
        self.assert_file_downloaded("clip.stl", MODEL_BODY)

        page_call = net.calls_to("cults3d.com/en/3d-model")[0]
        self.assertIn("session=cults-secret", page_call.cookies or "")
        self.assert_only_reached(net, "cults-secret", ("cults3d.com",))

    def test_a_signed_out_session_is_surfaced(self):
        auth = self.connected("cults3d", self.COOKIE)
        net = self.network()
        net.add(
            "cults3d.com/en/3d-model",
            Response(status=403, url="https://cults3d.com/en/3d-model/tools/handy-clip"),
        )
        model = {"url": "https://cults3d.com/en/3d-model/tools/handy-clip"}
        with self.assertRaises(mod.AuthRequired):
            mod.Cults3DSearcher.get_files(model, auth)


class GrabCadScenario(ScenarioTest):
    COOKIE = "_grabcad_session=gc-secret; other=1"

    def test_search_is_refused_before_a_session_is_saved(self):
        with self.assertRaises(mod.AuthRequired):
            mod.GrabcadSearcher.search("bracket", self.fresh_auth(), {})

    def test_end_to_end_search_resolve_and_import(self):
        auth = self.connected("grabcad", self.COOKIE)
        net = self.network()
        net.add(
            "grabcad.com/library?",
            Response(text=GRABCAD_PAGE, url="https://grabcad.com/library"),
        )
        net.add(
            "grabcad.com/library/precision-bracket",
            Response(
                text=GRABCAD_MODEL_PAGE,
                url="https://grabcad.com/library/precision-bracket",
            ),
        )
        net.add("files.grabcad.test/bracket.step", model_file("bracket.step"))

        results = mod.GrabcadSearcher.search("bracket", auth, {})
        model = results[0]

        files = mod.GrabcadSearcher.get_files(model, auth)
        self.assertEqual(files[0]["name"], "bracket.step")

        posts = self.download_and_import("grabcad", files, auth)
        self.assertEqual(posts[-1]["action"], "imported")
        self.assert_file_downloaded("bracket.step", MODEL_BODY)

        page_call = net.calls_to("grabcad.com/library/precision-bracket")[0]
        self.assertIn("_grabcad_session=gc-secret", page_call.cookies or "")
        self.assert_only_reached(net, "gc-secret", ("grabcad.com",))

    def test_a_signed_out_session_is_surfaced(self):
        auth = self.connected("grabcad", self.COOKIE)
        net = self.network()
        net.add(
            "grabcad.com/library/precision-bracket",
            Response(status=401, url="https://grabcad.com/library/precision-bracket"),
        )
        model = {"url": "https://grabcad.com/library/precision-bracket"}
        with self.assertRaises(mod.AuthRequired):
            mod.GrabcadSearcher.get_files(model, auth)


# ---------------------------------------------------------------------------
# Cross-portal invariants
# ---------------------------------------------------------------------------


class AllAuthorizedPortalsTests(ScenarioTest):
    def test_every_authorized_portal_has_a_scenario(self):
        registered = {
            spec.key for spec in mod._PLATFORM_SPECS if spec.requires_auth
        }
        self.assertEqual(
            registered,
            set(AUTHORIZED),
            "a portal gained or lost authentication without an end-to-end scenario",
        )
        covered = {
            cls.__name__
            for cls in ScenarioTest.__subclasses__()
            if cls is not AllAuthorizedPortalsTests
        }
        self.assertEqual(len(covered), len(AUTHORIZED))

    # Entry points that must refuse before a credential exists. Keyed by
    # portal so a new authorized portal cannot quietly arrive without one.
    GATED_SEARCHES: ClassVar[dict] = {
        "thingiverse": lambda auth: mod.ThingiverseSearcher.search("x", auth, {}),
        "myminifactory": lambda auth: mod.MyMiniFactorySearcher.search("x", auth, {}),
        "grabcad": lambda auth: mod.GrabcadSearcher.search("x", auth, {}),
    }

    GATED_RESOLVERS: ClassVar[dict] = {
        "thingiverse": lambda auth: mod.ThingiverseSearcher.get_files(
            {"_thing_id": 1}, auth
        ),
        "makerworld": lambda auth: mod.MakerWorldSearcher.get_files(
            {"_model_id": 1}, auth
        ),
        "nexprint": lambda auth: mod.NexprintSearcher.get_files(
            {"_model_id": "m-1"}, auth
        ),
        "makeronline": lambda auth: mod.MakeronlineSearcher.get_files(
            {"_mold_id": 1}, auth
        ),
        "myminifactory": lambda auth: mod.MyMiniFactorySearcher.get_files(
            {"_archive_download_url": "https://cdn.mmf.test/a.zip"}, auth
        ),
        "thangs": lambda auth: mod.ThangsSearcher.get_files(
            {
                "download_url": (
                    "https://production-api.thangs.com/v2/models/31/download-url"
                ),
                "url": "https://thangs.com/m/31",
            },
            auth,
        ),
        "crealitycloud": lambda auth: mod.CrealityCloudSearcher.get_files(
            {"_profile_id": "3001", "_download_format": "3mf"}, auth
        ),
        "cults3d": lambda auth: mod.Cults3DSearcher.get_files(
            {"url": "https://cults3d.com/en/3d-model/tools/handy-clip"}, auth
        ),
        "grabcad": lambda auth: mod.GrabcadSearcher.get_files(
            {"url": "https://grabcad.com/library/precision-bracket"}, auth
        ),
    }

    def test_every_authorized_portal_gates_file_resolution(self):
        """No portal may resolve files before its credential is connected."""
        self.assertEqual(
            set(self.GATED_RESOLVERS),
            set(AUTHORIZED),
            "an authorized portal has no credential-gate scenario",
        )
        # Nothing is routed: a portal that reaches the network instead of
        # refusing fails loudly rather than silently passing.
        net = self.network()
        for key, call in self.GATED_RESOLVERS.items():
            with self.subTest(portal=key), self.assertRaises(mod.AuthRequired):
                call(self.fresh_auth())
        self.assertEqual(net.calls, [])

    def test_portals_that_gate_search_refuse_before_connecting(self):
        gated = {
            spec.key
            for spec in mod._PLATFORM_SPECS
            if spec.key in ("thingiverse", "myminifactory", "grabcad")
        }
        self.assertEqual(set(self.GATED_SEARCHES), gated)
        net = self.network()
        for key, call in self.GATED_SEARCHES.items():
            with self.subTest(portal=key), self.assertRaises(mod.AuthRequired):
                call(self.fresh_auth())
        self.assertEqual(net.calls, [])

    def test_saved_credentials_stay_isolated_per_portal(self):
        auth = self.fresh_auth()
        for index, key in enumerate(AUTHORIZED):
            auth.save_token(key, f"secret-{index}=value-{index}")
        for index, key in enumerate(AUTHORIZED):
            self.assertIn(f"value-{index}", auth.token(key))
            for other_index, other in enumerate(AUTHORIZED):
                if other == key:
                    continue
                self.assertNotIn(f"value-{other_index}", auth.token(key))

    def test_no_portal_credential_reaches_a_foreign_host(self):
        for key in AUTHORIZED:
            with self.subTest(portal=key):
                auth = mod.AuthManager(
                    mod.AuthStore(os.path.join(self._tmp.name, f"{key}.json"))
                )
                auth.save_token(key, "portal_secret=leak-canary")
                net = self.network()
                net.add("cdn.attacker.example", model_file("payload.stl"))
                session = auth.session(key)
                auth.request(key, "GET", FOREIGN_HOST, session=session)
                self.assertEqual(
                    net.credential_traces("leak-canary"),
                    [],
                    f"{key} credential reached a foreign host",
                )

    def test_logout_clears_only_the_selected_portal(self):
        auth = self.fresh_auth()
        for key in AUTHORIZED:
            auth.save_token(key, f"{key}_token=value")
        auth.logout("cults3d")
        self.assertFalse(auth.authenticated("cults3d"))
        for key in AUTHORIZED:
            if key != "cults3d":
                self.assertTrue(auth.authenticated(key), key)

    def test_status_reports_every_authorized_portal(self):
        auth = self.fresh_auth()
        status = auth.status()
        self.assertEqual(set(status), set(AUTHORIZED))
        for key in AUTHORIZED:
            self.assertFalse(status[key]["authenticated"])

    def test_a_password_is_never_written_for_any_portal(self):
        auth = self.fresh_auth()
        for key in AUTHORIZED:
            auth.store.set(key, {"access_token": "t", "password": "hunter2"})
        with open(self.store_path, encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn("hunter2", raw)


if __name__ == "__main__":
    unittest.main()
