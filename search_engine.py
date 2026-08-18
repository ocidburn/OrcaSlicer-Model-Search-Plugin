# /// script
# requires-python = ">=3.12"
# dependencies = ["requests"]
#
# [tool.orcaslicer.plugin]
# name = "3D Model Search Engine"
# description = "Search and import 3D models from MakerWorld, Printables, Thingiverse, Cults3D, MyMiniFactory, Thangs, Makeronline, Creality Cloud, Nexprint, and GrabCAD."
# author = "Tommaso Bianchi"
# version = "0.4.0"
# ///

try:
    import orca
except ImportError:
    orca = None

import html
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
from contextlib import suppress
from typing import ClassVar
from html.parser import HTMLParser


PLUGIN_VERSION = "0.4.0"
_BROWSER_UA = (
    f"OrcaSlicer-Model-Search-Plugin/{PLUGIN_VERSION} "
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
    with suppress(OSError):
        os.chmod(path, 0o700)


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


class BrowserRequired(RuntimeError):
    """The platform requires its own browser flow for this model/file."""

    def __init__(self, message, url=""):
        super().__init__(message)
        self.url = url


class VerificationRequired(AuthError):
    def __init__(self, message="Verification code required"):
        super().__init__(message)


class AuthStore:
    """Small token/session-only credential store with atomic writes."""

    _FORBIDDEN_KEYS = frozenset({"password", "passwd", "secret"})

    def __init__(self, path=None):
        self.path = path or _auth_file()
        self._lock = threading.RLock()

    def load(self):
        with self._lock:
            try:
                with open(self.path, encoding="utf-8") as fh:
                    data = json.load(fh)
                return data if isinstance(data, dict) else {}
            except (FileNotFoundError, ValueError, OSError):
                return {}

    def get(self, platform):
        value = self.load().get(platform, {})
        return value if isinstance(value, dict) else {}

    def _write(self, data):
        folder = os.path.dirname(self.path) or "."
        _ensure_private_dir(folder)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        with suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        with suppress(OSError):
            os.chmod(self.path, 0o600)

    def set(self, platform, value):
        clean = dict(value or {})
        for forbidden in self._FORBIDDEN_KEYS:
            clean.pop(forbidden, None)
        with self._lock:
            data = self.load()
            data[platform] = clean
            self._write(data)

    def delete(self, platform):
        with self._lock:
            data = self.load()
            if platform not in data:
                return
            del data[platform]
            self._write(data)

_PLATFORM_HOSTS = {
    "makerworld": ("api.bambulab.com", "makerworld.com"),
    "nexprint": ("nexprint.com",),
    "makeronline": ("makeronline.com", "anycubic.com"),
    "grabcad": ("grabcad.com",),
    "cults3d": ("cults3d.com",),
}

_PLATFORM_DISPLAY = {
    "makerworld": "MakerWorld",
    "nexprint": "Nexprint",
    "makeronline": "Makeronline",
    "printables": "Printables",
    "thingiverse": "Thingiverse",
    "cults3d": "Cults3D",
    "myminifactory": "MyMiniFactory",
    "thangs": "Thangs",
    "crealitycloud": "Creality Cloud",
    "grabcad": "GrabCAD",
}
_PLATFORM_KEY_BY_DISPLAY = {display: key for key, display in _PLATFORM_DISPLAY.items()}
_AUTH_TOKEN_FIELD = {
    "makerworld": "access_token",
    "nexprint": "auth_token",
    "makeronline": "access_token",
    "cults3d": "auth_token",
    "grabcad": "auth_token",
}
_AUTH_DEFAULT_LABEL = {
    "cults3d": "Cults3D browser session",
    "grabcad": "GrabCAD browser session",
}
_AUTH_LOGIN_URL = {
    "makerworld": "https://makerworld.com/en/sign-in",
    "nexprint": "https://www.nexprint.com/en/account/login",
    "makeronline": "https://uc.makeronline.com/",
    "cults3d": "https://cults3d.com/en/users/sign_in",
    "grabcad": "https://login.grabcad.com/login",
}
_TOKEN_ONLY_AUTH_ERROR = {
    "makeronline": (
        "Makeronline direct email/password login is no longer supported by the current Anycubic flow. "
        "Sign in with Anycubic Slicer Next and use 'Import from Anycubic Slicer Next', "
        "or paste an existing access token."
    ),
    "nexprint": "Nexprint login requires auth_token from the official signed-in browser session",
    "cults3d": "Cults3D requires a Cookie header/session cookies from the official signed-in browser session",
    "grabcad": "GrabCAD requires a Cookie header/session cookie from the official signed-in browser session",
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
        field = _AUTH_TOKEN_FIELD.get(platform)
        return self.credential(platform).get(field, "") if field else ""

    def authenticated(self, platform):
        return bool(self.token(platform))

    def status(self):
        out = {}
        for platform in _AUTH_TOKEN_FIELD:
            data = self.credential(platform)
            configured = self.authenticated(platform)
            out[platform] = {
                "authenticated": configured,
                "label": data.get("label") or ("Connected" if configured else "Not connected"),
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
        if platform in ("grabcad", "cults3d") and token.lower().startswith("cookie:"):
            # Accept either a raw Cookie header value or a copied "Cookie: ..." header.
            token = token.split(":", 1)[1].strip()
        if platform == "makeronline":
            m = re.match(r"(?i)^(?:XX-Token|Authorization)\s*:\s*(?:Bearer\s+)?(.+)$", token)
            if m:
                token = m.group(1).strip()
        return token

    def save_token(self, platform, token, label="", refresh_token="", expires_in=None):
        field = _AUTH_TOKEN_FIELD.get(platform)
        if field is None:
            raise AuthError(f"Unknown authentication platform: {platform or '<empty>'}")
        token = self.normalize_token(platform, token)
        if not token:
            raise AuthError("Token is empty")
        if platform in ("cults3d", "grabcad") and "=" not in token:
            raise AuthError("Paste browser session cookies in name=value form (or the full Cookie request header)")
        data = {
            field: token,
            "label": (label or _AUTH_DEFAULT_LABEL.get(platform, "")).strip(),
            "saved_at": int(time.time()),
        }
        if refresh_token:
            data["refresh_token"] = refresh_token
        if expires_in:
            with suppress(TypeError, ValueError):
                data["expires_at"] = int(time.time()) + int(expires_in)
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
        elif platform == "grabcad":
            headers["Referer"] = "https://grabcad.com/library"
        elif platform == "cults3d":
            headers["Referer"] = "https://cults3d.com/"
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
        elif platform in ("cults3d", "grabcad"):
            token = self.token(platform)
            domain = ".cults3d.com" if platform == "cults3d" else ".grabcad.com"
            if token:
                # Parse a copied Cookie header into a domain-scoped cookie jar.
                # This prevents session cookies from following redirects to CDNs.
                for part in token.split(";"):
                    part = part.strip()
                    if not part or "=" not in part:
                        continue
                    name, value = part.split("=", 1)
                    name, value = name.strip(), value.strip()
                    if name and value:
                        session.cookies.set(name, value, domain=domain, path="/")
        return session

    def request(self, platform, method, url, session=None, **kwargs):
        if not _is_http_url(url):
            raise ValueError("Refusing non-HTTP URL")
        session = session or self.session(platform)
        supplied = dict(kwargs.pop("headers", {}) or {})
        follow_redirects = bool(kwargs.pop("allow_redirects", True))
        current_url = url
        current_method = str(method or "GET").upper()
        redirect_codes = {301, 302, 303, 307, 308}

        for redirect_index in range(6):
            headers = self._request_headers(platform, current_url)
            headers.update(supplied)
            response = session.request(
                current_method,
                current_url,
                headers=headers,
                allow_redirects=False,
                **kwargs,
            )
            if not follow_redirects or response.status_code not in redirect_codes:
                return response
            location = response.headers.get("location") or response.headers.get("Location")
            if not location:
                return response
            next_url = urllib.parse.urljoin(response.url or current_url, location)
            response.close()
            if not _is_http_url(next_url):
                raise ValueError("Refusing redirect to non-HTTP URL")
            if redirect_index == 5:
                raise RuntimeError("Too many HTTP redirects")

            current_url = next_url
            kwargs.pop("params", None)
            if response.status_code == 303 or (
                response.status_code in (301, 302) and current_method not in ("GET", "HEAD")
            ):
                current_method = "GET"
                for key in ("data", "json", "files"):
                    kwargs.pop(key, None)

    @staticmethod
    def _makerworld_login_payload(account, password, code):
        if code:
            return {"account": account, "code": code.strip()}
        if password:
            return {"account": account, "password": password}
        raise AuthError("Password or verification code is required")

    @staticmethod
    def _makerworld_login_data(response):
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400:
            message = data.get("message") or data.get("error") or f"HTTP {response.status_code}"
            raise AuthError(f"MakerWorld login failed: {message}")
        if data.get("accessToken"):
            return data
        login_type = str(data.get("loginType") or "")
        if "verify" in login_type.lower() or data.get("needVerify"):
            raise VerificationRequired("MakerWorld requires a verification code. Enter the code sent by Bambu Lab.")
        raise AuthError(data.get("message") or data.get("error") or "MakerWorld did not return an access token")

    @staticmethod
    def _makerworld_profile_label(access_token, fallback):
        import requests
        try:
            response = requests.get(
                AuthManager.BAMBU_PROFILE,
                headers={"Authorization": "Bearer " + access_token, "User-Agent": _BROWSER_UA},
                timeout=15,
            )
            if response.ok:
                data = response.json()
                return data.get("name") or data.get("handle") or fallback
        except Exception:
            return fallback
        return fallback

    def login_makerworld(self, account, password="", code=""):
        import requests
        account = (account or "").strip()
        if not account:
            raise AuthError("MakerWorld email/account is required")
        response = requests.post(
            self.BAMBU_LOGIN,
            json=self._makerworld_login_payload(account, password, code),
            headers={"User-Agent": _BROWSER_UA, "Accept": "application/json"},
            timeout=30,
        )
        data = self._makerworld_login_data(response)
        access = data["accessToken"]
        return self.save_token(
            "makerworld",
            access,
            label=self._makerworld_profile_label(access, account),
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
                with open(path, encoding="utf-8") as fh:
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
# Public web-catalog helpers
# ---------------------------------------------------------------------------

_LOADABLE_MODEL_EXTS = (
    ".3mf", ".stl", ".obj", ".step", ".stp", ".iges", ".igs", ".amf",
    ".ply", ".scad", ".fcstd", ".f3d",
)
_MODEL_FILE_EXTS = (*_LOADABLE_MODEL_EXTS, ".zip")
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".avif", ".mp4", ".webm")


def _clean_web_text(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _decode_embedded_url(value):
    value = html.unescape(str(value or "")).strip().strip('"\'')
    value = value.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    value = value.replace("\\u003A", ":").replace("\\u003a", ":")
    value = value.replace("\\u0026", "&")
    return value


def _slug_title(url):
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").split("/")[-1])
    path = re.sub(r"-\d+$", "", path)
    path = re.sub(r"[-_]+", " ", path).strip()
    return path[:120] or "Untitled"


class _CatalogHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = []
        self._anchor = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self._anchor = {
                "href": attrs.get("href", ""),
                "title": attrs.get("title", ""),
                "aria": attrs.get("aria-label", ""),
                "img": "",
            }
            self._text = []
        elif tag == "img" and self._anchor is not None:
            self._anchor["img"] = attrs.get("src") or attrs.get("data-src") or attrs.get("data-lazy-src") or ""
            if not self._anchor.get("title"):
                self._anchor["title"] = attrs.get("alt", "")

    def handle_data(self, data):
        if self._anchor is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor is not None:
            self._anchor["text"] = _clean_web_text(" ".join(self._text))
            self.anchors.append(self._anchor)
            self._anchor = None
            self._text = []


def _parse_catalog_html(raw):
    parser = _CatalogHTMLParser()
    with suppress(Exception):
        parser.feed(raw or "")
    return parser


def _fetch_html(url, auth=None, platform="", timeout=30):
    import requests
    session = auth.session(platform) if auth is not None and platform else requests.Session()
    if auth is not None and platform:
        response = auth.request(platform, "GET", url, session=session, timeout=timeout, allow_redirects=True,
                                headers={"Accept": "text/html,application/xhtml+xml"})
    else:
        response = session.get(url, timeout=timeout, allow_redirects=True,
                               headers={"User-Agent": _BROWSER_UA, "Accept": "text/html,application/xhtml+xml"})
    if response.status_code in (401, 403) and platform in ("grabcad", "cults3d"):
        display = _PLATFORM_DISPLAY.get(platform, platform)
        raise AuthRequired(f"{display} rejected the browser session. Sign in again and paste a fresh Cookie header.")
    response.raise_for_status()
    return response.text, response.url


def _looks_like_login_page(raw, platform=""):
    sample = (raw or "")[:300000].lower()
    if platform == "grabcad":
        return ("sign in or create account" in sample or "sign in with email" in sample or
                ('action="/login"' in sample and "forgot password" in sample))
    if platform == "cults3d":
        return ("/users/sign_in" in sample or "/en/users/sign_in" in sample or
                ("sign in" in sample and "forgot your password" in sample))
    return False


def _extract_catalog_models(raw, base_url, path_pattern, platform, requires_auth=False, limit=30):
    parser = _parse_catalog_html(raw)
    regex = re.compile(path_pattern, re.I)
    found = []
    seen = set()

    def add(href, title="", img=""):
        href = _decode_embedded_url(href)
        if not href:
            return
        absolute = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlsplit(absolute)
        canonical = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        if not regex.search(parsed.path):
            return
        if canonical in seen:
            return
        seen.add(canonical)
        found.append({
            "name": _clean_web_text(title) or _slug_title(canonical),
            "author": "Unknown",
            "platform": platform,
            "thumbnail_url": urllib.parse.urljoin(base_url, _decode_embedded_url(img)) if img else "",
            "license": "Unknown",
            "license_url": "",
            "license_summary": "Open the model page to review the exact license before importing.",
            "download_url": canonical,
            "url": canonical,
            "requires_auth": requires_auth,
        })

    for a in parser.anchors:
        add(a.get("href"), a.get("text") or a.get("title") or a.get("aria"), a.get("img"))
        if len(found) >= limit:
            return found

    # SSR/Next/Vue pages frequently put model paths in JSON rather than anchors.
    normalized = (raw or "").replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    for match in regex.finditer(normalized):
        path = match.group(0)
        add(path)
        if len(found) >= limit:
            break
    return found


def _catalog_search(query, url_templates, path_pattern, platform, auth=None, auth_platform="", requires_auth=False):
    last_error = None
    for template in url_templates:
        url = template.format(query=urllib.parse.quote(query.strip(), safe=""))
        try:
            raw, final_url = _fetch_html(
                url,
                auth=auth if auth_platform else None,
                platform=auth_platform,
            )
            if auth_platform and _looks_like_login_page(raw, auth_platform):
                display = _PLATFORM_DISPLAY.get(auth_platform, auth_platform)
                raise AuthRequired(f"{display} search requires a signed-in browser session.")
            models = _extract_catalog_models(raw, final_url, path_pattern, platform, requires_auth=requires_auth)
            if models:
                return models
        except AuthRequired:
            raise
        except Exception as exc:
            last_error = exc
    if last_error:
        raise RuntimeError(f"{platform} search failed: {last_error}")
    return []

def _extract_download_candidates(raw, base_url):
    parser = _parse_catalog_html(raw)
    candidates = []
    seen = set()

    def add(value, label=""):
        value = _decode_embedded_url(value)
        if not value or value.startswith(("javascript:", "data:", "mailto:")):
            return
        url = urllib.parse.urljoin(base_url, value)
        if not _is_http_url(url):
            return
        low_url = urllib.parse.unquote(url).lower()
        path = urllib.parse.urlsplit(low_url).path
        if path.endswith(_IMAGE_EXTS):
            return
        low_label = (label or "").lower()
        direct = path.endswith(_MODEL_FILE_EXTS)
        action = "download" in low_label or "download" in low_url or "/files/" in low_url or "/file/" in low_url
        if not (direct or action):
            return
        if url in seen:
            return
        seen.add(url)
        candidates.append((url, _clean_web_text(label)))

    for a in parser.anchors:
        add(a.get("href"), a.get("text") or a.get("title") or a.get("aria"))

    normalized = (raw or "").replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    normalized = normalized.replace("\\u003A", ":").replace("\\u003a", ":")
    for match in re.finditer(r'https?://[^"\'<>\\\s]+', normalized, re.I):
        add(match.group(0))
    for match in re.finditer(r'(?P<q>["\'])(?P<path>/[^"\']{1,700}(?:download|files?)[^"\']*)(?P=q)', normalized, re.I):
        add(match.group("path"))
    return candidates


def _probe_request(session, url, auth=None, platform="", use_range=True):
    import requests

    headers = {"Accept": "*/*"}
    if use_range:
        headers["Range"] = "bytes=0-0"
    try:
        if auth is not None and platform:
            return auth.request(
                platform, "GET", url, session=session, stream=True, timeout=25,
                allow_redirects=True, headers=headers,
            )
        return session.get(
            url, stream=True, timeout=25, allow_redirects=True,
            headers={"User-Agent": _BROWSER_UA, **headers},
        )
    except requests.RequestException:
        return None


def _download_info(response):
    if response.status_code >= 400:
        return None
    content_type = (response.headers.get("content-type") or "").lower()
    disposition = response.headers.get("content-disposition") or ""
    final_url = response.url
    final_path = urllib.parse.urlsplit(final_url).path.lower()
    if "text/html" in content_type or "application/xhtml" in content_type:
        return None
    downloadable = (
        final_path.endswith(_MODEL_FILE_EXTS)
        or "attachment" in disposition.lower()
        or any(token in content_type for token in ("zip", "octet-stream", "model/", "3mf", "stl"))
    )
    if not downloadable:
        return None
    match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", disposition, re.I)
    filename = urllib.parse.unquote(match.group(1).strip().strip('"')) if match else ""
    if not filename:
        filename = os.path.basename(urllib.parse.urlsplit(final_url).path) or "model_download"
    return {"url": final_url, "name": _safe_filename(filename, "model_download")}


def _probe_download(url, auth=None, platform=""):
    """Validate a candidate without consuming the full response body."""
    import requests

    if not _is_http_url(url):
        return None
    _reject_obvious_local_target(url)
    session = auth.session(platform) if auth is not None and platform else requests.Session()
    response = _probe_request(session, url, auth=auth, platform=platform, use_range=True)
    if response is None:
        return None
    try:
        if response.status_code in (405, 416):
            response.close()
            response = _probe_request(session, url, auth=auth, platform=platform, use_range=False)
            if response is None:
                return None
        if response.status_code in (401, 403) and platform in ("grabcad", "cults3d"):
            display = _PLATFORM_DISPLAY.get(platform, platform)
            raise AuthRequired(f"{display} session was rejected while resolving files.")
        return _download_info(response)
    finally:
        if response is not None:
            response.close()

def _collect_page_candidates(urls, auth=None, platform_key=""):
    body_parts = []
    candidates = []
    for url in urls:
        try:
            raw, fetched = _fetch_html(url, auth=auth if platform_key else None, platform=platform_key)
        except AuthRequired:
            raise
        except Exception:
            continue
        if platform_key in ("cults3d", "grabcad") and _looks_like_login_page(raw, platform_key):
            display = _PLATFORM_DISPLAY.get(platform_key, platform_key)
            raise AuthRequired(f"{display} browser session is no longer signed in. Refresh the saved Cookie header.")
        body_parts.append(raw)
        candidates.extend(_extract_download_candidates(raw, fetched))
    return "\n".join(body_parts), candidates


def _validated_candidates(candidates, auth=None, platform_key=""):
    ordered = sorted(
        candidates,
        key=lambda item: 0 if urllib.parse.urlsplit(item[0]).path.lower().endswith(_MODEL_FILE_EXTS) else 1,
    )
    files = []
    seen = set()
    for candidate, label in ordered[:30]:
        probed = _probe_download(candidate, auth=auth if platform_key else None, platform=platform_key)
        if not probed or probed["url"] in seen:
            continue
        seen.add(probed["url"])
        if not os.path.splitext(probed["name"])[1]:
            probed["name"] += ".3mf" if "3mf" in (label or "").lower() else ".zip"
        files.append(probed)
    return files


def _public_page_files(model, auth=None, platform_key="", extra_urls=(), restricted_markers=(), no_direct_message=""):
    page_url = model.get("url") or model.get("download_url") or ""
    if not page_url:
        raise ValueError("Model page URL is missing")
    body, candidates = _collect_page_candidates(
        [page_url, *(url for url in extra_urls if url)], auth=auth, platform_key=platform_key
    )
    if any(marker.lower() in body.lower() for marker in restricted_markers):
        raise BrowserRequired(
            "This model is paid, membership-gated, or requires the platform checkout/download page.", page_url
        )
    files = _validated_candidates(candidates, auth=auth, platform_key=platform_key)
    if files:
        return files
    raise BrowserRequired(
        no_direct_message or "The platform did not expose a direct downloadable model file to this session.", page_url
    )


def _extract_api_files(detail, list_keys, url_keys, name_keys):
    rows = next((detail.get(key) for key in list_keys if isinstance(detail.get(key), list)), [])
    files = []
    for index, item in enumerate(rows, 1):
        if not isinstance(item, dict):
            continue
        url = next((item.get(key) for key in url_keys if item.get(key)), "")
        if not url:
            continue
        name = next((item.get(key) for key in name_keys if item.get(key)), "")
        if not name:
            name = os.path.basename(urllib.parse.urlsplit(url).path) or f"model_{index}.stl"
        files.append({"name": name, "url": url})
    return files


# ---------------------------------------------------------------------------
# Search adapters

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
    def search(query, _context):
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
        return _extract_api_files(
            data.get("data") or {},
            ("files",),
            ("url", "file_url", "download_url"),
            ("file_name", "name"),
        )


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
    def search(query, _context):
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
        return _extract_api_files(
            data.get("data") or {},
            ("modelFileInfoList", "files"),
            ("fileUrl", "url", "downloadUrl"),
            ("fileName", "name"),
        )


class PrintablesSearcher:
    GRAPHQL_URL = "https://api.printables.com/graphql/"
    SEARCH_QUERY = (
        "query Search($query: String!, $limit: Int) {"
        " searchPrints2(query: $query, limit: $limit) {"
        " items { id name slug image { filePath } license { name } user { publicUsername } } } }"
    )


    @staticmethod
    def search(query, _context):
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
        query = "{print(id:" + m.group(1) + "){stls{name filePreviewPath}}}"
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
                "url": f"https://files.printables.com/{folder}/{urllib.parse.quote(name)}",
            })
        return files


class MakerWorldSearcher:
    SEARCH_URL = "https://api.bambulab.com/v1/search-service/select/design2"
    DESIGN_BASE = "https://api.bambulab.com/v1/design-service"
    BASE = "https://makerworld.com"
    LICENSES: ClassVar[dict] = {
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
    def search(query, _context):
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
            raw_license = hit.get("license", "Unknown")
            lic_name, lic_url = MakerWorldSearcher.LICENSES.get(raw_license, (raw_license, ""))
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
        match = re.search(r"#profileId[-=](\d+)", url or "", re.I)
        return match.group(1) if match else ""

    @staticmethod
    def _design_id(model):
        design_id = model.get("_model_id")
        if design_id:
            return design_id
        match = re.search(r"/models/(\d+)", model.get("url", ""))
        return match.group(1) if match else ""

    @staticmethod
    def _fetch_design(auth, session, design_id):
        response = auth.request(
            "makerworld", "GET", f"{MakerWorldSearcher.DESIGN_BASE}/design/{design_id}",
            session=session, timeout=30,
        )
        if response.status_code == 418:
            raise RuntimeError("MakerWorld is challenging this request with CAPTCHA; use Open in browser and retry later")
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _resolve_profile(auth, session, design_id, design, model_url):
        profile_id = MakerWorldSearcher._profile_from_url(model_url)
        if profile_id:
            return profile_id, ""
        instances = design.get("instances") or []
        if not instances:
            response = auth.request(
                "makerworld", "GET", f"{MakerWorldSearcher.DESIGN_BASE}/design/{design_id}/instances",
                session=session, timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            instances = payload.get("hits") or payload.get("instances") or []
        if not instances:
            raise RuntimeError("MakerWorld returned no printable profile for this design")
        first = instances[0] or {}
        profile_id = first.get("profileId") or first.get("profile_id") or first.get("id")
        if not profile_id:
            raise RuntimeError("MakerWorld returned a profile without an id")
        return profile_id, first.get("title") or first.get("name") or ""

    @staticmethod
    def _download_profile(auth, session, internal_model_id, profile_id, profile_title, design, design_id):
        response = auth.request(
            "makerworld", "GET",
            f"https://api.bambulab.com/v1/iot-service/api/user/profile/{profile_id}",
            session=session, params={"model_id": str(internal_model_id)}, timeout=30,
        )
        if response.status_code == 401:
            raise AuthRequired("MakerWorld session expired; log in again")
        if response.status_code == 418:
            raise RuntimeError("MakerWorld is challenging this request with CAPTCHA; use Open in browser and retry later")
        if response.status_code == 403:
            try:
                payload = response.json()
                reason = payload.get("error") or payload.get("message") or "access denied"
            except ValueError:
                reason = "access denied"
            raise RuntimeError(f"MakerWorld refused this profile: {reason}")
        response.raise_for_status()
        payload = response.json()
        body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        url = body.get("url") or body.get("downloadUrl") or body.get("download_url") or ""
        if not url:
            raise RuntimeError("MakerWorld download API returned no signed URL")
        name = body.get("name") or body.get("filename") or profile_title or design.get("title") or f"makerworld_{design_id}.3mf"
        if not os.path.splitext(name)[1]:
            name += ".3mf"
        return {"name": name, "url": url, "signed": True}

    @staticmethod
    def get_files(model, auth):
        if not auth.authenticated("makerworld"):
            raise AuthRequired("MakerWorld import requires a Bambu/MakerWorld account session")
        design_id = MakerWorldSearcher._design_id(model)
        if not design_id:
            raise ValueError("MakerWorld design id is missing")
        session = auth.session("makerworld")
        design = MakerWorldSearcher._fetch_design(auth, session, design_id)
        internal_model_id = design.get("modelId") or design.get("model_id")
        if not internal_model_id:
            raise RuntimeError("MakerWorld design metadata did not contain modelId")
        profile_id, profile_title = MakerWorldSearcher._resolve_profile(
            auth, session, design_id, design, model.get("url", "")
        )
        return [MakerWorldSearcher._download_profile(
            auth, session, internal_model_id, profile_id, profile_title, design, design_id
        )]

class ThingiverseSearcher:
    BASE = "https://www.thingiverse.com"
    SEARCH_URLS = (
        BASE + "/search?q={query}&page=1&type=things&sort=relevant",
        BASE + "/search?type=things&q={query}",
    )
    PATH_RE = r"/thing(?::|%3A)\d+"


    @staticmethod
    def search(query, _context):
        return _catalog_search(query, ThingiverseSearcher.SEARCH_URLS, ThingiverseSearcher.PATH_RE, "Thingiverse")

    @staticmethod
    def get_files(model, auth=None):
        url = (model.get("url") or "").rstrip("/")
        if not url:
            raise ValueError("Thingiverse model URL is missing")
        # Current Thingiverse exposes a public Download All Files action. Try the
        # traditional zip route first; if the site changes it, fall back to
        # parsing the model and Files pages for individual public file links.
        zip_candidate = _probe_download(url + "/zip")
        if zip_candidate:
            m = re.search(r"thing(?::|%3A)(\d+)", url, re.I)
            zip_candidate["name"] = f"thingiverse_{m.group(1) if m else 'model'}.zip"
            return [zip_candidate]
        return _public_page_files(
            model,
            extra_urls=(url + "/files",),
            no_direct_message="Thingiverse did not expose a public file URL for this model; open the model page in the browser.",
        )


class Cults3DSearcher:
    BASE = "https://cults3d.com"
    SEARCH_URLS = (
        BASE + "/en/tags/{query}",
        BASE + "/en/tags/{query}?only_free=true",
    )
    PATH_RE = r"/en/3d-model/[a-z0-9_-]+/[a-z0-9%._~+()-]+"


    @staticmethod
    def search(query, _context):
        items = _catalog_search(query, Cults3DSearcher.SEARCH_URLS, Cults3DSearcher.PATH_RE, "Cults3D")
        for item in items:
            item["requires_auth"] = True
        return items

    @staticmethod
    def get_files(model, auth=None):
        # Cults requires an account even for free-file downloads, while its
        # documented API intentionally does not expose other users' 3D files.
        # Use the user's browser session only against cults3d.com.
        if auth is None or not auth.authenticated("cults3d"):
            raise AuthRequired("Cults3D requires a signed-in Cults account before downloading files")
        return _public_page_files(
            model, auth=auth, platform_key="cults3d",
            no_direct_message="Cults3D did not expose a direct file to this signed-in session. Use Open in browser for the official download/checkout flow.",
        )


class MyMiniFactorySearcher:
    BASE = "https://www.myminifactory.com"
    SEARCH_URLS = (
        BASE + "/search/query/?query={query}",
        BASE + "/search/?query={query}",
        BASE + "/search/?q={query}",
        BASE + "/search/{query}",
    )
    PATH_RE = r"/object/3d-print-[a-z0-9%._~+()-]+-\d+"


    @staticmethod
    def search(query, _context):
        return _catalog_search(query, MyMiniFactorySearcher.SEARCH_URLS, MyMiniFactorySearcher.PATH_RE, "MyMiniFactory")

    @staticmethod
    def get_files(model, auth=None):
        return _public_page_files(
            model,
            restricted_markers=("Add Files To Cart",),
            no_direct_message="MyMiniFactory did not expose a public direct file for this object. Paid/member-only objects must be downloaded in the browser.",
        )


class ThangsSearcher:
    BASE = "https://thangs.com"
    SEARCH_URLS = (
        BASE + "/search/{query}?scope=thangs&view=list",
        BASE + "/digital/search/{query}?scope=thangs&view=list",
    )
    PATH_RE = r"/designer/[^\"'<>?#]+/3d-model/[^\"'<>?#]+"


    @staticmethod
    def search(query, _context):
        return _catalog_search(query, ThangsSearcher.SEARCH_URLS, ThangsSearcher.PATH_RE, "Thangs")

    @staticmethod
    def get_files(model, auth=None):
        return _public_page_files(
            model,
            restricted_markers=("Become a member to download", "Add download to cart", "Purchase model for"),
            no_direct_message="Thangs did not expose a public direct file for this free model. Use Open in browser for member/paid or interactive downloads.",
        )


class CrealityCloudSearcher:
    BASE = "https://www.crealitycloud.com"
    SEARCH_URLS = (BASE + "/search/model?q={query}",)
    PATH_RE = r"/model-detail/[a-z0-9%._~+()-]+"


    @staticmethod
    def search(query, _context):
        return _catalog_search(query, CrealityCloudSearcher.SEARCH_URLS, CrealityCloudSearcher.PATH_RE, "Creality Cloud")

    @staticmethod
    def get_files(model, auth=None):
        return _public_page_files(
            model,
            restricted_markers=("Buy now", "Purchase", "Subscribe to download"),
            no_direct_message="Creality Cloud did not expose a public direct 3MF/STL URL for this model. Use Open in browser for its official download flow.",
        )


class GrabcadSearcher:
    BASE = "https://grabcad.com"
    SEARCH_URLS = (
        BASE + "/library?query={query}",
        BASE + "/library?utf8=%E2%9C%93&query={query}",
    )
    PATH_RE = r"/library/[a-z0-9%._~+()-]+"


    @staticmethod
    def search(query, context):
        if not isinstance(context, AuthManager) or not context.authenticated("grabcad"):
            raise AuthRequired("GrabCAD requires a free member account to access/download Community Library models. Connect a browser session first.")
        return _catalog_search(
            query, GrabcadSearcher.SEARCH_URLS, GrabcadSearcher.PATH_RE,
            "GrabCAD", auth=context, auth_platform="grabcad", requires_auth=True,
        )

    @staticmethod
    def get_files(model, auth):
        if not auth.authenticated("grabcad"):
            raise AuthRequired("GrabCAD requires a signed-in browser session")
        return _public_page_files(
            model,
            auth=auth,
            platform_key="grabcad",
            no_direct_message="GrabCAD did not expose a downloadable CAD file to this browser session. Open the model page and refresh the saved Cookie header if necessary.",
        )


_SEARCHERS = {
    "printables": PrintablesSearcher,
    "nexprint": NexprintSearcher,
    "makeronline": MakeronlineSearcher,
    "makerworld": MakerWorldSearcher,
    "thingiverse": ThingiverseSearcher,
    "cults3d": Cults3DSearcher,
    "myminifactory": MyMiniFactorySearcher,
    "thangs": ThangsSearcher,
    "crealitycloud": CrealityCloudSearcher,
    "grabcad": GrabcadSearcher,
}


def _platform_key_for_model(model):
    key = str(model.get("_platform_key") or "")
    if key in _SEARCHERS:
        return key
    return _PLATFORM_KEY_BY_DISPLAY.get(model.get("platform", ""), "")


def _normalize_download_files(files):
    normalized = []
    for index, item in enumerate(files or []):
        if not isinstance(item, dict) or not item.get("url"):
            continue
        normalized.append({
            "url": item.get("url", ""),
            "name": item.get("name") or f"model_{index + 1}.3mf",
        })
    return normalized


def _selected_file_indices(values):
    selected = []
    seen = set()
    for value in values or []:
        index = int(value)
        if index >= 0 and index not in seen:
            seen.add(index)
            selected.append(index)
    return selected


# ---------------------------------------------------------------------------
# Orca import and download helpers

# ---------------------------------------------------------------------------
# Orca import and download helpers
# ---------------------------------------------------------------------------


def _current_orca_executable():
    """Return the executable of the OrcaSlicer process hosting this plugin."""
    try:
        if os.name == "nt":
            import ctypes
            buf = ctypes.create_unicode_buffer(32768)
            length = ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf))
            if length:
                return os.path.realpath(buf.value)
        elif sys.platform == "darwin":
            import ctypes
            libc = ctypes.CDLL(None)
            size = ctypes.c_uint32(0)
            libc._NSGetExecutablePath(None, ctypes.byref(size))
            if size.value:
                buf = ctypes.create_string_buffer(size.value)
                if libc._NSGetExecutablePath(buf, ctypes.byref(size)) == 0:
                    return os.path.realpath(os.fsdecode(buf.value))
        else:
            appimage = os.environ.get("APPIMAGE", "")
            if appimage and os.path.isfile(appimage):
                return os.path.realpath(appimage)
            proc_exe = "/proc/self/exe"
            if os.path.exists(proc_exe):
                return os.path.realpath(proc_exe)
    except Exception:
        pass
    candidate = os.path.realpath(sys.executable or "")
    return candidate if candidate and os.path.isfile(candidate) else ""


def _escape_strings_cstyle(values):
    """Serialize argv using OrcaSlicer's escape_strings_cstyle format."""
    escaped = []
    for value in values:
        value = os.fspath(value)
        needs_quotes = (len(values) == 1 and not value) or any(
            ch in value for ch in (" ", "\t", ";", "\\", '"', "\r", "\n")
        )
        if not needs_quotes:
            escaped.append(value)
            continue
        out = []
        for ch in value:
            if ch in ("\\", '"'):
                out.append("\\" + ch)
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\n":
                out.append("\\n")
            else:
                out.append(ch)
        escaped.append('"' + "".join(out) + '"')
    return ";".join(escaped)


def _send_windows_instance_message(executable, paths):
    """Send model paths directly to the current OrcaSlicer main window on Windows."""
    try:
        import ctypes
        from ctypes import wintypes

        win_dll = getattr(ctypes, "WinDLL", None)
        winfunctype = getattr(ctypes, "WINFUNCTYPE", None)
        get_last_error = getattr(ctypes, "get_last_error", lambda: 0)
        if win_dll is None or winfunctype is None:
            return False, "Windows ctypes API is unavailable"
        user32 = win_dll("user32", use_last_error=True)
        current_pid = os.getpid()
        candidates = []

        get_window_pid = user32.GetWindowThreadProcessId
        get_window_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        get_window_pid.restype = wintypes.DWORD
        get_class_name = user32.GetClassNameW
        get_class_name.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        get_class_name.restype = ctypes.c_int
        get_prop = user32.GetPropW
        get_prop.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        get_prop.restype = wintypes.HANDLE
        get_window_text = user32.GetWindowTextW
        get_window_text.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        get_window_text.restype = ctypes.c_int

        enum_proc_type = winfunctype(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @enum_proc_type
        def enum_proc(hwnd, _lparam):
            pid = wintypes.DWORD(0)
            get_window_pid(hwnd, ctypes.byref(pid))
            if pid.value != current_pid:
                return True
            class_buf = ctypes.create_unicode_buffer(256)
            if not get_class_name(hwnd, class_buf, len(class_buf)) or class_buf.value != "wxWindowNR":
                return True
            minor = get_prop(hwnd, "Instance_Hash_Minor")
            major = get_prop(hwnd, "Instance_Hash_Major")
            title_buf = ctypes.create_unicode_buffer(512)
            get_window_text(hwnd, title_buf, len(title_buf))
            score = 0
            if minor or major:
                score += 100
            if "orcaslicer" in title_buf.value.lower():
                score += 10
            candidates.append((score, int(hwnd)))
            return True

        enum_windows = user32.EnumWindows
        enum_windows.argtypes = [enum_proc_type, wintypes.LPARAM]
        enum_windows.restype = wintypes.BOOL
        if not enum_windows(enum_proc, 0):
            last_error = int(get_last_error())
            if last_error:
                return False, f"could not enumerate OrcaSlicer windows (Win32 error {last_error})"
        if not candidates:
            return False, "could not find the current OrcaSlicer main window"
        candidates.sort(reverse=True)
        hwnd = wintypes.HWND(candidates[0][1])

        payload = _escape_strings_cstyle([executable, *paths])
        payload_buf = ctypes.create_unicode_buffer(payload)

        class COPYDATASTRUCT(ctypes.Structure):
            _fields_ = [
                ("dwData", ctypes.c_size_t),
                ("cbData", wintypes.DWORD),
                ("lpData", wintypes.LPVOID),
            ]

        data = COPYDATASTRUCT(
            1,
            ctypes.sizeof(payload_buf),
            ctypes.cast(payload_buf, wintypes.LPVOID),
        )
        WM_COPYDATA = 0x004A
        send_message = user32.SendMessageW
        send_message.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        send_message.restype = ctypes.c_ssize_t
        send_message(hwnd, WM_COPYDATA, 0, ctypes.addressof(data))
        return True, ""
    except Exception as exc:
        return False, f"Windows OrcaSlicer import handoff failed: {exc}"


def _load_in_orca(paths):
    """Import local files into the already-open OrcaSlicer project.

    On Windows, send OrcaSlicer's native WM_COPYDATA single-instance message
    directly to the current main window. This avoids depending on CLI options
    that differ between OrcaSlicer releases. On macOS/Linux, start OrcaSlicer
    with only the file paths and let its configured single-instance handler
    forward them to the running plater.
    """
    normalized = []
    for path in paths:
        path = os.path.abspath(os.fspath(path))
        if not os.path.isfile(path):
            return False, f"downloaded model file no longer exists: {path}"
        normalized.append(path)
    if not normalized:
        return False, "no model files selected for import"

    executable = _current_orca_executable()
    if not executable:
        return False, "could not determine the running OrcaSlicer executable"

    if os.name == "nt":
        return _send_windows_instance_message(executable, normalized)

    try:
        proc = subprocess.run(
            [executable, *normalized],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
        )
    except Exception as exc:
        return False, f"OrcaSlicer import handoff failed: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().replace("\n", " ")[:400]
        return False, detail or f"OrcaSlicer handoff exited with code {proc.returncode}"
    return True, ""




def _expand_archives(paths, dest_dir):
    """Safely expand ZIP downloads and return files Orca can actually open."""
    loadable = []
    extracted_total = 0
    for path in paths:
        ext = os.path.splitext(path)[1].lower()
        if ext in _LOADABLE_MODEL_EXTS:
            loadable.append(path)
            continue
        if ext != ".zip":
            continue
        try:
            archive = zipfile.ZipFile(path, "r")
        except (OSError, zipfile.BadZipFile):
            continue
        with archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                member_ext = os.path.splitext(member.filename)[1].lower()
                if member_ext not in _LOADABLE_MODEL_EXTS:
                    continue
                if member.file_size > 500 * 1024 * 1024:
                    raise RuntimeError("Archive contains a model file larger than 500 MB")
                extracted_total += member.file_size
                if extracted_total > 1024 * 1024 * 1024:
                    raise RuntimeError("Archive extraction exceeds 1 GB safety limit")
                target = _unique_path(dest_dir, _safe_filename(os.path.basename(member.filename), "model" + member_ext))
                with archive.open(member, "r") as src, open(target, "wb") as dst:
                    while True:
                        chunk = src.read(262144)
                        if not chunk:
                            break
                        dst.write(chunk)
                loadable.append(target)
    return loadable

def _download_stream(url, name, dest_dir, auth, platform):
    import requests

    if not _is_http_url(url):
        raise ValueError("Refusing non-HTTP download URL")
    _reject_obvious_local_target(url)
    _ensure_private_dir(dest_dir)
    path = _unique_path(dest_dir, _safe_filename(name))
    session = auth.session(platform) if platform in _PLATFORM_HOSTS else requests.Session()
    response = auth.request(
        platform, "GET", url, session=session, stream=True, timeout=180, allow_redirects=True
    )
    try:
        if response.status_code in (401, 403) and platform in _PLATFORM_HOSTS:
            raise AuthRequired(f"{_PLATFORM_DISPLAY.get(platform, platform)} session was rejected while downloading")
        response.raise_for_status()
        content_type = (response.headers.get("content-type") or "").lower()
        if "text/html" in content_type or "application/xhtml" in content_type:
            raise RuntimeError("Platform returned an HTML/login page instead of a model file")
        total = 0
        with open(path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=262144):
                if not chunk:
                    continue
                total += len(chunk)
                if total > 500 * 1024 * 1024:
                    raise RuntimeError("Download exceeds 500 MB safety limit")
                fh.write(chunk)
    except Exception:
        with suppress(OSError):
            os.remove(path)
        raise
    finally:
        response.close()
        close_session = getattr(session, "close", None)
        if callable(close_session):
            close_session()
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
.accounts{display:grid;grid-template-columns:repeat(5,minmax(160px,1fr));gap:8px;margin:10px 0}.account{border:1px solid var(--orca-border,#444);border-radius:7px;padding:8px}.account strong{display:block;font-size:.86em}.auth-state{display:block;font-size:.75em;color:var(--orca-muted,#999);margin:3px 0 7px}.source-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:10px 0 6px}.source-head strong{font-size:.9em}.source-tools{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.source-tools button{padding:4px 8px;font-size:.76em}.source-count{font-size:.76em;color:var(--orca-muted,#999)}.platforms{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:7px;margin-bottom:12px}.portal-option{display:flex;align-items:center;gap:8px;padding:8px 9px;border:1px solid var(--orca-border,#444);border-radius:6px;font-size:.84em;color:var(--orca-fg,#eee);cursor:pointer;user-select:none}.portal-option:hover{border-color:var(--orca-accent,#4a9eff)}.portal-option input{margin:0;accent-color:var(--orca-accent,#4a9eff)}
#results{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}.card{border:1px solid var(--orca-border,#444);border-radius:8px;padding:10px;cursor:pointer}.card:hover{border-color:var(--orca-accent,#4a9eff)}.card img{width:100%;height:110px;object-fit:cover;border-radius:4px;background:#333}.card h3{font-size:.9em;margin:6px 0 2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.author{font-size:.78em;color:var(--orca-muted,#888)}.license-badge{display:inline-block;padding:1px 7px;border-radius:3px;font-size:.72em;margin-top:4px;background:#444}.license-cc{background:#1a5c2a;color:#8f8}.license-arr{background:#5c3a1a;color:#fc6}
.panel{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);width:min(650px,calc(100% - 32px));max-height:70vh;overflow:auto;z-index:20;padding:14px 34px 14px 14px;border:1px solid var(--orca-border,#444);border-radius:8px;background:var(--orca-bg,#1e1e1e);box-shadow:0 6px 28px rgba(0,0,0,.55);display:none}.panel.active{display:block}.close{position:absolute;right:8px;top:6px;background:none!important;font-size:1.35em;padding:2px 6px}.panel p{font-size:.86em;color:var(--orca-muted,#aaa);margin:6px 0}.panel a{color:var(--orca-accent,#4a9eff)}.responsibility{border-left:3px solid var(--orca-border,#444);padding:8px 10px;margin:10px 0;font-size:.78em;color:var(--orca-muted,#888)}
.modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.58);z-index:29;display:none}.modal-backdrop.active{display:block}.auth-modal{position:fixed;z-index:30;left:50%;top:50%;transform:translate(-50%,-50%);width:min(520px,calc(100% - 32px));background:var(--orca-bg,#1e1e1e);border:1px solid var(--orca-border,#555);border-radius:9px;padding:16px;display:none}.auth-modal.active{display:block}.field{margin:8px 0}.field label{display:block;font-size:.78em;color:var(--orca-muted,#999);margin-bottom:3px}.field input{width:100%;padding:8px;border:1px solid var(--orca-border,#555);background:var(--orca-bg,#222);color:inherit;border-radius:5px}.auth-note{font-size:.79em;color:var(--orca-muted,#aaa);line-height:1.4}.button-row{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}.file-list{max-height:46vh;overflow:auto;border:1px solid var(--orca-border,#555);border-radius:6px;margin:10px 0}.file-choice{display:flex;align-items:flex-start;gap:9px;padding:9px 10px;border-bottom:1px solid var(--orca-border,#444);cursor:pointer}.file-choice:last-child{border-bottom:0}.file-choice input{margin-top:2px}.file-choice span{overflow-wrap:anywhere}.file-tools{display:flex;gap:7px;margin:8px 0}.file-count{font-size:.8em;color:var(--orca-muted,#999)}#status{margin-top:10px;color:var(--orca-muted,#999);font-size:.8em}
@media(max-width:680px){.accounts{grid-template-columns:1fr}}
</style>
<h1 style="margin:0;font-size:1.25em">&#128269; 3D Model Search</h1>
<div class="accounts">
  <div class="account"><strong>MakerWorld (Bambu)</strong><span id="auth-makerworld" class="auth-state">Checking...</span><button class="secondary" onclick="openAuth('makerworld')">Account</button></div>
  <div class="account"><strong>Nexprint (Elegoo)</strong><span id="auth-nexprint" class="auth-state">Checking...</span><button class="secondary" onclick="openAuth('nexprint')">Account</button></div>
  <div class="account"><strong>Makeronline (Anycubic)</strong><span id="auth-makeronline" class="auth-state">Checking...</span><button class="secondary" onclick="openAuth('makeronline')">Account</button></div>
  <div class="account"><strong>Cults3D</strong><span id="auth-cults3d" class="auth-state">Checking...</span><button class="secondary" onclick="openAuth('cults3d')">Account</button></div>
  <div class="account"><strong>GrabCAD</strong><span id="auth-grabcad" class="auth-state">Checking...</span><button class="secondary" onclick="openAuth('grabcad')">Account</button></div>
</div>
<div class="search-row"><input id="query" placeholder="Search for 3D models..."><button id="search-btn" onclick="doSearch()">Search</button></div>
<div class="source-head"><strong>Search portals</strong><div class="source-tools"><button class="secondary" onclick="setAllPortals(true)">Select all</button><button class="secondary" onclick="setAllPortals(false)">Select none</button><span id="source-count" class="source-count"></span></div></div>
<div class="platforms" id="search-portals">
<label class="portal-option"><input id="portal-thingiverse" class="portal-search" type="checkbox" checked data-platform="thingiverse"> Thingiverse</label>
<label class="portal-option"><input id="portal-cults3d" class="portal-search" type="checkbox" checked data-platform="cults3d"> Cults3D</label>
<label class="portal-option"><input id="portal-myminifactory" class="portal-search" type="checkbox" checked data-platform="myminifactory"> MyMiniFactory</label>
<label class="portal-option"><input id="portal-thangs" class="portal-search" type="checkbox" checked data-platform="thangs"> Thangs</label>
<label class="portal-option"><input id="portal-makeronline" class="portal-search" type="checkbox" checked data-platform="makeronline"> Makeronline</label>
<label class="portal-option"><input id="portal-crealitycloud" class="portal-search" type="checkbox" checked data-platform="crealitycloud"> Creality Cloud</label>
<label class="portal-option"><input id="portal-nexprint" class="portal-search" type="checkbox" checked data-platform="nexprint"> Nexprint</label>
<label class="portal-option"><input id="portal-grabcad" class="portal-search" type="checkbox" checked data-platform="grabcad"> GrabCAD</label>
<label class="portal-option"><input id="portal-printables" class="portal-search" type="checkbox" checked data-platform="printables"> Printables</label>
<label class="portal-option"><input id="portal-makerworld" class="portal-search" type="checkbox" checked data-platform="makerworld"> MakerWorld</label>
</div>
<div id="results"></div>
<div id="detail" class="panel"><button class="close" onclick="closeDetail()">&times;</button><h2 id="det-name"></h2><p id="det-author"></p><p id="det-platform"></p><p id="det-url"></p><p id="det-license"></p><p id="det-summary"></p><p class="responsibility">Downloads use your own account session and the platform's own file URL. The plugin does not host or redistribute models. You remain responsible for the model license and the platform terms.</p><button id="det-import-btn" onclick="doImport()">Import into OrcaSlicer</button><button class="secondary" onclick="doDownload()">Open in browser</button></div>
<div id="modal-bg" class="modal-backdrop" onclick="closeTopModal()"></div>
<div id="auth-modal" class="auth-modal">
  <h2 id="auth-title" style="margin:0 0 5px;font-size:1.05em">Account</h2>
  <div id="auth-note" class="auth-note"></div>
  <div id="email-field" class="field"><label>Email / account</label><input id="auth-email" autocomplete="username"></div>
  <div id="password-field" class="field"><label>Password (never saved)</label><input id="auth-password" type="password" autocomplete="current-password"></div>
  <div id="code-field" class="field" style="display:none"><label>Verification code</label><input id="auth-code" autocomplete="one-time-code"></div>
  <div class="field"><label id="token-label">Session/access token (alternative)</label><input id="auth-token" type="password" autocomplete="off"></div>
  <div class="button-row"><button id="auth-submit" onclick="submitAuth()">Connect</button><button id="official-login" class="secondary" onclick="openOfficialLogin()">Open official login</button><button id="import-anycubic" class="secondary" style="display:none" onclick="importAnycubic()">Import from Anycubic Slicer Next</button><button id="auth-logout" class="danger" onclick="logoutAuth()">Forget session</button><button class="secondary" onclick="closeAuth()">Cancel</button></div>
</div>
<div id="file-modal" class="auth-modal">
  <h2 style="margin:0 0 5px;font-size:1.05em">Choose files to import</h2>
  <div class="auth-note">This model contains multiple downloadable files. Select the files that should be downloaded and added to the current OrcaSlicer project.</div>
  <div class="file-tools"><button class="secondary" onclick="setAllFiles(true)">Select all</button><button class="secondary" onclick="setAllFiles(false)">Select none</button><span id="file-count" class="file-count"></span></div>
  <div id="file-list" class="file-list"></div>
  <div class="button-row"><button id="file-import" onclick="confirmFileImport()">Import selected</button><button class="secondary" onclick="closeFilePicker()">Cancel</button></div>
</div>
<div id="status">Ready.</div>
<script>
var selectedModel=null, searching=false, authPlatform=null, authStates={}, pendingImport=null;
var $=function(id){return document.getElementById(id)};
function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;")}
function platformKey(display){return {MakerWorld:'makerworld',Nexprint:'nexprint',Makeronline:'makeronline',Printables:'printables',Thingiverse:'thingiverse',Cults3D:'cults3d',MyMiniFactory:'myminifactory',Thangs:'thangs','Creality Cloud':'crealitycloud',GrabCAD:'grabcad'}[display]||String(display||'').toLowerCase()}
function isAuthed(model){if(!model||!model.requires_auth)return true;var s=authStates[platformKey(model.platform)];return !!(s&&s.authenticated)}
function updateAuth(states){authStates=states||{};['makerworld','nexprint','makeronline','cults3d','grabcad'].forEach(function(p){var s=authStates[p]||{};$("auth-"+p).textContent=s.authenticated?("Connected: "+(s.label||'session')):'Not connected'});if(selectedModel)showDetail(selectedModel,false)}
var PORTAL_PREF_KEY='orca-model-search-portals-v1';
function selectedPortals(){var ps=[];document.querySelectorAll('.portal-search:checked').forEach(function(x){ps.push(x.dataset.platform)});return ps}
function updatePortalCount(){var all=document.querySelectorAll('.portal-search');var checked=document.querySelectorAll('.portal-search:checked');$('source-count').textContent=checked.length+' / '+all.length+' selected'}
function savePortalSelection(){try{localStorage.setItem(PORTAL_PREF_KEY,JSON.stringify(selectedPortals()))}catch(e){}}
function restorePortalSelection(){try{var raw=localStorage.getItem(PORTAL_PREF_KEY);if(raw){var saved=JSON.parse(raw);if(Array.isArray(saved)){var set={};saved.forEach(function(p){set[p]=true});document.querySelectorAll('.portal-search').forEach(function(x){x.checked=!!set[x.dataset.platform]})}}}catch(e){}updatePortalCount()}
function setAllPortals(value){document.querySelectorAll('.portal-search').forEach(function(x){x.checked=!!value});updatePortalCount();savePortalSelection()}
$('search-portals').addEventListener('change',function(e){if(e.target&&e.target.classList.contains('portal-search')){updatePortalCount();savePortalSelection()}});
function doSearch(){if(searching)return;var q=$('query').value.trim();if(!q)return;var ps=selectedPortals();if(!ps.length){$('status').textContent='Select at least one search portal.';return}searching=true;$('search-btn').disabled=true;$('search-btn').textContent='Searching...';$('status').textContent='Searching '+ps.length+' portal(s)...';closeDetail();orca.postMessage({action:'search',query:q,platforms:ps})}
$('query').addEventListener('keydown',function(e){if(e.key==='Enter')doSearch()});
function renderResults(models){window._results=models||[];var html='';window._results.forEach(function(m,i){html+='<div class="card" data-idx="'+i+'"><img src="'+esc(m.thumbnail_url||'')+'"><h3 title="'+esc(m.name)+'">'+esc(m.name)+'</h3><div class="author">'+esc(m.author)+' · '+esc(m.platform)+'</div><span class="license-badge '+licenseClass(m.license)+'">'+esc(m.license||'Unknown')+'</span></div>'});$('results').innerHTML=html;$('status').textContent=window._results.length+' result(s)'}
$('results').addEventListener('click',function(e){var c=e.target.closest&&e.target.closest('.card');if(!c)return;var m=window._results[parseInt(c.dataset.idx,10)];if(m)showDetail(m,true)});
function showDetail(m,open){selectedModel=m;$('det-name').textContent=m.name;$('det-author').innerHTML='<strong>Author:</strong> '+esc(m.author);$('det-platform').innerHTML='<strong>Platform:</strong> '+esc(m.platform);$('det-license').innerHTML='<strong>License:</strong> <span class="license-badge '+licenseClass(m.license)+'">'+esc(m.license||'Unknown')+'</span>';$('det-summary').textContent=m.license_summary||'No license information available.';$('det-url').innerHTML=m.url?'<strong>Model page:</strong> <a href="'+esc(m.url)+'">'+esc(m.url)+'</a>':'';var b=$('det-import-btn');b.disabled=false;b.textContent=(m.requires_auth&&!isAuthed(m))?('Log in to '+m.platform+' & import'):'Import into OrcaSlicer';if(open!==false)$('detail').classList.add('active')}
function closeDetail(){$('detail').classList.remove('active')}
document.addEventListener('pointerdown',function(e){var d=$('detail');if(!d||!d.classList.contains('active'))return;if(d.contains(e.target))return;closeDetail()},true);
$('detail').addEventListener('click',function(e){var a=e.target.closest&&e.target.closest('a[href]');if(!a)return;e.preventDefault();openExternal(a.getAttribute('href'))});
function licenseClass(l){if(/CC|Creative Commons|CC0|Public Domain/i.test(l||''))return'license-cc';if(/All Rights Reserved|Standard Digital|Exclusive/i.test(l||''))return'license-arr';return''}
function openExternal(url){orca.postMessage({action:'open_external',url:url})}
function doDownload(){if(selectedModel)openExternal(selectedModel.url||selectedModel.download_url)}
function doImport(){if(!selectedModel)return;if(selectedModel.requires_auth&&!isAuthed(selectedModel)){pendingImport=selectedModel;openAuth(platformKey(selectedModel.platform));return}var b=$('det-import-btn');b.disabled=true;b.textContent='Resolving...';$('status').textContent='Resolving files...';orca.postMessage({action:'resolve_import',model:selectedModel})}
function openAuth(p){authPlatform=p;$('auth-modal').classList.add('active');$('modal-bg').classList.add('active');$('auth-password').value='';$('auth-code').value='';$('auth-token').value='';$('code-field').style.display='none';$('import-anycubic').style.display=p==='makeronline'?'':'none';var tokenOnly=(p==='nexprint'||p==='makeronline'||p==='cults3d'||p==='grabcad');$('password-field').style.display=tokenOnly?'none':'';$('email-field').style.display=tokenOnly?'none':'';var title={makerworld:'MakerWorld / Bambu account',nexprint:'Nexprint / Elegoo account',makeronline:'Makeronline / Anycubic account',cults3d:'Cults3D account',grabcad:'GrabCAD account'}[p]||'Account';$('auth-title').textContent=title;$('token-label').textContent=p==='nexprint'?'Nexprint auth_token cookie value':p==='grabcad'?'GrabCAD Cookie header / session cookies':p==='cults3d'?'Cults3D Cookie header / session cookies':'Session/access token (alternative)';$('auth-note').textContent=p==='makerworld'?'Use Bambu email/password or paste an existing Bambu Cloud access token. MFA verification codes are supported.':p==='nexprint'?'Sign in on the official Nexprint site, then paste the auth_token session cookie. The plugin never asks for or stores your Nexprint password.':p==='grabcad'?'GrabCAD Community Library downloads require membership. Sign in on the official GrabCAD page, then paste the Cookie request header (or the session cookie string). The plugin never asks for your GrabCAD password.':p==='cults3d'?'Cults3D requires an account even for free downloads. Sign in on the official Cults3D page, then paste the Cookie request header/session cookies. The plugin never asks for your Cults3D password.':'Makeronline no longer uses the legacy direct password endpoint. Sign in with Anycubic Slicer Next and click Import from Anycubic Slicer Next, or paste an existing access token.';var st=authStates[p]||{};$('auth-logout').style.display=st.authenticated?'':'none'}
function syncBackdrop(){var active=$('auth-modal').classList.contains('active')||$('file-modal').classList.contains('active');$('modal-bg').classList.toggle('active',active)}
function closeAuth(){$('auth-modal').classList.remove('active');$('auth-password').value='';$('auth-token').value='';syncBackdrop()}
function closeFilePicker(){$('file-modal').classList.remove('active');syncBackdrop();var b=$('det-import-btn');if(b){b.disabled=false;b.textContent='Import into OrcaSlicer'}}
function closeTopModal(){if($('file-modal').classList.contains('active'))closeFilePicker();else closeAuth()}
function updateFileCount(){var all=document.querySelectorAll('#file-list input[type=checkbox]');var checked=document.querySelectorAll('#file-list input[type=checkbox]:checked');$('file-count').textContent=checked.length+' / '+all.length+' selected';$('file-import').disabled=checked.length===0}
function showFilePicker(files){var html='';(files||[]).forEach(function(f){html+='<label class="file-choice"><input type="checkbox" checked value="'+Number(f.index)+'" onchange="updateFileCount()"><span>'+esc(f.name||('File '+(Number(f.index)+1)))+'</span></label>'});$('file-list').innerHTML=html;$('file-modal').classList.add('active');syncBackdrop();updateFileCount()}
function setAllFiles(value){document.querySelectorAll('#file-list input[type=checkbox]').forEach(function(x){x.checked=!!value});updateFileCount()}
function confirmFileImport(){var selected=[];document.querySelectorAll('#file-list input[type=checkbox]:checked').forEach(function(x){selected.push(parseInt(x.value,10))});if(!selected.length)return;$('file-import').disabled=true;$('status').textContent='Downloading selected files...';$('file-modal').classList.remove('active');syncBackdrop();orca.postMessage({action:'import_selected',indices:selected})}
function submitAuth(){var token=$('auth-token').value.trim(),email=$('auth-email').value.trim(),password=$('auth-password').value,code=$('auth-code').value.trim();if(authPlatform==='nexprint'&&!token){$('status').textContent='Nexprint: paste auth_token after signing in.';return}if(authPlatform==='makeronline'&&!token){$('status').textContent='Makeronline: import the Anycubic Slicer Next session or paste an access token.';return}if(authPlatform==='grabcad'&&!token){$('status').textContent='GrabCAD: paste the Cookie header/session cookies after signing in.';return}if(authPlatform==='cults3d'&&!token){$('status').textContent='Cults3D: paste the Cookie header/session cookies after signing in.';return}orca.postMessage({action:'auth_login',platform:authPlatform,token:token,email:email,password:password,code:code});$('auth-submit').disabled=true;$('status').textContent='Saving session...'}
function logoutAuth(){orca.postMessage({action:'auth_logout',platform:authPlatform});closeAuth()}
function openOfficialLogin(){orca.postMessage({action:'auth_open_login',platform:authPlatform})}
function importAnycubic(){orca.postMessage({action:'auth_import_anycubic'});$('status').textContent='Looking for Anycubic Slicer Next session...'}
orca.onMessage(function(msg){msg=msg||{};if(msg.action==='results'){searching=false;$('search-btn').disabled=false;$('search-btn').textContent='Search';renderResults(msg.results||[])}else if(msg.action==='auth_status'||msg.action==='auth_changed'){updateAuth(msg.states||{});$('auth-submit').disabled=false;if(msg.action==='auth_changed'){closeAuth();$('status').textContent=msg.message||'Account session updated.';if(pendingImport&&isAuthed(pendingImport)){var m=pendingImport;pendingImport=null;selectedModel=m;doImport()}}}else if(msg.action==='auth_challenge'){$('auth-submit').disabled=false;$('code-field').style.display='';$('status').textContent=msg.message||'Verification code required.'}else if(msg.action==='auth_required'){$('det-import-btn').disabled=false;$('det-import-btn').textContent='Import into OrcaSlicer';$('status').textContent=msg.message||'Login required.';pendingImport=msg.model||selectedModel;openAuth(msg.platform)}else if(msg.action==='file_choices'){showFilePicker(msg.files||[]);$('status').textContent='Select one or more files to import.'}else if(msg.action==='status'){$('status').textContent=msg.message}else if(msg.action==='imported'){closeFilePicker();$('det-import-btn').disabled=false;$('det-import-btn').textContent='Import into OrcaSlicer';$('status').textContent='Imported '+msg.count+' file(s) into the current OrcaSlicer project.'}else if(msg.action==='downloaded_only'){closeFilePicker();$('det-import-btn').disabled=false;$('det-import-btn').textContent='Import into OrcaSlicer';$('status').textContent='Downloaded '+msg.count+' file(s) to '+msg.dir+'. '+msg.message}else if(msg.action==='browser_required'){$('det-import-btn').disabled=false;$('det-import-btn').textContent='Import into OrcaSlicer';$('status').textContent=msg.message||'This model must be downloaded in the browser.';if(msg.url)openExternal(msg.url)}else if(msg.action==='opened'){$('status').textContent='Opened in your browser.'}else if(msg.action==='activate_search'){var q=$('query');if(q){q.focus();q.select()}}else if(msg.action==='error'){searching=false;$('search-btn').disabled=false;$('search-btn').textContent='Search';$('auth-submit').disabled=false;if($('det-import-btn')){$('det-import-btn').disabled=false;$('det-import-btn').textContent='Import into OrcaSlicer'}if($('file-import'))$('file-import').disabled=false;$('status').textContent='Error: '+msg.message}});
orca.postMessage({action:'auth_status'});
restorePortalSelection();
setTimeout(function(){var q=$('query');if(q)q.focus()},0);
</script></body></html>"""


if orca is not None:
    _orca = orca

    class SearchEngineScript(_orca.script.ScriptPluginCapabilityBase):
        win = None

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.auth = AuthManager()
            self._pending_import_lock = threading.RLock()
            self._pending_import_model = None
            self._pending_import_files = []

        def get_name(self):
            return "Search 3D Models"

        def execute(self):
            if self.win is not None and self.win.is_open():
                # The public UiWindow API has no focus/raise method. Reuse the
                # existing window and move keyboard focus back to its search box.
                self.win.post({"action": "activate_search"})
                return _orca.ExecutionResult.success()
            self.win = _orca.host.ui.create_window(
                html=PAGE,
                title="Search 3D Models",
                width=980,
                height=720,
                on_message=self.on_message,
                on_close=self.on_close,
            )
            return _orca.ExecutionResult.success()

        def on_close(self):
            self.win = None

        def _post(self, msg):
            if self.win is not None and self.win.is_open():
                self.win.post(msg)

        def _post_auth(self, action="auth_status", message=""):
            self._post({"action": action, "states": self.auth.status(), "message": message})

        @staticmethod
        def _spawn(target, *args):
            threading.Thread(target=target, args=args, daemon=True).start()

        def on_message(self, msg):
            msg = msg or {}
            action = msg.get("action", "")
            if action == "auth_status":
                self._post_auth()
                return
            if action == "auth_logout":
                self.auth.logout(msg.get("platform", ""))
                self._post_auth("auth_changed", "Session removed.")
                return
            if action == "auth_open_login":
                url = _AUTH_LOGIN_URL.get(msg.get("platform", ""), "")
                if url:
                    self._spawn(self._open_external, url)
                return

            background = {
                "search": (self._do_search, msg),
                "import_selected": (self._import_selected, msg.get("indices") or []),
                "open_external": (self._open_external, msg.get("url", "")),
                "auth_login": (self._do_auth_login, msg),
                "auth_import_anycubic": (self._do_import_anycubic,),
            }
            if action == "resolve_import":
                model = msg.get("model") or {}
                if model:
                    self._spawn(self._resolve_import, model)
                return
            task = background.get(action)
            if task:
                self._spawn(*task)

        def _do_auth_login(self, msg):
            platform = msg.get("platform", "")
            try:
                if platform not in _AUTH_TOKEN_FIELD:
                    raise AuthError("Unknown platform")
                token = (msg.get("token") or "").strip()
                email = (msg.get("email") or "").strip()
                code = (msg.get("code") or "").strip()
                if token:
                    self.auth.save_token(platform, token, label=email or "Connected session")
                elif platform == "makerworld":
                    self.auth.login_makerworld(email, password=msg.get("password") or "", code=code)
                else:
                    raise AuthError(_TOKEN_ONLY_AUTH_ERROR[platform])
            except VerificationRequired as exc:
                self._post({"action": "auth_challenge", "platform": platform, "message": str(exc)})
                return
            except Exception as exc:
                self._post({"action": "error", "message": str(exc)})
                return
            finally:
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
            for platform in dict.fromkeys(msg.get("platforms", [])):
                adapter = _SEARCHERS.get(platform)
                if not adapter:
                    continue
                try:
                    items = adapter.search(query, self.auth)
                    for item in items:
                        item["_platform_key"] = platform
                        item["authenticated"] = (not item.get("requires_auth")) or self.auth.authenticated(platform)
                        item["importable"] = callable(getattr(adapter, "get_files", None))
                    results.extend(items)
                except Exception as exc:
                    errors.append(f"{platform}: {exc}")
            self._post({"action": "results", "results": results})
            if errors:
                self._post({"action": "status", "message": " | ".join(errors)})

        def _resolver_for_model(self, model):
            platform_key = _platform_key_for_model(model)
            adapter = _SEARCHERS.get(platform_key)
            resolver = getattr(adapter, "get_files", None) if adapter is not None else None
            return platform_key, resolver if callable(resolver) else None

        def _post_auth_required(self, platform_key, platform_name, model, message):
            self._post({
                "action": "auth_required",
                "platform": platform_key,
                "message": message,
                "model": model,
            })

        def _resolve_import(self, model):
            platform_key, resolver = self._resolver_for_model(model)
            platform_name = _PLATFORM_DISPLAY.get(platform_key, model.get("platform", ""))
            if resolver is None:
                self._post({"action": "error", "message": f"Import is not supported for {platform_name or 'this platform'}"})
                return
            if model.get("requires_auth") and not self.auth.authenticated(platform_key):
                self._post_auth_required(platform_key, platform_name, model, f"Log in to {platform_name} before importing.")
                return
            try:
                normalized = _normalize_download_files(resolver(model, self.auth))
            except AuthRequired as exc:
                self.auth.logout(platform_key)
                self._post_auth("auth_changed", f"{platform_name} session expired.")
                self._post_auth_required(platform_key, platform_name, model, str(exc))
                return
            except BrowserRequired as exc:
                self._post({"action": "browser_required", "message": str(exc), "url": exc.url or model.get("url", "")})
                return
            except Exception as exc:
                self._post({"action": "error", "message": f"Could not list files: {exc}"})
                return
            if not normalized:
                self._post({"action": "error", "message": "The platform returned no valid downloadable file URLs."})
                return
            if len(normalized) == 1:
                self._download_and_import(model, normalized)
                return
            with self._pending_import_lock:
                self._pending_import_model = dict(model)
                self._pending_import_files = normalized
            self._post({
                "action": "file_choices",
                "files": [{"index": index, "name": item["name"]} for index, item in enumerate(normalized)],
            })

        def _import_selected(self, indices):
            try:
                selected_indices = _selected_file_indices(indices)
            except (TypeError, ValueError):
                self._post({"action": "error", "message": "Invalid file selection."})
                return
            with self._pending_import_lock:
                model = self._pending_import_model
                files = list(self._pending_import_files)
                self._pending_import_model = None
                self._pending_import_files = []
            if not model or not files:
                self._post({"action": "error", "message": "File selection expired. Press Import again to refresh the file list."})
                return
            selected = [files[index] for index in selected_indices if index < len(files)]
            if not selected:
                self._post({"action": "error", "message": "Select at least one file to import."})
                return
            self._download_and_import(model, selected)

        def _download_and_import(self, model, files):
            platform_name = model.get("platform", "")
            platform_key = _platform_key_for_model(model)
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

            try:
                load_paths = _expand_archives(paths, dest_dir)
            except Exception as exc:
                self._post({"action": "error", "message": f"Downloaded files, but archive extraction failed: {exc}"})
                return
            if not load_paths:
                self._post({"action": "downloaded_only", "count": len(paths), "dir": dest_dir,
                            "message": "No directly loadable STL/3MF/CAD file was found in the download."})
                return

            self._post({"action": "status", "message": f"Adding {len(load_paths)} file(s) to the current OrcaSlicer project..."})
            ok, detail = _load_in_orca(load_paths)
            if ok:
                self._post({"action": "imported", "count": len(load_paths), "dir": dest_dir})
            else:
                self._post({"action": "downloaded_only", "count": len(load_paths), "dir": dest_dir, "message": detail})

        def _open_external(self, url):
            if not _is_http_url(url):
                self._post({"action": "error", "message": "Refusing to open non-HTTP URL."})
                return
            try:
                if sys.platform == "darwin":
                    subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif os.name == "nt":
                    os.startfile(url)
                else:
                    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._post({"action": "opened", "url": url})
            except Exception as exc:
                self._post({"action": "error", "message": f"Could not open browser: {exc}"})

    @_orca.plugin
    class SearchEnginePlugin(_orca.base):
        def register_capabilities(self):
            _orca.register_capability(SearchEngineScript)
