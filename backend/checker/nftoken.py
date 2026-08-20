"""NFToken generation — ported 1:1 from original Netflix-Cookie-Checker.

Creates a passwordless login link:
  PC:     https://netflix.com/?nftoken=TOKEN
  Phone:  https://netflix.com/unsupported?nftoken=TOKEN

Open the link in a clean browser (Incognito, no old Netflix cookies).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

import requests
from urllib3.exceptions import InsecureRequestWarning

from .utils import decode_netflix_value

requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

# ---- exact constants from original main.py ----
NFTOKEN_API_URL = "https://ios.prod.ftl.netflix.com/iosui/user/15.48"

# NOTE: esn keeps %3D like the original. We build the query string manually
# so requests does NOT double-encode it.
NFTOKEN_QUERY_PARAMS = {
    "appVersion": "15.48.1",
    "config": (
        '{"gamesInTrailersEnabled":"false","isTrailersEvidenceEnabled":"false",'
        '"cdsMyListSortEnabled":"true","kidsBillboardEnabled":"true",'
        '"addHorizontalBoxArtToVideoSummariesEnabled":"false","skOverlayTestEnabled":"false",'
        '"homeFeedTestTVMovieListsEnabled":"false","baselineOnIpadEnabled":"true",'
        '"trailersVideoIdLoggingFixEnabled":"true","postPlayPreviewsEnabled":"false",'
        '"bypassContextualAssetsEnabled":"false","roarEnabled":"false",'
        '"useSeason1AltLabelEnabled":"false",'
        '"disableCDSSearchPaginationSectionKinds":["searchVideoCarousel"],'
        '"cdsSearchHorizontalPaginationEnabled":"true","searchPreQueryGamesEnabled":"true",'
        '"kidsMyListEnabled":"true","billboardEnabled":"true","useCDSGalleryEnabled":"true",'
        '"contentWarningEnabled":"true","videosInPopularGamesEnabled":"true",'
        '"avifFormatEnabled":"false","sharksEnabled":"true"}'
    ),
    "device_type": "NFAPPL-02-",
    "esn": (
        "NFAPPL-02-IPHONE8%3D1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78L"
        "MD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200"
    ),
    "idiom": "phone",
    "iosVersion": "15.8.5",
    "isTablet": "false",
    "languages": "en-US",
    "locale": "en-US",
    "maxDeviceWidth": "375",
    "model": "saget",
    "modelType": "IPHONE8-1",
    "odpAware": "true",
    "path": '["account","token","default"]',
    "pathFormat": "graph",
    "pixelDensity": "2.0",
    "progressive": "false",
    "responseFormat": "json",
}

NFTOKEN_HEADERS = {
    "User-Agent": "Argo/15.48.1 (iPhone; iOS 15.8.5; Scale/2.00)",
    "x-netflix.request.attempt": "1",
    "x-netflix.request.client.user.guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.context.profile-guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.request.routing": (
        '{"path":"/nq/mobile/nqios/~15.48.0/user","control_tag":"iosui_argo"}'
    ),
    "x-netflix.context.app-version": "15.48.1",
    "x-netflix.argo.translated": "true",
    "x-netflix.context.form-factor": "phone",
    "x-netflix.context.sdk-version": "2012.4",
    "x-netflix.client.appversion": "15.48.1",
    "x-netflix.context.max-device-width": "375",
    "x-netflix.context.ab-tests": "",
    "x-netflix.tracing.cl.useractionid": "4DC655F2-9C3C-4343-8229-CA1B003C3053",
    "x-netflix.client.type": "argo",
    "x-netflix.client.ftl.esn": (
        "NFAPPL-02-IPHONE8=1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78L"
        "MD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200"
    ),
    "x-netflix.context.locales": "en-US",
    "x-netflix.context.top-level-uuid": "90AFE39F-ADF1-4D8A-B33E-528730990FE3",
    "x-netflix.client.iosversion": "15.8.5",
    "accept-language": "en-US;q=1",
    "x-netflix.argo.abtests": "",
    "x-netflix.context.os-version": "15.8.5",
    "x-netflix.request.client.context": '{"appState":"foreground"}',
    "x-netflix.context.ui-flavor": "argo",
    "x-netflix.argo.nfnsm": "9",
    "x-netflix.context.pixel-density": "2.0",
    "x-netflix.request.toplevel.uuid": "90AFE39F-ADF1-4D8A-B33E-528730990FE3",
    "x-netflix.request.client.timezoneid": "Asia/Dhaka",
}


def _build_request_url() -> str:
    """Build URL with original query encoding (esn already has %3D)."""
    # urlencode with doseq; then restore pre-encoded % in esn/config path
    # Safer: encode each value except leave esn as provided.
    parts = []
    for key, value in NFTOKEN_QUERY_PARAMS.items():
        if key == "esn":
            # original ships esn already percent-encoded
            parts.append(f"{key}={value}")
        else:
            parts.append(f"{quote(str(key), safe='')}={quote(str(value), safe='')}")
    return f"{NFTOKEN_API_URL}?{'&'.join(parts)}"


def get_nftoken_expiry_utc(expires: Any = None) -> str:
    """Match original: parse timestamp, else default +1 hour."""
    normalized = decode_netflix_value(expires) if not isinstance(expires, (int, float)) else expires
    if isinstance(normalized, str):
        normalized = normalized.strip()
        if normalized.isdigit():
            try:
                normalized = int(normalized)
            except Exception:
                normalized = None

    if isinstance(normalized, (int, float)):
        try:
            timestamp = int(normalized)
            if len(str(abs(timestamp))) == 13:
                timestamp //= 1000
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
        except Exception:
            pass

    return (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S UTC")


def _clean_token(token: Any) -> Optional[str]:
    """Light clean only — do NOT run full decode_netflix_value on tokens.

    decode_netflix_value collapses whitespace / unicode escapes and can
    corrupt a valid NFToken so the magic link no longer logs in.
    """
    if token is None:
        return None
    text = str(token).strip()
    # JSON may leave surrounding quotes
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]
    text = text.replace("\n", "").replace("\r", "").replace("\t", "").strip()
    return text or None


def create_nftoken(
    cookie_dict: Dict[str, str], attempts: int = 1
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Exact flow from original: Cookie: NetflixId=... only, verify=False."""
    # Prefer raw value; only lightly clean (original used decode_netflix_value —
    # we keep that for the cookie id which is not the token itself)
    netflix_id = decode_netflix_value(cookie_dict.get("NetflixId"))
    if not netflix_id:
        # fallback raw
        netflix_id = str(cookie_dict.get("NetflixId") or "").strip() or None
    if not netflix_id:
        return None, "Missing required cookies for NFToken (NetflixId)"

    headers = dict(NFTOKEN_HEADERS)
    # Original: ONLY NetflixId in Cookie header
    headers["Cookie"] = f"NetflixId={netflix_id}"

    try:
        attempts = max(1, int(attempts))
    except Exception:
        attempts = 1

    url = _build_request_url()
    last_error = "NFToken API error"

    for _ in range(attempts):
        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=30,
                verify=False,
            )
            if response.status_code != 200:
                if response.status_code == 403:
                    last_error = "403"
                elif response.status_code == 429:
                    last_error = "429"
                elif response.status_code == 401:
                    last_error = "401 — NetflixId hết hạn, export cookie mới"
                else:
                    last_error = f"HTTP {response.status_code}"
                continue

            data = response.json()
            token_data = (
                (((data.get("value") or {}).get("account") or {}).get("token") or {}).get(
                    "default"
                )
                or {}
            )
            # CRITICAL: do not over-decode the token string
            token = _clean_token(token_data.get("token"))
            if not token:
                # original also tried decode_netflix_value — use as last resort only
                token = _clean_token(decode_netflix_value(token_data.get("token")))
            expires = token_data.get("expires")
            if token:
                return {
                    "token": token,
                    "expires_at_utc": get_nftoken_expiry_utc(expires),
                }, None

            # empty value {} => dead cookie
            if isinstance(data, dict) and data.get("value") == {}:
                last_error = (
                    "Token missing — NetflixId invalid/expired. "
                    "Re-export cookie while logged in on netflix.com"
                )
            else:
                last_error = "Token missing in response"
        except requests.exceptions.Timeout:
            last_error = "timeout"
        except requests.exceptions.ProxyError:
            last_error = "proxy error"
        except requests.exceptions.RequestException as exc:
            last_error = f"NFToken API error ({exc.__class__.__name__})"
        except Exception as exc:
            last_error = f"NFToken API error ({exc.__class__.__name__})"
    return None, last_error


