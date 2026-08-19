# /// script
# requires-python = ">=3.12"
# dependencies = ["requests"]
#
# [tool.orcaslicer.plugin]
# name = "3D Model Search Engine"
# description = "Search, sort, and import 3D-printable models from community model portals."
# author = "Tommaso Bianchi"
# version = "0.7.0"
# ///

import html
import ipaddress
import json
import math
import os
import re
import socket
import subprocess  # nosec B404
import sys
import threading
import time
import urllib.parse
import webbrowser
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, ClassVar

try:
    import orca
except ImportError:
    orca = None


_BROWSER_UA = (
    "OrcaSlicer-Model-Search-Plugin/0.7.0 "
    "(+https://github.com/ocidburn/OrcaSlicer-Model-Search-Plugin)"
)
_MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
_MAX_REDIRECTS = 5

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
    "MyMiniFactory Digital File Store License": "MyMiniFactory store license. Check the model page for the current usage terms.",
    "Standard Digital File Store License": "Non-commercial personal use only. Sharing files or derivatives and remixing are not allowed; check MyMiniFactory for the complete terms.",
    "MakerWorld Exclusive License": "MakerWorld exclusive license. Check the model page for exact terms.",
    "Exclusive": "Platform exclusive license. Check the model page for exact terms.",
}

_CC_LICENSE_URLS = {
    "CC BY": "https://creativecommons.org/licenses/by/4.0/",
    "CC BY-SA": "https://creativecommons.org/licenses/by-sa/4.0/",
    "CC BY-NC": "https://creativecommons.org/licenses/by-nc/4.0/",
    "CC BY-NC-SA": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
    "CC BY-ND": "https://creativecommons.org/licenses/by-nd/4.0/",
    "CC BY-NC-ND": "https://creativecommons.org/licenses/by-nc-nd/4.0/",
    "CC0": "https://creativecommons.org/publicdomain/zero/1.0/",
}

