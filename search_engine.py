# /// script
# requires-python = ">=3.12"
# dependencies = ["requests"]
#
# [tool.orcaslicer.plugin]
# name = "3D Model Search Engine"
# description = "Search and download 3D models from Printables, Thingiverse, MakerWorld, and GrabCAD directly within OrcaSlicer. Each user authenticates with their own API tokens. License metadata is always displayed before download."
# author = "Tommaso Bianchi"
# version = "0.1.0"
# ///
try:
    import orca
except ImportError:
    orca = None
import json
import threading
import os
import time
import urllib.parse

LICENSE_DESCRIPTIONS = {
    "CC BY": "Share and adapt for any purpose. Must credit the author.",
    "CC BY-SA": "Share and adapt, credit author, same license for remixes.",
    "CC BY-NC": "Share and adapt, non-commercial only. Must credit.",
    "CC BY-NC-SA": "Non-commercial, credit required, share-alike.",
    "CC BY-ND": "Share but do NOT modify. Credit required.",
    "CC BY-NC-ND": "Share only, no mods, no commercial use. Credit required.",
    "CC0": "Public domain. No rights reserved. Free for any use.",
    "PD": "Public domain. No restrictions.",
    "All Rights Reserved": "Personal use only. No redistribution or modification.",
    "Standard Digital File License": "MakerWorld standard license. Personal use, no redistribution.",
    "Exclusive": "MakerWorld exclusive. Check platform terms.",
}


def _parse_license(name, url=""):
    name = (name or "").strip()
    summary = LICENSE_DESCRIPTIONS.get(name, "")
    if not summary:
        if "CC" in name.upper():
            summary = "Creative Commons license. See full text for terms."
        elif "GPL" in name.upper():
            summary = "GNU General Public License. Share modifications."
    return {"name": name, "url": url, "summary": summary}


def _first_image(images):
    if not images:
        return ""
    if isinstance(images, list) and images:
        img = images[0]
        if isinstance(img, dict):
            return img.get("filePath") or img.get("url") or ""
        return str(img)
    if isinstance(images, dict):
        return images.get("filePath") or images.get("url") or ""
    return str(images)


# ---------------------------------------------------------------------------
# Search adapters
# ---------------------------------------------------------------------------

MAKERONLINE_LICENSES = {
    1: ("CC BY", "https://creativecommons.org/licenses/by/4.0/"),
    2: ("CC BY-SA", "https://creativecommons.org/licenses/by-sa/4.0/"),
    3: ("CC BY-NC", "https://creativecommons.org/licenses/by-nc/4.0/"),
    4: ("CC BY-NC-SA", "https://creativecommons.org/licenses/by-nc-sa/4.0/"),
    5: ("CC BY-ND", "https://creativecommons.org/licenses/by-nd/4.0/"),
    6: ("CC BY-NC-ND", "https://creativecommons.org/licenses/by-nc-nd/4.0/"),
    7: ("CC0", "https://creativecommons.org/publicdomain/zero/1.0/"),
    8: ("Standard Digital File License", ""),
}

MAKERONLINE_BASE = "https://www.makeronline.com"


