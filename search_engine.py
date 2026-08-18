# /// script
# requires-python = ">=3.12"
# dependencies = ["requests"]
#
# [tool.orcaslicer.plugin]
# name = "3D Model Search Engine"
# description = "Search and import 3D models from MakerWorld, Nexprint, Makeronline, and Printables with per-portal authenticated sessions."
# author = "Tommaso Bianchi"
# version = "0.2.2"
# ///

try:
    import orca
except ImportError:
    orca = None

import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request


_BROWSER_UA = (
    "OrcaSlicer-Model-Search-Plugin/0.2.2 "
    "(+https://github.com/tommasobbianchi/OrcaSlicer-Model-Search-Plugin)"
)

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
    "Standard Digital File License": "Platform standard license. Check the model page for exact terms.",
    "MakerWorld Exclusive License": "MakerWorld exclusive license. Check the model page for exact terms.",
    "Exclusive": "Platform exclusive license. Check the model page for exact terms.",
}


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _orca_data_dir():
    """Return the Orca data directory when installed as a plugin.

    Orca's plugin audit hook allows writes under its data root.  When the file
    is executed directly (tests/dev), fall back to the source directory.
    """
    path = os.path.dirname(os.path.abspath(__file__))
    cursor = path
    while True:
        parent, leaf = os.path.split(cursor)
        if leaf == "orca_plugins":
            return parent
        if not parent or parent == cursor:
            return path
        cursor = parent


def _download_dir():
    return os.path.join(_orca_data_dir(), "model_downloads")


def _auth_file():
    return os.path.join(_orca_data_dir(), "model_search_auth", "sessions.json")


def _ensure_private_dir(path):
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _safe_filename(name, fallback="model.3mf"):
    name = os.path.basename(str(name or "")).strip()
    name = re.sub(r"[^\w.\- ()\[\]]", "_", name, flags=re.UNICODE).strip(" .")
    return name or fallback


def _unique_path(directory, name):
    path = os.path.join(directory, name)
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(name)
    i = 2
    while True:
        candidate = os.path.join(directory, f"{stem}_{i}{ext}")
        if not os.path.exists(candidate):
            return candidate
        i += 1


# ---------------------------------------------------------------------------
# License helpers
# ---------------------------------------------------------------------------


def _parse_license(name, url=""):
    name = (name or "").strip()
    summary = LICENSE_DESCRIPTIONS.get(name, "")
    if not summary:
        upper = name.upper()
        if "CC" in upper:
            summary = "Creative Commons license. See the model page for the complete terms."
        elif "GPL" in upper:
            summary = "GNU General Public License. See the model page for the complete terms."
        elif name:
            summary = "See the model page for the complete license terms."
    return {"name": name, "url": url or "", "summary": summary}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class AuthError(RuntimeError):
    pass


class AuthRequired(AuthError):
    pass


class VerificationRequired(AuthError):
    def __init__(self, message="Verification code required"):
        super().__init__(message)


class AuthStore:
    """Small token-only credential store.

    Passwords are never persisted.  The file lives below Orca's data directory
    and is written with restrictive permissions where the OS supports them.
    """

    def __init__(self, path=None):
        self.path = path or _auth_file()
        self._lock = threading.RLock()

    def load(self):
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                return data if isinstance(data, dict) else {}
            except (FileNotFoundError, ValueError, OSError):
                return {}

    def get(self, platform):
        value = self.load().get(platform, {})
        return value if isinstance(value, dict) else {}

    def set(self, platform, value):
        value = dict(value or {})
        # Defense in depth: never let caller accidentally persist a password.
        for forbidden in ("password", "passwd", "secret"):
            value.pop(forbidden, None)
        with self._lock:
            data = self.load()
            data[platform] = value
            folder = os.path.dirname(self.path)
            _ensure_private_dir(folder)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def delete(self, platform):
        with self._lock:
            data = self.load()
            data.pop(platform, None)
            folder = os.path.dirname(self.path)
            _ensure_private_dir(folder)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)


_PLATFORM_HOSTS = {
    "makerworld": ("api.bambulab.com", "makerworld.com"),
    "nexprint": ("nexprint.com",),
    "makeronline": ("makeronline.com", "anycubic.com"),
}

_PLATFORM_DISPLAY = {
    "makerworld": "MakerWorld",
    "nexprint": "Nexprint",
    "makeronline": "Makeronline",
    "printables": "Printables",
}


def _host_matches(host, suffixes):
    host = (host or "").lower().rstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def _url_host(url):
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _is_http_url(url):
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def _reject_obvious_local_target(url):
    """Reject localhost/private literal IPs. Hostnames are left to the OS resolver."""
    host = _url_host(url)
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        raise ValueError("Refusing a localhost download URL")
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved:
        raise ValueError("Refusing a private/local download URL")