_LICENSE_ALIASES = {
    "creative commons - attribution": "CC BY",
    "creative commons - attribution - share alike": "CC BY-SA",
    "creative commons - attribution - non-commercial": "CC BY-NC",
    "creative commons - attribution - non-commercial - share alike": "CC BY-NC-SA",
    "creative commons - attribution - no derivatives": "CC BY-ND",
    "creative commons - attribution - non-commercial - no derivatives": "CC BY-NC-ND",
    "creative commons - public domain dedication": "CC0",
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
    name = _LICENSE_ALIASES.get(name.casefold(), name)
    url = url or _CC_LICENSE_URLS.get(name, "")
    summary = LICENSE_DESCRIPTIONS.get(name, "")
    if not summary:
        upper = name.upper()
        if "CC" in upper:
            summary = (
                "Creative Commons license. See the model page for the complete terms."
            )
        elif "GPL" in upper:
            summary = (
                "GNU General Public License. See the model page for the complete terms."
            )
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


class PlatformSpec:
    """Single source of truth for search, import, and authentication behavior."""

    __slots__ = (
        "adapter",
        "auth_hosts",
        "auth_mode",
        "cookie_domain",
        "cookie_name",
        "display",
        "key",
        "login_url",
        "referer",
        "search_page_size",
    )

    def __init__(
        self,
        key,
        display,
        adapter,
        *,
        auth_hosts=(),
        auth_mode="",
        login_url="",
        referer="",
        cookie_domain="",
        cookie_name="",
        search_page_size=0,
    ):
        self.key = key
        self.display = display
        self.adapter = adapter
        self.auth_hosts = tuple(auth_hosts)
        self.auth_mode = auth_mode
        self.login_url = login_url
        self.referer = referer
        self.cookie_domain = cookie_domain
        self.cookie_name = cookie_name
        self.search_page_size = max(0, int(search_page_size))

    @property
    def requires_auth(self):
        return bool(self.auth_mode)

    @property
    def paginated_search(self):
        return self.search_page_size > 0


class SearchPage(list):
    """List-compatible search response with optional paging metadata."""

    def __init__(self, items=(), *, total=None, has_more=None):
        super().__init__(items)
        self.total = _number(total, integer=True)
        self.has_more = has_more


def _search_page_number(options):
    try:
        page = int((options or {}).get("page", 1))
    except (AttributeError, TypeError, ValueError):
        page = 1
    return min(max(page, 1), 100)


def _search_page_result(items, page, page_size, total=None):
    total = _number(total, integer=True)
    has_more = page * page_size < total if total is not None else len(items) >= page_size
    return SearchPage(items, total=total, has_more=has_more)


_COMMON_RESULT_FIELDS = (
    "downloads",
    "likes",
    "rating",
    "rating_count",
    "views",
    "makes",
    "published_at",
    "price",
    "is_free",
)


def _number(value, integer=False):
    """Return a finite number or None for inconsistent catalog values."""
    if value in (None, "", "null", "None") or isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return int(parsed) if integer else parsed


def _first(value: Any, default: Any = "") -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else default
    return value if value not in (None, "") else default


def _coalesce(*values: Any, default: Any = "") -> Any:
    return next((value for value in values if value not in (None, "")), default)


def _strip_html(value):
    return _clean_web_text(re.sub(r"<[^>]+>", " ", str(value or "")))


def _timestamp(value):
    numeric = _number(value)
    if numeric is not None:
        return numeric / 1000.0 if numeric > 10_000_000_000 else numeric
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _normalize_result(item, source_rank=0):
    normalized = dict(item or {})
    for field in _COMMON_RESULT_FIELDS:
        normalized.setdefault(field, None)
    for field in ("downloads", "likes", "rating_count", "views", "makes"):
        normalized[field] = _number(normalized.get(field), integer=True)
    normalized["rating"] = _number(normalized.get("rating"))
    normalized["price"] = _number(normalized.get("price"))
    normalized["_published_sort"] = _timestamp(normalized.get("published_at"))
    normalized["source_rank"] = int(source_rank)
    normalized.setdefault("result_type", "model")
    normalized.setdefault("direct_import", True)
    return normalized


def _add_popularity_scores(results):
    """Create a platform-relative score; raw counters are not cross-site units."""
    by_platform = {}
    for item in results:
        raw = 0.0
        for field, weight in (
            ("downloads", 1.0),
            ("likes", 1.5),
            ("views", 0.2),
            ("makes", 2.0),
        ):
            value = item.get(field)
            if value is not None and value >= 0:
                raw += weight * math.log1p(value)
        rating = item.get("rating")
        if rating is not None:
            raw += max(0.0, rating) * 0.75
        item["_popularity_raw"] = raw
        by_platform.setdefault(item.get("platform", ""), []).append(raw)
    for item in results:
        values = sorted(by_platform.get(item.get("platform", ""), ()))
        raw = item.get("_popularity_raw", 0.0)
        if not values or raw <= 0:
            item["popularity"] = None
            continue
        item["popularity"] = (
            100.0 if len(values) == 1 else 100.0 * values.index(raw) / (len(values) - 1)
        )


def _filter_results(results, options):
    if options.get("free_only"):
        results = [item for item in results if item.get("is_free") is True]
    if options.get("direct_only"):
        results = [
            item
            for item in results
            if item.get("direct_import") and item.get("result_type") == "model"
        ]
    return results


def _sort_results(results, sort):
    descending_fields = {
        "popularity": "popularity",
        "downloads": "downloads",
        "likes": "likes",
        "rating": "rating",
        "newest": "_published_sort",
        "makes": "makes",
    }
    field = descending_fields.get(sort)
    if field:
        present = [item for item in results if item.get(field) is not None]
        missing = [item for item in results if item.get(field) is None]
        present.sort(key=lambda item: item[field], reverse=True)
        results = present + missing
    elif sort in ("name", "platform"):
        results.sort(
            key=lambda item: (
                str(item.get(sort) or "").casefold(),
                item["source_rank"],
            )
        )
    return results


def _filter_and_sort_results(results, options=None):
    options = options if isinstance(options, dict) else {}
    normalized = [_normalize_result(item, index) for index, item in enumerate(results)]
    _add_popularity_scores(normalized)
    normalized = _filter_results(normalized, options)
    normalized = _sort_results(normalized, str(options.get("sort") or "relevance"))
    for item in normalized:
        item.pop("_popularity_raw", None)
        item.pop("_published_sort", None)
    return normalized


def _result_identity(item):
    platform = str(item.get("_platform_key") or item.get("platform") or "").casefold()
    identifier = _coalesce(
        item.get("_thing_id"),
        item.get("_model_id"),
        item.get("_mold_id"),
        item.get("url"),
        item.get("download_url"),
        default="",
    )
    if not identifier:
        identifier = "|".join(
            (
                str(item.get("name") or "").casefold(),
                str(item.get("author") or "").casefold(),
            )
        )
    return f"{platform}|{identifier}"


def _merge_unique_results(existing, incoming):
    merged = list(existing)
    seen = {_result_identity(item) for item in merged}
    added = 0
    for item in incoming:
        identity = _result_identity(item)
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(item)
        added += 1
    return merged, added


_PLATFORMS = {}
_PLATFORMS_BY_DISPLAY = {}


def _platform(key):
    return _PLATFORMS.get(key)


def _platform_for_display(display):
    return _PLATFORMS_BY_DISPLAY.get(display)


def _platform_for_model(model):
    spec = _platform(str(model.get("_platform_key") or ""))
    return spec or _platform_for_display(model.get("platform", ""))


def _display_name(key):
    spec = _platform(key)
    return spec.display if spec else key


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

    def _write(self, data):
        folder = os.path.dirname(self.path)
        _ensure_private_dir(folder)
        temporary_path = self.path + ".tmp"
        with open(temporary_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        try:
            os.chmod(temporary_path, 0o600)
        except OSError:
            pass
        os.replace(temporary_path, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def set(self, platform, value):
        value = dict(value or {})
        # Defense in depth: never let caller accidentally persist a password.
        for key in tuple(value):
            if str(key).lower() in ("password", "passwd", "secret"):
                value.pop(key)
        with self._lock:
            data = self.load()
            data[platform] = value
            self._write(data)

    def delete(self, platform):
        with self._lock:
            data = self.load()
            data.pop(platform, None)
            self._write(data)


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
    """Reject URLs whose hostname or current DNS answers are not globally routable."""
    host = _url_host(url)
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        raise ValueError("Refusing a localhost download URL")
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            }
        except socket.gaierror as exc:
            raise ValueError(f"Could not resolve download host: {host}") from exc
        if not addresses:
            raise ValueError(f"Could not resolve download host: {host}")
        if any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError(
                "Refusing a download host that resolves to a private/local address"
            )
        return
    if not ip.is_global:
        raise ValueError("Refusing a private/local download URL")


class AuthManager:
    BAMBU_LOGIN = "https://api.bambulab.com/v1/user-service/user/login"

    def __init__(self, store=None):
        self.store = store or AuthStore()

    def credential(self, platform):
        return self.store.get(platform)

    def token(self, platform):
        data = self.credential(platform)
        return (
            data.get("access_token")
            or data.get("auth_token")
            or data.get("token")
            or ""
        )

    def authenticated(self, platform):
        return bool(self.token(platform))

    def status(self):
        out = {}
        for spec in _PLATFORMS.values():
            if not spec.requires_auth:
                continue
            data = self.credential(spec.key)
            configured = bool(
                data.get("access_token") or data.get("auth_token") or data.get("token")
            )
            out[spec.key] = {
                "authenticated": configured,
                "label": data.get("label")
                or data.get("email")
                or data.get("username")
                or ("Connected" if configured else "Not connected"),
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
        spec = _platform(platform)
        mode = spec.auth_mode if spec else ""
        if mode == "named_cookie" and "auth_token=" in token:
            # Accept either the cookie value or a copied Cookie header.
            m = re.search(r"(?:^|[;\s])auth_token=([^;\s]+)", token)
            if m:
                token = m.group(1).strip()
        # Accept either a raw Cookie header value or a copied "Cookie: ..." header.
        if mode == "cookie_header" and token.lower().startswith("cookie:"):
            token = token.split(":", 1)[1].strip()
        if mode == "anycubic":
            m = re.search(r"(?i)(?:^|[;\s])mo_access_token=([^;\s]+)", token)
            if m:
                token = m.group(1).strip()
            else:
                m = re.match(
                    r"(?i)^(?:XX-Token|Authorization)\s*:\s*(?:Bearer\s+)?(.+)$",
                    token,
                )
            if m:
                token = m.group(1).strip()
        return token

    def save_token(
        self, platform, token, label="", refresh_token=None, expires_in=None
    ):
        spec = _platform(platform)
        if spec is None or not spec.requires_auth:
            raise AuthError("Unknown authentication platform")
        token = self.normalize_token(platform, token)
        if not token:
            raise AuthError("Token is empty")
        if spec.auth_mode == "cookie_header" and "=" not in token:
            raise AuthError(
                "Paste browser session cookies in name=value form (or the full Cookie request header)"
            )
        data = {
            "access_token": token,
            "label": (label or "").strip(),
            "saved_at": int(time.time()),
        }
        if spec.auth_mode in ("named_cookie", "cookie_header"):
            data = {
                "auth_token": token,
                "label": (label or "").strip(),
                "saved_at": int(time.time()),
            }
        if spec.auth_mode == "cookie_header" and not data["label"]:
            data["label"] = f"{spec.display} browser session"
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
        spec = _platform(platform)
        host = _url_host(url)
        if (
            spec is None
            or not spec.auth_hosts
            or not _host_matches(host, spec.auth_hosts)
        ):
            return headers
        token = self.token(platform)
        if not token:
            return headers
        if spec.auth_mode == "bearer":
            headers["Authorization"] = "Bearer " + token
        elif spec.auth_mode == "anycubic":
            # Current Anycubic web/cloud clients use XX-Token; Bearer is also
            # accepted by some Makeronline endpoints.
            headers["XX-Token"] = token
            headers["Authorization"] = "Bearer " + token
        if spec.referer:
            headers["Referer"] = spec.referer
        return headers

    def session(self, platform):
        import requests

        session = requests.Session()
        session.headers.update({"User-Agent": _BROWSER_UA})
        spec = _platform(platform)
        if spec is None:
            return session
        if spec.auth_mode == "named_cookie":
            token = self.token(platform)
            if token:
                session.cookies.set(
                    spec.cookie_name, token, domain=spec.cookie_domain, path="/"
                )
        elif spec.auth_mode == "cookie_header":
            token = self.token(platform)
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
                        session.cookies.set(
                            name, value, domain=spec.cookie_domain, path="/"
                        )
        return session

    def request(self, platform, method, url, session=None, **kwargs):
        """Request a URL while rebuilding scoped auth headers after every redirect."""
        import requests

        if not _is_http_url(url):
            raise ValueError("Refusing non-HTTP URL")
        session = session or self.session(platform)
        supplied = dict(kwargs.pop("headers", {}) or {})
        for name in tuple(supplied):
            if name.lower() in ("authorization", "cookie", "xx-token"):
                supplied.pop(name)
        follow_redirects = kwargs.pop("allow_redirects", True)
        current_url = url
        current_method = str(method or "GET").upper()
        request_kwargs = dict(kwargs)
        for redirect_count in range(_MAX_REDIRECTS + 1):
            _reject_obvious_local_target(current_url)
            headers = self._request_headers(platform, current_url)
            headers.update(supplied)
            response = session.request(
                current_method,
                current_url,
                headers=headers,
                allow_redirects=False,
                **request_kwargs,
            )
            if not follow_redirects or response.status_code not in (
                301,
                302,
                303,
                307,
                308,
            ):
                return response
            location = response.headers.get("location")
            if not location:
                return response
            if redirect_count == _MAX_REDIRECTS:
                response.close()
                raise requests.TooManyRedirects(f"More than {_MAX_REDIRECTS} redirects")
            redirect_status = response.status_code
            current_url = urllib.parse.urljoin(response.url, location)
            if not _is_http_url(current_url):
                response.close()
                raise ValueError("Refusing redirect to a non-HTTP URL")
            response.close()
            request_kwargs.pop("params", None)
            if redirect_status == 303 or (
                redirect_status in (301, 302) and current_method not in ("GET", "HEAD")
            ):
                current_method = "GET"
                for key in ("data", "files", "json"):
                    request_kwargs.pop(key, None)
        raise requests.TooManyRedirects(f"More than {_MAX_REDIRECTS} redirects")

    @staticmethod
    def _makerworld_login_payload(account, password, code):
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
        return account, payload

    @staticmethod
    def _makerworld_login_data(response):
        if 300 <= response.status_code < 400:
            raise AuthError("MakerWorld login returned an unexpected redirect")
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400:
            message = (
                data.get("message")
                or data.get("error")
                or f"HTTP {response.status_code}"
            )
            raise AuthError(f"MakerWorld login failed: {message}")
        access = data.get("accessToken")
        if access:
            return data
        login_type = str(data.get("loginType") or "")
        if "verify" in login_type.lower() or data.get("needVerify"):
            raise VerificationRequired(
                "MakerWorld requires a verification code. Enter the code sent by Bambu Lab."
            )
        raise AuthError(
            data.get("message")
            or data.get("error")
            or "MakerWorld did not return an access token"
        )

    def login_makerworld(self, account, password=None, code=None):
        import requests

        account, payload = self._makerworld_login_payload(account, password, code)
        response = requests.post(
            self.BAMBU_LOGIN,
            json=payload,
            headers={"User-Agent": _BROWSER_UA, "Accept": "application/json"},
            timeout=30,
            allow_redirects=False,
        )
        data = self._makerworld_login_data(response)
        return self.save_token(
            "makerworld",
            data["accessToken"],
            label=account,
            refresh_token=data.get("refreshToken"),
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
            paths.extend(
                [
                    os.path.join(
                        appdata, "AnycubicSlicerNext", "AnycubicSlicerNext.conf"
                    ),
                    os.path.join(appdata, "AnycubicSlicerNext", "config.json"),
                ]
            )
        paths.extend(
            [
                os.path.join(
                    home,
                    "Library",
                    "Application Support",
                    "AnycubicSlicerNext",
                    "AnycubicSlicerNext.conf",
                ),
                os.path.join(
                    home, ".config", "AnycubicSlicerNext", "AnycubicSlicerNext.conf"
                ),
                os.path.join(
                    home,
                    ".local",
                    "share",
                    "AnycubicSlicerNext",
                    "AnycubicSlicerNext.conf",
                ),
            ]
        )
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
            token = None
            label = "Anycubic Slicer Next"
            try:
                obj = json.loads(raw)
                token = self._find_access_token(obj)
                # Try a few common account fields without ever reading passwords.
                if isinstance(obj, dict):
                    label = obj.get("user_name") or obj.get("nickname") or label
            except ValueError:
                # Some builds use INI-like text. Match only explicit token keys.
                m = re.search(
                    r'(?im)^\s*(?:access_token|accessToken|XX-Token)\s*[=:]\s*["\']?([^"\'\s,}]+)',
                    raw,
                )
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

_MODEL_FILE_EXTS = (
    ".3mf",
    ".stl",
    ".obj",
    ".step",
    ".stp",
    ".iges",
    ".igs",
    ".amf",
    ".ply",
    ".scad",
    ".fcstd",
    ".f3d",
    ".zip",
)
_IMAGE_EXTS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".avif",
    ".mp4",
    ".webm",
)


def _clean_web_text(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _decode_embedded_url(value):
    value = html.unescape(str(value or "")).strip().strip("\"'")
    value = value.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    value = value.replace("\\u003A", ":").replace("\\u003a", ":")
    value = value.replace("\\u0026", "&")
    return value


def _slug_title(url):
    path = urllib.parse.unquote(
        urllib.parse.urlsplit(url).path.rstrip("/").split("/")[-1]
    )
    path = re.sub(r"-\d+$", "", path)
    path = re.sub(r"[-_]+", " ", path).strip()
    return path[:120] or "Untitled"


class _CatalogHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = []
        self._anchor = None
        self._text = []
        self.meta = {}

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
            self._anchor["img"] = (
                attrs.get("src")
                or attrs.get("data-src")
                or attrs.get("data-lazy-src")
                or ""
            )
            if not self._anchor.get("title"):
                self._anchor["title"] = attrs.get("alt", "")
        elif tag == "meta":
            key = attrs.get("property") or attrs.get("name")
            content = attrs.get("content")
            if key and content:
                self.meta[str(key).lower()] = content

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
    parser.feed(raw or "")
    return parser


def _fetch_html(url, auth=None, platform="", timeout=30):
    import requests

    session = (
        auth.session(platform) if auth is not None and platform else requests.Session()
    )
    if auth is not None and platform:
        response = auth.request(
            platform,
            "GET",
            url,
            session=session,
            timeout=timeout,
            allow_redirects=True,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
    else:
        response = session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={
                "User-Agent": _BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml",
            },
        )
    if response.status_code in (401, 403) and platform in ("grabcad", "cults3d"):
        display = _display_name(platform)
        raise AuthRequired(
            f"{display} rejected the browser session. Sign in again and paste a fresh Cookie header."
        )
    response.raise_for_status()
    return response.text, response.url


def _looks_like_login_page(raw, platform=""):
    sample = (raw or "")[:300000].lower()
    if platform == "grabcad":
        return (
            "sign in or create account" in sample
            or "sign in with email" in sample
            or ('action="/login"' in sample and "forgot password" in sample)
        )
    if platform == "cults3d":
        return (
            "/users/sign_in" in sample
            or "/en/users/sign_in" in sample
            or ("sign in" in sample and "forgot your password" in sample)
        )
    return False


def _extract_catalog_models(
    raw, base_url, path_pattern, platform, requires_auth=False, limit=30
):
    parser = _parse_catalog_html(raw)
    regex = re.compile(path_pattern, re.IGNORECASE)
    found = []
    seen = set()

    def add(href, title="", img=""):
        href = _decode_embedded_url(href)
        if not href:
            return
        absolute = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlsplit(absolute)
        canonical = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, "", "")
        )
        if not regex.search(parsed.path):
            return
        if canonical in seen:
            return
        seen.add(canonical)
        found.append(
            {
                "name": _clean_web_text(title) or _slug_title(canonical),
                "author": "Unknown",
                "platform": platform,
                "thumbnail_url": urllib.parse.urljoin(
                    base_url, _decode_embedded_url(img)
                )
                if img
                else "",
                "license": "Unknown",
                "license_url": "",
                "license_summary": "Open the model page to review the exact license before importing.",
                "download_url": canonical,
                "url": canonical,
                "requires_auth": requires_auth,
            }
        )

    for a in parser.anchors:
        add(
            a.get("href"),
            a.get("text") or a.get("title") or a.get("aria"),
            a.get("img"),
        )
        if len(found) >= limit:
            return found

    # SSR/Next/Vue pages frequently put model paths in JSON rather than anchors.
    normalized = (
        (raw or "").replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    )
    for match in regex.finditer(normalized):
        path = match.group(0)
        add(path)
        if len(found) >= limit:
            break
    return found


def _catalog_search(
    query, url_templates, path_pattern, platform, auth=None, requires_auth=False
):
    last_error = None
    for template in url_templates:
        url = template.format(query=urllib.parse.quote(query.strip(), safe=""))
        try:
            raw, final_url = _fetch_html(
                url,
                auth=auth if requires_auth else None,
                platform="grabcad" if requires_auth else "",
            )
            if requires_auth and _looks_like_login_page(raw, "grabcad"):
                raise AuthRequired(
                    "GrabCAD search requires a signed-in GrabCAD browser session."
                )
            models = _extract_catalog_models(
                raw, final_url, path_pattern, platform, requires_auth=requires_auth
            )
            if models:
                return models
        except AuthRequired:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
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
        action = (
            "download" in low_label
            or "download" in low_url
            or "/files/" in low_url
            or "/file/" in low_url
        )
        if not (direct or action):
            return
        if url in seen:
            return
        seen.add(url)
        candidates.append((url, _clean_web_text(label)))

    for a in parser.anchors:
        add(a.get("href"), a.get("text") or a.get("title") or a.get("aria"))

    normalized = (
        (raw or "").replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    )
    normalized = normalized.replace("\\u003A", ":").replace("\\u003a", ":")
    for match in re.finditer(r'https?://[^"\'<>\\\s]+', normalized, re.IGNORECASE):
        add(match.group(0))
    for match in re.finditer(
        r'(?P<q>["\'])(?P<path>/[^"\']{1,700}(?:download|files?)[^"\']*)(?P=q)',
        normalized,
        re.IGNORECASE,
    ):
        add(match.group("path"))
    return candidates


def _request_probe(session, url, auth, platform, use_range):
    headers = {"Accept": "*/*"}
    if use_range:
        headers["Range"] = "bytes=0-0"
    return auth.request(
        platform,
        "GET",
        url,
        session=session,
        stream=True,
        timeout=25,
        allow_redirects=True,
        headers=headers,
    )


def _probe_result(response, platform):
    if response.status_code in (401, 403) and platform in ("grabcad", "cults3d"):
        raise AuthRequired(
            f"{_display_name(platform)} session was rejected while resolving files."
        )
    if response.status_code >= 400:
        return None
    content_type = (response.headers.get("content-type") or "").lower()
    disposition = response.headers.get("content-disposition") or ""
    final_url = response.url
    final_path = urllib.parse.urlsplit(final_url).path.lower()
    is_html = "text/html" in content_type or "application/xhtml" in content_type
    is_download = (
        final_path.endswith(_MODEL_FILE_EXTS)
        or "attachment" in disposition.lower()
        or any(
            kind in content_type
            for kind in ("zip", "octet-stream", "model/", "3mf", "stl")
        )
    )
    if is_html or not is_download:
        return None
    match = re.search(
        r"filename\*?=(?:UTF-8''|\")?([^\";]+)", disposition, re.IGNORECASE
    )
    filename = urllib.parse.unquote(match.group(1).strip().strip('"')) if match else ""
    if not filename:
        filename = (
            os.path.basename(urllib.parse.urlsplit(final_url).path) or "model_download"
        )
    return {"url": final_url, "name": _safe_filename(filename, "model_download")}


def _probe_download(url, auth=None, platform=""):
    """Validate a candidate without downloading the full file."""
    import requests

    if not _is_http_url(url):
        return None
    try:
        _reject_obvious_local_target(url)
    except ValueError:
        return None
    request_auth = auth or AuthManager()
    session = request_auth.session(platform)
    try:
        response = _request_probe(session, url, request_auth, platform, use_range=True)
        if response.status_code in (405, 416):
            response.close()
            response = _request_probe(
                session, url, request_auth, platform, use_range=False
            )
    except (requests.RequestException, ValueError):
        return None
    try:
        return _probe_result(response, platform)
    finally:
        response.close()


def _collect_page_candidates(urls, auth, platform_key):
    import requests

    bodies = []
    candidates = []
    for url in urls:
        try:
            raw, fetched = _fetch_html(
                url, auth=auth if platform_key else None, platform=platform_key
            )
        except AuthRequired:
            raise
        except (requests.RequestException, OSError, ValueError):
            continue
        if platform_key in ("cults3d", "grabcad") and _looks_like_login_page(
            raw, platform_key
        ):
            display = _display_name(platform_key)
            raise AuthRequired(
                f"{display} browser session is no longer signed in. Refresh the saved Cookie header."
            )
        bodies.append(raw)
        candidates.extend(_extract_download_candidates(raw, fetched))
    return "\n".join(bodies), candidates


def _validated_candidates(candidates, auth, platform_key):
    candidates.sort(
        key=lambda item: (
            0
            if urllib.parse.urlsplit(item[0]).path.lower().endswith(_MODEL_FILE_EXTS)
            else 1
        )
    )
    files = []
    seen = set()
    for candidate, label in candidates[:30]:
        probed = _probe_download(
            candidate, auth=auth if platform_key else None, platform=platform_key
        )
        if not probed or probed["url"] in seen:
            continue
        seen.add(probed["url"])
        if not os.path.splitext(probed["name"])[1]:
            extension = ".3mf" if "3mf" in (label or "").lower() else ".zip"
            probed["name"] += extension
        files.append(probed)
    return files


def _public_page_files(
    model,
    auth=None,
    platform_key="",
    extra_urls=(),
    restricted_markers=(),
    no_direct_message="",
):
    page_url = model.get("url") or model.get("download_url") or ""
    if not page_url:
        raise ValueError("Model page URL is missing")
    urls = [page_url, *(url for url in extra_urls if url)]
    body, candidates = _collect_page_candidates(urls, auth, platform_key)
    if restricted_markers and any(
        marker.lower() in body.lower() for marker in restricted_markers
    ):
        raise BrowserRequired(
            "This model is paid, membership-gated, or requires the platform checkout/download page.",
            page_url,
        )
    files = _validated_candidates(candidates, auth, platform_key)
    if files:
        return files
    raise BrowserRequired(
        no_direct_message
        or "The platform did not expose a direct downloadable model file to this session.",
        page_url,
    )


# ---------------------------------------------------------------------------
# Search adapters
# ---------------------------------------------------------------------------


def _model_identifier(model, field, pattern, missing_message):
    value = model.get(field)
    if value:
        return value
    match = re.search(pattern, model.get("url", ""), re.IGNORECASE)
    if match:
        return match.group(1)
    raise ValueError(missing_message)


def _api_data(response, error_message):
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") not in (0, "0", 200, "200", None):
        raise RuntimeError(
            payload.get("msg") or payload.get("message") or error_message
        )
    return payload.get("data") or {}


def _file_records(records, url_keys, name_keys, fallback_extension=".stl"):
    files = []
    for index, item in enumerate(records or (), 1):
        if not isinstance(item, dict):
            continue
        url = next((item.get(key) for key in url_keys if item.get(key)), "")
        if not url:
            continue
        name = next((item.get(key) for key in name_keys if item.get(key)), "")
        if not name:
            name = (
                os.path.basename(urllib.parse.urlsplit(url).path)
                or f"model_{index}{fallback_extension}"
            )
        files.append({"name": name, "url": url})
    return files


def _normalize_download_files(files):
    normalized = []
    for index, item in enumerate(files or ()):
        if not isinstance(item, dict) or not item.get("url"):
            continue
        normalized.append(
            {
                "url": item["url"],
                "name": item.get("name") or f"model_{index + 1}.3mf",
                "preview_url": item.get("preview_url") or "",
                "size": item.get("size"),
            }
        )
    return normalized


def _selected_file_indices(values):
    selected = []
    seen = set()
    for value in values or ():
        index = int(value)
        if index >= 0 and index not in seen:
            seen.add(index)
            selected.append(index)
    return selected


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
    def search(query, _context, options=None):
        import requests

        page = _search_page_number(options)
        response = requests.post(
            MakeronlineSearcher.SEARCH_URL,
            json={
                "keyword": query,
                "page": page,
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
            lic_name, lic_url = MAKERONLINE_LICENSES.get(
                item.get("license", 0), ("Unknown", "")
            )
            lic = _parse_license(lic_name, lic_url)
            results.append(
                {
                    "name": item.get("title", "Untitled"),
                    "author": item.get("show_user_name")
                    or item.get("user_name", "Unknown"),
                    "platform": "Makeronline",
                    "thumbnail_url": (item.get("mold_image") or "").replace(
                        "thumbnail", "400x300"
                    ),
                    "license": lic["name"],
                    "license_url": lic["url"],
                    "license_summary": lic["summary"],
                    "download_url": item.get("target_url", ""),
                    "url": item.get("target_url", ""),
                    "_mold_id": item.get("mold_id"),
                    "requires_auth": True,
                    "downloads": item.get("download_num"),
                    "likes": item.get("like_num"),
                    "published_at": item.get("publish_time")
                    or item.get("created_time"),
                    "is_free": not bool(item.get("is_premium")),
                }
            )
        return _search_page_result(results, page, 30)

    @staticmethod
    def get_files(model, auth):
        if not auth.authenticated("makeronline"):
            raise AuthRequired("Makeronline requires an Anycubic account session")
        mold_id = _model_identifier(
            model,
            "_mold_id",
            r"(?:mold|model)[^0-9]*(\d+)",
            "Makeronline model id is missing",
        )
        session = auth.session("makeronline")
        response = auth.request(
            "makeronline",
            "GET",
            f"{MAKERONLINE_BASE}/api/mold/detail",
            session=session,
            params={"id": mold_id},
            timeout=30,
        )
        if response.status_code in (401, 403):
            raise AuthRequired("Makeronline session was rejected; log in again")
        detail = _api_data(response, "Makeronline detail API failed")
        return _file_records(
            detail.get("files"),
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
    SEARCH_URL = (
        f"{NEXPRINT_BASE}/gateway/api/v1/model-library-server/model-base-info/search"
    )

    @staticmethod
    def search(query, _context, options=None):
        import requests

        page_number = _search_page_number(options)
        response = requests.get(
            NexprintSearcher.SEARCH_URL,
            params={
                "keyword": query,
                "pageNo": str(page_number),
                "pageSize": "30",
            },
            headers={"User-Agent": _BROWSER_UA},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") not in (0, "0", 200, "200", None):
            return []
        page = (data.get("data") or {}).get("pageResult") or {}
        results = []
        for item in page.get("list") or []:
            statistics = item.get("statistics") or {}
            lic_name, lic_url = NEXPRINT_LICENSES.get(
                item.get("licenseType", 0), ("Unknown", "")
            )
            lic = _parse_license(lic_name, lic_url)
            model_id = item.get("modelId") or item.get("id")
            results.append(
                {
                    "name": item.get("modelName", "Untitled"),
                    "author": item.get("authorName")
                    or ((item.get("author") or {}).get("nickname", "Unknown")),
                    "platform": "Nexprint",
                    "thumbnail_url": item.get("coverImgUrl", ""),
                    "license": lic["name"],
                    "license_url": lic["url"],
                    "license_summary": lic["summary"],
                    "download_url": f"{NEXPRINT_BASE}/models/{model_id or ''}",
                    "url": f"{NEXPRINT_BASE}/models/{model_id or ''}",
                    "_model_id": model_id,
                    "requires_auth": True,
                    "downloads": statistics.get("staticsModelDownloadCount"),
                    "likes": statistics.get("staticsModelStarPeoPleCount"),
                    "views": statistics.get("staticsModelReadCount"),
                    "makes": statistics.get("staticsModelPrintCount"),
                    "published_at": item.get("publishTime"),
                    "is_free": not bool(item.get("price")),
                }
            )
        return _search_page_result(
            results, page_number, 30, total=page.get("total")
        )

    @staticmethod
    def get_files(model, auth):
        if not auth.authenticated("nexprint"):
            raise AuthRequired("Nexprint requires a logged-in auth_token session")
        model_id = _model_identifier(
            model,
            "_model_id",
            r"/models/(\d+)",
            "Nexprint model id is missing",
        )
        session = auth.session("nexprint")
        response = auth.request(
            "nexprint",
            "GET",
            f"{NEXPRINT_BASE}/gateway/api/v1/model-library-server/model-base-info/get",
            session=session,
            params={"id": model_id},
            timeout=30,
        )
        if response.status_code in (401, 403):
            raise AuthRequired(
                "Nexprint session was rejected; refresh auth_token and log in again"
            )
        detail = _api_data(response, "Nexprint detail API failed")
        return _file_records(
            detail.get("modelFileInfoList") or detail.get("files"),
            ("fileUrl", "url", "downloadUrl"),
            ("fileName", "name"),
        )


class PrintablesSearcher:
    GRAPHQL_URL = "https://api.printables.com/graphql/"
    SEARCH_QUERY = (
        "query Search($query: String!, $limit: Int, $offset: Int, "
        "$ordering: SearchChoicesEnum) {"
        " searchPrints2(query: $query, limit: $limit, offset: $offset, "
        "ordering: $ordering) {"
        " items { id name slug downloadCount likesCount ratingAvg datePublished "
        " image { filePath } license { name } user { publicUsername } } } }"
    )
    FILES_QUERY = (
        "query PrintFiles($id: ID!) { print(id: $id) {"
        " stls { id name fileSize filePreviewPath } } }"
    )
    DOWNLOAD_LINK_MUTATION = (
        "mutation GetDownloadLink($id: ID!, $modelId: ID!, "
        "$fileType: DownloadFileTypeEnum!, $source: DownloadSourceEnum!) {"
        " getDownloadLink(id: $id, printId: $modelId, fileType: $fileType, "
        "source: $source) { ok errors { field messages } "
        "output { link count ttl } } }"
    )

    @staticmethod
    def _graphql(query, variables):
        import requests

        response = requests.post(
            PrintablesSearcher.GRAPHQL_URL,
            json={"query": query, "variables": variables},
            headers={"Content-Type": "application/json", "User-Agent": _BROWSER_UA},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError(payload["errors"][0].get("message", "GraphQL error"))
        return payload.get("data") or {}

    @staticmethod
    def search(query, _context, options=None):
        import requests

        page = _search_page_number(options)
        requested_sort = (
            str(options.get("sort") or "") if isinstance(options, dict) else ""
        )
        ordering = {
            "popularity": "popular",
            "likes": "popular",
            "downloads": "popular",
            "rating": "rating",
            "newest": "latest",
            "makes": "makes_count",
        }.get(requested_sort, "best_match")

        response = requests.post(
            PrintablesSearcher.GRAPHQL_URL,
            json={
                "query": PrintablesSearcher.SEARCH_QUERY,
                "variables": {
                    "query": query,
                    "limit": 30,
                    "offset": (page - 1) * 30,
                    "ordering": ordering,
                },
            },
            headers={"Content-Type": "application/json", "User-Agent": _BROWSER_UA},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError(payload["errors"][0].get("message", "GraphQL error"))
        items = ((payload.get("data") or {}).get("searchPrints2") or {}).get(
            "items"
        ) or []
        results = []
        for item in items:
            path = (item.get("image") or {}).get("filePath", "")
            lic_obj = item.get("license") or {}
            lic = _parse_license(
                lic_obj.get("name", "") if isinstance(lic_obj, dict) else ""
            )
            url = f"https://www.printables.com/model/{item.get('id', '')}-{item.get('slug', '')}"
            results.append(
                {
                    "name": item.get("name", "Untitled"),
                    "author": (item.get("user") or {}).get("publicUsername", "Unknown"),
                    "platform": "Printables",
                    "thumbnail_url": f"https://media.printables.com/{path}"
                    if path
                    else "",
                    "license": lic["name"] or "Unknown",
                    "license_url": lic["url"],
                    "license_summary": lic["summary"]
                    or "Check Printables for license details.",
                    "download_url": url,
                    "url": url,
                    "requires_auth": False,
                    "importable": True,
                    "downloads": item.get("downloadCount"),
                    "likes": item.get("likesCount"),
                    "rating": item.get("ratingAvg"),
                    "published_at": item.get("datePublished"),
                    "is_free": True,
                }
            )
        return _search_page_result(results, page, 30)

    @staticmethod
    def _download_record(model_id, stl):
        file_id, name = stl.get("id"), stl.get("name") or ""
        if not file_id or not name:
            return None
        link_data = PrintablesSearcher._graphql(
            PrintablesSearcher.DOWNLOAD_LINK_MUTATION,
            {
                "id": str(file_id),
                "modelId": model_id,
                "fileType": "stl",
                "source": "model_detail",
            },
        )
        result = link_data.get("getDownloadLink") or {}
        output = result.get("output") or {}
        url = output.get("link") or ""
        if not result.get("ok") or not url:
            messages = (
                item
                for error in (result.get("errors") or [])
                for item in (error.get("messages") or [])
            )
            message = "; ".join(str(item) for item in messages)
            raise RuntimeError(
                message or f"Printables returned no download link for {name}"
            )
        preview_path = str(stl.get("filePreviewPath") or "").lstrip("/")
        return {
            "name": name,
            "url": url,
            "signed": True,
            "size": stl.get("fileSize"),
            "preview_url": (
                f"https://media.printables.com/{preview_path}" if preview_path else ""
            ),
        }

    @staticmethod
    def get_files(model, auth=None):
        model_url = (
            model.get("url", "") if isinstance(model, dict) else str(model or "")
        )
        match = re.search(r"/model/(\d+)", model_url)
        if not match:
            return []
        model_id = match.group(1)
        data = PrintablesSearcher._graphql(
            PrintablesSearcher.FILES_QUERY, {"id": model_id}
        )
        files = []
        for stl in (data.get("print") or {}).get("stls") or []:
            record = PrintablesSearcher._download_record(model_id, stl)
            if record:
                files.append(record)
        return files


class MakerWorldSearcher:
    SEARCH_URL = "https://api.bambulab.com/v1/search-service/select/design2"
    DESIGN_BASE = "https://api.bambulab.com/v1/design-service"
    BASE = "https://makerworld.com"
    LICENSES: ClassVar = {
        "CC0": ("CC0", "https://creativecommons.org/publicdomain/zero/1.0/"),
        "BY": ("CC BY", "https://creativecommons.org/licenses/by/4.0/"),
        "BY-SA": ("CC BY-SA", "https://creativecommons.org/licenses/by-sa/4.0/"),
        "BY-NC": ("CC BY-NC", "https://creativecommons.org/licenses/by-nc/4.0/"),
        "BY-NC-SA": (
            "CC BY-NC-SA",
            "https://creativecommons.org/licenses/by-nc-sa/4.0/",
        ),
        "BY-ND": ("CC BY-ND", "https://creativecommons.org/licenses/by-nd/4.0/"),
        "BY-NC-ND": (
            "CC BY-NC-ND",
            "https://creativecommons.org/licenses/by-nc-nd/4.0/",
        ),
        "Standard Digital File License": ("Standard Digital File License", ""),
        "MakerWorld Exclusive License": ("MakerWorld Exclusive License", ""),
    }

    @staticmethod
    def search(query, _context, options=None):
        import requests

        page = _search_page_number(options)
        response = requests.get(
            MakerWorldSearcher.SEARCH_URL,
            params={
                "keyword": query,
                "limit": "30",
                "offset": str((page - 1) * 30),
            },
            headers={"User-Agent": _BROWSER_UA},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        results = []
        for hit in payload.get("hits") or []:
            lic_name, lic_url = MakerWorldSearcher.LICENSES.get(
                hit.get("license", "Unknown"), (hit.get("license", "Unknown"), "")
            )
            lic = _parse_license(lic_name, lic_url)
            creator = hit.get("designCreator") or {}
            model_id = hit.get("id")
            results.append(
                {
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
                    "downloads": hit.get("downloadCount")
                    or hit.get("rawModelFileDownloadCount"),
                    "likes": hit.get("likeCount"),
                    "views": hit.get("readCount"),
                    "makes": hit.get("printCount"),
                    "published_at": hit.get("createTime"),
                    "is_free": not bool(hit.get("price")),
                }
            )
        return _search_page_result(results, page, 30, total=payload.get("total"))

    @staticmethod
    def _profile_from_url(url):
        m = re.search(r"#profileId[-=](\d+)", url or "", re.IGNORECASE)
        return m.group(1) if m else ""

    @staticmethod
    def _load_design(design_id, auth, session):
        response = auth.request(
            "makerworld",
            "GET",
            f"{MakerWorldSearcher.DESIGN_BASE}/design/{design_id}",
            session=session,
            timeout=30,
        )
        if response.status_code == 418:
            raise RuntimeError(
                "MakerWorld is challenging this request with CAPTCHA; use Open in browser and retry later"
            )
        response.raise_for_status()
        design = response.json()
        if not (design.get("modelId") or design.get("model_id")):
            raise RuntimeError("MakerWorld design metadata did not contain modelId")
        return design

    @staticmethod
    def _profile_rating(item):
        count = item.get("ratingCount")
        total = item.get("ratingScoreTotal")
        if not count or total is None:
            return None
        try:
            return round(float(total) / float(count), 1)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    @staticmethod
    def _profile_record(instance):
        if not isinstance(instance, dict):
            return None
        detail = instance.get("detail")
        detail_id = (
            _coalesce(
                detail.get("profileId"),
                detail.get("profile_id"),
                detail.get("id"),
            )
            if isinstance(detail, dict)
            else ""
        )
        item = detail if detail_id and isinstance(detail, dict) else instance
        profile_id = _coalesce(
            item.get("profileId"), item.get("profile_id"), item.get("id")
        )
        if not profile_id:
            profile_id = _coalesce(
                instance.get("profileId"),
                instance.get("profile_id"),
                instance.get("id"),
            )
        if not profile_id:
            return None
        extension = _coalesce(item.get("extention"), item.get("extension"), default={})
        model_info = _coalesce(extension.get("modelInfo"), default={})
        settings = _coalesce(model_info.get("projectSettings"), default={})
        creator = _coalesce(
            item.get("instanceCreator"), item.get("creator"), default={}
        )
        plates = _coalesce(model_info.get("plates"), default=[])
        return {
            "profile_id": str(profile_id),
            "title": _coalesce(
                item.get("title"), item.get("name"), default="Print profile"
            ),
            "creator": creator.get("name")
            if isinstance(creator, dict)
            else str(creator or ""),
            "cover": _coalesce(item.get("cover"), item.get("coverUrl")),
            "summary": _strip_html(
                _coalesce(item.get("summaryTranslated"), item.get("summary"))
            ),
            "printer": _first(
                (model_info.get("compatibility") or {}).get("devProductName")
            ),
            "layer_height": _first(settings.get("layerHeight")),
            "walls": _first(settings.get("wallLoops")),
            "infill": _first(settings.get("sparseInfillDensity")),
            "prediction": item.get("prediction"),
            "plates": len(plates) if isinstance(plates, list) else plates,
            "rating": MakerWorldSearcher._profile_rating(item),
            "rating_count": item.get("ratingCount"),
            "has_raw": bool(item.get("hasZipStl")),
            "is_default": bool(item.get("isDefault")),
        }

    @staticmethod
    def _merge_profile_instance(summary, detail):
        if not isinstance(detail, dict):
            return summary
        merged = dict(summary)
        merged.update(
            {
                key: value
                for key, value in detail.items()
                if value not in (None, "", [], {})
            }
        )
        return merged

    @staticmethod
    def _load_profiles(design_id, design, auth, session, complete=False):
        instances = design.get("instances") or []
        if complete or not instances:
            response = auth.request(
                "makerworld",
                "GET",
                f"{MakerWorldSearcher.DESIGN_BASE}/design/{design_id}/instances",
                session=session,
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            complete_instances = payload.get("hits") or payload.get("instances") or []
            if complete_instances:
                rich_profiles = {
                    str(profile["profile_id"]): instance
                    for instance in instances
                    if (profile := MakerWorldSearcher._profile_record(instance))
                    is not None
                }
                instances = [
                    MakerWorldSearcher._merge_profile_instance(
                        instance,
                        rich_profiles.get(str(profile["profile_id"])),
                    )
                    for instance in complete_instances
                    if (profile := MakerWorldSearcher._profile_record(instance))
                    is not None
                ]
        profiles = [
            profile
            for instance in instances
            if (profile := MakerWorldSearcher._profile_record(instance)) is not None
        ]
        if not profiles:
            raise RuntimeError(
                "MakerWorld returned no printable profile for this design"
            )
        return profiles

    @staticmethod
    def get_download_choices(model, auth):
        design_id = _model_identifier(
            model, "_model_id", r"/models/(\d+)", "MakerWorld design id is missing"
        )
        session = auth.session("makerworld")
        design = MakerWorldSearcher._load_design(design_id, auth, session)
        profiles = MakerWorldSearcher._load_profiles(
            design_id, design, auth, session, complete=True
        )
        requested = MakerWorldSearcher._profile_from_url(model.get("url", ""))
        default = next(
            (
                item["profile_id"]
                for item in profiles
                if item["profile_id"] == requested
            ),
            "",
        )
        if not default:
            default = next(
                (item["profile_id"] for item in profiles if item["is_default"]),
                profiles[0]["profile_id"],
            )
        return {
            "profiles": profiles,
            "default_profile_id": default,
            "formats": [
                {
                    "id": "3mf",
                    "label": "3MF print profile",
                    "description": "Download and import the selected print profile directly.",
                    "direct": True,
                    "available": True,
                },
                {
                    "id": "raw_browser",
                    "label": "STL/CAD files",
                    "description": (
                        "Open MakerWorld to choose raw model files; its raw-file API "
                        "requires a browser session."
                    ),
                    "direct": False,
                    "available": any(item["has_raw"] for item in profiles),
                },
            ],
        }

    @staticmethod
    def _download_profile(profile_id, internal_model_id, auth, session):
        download_api = (
            f"https://api.bambulab.com/v1/iot-service/api/user/profile/{profile_id}"
        )
        response = auth.request(
            "makerworld",
            "GET",
            download_api,
            session=session,
            params={"model_id": str(internal_model_id)},
            timeout=30,
        )
        if response.status_code == 401:
            raise AuthRequired("MakerWorld session expired; log in again")
        if response.status_code == 403:
            try:
                body = response.json()
                reason = body.get("error") or body.get("message") or "access denied"
            except ValueError:
                reason = "access denied"
            raise RuntimeError(f"MakerWorld refused this profile: {reason}")
        if response.status_code == 418:
            raise RuntimeError(
                "MakerWorld is challenging this request with CAPTCHA; use Open in browser and retry later"
            )
        response.raise_for_status()
        payload = response.json()
        return payload.get("data") if isinstance(payload.get("data"), dict) else payload

    @staticmethod
    def _selected_profile(profile_id, design_id, design, auth, session):
        profiles = MakerWorldSearcher._load_profiles(design_id, design, auth, session)
        selected = next(
            (item for item in profiles if item["profile_id"] == profile_id), None
        )
        if selected is not None:
            return selected
        profiles = MakerWorldSearcher._load_profiles(
            design_id, design, auth, session, complete=True
        )
        selected = next(
            (item for item in profiles if item["profile_id"] == profile_id), None
        )
        if selected is None:
            raise ValueError("The selected MakerWorld print profile is unavailable")
        return selected

    @staticmethod
    def get_files(model, auth):
        if not auth.authenticated("makerworld"):
            raise AuthRequired(
                "MakerWorld import requires a Bambu/MakerWorld account session"
            )
        design_id = _model_identifier(
            model,
            "_model_id",
            r"/models/(\d+)",
            "MakerWorld design id is missing",
        )
        session = auth.session("makerworld")
        design = MakerWorldSearcher._load_design(design_id, auth, session)
        internal_model_id = design.get("modelId") or design.get("model_id")
        if model.get("_download_format", "3mf") != "3mf":
            raise ValueError("MakerWorld direct import supports the 3MF format only")
        profile_id = str(
            model.get("_profile_id")
            or MakerWorldSearcher._profile_from_url(model.get("url", ""))
        )
        if not profile_id:
            raise ValueError("Select a MakerWorld print profile before downloading")
        selected = MakerWorldSearcher._selected_profile(
            profile_id, design_id, design, auth, session
        )
        body = MakerWorldSearcher._download_profile(
            profile_id, internal_model_id, auth, session
        )
        url = (
            body.get("url") or body.get("downloadUrl") or body.get("download_url") or ""
        )
        if not url:
            raise RuntimeError("MakerWorld download API returned no signed URL")
        name = (
            body.get("name")
            or body.get("filename")
            or selected["title"]
            or design.get("title")
            or f"makerworld_{design_id}.3mf"
        )
        name = _safe_filename(name, f"makerworld_{design_id}.3mf")
        if not name.casefold().endswith(".3mf"):
            name += ".3mf"
        return [{"name": name, "url": url, "signed": True}]


def _license_from_api(value):
    value = _first(value, "Unknown")
    if isinstance(value, dict):
        value = value.get("name") or "Unknown"
    return _parse_license(str(value))


def _thingiverse_thumbnail(item):
    direct = _coalesce(item.get("thumbnail"), item.get("preview_image"))
    if direct:
        return direct

    default_image = item.get("default_image") or {}
    if isinstance(default_image, dict):
        sizes = default_image.get("sizes") or []
        for preferred_size in ("large", "medium", "small", "tiny"):
            for image in sizes:
                if (
                    isinstance(image, dict)
                    and image.get("size") == preferred_size
                    and image.get("url")
                ):
                    return image["url"]
        if default_image.get("url"):
            return default_image["url"]

    images = item.get("images") or []
    first_image = images[0] if images else {}
    if isinstance(first_image, dict):
        return first_image.get("url") or ""
    return ""


def _thingiverse_file_record(item):
    records = _file_records(
        [item],
        ("download_url", "public_url", "url"),
        ("name", "filename"),
    )
    if not records:
        return None
    record = records[0]
    record["preview_url"] = _thingiverse_thumbnail(item)
    record["size"] = item.get("size")
    return record


def _thingiverse_result(item):
    creator = item.get("creator") or {}
    lic = _license_from_api(item.get("license"))
    thing_id = item.get("id")
    url = _coalesce(
        item.get("public_url"),
        default=f"https://www.thingiverse.com/thing:{thing_id}",
    )
    return {
        "name": _coalesce(item.get("name"), default="Untitled"),
        "author": _coalesce(
            creator.get("name"), creator.get("username"), default="Unknown"
        ),
        "platform": "Thingiverse",
        "thumbnail_url": _thingiverse_thumbnail(item),
        "license": lic["name"],
        "license_url": lic["url"],
        "license_summary": lic["summary"],
        "download_url": url,
        "url": url,
        "_thing_id": thing_id,
        "requires_auth": True,
        "downloads": item.get("download_count"),
        "likes": item.get("like_count"),
        "views": item.get("view_count"),
        "makes": item.get("make_count"),
        "published_at": _coalesce(item.get("added"), item.get("created_at")),
        "is_free": True,
    }


class ThingiverseSearcher:
    BASE = "https://www.thingiverse.com"
    API = "https://api.thingiverse.com"

    @staticmethod
    def search(query, context, options=None):
        if not isinstance(context, AuthManager) or not context.authenticated(
            "thingiverse"
        ):
            raise AuthRequired(
                "Thingiverse search now requires a personal Thingiverse API access token"
            )
        page = _search_page_number(options)
        response = context.request(
            "thingiverse",
            "GET",
            f"{ThingiverseSearcher.API}/search/{urllib.parse.quote(query, safe='')}",
            params={
                "type": "things",
                "sort": "relevant",
                "page": page,
                "per_page": 30,
            },
            timeout=30,
        )
        if response.status_code in (401, 403):
            raise AuthRequired("Thingiverse rejected the saved API access token")
        response.raise_for_status()
        payload = response.json()
        items = payload if isinstance(payload, list) else payload.get("hits") or []
        results = [_thingiverse_result(item) for item in items]
        for result in results:
            # Search hits are intentionally compact and omit the license and
            # complete counters. Load them from /things/{id} only when the user
            # opens a card, avoiding one API request per search result.
            result["_details_available"] = True
            result["_details_loaded"] = False
        total = payload.get("total") if isinstance(payload, dict) else None
        if total is None:
            headers = getattr(response, "headers", {}) or {}
            total = headers.get("X-Total-Count") or headers.get("x-total-count")
        return _search_page_result(results, page, 30, total=total)

    @staticmethod
    def get_details(model, auth):
        if not auth.authenticated("thingiverse"):
            raise AuthRequired("Thingiverse requires an API access token")
        thing_id = _model_identifier(
            model,
            "_thing_id",
            r"thing(?::|%3A)(\d+)",
            "Thingiverse model id is missing",
        )
        response = auth.request(
            "thingiverse",
            "GET",
            f"{ThingiverseSearcher.API}/things/{thing_id}",
            timeout=30,
        )
        if response.status_code in (401, 403):
            raise AuthRequired("Thingiverse rejected the saved API access token")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("Thingiverse returned invalid model details")
        result = dict(model)
        result.update(_thingiverse_result(payload))
        result["_details_available"] = True
        result["_details_loaded"] = True
        return result

    @staticmethod
    def get_files(model, auth):
        if not auth.authenticated("thingiverse"):
            raise AuthRequired("Thingiverse requires an API access token")
        thing_id = _model_identifier(
            model,
            "_thing_id",
            r"thing(?::|%3A)(\d+)",
            "Thingiverse model id is missing",
        )
        response = auth.request(
            "thingiverse",
            "GET",
            f"{ThingiverseSearcher.API}/things/{thing_id}/files",
            timeout=30,
        )
        if response.status_code in (401, 403):
            raise AuthRequired("Thingiverse rejected the saved API access token")
        response.raise_for_status()
        files = []
        for item in response.json():
            if not isinstance(item, dict):
                continue
            record = _thingiverse_file_record(item)
            if record:
                files.append(record)
        return files


class Cults3DSearcher:
    BASE = "https://cults3d.com"
    SEARCH_URLS = (
        BASE + "/en/tags/{query}",
        BASE + "/en/tags/{query}?only_free=true",
    )
    PATH_RE = r"/en/3d-model/[a-z0-9_-]+/[a-z0-9%._~+()-]+"

    @staticmethod
    def search(query, context, options=None):
        items = _catalog_search(
            query, Cults3DSearcher.SEARCH_URLS, Cults3DSearcher.PATH_RE, "Cults3D"
        )
        for item in items:
            item["requires_auth"] = True
        return items

    @staticmethod
    def get_files(model, auth=None):
        # Cults requires an account even for free-file downloads, while its
        # documented API intentionally does not expose other users' 3D files.
        # Use the user's browser session only against cults3d.com.
        if auth is None or not auth.authenticated("cults3d"):
            raise AuthRequired(
                "Cults3D requires a signed-in Cults account before downloading files"
            )
        return _public_page_files(
            model,
            auth=auth,
            platform_key="cults3d",
            no_direct_message="Cults3D did not expose a direct file to this signed-in session. Use Open in browser for the official download/checkout flow.",
        )


_MYMINIFACTORY_LICENSE_URL = "https://www.myminifactory.com/object-licensing"


def _myminifactory_image_url(item):
    images = item.get("images") or item.get("image") or []
    if isinstance(images, dict):
        images = [images]
    usable = [image for image in images if isinstance(image, dict)]
    primary = next((value for value in usable if value.get("is_primary")), None)
    ordered = ([primary] if primary else []) + [
        image for image in usable if image is not primary
    ]
    for image in ordered:
        for key in ("thumbnail", "standard", "original", "thumbnail_url", "url"):
            value = image.get(key)
            if isinstance(value, dict):
                value = value.get("url")
            if value:
                return value
    return _coalesce(item.get("thumbnail_url"), item.get("image_url"))


def _myminifactory_license(item):
    value = item.get("license")
    if isinstance(value, str):
        name = _strip_html(value)
        folded = name.casefold()
        if "standard digital file store license" in folded:
            return _parse_license(
                "Standard Digital File Store License",
                _MYMINIFACTORY_LICENSE_URL,
            )
        if "digital file store license" in folded:
            return _parse_license(
                "MyMiniFactory Digital File Store License",
                _MYMINIFACTORY_LICENSE_URL,
            )
        if name:
            return _parse_license(name)
    if value:
        return _license_from_api(value)

    flags = item.get("licenses") or []
    if any(
        isinstance(flag, dict)
        and flag.get("type") == "store"
        and flag.get("value") is True
        for flag in flags
    ):
        return _parse_license(
            "MyMiniFactory Digital File Store License",
            _MYMINIFACTORY_LICENSE_URL,
        )
    return _license_from_api(flags)


def _myminifactory_result(item):
    designer = item.get("designer") or {}
    lic = _myminifactory_license(item)
    url = _coalesce(
        item.get("url"),
        default=f"https://www.myminifactory.com/object/{item.get('id', '')}",
    )
    archive = item.get("archive_download_url") or ""
    return {
        "name": _coalesce(item.get("name"), default="Untitled"),
        "author": _coalesce(
            designer.get("username"), designer.get("name"), default="Unknown"
        ),
        "platform": "MyMiniFactory",
        "thumbnail_url": _myminifactory_image_url(item),
        "license": lic["name"],
        "license_url": lic["url"],
        "license_summary": lic["summary"],
        "download_url": url,
        "url": url,
        "_archive_download_url": archive,
        "requires_auth": True,
        "direct_import": bool(archive),
        "views": item.get("views"),
        "likes": item.get("likes"),
        "published_at": item.get("published_at"),
        "is_free": None,
    }


class MyMiniFactorySearcher:
    BASE = "https://www.myminifactory.com"
    API = BASE + "/api/v2/search"

    @staticmethod
    def search(query, context, options=None):
        if not isinstance(context, AuthManager) or not context.authenticated(
            "myminifactory"
        ):
            raise AuthRequired(
                "MyMiniFactory search requires a personal API key from MyMiniFactory"
            )
        page = _search_page_number(options)
        requested_sort = (
            str(options.get("sort") or "") if isinstance(options, dict) else ""
        )
        sort = {
            "popularity": "popularity",
            "views": "visits",
            "newest": "date",
        }.get(requested_sort, "popularity")
        response = context.request(
            "myminifactory",
            "GET",
            MyMiniFactorySearcher.API,
            params={
                "key": context.token("myminifactory"),
                "q": query,
                "page": page,
                "per_page": 30,
                "sort": sort,
                "order": "desc",
            },
            timeout=30,
        )
        if response.status_code in (401, 403):
            raise AuthRequired("MyMiniFactory rejected the saved API key")
        response.raise_for_status()
        payload = response.json()
        results = [_myminifactory_result(item) for item in payload.get("items") or []]
        total = _coalesce(
            payload.get("total_count"),
            payload.get("total"),
            (payload.get("meta") or {}).get("total")
            if isinstance(payload.get("meta"), dict)
            else None,
            default=None,
        )
        return _search_page_result(results, page, 30, total=total)

    @staticmethod
    def get_files(model, auth):
        if not auth.authenticated("myminifactory"):
            raise AuthRequired("MyMiniFactory requires a personal API key")
        archive = model.get("_archive_download_url") or ""
        if archive:
            return [{"name": "myminifactory_model.zip", "url": archive}]
        raise BrowserRequired(
            "MyMiniFactory API keys can search metadata, but file archives require the official account/OAuth download flow.",
            model.get("url", ""),
        )


def _browser_search_result(query, platform, url):
    return {
        "name": f"Open {platform} results for “{query}”",
        "author": "Browser search",
        "platform": platform,
        "thumbnail_url": "",
        "license": "Varies by model",
        "license_url": "",
        "license_summary": "Review the license on the selected model page.",
        "download_url": url,
        "url": url,
        "requires_auth": False,
        "direct_import": False,
        "result_type": "search_link",
        "is_free": None,
    }


class ThangsSearcher:
    BASE = "https://thangs.com"
    @staticmethod
    def search(query, context, options=None):
        url = f"{ThangsSearcher.BASE}/search/{urllib.parse.quote(query, safe='')}?scope=thangs"
        return [_browser_search_result(query, "Thangs", url)]

    @staticmethod
    def get_files(model, auth=None):
        raise BrowserRequired(
            "Thangs protects search and downloads with an interactive browser check.",
            model.get("url", ""),
        )


class CrealityCloudSearcher:
    BASE = "https://www.crealitycloud.com"
    SEARCH_URLS = (BASE + "/model-tags/{query}",)
    PATH_RE = r"/model-detail/[a-z0-9%._~+()-]+"

    @staticmethod
    def search(query, context, options=None):
        items = _catalog_search(
            query,
            CrealityCloudSearcher.SEARCH_URLS,
            CrealityCloudSearcher.PATH_RE,
            "Creality Cloud",
        )
        for item in items:
            item["is_free"] = None
        return items

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
    def search(query, context, options=None):
        if not isinstance(context, AuthManager) or not context.authenticated("grabcad"):
            raise AuthRequired(
                "GrabCAD requires a free member account to access/download Community Library models. Connect a browser session first."
            )
        return _catalog_search(
            query,
            GrabcadSearcher.SEARCH_URLS,
            GrabcadSearcher.PATH_RE,
            "GrabCAD",
            auth=context,
            requires_auth=True,
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


class YouMagineSearcher:
    BASE = "https://youmagine.com"
    SEARCH_URLS = (BASE + "/explore?query={query}",)
    PATH_RE = r"/designs/[a-f0-9-]+"

    @staticmethod
    def search(query, context, options=None):
        items = _catalog_search(
            query, YouMagineSearcher.SEARCH_URLS, YouMagineSearcher.PATH_RE, "YouMagine"
        )
        for item in items:
            item["is_free"] = None
        return items

    @staticmethod
    def get_files(model, auth=None):
        return _public_page_files(
            model,
            no_direct_message="YouMagine requires its official model page for this download.",
        )


class PinshapeSearcher:
    BASE = "https://pinshape.com"
    SEARCH_URLS = (BASE + "/items?search={query}",)
    PATH_RE = r"/items/\d+-[a-z0-9%._~+()-]+"

    @staticmethod
    def search(query, context, options=None):
        items = _catalog_search(
            query, PinshapeSearcher.SEARCH_URLS, PinshapeSearcher.PATH_RE, "Pinshape"
        )
        for item in items:
            if item.get("name", "").casefold() in ("free", "premium"):
                item["name"] = _slug_title(item.get("url", ""))
            item["direct_import"] = False
            item["is_free"] = None
        return items

    @staticmethod
    def get_files(model, auth=None):
        raise BrowserRequired(
            "Pinshape downloads use the official account/browser flow.",
            model.get("url", ""),
        )


class CgTraderSearcher:
    BASE = "https://www.cgtrader.com"

    @staticmethod
    def search(query, context, options=None):
        url = CgTraderSearcher.BASE + "/3d-models?keywords=" + urllib.parse.quote(
            query, safe=""
        )
        return [_browser_search_result(query, "CGTrader", url)]

    @staticmethod
    def get_files(model, auth=None):
        raise BrowserRequired(
            "CGTrader search and downloads require its interactive browser flow.",
            model.get("url", ""),
        )


_PLATFORM_SPECS = (
    PlatformSpec(
        "printables", "Printables", PrintablesSearcher, search_page_size=30
    ),
    PlatformSpec(
        "nexprint",
        "Nexprint",
        NexprintSearcher,
        auth_hosts=("nexprint.com",),
        auth_mode="named_cookie",
        login_url="https://www.nexprint.com/en/account/login",
        referer="https://www.nexprint.com/",
        cookie_domain=".nexprint.com",
        cookie_name="auth_token",
        search_page_size=30,
    ),
    PlatformSpec(
        "makeronline",
        "Makeronline",
        MakeronlineSearcher,
        auth_hosts=("makeronline.com", "anycubic.com"),
        auth_mode="anycubic",
        login_url=(
            "https://cas.anycubic.com/login/oauth/authorize"
            "?client_id=69ce24b6eaf78e597ac0&response_type=code"
            "&redirect_uri=https%3A%2F%2Fwww.makeronline.com%2Fen%2F"
            "&scope=read&state=ac_maker_online&lang=en"
        ),
        referer="https://www.makeronline.com/",
        search_page_size=30,
    ),
    PlatformSpec(
        "makerworld",
        "MakerWorld",
        MakerWorldSearcher,
        auth_hosts=("api.bambulab.com", "makerworld.com"),
        auth_mode="bearer",
        login_url="https://makerworld.com/en/sign-in",
        referer="https://makerworld.com/",
        search_page_size=30,
    ),
    PlatformSpec(
        "thingiverse",
        "Thingiverse",
        ThingiverseSearcher,
        auth_hosts=("api.thingiverse.com",),
        auth_mode="bearer",
        login_url="https://www.thingiverse.com/developers",
        referer="https://www.thingiverse.com/",
        search_page_size=30,
    ),
    PlatformSpec(
        "cults3d",
        "Cults3D",
        Cults3DSearcher,
        auth_hosts=("cults3d.com",),
        auth_mode="cookie_header",
        login_url="https://cults3d.com/en/users/sign_in",
        referer="https://cults3d.com/",
        cookie_domain=".cults3d.com",
    ),
    PlatformSpec(
        "myminifactory",
        "MyMiniFactory",
        MyMiniFactorySearcher,
        auth_hosts=("myminifactory.com",),
        auth_mode="api_key",
        login_url="https://www.myminifactory.com/pages/for-developers",
        referer="https://www.myminifactory.com/",
        search_page_size=30,
    ),
    PlatformSpec("thangs", "Thangs", ThangsSearcher),
    PlatformSpec("crealitycloud", "Creality Cloud", CrealityCloudSearcher),
    PlatformSpec(
        "grabcad",
        "GrabCAD",
        GrabcadSearcher,
        auth_hosts=("grabcad.com",),
        auth_mode="cookie_header",
        login_url="https://login.grabcad.com/login",
        referer="https://grabcad.com/library",
        cookie_domain=".grabcad.com",
    ),
    PlatformSpec("youmagine", "YouMagine", YouMagineSearcher),
    PlatformSpec("pinshape", "Pinshape", PinshapeSearcher),
    PlatformSpec("cgtrader", "CGTrader", CgTraderSearcher),
)
_PLATFORMS.update((spec.key, spec) for spec in _PLATFORM_SPECS)
_PLATFORMS_BY_DISPLAY.update((spec.display, spec) for spec in _PLATFORM_SPECS)


# ---------------------------------------------------------------------------
# Orca import and download helpers
# ---------------------------------------------------------------------------


def _current_orca_executable():
    """Return the executable of the OrcaSlicer process hosting this plugin."""
    candidate = os.path.realpath(sys.executable or "")
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
    except (AttributeError, OSError, TypeError, ValueError):
        return candidate if candidate and os.path.isfile(candidate) else ""
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
            if (
                not get_class_name(hwnd, class_buf, len(class_buf))
                or class_buf.value != "wxWindowNR"
            ):
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
            return (
                False,
                f"could not enumerate OrcaSlicer windows (Win32 error {last_error})",
            )
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
        send_message = user32.SendMessageTimeoutW
        send_message.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        send_message.restype = wintypes.LPARAM
        result = ctypes.c_size_t(0)
        SMTO_ABORTIFHUNG = 0x0002
        if not send_message(
            hwnd,
            WM_COPYDATA,
            0,
            ctypes.addressof(data),
            SMTO_ABORTIFHUNG,
            10000,
            ctypes.byref(result),
        ):
            error = int(get_last_error())
            return (
                False,
                f"OrcaSlicer main window did not accept the import message (Win32 error {error})",
            )
        return True, ""
    except (AttributeError, OSError, TypeError, ValueError) as exc:
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
        proc = subprocess.run(  # nosec B603
            [executable, *normalized],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"OrcaSlicer import handoff failed: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().replace("\n", " ")[:400]
        return False, detail or f"OrcaSlicer handoff exited with code {proc.returncode}"
    return True, ""


_LOADABLE_MODEL_EXTS = (
    ".3mf",
    ".stl",
    ".obj",
    ".step",
    ".stp",
    ".iges",
    ".igs",
    ".amf",
    ".ply",
    ".scad",
    ".fcstd",
    ".f3d",
)


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
                if member.file_size > _MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(
                        "Archive contains a model file larger than 500 MB"
                    )
                extracted_total += member.file_size
                if extracted_total > _MAX_ARCHIVE_BYTES:
                    raise RuntimeError("Archive extraction exceeds 1 GB safety limit")
                target = _unique_path(
                    dest_dir,
                    _safe_filename(
                        os.path.basename(member.filename), "model" + member_ext
                    ),
                )
                with archive.open(member, "r") as src, open(target, "wb") as dst:
                    while True:
                        chunk = src.read(262144)
                        if not chunk:
                            break
                        dst.write(chunk)
                loadable.append(target)
    return loadable


def _write_download_response(response, path):
    content_type = (response.headers.get("content-type") or "").lower()
    if "text/html" in content_type or "application/xhtml" in content_type:
        raise RuntimeError(
            "Platform returned an HTML/login page instead of a model file"
        )
    declared_size = response.headers.get("content-length")
    if declared_size:
        try:
            if int(declared_size) > _MAX_DOWNLOAD_BYTES:
                raise RuntimeError("Download exceeds 500 MB safety limit")
        except ValueError:
            pass
    total = 0
    with open(path, "wb") as fh:
        for chunk in response.iter_content(chunk_size=262144):
            if not chunk:
                continue
            total += len(chunk)
            if total > _MAX_DOWNLOAD_BYTES:
                raise RuntimeError("Download exceeds 500 MB safety limit")
            fh.write(chunk)


def _download_stream(url, name, dest_dir, auth, platform):
    import requests

    if not _is_http_url(url):
        raise ValueError("Refusing non-HTTP download URL")
    _reject_obvious_local_target(url)
    _ensure_private_dir(dest_dir)
    path = _unique_path(dest_dir, _safe_filename(name))
    session = auth.session(platform)
    response = None
    try:
        response = auth.request(
            platform,
            "GET",
            url,
            session=session,
            stream=True,
            timeout=180,
            allow_redirects=True,
        )
        spec = _platform(platform)
        if (
            response.status_code in (401, 403)
            and spec is not None
            and spec.requires_auth
        ):
            raise AuthRequired(
                f"{_display_name(platform)} session was rejected while downloading"
            )
        response.raise_for_status()
        _write_download_response(response, path)
    except (OSError, RuntimeError, requests.RequestException):
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    finally:
        if response is not None:
            response.close()
        session.close()
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
button,input,select{font:inherit}.search-row{display:flex;gap:8px;margin:12px 0}.search-row input{flex:1;padding:8px 12px;border:1px solid var(--orca-border,#444);border-radius:6px;background:var(--orca-bg,#1e1e1e);color:inherit}.btn,button{padding:7px 12px;border:0;border-radius:6px;background:var(--orca-accent,#4a9eff);color:var(--orca-accent-fg,#fff);cursor:pointer}.secondary{background:transparent!important;border:1px solid var(--orca-border,#555)!important;color:var(--orca-fg,#eee)!important}.danger{background:#7a3030!important}.muted{color:var(--orca-muted,#999)}
.accounts{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px;margin:10px 0}.account{border:1px solid var(--orca-border,#444);border-radius:7px;padding:8px}.account-head{display:flex;align-items:center;justify-content:space-between;gap:6px}.account strong{display:block;font-size:.86em}.auth-help{width:20px;height:20px;min-width:20px;padding:0!important;border:1px solid var(--orca-border,#666)!important;border-radius:50%!important;background:transparent!important;color:var(--orca-muted,#aaa)!important;font-size:.75em;font-weight:700;line-height:18px}.auth-help:hover,.auth-help:focus{border-color:var(--orca-accent,#4a9eff)!important;color:var(--orca-accent,#4a9eff)!important;outline:none}.auth-tooltip{position:fixed;z-index:80;display:none;width:min(330px,calc(100vw - 24px));padding:9px 11px;border:1px solid var(--orca-border,#666);border-radius:7px;background:var(--orca-bg,#202020);color:var(--orca-fg,#eee);box-shadow:0 5px 20px rgba(0,0,0,.5);font-size:.76em;line-height:1.4;text-align:left;pointer-events:none}.auth-tooltip.active{display:block}.auth-state{display:block;font-size:.75em;color:var(--orca-muted,#999);margin:3px 0 7px}.search-options{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:-4px 0 10px;font-size:.82em}.search-options select{padding:5px 8px;border:1px solid var(--orca-border,#555);border-radius:5px;background:var(--orca-bg,#222);color:inherit}.search-options label{display:flex;align-items:center;gap:5px}.source-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:10px 0 6px}.source-head strong{font-size:.9em}.source-tools{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.source-tools button{padding:4px 8px;font-size:.76em}.source-count{font-size:.76em;color:var(--orca-muted,#999)}.platforms{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:7px;margin-bottom:12px}.portal-option{display:flex;align-items:center;gap:8px;padding:8px 9px;border:1px solid var(--orca-border,#444);border-radius:6px;font-size:.84em;color:var(--orca-fg,#eee);cursor:pointer;user-select:none}.portal-option:hover{border-color:var(--orca-accent,#4a9eff)}.portal-option input{margin:0;accent-color:var(--orca-accent,#4a9eff)}
#results{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}.card{border:1px solid var(--orca-border,#444);border-radius:8px;padding:10px;cursor:pointer}.card:hover{border-color:var(--orca-accent,#4a9eff)}.card img{width:100%;height:110px;object-fit:cover;border-radius:4px;background:#333}.result-image{opacity:0;transition:opacity .18s ease}.result-image.loaded{opacity:1}.card h3{font-size:.9em;margin:6px 0 2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.author{font-size:.78em;color:var(--orca-muted,#888)}.metrics{font-size:.72em;color:var(--orca-muted,#999);min-height:1.2em;margin-top:3px}.license-badge{display:inline-block;padding:1px 7px;border-radius:3px;font-size:.72em;margin-top:4px;background:#444}.license-cc{background:#1a5c2a;color:#8f8}.license-arr{background:#5c3a1a;color:#fc6}
.pagination{display:none;align-items:center;justify-content:center;gap:8px;flex-wrap:wrap;margin:14px 0 4px}.pagination.active{display:flex}.pagination-summary{font-size:.8em;color:var(--orca-muted,#999);margin-right:4px}.pagination label{display:flex;align-items:center;gap:5px;font-size:.8em;color:var(--orca-muted,#999)}.pagination select{padding:5px 7px;border:1px solid var(--orca-border,#555);border-radius:5px;background:var(--orca-bg,#222);color:inherit}.page-numbers{display:flex;align-items:center;gap:4px}.page-button{min-width:34px;padding:6px 8px}.page-button.current{background:var(--orca-accent,#4a9eff)!important;color:var(--orca-accent-fg,#fff)!important;border-color:var(--orca-accent,#4a9eff)!important}.page-ellipsis{padding:0 2px;color:var(--orca-muted,#999)}
.source-results{display:none;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:6px;margin:0 0 12px}.source-results.active{display:grid}.source-result{padding:7px 9px;border:1px solid var(--orca-border,#444);border-radius:6px;font-size:.75em}.source-result strong,.source-result span,.source-result small{display:block}.source-result span{color:var(--orca-muted,#999);margin-top:2px}.source-result small{color:#e78b8b;margin-top:3px;overflow-wrap:anywhere}.load-more-row{display:none;justify-content:center;margin:10px 0 4px}.load-more-row.active{display:flex}.load-more-row button{min-width:220px}
.panel{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);width:min(650px,calc(100% - 32px));max-height:70vh;overflow:auto;z-index:20;padding:14px 34px 14px 14px;border:1px solid var(--orca-border,#444);border-radius:8px;background:var(--orca-bg,#1e1e1e);box-shadow:0 6px 28px rgba(0,0,0,.55);display:none}.panel.active{display:block}.close{position:absolute;right:8px;top:6px;background:none!important;font-size:1.35em;padding:2px 6px}.panel p{font-size:.86em;color:var(--orca-muted,#aaa);margin:6px 0}.panel a{color:var(--orca-accent,#4a9eff)}.responsibility{border-left:3px solid var(--orca-border,#444);padding:8px 10px;margin:10px 0;font-size:.78em;color:var(--orca-muted,#888)}
.modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.58);z-index:29;display:none}.modal-backdrop.active{display:block}.auth-modal{position:fixed;z-index:30;left:50%;top:50%;transform:translate(-50%,-50%);width:min(520px,calc(100% - 32px));background:var(--orca-bg,#1e1e1e);border:1px solid var(--orca-border,#555);border-radius:9px;padding:16px;display:none}.auth-modal.active{display:block}.field{margin:8px 0}.field label{display:block;font-size:.78em;color:var(--orca-muted,#999);margin-bottom:3px}.field input{width:100%;padding:8px;border:1px solid var(--orca-border,#555);background:var(--orca-bg,#222);color:inherit;border-radius:5px}.auth-note{font-size:.79em;color:var(--orca-muted,#aaa);line-height:1.4}.button-row{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}.file-modal{width:min(700px,calc(100% - 32px))}.file-list{max-height:46vh;overflow:auto;border:1px solid var(--orca-border,#555);border-radius:6px;margin:10px 0}.file-choice{display:grid;grid-template-columns:auto 96px minmax(0,1fr);align-items:center;gap:10px;padding:9px 10px;border-bottom:1px solid var(--orca-border,#444);cursor:pointer}.file-choice:last-child{border-bottom:0}.file-choice:has(input:checked){background:rgba(74,158,255,.06)}.file-choice input{margin:0}.file-preview,.file-preview-placeholder{width:96px;height:76px;border-radius:5px;background:#333}.file-preview{object-fit:cover;cursor:zoom-in}.file-preview:focus,.mw-cover:focus{outline:2px solid var(--orca-accent,#4a9eff);outline-offset:2px}.file-preview-placeholder{display:flex;align-items:center;justify-content:center;color:var(--orca-muted,#888);font-size:.7em}.file-details{min-width:0}.file-name{display:block;overflow-wrap:anywhere;font-weight:600;font-size:.86em}.file-meta{display:block;color:var(--orca-muted,#999);font-size:.75em;margin-top:4px}.file-tools{display:flex;gap:7px;margin:8px 0}.file-count{font-size:.8em;color:var(--orca-muted,#999)}.makerworld-modal{width:min(700px,calc(100% - 32px))}.mw-profiles{max-height:42vh;overflow:auto;margin:10px 0}.mw-profile{display:grid;grid-template-columns:auto 88px 1fr;gap:10px;align-items:center;padding:9px;border:1px solid var(--orca-border,#4b4b4b);border-radius:7px;margin:7px 0;cursor:pointer}.mw-profile:has(input:checked){border-color:var(--orca-accent,#4a9eff);background:rgba(74,158,255,.08)}.mw-profile input{margin:0}.mw-cover{width:88px;height:68px;object-fit:cover;border-radius:5px;background:#333;cursor:zoom-in}.image-preview{position:fixed;z-index:70;display:none;width:min(420px,55vw);max-height:65vh;object-fit:contain;border:1px solid var(--orca-border,#666);border-radius:8px;background:#222;box-shadow:0 8px 30px rgba(0,0,0,.7);pointer-events:none}.image-preview.active{display:block}.mw-title{display:block;font-weight:600;font-size:.88em}.mw-summary{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;font-size:.78em;line-height:1.3;margin-top:3px}.mw-meta{display:block;font-size:.75em;color:var(--orca-muted,#aaa);margin-top:4px}.mw-formats{display:grid;gap:7px;margin:10px 0}.mw-format{display:flex;gap:9px;padding:9px;border:1px solid var(--orca-border,#4b4b4b);border-radius:7px;cursor:pointer}.mw-format:has(input:checked){border-color:var(--orca-accent,#4a9eff)}.mw-format small{display:block;color:var(--orca-muted,#aaa);margin-top:2px}#status{margin-top:10px;color:var(--orca-muted,#999);font-size:.8em}
@media(max-width:680px){.accounts{grid-template-columns:1fr}}
</style>
<h1 style="margin:0;font-size:1.25em">&#128269; 3D Model Search</h1>
<div class="accounts">
  <div class="account"><div class="account-head"><strong>MakerWorld (Bambu)</strong><button type="button" class="auth-help" data-platform="makerworld" aria-label="MakerWorld authorization instructions" aria-describedby="auth-tooltip" onmouseenter="showAuthHelp(this)" onmouseleave="hideAuthHelp(this)" onfocus="showAuthHelp(this)" onblur="hideAuthHelp(this)">?</button></div><span id="auth-makerworld" class="auth-state">Checking...</span><button class="secondary" onclick="openAuth('makerworld')">Account</button></div>
  <div class="account"><div class="account-head"><strong>Nexprint (Elegoo)</strong><button type="button" class="auth-help" data-platform="nexprint" aria-label="Nexprint authorization instructions" aria-describedby="auth-tooltip" onmouseenter="showAuthHelp(this)" onmouseleave="hideAuthHelp(this)" onfocus="showAuthHelp(this)" onblur="hideAuthHelp(this)">?</button></div><span id="auth-nexprint" class="auth-state">Checking...</span><button class="secondary" onclick="openAuth('nexprint')">Account</button></div>
  <div class="account"><div class="account-head"><strong>Makeronline (Anycubic)</strong><button type="button" class="auth-help" data-platform="makeronline" aria-label="Makeronline authorization instructions" aria-describedby="auth-tooltip" onmouseenter="showAuthHelp(this)" onmouseleave="hideAuthHelp(this)" onfocus="showAuthHelp(this)" onblur="hideAuthHelp(this)">?</button></div><span id="auth-makeronline" class="auth-state">Checking...</span><button class="secondary" onclick="openAuth('makeronline')">Account</button></div>
  <div class="account"><div class="account-head"><strong>Cults3D</strong><button type="button" class="auth-help" data-platform="cults3d" aria-label="Cults3D authorization instructions" aria-describedby="auth-tooltip" onmouseenter="showAuthHelp(this)" onmouseleave="hideAuthHelp(this)" onfocus="showAuthHelp(this)" onblur="hideAuthHelp(this)">?</button></div><span id="auth-cults3d" class="auth-state">Checking...</span><button class="secondary" onclick="openAuth('cults3d')">Account</button></div>
  <div class="account"><div class="account-head"><strong>GrabCAD</strong><button type="button" class="auth-help" data-platform="grabcad" aria-label="GrabCAD authorization instructions" aria-describedby="auth-tooltip" onmouseenter="showAuthHelp(this)" onmouseleave="hideAuthHelp(this)" onfocus="showAuthHelp(this)" onblur="hideAuthHelp(this)">?</button></div><span id="auth-grabcad" class="auth-state">Checking...</span><button class="secondary" onclick="openAuth('grabcad')">Account</button></div>
  <div class="account"><div class="account-head"><strong>Thingiverse API</strong><button type="button" class="auth-help" data-platform="thingiverse" aria-label="Thingiverse authorization instructions" aria-describedby="auth-tooltip" onmouseenter="showAuthHelp(this)" onmouseleave="hideAuthHelp(this)" onfocus="showAuthHelp(this)" onblur="hideAuthHelp(this)">?</button></div><span id="auth-thingiverse" class="auth-state">Checking...</span><button class="secondary" onclick="openAuth('thingiverse')">API token</button></div>
  <div class="account"><div class="account-head"><strong>MyMiniFactory API</strong><button type="button" class="auth-help" data-platform="myminifactory" aria-label="MyMiniFactory authorization instructions" aria-describedby="auth-tooltip" onmouseenter="showAuthHelp(this)" onmouseleave="hideAuthHelp(this)" onfocus="showAuthHelp(this)" onblur="hideAuthHelp(this)">?</button></div><span id="auth-myminifactory" class="auth-state">Checking...</span><button class="secondary" onclick="openAuth('myminifactory')">API key</button></div>
</div>
<div id="auth-tooltip" class="auth-tooltip" role="tooltip"></div>
<div class="search-row"><input id="query" placeholder="Search for 3D models..."><button id="search-btn" onclick="doSearch()">Search</button></div>
<div class="search-options"><label>Sort <select id="sort"><option value="relevance">Relevance</option><option value="popularity">Popularity (normalized)</option><option value="downloads">Downloads</option><option value="likes">Likes</option><option value="rating">Rating</option><option value="newest">Newest</option><option value="makes">Most printed</option><option value="name">Name</option><option value="platform">Platform</option></select></label><label><input id="free-only" type="checkbox"> Free only</label><label><input id="direct-only" type="checkbox"> Direct import only</label></div>
<div class="source-head"><strong>Search portals</strong><div class="source-tools"><button class="secondary" onclick="setAllPortals(true)">Select all</button><button class="secondary" onclick="setAllPortals(false)">Select none</button><span id="source-count" class="source-count"></span></div></div>
<div class="platforms" id="search-portals">
<label class="portal-option"><input id="portal-thingiverse" class="portal-search" type="checkbox" data-platform="thingiverse"> Thingiverse</label>
<label class="portal-option"><input id="portal-cults3d" class="portal-search" type="checkbox" checked data-platform="cults3d"> Cults3D</label>
<label class="portal-option"><input id="portal-myminifactory" class="portal-search" type="checkbox" data-platform="myminifactory"> MyMiniFactory</label>
<label class="portal-option"><input id="portal-thangs" class="portal-search" type="checkbox" data-platform="thangs"> Thangs (browser)</label>
<label class="portal-option"><input id="portal-makeronline" class="portal-search" type="checkbox" checked data-platform="makeronline"> Makeronline</label>
<label class="portal-option"><input id="portal-crealitycloud" class="portal-search" type="checkbox" checked data-platform="crealitycloud"> Creality Cloud</label>
<label class="portal-option"><input id="portal-nexprint" class="portal-search" type="checkbox" checked data-platform="nexprint"> Nexprint</label>
<label class="portal-option"><input id="portal-grabcad" class="portal-search" type="checkbox" data-platform="grabcad"> GrabCAD</label>
<label class="portal-option"><input id="portal-printables" class="portal-search" type="checkbox" checked data-platform="printables"> Printables</label>
<label class="portal-option"><input id="portal-makerworld" class="portal-search" type="checkbox" checked data-platform="makerworld"> MakerWorld</label>
<label class="portal-option"><input id="portal-youmagine" class="portal-search" type="checkbox" checked data-platform="youmagine"> YouMagine</label>
<label class="portal-option"><input id="portal-pinshape" class="portal-search" type="checkbox" checked data-platform="pinshape"> Pinshape</label>
<label class="portal-option"><input id="portal-cgtrader" class="portal-search" type="checkbox" data-platform="cgtrader"> CGTrader (browser)</label>
</div>
<div id="source-results" class="source-results" aria-live="polite"></div>
<div id="results"></div>
<nav id="pagination" class="pagination" aria-label="Search result pages"><span id="pagination-summary" class="pagination-summary"></span><label>Per page <select id="page-size" onchange="changePageSize()"><option value="12">12</option><option value="24" selected>24</option><option value="48">48</option></select></label><button id="page-prev" class="secondary page-button" type="button" onclick="setResultPage(currentPage-1)" aria-label="Previous page">&larr;</button><span id="page-numbers" class="page-numbers"></span><button id="page-next" class="secondary page-button" type="button" onclick="setResultPage(currentPage+1)" aria-label="Next page">&rarr;</button></nav>
<div id="load-more-row" class="load-more-row"><button id="load-more" type="button" onclick="loadMoreResults()">Load more from portals</button></div>
<div id="detail" class="panel"><button class="close" onclick="closeDetail()">&times;</button><h2 id="det-name"></h2><p id="det-author"></p><p id="det-platform"></p><p id="det-metrics"></p><p id="det-url"></p><p id="det-license"></p><p id="det-summary"></p><p class="responsibility">Downloads use your own account session and the platform's own file URL. The plugin does not host or redistribute models. You remain responsible for the model license and the platform terms.</p><button id="det-import-btn" onclick="doImport()">Import into OrcaSlicer</button><button class="secondary" onclick="doDownload()">Open in browser</button></div>
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
<div id="file-modal" class="auth-modal file-modal">
  <h2 style="margin:0 0 5px;font-size:1.05em">Choose files to import</h2>
  <div class="auth-note">This model contains multiple downloadable files. Select the files that should be downloaded and added to the current OrcaSlicer project.</div>
  <div class="file-tools"><button class="secondary" onclick="setAllFiles(true)">Select all</button><button class="secondary" onclick="setAllFiles(false)">Select none</button><span id="file-count" class="file-count"></span></div>
  <div id="file-list" class="file-list"></div>
  <div class="button-row"><button id="file-import" onclick="confirmFileImport()">Import selected</button><button class="secondary" onclick="closeFilePicker()">Cancel</button></div>
</div>
<div id="makerworld-modal" class="auth-modal makerworld-modal">
  <h2 style="margin:0 0 5px;font-size:1.05em">Choose MakerWorld download</h2>
  <div class="auth-note">Select a print profile and file format. Direct import uses the official signed 3MF profile URL.</div>
  <div id="mw-profiles" class="mw-profiles"></div>
  <h3 style="margin:10px 0 4px;font-size:.9em">File format</h3>
  <div id="mw-formats" class="mw-formats"></div>
  <div class="button-row"><button id="mw-import" onclick="confirmMakerWorldChoice()">Download 3MF</button><button class="secondary" onclick="closeMakerWorldPicker()">Cancel</button></div>
</div>
<img id="image-preview" class="image-preview" alt="Enlarged file or print profile preview">
<div id="status">Ready.</div>
<script>
var selectedModel=null, searching=false, canLoadMore=false, authPlatform=null, authStates={}, pendingImport=null, pendingFiles=[], pendingMakerWorldModel=null, activeAuthHelp=null, resultImageObserver=null, makerWorldChoicesCache={}, makerWorldPrefetching={}, makerWorldPreloadedImages={}, currentPage=1, pageSize=24;
var $=function(id){return document.getElementById(id)};
var AUTH_HELP={makerworld:'Click Account, then sign in with your Bambu email and password, including the MFA code when requested. You can alternatively paste an existing Bambu Cloud access token. Passwords are never saved.',nexprint:'Click Account, open the official Nexprint login, and sign in. Copy the auth_token cookie value from that signed-in browser session, paste it into the plugin, and connect.',makeronline:'Open the official Anycubic OAuth login in your browser. After MakerOnline returns, copy the mo_access_token cookie value (or its Cookie header), paste it below, and connect. You can alternatively import the Anycubic Slicer Next session. The plugin never reads the browser profile or password.',cults3d:'Click Account, open the official Cults3D login, and sign in. From the signed-in browser request headers, copy the Cookie header or session-cookie string, paste it into the plugin, and connect.',grabcad:'A GrabCAD Community Library membership is required. Click Account, sign in on the official site, copy the Cookie request header or session-cookie string from the signed-in browser, and paste it into the plugin.',thingiverse:'Create or open a Thingiverse developer app, obtain your personal API access token, then click API token, paste it, and connect.',myminifactory:'Create a MyMiniFactory API client and obtain its API key. Click API key, paste the key, and connect. Storefront and OAuth-only downloads will still open in the browser.'};
function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;")}
function safeUrl(s){try{var u=new URL(String(s||''));return(u.protocol==='http:'||u.protocol==='https:')?u.href:''}catch(e){return''}}
function showAuthHelp(button){var tooltip=$('auth-tooltip'),text=AUTH_HELP[button.dataset.platform]||'';if(!text)return;activeAuthHelp=button;tooltip.textContent=text;tooltip.classList.add('active');tooltip.style.left='0px';tooltip.style.top='0px';var r=button.getBoundingClientRect(),gap=8,margin=8,left=Math.max(margin,Math.min(window.innerWidth-tooltip.offsetWidth-margin,r.left+r.width/2-tooltip.offsetWidth/2)),top=r.bottom+gap;if(top+tooltip.offsetHeight>window.innerHeight-margin)top=Math.max(margin,r.top-tooltip.offsetHeight-gap);tooltip.style.left=Math.round(left)+'px';tooltip.style.top=Math.round(top)+'px'}
function hideAuthHelp(button){if(activeAuthHelp!==button||button.matches(':hover')||document.activeElement===button)return;$('auth-tooltip').classList.remove('active');activeAuthHelp=null}
function platformKey(display){return {MakerWorld:'makerworld',Nexprint:'nexprint',Makeronline:'makeronline',Printables:'printables',Thingiverse:'thingiverse',Cults3D:'cults3d',MyMiniFactory:'myminifactory',Thangs:'thangs','Creality Cloud':'crealitycloud',GrabCAD:'grabcad',YouMagine:'youmagine',Pinshape:'pinshape',CGTrader:'cgtrader'}[display]||String(display||'').toLowerCase()}
function isAuthed(model){if(!model||!model.requires_auth)return true;var s=authStates[platformKey(model.platform)];return !!(s&&s.authenticated)}
function updateAuth(states){authStates=states||{};['makerworld','nexprint','makeronline','cults3d','grabcad','thingiverse','myminifactory'].forEach(function(p){var s=authStates[p]||{};$("auth-"+p).textContent=s.authenticated?("Connected: "+(s.label||'session')):'Not connected'});if(selectedModel)showDetail(selectedModel,false)}
var PORTAL_PREF_KEY='orca-model-search-portals-v2';
function selectedPortals(){var ps=[];document.querySelectorAll('.portal-search:checked').forEach(function(x){ps.push(x.dataset.platform)});return ps}
function updatePortalCount(){var all=document.querySelectorAll('.portal-search');var checked=document.querySelectorAll('.portal-search:checked');$('source-count').textContent=checked.length+' / '+all.length+' selected'}
function savePortalSelection(){try{localStorage.setItem(PORTAL_PREF_KEY,JSON.stringify(selectedPortals()))}catch(e){}}
function restorePortalSelection(){try{var raw=localStorage.getItem(PORTAL_PREF_KEY);if(raw){var saved=JSON.parse(raw);if(Array.isArray(saved)){var set={};saved.forEach(function(p){set[p]=true});document.querySelectorAll('.portal-search').forEach(function(x){x.checked=!!set[x.dataset.platform]})}}}catch(e){}updatePortalCount()}
function setAllPortals(value){document.querySelectorAll('.portal-search').forEach(function(x){x.checked=!!value});updatePortalCount();savePortalSelection()}
$('search-portals').addEventListener('change',function(e){if(e.target&&e.target.classList.contains('portal-search')){updatePortalCount();savePortalSelection()}});
function doSearch(){if(searching)return;var q=$('query').value.trim();if(!q)return;var ps=selectedPortals();if(!ps.length){$('status').textContent='Select at least one search portal.';return}searching=true;canLoadMore=false;$('search-btn').disabled=true;$('search-btn').textContent='Searching...';$('source-results').classList.remove('active');$('source-results').innerHTML='';$('load-more-row').classList.remove('active');$('status').textContent='Searching '+ps.length+' portal(s)...';closeDetail();orca.postMessage({action:'search',query:q,platforms:ps,options:{sort:$('sort').value,free_only:$('free-only').checked,direct_only:$('direct-only').checked}})}
$('query').addEventListener('keydown',function(e){if(e.key==='Enter')doSearch()});
function compactNumber(v){v=Number(v);if(!isFinite(v))return'';if(v>=1000000)return(v/1000000).toFixed(v>=10000000?0:1)+'M';if(v>=1000)return(v/1000).toFixed(v>=10000?0:1)+'K';return String(Math.round(v))}
function metricText(m){var p=[];if(m.downloads!=null)p.push('Downloads '+compactNumber(m.downloads));if(m.likes!=null)p.push('Likes '+compactNumber(m.likes));if(m.rating!=null)p.push('Rating '+Number(m.rating).toFixed(1));if(m.views!=null)p.push('Views '+compactNumber(m.views));if(m.makes!=null)p.push('Prints '+compactNumber(m.makes));return p.join(' / ')}
function loadResultImage(img){var url=img.dataset.src;if(!url)return;img.addEventListener('load',function(){img.classList.add('loaded')},{once:true});img.src=url;img.removeAttribute('data-src');if(img.complete)img.classList.add('loaded')}
function observeResultImages(){if(resultImageObserver){resultImageObserver.disconnect();resultImageObserver=null}var images=document.querySelectorAll('#results .result-image[data-src]');if(!('IntersectionObserver' in window)){images.forEach(loadResultImage);return}resultImageObserver=new IntersectionObserver(function(entries){entries.forEach(function(entry){if(!entry.isIntersecting)return;resultImageObserver.unobserve(entry.target);loadResultImage(entry.target)})},{rootMargin:'240px 0px'});images.forEach(function(img){resultImageObserver.observe(img)})}
function paginationItems(page,total){var items=[1],start=Math.max(2,page-2),end=Math.min(total-1,page+2);if(total<=1)return items;if(start>2)items.push('…');for(var i=start;i<=end;i++)items.push(i);if(end<total-1)items.push('…');items.push(total);return items}
function renderPagination(total,start,end){var pages=Math.max(1,Math.ceil(total/pageSize)),nav=$('pagination');currentPage=Math.max(1,Math.min(currentPage,pages));nav.classList.toggle('active',total>0);$('pagination-summary').textContent=total?(start+1)+'–'+end+' of '+total:'0 results';$('page-prev').disabled=currentPage<=1;$('page-next').disabled=currentPage>=pages;var html='';paginationItems(currentPage,pages).forEach(function(item){if(item==='…'){html+='<span class="page-ellipsis" aria-hidden="true">…</span>';return}var active=item===currentPage;html+='<button type="button" class="secondary page-button'+(active?' current':'')+'" onclick="setResultPage('+item+')"'+(active?' aria-current="page"':'')+' aria-label="Page '+item+'">'+item+'</button>'});$('page-numbers').innerHTML=html}
function renderResultPage(){var total=window._results.length,pages=Math.max(1,Math.ceil(total/pageSize));currentPage=Math.max(1,Math.min(currentPage,pages));var start=(currentPage-1)*pageSize,end=Math.min(start+pageSize,total),html='';window._results.slice(start,end).forEach(function(m,i){var image=safeUrl(m.thumbnail_url),index=start+i;html+='<div class="card" data-idx="'+index+'"><img class="result-image" loading="lazy" decoding="async" data-src="'+esc(image)+'" alt="'+esc(m.name||'Model preview')+'"><h3 title="'+esc(m.name)+'">'+esc(m.name)+'</h3><div class="author">'+esc(m.author)+' · '+esc(m.platform)+'</div><div class="metrics">'+esc(metricText(m))+'</div><span class="license-badge '+licenseClass(m.license)+'">'+esc(m.license||'Unknown')+'</span></div>'});$('results').innerHTML=html;renderPagination(total,start,end);observeResultImages();$('status').textContent=total+' result(s)'}
function renderResults(models,resetPage){window._results=models||[];if(resetPage!==false)currentPage=1;renderResultPage()}
function setResultPage(page){var pages=Math.max(1,Math.ceil(window._results.length/pageSize)),next=Math.max(1,Math.min(Number(page)||1,pages));if(next===currentPage)return;currentPage=next;closeDetail();renderResultPage();$('results').scrollIntoView({behavior:'smooth',block:'start'})}
function changePageSize(){var value=parseInt($('page-size').value,10);pageSize=value===12||value===48?value:24;currentPage=1;closeDetail();renderResultPage()}
function renderSourceResults(sources,more){var html='',moreCount=0;(sources||[]).forEach(function(s){var loaded=Number(s.loaded)||0,visible=Number(s.visible)||0,parts=[];if(s.total!=null)parts.push(loaded+' of '+Number(s.total)+' loaded');else parts.push(loaded+' loaded');if(visible!==loaded)parts.push(visible+' shown by filters');if(s.has_more){parts.push('more available');moreCount++}else if(!s.error){parts.push(s.paginated?'complete':'first page only')}html+='<div class="source-result'+(s.error?' error':'')+'"><strong>'+esc(s.display||s.key)+'</strong><span>'+esc(parts.join(' · '))+'</span>'+(s.error?'<small>'+esc(s.error)+'</small>':'')+'</div>'});$('source-results').innerHTML=html;$('source-results').classList.toggle('active',!!html);canLoadMore=!!more&&moreCount>0;$('load-more-row').classList.toggle('active',canLoadMore);$('load-more').disabled=false;$('load-more').textContent=moreCount===1?'Load next page from 1 portal':'Load next pages from '+moreCount+' portals'}
function loadMoreResults(){if(searching||!canLoadMore)return;searching=true;$('search-btn').disabled=true;$('load-more').disabled=true;$('load-more').textContent='Loading...';$('status').textContent='Loading next portal pages...';orca.postMessage({action:'search_more'})}
function modelIdentity(m){return String((m&&m._platform_key)||platformKey(m&&m.platform)||'')+'|'+String((m&&m._thing_id)||(m&&m._model_id)||(m&&m.url)||'')}
function preloadMakerWorldImages(key,profiles){var images=[];(profiles||[]).forEach(function(p){var url=safeUrl(p.cover);if(!url)return;var img=new Image();img.decoding='async';img.src=url;images.push(img)});makerWorldPreloadedImages[key]=images}
function cacheMakerWorldChoices(msg){var model=msg.model||selectedModel;if(!model)return;var key=modelIdentity(model);makerWorldChoicesCache[key]={profiles:msg.profiles||[],formats:msg.formats||[],default_profile_id:msg.default_profile_id||''};makerWorldPrefetching[key]=false;if(!makerWorldPreloadedImages[key])preloadMakerWorldImages(key,msg.profiles||[])}
function prefetchMakerWorld(m){if(!m||platformKey(m.platform)!=='makerworld')return;var key=modelIdentity(m);if(makerWorldChoicesCache[key]||makerWorldPrefetching[key])return;makerWorldPrefetching[key]=true;orca.postMessage({action:'prefetch_makerworld_profiles',model:m})}
function openModelDetail(m){var load=m._details_available&&!m._details_loaded&&!m._details_loading;if(load)m._details_loading=true;showDetail(m,true);prefetchMakerWorld(m);if(load)orca.postMessage({action:'model_details',model:m})}
$('results').addEventListener('click',function(e){var c=e.target.closest&&e.target.closest('.card');if(!c)return;var m=window._results[parseInt(c.dataset.idx,10)];if(m)openModelDetail(m)});
function showDetail(m,open){selectedModel=m;var loading=!!m._details_loading;$('det-name').textContent=m.name;$('det-author').innerHTML='<strong>Author:</strong> '+esc(m.author);$('det-platform').innerHTML='<strong>Platform:</strong> '+esc(m.platform);$('det-metrics').innerHTML=metricText(m)?'<strong>Metrics:</strong> '+esc(metricText(m)):'';$('det-license').innerHTML='<strong>License:</strong> <span class="license-badge '+licenseClass(m.license)+'">'+esc(loading?'Loading...':(m.license||'Unknown'))+'</span>';$('det-summary').textContent=loading?'Loading the official license and complete metrics...':(m.license_summary||'No license information available.');var modelUrl=safeUrl(m.url);$('det-url').innerHTML=modelUrl?'<strong>Model page:</strong> <a href="'+esc(modelUrl)+'">'+esc(modelUrl)+'</a>':'';var b=$('det-import-btn');b.disabled=m.result_type==='search_link';b.textContent=m.result_type==='search_link'?'Browser search only':(m.requires_auth&&!isAuthed(m))?('Log in to '+m.platform+' & import'):'Import into OrcaSlicer';if(open!==false)$('detail').classList.add('active')}
function applyModelDetails(m){m._details_loading=false;var idx=-1;for(var i=0;i<(window._results||[]).length;i++){if(modelIdentity(window._results[i])===modelIdentity(m)){idx=i;break}}if(idx>=0){window._results[idx]=m;renderResults(window._results,false)}if(selectedModel&&modelIdentity(selectedModel)===modelIdentity(m))showDetail(m,false);if(m._details_error)$('status').textContent='Could not load model details: '+m._details_error}
function closeDetail(){$('detail').classList.remove('active')}
document.addEventListener('pointerdown',function(e){var d=$('detail');if(!d||!d.classList.contains('active'))return;if(d.contains(e.target))return;closeDetail()},true);
$('detail').addEventListener('click',function(e){var a=e.target.closest&&e.target.closest('a[href]');if(!a)return;e.preventDefault();openExternal(a.getAttribute('href'))});
function licenseClass(l){if(/CC|Creative Commons|CC0|Public Domain/i.test(l||''))return'license-cc';if(/All Rights Reserved|Standard Digital|Exclusive/i.test(l||''))return'license-arr';return''}
function openExternal(url){orca.postMessage({action:'open_external',url:url})}
function doDownload(){if(selectedModel)openExternal(selectedModel.url||selectedModel.download_url)}
function doImport(){if(!selectedModel)return;if(selectedModel.result_type==='search_link'){doDownload();return}if(selectedModel.requires_auth&&!isAuthed(selectedModel)){pendingImport=selectedModel;openAuth(platformKey(selectedModel.platform));return}if(platformKey(selectedModel.platform)==='makerworld'){var cached=makerWorldChoicesCache[modelIdentity(selectedModel)];if(cached){showMakerWorldChoices({model:selectedModel,profiles:cached.profiles,formats:cached.formats,default_profile_id:cached.default_profile_id});$('status').textContent='Select a MakerWorld print profile and file format.';return}}var b=$('det-import-btn');b.disabled=true;b.textContent='Resolving...';$('status').textContent='Resolving files...';orca.postMessage({action:'resolve_import',model:selectedModel})}
function openAuth(p){authPlatform=p;$('auth-modal').classList.add('active');$('modal-bg').classList.add('active');$('auth-password').value='';$('auth-code').value='';$('auth-token').value='';$('code-field').style.display='none';$('import-anycubic').style.display=p==='makeronline'?'':'none';var tokenOnly=(p==='nexprint'||p==='makeronline'||p==='cults3d'||p==='grabcad'||p==='thingiverse'||p==='myminifactory');$('password-field').style.display=tokenOnly?'none':'';$('email-field').style.display=tokenOnly?'none':'';var title={makerworld:'MakerWorld / Bambu account',nexprint:'Nexprint / Elegoo account',makeronline:'Makeronline / Anycubic account',cults3d:'Cults3D account',grabcad:'GrabCAD account',thingiverse:'Thingiverse API',myminifactory:'MyMiniFactory API'}[p]||'Account';$('auth-title').textContent=title;$('token-label').textContent=p==='nexprint'?'Nexprint auth_token cookie value':p==='makeronline'?'MakerOnline mo_access_token value or Cookie header':p==='grabcad'?'GrabCAD Cookie header / session cookies':p==='cults3d'?'Cults3D Cookie header / session cookies':p==='thingiverse'?'Thingiverse access token':p==='myminifactory'?'MyMiniFactory API key':'Session/access token (alternative)';$('auth-note').textContent=AUTH_HELP[p]||'Use the account credentials supplied by the selected platform.';var st=authStates[p]||{};$('auth-logout').style.display=st.authenticated?'':'none'}
function syncBackdrop(){var active=$('auth-modal').classList.contains('active')||$('file-modal').classList.contains('active')||$('makerworld-modal').classList.contains('active');$('modal-bg').classList.toggle('active',active)}
function closeAuth(){$('auth-modal').classList.remove('active');$('auth-password').value='';$('auth-token').value='';syncBackdrop()}
function closeFilePicker(){$('file-modal').classList.remove('active');hideImagePreview(null,true);pendingFiles=[];syncBackdrop();var b=$('det-import-btn');if(b){b.disabled=false;b.textContent='Import into OrcaSlicer'}}
function closeMakerWorldPicker(){$('makerworld-modal').classList.remove('active');hideImagePreview(null,true);pendingMakerWorldModel=null;syncBackdrop();var b=$('det-import-btn');if(b){b.disabled=false;b.textContent='Import into OrcaSlicer'}}
function closeTopModal(){if($('file-modal').classList.contains('active'))closeFilePicker();else if($('makerworld-modal').classList.contains('active'))closeMakerWorldPicker();else closeAuth()}
function updateFileCount(){var all=document.querySelectorAll('#file-list input[type=checkbox]');var checked=document.querySelectorAll('#file-list input[type=checkbox]:checked');$('file-count').textContent=checked.length+' / '+all.length+' selected';$('file-import').disabled=checked.length===0}
function formatBytes(v){v=Number(v);if(!isFinite(v)||v<0)return'';var units=['B','KB','MB','GB'],i=0;while(v>=1024&&i<units.length-1){v/=1024;i++}return(v>=10||i===0?Math.round(v):Math.round(v*10)/10)+' '+units[i]}
function showFilePicker(files){pendingFiles=files||[];var html='';pendingFiles.forEach(function(f){var name=f.name||('File '+(Number(f.index)+1)),image=safeUrl(f.preview_url),preview=image?'<img class="file-preview" src="'+esc(image)+'" loading="lazy" decoding="async" alt="'+esc(name)+' preview" tabindex="0" onmouseenter="showImagePreview(this)" onmouseleave="hideImagePreview(this)" onfocus="showImagePreview(this)" onblur="hideImagePreview(this)">':'<span class="file-preview-placeholder">No preview</span>',meta=formatBytes(f.size);html+='<label class="file-choice"><input type="checkbox" checked value="'+Number(f.index)+'" onchange="updateFileCount()">'+preview+'<span class="file-details"><span class="file-name">'+esc(name)+'</span>'+(meta?'<span class="file-meta">'+esc(meta)+'</span>':'')+'</span></label>'});$('file-list').innerHTML=html;$('file-modal').classList.add('active');syncBackdrop();updateFileCount()}
function setAllFiles(value){document.querySelectorAll('#file-list input[type=checkbox]').forEach(function(x){x.checked=!!value});updateFileCount()}
function confirmFileImport(){var selected=[];document.querySelectorAll('#file-list input[type=checkbox]:checked').forEach(function(x){selected.push(parseInt(x.value,10))});if(!selected.length)return;$('file-import').disabled=true;$('status').textContent='Downloading selected files...';$('file-modal').classList.remove('active');syncBackdrop();orca.postMessage({action:'import_selected',indices:selected})}
function profileMeta(p){var v=[];if(p.creator)v.push('by '+p.creator);if(p.printer)v.push(p.printer);if(p.layer_height)v.push(p.layer_height+' layer');if(p.walls)v.push(p.walls+' walls');if(p.infill)v.push(p.infill+' infill');if(p.prediction)v.push(Math.max(1,Math.round(Number(p.prediction)/3600*10)/10)+' h');if(p.plates)v.push(p.plates+' plate'+(Number(p.plates)===1?'':'s'));if(p.rating!=null)v.push('Rating '+p.rating+(p.rating_count?' ('+p.rating_count+')':''));return v.join(' / ')}
function updateMakerWorldChoice(){var f=document.querySelector('#mw-formats input:checked');var p=document.querySelector('#mw-profiles input:checked');$('mw-import').disabled=!(f&&p);$('mw-import').textContent=f&&f.value==='raw_browser'?'Open STL/CAD files in MakerWorld':'Download selected 3MF'}
function positionImagePreview(source){var preview=$('image-preview'),r=source.getBoundingClientRect(),gap=12,margin=10,left=r.right+gap,top=r.top;if(left+preview.offsetWidth>window.innerWidth-margin)left=r.left-preview.offsetWidth-gap;left=Math.max(margin,Math.min(left,window.innerWidth-preview.offsetWidth-margin));top=Math.max(margin,Math.min(top,window.innerHeight-preview.offsetHeight-margin));preview.style.left=Math.round(left)+'px';preview.style.top=Math.round(top)+'px';preview.style.visibility='visible'}
function showImagePreview(source){var url=safeUrl(source.currentSrc||source.src);if(!url)return;var preview=$('image-preview');preview.src=url;preview.classList.add('active');preview.style.visibility='hidden';requestAnimationFrame(function(){positionImagePreview(source)})}
function hideImagePreview(source,force){if(!force&&source&&(source.matches(':hover')||document.activeElement===source))return;var preview=$('image-preview');preview.classList.remove('active');preview.style.visibility='hidden'}
function showMakerWorldChoices(msg){cacheMakerWorldChoices(msg);pendingMakerWorldModel=msg.model||selectedModel;var profiles=msg.profiles||[],formats=msg.formats||[],defaultId=String(msg.default_profile_id||'');var phtml='';profiles.forEach(function(p,i){var id=String(p.profile_id||''),title=p.title||'Print profile';var image=safeUrl(p.cover),summary=p.summary?'<span class="mw-summary">'+esc(p.summary)+'</span>':'';phtml+='<label class="mw-profile"><input type="radio" name="mw-profile" value="'+esc(id)+'" '+((id===defaultId||(!defaultId&&i===0))?'checked':'')+' onchange="updateMakerWorldChoice()">'+(image?'<img class="mw-cover" src="'+esc(image)+'" alt="'+esc(title)+'" tabindex="0" onmouseenter="showImagePreview(this)" onmouseleave="hideImagePreview(this)" onfocus="showImagePreview(this)" onblur="hideImagePreview(this)">':'<span class="mw-cover"></span>')+'<span><span class="mw-title">'+esc(title)+'</span>'+summary+'<span class="mw-meta">'+esc(profileMeta(p))+'</span></span></label>'});$('mw-profiles').innerHTML=phtml;var fhtml='';formats.forEach(function(f,i){if(f.available===false)return;fhtml+='<label class="mw-format"><input type="radio" name="mw-format" value="'+esc(f.id)+'" '+(i===0?'checked':'')+' onchange="updateMakerWorldChoice()"><span><strong>'+esc(f.label)+'</strong><small>'+esc(f.description||'')+'</small></span></label>'});$('mw-formats').innerHTML=fhtml;$('makerworld-modal').classList.add('active');syncBackdrop();updateMakerWorldChoice()}
function confirmMakerWorldChoice(){var p=document.querySelector('#mw-profiles input:checked'),f=document.querySelector('#mw-formats input:checked');if(!p||!f||!pendingMakerWorldModel)return;$('mw-import').disabled=true;$('status').textContent=f.value==='3mf'?'Resolving selected MakerWorld profile...':'Opening MakerWorld...';orca.postMessage({action:'resolve_makerworld_choice',model:pendingMakerWorldModel,profile_id:p.value,format:f.value})}
function submitAuth(){var token=$('auth-token').value.trim(),email=$('auth-email').value.trim(),password=$('auth-password').value,code=$('auth-code').value.trim();if(authPlatform==='nexprint'&&!token){$('status').textContent='Nexprint: paste auth_token after signing in.';return}if(authPlatform==='makeronline'&&!token){$('status').textContent='Makeronline: import the Anycubic Slicer Next session or paste an access token.';return}if(authPlatform==='grabcad'&&!token){$('status').textContent='GrabCAD: paste the Cookie header/session cookies after signing in.';return}if(authPlatform==='cults3d'&&!token){$('status').textContent='Cults3D: paste the Cookie header/session cookies after signing in.';return}if((authPlatform==='thingiverse'||authPlatform==='myminifactory')&&!token){$('status').textContent='Paste the API token/key first.';return}orca.postMessage({action:'auth_login',platform:authPlatform,token:token,email:email,password:password,code:code});$('auth-submit').disabled=true;$('status').textContent='Saving session...'}
function logoutAuth(){orca.postMessage({action:'auth_logout',platform:authPlatform});closeAuth()}
function openOfficialLogin(){orca.postMessage({action:'auth_open_login',platform:authPlatform});if(authPlatform==='makeronline')$('status').textContent='Anycubic login opened. After MakerOnline returns, copy mo_access_token, paste it here, and connect.'}
function importAnycubic(){orca.postMessage({action:'auth_import_anycubic'});$('status').textContent='Looking for Anycubic Slicer Next session...'}
orca.onMessage(function(msg){
  msg=msg||{};
  if(msg.action==='results'){
    searching=false;$('search-btn').disabled=false;$('search-btn').textContent='Search';renderResults(msg.results||[],msg.append?false:true);renderSourceResults(msg.sources||[],msg.can_load_more);
  }else if(msg.action==='model_details'){
    applyModelDetails(msg.model||{});
  }else if(msg.action==='auth_status'||msg.action==='auth_changed'){
    updateAuth(msg.states||{});$('auth-submit').disabled=false;
    if(msg.action==='auth_changed'){
      closeAuth();$('status').textContent=msg.message||'Account session updated.';
      if(pendingImport&&isAuthed(pendingImport)){var m=pendingImport;pendingImport=null;selectedModel=m;doImport()}
      else if(selectedModel){prefetchMakerWorld(selectedModel)}
    }
  }else if(msg.action==='auth_challenge'){
    $('auth-submit').disabled=false;$('code-field').style.display='';$('status').textContent=msg.message||'Verification code required.';
  }else if(msg.action==='auth_required'){
    $('det-import-btn').disabled=false;$('det-import-btn').textContent='Import into OrcaSlicer';$('status').textContent=msg.message||'Login required.';pendingImport=msg.model||selectedModel;openAuth(msg.platform);
  }else if(msg.action==='makerworld_prefetched'){
    cacheMakerWorldChoices(msg);
  }else if(msg.action==='makerworld_prefetch_failed'){
    makerWorldPrefetching[modelIdentity(msg.model||{})]=false;
  }else if(msg.action==='makerworld_choices'){
    showMakerWorldChoices(msg);$('status').textContent='Select a MakerWorld print profile and file format.';
  }else if(msg.action==='file_choices'){
    showFilePicker(msg.files||[]);$('status').textContent='Select one or more files to import.';
  }else if(msg.action==='status'){
    $('status').textContent=msg.message;
  }else if(msg.action==='imported'){
    closeFilePicker();closeMakerWorldPicker();$('status').textContent='Imported '+msg.count+' file(s) into the current OrcaSlicer project.';
  }else if(msg.action==='downloaded_only'){
    closeFilePicker();closeMakerWorldPicker();$('status').textContent='Downloaded '+msg.count+' file(s) to '+msg.dir+'. '+msg.message;
  }else if(msg.action==='browser_required'){
    closeMakerWorldPicker();$('status').textContent=msg.message||'This model must be downloaded in the browser.';if(msg.url)openExternal(msg.url);
  }else if(msg.action==='opened'){
    $('status').textContent='Opened in your browser.';
  }else if(msg.action==='activate_search'){
    var q=$('query');if(q){q.focus();q.select()}
  }else if(msg.action==='error'){
    searching=false;$('search-btn').disabled=false;$('search-btn').textContent='Search';$('auth-submit').disabled=false;
    if($('load-more')){$('load-more').disabled=false;$('load-more').textContent='Load more from portals'}
    if($('det-import-btn')){$('det-import-btn').disabled=false;$('det-import-btn').textContent='Import into OrcaSlicer'}
    if($('file-import'))$('file-import').disabled=false;
    if($('mw-import'))$('mw-import').disabled=false;
    $('status').textContent='Error: '+msg.message;
  }
});
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
            self._search_lock = threading.RLock()
            self._search_generation = 0
            self._search_loading_more = False
            self._search_query = ""
            self._search_platforms = []
            self._search_options = {}
            self._search_results = []
            self._search_pages = {}
            self._search_stats = {}

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
            self._post(
                {"action": action, "states": self.auth.status(), "message": message}
            )

        @staticmethod
        def _start(target, *args):
            threading.Thread(target=target, args=args, daemon=True).start()

        def on_message(self, msg):
            msg = msg or {}
            handler = {
                "search": self._handle_search,
                "search_more": self._handle_search_more,
                "model_details": self._handle_model_details,
                "prefetch_makerworld_profiles": self._handle_makerworld_prefetch,
                "resolve_import": self._handle_resolve_import,
                "resolve_makerworld_choice": self._handle_makerworld_choice,
                "import_selected": self._handle_import_selected,
                "open_external": self._handle_open_external,
                "auth_status": self._handle_auth_status,
                "auth_login": self._handle_auth_login,
                "auth_logout": self._handle_auth_logout,
                "auth_open_login": self._handle_auth_open_login,
                "auth_import_anycubic": self._handle_auth_import_anycubic,
            }.get(msg.get("action", ""))
            if handler is not None:
                handler(msg)

        def _handle_search(self, msg):
            with self._search_lock:
                self._search_generation += 1
                generation = self._search_generation
                self._search_loading_more = False
            self._start(self._do_search, msg, generation)

        def _handle_search_more(self, _msg):
            with self._search_lock:
                if self._search_loading_more or not self._search_query:
                    return
                self._search_loading_more = True
                generation = self._search_generation
            self._start(self._do_search_more, generation)

        def _handle_model_details(self, msg):
            model = msg.get("model") or {}
            if model:
                self._start(self._do_model_details, model)

        def _handle_makerworld_prefetch(self, msg):
            model = msg.get("model") or {}
            if model:
                self._start(self._prefetch_makerworld_profiles, model)

        def _handle_resolve_import(self, msg):
            model = msg.get("model") or {}
            if model:
                self._start(self._resolve_import, model)

        def _handle_makerworld_choice(self, msg):
            model = msg.get("model") or {}
            if model:
                self._start(self._resolve_makerworld_choice, model, msg)

        def _handle_import_selected(self, msg):
            self._start(self._import_selected, msg.get("indices") or [])

        def _handle_open_external(self, msg):
            self._start(self._open_external, msg.get("url", ""))

        def _handle_auth_status(self, _msg):
            self._post_auth()

        def _handle_auth_login(self, msg):
            self._start(self._do_auth_login, msg)

        def _handle_auth_logout(self, msg):
            platform = msg.get("platform", "")
            spec = _platform(platform)
            if spec is not None and spec.requires_auth:
                self.auth.logout(platform)
            self._post_auth("auth_changed", "Session removed.")

        def _handle_auth_open_login(self, msg):
            spec = _platform(msg.get("platform", ""))
            if spec is not None and spec.login_url:
                self._start(self._open_external, spec.login_url)

        def _handle_auth_import_anycubic(self, _msg):
            self._start(self._do_import_anycubic)

        @staticmethod
        def _token_login_error(platform):
            messages = {
                "makeronline": (
                    "Makeronline direct email/password login is no longer supported. "
                    "Import the Anycubic Slicer Next session or paste an access token."
                ),
                "nexprint": "Nexprint login requires auth_token from the official signed-in browser session",
                "cults3d": "Cults3D requires session cookies from the official signed-in browser session",
                "grabcad": "GrabCAD requires session cookies from the official signed-in browser session",
            }
            return messages.get(platform, "Unknown platform")

        def _save_login(self, msg):
            platform = msg.get("platform", "")
            token = (msg.get("token") or "").strip()
            email = (msg.get("email") or "").strip()
            if token:
                self.auth.save_token(
                    platform, token, label=email or "Connected session"
                )
                return
            if platform == "makerworld":
                self.auth.login_makerworld(
                    email,
                    password=msg.get("password"),
                    code=(msg.get("code") or "").strip(),
                )
                return
            raise AuthError(self._token_login_error(platform))

        def _do_auth_login(self, msg):
            import requests

            platform = msg.get("platform", "")
            # Never log the incoming message: it may contain a password/token.
            try:
                self._save_login(msg)
            except VerificationRequired as exc:
                self._post(
                    {
                        "action": "auth_challenge",
                        "platform": platform,
                        "message": str(exc),
                    }
                )
                return
            except (AuthError, OSError, ValueError, requests.RequestException) as exc:
                self._post({"action": "error", "message": str(exc)})
                return
            finally:
                # Drop references to secrets promptly.
                msg.pop("password", None)
                msg.pop("token", None)
            self._post_auth(
                "auth_changed", f"{_display_name(platform)} session connected."
            )

        def _do_import_anycubic(self):
            try:
                data = self.auth.import_anycubic_slicer_token()
            except (AuthError, OSError, ValueError) as exc:
                self._post({"action": "error", "message": str(exc)})
                return
            self._post_auth(
                "auth_changed",
                f"Imported Anycubic session from {data.get('source', 'Anycubic Slicer Next')}.",
            )

        def _load_search_page(self, spec, query, options, page):
            page_options = dict(options)
            page_options["page"] = page
            items = spec.adapter.search(query, self.auth, page_options)
            total = getattr(items, "total", None)
            has_more = getattr(items, "has_more", None)
            rows = list(items)
            for item in rows:
                item["_platform_key"] = spec.key
                item["authenticated"] = not item.get(
                    "requires_auth"
                ) or self.auth.authenticated(spec.key)
                item["importable"] = callable(
                    getattr(spec.adapter, "get_files", None)
                )
            if not spec.paginated_search:
                has_more = False
            elif has_more is None:
                has_more = len(rows) >= spec.search_page_size
            return rows, total, bool(has_more)

        def _search_payload(self, *, append):
            with self._search_lock:
                results = list(self._search_results)
                options = dict(self._search_options)
                platforms = list(self._search_platforms)
                stats = {key: dict(value) for key, value in self._search_stats.items()}
            visible_results = _filter_and_sort_results(results, options)
            visible_counts = {}
            for item in visible_results:
                key = str(item.get("_platform_key") or "")
                visible_counts[key] = visible_counts.get(key, 0) + 1
            sources = []
            for key in platforms:
                source = stats.get(key)
                if source is None:
                    continue
                source["visible"] = visible_counts.get(key, 0)
                sources.append(source)
            return {
                "action": "results",
                "append": append,
                "results": visible_results,
                "sources": sources,
                "can_load_more": any(source.get("has_more") for source in sources),
            }

        def _do_search(self, msg, generation):
            query = str(msg.get("query") or "").strip()
            options = msg.get("options") if isinstance(msg.get("options"), dict) else {}
            platforms = [
                key
                for key in dict.fromkeys(msg.get("platforms", []))
                if _platform(key) is not None
            ]
            results = []
            stats = {}
            pages = {}
            for key in platforms:
                spec = _platform(key)
                if spec is None:
                    continue
                try:
                    items, total, has_more = self._load_search_page(
                        spec, query, options, 1
                    )
                    results, _added = _merge_unique_results(results, items)
                    pages[key] = 1
                    stats[key] = {
                        "key": key,
                        "display": spec.display,
                        "loaded": sum(
                            item.get("_platform_key") == key for item in results
                        ),
                        "page": 1,
                        "total": total,
                        "has_more": has_more,
                        "paginated": spec.paginated_search,
                        "error": "",
                    }
                except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                    pages[key] = 0
                    stats[key] = {
                        "key": key,
                        "display": spec.display,
                        "loaded": 0,
                        "page": 0,
                        "total": None,
                        "has_more": False,
                        "paginated": spec.paginated_search,
                        "error": str(exc),
                    }
            with self._search_lock:
                if generation != self._search_generation:
                    return
                self._search_query = query
                self._search_platforms = platforms
                self._search_options = dict(options)
                self._search_results = results
                self._search_pages = pages
                self._search_stats = stats
                self._search_loading_more = False
            self._post(self._search_payload(append=False))

        def _do_search_more(self, generation):
            with self._search_lock:
                query = self._search_query
                options = dict(self._search_options)
                platforms = list(self._search_platforms)
                results = list(self._search_results)
                pages = dict(self._search_pages)
                stats = {key: dict(value) for key, value in self._search_stats.items()}
            for key in platforms:
                source = stats.get(key) or {}
                if not source.get("has_more"):
                    continue
                spec = _platform(key)
                if spec is None:
                    continue
                next_page = pages.get(key, 0) + 1
                try:
                    items, total, has_more = self._load_search_page(
                        spec, query, options, next_page
                    )
                    results, added = _merge_unique_results(results, items)
                    pages[key] = next_page
                    source.update(
                        {
                            "loaded": sum(
                                item.get("_platform_key") == key for item in results
                            ),
                            "page": next_page,
                            "total": total if total is not None else source.get("total"),
                            "has_more": has_more and added > 0,
                            "error": "",
                        }
                    )
                except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                    source["error"] = str(exc)
                stats[key] = source
            with self._search_lock:
                if generation != self._search_generation:
                    return
                self._search_results = results
                self._search_pages = pages
                self._search_stats = stats
                self._search_loading_more = False
            self._post(self._search_payload(append=True))

        def _do_model_details(self, model):
            result = dict(model)
            spec = _platform_for_model(model)
            loader = getattr(spec.adapter, "get_details", None) if spec else None
            if not callable(loader):
                result["_details_loaded"] = True
            else:
                try:
                    loaded = loader(model, self.auth)
                    if not isinstance(loaded, dict):
                        raise TypeError("Model detail loader returned invalid data")
                    result = loaded
                except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                    result["_details_loaded"] = True
                    result["_details_error"] = str(exc)
            result["_details_loading"] = False
            identity = _result_identity(result)
            with self._search_lock:
                for index, item in enumerate(self._search_results):
                    if _result_identity(item) == identity:
                        self._search_results[index] = dict(result)
                        break
            self._post({"action": "model_details", "model": result})

        def _resolve_import(self, model):
            platform_name = model.get("platform", "")
            spec = _platform_for_model(model)
            if spec is None:
                self._post(
                    {
                        "action": "error",
                        "message": f"Import is not supported for {platform_name or 'this platform'}",
                    }
                )
                return
            if model.get("requires_auth") and not self.auth.authenticated(spec.key):
                self._post(
                    {
                        "action": "auth_required",
                        "platform": spec.key,
                        "message": f"Log in to {platform_name} before importing.",
                        "model": model,
                    }
                )
                return
            if spec.key == "makerworld" and not model.get("_download_format"):
                self._show_makerworld_choices(model)
                return
            files = self._list_model_files(spec, model)
            if files is None:
                return
            normalized = _normalize_download_files(files)
            if not normalized:
                self._post(
                    {
                        "action": "error",
                        "message": "The platform returned no valid downloadable file URLs.",
                    }
                )
                return
            self._begin_import(model, normalized)

        def _show_makerworld_choices(self, model):
            try:
                choices = MakerWorldSearcher.get_download_choices(model, self.auth)
            except AuthRequired as exc:
                self.auth.logout("makerworld")
                self._post_auth("auth_changed", "MakerWorld session expired.")
                self._post(
                    {
                        "action": "auth_required",
                        "platform": "makerworld",
                        "message": str(exc),
                        "model": model,
                    }
                )
                return
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                self._post(
                    {
                        "action": "error",
                        "message": f"Could not load MakerWorld profiles: {exc}",
                    }
                )
                return
            self._post(
                {
                    "action": "makerworld_choices",
                    "model": model,
                    **choices,
                }
            )

        def _prefetch_makerworld_profiles(self, model):
            try:
                choices = MakerWorldSearcher.get_download_choices(model, self.auth)
            except (AuthRequired, OSError, ValueError, RuntimeError, KeyError, TypeError):
                self._post({"action": "makerworld_prefetch_failed", "model": model})
                return
            self._post(
                {
                    "action": "makerworld_prefetched",
                    "model": model,
                    **choices,
                }
            )

        def _resolve_makerworld_choice(self, model, msg):
            spec = _platform_for_model(model)
            if spec is None or spec.key != "makerworld":
                self._post({"action": "error", "message": "Invalid platform choice."})
                return
            profile_id = str(msg.get("profile_id") or "")
            download_format = str(msg.get("format") or "")
            if not profile_id.isdigit():
                self._post(
                    {"action": "error", "message": "Select a MakerWorld print profile."}
                )
                return
            if download_format == "raw_browser":
                try:
                    design_id = _model_identifier(
                        model,
                        "_model_id",
                        r"/models/(\d+)",
                        "MakerWorld design id is missing",
                    )
                except ValueError as exc:
                    self._post({"action": "error", "message": str(exc)})
                    return
                url = (
                    f"{MakerWorldSearcher.BASE}/en/models/{design_id}"
                    f"#profileId-{profile_id}"
                )
                self._post(
                    {
                        "action": "browser_required",
                        "message": (
                            "MakerWorld STL/CAD files require its signed-in browser "
                            "download flow. The selected profile was opened there."
                        ),
                        "url": url,
                    }
                )
                return
            if download_format != "3mf":
                self._post({"action": "error", "message": "Select a download format."})
                return
            selected = dict(model)
            selected["_profile_id"] = profile_id
            selected["_download_format"] = "3mf"
            self._resolve_import(selected)

        def _list_model_files(self, spec, model):
            platform_name = spec.display
            try:
                files = spec.adapter.get_files(model, self.auth)
            except AuthRequired as exc:
                self.auth.logout(spec.key)
                self._post_auth("auth_changed", f"{platform_name} session expired.")
                self._post(
                    {
                        "action": "auth_required",
                        "platform": spec.key,
                        "message": str(exc),
                        "model": model,
                    }
                )
                return None
            except BrowserRequired as exc:
                self._post(
                    {
                        "action": "browser_required",
                        "message": str(exc),
                        "url": exc.url or model.get("url", ""),
                    }
                )
                return None
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                self._post(
                    {"action": "error", "message": f"Could not list files: {exc}"}
                )
                return None
            if not files:
                self._post(
                    {
                        "action": "error",
                        "message": "No downloadable files were returned by the platform.",
                    }
                )
                return None
            return files

        def _begin_import(self, model, normalized):
            if len(normalized) == 1:
                self._download_and_import(model, normalized)
                return

            with self._pending_import_lock:
                self._pending_import_model = dict(model)
                self._pending_import_files = normalized
            self._post(
                {
                    "action": "file_choices",
                    "files": [
                        {
                            "index": i,
                            "name": item["name"],
                            "preview_url": item.get("preview_url", ""),
                            "size": item.get("size"),
                        }
                        for i, item in enumerate(normalized)
                    ],
                }
            )

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
                self._post(
                    {
                        "action": "error",
                        "message": "File selection expired. Press Import again to refresh the file list.",
                    }
                )
                return
            selected = [files[i] for i in selected_indices if i < len(files)]
            if not selected:
                self._post(
                    {
                        "action": "error",
                        "message": "Select at least one file to import.",
                    }
                )
                return
            self._download_and_import(model, selected)

        def _download_and_import(self, model, files):
            import requests

            platform_name = model.get("platform", "")
            spec = _platform_for_model(model)
            platform_key = spec.key if spec is not None else ""
            dest_dir = _download_dir()
            try:
                _ensure_private_dir(dest_dir)
            except OSError as exc:
                self._post(
                    {
                        "action": "error",
                        "message": f"Cannot create download directory {dest_dir}: {exc}",
                    }
                )
                return

            paths = []
            for index, item in enumerate(files, 1):
                name = item.get("name") or f"model_{index}.3mf"
                self._post(
                    {
                        "action": "status",
                        "message": f"Downloading {index}/{len(files)}: {name}",
                    }
                )
                try:
                    paths.append(
                        _download_stream(
                            item.get("url", ""), name, dest_dir, self.auth, platform_key
                        )
                    )
                except AuthRequired as exc:
                    self.auth.logout(platform_key)
                    self._post_auth("auth_changed", f"{platform_name} session expired.")
                    self._post(
                        {
                            "action": "auth_required",
                            "platform": platform_key,
                            "message": str(exc),
                            "model": model,
                        }
                    )
                    return
                except (
                    OSError,
                    ValueError,
                    RuntimeError,
                    requests.RequestException,
                ) as exc:
                    self._post({"action": "error", "message": f"{name}: {exc}"})
                    return

            try:
                load_paths = _expand_archives(paths, dest_dir)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                self._post(
                    {
                        "action": "error",
                        "message": f"Downloaded files, but archive extraction failed: {exc}",
                    }
                )
                return
            if not load_paths:
                self._post(
                    {
                        "action": "downloaded_only",
                        "count": len(paths),
                        "dir": dest_dir,
                        "message": "No directly loadable STL/3MF/CAD file was found in the download.",
                    }
                )
                return

            self._post(
                {
                    "action": "status",
                    "message": f"Adding {len(load_paths)} file(s) to the current OrcaSlicer project...",
                }
            )
            ok, detail = _load_in_orca(load_paths)
            if ok:
                self._post(
                    {"action": "imported", "count": len(load_paths), "dir": dest_dir}
                )
            else:
                self._post(
                    {
                        "action": "downloaded_only",
                        "count": len(load_paths),
                        "dir": dest_dir,
                        "message": detail,
                    }
                )

        def _open_external(self, url):
            if not _is_http_url(url):
                self._post(
                    {"action": "error", "message": "Refusing to open non-HTTP URL."}
                )
                return
            try:
                if not webbrowser.open(url, new=2):
                    raise RuntimeError("No system browser accepted the URL")
                self._post({"action": "opened", "url": url})
            except (OSError, RuntimeError) as exc:
                self._post(
                    {"action": "error", "message": f"Could not open browser: {exc}"}
                )

    @_orca.plugin
    class SearchEnginePlugin(_orca.base):
        def register_capabilities(self):
            _orca.register_capability(SearchEngineScript)
