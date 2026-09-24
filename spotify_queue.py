"""
spotify_queue.py - "add X to the queue" for Raziel.

WHY THIS EXISTS
---------------
The tool list had play_music / play_playlist / pause / resume / next / previous
and NO queue tool, so "add Kesariya to the queue" was routed by the (flaky,
qwen3:8b) tool-picker to play_music - which starts the song immediately and
throws away whatever was playing.

Following the project's own rule (handoff section 5: deterministic matching
BEFORE the LLM for anything the model keeps getting wrong), this module has two
pure pieces and no state:

    parse_queue_request(transcript) -> query | None     the regex matcher
    queue_song(sp, query, device_id) -> spoken reply    the Spotify call

tools.py wires them in: queue_music() (also exposed to the LLM as a tool, so
oddly-phrased requests still work) and try_auto_queue_music() (the matcher that
main.py runs before the LLM).

Uses sp.add_to_queue(), which needs the `user-modify-playback-state` scope -
already requested by tools._get_spotify_client(), so no re-login is needed.
Spotify returns null entries in search results (known upstream bug), so results
are filtered exactly as play_music does.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("voice_assistant")

# Same filler stripping as the other deterministic matchers in tools.py.
_FILLER_RE = re.compile(
    r"^(?:okay|ok|so|alright|well|umm?|uhh?|hey|please|now|actually|"
    r"can you|could you|would you|will you)[,.]?\s+",
    re.IGNORECASE,
)

_SPOTIFY_TAIL = r"(?:\s+(?:on|in|with)\s+spotify)?"

_ADD_TO_QUEUE_RE = re.compile(
    r"^(?:add|put|throw|stick|drop)\s+(?P<q>.+?)\s+(?:to|in|into|onto|on)\s+"
    r"(?:the\s+|my\s+|our\s+)?(?:spotify\s+)?(?:play\s*)?queue" + _SPOTIFY_TAIL + r"(?:\s+please)?$",
    re.IGNORECASE,
)
_QUEUE_UP_RE = re.compile(
    r"^queue(?:\s+up)?\s+(?P<q>.+?)" + _SPOTIFY_TAIL + r"(?:\s+please)?$",
    re.IGNORECASE,
)

# "queue is empty", "queue length" etc. are questions ABOUT the queue, not requests.
_NOT_A_SONG_RE = re.compile(
    r"^(?:is|are|was|has|have|does|do|length|size|status|empty|full|"
    r"list|position|next|now)\b",
    re.IGNORECASE,
)
_LEADING_NOISE_RE = re.compile(r"^(?:the\s+)?(?:song|track|tune|music)\s+", re.IGNORECASE)
_VAGUE = {"it", "this", "that", "this song", "that song", "this track", "something",
          "a song", "some music", "music", "songs"}


def _clean_query(q: str) -> str:
    q = q.strip().strip("\"'.,!?")
    q = _LEADING_NOISE_RE.sub("", q)
    # "Kesariya by Arijit Singh" -> "Kesariya Arijit Singh": Spotify's free-text
    # search ranks title+artist words together far better than the word "by".
    q = re.sub(r"\s+by\s+", " ", q, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", q).strip()


def parse_queue_request(transcript: str) -> Optional[str]:
    """
    If the transcript is a request to queue a song, return the search query;
    otherwise None (so the caller falls through to the normal routing).

    Deliberately narrow: it must say "queue" and it must name something.
    """
    if not transcript:
        return None
    # Whisper garbling guard, same as try_auto_recall_favorite: drop anything
    # after a '?' - a question about the queue is not a request to add to it.
    if "?" in transcript:
        return None

    text = transcript.strip().rstrip(".!")
    for _ in range(3):
        stripped = _FILLER_RE.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped

    m = _ADD_TO_QUEUE_RE.match(text) or _QUEUE_UP_RE.match(text)
    if not m:
        return None

    q = _clean_query(m.group("q"))
    if not q or _NOT_A_SONG_RE.match(q):
        return None
    return q


def queue_song(sp, query: str, device_id: Optional[str] = None) -> str:
    """Search Spotify for `query` and append the top track to the queue."""
    query = (query or "").strip()
    if not query or query.lower() in _VAGUE:
        return "Specify which song to add to the queue."

    try:
        results = sp.search(q=query, type="track", limit=5)
    except Exception as e:
        logger.error("Spotify search failed while queueing %r: %s", query, e)
        return f"I couldn't search Spotify: {e}"

    raw_items = ((results or {}).get("tracks") or {}).get("items") or []
    items = [i for i in raw_items if i]      # null entries: known Spotify API quirk
    if not items:
        return f"I couldn't find a song matching '{query}' on Spotify."

    track = items[0]
    try:
        if device_id:
            sp.add_to_queue(track["uri"], device_id=device_id)
        else:
            sp.add_to_queue(track["uri"])
    except Exception as e:
        status = getattr(e, "http_status", None)
        logger.error("Spotify add_to_queue failed (http %s): %s", status, e)
        if status == 404:
            return "Nothing is playing on Spotify right now, so there is no queue. Start a song first."
        if status == 403:
            return "Spotify refused the request. Queue control needs a Premium account."
        return f"I couldn't add that to the queue: {e}"

    artists = track.get("artists") or []
    artist = artists[0].get("name", "") if artists else ""
    by = f" by {artist}" if artist else ""
    return f"Added {track.get('name', 'that track')}{by} to your queue."