class AuthManager:
    BAMBU_LOGIN = "https://api.bambulab.com/v1/user-service/user/login"
    BAMBU_PROFILE = "https://api.bambulab.com/v1/design-user-service/my/preference"

    def __init__(self, store=None):
        self.store = store or AuthStore()

    def credential(self, platform):
        return self.store.get(platform)

    def token(self, platform):
        data = self.credential(platform)
        return data.get("access_token") or data.get("auth_token") or data.get("token") or ""

    def authenticated(self, platform):
        return bool(self.token(platform))

    def status(self):
        out = {}
        for platform in ("makerworld", "nexprint", "makeronline"):
            data = self.credential(platform)
            configured = bool(data.get("access_token") or data.get("auth_token") or data.get("token"))
            out[platform] = {
                "authenticated": configured,
                "label": data.get("label") or data.get("email") or data.get("username") or ("Connected" if configured else "Not connected"),
                "expires_at": data.get("expires_at"),
            }
        return out

    def logout(self, platform):
        self.store.delete(platform)

    @staticmethod
    def normalize_token(platform, token):
        token = (token or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if platform == "nexprint" and "auth_token=" in token:
            # Accept either the cookie value or a copied Cookie header.
            m = re.search(r"(?:^|[;\s])auth_token=([^;\s]+)", token)
            if m:
                token = m.group(1).strip()
        if platform == "makeronline":
            m = re.match(r"(?i)^(?:XX-Token|Authorization)\s*:\s*(?:Bearer\s+)?(.+)$", token)
            if m:
                token = m.group(1).strip()
        return token

    def save_token(self, platform, token, label="", refresh_token="", expires_in=None):
        token = self.normalize_token(platform, token)
        if not token:
            raise AuthError("Token is empty")
        data = {"access_token": token, "label": (label or "").strip(), "saved_at": int(time.time())}
        if platform == "nexprint":
            data = {"auth_token": token, "label": (label or "").strip(), "saved_at": int(time.time())}
        if refresh_token:
            data["refresh_token"] = refresh_token
        if expires_in:
            try:
                data["expires_at"] = int(time.time()) + int(expires_in)
            except (TypeError, ValueError):
                pass
        self.store.set(platform, data)
        return data

    def _request_headers(self, platform, url):
        headers = {
            "User-Agent": _BROWSER_UA,
            "Accept": "application/json, text/plain, */*",
        }
        host = _url_host(url)
        if platform not in _PLATFORM_HOSTS or not _host_matches(host, _PLATFORM_HOSTS[platform]):
            return headers
        token = self.token(platform)
        if not token:
            return headers
        if platform == "makerworld":
            headers["Authorization"] = "Bearer " + token
            headers["Referer"] = "https://makerworld.com/"
        elif platform == "makeronline":
            # Current Anycubic web/cloud clients use XX-Token; Bearer is also
            # accepted by some Makeronline endpoints. Both are scoped only to
            # *.makeronline.com / *.anycubic.com to prevent credential leakage.
            headers["XX-Token"] = token
            headers["Authorization"] = "Bearer " + token
            headers["Referer"] = "https://www.makeronline.com/"
        elif platform == "nexprint":
            headers["Referer"] = "https://www.nexprint.com/"
        return headers

    def session(self, platform):
        import requests

        session = requests.Session()
        session.headers.update({"User-Agent": _BROWSER_UA})
        if platform == "nexprint":
            token = self.token(platform)
            if token:
                # Nexprint documents auth_token as the login-session cookie used
                # to authenticate API requests. Domain scoping prevents it from
                # being sent to model CDN hosts.
                session.cookies.set("auth_token", token, domain=".nexprint.com", path="/")
        return session

    def request(self, platform, method, url, session=None, **kwargs):
        if not _is_http_url(url):
            raise ValueError("Refusing non-HTTP URL")
        session = session or self.session(platform)
        supplied = dict(kwargs.pop("headers", {}) or {})
        headers = self._request_headers(platform, url)
        headers.update(supplied)
        return session.request(method, url, headers=headers, **kwargs)

    def login_makerworld(self, account, password="", code=""):
        import requests

        account = (account or "").strip()
        if not account:
            raise AuthError("MakerWorld email/account is required")
        payload = {"account": account}
        if code:
            payload["code"] = code.strip()
        elif password:
            payload["password"] = password
        else:
            raise AuthError("Password or verification code is required")

        response = requests.post(
            self.BAMBU_LOGIN,
            json=payload,
            headers={"User-Agent": _BROWSER_UA, "Accept": "application/json"},
            timeout=30,
        )
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400:
            message = data.get("message") or data.get("error") or f"HTTP {response.status_code}"
            raise AuthError(f"MakerWorld login failed: {message}")
        access = data.get("accessToken") or ""
        if not access:
            login_type = str(data.get("loginType") or "")
            if "verify" in login_type.lower() or data.get("needVerify"):
                raise VerificationRequired("MakerWorld requires a verification code. Enter the code sent by Bambu Lab.")
            raise AuthError(data.get("message") or data.get("error") or "MakerWorld did not return an access token")

        label = account
        try:
            profile = requests.get(
                self.BAMBU_PROFILE,
                headers={"Authorization": "Bearer " + access, "User-Agent": _BROWSER_UA},
                timeout=15,
            )
            if profile.ok:
                p = profile.json()
                label = p.get("name") or p.get("handle") or account
        except Exception:
            pass
        return self.save_token(
            "makerworld",
            access,
            label=label,
            refresh_token=data.get("refreshToken") or "",
            expires_in=data.get("expiresIn"),
        )

    @staticmethod
    def _find_access_token(obj):
        if isinstance(obj, dict):
            # Common Anycubic Slicer Next config shapes.
            for key in ("access_token", "accessToken", "XX-Token", "token"):
                value = obj.get(key)
                if isinstance(value, str) and len(value.strip()) >= 16:
                    return value.strip()
            for key, value in obj.items():
                if "anycubic" in str(key).lower() or isinstance(value, (dict, list)):
                    found = AuthManager._find_access_token(value)
                    if found:
                        return found
        elif isinstance(obj, list):
            for value in obj:
                found = AuthManager._find_access_token(value)
                if found:
                    return found
        return ""

    @staticmethod
    def anycubic_config_candidates():
        home = os.path.expanduser("~")
        paths = []
        appdata = os.environ.get("APPDATA")
        if appdata:
            paths.extend([
                os.path.join(appdata, "AnycubicSlicerNext", "AnycubicSlicerNext.conf"),
                os.path.join(appdata, "AnycubicSlicerNext", "config.json"),
            ])
        paths.extend([
            os.path.join(home, "Library", "Application Support", "AnycubicSlicerNext", "AnycubicSlicerNext.conf"),
            os.path.join(home, ".config", "AnycubicSlicerNext", "AnycubicSlicerNext.conf"),
            os.path.join(home, ".local", "share", "AnycubicSlicerNext", "AnycubicSlicerNext.conf"),
        ])
        # Preserve order, remove duplicates.
        return list(dict.fromkeys(paths))

    def import_anycubic_slicer_token(self, candidates=None):
        candidates = candidates or self.anycubic_config_candidates()
        for path in candidates:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = fh.read()
            except OSError:
                continue
            token = ""
            label = "Anycubic Slicer Next"
            try:
                obj = json.loads(raw)
                token = self._find_access_token(obj)
                # Try a few common account fields without ever reading passwords.
                if isinstance(obj, dict):
                    label = obj.get("user_name") or obj.get("nickname") or label
            except ValueError:
                # Some builds use INI-like text. Match only explicit token keys.
                m = re.search(r'(?im)^\s*(?:access_token|accessToken|XX-Token)\s*[=:]\s*["\']?([^"\'\s,}]+)', raw)
                token = m.group(1).strip() if m else ""
            if token:
                data = self.save_token("makeronline", token, label=str(label))
                data["source"] = path
                return data
        raise AuthError(
            "No readable Anycubic Slicer Next access token was found. "
            "Newer builds may encrypt it; paste the token from the authenticated web/slicer session instead."
        )


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

        response = requests.post(
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
            headers={"Content-Type": "application/json", "User-Agent": _BROWSER_UA},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") not in (0, "0", None):
            return []
        results = []
        for item in (data.get("data") or {}).get("data") or []:
            lic_name, lic_url = MAKERONLINE_LICENSES.get(item.get("license", 0), ("Unknown", ""))
            lic = _parse_license(lic_name, lic_url)
            results.append({
                "name": item.get("title", "Untitled"),
                "author": item.get("show_user_name") or item.get("user_name", "Unknown"),
                "platform": "Makeronline",
                "thumbnail_url": (item.get("mold_image") or "").replace("thumbnail", "400x300"),
                "license": lic["name"],
                "license_url": lic["url"],
                "license_summary": lic["summary"],
                "download_url": item.get("target_url", ""),
                "url": item.get("target_url", ""),
                "_mold_id": item.get("mold_id"),
                "requires_auth": True,
            })
        return results

    @staticmethod
    def get_files(model, auth):
        if not auth.authenticated("makeronline"):
            raise AuthRequired("Makeronline requires an Anycubic account session")
        mold_id = model.get("_mold_id")
        if not mold_id:
            m = re.search(r"(?:mold|model)[^0-9]*(\d+)", model.get("url", ""), re.I)
            mold_id = m.group(1) if m else ""
        if not mold_id:
            raise ValueError("Makeronline model id is missing")
        session = auth.session("makeronline")
        response = auth.request(
            "makeronline", "GET", f"{MAKERONLINE_BASE}/api/mold/detail",
            session=session, params={"id": mold_id}, timeout=30,
        )
        if response.status_code in (401, 403):
            raise AuthRequired("Makeronline session was rejected; log in again")
        response.raise_for_status()
        data = response.json()
        if data.get("code") not in (0, "0", None):
            raise RuntimeError(data.get("msg") or data.get("message") or "Makeronline detail API failed")
        detail = data.get("data") or {}
        files = []
        for idx, item in enumerate(detail.get("files") or [], 1):
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("file_url") or item.get("download_url") or ""
            if not url:
                continue
            name = item.get("file_name") or item.get("name") or os.path.basename(urllib.parse.urlsplit(url).path) or f"model_{idx}.stl"
            files.append({"name": name, "url": url})
        return files


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

        response = requests.get(
            NexprintSearcher.SEARCH_URL,
            params={"keyword": query, "pageNo": "1", "pageSize": "30"},
            headers={"User-Agent": _BROWSER_UA},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") not in (0, "0", 200, "200", None):
            return []
        page = ((data.get("data") or {}).get("pageResult") or {})
        results = []
        for item in page.get("list") or []:
            lic_name, lic_url = NEXPRINT_LICENSES.get(item.get("licenseType", 0), ("Unknown", ""))
            lic = _parse_license(lic_name, lic_url)
            model_id = item.get("modelId") or item.get("id")
            results.append({
                "name": item.get("modelName", "Untitled"),
                "author": item.get("authorName") or ((item.get("author") or {}).get("nickname", "Unknown")),
                "platform": "Nexprint",
                "thumbnail_url": item.get("coverImgUrl", ""),
                "license": lic["name"],
                "license_url": lic["url"],
                "license_summary": lic["summary"],
                "download_url": f"{NEXPRINT_BASE}/models/{model_id or ''}",
                "url": f"{NEXPRINT_BASE}/models/{model_id or ''}",
                "_model_id": model_id,
                "requires_auth": True,
            })
        return results

    @staticmethod
    def get_files(model, auth):
        if not auth.authenticated("nexprint"):
            raise AuthRequired("Nexprint requires a logged-in auth_token session")
        model_id = model.get("_model_id")
        if not model_id:
            m = re.search(r"/models/(\d+)", model.get("url", ""))
            model_id = m.group(1) if m else ""
        if not model_id:
            raise ValueError("Nexprint model id is missing")
        session = auth.session("nexprint")
        response = auth.request(
            "nexprint", "GET",
            f"{NEXPRINT_BASE}/gateway/api/v1/model-library-server/model-base-info/get",
            session=session, params={"id": model_id}, timeout=30,
        )
        if response.status_code in (401, 403):
            raise AuthRequired("Nexprint session was rejected; refresh auth_token and log in again")
        response.raise_for_status()
        data = response.json()
        if data.get("code") not in (0, "0", 200, "200", None):
            raise RuntimeError(data.get("msg") or data.get("message") or "Nexprint detail API failed")
        detail = data.get("data") or {}
        files = []
        for idx, item in enumerate(detail.get("modelFileInfoList") or detail.get("files") or [], 1):
            if not isinstance(item, dict):
                continue
            url = item.get("fileUrl") or item.get("url") or item.get("downloadUrl") or ""
            if not url:
                continue
            name = item.get("fileName") or item.get("name") or os.path.basename(urllib.parse.urlsplit(url).path) or f"model_{idx}.stl"
            files.append({"name": name, "url": url})
        return files


class PrintablesSearcher:
    GRAPHQL_URL = "https://api.printables.com/graphql/"
    SEARCH_QUERY = (
        "query Search($query: String!, $limit: Int) {"
        " searchPrints2(query: $query, limit: $limit) {"
        " items { id name slug image { filePath } license { name } user { publicUsername } } } }"
    )

    @staticmethod
    def enabled(tokens):
        return True

    @staticmethod
    def search(query, tokens):
        import requests

        response = requests.post(
            PrintablesSearcher.GRAPHQL_URL,
            json={"query": PrintablesSearcher.SEARCH_QUERY, "variables": {"query": query, "limit": 30}},
            headers={"Content-Type": "application/json", "User-Agent": _BROWSER_UA},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError(payload["errors"][0].get("message", "GraphQL error"))
        items = ((payload.get("data") or {}).get("searchPrints2") or {}).get("items") or []
        results = []
        for item in items:
            path = (item.get("image") or {}).get("filePath", "")
            lic_obj = item.get("license") or {}
            lic = _parse_license(lic_obj.get("name", "") if isinstance(lic_obj, dict) else "")
            url = f"https://www.printables.com/model/{item.get('id','')}-{item.get('slug','')}"
            results.append({
                "name": item.get("name", "Untitled"),
                "author": (item.get("user") or {}).get("publicUsername", "Unknown"),
                "platform": "Printables",
                "thumbnail_url": f"https://media.printables.com/{path}" if path else "",
                "license": lic["name"] or "Unknown",
                "license_url": lic["url"],
                "license_summary": lic["summary"] or "Check Printables for license details.",
                "download_url": url,
                "url": url,
                "requires_auth": False,
                "importable": True,
            })
        return results

    @staticmethod
    def get_files(model, auth=None):
        import requests

        model_url = model.get("url", "") if isinstance(model, dict) else str(model or "")
        m = re.search(r"/model/(\d+)", model_url)
        if not m:
            return []
        query = "{print(id:%s){stls{name filePreviewPath}}}" % m.group(1)
        response = requests.post(
            PrintablesSearcher.GRAPHQL_URL,
            json={"query": query},
            headers={"Content-Type": "application/json", "User-Agent": _BROWSER_UA},
            timeout=30,
        )
        response.raise_for_status()
        stls = ((response.json().get("data") or {}).get("print") or {}).get("stls") or []
        files = []
        for stl in stls:
            preview, name = stl.get("filePreviewPath") or "", stl.get("name") or ""
            if not preview or "/" not in preview or not name:
                continue
            folder = preview.rsplit("/", 1)[0]
            files.append({
                "name": name,
                "url": "https://files.printables.com/%s/%s" % (folder, urllib.parse.quote(name)),
            })
        return files


class MakerWorldSearcher:
    SEARCH_URL = "https://api.bambulab.com/v1/search-service/select/design2"
    DESIGN_BASE = "https://api.bambulab.com/v1/design-service"
    BASE = "https://makerworld.com"
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
    def enabled(tokens):
        return True

    @staticmethod
    def search(query, tokens):
        import requests

        response = requests.get(
            MakerWorldSearcher.SEARCH_URL,
            params={"keyword": query, "limit": "30"},
            headers={"User-Agent": _BROWSER_UA},
            timeout=30,
        )
        response.raise_for_status()
        results = []
        for hit in response.json().get("hits") or []:
            lic_name, lic_url = MakerWorldSearcher.LICENSES.get(hit.get("license", "Unknown"), (hit.get("license", "Unknown"), ""))
            lic = _parse_license(lic_name, lic_url)
            creator = hit.get("designCreator") or {}
            model_id = hit.get("id")
            results.append({
                "name": hit.get("title", "Untitled"),
                "author": creator.get("name", "Unknown"),
                "platform": "MakerWorld",
                "thumbnail_url": hit.get("cover", ""),
                "license": lic["name"],
                "license_url": lic["url"],
                "license_summary": lic["summary"],
                "download_url": f"{MakerWorldSearcher.BASE}/en/models/{model_id or ''}",
                "url": f"{MakerWorldSearcher.BASE}/en/models/{model_id or ''}",
                "_model_id": model_id,
                "requires_auth": True,
            })
        return results

    @staticmethod
    def _profile_from_url(url):
        m = re.search(r"#profileId[-=](\d+)", url or "", re.I)
        return m.group(1) if m else ""

    @staticmethod
    def get_files(model, auth):
        if not auth.authenticated("makerworld"):
            raise AuthRequired("MakerWorld import requires a Bambu/MakerWorld account session")
        design_id = model.get("_model_id")
        if not design_id:
            m = re.search(r"/models/(\d+)", model.get("url", ""))
            design_id = m.group(1) if m else ""
        if not design_id:
            raise ValueError("MakerWorld design id is missing")

        session = auth.session("makerworld")
        # Design metadata is public, but using the same helper keeps UA/referer consistent.
        response = auth.request(
            "makerworld", "GET", f"{MakerWorldSearcher.DESIGN_BASE}/design/{design_id}",
            session=session, timeout=30,
        )
        if response.status_code == 418:
            raise RuntimeError("MakerWorld is challenging this request with CAPTCHA; use Open in browser and retry later")
        response.raise_for_status()
        design = response.json()
        internal_model_id = design.get("modelId") or design.get("model_id")
        if not internal_model_id:
            raise RuntimeError("MakerWorld design metadata did not contain modelId")

        profile_id = MakerWorldSearcher._profile_from_url(model.get("url", ""))
        profile_title = ""
        if not profile_id:
            instances = design.get("instances") or []
            if not instances:
                ir = auth.request(
                    "makerworld", "GET", f"{MakerWorldSearcher.DESIGN_BASE}/design/{design_id}/instances",
                    session=session, timeout=30,
                )
                ir.raise_for_status()
                payload = ir.json()
                instances = payload.get("hits") or payload.get("instances") or []
            if instances:
                first = instances[0] or {}
                profile_id = first.get("profileId") or first.get("profile_id") or first.get("id")
                profile_title = first.get("title") or first.get("name") or ""
        if not profile_id:
            raise RuntimeError("MakerWorld returned no printable profile for this design")

        download_api = f"https://api.bambulab.com/v1/iot-service/api/user/profile/{profile_id}"
        dr = auth.request(
            "makerworld", "GET", download_api, session=session,
            params={"model_id": str(internal_model_id)}, timeout=30,
        )
        if dr.status_code == 401:
            raise AuthRequired("MakerWorld session expired; log in again")
        if dr.status_code == 403:
            try:
                body = dr.json()
                reason = body.get("error") or body.get("message") or "access denied"
            except ValueError:
                reason = "access denied"
            raise RuntimeError(f"MakerWorld refused this profile: {reason}")
        if dr.status_code == 418:
            raise RuntimeError("MakerWorld is challenging this request with CAPTCHA; use Open in browser and retry later")
        dr.raise_for_status()
        payload = dr.json()
        body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        url = body.get("url") or body.get("downloadUrl") or body.get("download_url") or ""
        if not url:
            raise RuntimeError("MakerWorld download API returned no signed URL")
        name = body.get("name") or body.get("filename") or profile_title or design.get("title") or f"makerworld_{design_id}.3mf"
        if not os.path.splitext(name)[1]:
            name += ".3mf"
        return [{"name": name, "url": url, "signed": True}]


class ThingiverseSearcher:
    """Compatibility placeholder.

    Thingiverse remains disabled because the plugin has no application token
    provisioning flow. Kept so existing development scripts importing this
    class continue to work.
    """
    BASE = "https://api.thingiverse.com"

    @staticmethod
    def enabled(tokens):
        return bool((tokens or {}).get("thingiverse_token"))

    @staticmethod
    def search(query, tokens):
        import requests
        token = (tokens or {}).get("thingiverse_token", "")
        if not token:
            return []
        response = requests.get(
            f"{ThingiverseSearcher.BASE}/search/{urllib.parse.quote(query)}",
            params={"type": "things", "per_page": 20, "access_token": token},
            headers={"User-Agent": _BROWSER_UA}, timeout=30,
        )
        response.raise_for_status()
        results = []
        for hit in response.json().get("hits") or []:
            lic = _parse_license(hit.get("license", ""))
            public_url = hit.get("public_url", "")
            results.append({
                "name": hit.get("name", "Untitled"),
                "author": (hit.get("creator") or {}).get("name", "Unknown"),
                "platform": "Thingiverse",
                "thumbnail_url": hit.get("thumbnail", ""),
                "license": lic["name"] or "Unknown",
                "license_url": lic["url"],
                "license_summary": lic["summary"],
                "download_url": public_url + "/zip" if public_url else "",
                "url": public_url,
            })
        return results


class GrabcadSearcher:
    """Compatibility placeholder for the retired GrabCAD public API."""
    BASE = "https://api.grabcad.com/api/v1"

    @staticmethod
    def enabled(tokens):
        return False

    @staticmethod
    def search(query, tokens):
        return []


_SEARCHERS = {
    "printables": PrintablesSearcher,
    "nexprint": NexprintSearcher,
    "makeronline": MakeronlineSearcher,
    "makerworld": MakerWorldSearcher,
    "thingiverse": ThingiverseSearcher,
    "grabcad": GrabcadSearcher,
}

_FILE_RESOLVERS = {
    "Printables": PrintablesSearcher.get_files,
    "Nexprint": NexprintSearcher.get_files,
    "Makeronline": MakeronlineSearcher.get_files,
    "MakerWorld": MakerWorldSearcher.get_files,
}

_PLATFORM_KEY_BY_DISPLAY = {
    "MakerWorld": "makerworld",
    "Nexprint": "nexprint",
    "Makeronline": "makeronline",
    "Printables": "printables",
}


# ---------------------------------------------------------------------------
# Orca import and download helpers
# ---------------------------------------------------------------------------


def _load_in_orca(paths):
    """Hand local files to the running OrcaSlicer plater.

    The current plugin host does not expose a direct mutable plater API. On
    Linux, reuse Orca's single-instance D-Bus handoff. On other platforms the
    download remains available in model_downloads and a clear error is shown.
    """
    if not sys.platform.startswith("linux"):
        return False, "automatic plater handoff is currently available only on Linux; files were downloaded successfully"
    try:
        names = subprocess.run(
            ["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.DBus",
             "/org/freedesktop/DBus", "org.freedesktop.DBus.ListNames"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception as exc:
        return False, f"dbus-send unavailable: {exc}"
    found = re.findall(r"com\.orcaslicer\.OrcaSlicer\.InstanceCheck\.Object(\d+)", names)
    if not found:
        return False, "no running OrcaSlicer instance found on the session bus"
    instance = found[0]
    argv = ["orca-slicer"] + list(paths)
    payload = ";".join('"%s"' % a.replace("\\", "\\\\").replace('"', '\\"') for a in argv)
    iface = "com.orcaslicer.OrcaSlicer.InstanceCheck.Object" + instance
    try:
        proc = subprocess.run(
            ["dbus-send", "--session", "--type=method_call", "--dest=" + iface,
             "/com/orcaslicer/OrcaSlicer/InstanceCheck/Object" + instance,
             iface + ".AnotherInstance", "string:" + payload],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or "").strip()[:300]
    return True, ""


def _download_stream(url, name, dest_dir, auth, platform):
    if not _is_http_url(url):
        raise ValueError("Refusing non-HTTP download URL")
    _reject_obvious_local_target(url)
    _ensure_private_dir(dest_dir)
    path = _unique_path(dest_dir, _safe_filename(name))
    host = _url_host(url)

    # MakerWorld may return an AWS S3 presigned URL. urllib.request preserves
    # its query string bytes and, critically, no Bambu bearer is sent to S3.
    if host.endswith(".amazonaws.com"):
        request = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
        with urllib.request.urlopen(request, timeout=180) as response, open(path, "wb") as fh:
            total = 0
            while True:
                chunk = response.read(262144)
                if not chunk:
                    break
                total += len(chunk)
                if total > 500 * 1024 * 1024:
                    raise RuntimeError("Download exceeds 500 MB safety limit")
                fh.write(chunk)
        return path

    session = auth.session(platform) if platform in _PLATFORM_HOSTS else None
    # AuthManager only attaches sensitive headers if the target is an allowlisted
    # portal host; external CDN URLs get UA only.
    if session is None:
        import requests
        session = requests.Session()
    response = auth.request(platform, "GET", url, session=session, stream=True, timeout=180, allow_redirects=True)
    if response.status_code in (401, 403) and platform in _PLATFORM_HOSTS:
        response.close()
        raise AuthRequired(f"{_PLATFORM_DISPLAY.get(platform, platform)} session was rejected while downloading")
    response.raise_for_status()
    content_type = (response.headers.get("content-type") or "").lower()
    if "text/html" in content_type:
        response.close()
        raise RuntimeError("Platform returned an HTML/login page instead of a model file")
    total = 0
    try:
        with open(path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=262144):
                if not chunk:
                    continue
                total += len(chunk)
                if total > 500 * 1024 * 1024:
                    raise RuntimeError("Download exceeds 500 MB safety limit")
                fh.write(chunk)
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    finally:
        response.close()
    return path


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>3D Model Search</title></head>
<body>
<style>
*{box-sizing:border-box} body{font-family:var(--orca-font,sans-serif);background:var(--orca-bg,#1e1e1e);color:var(--orca-fg,#eee);padding:16px;margin:0}
button,input{font:inherit}.search-row{display:flex;gap:8px;margin:12px 0}.search-row input{flex:1;padding:8px 12px;border:1px solid var(--orca-border,#444);border-radius:6px;background:var(--orca-bg,#1e1e1e);color:inherit}.btn,button{padding:7px 12px;border:0;border-radius:6px;background:var(--orca-accent,#4a9eff);color:var(--orca-accent-fg,#fff);cursor:pointer}.secondary{background:transparent!important;border:1px solid var(--orca-border,#555)!important;color:var(--orca-fg,#eee)!important}.danger{background:#7a3030!important}.muted{color:var(--orca-muted,#999)}
.accounts{display:grid;grid-template-columns:repeat(3,minmax(180px,1fr));gap:8px;margin:10px 0}.account{border:1px solid var(--orca-border,#444);border-radius:7px;padding:8px}.account strong{display:block;font-size:.86em}.auth-state{display:block;font-size:.75em;color:var(--orca-muted,#999);margin:3px 0 7px}.platforms{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:12px}.platforms label{font-size:.84em;color:var(--orca-muted,#aaa)}
#results{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}.card{border:1px solid var(--orca-border,#444);border-radius:8px;padding:10px;cursor:pointer}.card:hover{border-color:var(--orca-accent,#4a9eff)}.card img{width:100%;height:110px;object-fit:cover;border-radius:4px;background:#333}.card h3{font-size:.9em;margin:6px 0 2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.author{font-size:.78em;color:var(--orca-muted,#888)}.license-badge{display:inline-block;padding:1px 7px;border-radius:3px;font-size:.72em;margin-top:4px;background:#444}.license-cc{background:#1a5c2a;color:#8f8}.license-arr{background:#5c3a1a;color:#fc6}
.panel{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);width:min(650px,calc(100% - 32px));max-height:70vh;overflow:auto;z-index:20;padding:14px 34px 14px 14px;border:1px solid var(--orca-border,#444);border-radius:8px;background:var(--orca-bg,#1e1e1e);box-shadow:0 6px 28px rgba(0,0,0,.55);display:none}.panel.active{display:block}.close{position:absolute;right:8px;top:6px;background:none!important;font-size:1.35em;padding:2px 6px}.panel p{font-size:.86em;color:var(--orca-muted,#aaa);margin:6px 0}.panel a{color:var(--orca-accent,#4a9eff)}.responsibility{border-left:3px solid var(--orca-border,#444);padding:8px 10px;margin:10px 0;font-size:.78em;color:var(--orca-muted,#888)}
.modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.58);z-index:29;display:none}.modal-backdrop.active{display:block}.auth-modal{position:fixed;z-index:30;left:50%;top:50%;transform:translate(-50%,-50%);width:min(520px,calc(100% - 32px));background:var(--orca-bg,#1e1e1e);border:1px solid var(--orca-border,#555);border-radius:9px;padding:16px;display:none}.auth-modal.active{display:block}.field{margin:8px 0}.field label{display:block;font-size:.78em;color:var(--orca-muted,#999);margin-bottom:3px}.field input{width:100%;padding:8px;border:1px solid var(--orca-border,#555);background:var(--orca-bg,#222);color:inherit;border-radius:5px}.auth-note{font-size:.79em;color:var(--orca-muted,#aaa);line-height:1.4}.button-row{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}#status{margin-top:10px;color:var(--orca-muted,#999);font-size:.8em}
@media(max-width:680px){.accounts{grid-template-columns:1fr}}
</style>
<h1 style="margin:0;font-size:1.25em">&#128269; 3D Model Search</h1>
<div class="accounts">
  <div class="account"><strong>MakerWorld (Bambu)</strong><span id="auth-makerworld" class="auth-state">Checking...</span><button class="secondary" onclick="openAuth('makerworld')">Account</button></div>
  <div class="account"><strong>Nexprint (Elegoo)</strong><span id="auth-nexprint" class="auth-state">Checking...</span><button class="secondary" onclick="openAuth('nexprint')">Account</button></div>
  <div class="account"><strong>Makeronline (Anycubic)</strong><span id="auth-makeronline" class="auth-state">Checking...</span><button class="secondary" onclick="openAuth('makeronline')">Account</button></div>
</div>
<div class="search-row"><input id="query" placeholder="Search for 3D models..."><button id="search-btn" onclick="doSearch()">Search</button></div>
<div class="platforms">
<label><input type="checkbox" checked data-platform="nexprint"> Nexprint</label>
<label><input type="checkbox" checked data-platform="printables"> Printables</label>
<label><input type="checkbox" checked data-platform="makeronline"> Makeronline</label>
<label><input type="checkbox" checked data-platform="makerworld"> MakerWorld</label>
</div>
<div id="results"></div>
<div id="detail" class="panel"><button class="close" onclick="closeDetail()">&times;</button><h2 id="det-name"></h2><p id="det-author"></p><p id="det-platform"></p><p id="det-url"></p><p id="det-license"></p><p id="det-summary"></p><p class="responsibility">Downloads use your own account session and the platform's own file URL. The plugin does not host or redistribute models. You remain responsible for the model license and the platform terms.</p><button id="det-import-btn" onclick="doImport()">Import into OrcaSlicer</button><button class="secondary" onclick="doDownload()">Open in browser</button></div>
<div id="modal-bg" class="modal-backdrop" onclick="closeAuth()"></div>
<div id="auth-modal" class="auth-modal">
  <h2 id="auth-title" style="margin:0 0 5px;font-size:1.05em">Account</h2>
  <div id="auth-note" class="auth-note"></div>
  <div id="email-field" class="field"><label>Email / account</label><input id="auth-email" autocomplete="username"></div>
  <div id="password-field" class="field"><label>Password (never saved)</label><input id="auth-password" type="password" autocomplete="current-password"></div>
  <div id="code-field" class="field" style="display:none"><label>Verification code</label><input id="auth-code" autocomplete="one-time-code"></div>
  <div class="field"><label id="token-label">Session/access token (alternative)</label><input id="auth-token" type="password" autocomplete="off"></div>
  <div class="button-row"><button id="auth-submit" onclick="submitAuth()">Connect</button><button id="official-login" class="secondary" onclick="openOfficialLogin()">Open official login</button><button id="import-anycubic" class="secondary" style="display:none" onclick="importAnycubic()">Import from Anycubic Slicer Next</button><button id="auth-logout" class="danger" onclick="logoutAuth()">Forget session</button><button class="secondary" onclick="closeAuth()">Cancel</button></div>
</div>
<div id="status">Ready.</div>
<script>
var selectedModel=null, searching=false, authPlatform=null, authStates={}, pendingImport=null;
var $=function(id){return document.getElementById(id)};
function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;")}
function platformKey(display){return {MakerWorld:'makerworld',Nexprint:'nexprint',Makeronline:'makeronline',Printables:'printables'}[display]||String(display||'').toLowerCase()}
function isAuthed(model){if(!model||!model.requires_auth)return true;var s=authStates[platformKey(model.platform)];return !!(s&&s.authenticated)}
function updateAuth(states){authStates=states||{};['makerworld','nexprint','makeronline'].forEach(function(p){var s=authStates[p]||{};$("auth-"+p).textContent=s.authenticated?("Connected: "+(s.label||'session')):'Not connected'});if(selectedModel)showDetail(selectedModel,false)}
function doSearch(){if(searching)return;var q=$('query').value.trim();if(!q)return;searching=true;$('search-btn').disabled=true;$('search-btn').textContent='Searching...';var ps=[];document.querySelectorAll('.platforms input:checked').forEach(function(x){ps.push(x.dataset.platform)});$('status').textContent='Searching...';closeDetail();orca.postMessage({action:'search',query:q,platforms:ps})}
$('query').addEventListener('keydown',function(e){if(e.key==='Enter')doSearch()});
function renderResults(models){window._results=models||[];var html='';window._results.forEach(function(m,i){html+='<div class="card" data-idx="'+i+'"><img src="'+esc(m.thumbnail_url||'')+'"><h3 title="'+esc(m.name)+'">'+esc(m.name)+'</h3><div class="author">'+esc(m.author)+' · '+esc(m.platform)+'</div><span class="license-badge '+licenseClass(m.license)+'">'+esc(m.license||'Unknown')+'</span></div>'});$('results').innerHTML=html;$('status').textContent=window._results.length+' result(s)'}
$('results').addEventListener('click',function(e){var c=e.target.closest&&e.target.closest('.card');if(!c)return;var m=window._results[parseInt(c.dataset.idx,10)];if(m)showDetail(m,true)});
function showDetail(m,open){selectedModel=m;$('det-name').textContent=m.name;$('det-author').innerHTML='<strong>Author:</strong> '+esc(m.author);$('det-platform').innerHTML='<strong>Platform:</strong> '+esc(m.platform);$('det-license').innerHTML='<strong>License:</strong> <span class="license-badge '+licenseClass(m.license)+'">'+esc(m.license||'Unknown')+'</span>';$('det-summary').textContent=m.license_summary||'No license information available.';$('det-url').innerHTML=m.url?'<strong>Model page:</strong> <a href="'+esc(m.url)+'">'+esc(m.url)+'</a>':'';var b=$('det-import-btn');b.disabled=false;b.textContent=(m.requires_auth&&!isAuthed(m))?('Log in to '+m.platform+' & import'):'Import into OrcaSlicer';if(open!==false)$('detail').classList.add('active')}
function closeDetail(){$('detail').classList.remove('active')}
$('detail').addEventListener('click',function(e){var a=e.target.closest&&e.target.closest('a[href]');if(!a)return;e.preventDefault();openExternal(a.getAttribute('href'))});
function licenseClass(l){if(/CC|Creative Commons|CC0|Public Domain/i.test(l||''))return'license-cc';if(/All Rights Reserved|Standard Digital|Exclusive/i.test(l||''))return'license-arr';return''}
function openExternal(url){orca.postMessage({action:'open_external',url:url})}
function doDownload(){if(selectedModel)openExternal(selectedModel.url||selectedModel.download_url)}
function doImport(){if(!selectedModel)return;if(selectedModel.requires_auth&&!isAuthed(selectedModel)){pendingImport=selectedModel;openAuth(platformKey(selectedModel.platform));return}var b=$('det-import-btn');b.disabled=true;b.textContent='Importing...';$('status').textContent='Resolving files...';orca.postMessage({action:'import',model:selectedModel})}
function openAuth(p){authPlatform=p;$('auth-modal').classList.add('active');$('modal-bg').classList.add('active');$('auth-password').value='';$('auth-code').value='';$('auth-token').value='';$('code-field').style.display='none';$('import-anycubic').style.display=p==='makeronline'?'':'none';$('password-field').style.display=(p==='nexprint'||p==='makeronline')?'none':'';$('email-field').style.display=(p==='nexprint'||p==='makeronline')?'none':'';var title={makerworld:'MakerWorld / Bambu account',nexprint:'Nexprint / Elegoo account',makeronline:'Makeronline / Anycubic account'}[p];$('auth-title').textContent=title;$('token-label').textContent=p==='nexprint'?'Nexprint auth_token cookie value':'Session/access token (alternative)';$('auth-note').textContent=p==='makerworld'?'Use Bambu email/password or paste an existing Bambu Cloud access token. MFA verification codes are supported.':p==='nexprint'?'Sign in on the official Nexprint site, then paste the auth_token session cookie. The plugin never asks for or stores your Nexprint password.':'Makeronline no longer uses the legacy direct password endpoint. Sign in with Anycubic Slicer Next and click Import from Anycubic Slicer Next, or paste an existing access token.';var st=authStates[p]||{};$('auth-logout').style.display=st.authenticated?'':'none'}
function closeAuth(){$('auth-modal').classList.remove('active');$('modal-bg').classList.remove('active');$('auth-password').value='';$('auth-token').value=''}
function submitAuth(){var token=$('auth-token').value.trim(),email=$('auth-email').value.trim(),password=$('auth-password').value,code=$('auth-code').value.trim();if(authPlatform==='nexprint'&&!token){$('status').textContent='Nexprint: paste auth_token after signing in.';return}if(authPlatform==='makeronline'&&!token){$('status').textContent='Makeronline: import the Anycubic Slicer Next session or paste an access token.';return}orca.postMessage({action:'auth_login',platform:authPlatform,token:token,email:email,password:password,code:code});$('auth-submit').disabled=true;$('status').textContent='Signing in...'}
function logoutAuth(){orca.postMessage({action:'auth_logout',platform:authPlatform});closeAuth()}
function openOfficialLogin(){orca.postMessage({action:'auth_open_login',platform:authPlatform})}
function importAnycubic(){orca.postMessage({action:'auth_import_anycubic'});$('status').textContent='Looking for Anycubic Slicer Next session...'}
orca.onMessage(function(msg){msg=msg||{};if(msg.action==='results'){searching=false;$('search-btn').disabled=false;$('search-btn').textContent='Search';renderResults(msg.results||[])}else if(msg.action==='auth_status'||msg.action==='auth_changed'){updateAuth(msg.states||{});$('auth-submit').disabled=false;if(msg.action==='auth_changed'){closeAuth();$('status').textContent=msg.message||'Account session updated.';if(pendingImport&&isAuthed(pendingImport)){var m=pendingImport;pendingImport=null;selectedModel=m;doImport()}}}else if(msg.action==='auth_challenge'){$('auth-submit').disabled=false;$('code-field').style.display='';$('status').textContent=msg.message||'Verification code required.'}else if(msg.action==='auth_required'){$('det-import-btn').disabled=false;$('status').textContent=msg.message||'Login required.';pendingImport=msg.model||selectedModel;openAuth(msg.platform)}else if(msg.action==='status'){$('status').textContent=msg.message}else if(msg.action==='imported'){$('det-import-btn').disabled=false;$('det-import-btn').textContent='Import into OrcaSlicer';$('status').textContent='Imported '+msg.count+' file(s) into OrcaSlicer.'}else if(msg.action==='downloaded_only'){$('det-import-btn').disabled=false;$('status').textContent='Downloaded '+msg.count+' file(s) to '+msg.dir+'. '+msg.message}else if(msg.action==='opened'){$('status').textContent='Opened in your browser.'}else if(msg.action==='error'){searching=false;$('search-btn').disabled=false;$('search-btn').textContent='Search';$('auth-submit').disabled=false;if($('det-import-btn'))$('det-import-btn').disabled=false;$('status').textContent='Error: '+msg.message}});
orca.postMessage({action:'auth_status'});
</script></body></html>"""


if orca is not None:

    class SearchEngineScript(orca.script.ScriptPluginCapabilityBase):
        win = None

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.auth = AuthManager()

        def get_name(self):
            return "3D Model Search"

        def execute(self):
            if self.win is not None and self.win.is_open():
                self.win.close()
            self.win = orca.host.ui.create_window(
                html=PAGE,
                title="3D Model Search",
                width=980,
                height=720,
                on_message=self.on_message,
                on_close=self.on_close,
            )
            return orca.ExecutionResult.success()

        def on_close(self):
            self.win = None

        def _post(self, msg):
            if self.win is not None and self.win.is_open():
                self.win.post(msg)

        def _post_auth(self, action="auth_status", message=""):
            self._post({"action": action, "states": self.auth.status(), "message": message})

        def on_message(self, msg):
            msg = msg or {}
            action = msg.get("action", "")
            if action == "search":
                threading.Thread(target=self._do_search, args=(msg,), daemon=True).start()
            elif action == "import":
                model = msg.get("model") or {}
                if model:
                    threading.Thread(target=self._do_import, args=(model,), daemon=True).start()
            elif action == "open_external":
                threading.Thread(target=self._open_external, args=(msg.get("url", ""),), daemon=True).start()
            elif action == "auth_status":
                self._post_auth()
            elif action == "auth_login":
                threading.Thread(target=self._do_auth_login, args=(msg,), daemon=True).start()
            elif action == "auth_logout":
                platform = msg.get("platform", "")
                if platform in _PLATFORM_HOSTS:
                    self.auth.logout(platform)
                self._post_auth("auth_changed", "Session removed.")
            elif action == "auth_open_login":
                platform = msg.get("platform", "")
                url = {
                    "makerworld": "https://makerworld.com/en/sign-in",
                    "nexprint": "https://www.nexprint.com/en/account/login",
                    "makeronline": "https://uc.makeronline.com/",
                }.get(platform, "")
                if url:
                    threading.Thread(target=self._open_external, args=(url,), daemon=True).start()
            elif action == "auth_import_anycubic":
                threading.Thread(target=self._do_import_anycubic, daemon=True).start()

        def _do_auth_login(self, msg):
            platform = msg.get("platform", "")
            # Never log the incoming message: it may contain a password/token.
            try:
                token = (msg.get("token") or "").strip()
                email = (msg.get("email") or "").strip()
                code = (msg.get("code") or "").strip()
                if token:
                    self.auth.save_token(platform, token, label=email or "Connected session")
                elif platform == "makerworld":
                    self.auth.login_makerworld(email, password=msg.get("password") or "", code=code)
                elif platform == "makeronline":
                    raise AuthError(
                        "Makeronline direct email/password login is no longer supported by the current Anycubic flow. "
                        "Sign in with Anycubic Slicer Next and use 'Import from Anycubic Slicer Next', "
                        "or paste an existing access token."
                    )
                elif platform == "nexprint":
                    raise AuthError("Nexprint login requires auth_token from the official signed-in browser session")
                else:
                    raise AuthError("Unknown platform")
            except VerificationRequired as exc:
                self._post({"action": "auth_challenge", "platform": platform, "message": str(exc)})
                return
            except Exception as exc:
                self._post({"action": "error", "message": str(exc)})
                return
            finally:
                # Drop references to secrets promptly.
                msg["password"] = ""
                msg["token"] = ""
            self._post_auth("auth_changed", f"{_PLATFORM_DISPLAY.get(platform, platform)} session connected.")

        def _do_import_anycubic(self):
            try:
                data = self.auth.import_anycubic_slicer_token()
            except Exception as exc:
                self._post({"action": "error", "message": str(exc)})
                return
            self._post_auth("auth_changed", f"Imported Anycubic session from {data.get('source', 'Anycubic Slicer Next')}.")

        def _do_search(self, msg):
            query = msg.get("query", "")
            results = []
            errors = []
            for platform in msg.get("platforms", []):
                adapter = _SEARCHERS.get(platform)
                if not adapter or not adapter.enabled({}):
                    continue
                try:
                    items = adapter.search(query, {})
                    for item in items:
                        key = _PLATFORM_KEY_BY_DISPLAY.get(item.get("platform", ""), "")
                        item["authenticated"] = key == "printables" or self.auth.authenticated(key)
                        item["importable"] = item.get("platform") in _FILE_RESOLVERS
                    results.extend(items)
                except Exception as exc:
                    errors.append(f"{platform}: {exc}")
            self._post({"action": "results", "results": results})
            if errors:
                self._post({"action": "status", "message": " | ".join(errors)})

        def _do_import(self, model):
            platform_name = model.get("platform", "")
            platform_key = _PLATFORM_KEY_BY_DISPLAY.get(platform_name, "")
            resolver = _FILE_RESOLVERS.get(platform_name)
            if resolver is None:
                self._post({"action": "error", "message": f"Import is not supported for {platform_name or 'this platform'}"})
                return
            if model.get("requires_auth") and not self.auth.authenticated(platform_key):
                self._post({
                    "action": "auth_required",
                    "platform": platform_key,
                    "message": f"Log in to {platform_name} before importing.",
                    "model": model,
                })
                return
            try:
                files = resolver(model, self.auth)
            except AuthRequired as exc:
                self.auth.logout(platform_key)
                self._post_auth("auth_changed", f"{platform_name} session expired.")
                self._post({"action": "auth_required", "platform": platform_key, "message": str(exc), "model": model})
                return
            except Exception as exc:
                self._post({"action": "error", "message": f"Could not list files: {exc}"})
                return
            if not files:
                self._post({"action": "error", "message": "No downloadable files were returned by the platform."})
                return

            dest_dir = _download_dir()
            try:
                _ensure_private_dir(dest_dir)
            except OSError as exc:
                self._post({"action": "error", "message": f"Cannot create download directory {dest_dir}: {exc}"})
                return
            paths = []
            for index, item in enumerate(files, 1):
                name = item.get("name") or f"model_{index}.3mf"
                self._post({"action": "status", "message": f"Downloading {index}/{len(files)}: {name}"})
                try:
                    paths.append(_download_stream(item.get("url", ""), name, dest_dir, self.auth, platform_key))
                except AuthRequired as exc:
                    self.auth.logout(platform_key)
                    self._post_auth("auth_changed", f"{platform_name} session expired.")
                    self._post({"action": "auth_required", "platform": platform_key, "message": str(exc), "model": model})
                    return
                except Exception as exc:
                    self._post({"action": "error", "message": f"{name}: {exc}"})
                    return

            ok, detail = _load_in_orca(paths)
            if ok:
                self._post({"action": "imported", "count": len(paths), "dir": dest_dir})
            else:
                self._post({"action": "downloaded_only", "count": len(paths), "dir": dest_dir, "message": detail})

        def _open_external(self, url):
            if not _is_http_url(url):
                self._post({"action": "error", "message": "Refusing to open non-HTTP URL."})
                return
            try:
                if sys.platform == "darwin":
                    subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif os.name == "nt":
                    os.startfile(url)  # noqa: S606
                else:
                    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._post({"action": "opened", "url": url})
            except Exception as exc:
                self._post({"action": "error", "message": f"Could not open browser: {exc}"})

    @orca.plugin
    class SearchEnginePlugin(orca.base):
        def register_capabilities(self):
            orca.register_capability(SearchEngineScript)
            if os.environ.get("SEARCH_ENGINE_AUTORUN"):
                def _autorun():
                    time.sleep(12)
                    try:
                        SearchEngineScript().execute()
                    except Exception as exc:
                        print("[search_engine] autorun failed: %r" % (exc,), file=sys.stderr, flush=True)
                threading.Thread(target=_autorun, daemon=True).start()
