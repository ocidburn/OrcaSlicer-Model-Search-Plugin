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
    def test_smithsonian_returns_direct_zip(self):
        payload = {
            "rows": [
                {
                    "title": "Apollo Hatch",
                    "content": {
                        "uri": "https://3d-api.si.edu/content/model.zip",
                        "model_url": "3d_package:abc",
                    },
                }
            ]
        }
        with mock.patch("requests.get", return_value=Response(payload)):
            rows = mod.SmithsonianSearcher.search("apollo", None)
        self.assertEqual(rows[0]["platform"], "Smithsonian 3D")
        self.assertTrue(rows[0]["direct_import"])
        self.assertEqual(
            mod.SmithsonianSearcher.get_files(rows[0])[0]["name"], "model.zip"
        )

    def test_wikimedia_preserves_license_and_direct_stl(self):
        payload = {
            "query": {
                "pages": [
                    {
                        "title": "File:Cube.stl",
                        "imageinfo": [
                            {
                                "url": "https://upload.wikimedia.org/cube.stl",
                                "descriptionurl": "https://commons.wikimedia.org/wiki/File:Cube.stl",
                                "extmetadata": {
                                    "ObjectName": {"value": "Cube"},
                                    "Artist": {"value": "<b>Maker</b>"},
                                    "LicenseShortName": {"value": "CC0"},
                                    "LicenseUrl": {"value": "https://creativecommons.org/publicdomain/zero/1.0/"},
                                },
                            }
                        ],
                    }
                ]
            }
        }
        with mock.patch("requests.get", return_value=Response(payload)):
            rows = mod.WikimediaCommonsSearcher.search("cube", None)
        self.assertEqual(rows[0]["author"], "Maker")
        self.assertEqual(rows[0]["license"], "CC0")
        self.assertEqual(
            mod.WikimediaCommonsSearcher.get_files(rows[0])[0]["name"], "cube.stl"
        )

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

    def test_nih_parses_metrics_from_flight_response(self):
        payload = {
            "status": {"timems": 1},
            "hits": {
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
                ]
            }
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
            rows = mod.Nih3DSearcher.search("heart", None)
        self.assertEqual(rows[0]["downloads"], "9")
        self.assertEqual(rows[0]["views"], "20")
        self.assertEqual(rows[0]["license"], "CC BY")

    def test_browser_only_catalogs_are_not_direct_imports(self):
        for searcher in (mod.ThangsSearcher, mod.CgTraderSearcher):
            row = searcher.search("benchy", None)[0]
            self.assertEqual(row["result_type"], "search_link")
            self.assertFalse(row["direct_import"])


if __name__ == "__main__":
    unittest.main()
