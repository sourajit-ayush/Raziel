"""The saved Spotify login, shared by every background user of it (music card, thumbs-up, playlist names).

Why this exists: spotipy writes the auth manager's own `scope` into .spotify_cache every time it refreshes
the hourly token. The music card and the playlist-name refresh asked for fewer permissions than
play_music does, so after their first refresh the saved login "lost" permissions, and the next
"play X" opened the Spotify login page in the browser again (the log shows it on every play command).

Here the auth manager is always created with the scope the saved token already has, so a refresh
keeps it as it is. Nothing here ever opens a login page.
"""
import json
import logging
import os
from typing import Optional, Tuple

import config

logger = logging.getLogger(__name__)


def cache_path() -> str:
    return os.path.join(getattr(config, "_SCRIPT_DIR", "."), ".spotify_cache")


def saved_scope() -> str:
    """The permissions of the saved login ("" when there is none)."""
    try:
        with open(cache_path(), encoding="utf-8") as f:
            return str((json.load(f) or {}).get("scope") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def has_scope(needed: str) -> bool:
    have = set(saved_scope().split())
    return bool(have) and set((needed or "").split()) <= have


def configured() -> bool:
    cid = str(getattr(config, "SPOTIFY_CLIENT_ID", "") or "")
    return bool(cid) and "your" not in cid.lower() and os.path.isfile(cache_path())


def saved_login_token(needed: str = "") -> Tuple[Optional[dict], str]:
    """-> (token_info, "") or (None, why) with why in {"off", "need_permission", "expired"}.
    Refreshes an expired token without changing its permissions."""
    if not configured():
        return None, "off"
    scope = saved_scope()
    if needed and not has_scope(needed):
        return None, "need_permission"
    from spotipy.oauth2 import SpotifyOAuth
    auth = SpotifyOAuth(client_id=config.SPOTIFY_CLIENT_ID, client_secret=config.SPOTIFY_CLIENT_SECRET,
                        redirect_uri=config.SPOTIFY_REDIRECT_URI, scope=scope or needed or None,
                        cache_path=cache_path(), open_browser=False)
    token = auth.validate_token(auth.cache_handler.get_cached_token())
    if not token:
        return None, "expired"
    return token, ""


def saved_login_client(needed: str = "", timeout: float = 5.0):
    """A spotipy client from the saved login, or None. Never opens a login page."""
    token, _why = saved_login_token(needed)
    if not token:
        return None
    import spotipy
    return spotipy.Spotify(auth=token["access_token"], requests_timeout=timeout, retries=0)
