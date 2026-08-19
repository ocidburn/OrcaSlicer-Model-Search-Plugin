import os
import tempfile
import unittest
import zipfile
from unittest import mock

from tests._module_loader import PLUGIN_PATH, load_plugin

mod = load_plugin("search_engine_catalog")


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None, url="https://example.test/"):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url
        self.text = payload if isinstance(payload, str) else ""
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


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
            with mock.patch.object(
                auth, "request", return_value=FakeResponse(payload)
            ) as request:
                rows = mod.ThingiverseSearcher.search("cube", auth, {"page": 2})
        self.assertEqual(rows[0]["platform"], "Thingiverse")
        self.assertEqual(rows[0]["name"], "Calibration cube")
        self.assertTrue(rows[0]["requires_auth"])
        self.assertEqual(rows[0]["downloads"], 42)
        self.assertIn("thing:7379392", rows[0]["url"])
        self.assertTrue(rows[0]["_details_available"])
        self.assertFalse(rows[0]["_details_loaded"])
        self.assertEqual(request.call_args.kwargs["params"]["page"], 2)

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

    def test_html_fetch_retries_cloudflare_challenge_with_standard_browser_ua(self):
        url = "https://cults3d.com/en/tags/benchy"
        challenged = FakeResponse(
            "<title>Just a moment...</title>",
            status_code=403,
            headers={"cf-mitigated": "challenge", "server": "cloudflare"},
            url=url,
        )
        success = FakeResponse("<title>Models</title>", url=url)
        session = mock.Mock()
        session.request.side_effect = [challenged, success]

        with (
            mock.patch("requests.Session", return_value=session),
            mock.patch.object(mod, "_reject_obvious_local_target"),
        ):
            raw, final_url = mod._fetch_html(url)

        self.assertEqual(raw, "<title>Models</title>")
        self.assertEqual(final_url, url)
        self.assertTrue(challenged.closed)
        self.assertTrue(success.closed)
        self.assertEqual(session.request.call_count, 2)
        first_headers = session.request.call_args_list[0].kwargs["headers"]
        retry_headers = session.request.call_args_list[1].kwargs["headers"]
        self.assertEqual(first_headers["User-Agent"], mod._BROWSER_UA)
        self.assertEqual(retry_headers["User-Agent"], mod._STANDARD_BROWSER_UA)
        session.close.assert_called_once_with()

    def test_html_fetch_surfaces_persistent_cloudflare_challenge_for_browser(self):
        url = "https://cults3d.com/en/tags/benchy"
        responses = [
            FakeResponse(
                "<title>Just a moment...</title>",
                status_code=403,
                headers={"cf-mitigated": "challenge", "server": "cloudflare"},
                url=url,
            )
            for _ in range(2)
        ]
        session = mock.Mock()
        session.request.side_effect = responses

        with (
            mock.patch("requests.Session", return_value=session),
            mock.patch.object(mod, "_reject_obvious_local_target"),
            self.assertRaises(mod.CloudflareChallenge) as raised,
        ):
            mod._fetch_html(url)

        self.assertEqual(raised.exception.url, url)
        # The message must name the blocked host and point at the verification
        # hand-off rather than telling the user to just try again later.
        self.assertEqual(raised.exception.host, "cults3d.com")
        self.assertIn("cults3d.com", str(raised.exception))
        self.assertIn("cf_clearance", str(raised.exception))
        self.assertEqual(session.request.call_count, 2)
        self.assertTrue(all(response.closed for response in responses))
        session.close.assert_called_once_with()

    def test_anonymous_html_fetch_rejects_private_redirect_targets(self):
        session = mock.Mock()
        session.request.side_effect = AssertionError(
            "the SSRF guard must run before the request"
        )
        with (
            mock.patch("requests.Session", return_value=session),
            self.assertRaises(ValueError),
        ):
            mod._fetch_html("http://127.0.0.1:8080/admin")
        session.request.assert_not_called()
        session.close.assert_called_once_with()

    def test_cloudflare_turnstile_page_is_detected_even_with_http_200(self):
        response = FakeResponse(
            '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js">'
            '</script><div class="cf-turnstile"></div>',
            status_code=200,
        )
        self.assertTrue(mod._is_cloudflare_challenge(response))

    def test_myminifactory_search_uses_official_api(self):
        payload = {
            "items": [
                {
                    "id": 127830,
                    "name": "Yak Mount",
                    "url": "https://www.myminifactory.com/object/3d-print-yak-mount-127830",
                    "designer": {"username": "maker"},
                    "images": [
                        {
                            "is_primary": False,
                            "thumbnail": {"url": "https://cdn.example/other-thumb.jpg"},
                        },
                        {
                            "is_primary": True,
                            "thumbnail": {"url": "https://cdn.example/yak-thumb.jpg"},
                            "standard": {"url": "https://cdn.example/yak-standard.jpg"},
                            "original": {"url": "https://cdn.example/yak-original.jpg"},
                        },
                    ],
                    "license": (
                        "MyMiniFactory Digital File Store License | "
                        "Standard Digital File Store License"
                    ),
                    "views": 50,
                    "likes": 4,
                }
            ]
        }
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            auth.save_token("myminifactory", "api-key")
            with mock.patch.object(
                auth, "request", return_value=FakeResponse(payload)
            ) as request:
                rows = mod.MyMiniFactorySearcher.search("yak", auth, {"page": 3})
        self.assertEqual(rows[0]["name"], "Yak Mount")
        self.assertEqual(rows[0]["platform"], "MyMiniFactory")
        self.assertEqual(rows[0]["views"], 50)
        self.assertEqual(
            rows[0]["thumbnail_url"], "https://cdn.example/yak-thumb.jpg"
        )
        self.assertEqual(rows[0]["license"], "Standard Digital File Store License")
        self.assertEqual(
            rows[0]["license_url"],
            "https://www.myminifactory.com/object-licensing",
        )
        self.assertIn("Non-commercial personal use", rows[0]["license_summary"])
        self.assertEqual(request.call_args.kwargs["params"]["page"], 3)

    def test_api_searchers_send_real_page_parameters(self):
        response = FakeResponse({"code": 0, "data": {"data": []}})
        with mock.patch("requests.post", return_value=response) as post:
            rows = mod.MakeronlineSearcher.search("cube", None, {"page": 4})
        self.assertEqual(post.call_args.kwargs["json"]["page"], 4)
        self.assertIsInstance(rows, mod.SearchPage)

        response = FakeResponse({"code": 0, "data": {"pageResult": {"list": [], "total": 61}}})
        with mock.patch("requests.get", return_value=response) as get:
            rows = mod.NexprintSearcher.search("cube", None, {"page": 2})
        self.assertEqual(get.call_args.kwargs["params"]["pageNo"], "2")
        self.assertEqual(rows.total, 61)
        self.assertTrue(rows.has_more)

    def test_makeronline_preserves_working_cdn_thumbnail_url(self):
        thumbnail = (
            "https://cdn-acop.makeronline.com/asop/2026-07/25/jpg/"
            "178495837862472000-6a644daa9885_thumbnail.jpg"
        )
        payload = {
            "code": 0,
            "data": {
                "data": [
                    {
                        "title": "Cup",
                        "mold_id": 314922,
                        "mold_image": thumbnail,
                        "target_url": "https://www.makeronline.com/model/Cup/314922.html",
                    }
                ]
            },
        }
        with mock.patch("requests.post", return_value=FakeResponse(payload)):
            rows = mod.MakeronlineSearcher.search("cup", None)

        self.assertEqual(rows[0]["thumbnail_url"], thumbnail)
        self.assertNotIn("_400x300", rows[0]["thumbnail_url"])

    def test_makeronline_normalizes_relative_and_protocol_relative_images(self):
        self.assertEqual(
            mod._makeronline_thumbnail_url("/images/cup_thumbnail.jpg"),
            "https://www.makeronline.com/images/cup_thumbnail.jpg",
        )
        self.assertEqual(
            mod._makeronline_thumbnail_url("//cdn-acop.makeronline.com/cup.webp"),
            "https://cdn-acop.makeronline.com/cup.webp",
        )
        self.assertEqual(mod._makeronline_thumbnail_url("javascript:alert(1)"), "")

    def test_printables_and_makerworld_use_offsets(self):
        printables_payload = {"data": {"searchPrints2": {"items": []}}}
        with mock.patch(
            "requests.post", return_value=FakeResponse(printables_payload)
        ) as post:
            mod.PrintablesSearcher.search("cube", None, {"page": 3})
        variables = post.call_args.kwargs["json"]["variables"]
        self.assertEqual(variables["offset"], 60)
        self.assertIn("offset: $offset", mod.PrintablesSearcher.SEARCH_QUERY)

        makerworld_payload = {"hits": [], "total": 75}
        with mock.patch(
            "requests.get", return_value=FakeResponse(makerworld_payload)
        ) as get:
            rows = mod.MakerWorldSearcher.search("cube", None, {"page": 2})
        self.assertEqual(get.call_args.kwargs["params"]["offset"], "30")
        self.assertEqual(rows.total, 75)
        self.assertTrue(rows.has_more)

    def test_myminifactory_store_flag_supplies_license_fallback(self):
        row = mod._myminifactory_result(
            {
                "id": 10,
                "name": "Store model",
                "license": "",
                "licenses": [{"type": "store", "value": True}],
            }
        )
        self.assertEqual(row["license"], "MyMiniFactory Digital File Store License")
        self.assertNotEqual(row["license"], "Unknown")

    def test_myminifactory_preserves_creative_commons_license_url(self):
        row = mod._myminifactory_result(
            {
                "id": 11,
                "name": "Free model",
                "license": "Creative Commons - Attribution",
            }
        )
        self.assertEqual(row["license"], "CC BY")
        self.assertEqual(
            row["license_url"], "https://creativecommons.org/licenses/by/4.0/"
        )

    def test_thangs_search_uses_json_api_with_pagination_and_metrics(self):
        payload = {
            "items": [
                {
                    "modelId": "17751",
                    "name": "Cup",
                    "ownerUsername": "Maker",
                    "modelPageUrl": "https://thangs.com/m/17751",
                    "downloadUrl": "https://thangs.com/api/v2/models/17751/download-url",
                    "thumbnailUrl": "https://storage.googleapis.com/thumb/cup.png",
                    "downloadCount": 9649,
                    "likesCount": 2013,
                    "publishedOn": "2021-05-28T23:19:43.078Z",
                    "marketplaceInfo": {"priceInUSD": 3},
                }
            ],
            "totalPages": 75,
            "totalResults": 3733,
        }
        with mock.patch("requests.get", return_value=FakeResponse(payload)) as get:
            rows = mod.ThangsSearcher.search(
                "cup", None, {"page": 2, "sort": "downloads"}
            )

        self.assertEqual(rows[0]["platform"], "Thangs")
        self.assertEqual(rows[0]["downloads"], 9649)
        self.assertEqual(rows[0]["likes"], 2013)
        self.assertEqual(rows[0]["price"], 3)
        self.assertEqual(
            rows[0]["thumbnail_url"],
            "https://storage.googleapis.com/thumb/cup.png",
        )
        self.assertEqual(rows.total, 3733)
        self.assertTrue(rows.has_more)
        self.assertEqual(
            rows[0]["download_url"],
            "https://production-api.thangs.com/v2/models/17751/download-url",
        )
        self.assertTrue(rows[0]["requires_auth"])
        self.assertTrue(rows[0]["direct_import"])
        self.assertEqual(get.call_args.kwargs["params"]["page"], 1)
        self.assertEqual(get.call_args.kwargs["params"]["pageSize"], 50)
        self.assertEqual(get.call_args.kwargs["params"]["sort"], "downloads")
        self.assertEqual(
            mod.ThangsSearcher.SEARCH_URL,
            "https://production-api.thangs.com/search/v5/search-by-text",
        )
        self.assertEqual(get.call_args.kwargs["headers"]["Origin"], "https://thangs.com")

    def test_thangs_cloudflare_challenge_retries_then_opens_browser(self):
        first = FakeResponse(
            "Just a moment cf-chl-",
            status_code=403,
            headers={"server": "cloudflare", "cf-mitigated": "challenge"},
            url=mod.ThangsSearcher.SEARCH_URL,
        )
        second = FakeResponse(
            "Just a moment challenge-platform",
            status_code=403,
            headers={"server": "cloudflare", "cf-mitigated": "challenge"},
            url=mod.ThangsSearcher.SEARCH_URL,
        )
        with (
            mock.patch("requests.get", side_effect=(first, second)) as get,
            self.assertRaises(mod.CloudflareChallenge) as raised,
        ):
            mod.ThangsSearcher.search("dragon", None)
        self.assertEqual(get.call_count, 2)
        self.assertIn("/search/dragon", raised.exception.url)

    def test_thangs_download_resolver_normalization_is_strict(self):
        self.assertEqual(
            mod._thangs_api_url(
                "https://www.thangs.com/api/v2/models/17751/download-url"
            ),
            "https://production-api.thangs.com/v2/models/17751/download-url",
        )
        self.assertEqual(
            mod._thangs_api_url(
                "http://thangs.com/api/v2/models/17751/download-url"
            ),
            "",
        )
        self.assertEqual(
            mod._thangs_api_url("https://evil.thangs.com/api/v2/models/1/download-url"),
            "",
        )

    def test_creality_search_uses_api_and_keeps_high_resolution_cover(self):
        payload = {
            "code": 0,
            "result": {
                "count": 61,
                "list": [
                    {
                        "id": "68df5251aaaa058eab3729a1",
                        "urlAlias": "dragon-cup-3d-print",
                        "groupName": "DRAGON CUP",
                        "userInfo": {"nickName": "ABDULLAH ALAN"},
                        "covers": [
                            {
                                "url": "https://pic2-cdn.creality.com/comp/model/dragon.webp",
                                "originUrl": "https://pic2-cdn.creality.com/model/dragon.png",
                            }
                        ],
                        "license": "CXY-SL",
                        "downloadCount": 436,
                        "likeCount": 202,
                        "pv": 2405,
                        "model3mfCount": 2,
                        "isPay": False,
                    }
                ],
            },
        }
        with mock.patch("requests.post", return_value=FakeResponse(payload)) as post:
            rows = mod.CrealityCloudSearcher.search(
                "dragon cup", None, {"page": 2, "sort": "downloads"}
            )
        self.assertEqual(rows[0]["platform"], "Creality Cloud")
        self.assertEqual(rows[0]["name"], "DRAGON CUP")
        self.assertEqual(
            rows[0]["thumbnail_url"],
            "https://pic2-cdn.creality.com/comp/model/dragon.webp",
        )
        self.assertNotIn("h_10", rows[0]["thumbnail_url"])
        self.assertEqual(rows[0]["license"], "Standard Digital File License")
        self.assertEqual(rows[0]["downloads"], 436)
        self.assertEqual(rows.total, 61)
        self.assertTrue(rows.has_more)
        self.assertEqual(post.call_args.kwargs["json"]["page"], 2)
        self.assertEqual(post.call_args.kwargs["json"]["sortType"], 7)

    def test_creality_lists_print_profiles_and_formats(self):
        payload = {
            "code": 0,
            "result": {
                "count": 1,
                "list": [
                    {
                        "id": "690dd45f2904ad6ab51c9f2c",
                        "secondName": "0.16mm layer, 2 walls, 15% infill",
                        "thumbnail": "https://pic2-cdn.creality.com/profile.png",
                        "printerName": "Creality Hi",
                        "layerHeight": "0.16",
                        "wallLoops": "2",
                        "sparseInfillDensity": "15%",
                        "plateCount": 1,
                        "printTime": 4135,
                        "size": 13954344,
                        "userInfo": {"nickName": "3dtex"},
                    }
                ],
            },
        }
        with mock.patch("requests.post", return_value=FakeResponse(payload)):
            choices = mod.CrealityCloudSearcher.get_download_choices(
                {
                    "_model_id": "68df5251aaaa058eab3729a1",
                    "platform": "Creality Cloud",
                }
            )
        self.assertEqual(choices["picker_platform"], "Creality Cloud")
        self.assertEqual(
            choices["profiles"][0]["profile_id"], "690dd45f2904ad6ab51c9f2c"
        )
        self.assertEqual(choices["profiles"][0]["printer"], "Creality Hi")
        self.assertEqual(
            [item["id"] for item in choices["formats"]], ["3mf", "raw_browser"]
        )

    def test_creality_signed_3mf_keeps_file_extension(self):
        auth = mock.Mock()
        auth.authenticated.return_value = True
        auth.token.return_value = (
            "model_token=model-token; model_user_id=5925463110"
        )
        responses = [
            FakeResponse(
                {
                    "code": 0,
                    "result": {"name": "0.16mm layer, 2 walls", "size": 4096},
                }
            ),
            FakeResponse(
                {
                    "code": 0,
                    "result": "https://file2-cdn.creality.com/signed/profile",
                }
            ),
        ]
        with mock.patch("requests.post", side_effect=responses) as post:
            files = mod.CrealityCloudSearcher.get_files(
                {
                    "platform": "Creality Cloud",
                    "_profile_id": "690dd45f2904ad6ab51c9f2c",
                    "_download_format": "3mf",
                },
                auth,
            )
        self.assertEqual(files[0]["name"], "0.16mm layer_ 2 walls.3mf")
        self.assertEqual(
            files[0]["url"], "https://file2-cdn.creality.com/signed/profile"
        )
        self.assertEqual(
            post.call_args.kwargs["headers"]["__CXY_TOKEN_"], "model-token"
        )
        self.assertEqual(
            post.call_args.kwargs["headers"]["__CXY_UID_"], "5925463110"
        )

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

    def test_thangs_model_without_download_resolver_goes_to_browser(self):
        model = {"url": "https://thangs.com/designer/A/3d-model/Paid-1"}
        with self.assertRaises(mod.BrowserRequired):
            mod.ThangsSearcher.get_files(model)

    def test_thangs_download_resolver_returns_signed_archive_with_extension(self):
        model = {
            "url": "https://thangs.com/m/17751",
            "download_url": "https://thangs.com/api/v2/models/17751/download-url",
        }
        signed_url = (
            "https://storage.googleapis.com/thangs/models/Kyle_Cup.zip"
            "?response-content-disposition=attachment%3B%20filename%3D%22Kyle_Cup.zip%22"
        )
        payload = {
            "fileName": "Kyle Cup V5 - New Design",
            "signedUrl": signed_url,
            "modelSource": 0,
            "downloadId": 26996842,
        }
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            auth.save_token("thangs", "Bearer thangs-token")
            with mock.patch.object(
                auth, "request", return_value=FakeResponse(payload)
            ) as request:
                files = mod.ThangsSearcher.get_files(model, auth)
        self.assertEqual(files[0]["name"], "Kyle Cup V5 - New Design.zip")
        self.assertEqual(files[0]["url"], signed_url)
        self.assertEqual(
            request.call_args.args[:3],
            (
                "thangs",
                "GET",
                "https://production-api.thangs.com/v2/models/17751/download-url",
            ),
        )
        self.assertFalse(request.call_args.kwargs["allow_redirects"])

    def test_thangs_bearer_token_is_scoped_to_thangs_hosts(self):
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            auth.save_token("thangs", "Authorization: Bearer secret-token")
            resolver_headers = auth._request_headers(
                "thangs",
                "https://production-api.thangs.com/v2/models/1/download-url",
            )
            storage_headers = auth._request_headers(
                "thangs", "https://storage.googleapis.com/thangs/model.zip"
            )
            website_headers = auth._request_headers(
                "thangs", "https://thangs.com/api/v2/models/1/download-url"
            )
        self.assertEqual(resolver_headers["Authorization"], "Bearer secret-token")
        self.assertNotIn("Authorization", storage_headers)
        self.assertNotIn("Authorization", website_headers)

    def test_thingiverse_lists_files_through_official_api(self):
        model = {"url": "https://www.thingiverse.com/thing:7379392"}
        payload = [
            {
                "name": "cube.stl",
                "download_url": "https://cdn.example/cube.stl",
                "thumbnail": "https://cdn.example/cube_thumb_medium.jpg",
                "size": 4096,
            },
            {
                "name": "base.stl",
                "download_url": "https://cdn.example/base.stl",
                "default_image": {
                    "url": "https://cdn.example/base-original.jpg",
                    "sizes": [
                        {
                            "size": "medium",
                            "url": "https://cdn.example/base-medium.jpg",
                        },
                        {
                            "size": "large",
                            "url": "https://cdn.example/base-large.jpg",
                        },
                    ],
                },
                "size": 8192,
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            auth = mod.AuthManager(mod.AuthStore(os.path.join(td, "sessions.json")))
            auth.save_token("thingiverse", "token")
            with mock.patch.object(auth, "request", return_value=FakeResponse(payload)):
                files = mod.ThingiverseSearcher.get_files(model, auth)
        self.assertEqual(files[0]["name"], "cube.stl")
        self.assertEqual(
            files[0]["preview_url"], "https://cdn.example/cube_thumb_medium.jpg"
        )
        self.assertEqual(files[0]["size"], 4096)
        self.assertEqual(files[1]["preview_url"], "https://cdn.example/base-large.jpg")

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
    def test_supported_catalogs_are_registered_and_removed_catalogs_are_absent(self):
        requested = {
            "thingiverse",
            "cults3d",
            "yeggi",
            "myminifactory",
            "thangs",
            "stlfinder",
            "makeronline",
            "crealitycloud",
            "nexprint",
            "grabcad",
            "smithsonian",
            "nasa",
            "nih3d",
            "youmagine",
            "pinshape",
        }
        self.assertTrue(requested.issubset(mod._PLATFORMS))
        display = {
            "Thingiverse",
            "Cults3D",
            "Yeggi",
            "MyMiniFactory",
            "Thangs",
            "STLFinder",
            "Makeronline",
            "Creality Cloud",
            "Nexprint",
            "GrabCAD",
            "Smithsonian 3D",
            "NASA 3D Resources",
            "NIH 3D",
            "YouMagine",
            "Pinshape",
        }
        self.assertTrue(display.issubset(mod._PLATFORMS_BY_DISPLAY))
        removed_keys = {
            "sketchfab",
            "cgtrader",
            "free3d",
            "3dfindit",
            "3dexport",
            "wikimedia",
            "zortrax",
            "qidimaker",
        }
        removed_names = {
            "Sketchfab",
            "CGTrader",
            "Free3D",
            "3DfindIT",
            "3DExport",
            "Wikimedia Commons",
            "Zortrax Library",
            "QIDI Maker",
        }
        self.assertTrue(removed_keys.isdisjoint(mod._PLATFORMS))
        self.assertTrue(removed_names.isdisjoint(mod._PLATFORMS_BY_DISPLAY))
        for key in removed_keys:
            self.assertNotIn(f'data-platform="{key}"', mod.PAGE)

    def test_platform_registry_is_consistent(self):
        self.assertEqual(len(mod._PLATFORM_SPECS), len(mod._PLATFORMS))
        self.assertEqual(len(mod._PLATFORM_SPECS), len(mod._PLATFORMS_BY_DISPLAY))
        for spec in mod._PLATFORM_SPECS:
            self.assertIs(mod._PLATFORMS[spec.key], spec)
            self.assertIs(mod._PLATFORMS_BY_DISPLAY[spec.display], spec)
            self.assertTrue(callable(spec.adapter.search))
            self.assertTrue(callable(spec.adapter.get_files))
            self.assertFalse(hasattr(spec.adapter, "enabled"))
        self.assertEqual(
            {spec.key for spec in mod._PLATFORM_SPECS if spec.profile_picker},
            {"makerworld", "nexprint", "crealitycloud"},
        )
        self.assertEqual(
            {spec.key for spec in mod._PLATFORM_SPECS if spec.session_recheck},
            {"cults3d", "grabcad"},
        )

    def test_only_authenticated_sites_have_auth_controls(self):
        for platform in (
            "yeggi",
            "stlfinder",
            "smithsonian",
            "nasa",
            "nih3d",
            "youmagine",
            "pinshape",
        ):
            self.assertNotIn(f'id="auth-{platform}"', mod.PAGE)
        for platform in (
            "cults3d",
            "grabcad",
            "thingiverse",
            "myminifactory",
            "crealitycloud",
            "thangs",
        ):
            self.assertIn(f'id="auth-{platform}"', mod.PAGE)

    def test_every_authenticated_card_has_instruction_tooltip(self):
        import re

        expected = {spec.key for spec in mod._PLATFORM_SPECS if spec.requires_auth}
        actual = set(
            re.findall(r'class="auth-help" data-platform="([^"]+)"', mod.PAGE)
        )
        self.assertEqual(actual, expected)
        self.assertIn('id="auth-tooltip"', mod.PAGE)
        self.assertIn('role="tooltip"', mod.PAGE)
        self.assertIn("function showAuthHelp(button)", mod.PAGE)
        for platform in expected:
            self.assertIn(f"{platform}:", mod.PAGE)

    def test_makeronline_login_uses_current_anycubic_oauth_endpoint(self):
        self.assertEqual(
            mod._PLATFORMS["makeronline"].login_url,
            "https://cas.anycubic.com/login/oauth/authorize"
            "?client_id=69ce24b6eaf78e597ac0&response_type=code"
            "&redirect_uri=https%3A%2F%2Fwww.makeronline.com%2Fen%2F"
            "&scope=read&state=ac_maker_online&lang=en",
        )
        self.assertIn("copy the mo_access_token cookie value", mod.PAGE)
        self.assertIn("MakerOnline mo_access_token value or Cookie header", mod.PAGE)

    def test_creality_login_uses_official_identity_connect_endpoint(self):
        login_url = mod._PLATFORMS["crealitycloud"].login_url
        self.assertTrue(login_url.startswith("https://id.creality.com/connect?"))
        self.assertIn("client_id=f9c302ecc29c59a0a6e921ff39a073ca", login_url)
        self.assertIn("redirect_uri=https%3A%2F%2Fwww.crealitycloud.com%2F", login_url)
        self.assertIn("model_token cookie value", mod.PAGE)

    def test_every_available_searcher_has_portal_checkbox(self):
        import re

        portals = set(
            re.findall(r'class="portal-search"[^>]*data-platform="([^"]+)"', mod.PAGE)
        )
        self.assertEqual(portals, set(mod._PLATFORMS))

    def test_ui_platform_key_map_is_generated_from_the_registry(self):
        import json
        import re

        match = re.search(
            r"function platformKey\(display\)\{return (\{.*?\})\[display\]",
            mod.PAGE,
        )
        self.assertIsNotNone(match)
        self.assertEqual(
            json.loads(match.group(1)),
            {spec.display: spec.key for spec in mod._PLATFORM_SPECS},
        )

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

    def test_search_results_have_client_side_pagination(self):
        self.assertIn('id="pagination"', mod.PAGE)
        self.assertIn('id="page-size"', mod.PAGE)
        self.assertIn('<option value="100" selected>100</option>', mod.PAGE)
        for size in (150, 200, 250, 300):
            self.assertIn(f'<option value="{size}">{size}</option>', mod.PAGE)
        self.assertIn("currentPage=1, pageSize=100", mod.PAGE)
        self.assertIn("[100,150,200,250,300].indexOf(value)", mod.PAGE)
        self.assertIn('id="page-prev"', mod.PAGE)
        self.assertIn('id="page-next"', mod.PAGE)
        self.assertIn("function paginationItems(page,total)", mod.PAGE)
        self.assertIn("window._results.slice(start,end)", mod.PAGE)
        self.assertIn("index=start+i", mod.PAGE)
        self.assertIn("renderResults(window._results,false)", mod.PAGE)

    def test_search_results_support_server_side_pagination(self):
        self.assertIn('id="source-results"', mod.PAGE)
        self.assertIn('id="load-more"', mod.PAGE)
        self.assertIn("function loadMoreResults()", mod.PAGE)
        self.assertIn("orca.postMessage({action:'search_more'})", mod.PAGE)
        self.assertIn("function renderSourceResults(sources,more)", mod.PAGE)
        self.assertEqual(
            {
                key
                for key, spec in mod._PLATFORMS.items()
                if spec.paginated_search
            },
            {
                "printables",
                "nexprint",
                "makeronline",
                "makerworld",
                "thingiverse",
                "myminifactory",
                "thangs",
                "stlfinder",
                "crealitycloud",
                "smithsonian",
                "nih3d",
            },
        )

    def test_source_errors_can_offer_a_safe_browser_fallback(self):
        self.assertIn("browserUrl=safeUrl(s.browser_url)", mod.PAGE)
        self.assertIn('class="secondary source-browser"', mod.PAGE)
        self.assertIn("e.target.closest('.source-browser')", mod.PAGE)
        self.assertIn("openExternal(button.dataset.url)", mod.PAGE)

    def test_thingiverse_background_details_update_cards_silently(self):
        self.assertIn("function applyModelDetails(m,silent)", mod.PAGE)
        self.assertIn("applyModelDetails(msg.model||{},!!msg.background)", mod.PAGE)
        self.assertIn("m._details_error&&!silent", mod.PAGE)

    def test_search_page_helpers_clamp_and_deduplicate(self):
        self.assertEqual(mod._search_page_number({"page": 0}), 1)
        self.assertEqual(mod._search_page_number({"page": 1000}), 100)
        existing = [
            {
                "_platform_key": "printables",
                "_model_id": 1,
                "name": "First",
            }
        ]
        incoming = [
            {
                "_platform_key": "printables",
                "_model_id": 1,
                "name": "Duplicate",
            },
            {
                "_platform_key": "printables",
                "_model_id": 2,
                "name": "Second",
            },
        ]
        merged, added = mod._merge_unique_results(existing, incoming)
        self.assertEqual(added, 1)
        self.assertEqual([row["name"] for row in merged], ["First", "Second"])

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

    def test_popularity_scores_sort_once_per_platform(self):
        rows = [
            {"platform": "A", "downloads": value}
            for value in range(1, 31)
        ] + [
            {"platform": "B", "downloads": value}
            for value in range(1, 31)
        ]
        real_sorted = sorted
        with mock.patch("builtins.sorted", wraps=real_sorted) as sorted_call:
            mod._add_popularity_scores(rows)
        self.assertEqual(sorted_call.call_count, 2)
        self.assertEqual(rows[0]["popularity"], 0)
        self.assertEqual(rows[29]["popularity"], 100)

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
        self.assertEqual(
            mod._MODEL_FILE_EXTS, mod._LOADABLE_MODEL_EXTS + (".zip",)
        )
        self.assertNotIn(".rar", mod._MODEL_FILE_EXTS)
        self.assertNotIn(".7z", mod._MODEL_FILE_EXTS)
        self.assertNotIn(".gcode", mod._MODEL_FILE_EXTS)

    def test_version_is_089(self):
        with PLUGIN_PATH.open(encoding="utf-8") as fh:
            head = fh.read(500)
        self.assertIn('# version = "0.8.9"', head)


if __name__ == "__main__":
    unittest.main()
