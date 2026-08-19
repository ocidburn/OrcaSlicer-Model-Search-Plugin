# /// script
# requires-python = ">=3.12"
# dependencies = ["requests"]
#
# [tool.orcaslicer.plugin]
# name = "3D Model Search Engine"
# description = "Search, sort, and import 3D-printable models from community and public institutional catalogs."
# author = "Tommaso Bianchi"
# version = "0.8.9"
# ///

import hmac
import html
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import subprocess  # nosec B404
import sys
import threading
import time
import urllib.parse
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

try:
    import orca
except ImportError:
    orca = None


_BROWSER_UA = (
    "OrcaSlicer-Model-Search-Plugin/0.8.9 "
    "(+https://github.com/ocidburn/OrcaSlicer-Model-Search-Plugin)"
)
_STANDARD_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
    "Gecko/20100101 Firefox/140.0"
)
_HTML_ACCEPT = "text/html,application/xhtml+xml"
_STANDARD_BROWSER_HTML_HEADERS = {
    "User-Agent": _STANDARD_BROWSER_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
_MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
_MAX_REDIRECTS = 5
_DETAIL_PREFETCH_WORKERS = 4
_SEARCH_WORKERS = 8

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
    "cxy-sl": "Standard Digital File License",
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


class CloudflareChallenge(BrowserRequired):
    """Cloudflare requires an interactive browser verification step.

    `url` is the page a human should open; `host` is the host whose check is
    failing.  They differ when a site's API lives on its own subdomain.
    """

    def __init__(self, message, url="", host=""):
        super().__init__(message, url)
        self.host = host or _url_host(url)


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
        "profile_picker",
        "referer",
        "search_page_size",
        "session_recheck",
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
        profile_picker=False,
        session_recheck=False,
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
        self.profile_picker = bool(profile_picker)
        self.session_recheck = bool(session_recheck)

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


def _popularity_ranks(by_platform):
    """Map each raw score to its first ascending position per platform."""
    ranks = {}
    for platform, values in by_platform.items():
        positions = {}
        for position, value in enumerate(sorted(values)):
            positions.setdefault(value, position)
        ranks[platform] = (positions, len(values))
    return ranks


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
    ranks = _popularity_ranks(by_platform)
    for item in results:
        positions, count = ranks.get(item.get("platform", ""), ({}, 0))
        raw = item.get("_popularity_raw", 0.0)
        if not count or raw <= 0:
            item["popularity"] = None
            continue
        item["popularity"] = (
            100.0 if count == 1 else 100.0 * positions[raw] / (count - 1)
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


def _source_stats(
    spec,
    *,
    page=0,
    total=None,
    has_more=False,
    error="",
    browser_url="",
    cloudflare_host="",
):
    """Create one search-source status row; loaded is filled after merging."""
    return {
        "key": spec.key,
        "display": spec.display,
        "loaded": 0,
        "page": page,
        "total": total,
        "has_more": has_more,
        "paginated": spec.paginated_search,
        "error": error,
        "browser_url": browser_url,
        "cloudflare_host": cloudflare_host,
    }


def _apply_loaded_counts(results, stats):
    """Count merged rows once instead of rescanning them for every portal."""
    counts = {}
    for item in results:
        key = item.get("_platform_key")
        counts[key] = counts.get(key, 0) + 1
    for key, source in stats.items():
        source["loaded"] = counts.get(key, 0)


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


def _session_recheck(key):
    spec = _platform(key)
    return spec is not None and spec.session_recheck


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


class CloudflareClearance:
    """Replay a Cloudflare check that a human completed in their own browser.

    Nothing here solves, answers, or circumvents a challenge.  The user opens
    the protected page, passes the check themselves, and copies the resulting
    clearance in.  Cloudflare binds that clearance to the exact browser
    User-Agent and to the client IP address, so the cookie and the User-Agent
    are stored and replayed as a pair -- replaying the cookie under a different
    User-Agent is the usual reason a pasted clearance silently does nothing.
    """

    COOKIE = "cf_clearance"
    STORE_KEY = "__cloudflare__"

    def __init__(self, store=None):
        self.store = store or AuthStore()

    def _records(self):
        data = self.store.get(self.STORE_KEY)
        return {
            str(host): dict(record)
            for host, record in data.items()
            if isinstance(record, dict)
        }

    @staticmethod
    def normalize_host(target):
        host = _url_host(target)
        if host:
            return host
        host = str(target or "").strip().lower().strip(".")
        return host if re.fullmatch(r"[a-z0-9.-]{1,253}", host or "") else ""

    @classmethod
    def parse_cookie(cls, value):
        text = str(value or "").strip()
        if text.lower().startswith("cookie:"):
            text = text.split(":", 1)[1].strip()
        match = re.search(r"(?:^|[;\s])cf_clearance=([^;\s]+)", text)
        if match:
            return match.group(1).strip()
        # A bare cookie value is accepted; some other cookie pair is not.
        return "" if "=" in text or " " in text else text

    def save(self, target, cookie, user_agent):
        host = self.normalize_host(target)
        if not host:
            raise AuthError("A Cloudflare-protected host is required")
        token = self.parse_cookie(cookie)
        if not token:
            raise AuthError(
                "Paste the cf_clearance cookie from the browser tab that passed "
                "the check"
            )
        agent = " ".join(str(user_agent or "").split())
        if not agent:
            raise AuthError(
                "The browser User-Agent is required: Cloudflare ties the "
                "clearance to it, so the cookie alone will not be accepted"
            )
        record = {
            "cf_clearance": token,
            "user_agent": agent,
            "saved_at": int(time.time()),
        }
        records = self._records()
        records[host] = record
        self.store.set(self.STORE_KEY, records)
        return dict(record, host=host)

    def for_url(self, url):
        """Return the clearance covering this URL's host, if one is stored.

        A clearance saved for a parent domain also covers its subdomains,
        matching how the browser scopes the cookie it was copied from.
        """
        host = _url_host(url)
        if not host:
            return {}
        for candidate, record in self._records().items():
            if _host_matches(host, (candidate,)):
                return record
        return {}

    def apply(self, url, headers):
        """Pin the stored User-Agent into headers; return cookies for the host."""
        record = self.for_url(url)
        token = record.get("cf_clearance") or ""
        agent = record.get("user_agent") or ""
        if not token or not agent:
            return {}
        # Cloudflare validates the clearance against the agent that earned it.
        headers["User-Agent"] = agent
        return {self.COOKIE: token}

    def forget(self, target):
        host = self.normalize_host(target)
        records = self._records()
        removed = [name for name in records if host and _host_matches(host, (name,))]
        if not removed:
            return False
        for name in removed:
            records.pop(name, None)
        self.store.set(self.STORE_KEY, records)
        return True

    def status(self):
        return [
            {
                "host": host,
                "user_agent": record.get("user_agent", ""),
                "saved_at": record.get("saved_at"),
            }
            for host, record in sorted(self._records().items())
        ]


def _cloudflare_challenge(url, auth=None, detail="", host=""):
    """Report a challenge, retiring a stored clearance that stopped working."""
    host = host or _url_host(url)
    stale = bool(auth is not None and host and auth.clearance.forget(host))
    if stale:
        message = (
            f"The saved Cloudflare verification for {host} is no longer accepted "
            "and has been removed. A clearance expires after a while and is tied "
            "to your IP address and browser User-Agent, so it stops working if "
            "any of those changed. Pass the check again in your browser and add "
            "the new clearance."
        )
    else:
        message = detail or (
            f"{host or 'This catalog'} is asking for a Cloudflare browser check. "
            "Open the page, complete the verification yourself, then add the "
            "cf_clearance cookie together with your browser User-Agent under "
            "Cloudflare verification."
        )
    return CloudflareChallenge(message, url, host)


_LOGIN_TIMEOUT_SECONDS = 300

_LOGIN_PAGE_STYLE = (
    "body{font:15px/1.5 system-ui,sans-serif;margin:0;padding:32px;"
    "background:#1e1e1e;color:#eee}"
    "main{max-width:640px;margin:0 auto}"
    "h1{font-size:1.3em;margin:0 0 4px}"
    "p{color:#bbb}"
    "ol{color:#bbb;padding-left:20px}"
    "code{background:#2b2b2b;padding:1px 5px;border-radius:4px;color:#eee}"
    "textarea{width:100%;min-height:120px;background:#151515;color:#eee;"
    "border:1px solid #444;border-radius:6px;padding:10px;font:13px monospace}"
    "button{margin-top:12px;padding:9px 16px;border:0;border-radius:6px;"
    "background:#4a9eff;color:#fff;font-size:1em;cursor:pointer}"
    ".bad{color:#e78b8b}.good{color:#8fd18f}"
)


def _login_page(title, body):
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title><style>{_LOGIN_PAGE_STYLE}</style>"
        f"</head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"
    ).encode()


class LoginReceiver:
    """Finish a portal sign-in in the browser the user signed in with.

    OrcaSlicer's plugin UI can only render HTML the plugin supplies: there is
    no API to load a portal's own login page in-process and no access to any
    cookie jar, so a portal session cannot be captured automatically and has to
    be handed over deliberately.  This receiver moves that hand-over into the
    browser tab where the user has just signed in -- it listens on loopback
    only, for one credential, for a few minutes -- instead of asking them to
    carry the value back into OrcaSlicer by hand.

    A portal that can be configured to redirect to this loopback origin (an
    OAuth app the user registers themselves) completes without any paste at
    all.  Portals whose OAuth clients are registered against their own domains
    cannot redirect here; for those the browser page is still the drop-off
    point, which is why both routes exist.
    """

    def __init__(self, platform, on_credential, timeout=_LOGIN_TIMEOUT_SECONDS):
        self.platform = platform
        self._on_credential = on_credential
        self.timeout = timeout
        self.state = secrets.token_urlsafe(24)
        self._server = None
        self._timer = None
        self._lock = threading.RLock()
        self._done = False

    @property
    def origin(self):
        with self._lock:
            server = self._server
        if server is None:
            return ""
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def url(self):
        origin = self.origin
        if not origin:
            return ""
        return f"{origin}/connect?state={urllib.parse.quote(self.state)}"

    @property
    def finished(self):
        with self._lock:
            return self._done

    def start(self):
        """Bind loopback and serve until a credential arrives or time runs out."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), _login_handler(self))
        server.daemon_threads = True
        with self._lock:
            self._server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        timer = threading.Timer(self.timeout, self.stop)
        timer.daemon = True
        timer.start()
        with self._lock:
            self._timer = timer
        return self.url

    def stop(self):
        with self._lock:
            server, self._server = self._server, None
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        if server is not None:
            # shutdown() blocks until serve_forever returns, so keep it off the
            # serving thread.
            threading.Thread(target=self._close, args=(server,), daemon=True).start()

    @staticmethod
    def _close(server):
        try:
            server.shutdown()
        finally:
            server.server_close()

    def valid_state(self, value):
        return hmac.compare_digest(str(value or ""), self.state)

    def submit(self, value):
        """Hand one credential to the plugin. Returns (accepted, message)."""
        with self._lock:
            if self._done:
                return False, "This sign-in link has already been used."
        try:
            self._on_credential(self.platform, value)
        except (AuthError, OSError, ValueError) as exc:
            return False, str(exc)
        with self._lock:
            self._done = True
        return True, ""


def _login_handler(receiver):
    """Build a request handler bound to one receiver."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "OrcaSlicerModelSearch"
        sys_version = ""

        def log_message(self, *_args):
            """Stay silent: a request line here can contain the credential."""

        def _reply(self, status, page):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(page)

        def _is_local(self):
            """Only this machine, and only pages this receiver served."""
            host = self.headers.get("Host", "")
            if not re.fullmatch(r"(?:127\.0\.0\.1|localhost)(?::\d+)?", host):
                return False
            origin = receiver.origin
            for name in ("Origin", "Referer"):
                value = self.headers.get(name)
                if value and origin and not value.startswith(origin):
                    return False
            return True

        def _query(self):
            parsed = urllib.parse.urlsplit(self.path)
            return parsed.path, urllib.parse.parse_qs(parsed.query)

        def do_GET(self):
            path, query = self._query()
            if not self._is_local():
                self._reply(403, _login_page("Blocked", "<p>Local requests only.</p>"))
                return
            if not receiver.valid_state(_first(query.get("state"))):
                self._reply(
                    403,
                    _login_page(
                        "Link expired",
                        "<p>Start the sign-in again from OrcaSlicer.</p>",
                    ),
                )
                return
            if path == "/callback":
                self._handle_callback(query)
                return
            if path != "/connect":
                self._reply(404, _login_page("Not found", "<p>Nothing here.</p>"))
                return
            self._reply(200, _connect_page(receiver.platform, receiver.state, ""))

        def _handle_callback(self, query):
            value = _coalesce(
                _first(query.get("access_token")),
                _first(query.get("auth_token")),
                _first(query.get("token")),
                _first(query.get("code")),
                default="",
            )
            if not value:
                self._reply(
                    400,
                    _login_page(
                        "Nothing to connect",
                        "<p>The portal redirect carried no token.</p>",
                    ),
                )
                return
            ok, message = receiver.submit(value)
            self._reply(200 if ok else 400, _login_result(ok, message))
            if ok:
                # The reply is written, so tearing the listener down now cannot
                # truncate it.
                receiver.stop()

        def do_POST(self):
            if not self._is_local():
                self._reply(403, _login_page("Blocked", "<p>Local requests only.</p>"))
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 64 * 1024:
                self._reply(400, _login_page("Bad request", "<p>Empty submission.</p>"))
                return
            fields = urllib.parse.parse_qs(
                self.rfile.read(length).decode("utf-8", "replace")
            )
            if not receiver.valid_state(_first(fields.get("state"))):
                self._reply(
                    403,
                    _login_page(
                        "Link expired",
                        "<p>Start the sign-in again from OrcaSlicer.</p>",
                    ),
                )
                return
            value = _first(fields.get("value"), "")
            ok, message = receiver.submit(value)
            if ok:
                self._reply(200, _login_result(True, ""))
                receiver.stop()
                return
            self._reply(
                400, _connect_page(receiver.platform, receiver.state, message)
            )

    return Handler


def _connect_page(platform, state, error):
    spec = _platform(platform)
    display = spec.display if spec else platform
    hint = _LOGIN_HINTS.get(
        platform, "Paste the session value the portal issued after you signed in."
    )
    problem = (
        f'<p class="bad">{html.escape(error)}</p>' if error else ""
    )
    body = (
        f"<p>Signed in to {html.escape(display)} in this browser? "
        "Finish the connection here and OrcaSlicer picks it up automatically "
        "&mdash; you do not need to switch back and retype anything.</p>"
        f"<ol><li>{hint}</li><li>Paste it below and press Connect.</li></ol>"
        f"{problem}"
        '<form method="post" action="/connect">'
        f'<input type="hidden" name="state" value="{html.escape(state)}">'
        '<textarea name="value" autofocus placeholder="Paste here"></textarea>'
        "<div><button type=\"submit\">Connect</button></div>"
        "</form>"
        "<p>This page is served by the plugin on your own machine and is "
        "reachable only from it. Nothing is sent anywhere else.</p>"
    )
    return _login_page(f"Connect {display}", body)


def _login_result(ok, message):
    if ok:
        return _login_page(
            "Connected",
            '<p class="good">OrcaSlicer has the session. You can close this tab.</p>',
        )
    return _login_page(
        "Not connected",
        f'<p class="bad">{html.escape(message or "The value was not accepted.")}</p>'
        "<p>Start the sign-in again from OrcaSlicer.</p>",
    )


_LOGIN_HINTS = {
    "nexprint": (
        "Copy the <code>auth_token</code> cookie value from this signed-in "
        "session."
    ),
    "makeronline": (
        "Copy the <code>mo_access_token</code> cookie value that MakerOnline "
        "set after the Anycubic login."
    ),
    "cults3d": "Copy the <code>Cookie</code> request header of this session.",
    "grabcad": "Copy the <code>Cookie</code> request header of this session.",
    "crealitycloud": (
        "Copy the <code>model_token</code> cookie value (include "
        "<code>model_user_id</code> if you have it)."
    ),
    "thingiverse": "Copy your personal Thingiverse API access token.",
    "myminifactory": "Copy your MyMiniFactory API key.",
    "thangs": (
        "Copy the token from an <code>Authorization: Bearer</code> request "
        "header of this signed-in session."
    ),
    "makerworld": (
        "Copy the Bambu Cloud access token, or sign in directly from "
        "OrcaSlicer instead."
    ),
}


class AuthManager:
    BAMBU_LOGIN = "https://api.bambulab.com/v1/user-service/user/login"

    def __init__(self, store=None):
        self.store = store or AuthStore()
        self.clearance = CloudflareClearance(self.store)

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
        if token.lower().startswith("authorization:"):
            token = token.split(":", 1)[1].strip()
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
            m = re.search(
                r"(?i)(?:^|[;\s])mo_access_token=([^;\s]+)", token
            ) or re.match(
                r"(?i)^(?:XX-Token|Authorization)\s*:\s*(?:Bearer\s+)?(.+)$",
                token,
            )
            if m:
                token = m.group(1).strip()
        if mode == "creality" and token.lower().startswith("cookie:"):
            token = token.split(":", 1)[1].strip()
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

    def _apply_clearance(self, session, url, headers):
        """Attach a human-completed Cloudflare clearance for this exact host."""
        cookies = self.clearance.apply(url, headers)
        if not cookies:
            return
        # Scope to the exact host so a later redirect cannot carry it elsewhere.
        session.cookies.set(
            CloudflareClearance.COOKIE,
            cookies[CloudflareClearance.COOKIE],
            domain=_url_host(url),
            path="/",
        )

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
            self._apply_clearance(session, current_url, headers)
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
_MODEL_FILE_EXTS = _LOADABLE_MODEL_EXTS + (".zip",)
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


def _is_cloudflare_challenge(response):
    headers = response.headers or {}
    if str(headers.get("cf-mitigated") or "").strip().casefold() == "challenge":
        return True
    sample = str(getattr(response, "text", "") or "")[:20000].casefold()
    if "challenges.cloudflare.com/turnstile" in sample and "cf-turnstile" in sample:
        return True
    if response.status_code not in (403, 429, 503):
        return False
    if "cloudflare" not in str(headers.get("server") or "").casefold():
        return False
    return any(
        marker in sample
        for marker in (
            "just a moment",
            "challenge-platform",
            "cf-chl-",
            "attention required",
        )
    )


def _fetch_html(url, auth=None, platform="", timeout=30):
    """Fetch HTML with the same redirect and SSRF policy for every caller."""
    if auth is not None and platform:
        manager = auth
        key = platform
    else:
        manager = AuthManager()
        key = ""
    session = manager.session(key)

    def request(headers):
        return manager.request(
            key,
            "GET",
            url,
            session=session,
            timeout=timeout,
            allow_redirects=True,
            headers=headers,
        )

    response = None
    try:
        response = request({"User-Agent": _BROWSER_UA, "Accept": _HTML_ACCEPT})
        if _is_cloudflare_challenge(response):
            response.close()
            response = request(dict(_STANDARD_BROWSER_HTML_HEADERS))
            if _is_cloudflare_challenge(response):
                raise _cloudflare_challenge(response.url or url, manager)
        if response.status_code in (401, 403) and _session_recheck(platform):
            display = _display_name(platform)
            raise AuthRequired(
                f"{display} rejected the browser session. Sign in again and paste a fresh Cookie header."
            )
        response.raise_for_status()
        return response.text, response.url
    finally:
        if response is not None:
            response.close()
        session.close()


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
        except BrowserRequired:
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
    if response.status_code in (401, 403) and _session_recheck(platform):
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
        try:
            response = _request_probe(
                session, url, request_auth, platform, use_range=True
            )
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
    finally:
        session.close()


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
        if _session_recheck(platform_key) and _looks_like_login_page(
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


def _makeronline_thumbnail_url(value):
    """Return MakerOnline's CDN image unchanged, apart from URL normalization."""
    value = _decode_embedded_url(value)
    if value.startswith("//"):
        value = "https:" + value
    url = urllib.parse.urljoin(MAKERONLINE_BASE, value)
    return url if _is_http_url(url) else ""


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
                    "thumbnail_url": _makeronline_thumbnail_url(
                        item.get("mold_image")
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
        try:
            response = auth.request(
                "makeronline",
                "GET",
                f"{MAKERONLINE_BASE}/api/mold/detail",
                session=session,
                params={"id": mold_id},
                timeout=30,
            )
            try:
                if response.status_code in (401, 403):
                    raise AuthRequired(
                        "Makeronline session was rejected; log in again"
                    )
                detail = _api_data(response, "Makeronline detail API failed")
            finally:
                response.close()
            return _file_records(
                detail.get("files"),
                ("url", "file_url", "download_url"),
                ("file_name", "name"),
            )
        finally:
            session.close()


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
    DETAIL_URL = (
        f"{NEXPRINT_BASE}/gateway/api/v1/model-library-server/model-base-info/get"
    )
    DOWNLOAD_URL = (
        f"{NEXPRINT_BASE}/gateway/api/v1/model-library-server/common/"
        "presigned-download-url"
    )
    MODEL_CONFIG_FILE = "5"

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
        session = auth.session("nexprint")
        try:
            detail = NexprintSearcher._load_detail(model, auth, session)
            profile_id = str(model.get("_profile_id") or "").strip()
            if not profile_id:
                return NexprintSearcher._legacy_model_files(detail)
            if not profile_id.isdigit():
                raise ValueError("Select a Nexprint print profile before downloading")
            if model.get("_download_format") != "3mf":
                raise ValueError("Nexprint direct profile import supports 3MF only")
            selected = NexprintSearcher._selected_profile(detail, profile_id)
            file_info = NexprintSearcher._profile_file(selected)
            signed = NexprintSearcher._signed_profile(
                profile_id, file_info, auth, session
            )
            return [
                NexprintSearcher._profile_download(
                    selected, file_info, signed, model
                )
            ]
        finally:
            session.close()

    @staticmethod
    def _legacy_model_files(detail):
        records = detail.get("modelFileInfoList")
        if not records:
            records = detail.get("files")
        if not records:
            records = detail.get("modelFileList")
        return _file_records(
            records,
            ("fileUrl", "url", "downloadUrl"),
            ("fileName", "name"),
        )

    @staticmethod
    def _selected_profile(detail, profile_id):
        for item in detail.get("settingInfoList") or []:
            if isinstance(item, dict) and str(item.get("id") or "") == profile_id:
                return item
        raise ValueError("The selected Nexprint print profile is unavailable")

    @staticmethod
    def _profile_file(selected):
        file_info = selected.get("settingFile")
        if not isinstance(file_info, dict) or not file_info.get("fileId"):
            raise RuntimeError("The selected Nexprint profile has no downloadable 3MF")
        return file_info

    @staticmethod
    def _signed_profile(profile_id, file_info, auth, session):
        response = auth.request(
            "nexprint",
            "POST",
            NexprintSearcher.DOWNLOAD_URL,
            session=session,
            json={
                "fileInfoList": [
                    {
                        "fileId": str(file_info["fileId"]),
                        "type": NexprintSearcher.MODEL_CONFIG_FILE,
                        "bizId": profile_id,
                    }
                ]
            },
            timeout=30,
        )
        try:
            payload = NexprintSearcher._response_data(
                response, "Nexprint could not create a signed profile URL", auth=True
            )
        finally:
            response.close()
        files = payload.get("fileInfoList") if isinstance(payload, dict) else None
        if not isinstance(files, list) or not files or not isinstance(files[0], dict):
            raise RuntimeError("Nexprint did not return signed profile metadata")
        return files[0]

    @staticmethod
    def _profile_download(selected, file_info, signed, model):
        url = _coalesce(
            signed.get("resultUrl"),
            signed.get("downloadUrl"),
            signed.get("fileUrl"),
            signed.get("url"),
        )
        if not _is_http_url(url):
            raise RuntimeError("Nexprint did not return a signed 3MF URL")
        name_source = _coalesce(
            file_info.get("fileName"),
            selected.get("settingName"),
            model.get("name"),
            default="nexprint-profile.3mf",
        )
        name = _safe_filename(name_source, "nexprint-profile.3mf")
        if os.path.splitext(name)[1].lower() != ".3mf":
            name += ".3mf"
        return {
            "name": name,
            "url": url,
            "preview_url": NexprintSearcher._profile_cover(selected),
            "size": _number(file_info.get("fileSize"), integer=True),
        }

    @staticmethod
    def _response_data(response, error_message, *, auth=False):
        if response.status_code in (401, 403):
            raise AuthRequired(
                "Nexprint session was rejected; refresh auth_token and log in again"
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("Nexprint returned an invalid API response")
        code = _number(payload.get("code"), integer=True)
        if code not in (None, 0, 200):
            if auth and code in (401, 403):
                raise AuthRequired(
                    "Nexprint session was rejected; refresh auth_token and log in again"
                )
            raise RuntimeError(
                str(payload.get("msg") or payload.get("message") or error_message)
            )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _model_id(model):
        model_id = str(
            _model_identifier(
                model,
                "_model_id",
                r"/models/([A-Za-z0-9_-]+)",
                "Nexprint model id is missing",
            )
        ).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", model_id):
            raise ValueError("Nexprint model id is invalid")
        return model_id

    @staticmethod
    def _load_detail(model, auth=None, session=None):
        import requests

        model_id = NexprintSearcher._model_id(model)
        owned_session = None
        if auth is None:
            response = requests.get(
                NexprintSearcher.DETAIL_URL,
                params={"id": model_id},
                headers={"User-Agent": _BROWSER_UA},
                timeout=30,
            )
        else:
            if session is None:
                owned_session = auth.session("nexprint")
                session = owned_session
            response = auth.request(
                "nexprint",
                "GET",
                NexprintSearcher.DETAIL_URL,
                session=session,
                params={"id": model_id},
                timeout=30,
            )
        try:
            return NexprintSearcher._response_data(
                response, "Nexprint detail API failed"
            )
        finally:
            response.close()
            if owned_session is not None:
                owned_session.close()

    @staticmethod
    def _profile_cover(item):
        cover = str(item.get("settingCoverImgUrl") or "")
        if not _is_http_url(cover):
            first_plate = NexprintSearcher._first_mapping(item.get("plateList"))
            cover = str(first_plate.get("coverImgUrl") or "")
        return cover if _is_http_url(cover) else ""

    @staticmethod
    def _first_mapping(values):
        if not isinstance(values, list):
            return {}
        return next((value for value in values if isinstance(value, dict)), {})

    @staticmethod
    def _profile_filament(item):
        filament = item.get("filamentType")
        if not isinstance(filament, list):
            filament = [filament] if filament else []
        return ", ".join(str(value) for value in filament if value)

    @staticmethod
    def _measurement(value, suffix):
        number = _number(value)
        return f"{number:g}{suffix}" if number is not None else ""

    @staticmethod
    def _profile_record(item):
        profile_id = str(item.get("id") or "")
        file_info = item.get("settingFile")
        if not profile_id or not isinstance(file_info, dict):
            return None
        if not file_info.get("fileId"):
            return None
        params = NexprintSearcher._first_mapping(item.get("settingParamList"))
        plates = item.get("plateList")
        if not isinstance(plates, list):
            plates = []
        statistics = item.get("statistics")
        if not isinstance(statistics, dict):
            statistics = {}
        author = item.get("author")
        if not isinstance(author, dict):
            author = {}
        return {
            "profile_id": profile_id,
            "title": _coalesce(item.get("settingName"), default="Print profile"),
            "cover": NexprintSearcher._profile_cover(item),
            "creator": _coalesce(author.get("nickname"), item.get("authorName")),
            "printer": str(params.get("printerModel") or ""),
            "filament": NexprintSearcher._profile_filament(item),
            "layer_height": NexprintSearcher._measurement(
                params.get("layerHeightMm"), "mm"
            ),
            "walls": _number(params.get("wallLoops"), integer=True),
            "infill": NexprintSearcher._measurement(
                params.get("infillDensity"), "%"
            ),
            "prediction_text": str(params.get("printTimeStr") or ""),
            "plates": len(plates),
            "rating": _number(item.get("score")),
            "rating_count": _number(item.get("scoreCount"), integer=True),
            "downloads": _number(
                statistics.get("printSettingDownloadCount"), integer=True
            ),
            "size": _number(file_info.get("fileSize"), integer=True),
        }

    @staticmethod
    def get_download_choices(model, auth=None):
        detail = NexprintSearcher._load_detail(model, auth)
        profiles = [
            profile
            for item in detail.get("settingInfoList") or []
            if isinstance(item, dict)
            and (profile := NexprintSearcher._profile_record(item)) is not None
        ]
        if not profiles:
            raise RuntimeError("Nexprint returned no downloadable print profiles")
        return {
            "picker_platform": "Nexprint",
            "profiles": profiles,
            "default_profile_id": profiles[0]["profile_id"],
            "formats": [
                {
                    "id": "3mf",
                    "label": "3MF print profile",
                    "description": (
                        "Download and import the selected Nexprint print profile."
                    ),
                    "available": True,
                }
            ],
        }


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
        try:
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
        finally:
            session.close()

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
        try:
            design = MakerWorldSearcher._load_design(design_id, auth, session)
            internal_model_id = design.get("modelId") or design.get("model_id")
            if model.get("_download_format", "3mf") != "3mf":
                raise ValueError("MakerWorld direct import supports the 3MF format only")
            profile_id = str(
                model.get("_profile_id")
                or MakerWorldSearcher._profile_from_url(model.get("url", ""))
            )
            if not profile_id:
                raise ValueError(
                    "Select a MakerWorld print profile before downloading"
                )
            selected = MakerWorldSearcher._selected_profile(
                profile_id, design_id, design, auth, session
            )
            body = MakerWorldSearcher._download_profile(
                profile_id, internal_model_id, auth, session
            )
            url = (
                body.get("url")
                or body.get("downloadUrl")
                or body.get("download_url")
                or ""
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
        finally:
            session.close()


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
            # complete counters. The search coordinator hydrates unknown
            # licenses in a bounded background worker pool.
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


_INDEXED_SOURCE_HOSTS = (
    ("thingiverse.com", "thingiverse"),
    ("printables.com", "printables"),
    ("prusaprinters.org", "printables"),
    ("makerworld.com", "makerworld"),
    ("cults3d.com", "cults3d"),
    ("myminifactory.com", "myminifactory"),
    ("crealitycloud.com", "crealitycloud"),
    ("grabcad.com", "grabcad"),
    ("youmagine.com", "youmagine"),
    ("pinshape.com", "pinshape"),
)


def _indexed_source(url):
    host = _url_host(url)
    for suffix, key in _INDEXED_SOURCE_HOSTS:
        if _host_matches(host, (suffix,)):
            return key
    return ""


def _extract_indexed_source_url(raw, base_url):
    preferred = []
    fallback = []
    for anchor in _parse_catalog_html(raw).anchors:
        url = urllib.parse.urljoin(base_url, anchor.get("href") or "")
        if urllib.parse.urlsplit(url).scheme.lower() != "https" or not _indexed_source(
            url
        ):
            continue
        text = str(anchor.get("text") or anchor.get("title") or "").casefold()
        target = preferred if "view on" in text else fallback
        if url not in target:
            target.append(url)
    return _first(preferred or fallback, "")


def _indexed_model_files(model, auth, index_name):
    page_url = model.get("url") or model.get("download_url") or ""
    raw, fetched = _fetch_html(page_url)
    source_url = _extract_indexed_source_url(raw, fetched)
    source_key = _indexed_source(source_url)
    source_spec = _platform(source_key)
    if source_spec is None:
        raise BrowserRequired(
            f"{index_name} does not host model files. Open the indexed model and download it from its original portal.",
            page_url,
        )
    if source_spec.requires_auth and (
        auth is None or not auth.authenticated(source_spec.key)
    ):
        raise BrowserRequired(
            f"The original {source_spec.display} model requires its account session. Connect that portal or open the original page.",
            source_url,
        )
    source_model = dict(model)
    source_model.update(
        {
            "_platform_key": source_spec.key,
            "platform": source_spec.display,
            "url": source_url,
            "download_url": source_url,
            "requires_auth": source_spec.requires_auth,
        }
    )
    return source_spec.adapter.get_files(source_model, auth)


class YeggiSearcher:
    BASE = "https://www.yeggi.com"

    @staticmethod
    def search(query, context, options=None):
        url = f"{YeggiSearcher.BASE}/q/{urllib.parse.quote(query, safe='')}/"
        return [_browser_search_result(query, "Yeggi", url)]

    @staticmethod
    def get_files(model, auth=None):
        raise BrowserRequired(
            "Yeggi is a meta-search engine and requires an interactive Turnstile check before opening the original model portal.",
            model.get("url", ""),
        )


class StlFinderSearcher:
    BASE = "https://www.stlfinder.com"
    PATH_RE = r"/model/[a-z0-9%._~+()-]+/\d+"

    @staticmethod
    def search(query, context, options=None):
        page = _search_page_number(options)
        url = (
            f"{StlFinderSearcher.BASE}/3dmodels/"
            f"{urllib.parse.quote(query.strip(), safe='')}/"
        )
        if page > 1:
            url += f"?page={page}"
        raw, final_url = _fetch_html(url)
        items = _extract_catalog_models(
            raw, final_url, StlFinderSearcher.PATH_RE, "STLFinder"
        )
        for item in items:
            item["is_free"] = None
        return SearchPage(items, has_more=len(items) >= 30)

    @staticmethod
    def get_files(model, auth=None):
        return _indexed_model_files(model, auth, "STLFinder")


THANGS_BASE = "https://thangs.com"
THANGS_API_BASE = "https://production-api.thangs.com"


def _thangs_api_url(url):
    """Resolve a public Thangs web proxy URL against its official API host."""
    decoded = _decode_embedded_url(url)
    if not _is_http_url(decoded):
        return ""
    parsed = urllib.parse.urlsplit(decoded)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not re.fullmatch(
        r"/(?:api/)?v2/models/\d+/download-url", parsed.path
    ):
        return ""
    api_path = parsed.path[4:] if parsed.path.startswith("/api/") else parsed.path
    if host == "production-api.thangs.com":
        return urllib.parse.urlunsplit(("https", host, api_path, parsed.query, ""))
    is_web_proxy = host in ("thangs.com", "www.thangs.com")
    if not is_web_proxy or not parsed.path.startswith("/api/"):
        return ""
    return urllib.parse.urlunsplit(
        ("https", "production-api.thangs.com", api_path, parsed.query, "")
    )


def _thangs_thumbnail(item):
    thumbnail = _decode_embedded_url(item.get("thumbnailUrl"))
    if _is_http_url(thumbnail):
        return thumbnail
    return next(
        (
            _decode_embedded_url(value)
            for value in item.get("thumbnails") or ()
            if _is_http_url(_decode_embedded_url(value))
        ),
        "",
    )


def _thangs_result(item):
    model_id = item.get("modelId")
    url = _decode_embedded_url(item.get("modelPageUrl"))
    if not _is_http_url(url) or not _host_matches(_url_host(url), ("thangs.com",)):
        url = f"{THANGS_BASE}/m/{model_id}" if model_id else ""
    download_url = _thangs_api_url(item.get("downloadUrl"))
    marketplace = item.get("marketplaceInfo") or {}
    price = _number(marketplace.get("priceInUSD"))
    return {
        "name": _clean_web_text(item.get("name")) or "Untitled",
        "author": _clean_web_text(item.get("ownerUsername")) or "Unknown",
        "platform": "Thangs",
        "thumbnail_url": _thangs_thumbnail(item),
        "license": "Unknown",
        "license_url": "",
        "license_summary": "Open the Thangs model page to review the exact license before downloading.",
        "download_url": download_url,
        "url": url,
        "_model_id": model_id,
        "requires_auth": bool(download_url),
        "direct_import": bool(download_url),
        "downloads": item.get("downloadCount"),
        "likes": item.get("likesCount"),
        "published_at": item.get("publishedOn"),
        "price": price,
        "is_free": price is None or price <= 0,
    }


class ThangsSearcher:
    BASE = THANGS_BASE
    SEARCH_URL = THANGS_API_BASE + "/search/v5/search-by-text"

    @staticmethod
    def _browser_url(query):
        return f"{ThangsSearcher.BASE}/search/{urllib.parse.quote(query, safe='')}?scope=thangs"

    @staticmethod
    def _request(params, query, auth=None):
        import requests

        manager = auth if isinstance(auth, AuthManager) else AuthManager()

        def request(user_agent):
            headers = {
                "User-Agent": user_agent,
                "Accept": "application/json,text/plain,*/*",
                "Origin": ThangsSearcher.BASE,
                "Referer": ThangsSearcher.BASE + "/",
            }
            # A stored clearance pins its own User-Agent; that is deliberate.
            cookies = manager.clearance.apply(ThangsSearcher.SEARCH_URL, headers)
            return requests.get(
                ThangsSearcher.SEARCH_URL,
                params=params,
                headers=headers,
                cookies=cookies,
                timeout=30,
            )

        response = request(_BROWSER_UA)
        if _is_cloudflare_challenge(response):
            response.close()
            response = request(_STANDARD_BROWSER_UA)
            if _is_cloudflare_challenge(response):
                response.close()
                raise _cloudflare_challenge(
                    ThangsSearcher._browser_url(query),
                    manager,
                    host=_url_host(ThangsSearcher.SEARCH_URL),
                    detail=(
                        "The official Thangs API is asking for a Cloudflare "
                        "browser check. Open Thangs, complete the verification "
                        "yourself, then add the cf_clearance cookie and your "
                        "browser User-Agent for "
                        f"{_url_host(ThangsSearcher.SEARCH_URL)}."
                    ),
                )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def search(query, context, options=None):
        page = _search_page_number(options)
        sort = str((options or {}).get("sort") or "relevance")
        api_sort = {
            "downloads": "downloads",
            "likes": "likes",
            "newest": "newest",
            "popularity": "downloads",
        }.get(sort, "relevance")
        payload = ThangsSearcher._request(
            {
                "searchTerm": query,
                "page": page - 1,
                "pageSize": 50,
                "pageScope": "root",
                "sort": api_sort,
            },
            query,
            context,
        )
        results = [
            _thangs_result(item)
            for item in payload.get("items") or []
            if isinstance(item, dict)
        ]
        total_pages = int(_number(payload.get("totalPages"), integer=True) or 0)
        return SearchPage(
            results,
            total=payload.get("totalResults"),
            has_more=page < total_pages,
        )

    @staticmethod
    def get_files(model, auth=None):
        download_url = _thangs_api_url(model.get("download_url"))
        if not _is_http_url(download_url) or not _host_matches(
            _url_host(download_url), ("thangs.com",)
        ):
            raise BrowserRequired(
                "Thangs did not provide a direct download resolver for this model.",
                model.get("url", ""),
            )
        if auth is None or not auth.authenticated("thangs"):
            raise AuthRequired(
                "Connect your Thangs access token before importing this model."
            )
        response = auth.request(
            "thangs",
            "GET",
            download_url,
            timeout=30,
            allow_redirects=False,
            headers={"Accept": "application/json"},
        )
        try:
            if response.status_code in (401, 403):
                raise AuthRequired(
                    "Thangs rejected the access token or this account cannot download the model."
                )
            response.raise_for_status()
            try:
                payload = response.json()
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Thangs returned an invalid download response") from exc
        finally:
            response.close()
        if not isinstance(payload, dict):
            raise TypeError("Thangs returned an invalid download response")
        signed_url = _decode_embedded_url(payload.get("signedUrl"))
        if not _is_http_url(signed_url):
            raise RuntimeError("Thangs did not return a signed download URL")
        name = _thangs_download_name(payload.get("fileName"), signed_url)
        return [{"name": name, "url": signed_url}]


def _thangs_download_name(file_name, signed_url):
    """Keep the API filename while recovering the archive extension from its URL."""
    name = _clean_web_text(file_name)
    parsed = urllib.parse.urlsplit(signed_url)
    query = urllib.parse.parse_qs(parsed.query)
    disposition = _first(query.get("response-content-disposition"), "")
    match = re.search(
        r"filename\*?=(?:UTF-8''|\")?([^\";]+)", str(disposition), re.IGNORECASE
    )
    disposition_name = (
        urllib.parse.unquote(match.group(1).strip().strip('"')) if match else ""
    )
    path_name = urllib.parse.unquote(os.path.basename(parsed.path))
    extension = next(
        (
            os.path.splitext(candidate)[1].lower()
            for candidate in (disposition_name, path_name)
            if os.path.splitext(candidate)[1].lower() in _MODEL_FILE_EXTS
        ),
        ".zip",
    )
    if not name:
        name = disposition_name or path_name or "thangs_model"
    if os.path.splitext(name)[1].lower() not in _MODEL_FILE_EXTS:
        name += extension
    return _safe_filename(name, "thangs_model" + extension)


class CrealityCloudSearcher:
    BASE = "https://www.crealitycloud.com"
    API_BASE = "https://admin.crealitycloud.com"
    LOGIN_URL = (
        "https://id.creality.com/connect?lang=en-US"
        "&client_id=f9c302ecc29c59a0a6e921ff39a073ca"
        "&app_id=cxy-gen2"
        "&redirect_uri=https%3A%2F%2Fwww.crealitycloud.com%2F"
        "&platform=-100"
        "&back_url=https%3A%2F%2Fwww.crealitycloud.com%2Foauth"
    )
    SEARCH_PATH = "/api/cxy/smart_search/v1/model"
    PROFILE_LIST_PATH = "/api/cxy/v3/model/3mfList"
    PROFILE_DETAIL_PATH = "/api/cxy/v3/model/3mfDetail"
    PROFILE_DOWNLOAD_PATH = "/api/cxy/v3/model/3mfDownload"
    PAGE_SIZE = 30
    SORT_TYPES: ClassVar[dict[str, int]] = {
        "relevance": 10,
        "popularity": 10,
        "downloads": 7,
        "likes": 1,
        "newest": 3,
        "makes": 6,
    }

    @staticmethod
    def _auth_values(auth):
        raw = auth.token("crealitycloud") if auth is not None else ""
        token = str(raw or "").strip()
        uid = ""
        match = re.search(r"(?i)(?:^|[;\s])model_token=([^;\s]+)", token)
        if match:
            token = match.group(1).strip()
        uid_match = re.search(
            r"(?i)(?:^|[;\s])model_user_id=([^;\s]+)", str(raw or "")
        )
        if uid_match:
            uid = uid_match.group(1).strip()
        return token, uid

    @staticmethod
    def _headers(auth=None):
        token, uid = CrealityCloudSearcher._auth_values(auth)
        return {
            "User-Agent": _STANDARD_BROWSER_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": CrealityCloudSearcher.BASE,
            "Referer": CrealityCloudSearcher.BASE + "/",
            "__CXY_OS_VER_": "Windows 10",
            "__CXY_PLATFORM_": "2",
            "__CXY_BRAND_": "creality",
            "__CXY_APP_CH_": "nuxt_proxy_pc",
            "__CXY_DUID_": "not set",
            "__CXY_APP_ID_": "cxy-gen2",
            "__CXY_APP_VER_": "7.3.20",
            "__CXY_TIMEZONE_": "0",
            "__CXY_OS_LANG_": "0",
            "__CXY_TOKEN_": token,
            "__CXY_UID_": uid,
            "__CXY_REQUESTID_": str(time.time_ns()),
        }

    @staticmethod
    def _post(path, body, auth=None, *, requires_auth=False):
        import requests

        response = requests.post(
            CrealityCloudSearcher.API_BASE + path,
            json=body,
            headers=CrealityCloudSearcher._headers(auth),
            timeout=30,
            allow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("Creality Cloud returned an invalid API response")
        code = _number(payload.get("code"), integer=True)
        if code not in (None, 0):
            message = str(payload.get("msg") or payload.get("message") or code)
            if requires_auth and code in (4, 401, 403):
                raise AuthRequired(
                    "Creality Cloud session expired. Open the official login and "
                    "paste the model_token cookie value again."
                )
            raise RuntimeError(f"Creality Cloud API: {message}")
        return payload.get("result")

    @staticmethod
    def _cover(item):
        for field in ("pcCovers", "covers", "editCovers", "modelCovers"):
            covers = item.get(field) or []
            if not isinstance(covers, list):
                continue
            for cover in covers:
                if isinstance(cover, dict):
                    url = str(cover.get("url") or cover.get("originUrl") or "")
                else:
                    url = str(cover or "")
                if _is_http_url(url):
                    return url
        return ""

    @staticmethod
    def _model_id(model):
        identifier = str(model.get("_model_id") or "").strip()
        if identifier:
            return identifier
        path = urllib.parse.urlsplit(str(model.get("url") or "")).path.rstrip("/")
        identifier = urllib.parse.unquote(path.rsplit("/", 1)[-1]).strip()
        if not identifier:
            raise ValueError("Creality Cloud model id is missing")
        return identifier

    @staticmethod
    def _model_url(item):
        slug = str(item.get("urlAlias") or item.get("id") or "").strip()
        return CrealityCloudSearcher.BASE + "/model-detail/" + urllib.parse.quote(
            slug, safe="%._~+()-"
        )

    @staticmethod
    def _result(item):
        license_info = _parse_license(str(item.get("license") or ""))
        paid = bool(item.get("isPay"))
        profile_count = int(_number(item.get("model3mfCount"), integer=True) or 0)
        user = item.get("userInfo") if isinstance(item.get("userInfo"), dict) else {}
        return _normalize_result(
            {
                "name": item.get("groupName") or item.get("name") or "Untitled model",
                "url": CrealityCloudSearcher._model_url(item),
                "thumbnail_url": CrealityCloudSearcher._cover(item),
                "author": user.get("nickName") or user.get("name") or "Unknown",
                "platform": "Creality Cloud",
                "license": license_info["name"] or "Unknown",
                "license_url": license_info["url"],
                "license_summary": license_info["summary"],
                "downloads": item.get("downloadCount"),
                "likes": item.get("likeCount"),
                "views": item.get("pv"),
                "makes": item.get("printCountTotal"),
                "published_at": item.get("createTime"),
                "price": _coalesce(item.get("promoPrice"), item.get("totalPrice"), default=None),
                "is_free": not paid,
                "requires_auth": False,
                "direct_import": profile_count > 0,
                "_model_id": str(item.get("id") or ""),
                "_profile_count": profile_count,
            }
        )

    @staticmethod
    def search(query, context, options=None):
        page = _search_page_number(options)
        sort = str((options or {}).get("sort") or "relevance")
        result = CrealityCloudSearcher._post(
            CrealityCloudSearcher.SEARCH_PATH,
            {
                "page": page,
                "pageSize": CrealityCloudSearcher.PAGE_SIZE,
                "keyword": query,
                "sortType": CrealityCloudSearcher.SORT_TYPES.get(sort, 10),
                "printers": [],
                "promoType": 0,
                "isVip": 0,
                "trendType": 1,
                "multiMark": 0,
            },
        )
        result = result if isinstance(result, dict) else {}
        items = [
            CrealityCloudSearcher._result(item)
            for item in result.get("list") or []
            if isinstance(item, dict) and item.get("id")
        ]
        return _search_page_result(
            items, page, CrealityCloudSearcher.PAGE_SIZE, result.get("count")
        )

    @staticmethod
    def _profile_record(item):
        user = item.get("userInfo") if isinstance(item.get("userInfo"), dict) else {}
        cover = str(item.get("thumbnail") or "") or CrealityCloudSearcher._cover(item)
        return {
            "profile_id": str(item.get("id") or ""),
            "title": item.get("secondName") or item.get("name") or "Print profile",
            "summary": _strip_html(item.get("desc")),
            "cover": cover if _is_http_url(cover) else "",
            "creator": user.get("nickName") or "",
            "printer": item.get("printerName") or "",
            "layer_height": item.get("layerHeight") or "",
            "walls": item.get("wallLoops") or "",
            "infill": item.get("sparseInfillDensity") or "",
            "prediction": _number(item.get("printTime"), integer=True),
            "plates": _number(item.get("plateCount"), integer=True),
            "rating": _number(item.get("score")),
            "rating_count": _number(item.get("commnetCount"), integer=True),
            "size": _number(item.get("size"), integer=True),
        }

    @staticmethod
    def get_download_choices(model, auth=None):
        model_id = CrealityCloudSearcher._model_id(model)
        result = CrealityCloudSearcher._post(
            CrealityCloudSearcher.PROFILE_LIST_PATH,
            {
                "page": 1,
                "pageSize": 199,
                "modelGroupId": model_id,
                "printerInternalNameList": [],
                "filterType": 11,
            },
            auth,
        )
        result = result if isinstance(result, dict) else {}
        profiles = [
            CrealityCloudSearcher._profile_record(item)
            for item in result.get("list") or []
            if isinstance(item, dict) and item.get("id")
        ]
        if not profiles:
            profiles = [
                {
                    "profile_id": "model-files",
                    "title": "Original model files",
                    "summary": "No public Creality print profile is available.",
                    "cover": str(model.get("thumbnail_url") or ""),
                }
            ]
        return {
            "picker_platform": "Creality Cloud",
            "profiles": profiles,
            "default_profile_id": profiles[0]["profile_id"],
            "formats": [
                {
                    "id": "3mf",
                    "label": "3MF print profile",
                    "description": "Download and import the selected Creality print profile directly.",
                    "available": profiles[0]["profile_id"] != "model-files",
                },
                {
                    "id": "raw_browser",
                    "label": "STL/CAD files",
                    "description": "Open the official Creality Cloud file list in your browser.",
                    "available": True,
                },
            ],
        }

    @staticmethod
    def get_files(model, auth=None):
        if model.get("_download_format") != "3mf":
            raise BrowserRequired(
                "Creality Cloud STL/CAD files use its official browser download flow.",
                model.get("url", ""),
            )
        profile_id = str(model.get("_profile_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", profile_id):
            raise ValueError("Creality Cloud print profile id is invalid")
        if auth is None or not auth.authenticated("crealitycloud"):
            raise AuthRequired(
                "Sign in to Creality Cloud and connect its model_token before importing a 3MF profile."
            )
        detail = CrealityCloudSearcher._post(
            CrealityCloudSearcher.PROFILE_DETAIL_PATH,
            {"id": profile_id},
            auth,
        )
        result = CrealityCloudSearcher._post(
            CrealityCloudSearcher.PROFILE_DOWNLOAD_PATH,
            {"id": profile_id},
            auth,
            requires_auth=True,
        )
        if isinstance(result, str):
            url = result
        elif isinstance(result, dict):
            url = str(
                _coalesce(
                    result.get("url"),
                    result.get("downloadUrl"),
                    result.get("route"),
                    result.get("fileUrl"),
                    default="",
                )
            )
        else:
            url = ""
        if not _is_http_url(url):
            raise RuntimeError("Creality Cloud did not return a signed 3MF URL")
        detail = detail if isinstance(detail, dict) else {}
        name = _safe_filename(
            detail.get("name") or model.get("name") or "creality-profile.3mf"
        )
        if not name.lower().endswith(".3mf"):
            name += ".3mf"
        return [
            {
                "url": url,
                "name": name,
                "preview_url": detail.get("thumbnail") or model.get("thumbnail_url") or "",
                "size": detail.get("size"),
            }
        ]


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


class SmithsonianSearcher:
    API = "https://3d-api.si.edu/api/v1.0/content/file/search"

    @staticmethod
    def search(query, _context, options=None):
        import requests

        page = _search_page_number(options)
        response = requests.get(
            SmithsonianSearcher.API,
            params={
                "q": query,
                "model_type": "stl",
                "start": (page - 1) * 30,
                "rows": 30,
            },
            headers={"User-Agent": _BROWSER_UA},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        results = []
        for item in payload.get("rows") or []:
            content = item.get("content") or {}
            direct = content.get("uri") or ""
            identifier = str(content.get("model_url") or "").split(":", 1)[-1]
            results.append(
                {
                    "name": item.get("title") or "Untitled",
                    "author": "Smithsonian Institution",
                    "platform": "Smithsonian 3D",
                    "thumbnail_url": "",
                    "license": "Smithsonian Open Access",
                    "license_url": "https://www.si.edu/openaccess",
                    "license_summary": "Released through the Smithsonian Open Access initiative.",
                    "download_url": direct,
                    "url": (
                        f"https://3d.si.edu/object/3d/{identifier}"
                        if identifier
                        else direct
                    ),
                    "_direct_file": direct,
                    "requires_auth": False,
                    "direct_import": bool(direct),
                    "is_free": True,
                }
            )
        total = _number(payload.get("rowCount"), integer=True)
        return SearchPage(
            results,
            total=total,
            has_more=bool(total is not None and page * 30 < total),
        )

    @staticmethod
    def get_files(model, auth=None):
        url = model.get("_direct_file") or model.get("download_url") or ""
        if not url:
            return []
        name = os.path.basename(urllib.parse.urlsplit(url).path) or "smithsonian.zip"
        return [{"name": urllib.parse.unquote(name), "url": url}]


class NasaSearcher:
    TREE_API = (
        "https://api.github.com/repos/nasa/NASA-3D-Resources/git/trees/master"
        "?recursive=1"
    )
    REPO = "https://github.com/nasa/NASA-3D-Resources"
    RAW = "https://raw.githubusercontent.com/nasa/NASA-3D-Resources/master/"

    @staticmethod
    def search(query, _context, options=None):
        import requests

        response = requests.get(
            NasaSearcher.TREE_API,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": _BROWSER_UA,
            },
            timeout=30,
        )
        response.raise_for_status()
        terms = [term.casefold() for term in re.findall(r"[\w-]+", query)]
        results = []
        for item in response.json().get("tree") or []:
            path = str(item.get("path") or "")
            lower = path.casefold()
            if item.get("type") != "blob" or not lower.endswith((".stl", ".3mf")):
                continue
            if terms and not all(term in lower for term in terms):
                continue
            quoted = urllib.parse.quote(path, safe="/")
            direct = NasaSearcher.RAW + quoted
            results.append(
                {
                    "name": os.path.splitext(os.path.basename(path))[0],
                    "author": "NASA",
                    "platform": "NASA 3D Resources",
                    "thumbnail_url": "",
                    "license": "NASA Media Usage Guidelines",
                    "license_url": "https://www.nasa.gov/nasa-brand-center/images-and-media/",
                    "license_summary": "NASA resource; review NASA's media usage guidelines.",
                    "download_url": direct,
                    "url": f"{NasaSearcher.REPO}/blob/master/{quoted}",
                    "_direct_file": direct,
                    "requires_auth": False,
                    "direct_import": True,
                    "is_free": True,
                }
            )
            if len(results) >= 30:
                break
        return results

    @staticmethod
    def get_files(model, auth=None):
        url = model.get("_direct_file") or model.get("download_url") or ""
        name = os.path.basename(urllib.parse.urlsplit(url).path) or "nasa_model.stl"
        return [{"name": urllib.parse.unquote(name), "url": url}] if url else []


class Nih3DSearcher:
    BASE = "https://3d.nih.gov"
    _server_action: ClassVar[str] = ""

    @classmethod
    def _discover_search_action(cls, session):
        if cls._server_action:
            return cls._server_action
        page = session.get(cls.BASE + "/discover", timeout=30)
        page.raise_for_status()
        paths = re.findall(r'<script[^>]+src="([^"]+)"', page.text, re.IGNORECASE)
        path = next((value for value in paths if "/discover/page-" in value), "")
        if not path:
            raise RuntimeError("NIH 3D discover application script was not found")
        script = session.get(urllib.parse.urljoin(cls.BASE, path), timeout=30)
        script.raise_for_status()
        match = re.search(
            r'createServerReference\)\("([0-9a-f]{40,64})".{0,250}"discoverSearch"',
            script.text,
        )
        if not match:
            raise RuntimeError("NIH 3D search action was not found")
        cls._server_action = match.group(1)
        return cls._server_action

    @staticmethod
    def _flight_payload(response):
        marker = response.text.find('1:{"status"')
        if marker < 0:
            return None
        payload, _end = json.JSONDecoder().raw_decode(response.text[marker + 2 :])
        return payload

    @staticmethod
    def search(query, _context, options=None):
        import requests

        terms = " ".join(re.findall(r"[\w-]+", query, re.UNICODE))[:160]
        if not terms:
            return []
        page = _search_page_number(options)
        start = (page - 1) * 30
        expression = (
            'type:entry AND submissionstatus:"Published" AND '
            f"{terms}?start={start}&size=30&sort=publisheddate desc"
        )
        session = requests.Session()
        session.headers.update({"User-Agent": _BROWSER_UA})
        try:
            payload = Nih3DSearcher._discover(session, expression)
        finally:
            session.close()
        if not payload:
            raise RuntimeError("NIH 3D search response format changed")
        return Nih3DSearcher._rows(payload, page)

    @staticmethod
    def _discover(session, expression):
        payload = None
        for _attempt in range(2):
            action = Nih3DSearcher._discover_search_action(session)
            response = session.post(
                Nih3DSearcher.BASE + "/discover",
                data=json.dumps([expression]),
                headers={
                    "Accept": "text/x-component",
                    "Content-Type": "text/plain;charset=UTF-8",
                    "Next-Action": action,
                },
                timeout=30,
            )
            if response.status_code < 400:
                payload = Nih3DSearcher._flight_payload(response)
            if payload:
                break
            Nih3DSearcher._server_action = ""
        return payload

    @staticmethod
    def _rows(payload, page):
        results = []
        hits = payload.get("hits") or {}
        for hit in hits.get("hit") or []:
            fields = hit.get("fields") or {}
            identifier = str(_first(fields.get("paddedentryid") or fields.get("id")))
            lic_name = str(_first(fields.get("license"), "Unknown"))
            lic = _parse_license(lic_name.replace("CC-", "CC "))
            page_url = f"{Nih3DSearcher.BASE}/entries/3DPX-{identifier}"
            thumbnail = str(_first(fields.get("thumbnail")))
            results.append(
                {
                    "name": str(_first(fields.get("title"), "Untitled")),
                    "author": str(_first(fields.get("createdby"), "Unknown")),
                    "platform": "NIH 3D",
                    "thumbnail_url": urllib.parse.urljoin(
                        Nih3DSearcher.BASE, thumbnail
                    ),
                    "license": lic["name"],
                    "license_url": lic["url"],
                    "license_summary": lic["summary"],
                    "download_url": page_url,
                    "url": page_url,
                    "requires_auth": False,
                    "direct_import": True,
                    "downloads": _first(fields.get("downloadcount"), None),
                    "views": _first(fields.get("viewcount"), None),
                    "published_at": _first(fields.get("publisheddate"), None),
                    "is_free": True,
                }
            )
        total = _number(hits.get("found"), integer=True)
        return SearchPage(
            results,
            total=total,
            has_more=bool(total is not None and page * 30 < total),
        )

    @staticmethod
    def get_files(model, auth=None):
        return _public_page_files(
            model,
            no_direct_message=(
                "NIH 3D uses an interactive file-selection flow for this entry. "
                "Open it in the browser."
            ),
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
            price_label = item.get("name", "").casefold()
            if price_label in ("free", "premium"):
                item["name"] = _slug_title(item.get("url", ""))
            item["direct_import"] = price_label == "free"
            item["is_free"] = (
                True if price_label == "free" else False if price_label == "premium" else None
            )
        return items

    @staticmethod
    def get_files(model, auth=None):
        return _public_page_files(
            model,
            no_direct_message=(
                "Pinshape did not expose a public STL file for this model. "
                "Open its official download page."
            ),
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
        profile_picker=True,
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
        profile_picker=True,
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
        session_recheck=True,
    ),
    PlatformSpec("yeggi", "Yeggi", YeggiSearcher),
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
    PlatformSpec(
        "thangs",
        "Thangs",
        ThangsSearcher,
        auth_hosts=("production-api.thangs.com",),
        auth_mode="bearer",
        login_url="https://thangs.com/?showSignin=true",
        referer="https://thangs.com/",
        search_page_size=50,
    ),
    PlatformSpec(
        "stlfinder",
        "STLFinder",
        StlFinderSearcher,
        search_page_size=30,
    ),
    PlatformSpec(
        "crealitycloud",
        "Creality Cloud",
        CrealityCloudSearcher,
        auth_hosts=("admin.crealitycloud.com",),
        auth_mode="creality",
        login_url=CrealityCloudSearcher.LOGIN_URL,
        referer="https://www.crealitycloud.com/",
        search_page_size=30,
        profile_picker=True,
    ),
    PlatformSpec(
        "grabcad",
        "GrabCAD",
        GrabcadSearcher,
        auth_hosts=("grabcad.com",),
        auth_mode="cookie_header",
        login_url="https://login.grabcad.com/login",
        referer="https://grabcad.com/library",
        cookie_domain=".grabcad.com",
        session_recheck=True,
    ),
    PlatformSpec(
        "smithsonian",
        "Smithsonian 3D",
        SmithsonianSearcher,
        search_page_size=30,
    ),
    PlatformSpec("nasa", "NASA 3D Resources", NasaSearcher),
    PlatformSpec("nih3d", "NIH 3D", Nih3DSearcher, search_page_size=30),
    PlatformSpec("youmagine", "YouMagine", YouMagineSearcher),
    PlatformSpec("pinshape", "Pinshape", PinshapeSearcher),
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
#results{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}.card{border:1px solid var(--orca-border,#444);border-radius:8px;padding:10px;cursor:pointer}.card:hover{border-color:var(--orca-accent,#4a9eff)}.card img{width:100%;height:110px;object-fit:cover;border-radius:4px;background:#333}.result-image{opacity:0;transition:opacity .18s ease;cursor:zoom-in}.result-image.loaded{opacity:1}.result-image:focus{outline:2px solid var(--orca-accent,#4a9eff);outline-offset:2px}.card h3{font-size:.9em;margin:6px 0 2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.author{font-size:.78em;color:var(--orca-muted,#888)}.metrics{font-size:.72em;color:var(--orca-muted,#999);min-height:1.2em;margin-top:3px}.license-badge{display:inline-block;padding:1px 7px;border-radius:3px;font-size:.72em;margin-top:4px;background:#444}.license-cc{background:#1a5c2a;color:#8f8}.license-arr{background:#5c3a1a;color:#fc6}
.pagination{display:none;align-items:center;justify-content:center;gap:8px;flex-wrap:wrap;margin:14px 0 4px}.pagination.active{display:flex}.pagination-summary{font-size:.8em;color:var(--orca-muted,#999);margin-right:4px}.pagination label{display:flex;align-items:center;gap:5px;font-size:.8em;color:var(--orca-muted,#999)}.pagination select{padding:5px 7px;border:1px solid var(--orca-border,#555);border-radius:5px;background:var(--orca-bg,#222);color:inherit}.page-numbers{display:flex;align-items:center;gap:4px}.page-button{min-width:34px;padding:6px 8px}.page-button.current{background:var(--orca-accent,#4a9eff)!important;color:var(--orca-accent-fg,#fff)!important;border-color:var(--orca-accent,#4a9eff)!important}.page-ellipsis{padding:0 2px;color:var(--orca-muted,#999)}
.source-results{display:none;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:6px;margin:0 0 12px}.source-results.active{display:grid}.source-result{padding:7px 9px;border:1px solid var(--orca-border,#444);border-radius:6px;font-size:.75em}.source-result strong,.source-result span,.source-result small{display:block}.source-result span{color:var(--orca-muted,#999);margin-top:2px}.source-result small{color:#e78b8b;margin-top:3px;overflow-wrap:anywhere}.source-browser{margin-top:7px;padding:4px 8px;font-size:1em}.load-more-row{display:none;justify-content:center;margin:10px 0 4px}.load-more-row.active{display:flex}.load-more-row button{min-width:220px}.cf-list{margin:8px 0 2px;font-size:.76em}.cf-head{color:var(--orca-muted,#999);margin-bottom:4px}.cf-row{display:flex;gap:8px;align-items:baseline;padding:3px 0;border-top:1px solid var(--orca-border,#3a3a3a)}.cf-row strong{flex:0 0 auto}.cf-row span{color:var(--orca-muted,#999);overflow-wrap:anywhere}.source-verify{margin-top:7px;margin-left:6px;padding:4px 8px;font-size:1em}
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
  <div class="account"><div class="account-head"><strong>Creality Cloud</strong><button type="button" class="auth-help" data-platform="crealitycloud" aria-label="Creality Cloud authorization instructions" aria-describedby="auth-tooltip" onmouseenter="showAuthHelp(this)" onmouseleave="hideAuthHelp(this)" onfocus="showAuthHelp(this)" onblur="hideAuthHelp(this)">?</button></div><span id="auth-crealitycloud" class="auth-state">Checking...</span><button class="secondary" onclick="openAuth('crealitycloud')">Account</button></div>
  <div class="account"><div class="account-head"><strong>Thangs</strong><button type="button" class="auth-help" data-platform="thangs" aria-label="Thangs authorization instructions" aria-describedby="auth-tooltip" onmouseenter="showAuthHelp(this)" onmouseleave="hideAuthHelp(this)" onfocus="showAuthHelp(this)" onblur="hideAuthHelp(this)">?</button></div><span id="auth-thangs" class="auth-state">Checking...</span><button class="secondary" onclick="openAuth('thangs')">Access token</button></div>
</div>
<div id="auth-tooltip" class="auth-tooltip" role="tooltip"></div>
<div class="search-row"><input id="query" placeholder="Search for 3D models..."><button id="search-btn" onclick="doSearch()">Search</button></div>
<div class="search-options"><label>Sort <select id="sort"><option value="relevance">Relevance</option><option value="popularity">Popularity (normalized)</option><option value="downloads">Downloads</option><option value="likes">Likes</option><option value="rating">Rating</option><option value="newest">Newest</option><option value="makes">Most printed</option><option value="name">Name</option><option value="platform">Platform</option></select></label><label><input id="free-only" type="checkbox"> Free only</label><label><input id="direct-only" type="checkbox"> Direct import only</label></div>
<div class="source-head"><strong>Search portals</strong><div class="source-tools"><button class="secondary" onclick="setAllPortals(true)">Select all</button><button class="secondary" onclick="setAllPortals(false)">Select none</button><span id="source-count" class="source-count"></span></div></div>
<div class="platforms" id="search-portals">
<label class="portal-option"><input id="portal-thingiverse" class="portal-search" type="checkbox" data-platform="thingiverse"> Thingiverse</label>
<label class="portal-option"><input id="portal-cults3d" class="portal-search" type="checkbox" checked data-platform="cults3d"> Cults3D</label>
<label class="portal-option"><input id="portal-yeggi" class="portal-search" type="checkbox" data-platform="yeggi"> Yeggi (browser)</label>
<label class="portal-option"><input id="portal-myminifactory" class="portal-search" type="checkbox" data-platform="myminifactory"> MyMiniFactory</label>
<label class="portal-option"><input id="portal-thangs" class="portal-search" type="checkbox" data-platform="thangs"> Thangs</label>
<label class="portal-option"><input id="portal-stlfinder" class="portal-search" type="checkbox" data-platform="stlfinder"> STLFinder</label>
<label class="portal-option"><input id="portal-makeronline" class="portal-search" type="checkbox" checked data-platform="makeronline"> Makeronline</label>
<label class="portal-option"><input id="portal-crealitycloud" class="portal-search" type="checkbox" checked data-platform="crealitycloud"> Creality Cloud</label>
<label class="portal-option"><input id="portal-nexprint" class="portal-search" type="checkbox" checked data-platform="nexprint"> Nexprint</label>
<label class="portal-option"><input id="portal-grabcad" class="portal-search" type="checkbox" data-platform="grabcad"> GrabCAD</label>
<label class="portal-option"><input id="portal-printables" class="portal-search" type="checkbox" checked data-platform="printables"> Printables</label>
<label class="portal-option"><input id="portal-makerworld" class="portal-search" type="checkbox" checked data-platform="makerworld"> MakerWorld</label>
<label class="portal-option"><input id="portal-smithsonian" class="portal-search" type="checkbox" checked data-platform="smithsonian"> Smithsonian 3D</label>
<label class="portal-option"><input id="portal-nasa" class="portal-search" type="checkbox" checked data-platform="nasa"> NASA 3D Resources</label>
<label class="portal-option"><input id="portal-nih3d" class="portal-search" type="checkbox" checked data-platform="nih3d"> NIH 3D</label>
<label class="portal-option"><input id="portal-youmagine" class="portal-search" type="checkbox" checked data-platform="youmagine"> YouMagine</label>
<label class="portal-option"><input id="portal-pinshape" class="portal-search" type="checkbox" checked data-platform="pinshape"> Pinshape</label>
</div>
<div id="source-results" class="source-results" aria-live="polite"></div>
<div id="results"></div>
<nav id="pagination" class="pagination" aria-label="Search result pages"><span id="pagination-summary" class="pagination-summary"></span><label>Per page <select id="page-size" onchange="changePageSize()"><option value="100" selected>100</option><option value="150">150</option><option value="200">200</option><option value="250">250</option><option value="300">300</option></select></label><button id="page-prev" class="secondary page-button" type="button" onclick="setResultPage(currentPage-1)" aria-label="Previous page">&larr;</button><span id="page-numbers" class="page-numbers"></span><button id="page-next" class="secondary page-button" type="button" onclick="setResultPage(currentPage+1)" aria-label="Next page">&rarr;</button></nav>
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
  <div class="button-row"><button id="auth-submit" onclick="submitAuth()">Connect</button><button id="auth-seamless" onclick="startSeamlessLogin()">Sign in in browser</button><button id="official-login" class="secondary" onclick="openOfficialLogin()">Open official login</button><button id="import-anycubic" class="secondary" style="display:none" onclick="importAnycubic()">Import from Anycubic Slicer Next</button><button id="auth-logout" class="danger" onclick="logoutAuth()">Forget session</button><button class="secondary" onclick="closeAuth()">Cancel</button></div>
</div>
<div id="cloudflare-modal" class="auth-modal">
  <h2 style="margin:0 0 5px;font-size:1.05em">Cloudflare verification</h2>
  <div class="auth-note">Some catalogs put a Cloudflare check in front of their pages. This plugin does not answer that check for you. Open the site, pass the verification yourself, then copy the resulting <code>cf_clearance</code> cookie and the exact User-Agent of that browser here, so the plugin's requests are recognised as the same verified browser. Cloudflare ties a clearance to your IP address and to that User-Agent, and retires it after a while &mdash; when it stops working the plugin discards it and asks again.</div>
  <div class="field"><label>Protected host</label><input id="cf-host" autocomplete="off" placeholder="example.com"></div>
  <div class="field"><label>cf_clearance cookie value (or a Cookie header containing it)</label><input id="cf-cookie" type="password" autocomplete="off"></div>
  <div class="field"><label>User-Agent of the browser that passed the check</label><input id="cf-agent" autocomplete="off" placeholder="Copy from that browser &mdash; not from this window"></div>
  <div id="cf-list" class="cf-list"></div>
  <div class="button-row"><button id="cf-submit" onclick="submitCloudflare()">Save verification</button><button id="cf-open" class="secondary" onclick="openCloudflareSite()">Open the page</button><button id="cf-forget" class="danger" onclick="forgetCloudflare()">Forget host</button><button class="secondary" onclick="closeCloudflare()">Cancel</button></div>
</div>
<div id="file-modal" class="auth-modal file-modal">
  <h2 style="margin:0 0 5px;font-size:1.05em">Choose files to import</h2>
  <div class="auth-note">This model contains multiple downloadable files. Select the files that should be downloaded and added to the current OrcaSlicer project.</div>
  <div class="file-tools"><button class="secondary" onclick="setAllFiles(true)">Select all</button><button class="secondary" onclick="setAllFiles(false)">Select none</button><span id="file-count" class="file-count"></span></div>
  <div id="file-list" class="file-list"></div>
  <div class="button-row"><button id="file-import" onclick="confirmFileImport()">Import selected</button><button class="secondary" onclick="closeFilePicker()">Cancel</button></div>
</div>
<div id="makerworld-modal" class="auth-modal makerworld-modal">
  <h2 id="profile-picker-title" style="margin:0 0 5px;font-size:1.05em">Choose profile download</h2>
  <div id="profile-picker-note" class="auth-note">Select a print profile and file format.</div>
  <div id="mw-profiles" class="mw-profiles"></div>
  <h3 style="margin:10px 0 4px;font-size:.9em">File format</h3>
  <div id="mw-formats" class="mw-formats"></div>
  <div class="button-row"><button id="mw-import" onclick="confirmMakerWorldChoice()">Download 3MF</button><button class="secondary" onclick="closeMakerWorldPicker()">Cancel</button></div>
</div>
<img id="image-preview" class="image-preview" alt="Enlarged model, file, or print profile preview">
<div id="status">Ready.</div>
<script>
var selectedModel=null, searching=false, canLoadMore=false, authPlatform=null, authStates={}, pendingImport=null, pendingFiles=[], pendingMakerWorldModel=null, activeAuthHelp=null, resultImageObserver=null, makerWorldChoicesCache={}, makerWorldPrefetching={}, makerWorldPreloadedImages={}, currentPage=1, pageSize=100;
var $=function(id){return document.getElementById(id)};
var AUTH_HELP={makerworld:'Click Account, then sign in with your Bambu email and password, including the MFA code when requested. You can alternatively paste an existing Bambu Cloud access token. Passwords are never saved.',nexprint:'Click Account, open the official Nexprint login, and sign in. Copy the auth_token cookie value from that signed-in browser session, paste it into the plugin, and connect.',makeronline:'Open the official Anycubic OAuth login in your browser. After MakerOnline returns, copy the mo_access_token cookie value (or its Cookie header), paste it below, and connect. You can alternatively import the Anycubic Slicer Next session. The plugin never reads the browser profile or password.',cults3d:'Click Account, open the official Cults3D login, and sign in. From the signed-in browser request headers, copy the Cookie header or session-cookie string, paste it into the plugin, and connect.',grabcad:'A GrabCAD Community Library membership is required. Click Account, sign in on the official site, copy the Cookie request header or session-cookie string from the signed-in browser, and paste it into the plugin.',thingiverse:'Create or open a Thingiverse developer app, obtain your personal API access token, then click API token, paste it, and connect.',myminifactory:'Create a MyMiniFactory API client and obtain its API key. Click API key, paste the key, and connect. Storefront and OAuth-only downloads will still open in the browser.',crealitycloud:'Open the official Creality Cloud login and sign in. In the signed-in browser session, copy the model_token cookie value (or a Cookie header containing model_token and model_user_id), paste it below, and connect. The plugin never reads your browser profile or password.',thangs:'Open the official Thangs sign-in page and log in. In the signed-in browser developer tools, copy the token from an Authorization: Bearer request header, paste it below, and connect. The token is sent only to Thangs and never to the signed storage URL.'};
function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;")}
function safeUrl(s){try{var u=new URL(String(s||''));return(u.protocol==='http:'||u.protocol==='https:')?u.href:''}catch(e){return''}}
function showAuthHelp(button){var tooltip=$('auth-tooltip'),text=AUTH_HELP[button.dataset.platform]||'';if(!text)return;activeAuthHelp=button;tooltip.textContent=text;tooltip.classList.add('active');tooltip.style.left='0px';tooltip.style.top='0px';var r=button.getBoundingClientRect(),gap=8,margin=8,left=Math.max(margin,Math.min(window.innerWidth-tooltip.offsetWidth-margin,r.left+r.width/2-tooltip.offsetWidth/2)),top=r.bottom+gap;if(top+tooltip.offsetHeight>window.innerHeight-margin)top=Math.max(margin,r.top-tooltip.offsetHeight-gap);tooltip.style.left=Math.round(left)+'px';tooltip.style.top=Math.round(top)+'px'}
function hideAuthHelp(button){if(activeAuthHelp!==button||button.matches(':hover')||document.activeElement===button)return;$('auth-tooltip').classList.remove('active');activeAuthHelp=null}
function platformKey(display){return __PLATFORM_KEYS__[display]||String(display||'').toLowerCase()}
function isAuthed(model){if(!model||!model.requires_auth)return true;var s=authStates[platformKey(model.platform)];return !!(s&&s.authenticated)}
function updateAuth(states){authStates=states||{};['makerworld','nexprint','makeronline','cults3d','grabcad','thingiverse','myminifactory','crealitycloud','thangs'].forEach(function(p){var s=authStates[p]||{};$("auth-"+p).textContent=s.authenticated?("Connected: "+(s.label||'session')):'Not connected'});if(selectedModel)showDetail(selectedModel,false)}
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
function renderResultPage(){hideImagePreview(null,true);var total=window._results.length,pages=Math.max(1,Math.ceil(total/pageSize));currentPage=Math.max(1,Math.min(currentPage,pages));var start=(currentPage-1)*pageSize,end=Math.min(start+pageSize,total),html='';window._results.slice(start,end).forEach(function(m,i){var image=safeUrl(m.thumbnail_url),index=start+i;html+='<div class="card" data-idx="'+index+'"><img class="result-image" loading="lazy" decoding="async" data-src="'+esc(image)+'" alt="'+esc(m.name||'Model preview')+'" tabindex="0" onmouseenter="showImagePreview(this)" onmouseleave="hideImagePreview(this)" onfocus="showImagePreview(this)" onblur="hideImagePreview(this)"><h3 title="'+esc(m.name)+'">'+esc(m.name)+'</h3><div class="author">'+esc(m.author)+' · '+esc(m.platform)+'</div><div class="metrics">'+esc(metricText(m))+'</div><span class="license-badge '+licenseClass(m.license)+'">'+esc(m.license||'Unknown')+'</span></div>'});$('results').innerHTML=html;renderPagination(total,start,end);observeResultImages();$('status').textContent=total+' result(s)'}
function renderResults(models,resetPage){window._results=models||[];if(resetPage!==false)currentPage=1;renderResultPage()}
function setResultPage(page){var pages=Math.max(1,Math.ceil(window._results.length/pageSize)),next=Math.max(1,Math.min(Number(page)||1,pages));if(next===currentPage)return;currentPage=next;closeDetail();renderResultPage();$('results').scrollIntoView({behavior:'smooth',block:'start'})}
function changePageSize(){var value=parseInt($('page-size').value,10);pageSize=[100,150,200,250,300].indexOf(value)>=0?value:100;currentPage=1;closeDetail();renderResultPage()}
function renderSourceResults(sources,more){var html='',moreCount=0;(sources||[]).forEach(function(s){var loaded=Number(s.loaded)||0,visible=Number(s.visible)||0,parts=[],browserUrl=safeUrl(s.browser_url);if(s.total!=null)parts.push(loaded+' of '+Number(s.total)+' loaded');else parts.push(loaded+' loaded');if(visible!==loaded)parts.push(visible+' shown by filters');if(s.has_more){parts.push('more available');moreCount++}else if(!s.error){parts.push(s.paginated?'complete':'first page only')}html+='<div class="source-result'+(s.error?' error':'')+'"><strong>'+esc(s.display||s.key)+'</strong><span>'+esc(parts.join(' · '))+'</span>'+(s.error?'<small>'+esc(s.error)+'</small>':'')+(browserUrl?'<button type="button" class="secondary source-browser" data-url="'+esc(browserUrl)+'">Open in browser</button>':'')+(s.cloudflare_host?'<button type="button" class="secondary source-verify" data-host="'+esc(s.cloudflare_host)+'" data-url="'+esc(browserUrl)+'">Add Cloudflare verification</button>':'')+'</div>'});$('source-results').innerHTML=html;$('source-results').classList.toggle('active',!!html);canLoadMore=!!more&&moreCount>0;$('load-more-row').classList.toggle('active',canLoadMore);$('load-more').disabled=false;$('load-more').textContent=moreCount===1?'Load next page from 1 portal':'Load next pages from '+moreCount+' portals'}
$('source-results').addEventListener('click',function(e){if(!e.target.closest)return;var verify=e.target.closest('.source-verify');if(verify){openCloudflare(verify.dataset.host,verify.dataset.url);return}var button=e.target.closest('.source-browser');if(button)openExternal(button.dataset.url)});
function loadMoreResults(){if(searching||!canLoadMore)return;searching=true;$('search-btn').disabled=true;$('load-more').disabled=true;$('load-more').textContent='Loading...';$('status').textContent='Loading next portal pages...';orca.postMessage({action:'search_more'})}
function modelIdentity(m){return String((m&&m._platform_key)||platformKey(m&&m.platform)||'')+'|'+String((m&&m._thing_id)||(m&&m._model_id)||(m&&m.url)||'')}
function preloadMakerWorldImages(key,profiles){var images=[];(profiles||[]).forEach(function(p){var url=safeUrl(p.cover);if(!url)return;var img=new Image();img.decoding='async';img.src=url;images.push(img)});makerWorldPreloadedImages[key]=images}
function cacheMakerWorldChoices(msg){var model=msg.model||selectedModel;if(!model)return;var key=modelIdentity(model);makerWorldChoicesCache[key]={profiles:msg.profiles||[],formats:msg.formats||[],default_profile_id:msg.default_profile_id||'',picker_platform:msg.picker_platform||model.platform||'MakerWorld'};makerWorldPrefetching[key]=false;if(!makerWorldPreloadedImages[key])preloadMakerWorldImages(key,msg.profiles||[])}
function prefetchMakerWorld(m){var platform=platformKey(m&&m.platform);if(platform!=='makerworld'&&platform!=='crealitycloud'&&platform!=='nexprint')return;var key=modelIdentity(m);if(makerWorldChoicesCache[key]||makerWorldPrefetching[key])return;makerWorldPrefetching[key]=true;orca.postMessage({action:'prefetch_profile_choices',model:m})}
function openModelDetail(m){var load=m._details_available&&!m._details_loaded&&!m._details_loading;if(load)m._details_loading=true;showDetail(m,true);prefetchMakerWorld(m);if(load)orca.postMessage({action:'model_details',model:m})}
$('results').addEventListener('click',function(e){var c=e.target.closest&&e.target.closest('.card');if(!c)return;var m=window._results[parseInt(c.dataset.idx,10)];if(m)openModelDetail(m)});
function showDetail(m,open){selectedModel=m;var loading=!!m._details_loading;$('det-name').textContent=m.name;$('det-author').innerHTML='<strong>Author:</strong> '+esc(m.author);$('det-platform').innerHTML='<strong>Platform:</strong> '+esc(m.platform);$('det-metrics').innerHTML=metricText(m)?'<strong>Metrics:</strong> '+esc(metricText(m)):'';$('det-license').innerHTML='<strong>License:</strong> <span class="license-badge '+licenseClass(m.license)+'">'+esc(loading?'Loading...':(m.license||'Unknown'))+'</span>';$('det-summary').textContent=loading?'Loading the official license and complete metrics...':(m.license_summary||'No license information available.');var modelUrl=safeUrl(m.url);$('det-url').innerHTML=modelUrl?'<strong>Model page:</strong> <a href="'+esc(modelUrl)+'">'+esc(modelUrl)+'</a>':'';var b=$('det-import-btn');b.disabled=m.result_type==='search_link';b.textContent=m.result_type==='search_link'?'Browser search only':(m.requires_auth&&!isAuthed(m))?('Log in to '+m.platform+' & import'):'Import into OrcaSlicer';if(open!==false)$('detail').classList.add('active')}
function applyModelDetails(m,silent){m._details_loading=false;var idx=-1;for(var i=0;i<(window._results||[]).length;i++){if(modelIdentity(window._results[i])===modelIdentity(m)){idx=i;break}}if(idx>=0){window._results[idx]=m;renderResults(window._results,false)}if(selectedModel&&modelIdentity(selectedModel)===modelIdentity(m))showDetail(m,false);if(m._details_error&&!silent)$('status').textContent='Could not load model details: '+m._details_error}
function closeDetail(){$('detail').classList.remove('active')}
document.addEventListener('pointerdown',function(e){var d=$('detail');if(!d||!d.classList.contains('active'))return;if(d.contains(e.target))return;closeDetail()},true);
$('detail').addEventListener('click',function(e){var a=e.target.closest&&e.target.closest('a[href]');if(!a)return;e.preventDefault();openExternal(a.getAttribute('href'))});
function licenseClass(l){if(/CC|Creative Commons|CC0|Public Domain/i.test(l||''))return'license-cc';if(/All Rights Reserved|Standard Digital|Exclusive/i.test(l||''))return'license-arr';return''}
function openExternal(url){orca.postMessage({action:'open_external',url:url})}
var cloudflareUrl='';
function renderCloudflare(list){var rows='';(list||[]).forEach(function(c){rows+='<div class="cf-row"><strong>'+esc(c.host)+'</strong><span>'+esc(String(c.user_agent||'').slice(0,90))+'</span></div>'});$('cf-list').innerHTML=rows?('<div class="cf-head">Stored verifications</div>'+rows):'<div class="cf-head">No verification stored yet.</div>'}
function openCloudflare(host,url){cloudflareUrl=safeUrl(url)||'';$('cf-host').value=host||'';$('cf-cookie').value='';$('cf-agent').value='';$('cf-submit').disabled=false;$('cloudflare-modal').classList.add('active');$('modal-bg').classList.add('active');$('cf-open').style.display=cloudflareUrl?'':'none'}
function closeCloudflare(){$('cloudflare-modal').classList.remove('active');$('cf-cookie').value='';syncBackdrop()}
function openCloudflareSite(){if(cloudflareUrl)openExternal(cloudflareUrl)}
function submitCloudflare(){var host=$('cf-host').value.trim(),cookie=$('cf-cookie').value.trim(),agent=$('cf-agent').value.trim();if(!host){$('status').textContent='Enter the Cloudflare-protected host.';return}if(!cookie){$('status').textContent='Paste the cf_clearance cookie from the tab that passed the check.';return}if(!agent){$('status').textContent='The User-Agent is required: Cloudflare ties the clearance to it.';return}orca.postMessage({action:'cloudflare_save',host:host,cookie:cookie,user_agent:agent});$('cf-submit').disabled=true;$('status').textContent='Saving Cloudflare verification...'}
function forgetCloudflare(){var host=$('cf-host').value.trim();if(!host){$('status').textContent='Enter the host to forget.';return}orca.postMessage({action:'cloudflare_forget',host:host})}
function doDownload(){if(selectedModel)openExternal(selectedModel.url||selectedModel.download_url)}
function doImport(){if(!selectedModel)return;if(selectedModel.result_type==='search_link'){doDownload();return}if(selectedModel.requires_auth&&!isAuthed(selectedModel)){pendingImport=selectedModel;openAuth(platformKey(selectedModel.platform));return}var platform=platformKey(selectedModel.platform);if(!selectedModel._download_format&&(platform==='makerworld'||platform==='crealitycloud'||platform==='nexprint')){var cached=makerWorldChoicesCache[modelIdentity(selectedModel)];if(cached){showMakerWorldChoices({model:selectedModel,profiles:cached.profiles,formats:cached.formats,default_profile_id:cached.default_profile_id,picker_platform:cached.picker_platform});$('status').textContent='Select a '+cached.picker_platform+' print profile and file format.';return}}var b=$('det-import-btn');b.disabled=true;b.textContent='Resolving...';$('status').textContent='Resolving files...';orca.postMessage({action:'resolve_import',model:selectedModel})}
function openAuth(p){authPlatform=p;$('auth-modal').classList.add('active');$('modal-bg').classList.add('active');$('auth-password').value='';$('auth-code').value='';$('auth-token').value='';$('code-field').style.display='none';$('import-anycubic').style.display=p==='makeronline'?'':'none';var tokenOnly=(p==='nexprint'||p==='makeronline'||p==='cults3d'||p==='grabcad'||p==='thingiverse'||p==='myminifactory'||p==='crealitycloud'||p==='thangs');$('password-field').style.display=tokenOnly?'none':'';$('email-field').style.display=tokenOnly?'none':'';var title={makerworld:'MakerWorld / Bambu account',nexprint:'Nexprint / Elegoo account',makeronline:'Makeronline / Anycubic account',cults3d:'Cults3D account',grabcad:'GrabCAD account',thingiverse:'Thingiverse API',myminifactory:'MyMiniFactory API',crealitycloud:'Creality Cloud account',thangs:'Thangs account'}[p]||'Account';$('auth-title').textContent=title;$('token-label').textContent=p==='nexprint'?'Nexprint auth_token cookie value':p==='makeronline'?'MakerOnline mo_access_token value or Cookie header':p==='crealitycloud'?'Creality model_token value or Cookie header':p==='thangs'?'Thangs Bearer access token':p==='grabcad'?'GrabCAD Cookie header / session cookies':p==='cults3d'?'Cults3D Cookie header / session cookies':p==='thingiverse'?'Thingiverse access token':p==='myminifactory'?'MyMiniFactory API key':'Session/access token (alternative)';$('auth-note').textContent=AUTH_HELP[p]||'Use the account credentials supplied by the selected platform.';var st=authStates[p]||{};$('auth-logout').style.display=st.authenticated?'':'none'}
function syncBackdrop(){var active=$('auth-modal').classList.contains('active')||$('file-modal').classList.contains('active')||$('cloudflare-modal').classList.contains('active')||$('makerworld-modal').classList.contains('active');$('modal-bg').classList.toggle('active',active)}
function closeAuth(){orca.postMessage({action:'auth_cancel_login'});$('auth-modal').classList.remove('active');$('auth-password').value='';$('auth-token').value='';syncBackdrop()}
function closeFilePicker(){$('file-modal').classList.remove('active');hideImagePreview(null,true);pendingFiles=[];syncBackdrop();var b=$('det-import-btn');if(b){b.disabled=false;b.textContent='Import into OrcaSlicer'}}
function closeMakerWorldPicker(){$('makerworld-modal').classList.remove('active');hideImagePreview(null,true);pendingMakerWorldModel=null;syncBackdrop();var b=$('det-import-btn');if(b){b.disabled=false;b.textContent='Import into OrcaSlicer'}}
function closeTopModal(){if($('file-modal').classList.contains('active'))closeFilePicker();else if($('makerworld-modal').classList.contains('active'))closeMakerWorldPicker();else if($('cloudflare-modal').classList.contains('active'))closeCloudflare();else closeAuth()}
function updateFileCount(){var all=document.querySelectorAll('#file-list input[type=checkbox]');var checked=document.querySelectorAll('#file-list input[type=checkbox]:checked');$('file-count').textContent=checked.length+' / '+all.length+' selected';$('file-import').disabled=checked.length===0}
function formatBytes(v){v=Number(v);if(!isFinite(v)||v<0)return'';var units=['B','KB','MB','GB'],i=0;while(v>=1024&&i<units.length-1){v/=1024;i++}return(v>=10||i===0?Math.round(v):Math.round(v*10)/10)+' '+units[i]}
function showFilePicker(files){pendingFiles=files||[];var html='';pendingFiles.forEach(function(f){var name=f.name||('File '+(Number(f.index)+1)),image=safeUrl(f.preview_url),preview=image?'<img class="file-preview" src="'+esc(image)+'" loading="lazy" decoding="async" alt="'+esc(name)+' preview" tabindex="0" onmouseenter="showImagePreview(this)" onmouseleave="hideImagePreview(this)" onfocus="showImagePreview(this)" onblur="hideImagePreview(this)">':'<span class="file-preview-placeholder">No preview</span>',meta=formatBytes(f.size);html+='<label class="file-choice"><input type="checkbox" checked value="'+Number(f.index)+'" onchange="updateFileCount()">'+preview+'<span class="file-details"><span class="file-name">'+esc(name)+'</span>'+(meta?'<span class="file-meta">'+esc(meta)+'</span>':'')+'</span></label>'});$('file-list').innerHTML=html;$('file-modal').classList.add('active');syncBackdrop();updateFileCount()}
function setAllFiles(value){document.querySelectorAll('#file-list input[type=checkbox]').forEach(function(x){x.checked=!!value});updateFileCount()}
function confirmFileImport(){var selected=[];document.querySelectorAll('#file-list input[type=checkbox]:checked').forEach(function(x){selected.push(parseInt(x.value,10))});if(!selected.length)return;$('file-import').disabled=true;$('status').textContent='Downloading selected files...';$('file-modal').classList.remove('active');syncBackdrop();orca.postMessage({action:'import_selected',indices:selected})}
function profileMeta(p){var v=[];if(p.creator)v.push('by '+p.creator);if(p.printer)v.push(p.printer);if(p.filament)v.push(p.filament);if(p.layer_height)v.push(p.layer_height+' layer');if(p.walls)v.push(p.walls+' walls');if(p.infill)v.push(p.infill+' infill');if(p.prediction_text)v.push(p.prediction_text);else if(p.prediction)v.push(Math.max(1,Math.round(Number(p.prediction)/3600*10)/10)+' h');if(p.plates)v.push(p.plates+' plate'+(Number(p.plates)===1?'':'s'));if(p.rating!=null)v.push('Rating '+p.rating+(p.rating_count?' ('+p.rating_count+')':''));if(p.downloads!=null)v.push('Downloads '+compactNumber(p.downloads));if(p.size!=null)v.push(formatBytes(p.size));return v.join(' / ')}
function updateMakerWorldChoice(){var f=document.querySelector('#mw-formats input:checked');var p=document.querySelector('#mw-profiles input:checked'),platform=pendingMakerWorldModel&&pendingMakerWorldModel.platform||'platform';$('mw-import').disabled=!(f&&p);$('mw-import').textContent=f&&f.value==='raw_browser'?'Open STL/CAD files in '+platform:'Download selected 3MF'}
function positionImagePreview(source){var preview=$('image-preview'),r=source.getBoundingClientRect(),gap=12,margin=10,left=r.right+gap,top=r.top;if(left+preview.offsetWidth>window.innerWidth-margin)left=r.left-preview.offsetWidth-gap;left=Math.max(margin,Math.min(left,window.innerWidth-preview.offsetWidth-margin));top=Math.max(margin,Math.min(top,window.innerHeight-preview.offsetHeight-margin));preview.style.left=Math.round(left)+'px';preview.style.top=Math.round(top)+'px';preview.style.visibility='visible'}
function showImagePreview(source){var url=safeUrl(source.currentSrc||source.src||(source.dataset&&source.dataset.src));if(!url)return;if(source.classList.contains('result-image')&&!source.src)loadResultImage(source);var preview=$('image-preview');preview.src=url;preview.alt=(source.alt||'Image')+' enlarged preview';preview.classList.add('active');preview.style.visibility='hidden';requestAnimationFrame(function(){positionImagePreview(source)})}
function hideImagePreview(source,force){if(!force&&source&&(source.matches(':hover')||document.activeElement===source))return;var preview=$('image-preview');preview.classList.remove('active');preview.style.visibility='hidden'}
function showMakerWorldChoices(msg){cacheMakerWorldChoices(msg);pendingMakerWorldModel=msg.model||selectedModel;var platform=msg.picker_platform||pendingMakerWorldModel.platform||'platform',profiles=msg.profiles||[],formats=msg.formats||[],defaultId=String(msg.default_profile_id||'');$('profile-picker-title').textContent='Choose '+platform+' download';$('profile-picker-note').textContent='Select a print profile and file format. 3MF uses the official signed profile URL.';var phtml='';profiles.forEach(function(p,i){var id=String(p.profile_id||''),title=p.title||'Print profile';var image=safeUrl(p.cover),summary=p.summary?'<span class="mw-summary">'+esc(p.summary)+'</span>':'';phtml+='<label class="mw-profile"><input type="radio" name="mw-profile" value="'+esc(id)+'" '+((id===defaultId||(!defaultId&&i===0))?'checked':'')+' onchange="updateMakerWorldChoice()">'+(image?'<img class="mw-cover" src="'+esc(image)+'" alt="'+esc(title)+'" tabindex="0" onmouseenter="showImagePreview(this)" onmouseleave="hideImagePreview(this)" onfocus="showImagePreview(this)" onblur="hideImagePreview(this)">':'<span class="mw-cover"></span>')+'<span><span class="mw-title">'+esc(title)+'</span>'+summary+'<span class="mw-meta">'+esc(profileMeta(p))+'</span></span></label>'});$('mw-profiles').innerHTML=phtml;var fhtml='',first=true;formats.forEach(function(f){if(f.available===false)return;fhtml+='<label class="mw-format"><input type="radio" name="mw-format" value="'+esc(f.id)+'" '+(first?'checked':'')+' onchange="updateMakerWorldChoice()"><span><strong>'+esc(f.label)+'</strong><small>'+esc(f.description||'')+'</small></span></label>';first=false});$('mw-formats').innerHTML=fhtml;$('makerworld-modal').classList.add('active');syncBackdrop();updateMakerWorldChoice()}
function confirmMakerWorldChoice(){var p=document.querySelector('#mw-profiles input:checked'),f=document.querySelector('#mw-formats input:checked');if(!p||!f||!pendingMakerWorldModel)return;var platform=pendingMakerWorldModel.platform||'platform';$('mw-import').disabled=true;$('status').textContent=f.value==='3mf'?'Resolving selected '+platform+' profile...':'Opening '+platform+'...';orca.postMessage({action:'resolve_profile_choice',model:pendingMakerWorldModel,profile_id:p.value,format:f.value})}
function submitAuth(){var token=$('auth-token').value.trim(),email=$('auth-email').value.trim(),password=$('auth-password').value,code=$('auth-code').value.trim();if(authPlatform==='nexprint'&&!token){$('status').textContent='Nexprint: paste auth_token after signing in.';return}if(authPlatform==='makeronline'&&!token){$('status').textContent='Makeronline: import the Anycubic Slicer Next session or paste an access token.';return}if(authPlatform==='crealitycloud'&&!token){$('status').textContent='Creality Cloud: paste model_token after signing in.';return}if(authPlatform==='thangs'&&!token){$('status').textContent='Thangs: paste the Bearer access token after signing in.';return}if(authPlatform==='grabcad'&&!token){$('status').textContent='GrabCAD: paste the Cookie header/session cookies after signing in.';return}if(authPlatform==='cults3d'&&!token){$('status').textContent='Cults3D: paste the Cookie header/session cookies after signing in.';return}if((authPlatform==='thingiverse'||authPlatform==='myminifactory')&&!token){$('status').textContent='Paste the API token/key first.';return}orca.postMessage({action:'auth_login',platform:authPlatform,token:token,email:email,password:password,code:code});$('auth-submit').disabled=true;$('status').textContent='Saving session...'}
function logoutAuth(){orca.postMessage({action:'auth_logout',platform:authPlatform});closeAuth()}
function startSeamlessLogin(){if(!authPlatform)return;orca.postMessage({action:'auth_start_login',platform:authPlatform});$('status').textContent='Opening the portal sign-in and a local finish page...'}
function openOfficialLogin(){orca.postMessage({action:'auth_open_login',platform:authPlatform});if(authPlatform==='makeronline')$('status').textContent='Anycubic login opened. After MakerOnline returns, copy mo_access_token, paste it here, and connect.'}
function importAnycubic(){orca.postMessage({action:'auth_import_anycubic'});$('status').textContent='Looking for Anycubic Slicer Next session...'}
orca.onMessage(function(msg){
  msg=msg||{};
  if(msg.action==='results'){
    searching=false;$('search-btn').disabled=false;$('search-btn').textContent='Search';renderResults(msg.results||[],msg.append?false:true);renderSourceResults(msg.sources||[],msg.can_load_more);
  }else if(msg.action==='model_details'){
    applyModelDetails(msg.model||{},!!msg.background);
  }else if(msg.action==='login_pending'){
    $('status').textContent='Sign in in the browser, then finish on the local page that opened. OrcaSlicer picks the session up automatically (link valid for '+Math.round((msg.timeout||300)/60)+' min).';
  }else if(msg.action==='login_cancelled'){
    $('status').textContent='Browser sign-in cancelled.';
  }else if(msg.action==='cloudflare_required'){
    $('status').textContent=msg.message||'Cloudflare verification is required.';openCloudflare(msg.host||'',msg.url||'');
  }else if(msg.action==='auth_status'||msg.action==='auth_changed'){
    updateAuth(msg.states||{});renderCloudflare(msg.cloudflare);$('auth-submit').disabled=false;$('cf-submit').disabled=false;
    if($('cloudflare-modal').classList.contains('active')&&msg.action==='auth_changed')closeCloudflare();
    if(msg.action==='auth_changed'){
      closeAuth();$('status').textContent=msg.message||'Account session updated.';
      if(pendingImport&&isAuthed(pendingImport)){var m=pendingImport;pendingImport=null;selectedModel=m;doImport()}
      else if(selectedModel){prefetchMakerWorld(selectedModel)}
    }
  }else if(msg.action==='auth_challenge'){
    $('auth-submit').disabled=false;$('code-field').style.display='';$('status').textContent=msg.message||'Verification code required.';
  }else if(msg.action==='auth_required'){
    $('det-import-btn').disabled=false;$('det-import-btn').textContent='Import into OrcaSlicer';$('status').textContent=msg.message||'Login required.';pendingImport=msg.model||selectedModel;openAuth(msg.platform);
  }else if(msg.action==='profile_prefetched'){
    cacheMakerWorldChoices(msg);
  }else if(msg.action==='profile_prefetch_failed'){
    makerWorldPrefetching[modelIdentity(msg.model||{})]=false;
  }else if(msg.action==='profile_choices'){
    showMakerWorldChoices(msg);$('status').textContent='Select a '+(msg.picker_platform||'platform')+' print profile and file format.';
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

PAGE = PAGE.replace(
    "__PLATFORM_KEYS__",
    json.dumps(
        {spec.display: spec.key for spec in _PLATFORM_SPECS},
        sort_keys=True,
        separators=(",", ":"),
    ),
)


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
            self._detail_prefetch_lock = threading.RLock()
            self._detail_prefetch_inflight = set()
            self._login_lock = threading.RLock()
            self._login = None

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
            self._stop_login()

        def _post(self, msg):
            if self.win is not None and self.win.is_open():
                self.win.post(msg)

        def _post_auth(self, action="auth_status", message=""):
            self._post(
                {
                    "action": action,
                    "states": self.auth.status(),
                    "cloudflare": self.auth.clearance.status(),
                    "message": message,
                }
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
                "prefetch_profile_choices": self._handle_profile_prefetch,
                "resolve_import": self._handle_resolve_import,
                "resolve_profile_choice": self._handle_profile_choice,
                "import_selected": self._handle_import_selected,
                "open_external": self._handle_open_external,
                "auth_status": self._handle_auth_status,
                "auth_login": self._handle_auth_login,
                "auth_logout": self._handle_auth_logout,
                "auth_open_login": self._handle_auth_open_login,
                "auth_import_anycubic": self._handle_auth_import_anycubic,
                "auth_start_login": self._handle_auth_start_login,
                "auth_cancel_login": self._handle_auth_cancel_login,
                "cloudflare_save": self._handle_cloudflare_save,
                "cloudflare_forget": self._handle_cloudflare_forget,
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

        def _handle_profile_prefetch(self, msg):
            model = msg.get("model") or {}
            if model:
                self._start(self._prefetch_profile_choices, model)

        def _handle_resolve_import(self, msg):
            model = msg.get("model") or {}
            if model:
                self._start(self._resolve_import, model)

        def _handle_profile_choice(self, msg):
            model = msg.get("model") or {}
            if model:
                self._start(self._resolve_profile_choice, model, msg)

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

        def _handle_auth_start_login(self, msg):
            self._start(self._do_start_login, msg.get("platform", ""))

        def _handle_auth_cancel_login(self, _msg):
            self._stop_login()
            self._post({"action": "login_cancelled"})

        def _stop_login(self):
            with self._login_lock:
                receiver, self._login = self._login, None
            if receiver is not None:
                receiver.stop()

        def _store_login_credential(self, platform, value):
            """Called from the loopback receiver once the user submits."""
            self.auth.save_token(platform, value, label="Browser sign-in")
            with self._login_lock:
                self._login = None
            self._post_auth(
                "auth_changed",
                f"{_display_name(platform)} connected from your browser.",
            )

        def _do_start_login(self, platform):
            spec = _platform(platform)
            if spec is None or not spec.requires_auth:
                self._post(
                    {"action": "error", "message": "This portal does not use a session."}
                )
                return
            self._stop_login()
            receiver = LoginReceiver(platform, self._store_login_credential)
            try:
                url = receiver.start()
            except OSError as exc:
                # A sandbox may refuse the loopback socket. Fall back to the
                # existing paste flow rather than leaving the user stuck.
                self._post(
                    {
                        "action": "error",
                        "message": (
                            "Could not open the local sign-in endpoint "
                            f"({exc}). Use the token field instead."
                        ),
                    }
                )
                return
            with self._login_lock:
                self._login = receiver
            if spec.login_url:
                self._open_external(spec.login_url)
            self._open_external(url)
            self._post(
                {
                    "action": "login_pending",
                    "platform": platform,
                    "url": url,
                    "timeout": receiver.timeout,
                }
            )

        def _handle_cloudflare_save(self, msg):
            self._start(self._do_cloudflare_save, msg)

        def _handle_cloudflare_forget(self, msg):
            host = CloudflareClearance.normalize_host(msg.get("host") or "")
            removed = self.auth.clearance.forget(host) if host else False
            self._post_auth(
                "auth_changed",
                f"Cloudflare verification for {host} removed."
                if removed
                else "No stored Cloudflare verification matched that host.",
            )

        def _do_cloudflare_save(self, msg):
            """Store a clearance the user obtained by passing the check itself."""
            try:
                record = self.auth.clearance.save(
                    msg.get("host") or msg.get("url") or "",
                    msg.get("cookie") or "",
                    msg.get("user_agent") or "",
                )
            except (AuthError, OSError, ValueError) as exc:
                self._post({"action": "error", "message": str(exc)})
                return
            finally:
                # The clearance is a session secret; drop the inbound copy.
                msg.pop("cookie", None)
            self._post_auth(
                "auth_changed",
                f"Cloudflare verification stored for {record['host']}. "
                "It expires on Cloudflare's schedule and is tied to this "
                "machine's IP address and the User-Agent you supplied.",
            )

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
            requires_auth = any(item.get("requires_auth") for item in rows)
            authenticated = (
                self.auth.authenticated(spec.key) if requires_auth else False
            )
            importable = callable(getattr(spec.adapter, "get_files", None))
            for item in rows:
                item["_platform_key"] = spec.key
                item["authenticated"] = not item.get("requires_auth") or authenticated
                item["importable"] = importable
            if not spec.paginated_search:
                has_more = False
            elif has_more is None:
                has_more = len(rows) >= spec.search_page_size
            return rows, total, bool(has_more)

        def _fetch_search_page(self, spec, query, options, page):
            """Load one independent portal page inside a search worker."""
            try:
                items, total, has_more = self._load_search_page(
                    spec, query, options, page
                )
            except BrowserRequired as exc:
                return (
                    None,
                    _source_stats(
                        spec,
                        error=str(exc),
                        browser_url=exc.url,
                        cloudflare_host=getattr(exc, "host", ""),
                    ),
                    True,
                )
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                return None, _source_stats(spec, error=str(exc)), False
            return (
                (items, total, has_more),
                _source_stats(spec, page=page, total=total, has_more=has_more),
                False,
            )

        def _run_search_pages(self, query, options, jobs):
            """Run portal requests concurrently and return outcomes by key."""
            if not jobs:
                return {}
            if len(jobs) == 1:
                spec, page = jobs[0]
                return {
                    spec.key: self._fetch_search_page(spec, query, options, page)
                }
            outcomes = {}
            with ThreadPoolExecutor(
                max_workers=min(_SEARCH_WORKERS, len(jobs))
            ) as executor:
                futures = {
                    executor.submit(
                        self._fetch_search_page, spec, query, options, page
                    ): spec.key
                    for spec, page in jobs
                }
                for future in as_completed(futures):
                    outcomes[futures[future]] = future.result()
            return outcomes

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

        def _prepare_thingiverse_prefetch(self, results, generation):
            candidates = []
            with self._detail_prefetch_lock:
                for item in results:
                    if item.get("_platform_key") != "thingiverse":
                        continue
                    license_name = str(item.get("license") or "").strip().casefold()
                    if license_name not in ("", "unknown"):
                        continue
                    if item.get("_details_loaded"):
                        continue
                    key = (generation, _result_identity(item))
                    if key in self._detail_prefetch_inflight:
                        continue
                    self._detail_prefetch_inflight.add(key)
                    item["_details_loading"] = True
                    candidates.append(dict(item))
            return candidates

        def _release_detail_prefetch(self, generation, candidates):
            with self._detail_prefetch_lock:
                for model in candidates:
                    self._detail_prefetch_inflight.discard(
                        (generation, _result_identity(model))
                    )

        def _load_model_details(self, model):
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
            return result

        def _cache_model_details(self, result, generation=None):
            identity = _result_identity(result)
            with self._search_lock:
                if generation is not None and generation != self._search_generation:
                    return False
                for index, item in enumerate(self._search_results):
                    if _result_identity(item) == identity:
                        self._search_results[index] = dict(result)
                        return True
            return False

        def _prefetch_thingiverse_details(self, generation, candidates):
            with ThreadPoolExecutor(max_workers=_DETAIL_PREFETCH_WORKERS) as executor:
                futures = {
                    executor.submit(self._load_model_details, model): model
                    for model in candidates
                }
                for future in as_completed(futures):
                    model = futures[future]
                    try:
                        result = future.result()
                    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                        result = dict(model)
                        result["_details_loaded"] = False
                        result["_details_loading"] = False
                    key = (generation, _result_identity(model))
                    with self._detail_prefetch_lock:
                        self._detail_prefetch_inflight.discard(key)
                    if result.get("_details_error"):
                        result = dict(model)
                        result["_details_loaded"] = False
                        result["_details_loading"] = False
                    if self._cache_model_details(result, generation):
                        self._post(
                            {
                                "action": "model_details",
                                "model": result,
                                "background": True,
                            }
                        )

        def _do_search(self, msg, generation):
            query = str(msg.get("query") or "").strip()
            options = msg.get("options") if isinstance(msg.get("options"), dict) else {}
            platforms = [
                key
                for key in dict.fromkeys(msg.get("platforms", []))
                if _platform(key) is not None
            ]
            jobs = [
                (spec, 1)
                for key in platforms
                if (spec := _platform(key)) is not None
            ]
            outcomes = self._run_search_pages(query, options, jobs)
            results = []
            stats = {}
            pages = {}
            for spec, page in jobs:
                payload, source, _stop = outcomes[spec.key]
                if payload is None:
                    pages[spec.key] = 0
                else:
                    items, _total, _has_more = payload
                    results, _added = _merge_unique_results(results, items)
                    pages[spec.key] = page
                stats[spec.key] = source
            _apply_loaded_counts(results, stats)
            detail_candidates = self._prepare_thingiverse_prefetch(
                results, generation
            )
            with self._search_lock:
                if generation != self._search_generation:
                    self._release_detail_prefetch(generation, detail_candidates)
                    return
                self._search_query = query
                self._search_platforms = platforms
                self._search_options = dict(options)
                self._search_results = results
                self._search_pages = pages
                self._search_stats = stats
                self._search_loading_more = False
            self._post(self._search_payload(append=False))
            if detail_candidates:
                self._start(
                    self._prefetch_thingiverse_details,
                    generation,
                    detail_candidates,
                )

        def _do_search_more(self, generation):
            with self._search_lock:
                query = self._search_query
                options = dict(self._search_options)
                platforms = list(self._search_platforms)
                results = list(self._search_results)
                pages = dict(self._search_pages)
                stats = {key: dict(value) for key, value in self._search_stats.items()}
            jobs = []
            for key in platforms:
                source = stats.get(key) or {}
                spec = _platform(key)
                if spec is None or not source.get("has_more"):
                    continue
                jobs.append((spec, pages.get(key, 0) + 1))
            outcomes = self._run_search_pages(query, options, jobs)
            for spec, next_page in jobs:
                source = stats[spec.key]
                payload, fresh, stop = outcomes[spec.key]
                if payload is None:
                    if stop:
                        source["has_more"] = False
                    source["error"] = fresh["error"]
                    source["browser_url"] = fresh["browser_url"]
                    continue
                items, total, has_more = payload
                results, added = _merge_unique_results(results, items)
                pages[spec.key] = next_page
                source.update(
                    {
                        "page": next_page,
                        "total": total if total is not None else source.get("total"),
                        "has_more": has_more and added > 0,
                        "error": "",
                        "browser_url": "",
                    }
                )
            _apply_loaded_counts(results, stats)
            detail_candidates = self._prepare_thingiverse_prefetch(
                results, generation
            )
            with self._search_lock:
                if generation != self._search_generation:
                    self._release_detail_prefetch(generation, detail_candidates)
                    return
                self._search_results = results
                self._search_pages = pages
                self._search_stats = stats
                self._search_loading_more = False
            self._post(self._search_payload(append=True))
            if detail_candidates:
                self._start(
                    self._prefetch_thingiverse_details,
                    generation,
                    detail_candidates,
                )

        def _do_model_details(self, model):
            result = self._load_model_details(model)
            self._cache_model_details(result)
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
            if spec.profile_picker and not model.get("_download_format"):
                self._show_profile_choices(model)
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

        def _profile_choices(self, model):
            spec = _platform_for_model(model)
            if spec is None or not spec.profile_picker:
                raise ValueError("This platform does not provide print profiles")
            loader = getattr(spec.adapter, "get_download_choices", None)
            if not callable(loader):
                raise TypeError(f"{spec.display} does not provide download choices")
            choices = loader(model, self.auth)
            if not isinstance(choices, dict):
                raise TypeError(f"{spec.display} returned invalid download choices")
            return spec, choices

        def _show_profile_choices(self, model):
            try:
                spec, choices = self._profile_choices(model)
            except AuthRequired as exc:
                spec = _platform_for_model(model)
                platform = spec.key if spec else ""
                if platform:
                    self.auth.logout(platform)
                    self._post_auth(
                        "auth_changed", f"{_display_name(platform)} session expired."
                    )
                self._post(
                    {
                        "action": "auth_required",
                        "platform": platform,
                        "message": str(exc),
                        "model": model,
                    }
                )
                return
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                self._post(
                    {
                        "action": "error",
                        "message": f"Could not load print profiles: {exc}",
                    }
                )
                return
            self._post(
                {
                    "action": "profile_choices",
                    "model": model,
                    "picker_platform": spec.display,
                    **choices,
                }
            )

        def _prefetch_profile_choices(self, model):
            try:
                spec, choices = self._profile_choices(model)
            except (AuthRequired, OSError, ValueError, RuntimeError, KeyError, TypeError):
                self._post({"action": "profile_prefetch_failed", "model": model})
                return
            self._post(
                {
                    "action": "profile_prefetched",
                    "model": model,
                    "picker_platform": spec.display,
                    **choices,
                }
            )

        def _resolve_profile_choice(self, model, msg):
            spec = _platform_for_model(model)
            if spec is None:
                self._post({"action": "error", "message": "Invalid platform choice."})
                return
            if spec.key == "makerworld":
                self._resolve_makerworld_choice(model, msg)
                return
            if spec.key == "nexprint":
                profile_id = str(msg.get("profile_id") or "")
                download_format = str(msg.get("format") or "")
                if not profile_id.isdigit():
                    self._post(
                        {
                            "action": "error",
                            "message": "Select a Nexprint print profile.",
                        }
                    )
                    return
                if download_format != "3mf":
                    self._post(
                        {"action": "error", "message": "Select the 3MF format."}
                    )
                    return
                selected = dict(model)
                selected["_profile_id"] = profile_id
                selected["_download_format"] = "3mf"
                self._resolve_import(selected)
                return
            if spec.key != "crealitycloud":
                self._post({"action": "error", "message": "Invalid platform choice."})
                return
            profile_id = str(msg.get("profile_id") or "")
            download_format = str(msg.get("format") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", profile_id):
                self._post(
                    {"action": "error", "message": "Select a Creality print profile."}
                )
                return
            if download_format == "raw_browser":
                base_url = str(model.get("url") or "")
                separator = "&" if "?" in base_url else "?"
                url = base_url
                if profile_id != "model-files":
                    url += separator + "profileId=" + urllib.parse.quote(profile_id)
                self._post(
                    {
                        "action": "browser_required",
                        "message": (
                            "Creality Cloud STL/CAD files require its official "
                            "browser download flow."
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
            except CloudflareChallenge as exc:
                self._post(
                    {
                        "action": "cloudflare_required",
                        "message": str(exc),
                        "url": exc.url or model.get("url", ""),
                        "host": exc.host,
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
                selected = [
                    files[i] for i in selected_indices if i < len(files)
                ]
                if model and selected:
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