def has_usable_nftoken(nftoken_data: Optional[Dict]) -> bool:
    if not isinstance(nftoken_data, dict):
        return False
    token = _clean_token(nftoken_data.get("token"))
    return bool(token)


def build_nftoken_links(token: Optional[str], mode: str) -> List[Tuple[str, str]]:
    """Exact link format from original (raw token, host netflix.com).

    PC magic link (no password):
      https://netflix.com/?nftoken=TOKEN
    Phone:
      https://netflix.com/unsupported?nftoken=TOKEN
    """
    normalized_token = _clean_token(token)
    normalized_mode = str(mode or "false").strip().lower()
    if not normalized_token or normalized_mode == "false":
        return []

    # Original puts token RAW in the URL (no quote). Keep that for copy-paste
    # compatibility with the original checker. Also provide a browser-safe
    # encoded form: browsers treat bare "+" in query as space and break tokens.
    raw = normalized_token
    browser_safe = quote(raw, safe="")

    if normalized_mode == "pc":
        return [
            ("PC Login", f"https://netflix.com/?nftoken={raw}"),
            ("PC Login (browser-safe)", f"https://netflix.com/?nftoken={browser_safe}"),
        ]
    if normalized_mode == "mobile":
        return [
            ("Phone Login", f"https://netflix.com/unsupported?nftoken={raw}"),
            (
                "Phone Login (browser-safe)",
                f"https://netflix.com/unsupported?nftoken={browser_safe}",
            ),
        ]
    return [
        ("PC Login", f"https://netflix.com/?nftoken={raw}"),
        ("PC Login (browser-safe)", f"https://netflix.com/?nftoken={browser_safe}"),
        ("Phone Login", f"https://netflix.com/unsupported?nftoken={raw}"),
        (
            "Phone Login (browser-safe)",
            f"https://netflix.com/unsupported?nftoken={browser_safe}",
        ),
    ]
