"""
netutil.py - the one place Raziel makes plain HTTP requests (news, weather, Gemini, Google).

Standard library only (urllib), so nothing extra has to be installed. Every function
raises NetError on failure, with .status (HTTP status or 0) and .body (response text, useful
for API error messages), so callers can turn a failure into one spoken sentence.
Tests replace these functions; nothing in the test-suite touches the network.
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional

logger = logging.getLogger("voice_assistant")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36 Raziel/1.0")
DEFAULT_TIMEOUT = 8.0


class NetError(Exception):
    def __init__(self, message: str, status: int = 0, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def _build_url(url: str, params: Optional[Dict[str, object]]) -> str:
    if not params:
        return url
    clean = {k: v for k, v in params.items() if v is not None}
    sep = "&" if "?" in url else "?"
    return url + sep + urllib.parse.urlencode(clean, doseq=True, quote_via=urllib.parse.quote)


def _request(req: urllib.request.Request, timeout: float, retries: int) -> str:
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            last = NetError(f"HTTP {e.code} from {req.full_url.split('?')[0]}", status=e.code, body=body)
            if e.code not in (429, 500, 502, 503, 504) or attempt >= retries:
                raise last from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            last = NetError(f"network error: {e}")
            if attempt >= retries:
                raise last from None
        time.sleep(0.6 * (attempt + 1))
    raise last or NetError("request failed")


def get_text(url: str, params: Optional[Dict[str, object]] = None, headers: Optional[Dict[str, str]] = None,
             timeout: float = DEFAULT_TIMEOUT, retries: int = 1) -> str:
    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"}
    hdrs.update(headers or {})
    req = urllib.request.Request(_build_url(url, params), headers=hdrs, method="GET")
    return _request(req, timeout, retries)


def get_json(url: str, params: Optional[Dict[str, object]] = None, headers: Optional[Dict[str, str]] = None,
             timeout: float = DEFAULT_TIMEOUT, retries: int = 1):
    text = get_text(url, params, headers, timeout, retries)
    try:
        return json.loads(text)
    except ValueError as e:
        raise NetError(f"not JSON from {url.split('?')[0]}: {e}", body=text[:300]) from None


def post_json(url: str, payload: dict, headers: Optional[Dict[str, str]] = None,
              timeout: float = 25.0, retries: int = 0, params: Optional[Dict[str, object]] = None):
    hdrs = {"User-Agent": USER_AGENT, "Content-Type": "application/json", "Accept": "application/json",
            "Accept-Encoding": "identity"}
    hdrs.update(headers or {})
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(_build_url(url, params), data=data, headers=hdrs, method="POST")
    text = _request(req, timeout, retries)
    try:
        return json.loads(text)
    except ValueError as e:
        raise NetError(f"not JSON from {url.split('?')[0]}: {e}", body=text[:300]) from None


def post_form(url: str, data: Dict[str, str], headers: Optional[Dict[str, str]] = None,
              timeout: float = 15.0, retries: int = 0):
    hdrs = {"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json", "Accept-Encoding": "identity"}
    hdrs.update(headers or {})
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    text = _request(req, timeout, retries)
    try:
        return json.loads(text)
    except ValueError as e:
        raise NetError(f"not JSON from {url.split('?')[0]}: {e}", body=text[:300]) from None