class MakeronlineSearcher:
    SEARCH_URL = f"{MAKERONLINE_BASE}/api/search/model"

    @staticmethod
    def enabled(tokens):
        return True

    @staticmethod
    def search(query, tokens):
        import requests

        r = requests.post(
            MakeronlineSearcher.SEARCH_URL,
            json={
                "keyword": query,
                "page": 1,
                "page_size": 30,
                "order": "",
                "search": 1,
                "weight": 1,
                "print_type": 0,
                "category_id": "",
                "color": "",
                "license": "",
                "award": "",
                "exclusive": "",
                "is_free": "",
            },
            headers={"Content-Type": "application/json", "User-Agent": "OrcaSlicer/2.0"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            return []
        results = []
        for item in data.get("data", {}).get("data") or []:
            lic_name, lic_url = MAKERONLINE_LICENSES.get(
                item.get("license", 0), ("Unknown", "")
            )
            lic_data = _parse_license(lic_name, lic_url)
            thumb = (item.get("mold_image") or "").replace(
                "thumbnail", "400x300"
            )
            results.append(
                {
                    "name": item.get("title", "Untitled"),
                    "author": item.get("show_user_name") or item.get("user_name", "Unknown"),
                    "platform": "Makeronline",
                    "thumbnail_url": thumb,
                    "license": lic_data["name"],
                    "license_url": lic_data["url"],
                    "license_summary": lic_data["summary"],
                    "download_url": item.get("target_url", ""),
                    "url": item.get("target_url", ""),
                    "_mold_id": item.get("mold_id"),
                }
            )
        return results

    @staticmethod
    def get_detail(mold_id):
        import requests

        r = requests.get(
            f"{MAKERONLINE_BASE}/api/mold/detail",
            params={"id": mold_id},
            headers={"User-Agent": "OrcaSlicer/2.0"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            return None
        detail = data["data"]
        files = detail.get("files") or []
        file_url = files[0].get("url") if files else ""
        return {
            "file_url": file_url,
            "file_name": files[0].get("file_name", "") if files else "",
            "images": [img["url"] for img in (detail.get("images") or [])],
        }


NEXPRINT_LICENSES = {
    0: ("CC0", "https://creativecommons.org/publicdomain/zero/1.0/"),
    1: ("CC BY", "https://creativecommons.org/licenses/by/4.0/"),
    2: ("CC BY-SA", "https://creativecommons.org/licenses/by-sa/4.0/"),
    3: ("CC BY-NC", "https://creativecommons.org/licenses/by-nc/4.0/"),
    4: ("CC BY-NC-SA", "https://creativecommons.org/licenses/by-nc-sa/4.0/"),
    5: ("CC BY-ND", "https://creativecommons.org/licenses/by-nd/4.0/"),
    6: ("CC BY-NC-ND", "https://creativecommons.org/licenses/by-nc-nd/4.0/"),
    7: ("All Rights Reserved", ""),
    8: ("Standard Digital File License", ""),
}

NEXPRINT_BASE = "https://www.nexprint.com"


class NexprintSearcher:
    SEARCH_URL = f"{NEXPRINT_BASE}/gateway/api/v1/model-library-server/model-base-info/search"

    @staticmethod
    def enabled(tokens):
        return True

    @staticmethod
    def search(query, tokens):
        import requests

        r = requests.get(
            NexprintSearcher.SEARCH_URL,
            params={"keyword": query, "pageNo": "1", "pageSize": "30"},
            headers={"User-Agent": "OrcaSlicer/2.0"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            return []
        results = []
        for item in (data.get("data", {}).get("pageResult", {}).get("list") or []):
            lic_id = item.get("licenseType", 0)
            lic_name, lic_url = NEXPRINT_LICENSES.get(lic_id, ("Unknown", ""))
            lic_data = _parse_license(lic_name, lic_url)
            results.append(
                {
                    "name": item.get("modelName", "Untitled"),
                    "author": item.get("authorName") or (
                        (item.get("author") or {}).get("nickname", "Unknown")
                    ),
                    "platform": "Nexprint",
                    "thumbnail_url": item.get("coverImgUrl", ""),
                    "license": lic_data["name"],
                    "license_url": lic_data["url"],
                    "license_summary": lic_data["summary"],
                    "download_url": f"{NEXPRINT_BASE}/models/{item.get('modelId', '')}",
                    "url": f"{NEXPRINT_BASE}/models/{item.get('modelId', '')}",
                    "_model_id": item.get("modelId"),
                }
            )
        return results

    @staticmethod
    def get_detail(model_id):
        import requests

        r = requests.get(
            f"{NEXPRINT_BASE}/gateway/api/v1/model-library-server/model-base-info/get",
            params={"id": model_id},
            headers={"User-Agent": "OrcaSlicer/2.0"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            return None
        detail = data["data"]
        files = detail.get("modelFileInfoList") or []
        file_url = files[0].get("fileUrl") if files else ""
        return {
            "file_url": file_url,
            "file_name": files[0].get("fileName", "") if files else "",
            "images": [p.get("fileUrl") for p in (detail.get("modelPicList") or [])],
        }


class PrintablesSearcher:
    SEARCH_URL = "https://www.printables.com/search/models"

    @staticmethod
    def enabled(tokens):
        return True  # ponytail: search is public HTML, no token needed; downloads may need auth

    @staticmethod
    def search(query, tokens):
        import requests

        r = requests.get(
            PrintablesSearcher.SEARCH_URL,
            params={"q": query},
            headers={"User-Agent": "OrcaSlicer/2.0"},
            timeout=30,
        )
        r.raise_for_status()
        html = r.text

        import re

        scripts = re.findall(
            r'<script[^>]*type="application/(?:ld\+)?json"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )

        items = []
        for s in scripts:
            try:
                d = json.loads(s)
                body = json.loads(d["body"])
                result = body.get("data", {}).get("result", {})
                candidate = result.get("items", [])
            except Exception:
                continue
            if candidate and len(candidate) > 5:
                items = candidate
                break

        results = []
        for item in items:
            img = item.get("image") or {}
            file_path = img.get("filePath", "")
            thumb = f"https://media.printables.com/{file_path}" if file_path else ""
            lic = item.get("license") or {}
            lic_data = _parse_license(
                lic.get("name", "") if isinstance(lic, dict) else "",
                lic.get("url", "") if isinstance(lic, dict) else "",
            )
            if not lic_data["name"]:
                lic_data = {"name": "Unknown", "url": "", "summary": "Check on Printables.com for license details."}
            user = item.get("user") or {}
            results.append(
                {
                    "name": item.get("name", "Untitled"),
                    "author": user.get("publicUsername") or user.get("handle", "Unknown"),
                    "platform": "Printables",
                    "thumbnail_url": thumb,
                    "license": lic_data["name"],
                    "license_url": lic_data["url"],
                    "license_summary": lic_data["summary"],
                    "download_url": f"https://www.printables.com/model/{item.get('id','')}-{item.get('slug','')}",
                    "url": f"https://www.printables.com/model/{item.get('id','')}-{item.get('slug','')}",
                }
            )
        return results


class ThingiverseSearcher:
    BASE = "https://api.thingiverse.com"

    @staticmethod
    def enabled(tokens):
        return bool(tokens.get("thingiverse_token"))

    @staticmethod
    def search(query, tokens):
        import requests
        url = f"{ThingiverseSearcher.BASE}/search/{urllib.parse.quote(query)}"
        params = {"type": "things", "per_page": 20, "access_token": tokens["thingiverse_token"]}
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        results = []
        for hit in (data.get("hits") or []):
            lic = _parse_license(hit.get("license", ""))
            results.append({
                "name": hit.get("name", "Untitled"),
                "author": (hit.get("creator") or {}).get("name", "Unknown"),
                "platform": "Thingiverse",
                "thumbnail_url": hit.get("thumbnail", ""),
                "license": lic["name"] or "Unknown",
                "license_url": lic["url"],
                "license_summary": lic["summary"],
                "download_url": (hit.get("public_url") or "") + "/zip",
                "url": hit.get("public_url", ""),
            })
        return results


class MakerWorldSearcher:
    SEARCH_URL = "https://api.bambulab.com/v1/search-service/select/design2"
    BASE = "https://makerworld.com"

    @staticmethod
    def enabled(tokens):
        return True

    LICENSES = {
        "CC0": ("CC0", "https://creativecommons.org/publicdomain/zero/1.0/"),
        "BY": ("CC BY", "https://creativecommons.org/licenses/by/4.0/"),
        "BY-SA": ("CC BY-SA", "https://creativecommons.org/licenses/by-sa/4.0/"),
        "BY-NC": ("CC BY-NC", "https://creativecommons.org/licenses/by-nc/4.0/"),
        "BY-NC-SA": ("CC BY-NC-SA", "https://creativecommons.org/licenses/by-nc-sa/4.0/"),
        "BY-ND": ("CC BY-ND", "https://creativecommons.org/licenses/by-nd/4.0/"),
        "BY-NC-ND": ("CC BY-NC-ND", "https://creativecommons.org/licenses/by-nc-nd/4.0/"),
        "Standard Digital File License": ("Standard Digital File License", ""),
        "MakerWorld Exclusive License": ("MakerWorld Exclusive License", ""),
    }

    @staticmethod
    def search(query, tokens):
        import requests

        r = requests.get(
            MakerWorldSearcher.SEARCH_URL,
            params={"keyword": query, "limit": "30"},
            headers={"User-Agent": "OrcaSlicer/2.0"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        results = []
        for hit in data.get("hits") or []:
            lic_str = hit.get("license", "Unknown")
            lic_name, lic_url = MakerWorldSearcher.LICENSES.get(
                lic_str, (lic_str, "")
            )
            lic_data = _parse_license(lic_name, lic_url)
            creator = hit.get("designCreator") or {}
            results.append(
                {
                    "name": hit.get("title", "Untitled"),
                    "author": creator.get("name", "Unknown"),
                    "platform": "MakerWorld",
                    "thumbnail_url": hit.get("cover", ""),
                    "license": lic_data["name"],
                    "license_url": lic_data["url"],
                    "license_summary": lic_data["summary"],
                    "download_url": f"{MakerWorldSearcher.BASE}/en/models/{hit.get('id', '')}",
                    "url": f"{MakerWorldSearcher.BASE}/en/models/{hit.get('id', '')}",
                    "_model_id": hit.get("id"),
                }
            )
        return results

    @staticmethod
    def get_detail(model_id):
        import requests

        r = requests.get(
            f"https://api.bambulab.com/v1/design-service/design/{model_id}",
            headers={"User-Agent": "OrcaSlicer/2.0"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        ext = data.get("designExtension") or {}
        files = ext.get("model_files") or []
        pics = ext.get("design_pictures") or []
        return {
            "file_url": files[0].get("modelUrl") if files else "",
            "file_name": files[0].get("modelName", "") if files else "",
            "file_size": files[0].get("modelSize", 0) if files else 0,
            "file_type": files[0].get("modelType", "") if files else "",
            "images": [p.get("url", "") for p in pics],
        }


class GrabcadSearcher:
    BASE = "https://api.grabcad.com/api/v1"

    @staticmethod
    def enabled(tokens):
        return False  # ponytail: GrabCAD retired all public APIs (v1/v2 both 404 as of 2026-08-09)

    @staticmethod
    def search(query, tokens):
        import requests
        url = f"{GrabcadSearcher.BASE}/search"
        params = {"query": query}
        headers = {}
        token = tokens.get("grabcad_token", "")
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return []
        results = []
        for item in (data.get("results") or data.get("items") or []):
            proj = item.get("project") or item.get("model") or {}
            thumb = proj.get("thumbnail") or proj.get("image", {}).get("url", "") or ""
            lic = _parse_license(proj.get("license", ""))
            results.append({
                "name": proj.get("name") or item.get("name", "Untitled"),
                "author": (proj.get("user") or {}).get("name") or (proj.get("author") or {}).get("name", "Unknown"),
                "platform": "GrabCAD",
                "thumbnail_url": thumb,
                "license": lic["name"] or "Unknown",
                "license_url": lic["url"],
                "license_summary": lic["summary"],
                "download_url": proj.get("downloadUrl") or f"https://grabcad.com/library/{proj.get('slug','')}",
                "url": f"https://grabcad.com/library/{proj.get('slug','')}",
            })
        return results


_SEARCHERS = {
    "printables": PrintablesSearcher,
    "nexprint": NexprintSearcher,
    "makeronline": MakeronlineSearcher,
    "thingiverse": ThingiverseSearcher,
    "makerworld": MakerWorldSearcher,
    "grabcad": GrabcadSearcher,
}


# ---------------------------------------------------------------------------
# Plugin capability (only available inside OrcaSlicer)
# ---------------------------------------------------------------------------

if orca is not None:

    class SearchEngineScript(orca.script.ScriptPluginCapabilityBase):
        def get_name(self):
            return "3D Model Search"

        def get_default_config(self):
            return {
                "first_run": True,
                "printables_token": "",
                "thingiverse_token": "",
                "makerworld_token": "",
                "grabcad_token": "",
                "license_filter": "all",
            }

        def has_config_ui(self):
            return True

        def get_config_ui(self):
            return _TOKEN_CONFIG_HTML

        def on_load(self):
            self._win = None

        def execute(self):
            config = json.loads(self.get_config())

            if config.get("first_run", True):
                accepted = orca.host.ui.message(
                    _DISCLAIMER_TEXT,
                    title="3D Model Search \u2014 Disclaimer",
                    buttons="ok_cancel",
                    icon="warning",
                )
                if accepted != "ok":
                    return orca.ExecutionResult.skipped("User declined disclaimer.")
                config["first_run"] = False
                self.save_config(json.dumps(config))

            self._win = orca.host.ui.create_window(
                html=_SEARCH_PAGE_HTML,
                title="3D Model Search",
                on_message=self._on_message,
                on_close=self._on_close,
            )
            return orca.ExecutionResult.success()

        def _on_message(self, msg):
            action = msg.get("action", "")
            if action == "search":
                threading.Thread(target=self._do_search, args=(msg,), daemon=True).start()
            elif action == "download":
                threading.Thread(target=self._do_download, args=(msg,), daemon=True).start()

        def _on_close(self):
            self._win = None

        def _do_search(self, msg):
            query = msg.get("query", "")
            platforms = msg.get("platforms", [])
            results = []
            tokens = json.loads(self.get_config())
            for platform in platforms:
                adapter = _SEARCHERS.get(platform)
                if not adapter:
                    continue
                if not adapter.enabled(tokens):
                    self._win.post({"action": "error", "message": f"{platform}: API token not configured. Set it in plugin settings."})
                    continue
                try:
                    results.extend(adapter.search(query, tokens))
                except Exception as e:
                    self._win.post({"action": "error", "message": f"{platform}: {e}"})
            self._win.post({"action": "results", "results": results})

        def _do_download(self, msg):
            model = msg.get("model", {})
            url = model.get("download_url", "")
            name = model.get("name", "model")
            if not url:
                self._win.post({"action": "error", "message": "No download URL available."})
                return
            try:
                import requests
                dl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
                os.makedirs(dl_dir, exist_ok=True)
                ext = ".stl"
                ulow = url.lower()
                if ".3mf" in ulow:
                    ext = ".3mf"
                elif ".obj" in ulow:
                    ext = ".obj"
                elif ".step" in ulow or ".stp" in ulow:
                    ext = ".step"
                safe_name = "".join(c if c.isalnum() or c in "._-() " else "_" for c in name)
                path = os.path.join(dl_dir, f"{safe_name}{ext}")
                headers = {}
                tokens = json.loads(self.get_config())
                platform = model.get("platform", "").lower()
                if platform == "printables" and tokens.get("printables_token"):
                    headers["Authorization"] = "Bearer " + tokens["printables_token"]
                r = requests.get(url, timeout=120, stream=True, headers=headers)
                r.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                self._win.post({"action": "download_done", "path": path, "name": name})
            except Exception as e:
                self._win.post({"action": "error", "message": f"Download failed: {e}"})


    @orca.plugin
    class SearchEnginePlugin(orca.base):
        def register_capabilities(self):
            orca.register_capability(SearchEngineScript)


# ---------------------------------------------------------------------------
# HTML templates
# ---------------------------------------------------------------------------

_DISCLAIMER_TEXT = """\
IMPORTANT \u2014 Please read before using this plugin.

This plugin searches for 3D models on external websites (Printables,
Thingiverse, MakerWorld, GrabCAD). Each model has a LICENSE that governs
how you may use it. You are responsible for complying with each license.

This plugin:
  - Does NOT host, cache, or redistribute any 3D model files.
  - Does NOT collect or transmit your personal data.
  - Requires your own API tokens for each platform you want to search.

By clicking OK, you acknowledge that:
  1. You will check each model\u2019s license before downloading.
  2. You will comply with each platform\u2019s Terms of Service.
  3. You assume full legal responsibility for downloaded models.

Click Cancel to disable the plugin."""


_TOKEN_CONFIG_HTML = """\
<h2>API Tokens</h2>
<p style="color:var(--orca-muted);margin-bottom:16px">
  Each platform requires a personal API token. Tokens are stored locally
  and never shared.
</p>

<h3>Printables (Prusa Research)</h3>
<p style="color:var(--orca-muted);font-size:0.9em">
  Search is public. Token only needed for authenticated downloads.<br>
</p>
<input id="printables_token" type="password" style="width:100%;padding:6px;margin-bottom:12px"
  placeholder="Paste your Printables API token here" />

<h3>Thingiverse (UltiMaker)</h3>
<p style="color:var(--orca-muted);font-size:0.9em">
  Get your token: Account Settings > App Tokens > Generate<br>
</p>
<input id="thingiverse_token" type="password" style="width:100%;padding:6px;margin-bottom:12px"
  placeholder="Paste your Thingiverse app token here" />

<h3>MakerWorld (Bambu Lab)</h3>
<p style="color:var(--orca-warning,#f90);font-size:0.9em">
  MakerWorld public API is currently unavailable. Disabled until API is restored.<br>
</p>
<input id="makerworld_token" type="password" style="width:100%;padding:6px;margin-bottom:12px"
  placeholder="Paste your MakerWorld session token here (optional)" />

<h3>GrabCAD (Stratasys)</h3>
<p style="color:var(--orca-warning,#f90);font-size:0.9em">
  GrabCAD has retired its public API. Disabled until a new API is available.<br>
</p>
<input id="grabcad_token" type="password" style="width:100%;padding:6px;margin-bottom:12px"
  placeholder="Paste your GrabCAD API token here (optional)" />

<button onclick="save()" style="margin-top:12px;padding:8px 24px;border:none;border-radius:6px;
  background:var(--orca-accent,#4a9eff);color:var(--orca-accent-fg,#fff);cursor:pointer">
  Save Tokens</button>
<span id="saved" style="display:none;color:#4a4;margin-left:12px">&check; Saved</span>

<script>
  var c = window.orca.getConfig() || {};
  document.getElementById('printables_token').value = c.printables_token || '';
  document.getElementById('thingiverse_token').value = c.thingiverse_token || '';
  document.getElementById('makerworld_token').value = c.makerworld_token || '';
  document.getElementById('grabcad_token').value = c.grabcad_token || '';
  function save() {
    window.orca.saveConfig({
      first_run: c.first_run !== undefined ? c.first_run : false,
      printables_token: document.getElementById('printables_token').value.trim(),
      thingiverse_token: document.getElementById('thingiverse_token').value.trim(),
      makerworld_token: document.getElementById('makerworld_token').value.trim(),
      grabcad_token: document.getElementById('grabcad_token').value.trim(),
      license_filter: c.license_filter || 'all'
    });
    var el = document.getElementById('saved');
    el.style.display = 'inline';
    setTimeout(function(){ el.style.display = 'none'; }, 2000);
  }
</script>"""


_SEARCH_PAGE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>3D Model Search</title></head>
<body>
<style>
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:var(--orca-font,sans-serif); background:var(--orca-bg,#1e1e1e); color:var(--orca-fg,#eee); padding:16px; }
  h1 { margin-bottom:12px; font-size:1.2em; }
  input,select,button { font-family:inherit; }
  .search-row { display:flex; gap:8px; margin-bottom:12px; }
  .search-row input { flex:1; padding:8px 12px; border:1px solid var(--orca-border,#444); border-radius:6px; background:var(--orca-bg,#1e1e1e); color:var(--orca-fg,#eee); font-size:0.95em; }
  .search-row button { padding:8px 20px; border:none; border-radius:6px; background:var(--orca-accent,#4a9eff); color:var(--orca-accent-fg,#fff); cursor:pointer; font-size:0.95em; }
  .search-row button:hover { opacity:0.9; }
  .platforms { display:flex; gap:16px; margin-bottom:12px; flex-wrap:wrap; }
  .platforms label { display:flex; align-items:center; gap:6px; color:var(--orca-muted,#aaa); font-size:0.85em; cursor:pointer; }
  #results { display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:10px; }
  .card { border:1px solid var(--orca-border,#444); border-radius:8px; padding:10px; cursor:pointer; transition:border-color .15s; }
  .card:hover { border-color:var(--orca-accent,#4a9eff); }
  .card img { width:100%; height:110px; object-fit:cover; border-radius:4px; margin-bottom:6px; background:var(--orca-border,#333); }
  .card h3 { font-size:0.9em; margin-bottom:2px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .card .author { font-size:0.78em; color:var(--orca-muted,#888); }
  .license-badge { display:inline-block; padding:1px 7px; border-radius:3px; font-size:0.72em; margin-top:4px; }
  .license-cc  { background:#1a5c2a; color:#6f6; }
  .license-arr { background:#5c3a1a; color:#fa3; }
  .license-unk { background:#444; color:#aaa; }
  .detail-panel { margin-top:16px; padding:14px; border:1px solid var(--orca-border,#444); border-radius:8px; display:none; }
  .detail-panel.active { display:block; }
  .detail-panel h2 { margin-bottom:6px; font-size:1.1em; }
  .detail-panel p { margin-bottom:6px; color:var(--orca-muted,#aaa); font-size:0.88em; }
  .detail-panel a { color:var(--orca-accent,#4a9eff); }
  .detail-panel button { margin-top:10px; padding:8px 20px; border:none; border-radius:6px; background:var(--orca-accent,#4a9eff); color:var(--orca-accent-fg,#fff); cursor:pointer; font-size:0.95em; }
  .detail-panel button:hover { opacity:0.9; }
  .detail-panel button:disabled { opacity:0.35; cursor:not-allowed; }
  #status { margin-top:10px; color:var(--orca-muted,#888); font-size:0.8em; }
</style>
<h1>&#128269; 3D Model Search</h1>
<div class="search-row">
  <input id="query" type="text" placeholder="Search for 3D models..." />
  <button id="search-btn">Search</button>
</div>
<div class="platforms">
  <label><input type="checkbox" checked data-platform="nexprint"> Nexprint (Elegoo)</label>
  <label><input type="checkbox" checked data-platform="printables"> Printables</label>
  <label><input type="checkbox" checked data-platform="makeronline"> Makeronline (Anycubic)</label>
  <label><input type="checkbox" checked data-platform="makerworld"> MakerWorld (Bambu Lab)</label>
  <label style="opacity:0.5"><input type="checkbox" data-platform="thingiverse" disabled> Thingiverse</label>
  <label style="opacity:0.5"><input type="checkbox" data-platform="grabcad" disabled> GrabCAD</label>
</div>
<div id="results"></div>
<div id="detail" class="detail-panel"></div>
<div id="status">Ready. Configure API tokens in plugin settings, then search.</div>
<script>
  var selectedModel = null;
  document.getElementById('search-btn').onclick = function() {
    var q = document.getElementById('query').value.trim();
    if (!q) return;
    var btn = document.getElementById('search-btn');
    btn.disabled = true;
    btn.textContent = 'Searching...';
    var platforms = [];
    var cbs = document.querySelectorAll('.platforms input:checked');
    for (var i = 0; i < cbs.length; i++) platforms.push(cbs[i].dataset.platform);
    document.getElementById('status').textContent = 'Searching...';
    document.getElementById('results').innerHTML = '';
    document.getElementById('detail').classList.remove('active');
    orca.postMessage({action:'search', query:q, platforms:platforms});
  };
  document.getElementById('query').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') document.getElementById('search-btn').click();
  });
  function renderResults(models) {
    var container = document.getElementById('results');
    for (var i = 0; i < models.length; i++) {
      (function(m) {
        var card = document.createElement('div');
        card.className = 'card';
        var img = m.thumbnail_url ? '<img src="' + esc(m.thumbnail_url) + '" onerror="this.style.display=\\'none\\'">' : '';
        card.innerHTML = img
          + '<h3 title="' + esc(m.name) + '">' + esc(m.name) + '</h3>'
          + '<div class="author">' + esc(m.author) + ' &middot; ' + esc(m.platform) + '</div>'
          + '<span class="license-badge ' + licenseClass(m.license) + '">' + esc(m.license || 'Unknown') + '</span>';
        card.onclick = function() { showDetail(m); };
        container.appendChild(card);
      })(models[i]);
    }
  }
  function showDetail(model) {
    selectedModel = model;
    var licUrl = model.license_url ? ' <a href="' + esc(model.license_url) + '" target="_blank" rel="noopener">View license &rarr;</a>' : '';
    var modelUrl = model.url ? '<p><strong>Open on ' + esc(model.platform) + ':</strong> <a href="' + esc(model.url) + '" target="_blank" rel="noopener">' + esc(model.url) + '</a></p>' : '';
    document.getElementById('detail').innerHTML =
      '<h2>' + esc(model.name) + '</h2>'
      + '<p><strong>Author:</strong> ' + esc(model.author) + '</p>'
      + '<p><strong>Platform:</strong> ' + esc(model.platform) + '</p>'
      + modelUrl
      + '<p><strong>License:</strong> <span class="license-badge ' + licenseClass(model.license) + '">' + esc(model.license || 'Unknown') + '</span>' + licUrl + '</p>'
      + '<p><strong>Summary:</strong> ' + esc(model.license_summary || 'No license information available for this model.') + '</p>'
      + '<button id="dl-btn" disabled onclick="doDownload()">Download Model</button>'
      + '<label style="margin-left:10px;font-size:0.85em;color:var(--orca-muted,#aaa)">'
      + '<input type="checkbox" id="license-ack" onchange="document.getElementById(\'dl-btn\').disabled=!this.checked">'
      + ' I have read and understood the license terms</label>';
    document.getElementById('detail').classList.add('active');
  }
  function doDownload() {
    if (!selectedModel) return;
    orca.postMessage({action:'download', model:selectedModel});
  }
  function licenseClass(lic) {
    if (!lic) return 'license-unk';
    if (/CC|Creative Commons|CC0|Public Domain/i.test(lic)) return 'license-cc';
    if (/All Rights Reserved|ARR|Standard Digital File/i.test(lic)) return 'license-arr';
    return 'license-unk';
  }
  function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
  window.orca.onMessage = function(msg) {
    var btn = document.getElementById('search-btn');
    if (msg.action === 'results') {
      btn.disabled = false;
      btn.textContent = 'Search';
      if (msg.results.length === 0) {
        document.getElementById('status').textContent = 'No results found.';
      } else {
        document.getElementById('status').textContent = msg.results.length + ' result(s)';
        renderResults(msg.results);
      }
    } else if (msg.action === 'download_progress') {
      document.getElementById('status').textContent = msg.message;
    } else if (msg.action === 'download_done') {
      document.getElementById('status').textContent = 'Downloaded: ' + msg.name;
      document.getElementById('detail').innerHTML +=
        '<p style="color:#4a4;margin-top:8px">Saved to:<br><code>' + esc(msg.path) + '</code></p>'
        + '<p style="color:var(--orca-muted)">Open this file from the OrcaSlicer File menu.</p>';
    } else if (msg.action === 'error') {
      btn.disabled = false;
      btn.textContent = 'Search';
      document.getElementById('status').textContent = 'Error: ' + msg.message;
    }
  };
</script>
</body></html>"""
