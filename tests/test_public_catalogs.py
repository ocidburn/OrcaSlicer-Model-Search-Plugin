import json
import unittest
from unittest import mock

from tests._module_loader import load_plugin

mod = load_plugin("search_engine_public_catalogs")


class Response:
    def __init__(self, payload=None, text="", status_code=200):
        self.payload = payload
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class PublicCatalogTests(unittest.TestCase):
    def test_smithsonian_returns_direct_paginated_zip(self):
        payload = {
            "rowCount": 31,
            "rows": [
                {
                    "title": "Apollo Hatch",
                    "content": {
                        "uri": "https://3d-api.si.edu/content/model.zip",
                        "model_url": "3d_package:abc",
                    },
                }
            ],
        }
        with mock.patch("requests.get", return_value=Response(payload)) as request:
            rows = mod.SmithsonianSearcher.search("apollo", None, {"page": 1})
        self.assertEqual(rows.total, 31)
        self.assertTrue(rows.has_more)
        self.assertEqual(rows[0]["platform"], "Smithsonian 3D")
        self.assertTrue(rows[0]["direct_import"])
        self.assertEqual(
            mod.SmithsonianSearcher.get_files(rows[0])[0]["name"], "model.zip"
        )
        self.assertEqual(request.call_args.kwargs["params"]["start"], 0)

    def test_nasa_filters_repository_tree_to_printable_files(self):
        payload = {
            "tree": [
                {"type": "blob", "path": "3D Printing/Apollo/Apollo.stl"},
                {"type": "blob", "path": "3D Printing/Apollo/readme.txt"},
                {"type": "blob", "path": "3D Printing/Mars/Mars.stl"},
            ]
        }
        with mock.patch("requests.get", return_value=Response(payload)):
            rows = mod.NasaSearcher.search("apollo", None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Apollo")
        self.assertTrue(rows[0]["download_url"].endswith("Apollo.stl"))
        self.assertEqual(mod.NasaSearcher.get_files(rows[0])[0]["name"], "Apollo.stl")

    def test_nih_parses_metrics_and_pagination_from_flight_response(self):
        payload = {
            "status": {"timems": 1},
            "hits": {
                "found": 31,
                "hit": [
                    {
                        "fields": {
                            "id": ["123"],
                            "paddedentryid": ["000123"],
                            "title": ["Heart"],
                            "createdby": ["NIH"],
                            "license": ["CC-BY"],
                            "downloadcount": ["9"],
                            "viewcount": ["20"],
                        }
                    }
                ],
            },
        }
        session = mock.Mock()
        session.headers = {}
        session.post.return_value = Response(text="0:{}\n1:" + json.dumps(payload))
        with (
            mock.patch("requests.Session", return_value=session),
            mock.patch.object(
                mod.Nih3DSearcher,
                "_discover_search_action",
                return_value="action-id",
            ),
        ):
            rows = mod.Nih3DSearcher.search("heart", None, {"page": 1})
        self.assertEqual(rows.total, 31)
        self.assertTrue(rows.has_more)
        self.assertEqual(rows[0]["downloads"], "9")
        self.assertEqual(rows[0]["views"], "20")
        self.assertEqual(rows[0]["license"], "CC BY")

    def test_yeggi_is_explicit_browser_meta_search(self):
        row = mod.YeggiSearcher.search("benchy", None)[0]
        self.assertEqual(row["platform"], "Yeggi")
        self.assertEqual(row["result_type"], "search_link")
        self.assertFalse(row["direct_import"])
        self.assertIn("/q/benchy/", row["url"])

    def test_stlfinder_search_returns_indexed_models(self):
        html = '<a href="/model/benchy-bench/4444720?q=benchy">Benchy Bench</a>'
        with mock.patch.object(
            mod,
            "_fetch_html",
            return_value=(html, "https://www.stlfinder.com/3dmodels/benchy/"),
        ):
            rows = mod.StlFinderSearcher.search("benchy", None)
        self.assertEqual(rows[0]["platform"], "STLFinder")
        self.assertIn("/model/benchy-bench/4444720", rows[0]["url"])

    def test_stlfinder_delegates_download_to_registered_original_portal(self):
        index_url = "https://www.stlfinder.com/model/benchy-bench/4444720"
        source_url = "https://www.printables.com/model/123-benchy"
        html = f'<a href="{source_url}">View on Printables</a>'
        expected = [{"name": "benchy.stl", "url": "https://cdn.test/benchy.stl"}]
        with (
            mock.patch.object(mod, "_fetch_html", return_value=(html, index_url)),
            mock.patch.object(
                mod.PrintablesSearcher, "get_files", return_value=expected
            ) as get_files,
        ):
            files = mod.StlFinderSearcher.get_files({"url": index_url})
        self.assertEqual(files, expected)
        delegated = get_files.call_args.args[0]
        self.assertEqual(delegated["_platform_key"], "printables")
        self.assertEqual(delegated["url"], source_url)

    def test_stlfinder_does_not_delegate_to_an_excluded_portal(self):
        index_url = "https://www.stlfinder.com/model/render-model/123"
        html = '<a href="https://www.cgtrader.com/3d-models/example">View on CGTrader</a>'
        with (
            mock.patch.object(mod, "_fetch_html", return_value=(html, index_url)),
            self.assertRaises(mod.BrowserRequired) as raised,
        ):
            mod.StlFinderSearcher.get_files({"url": index_url})
        self.assertEqual(raised.exception.url, index_url)

    def test_pinshape_imports_public_stl_urls_from_model_page(self):
        model = {"url": "https://pinshape.com/items/4786-benchy"}
        html = '<a href="https://pinshape.com/stl/benchy.stl">View 3D</a>'
        expected = {
            "url": "https://pinshape.com/stl/benchy.stl",
            "name": "benchy.stl",
        }
        with (
            mock.patch.object(mod, "_fetch_html", return_value=(html, model["url"])),
            mock.patch.object(mod, "_probe_download", return_value=expected),
        ):
            files = mod.PinshapeSearcher.get_files(model)
        self.assertEqual(files, [expected])

    def test_pinshape_marks_free_search_cards_as_directly_importable(self):
        html = '<a href="/items/4786-benchy">Free</a>'
        with mock.patch.object(
            mod,
            "_fetch_html",
            return_value=(html, "https://pinshape.com/items?search=benchy"),
        ):
            row = mod.PinshapeSearcher.search("benchy", None)[0]
        self.assertTrue(row["is_free"])
        self.assertTrue(row["direct_import"])


if __name__ == "__main__":
    unittest.main()
