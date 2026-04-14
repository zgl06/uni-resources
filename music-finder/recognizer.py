import os
import shutil
import tempfile
from urllib.parse import quote

import httpx
from pydub import AudioSegment
from shazamio import Shazam

# Maximum number of audio chunks to sample per video
MAX_SAMPLES = 15
# Duration of each sample chunk in milliseconds
CHUNK_MS = 15_000


async def _get_spotify_url(title: str, artist: str) -> str:
    """
    Return a Spotify URL for the given song.

    If SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET env vars are set, queries
    the Spotify API to get the exact track URL. Otherwise falls back to a
    Spotify search URL that works without any credentials.
    """
    query = quote(f"{title} {artist}")
    fallback = f"https://open.spotify.com/search/{query}"

    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        return fallback

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token_resp = await client.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
            )
            token = token_resp.json().get("access_token")
            if not token:
                return fallback

            search_resp = await client.get(
                "https://api.spotify.com/v1/search",
                params={"q": f"{title} {artist}", "type": "track", "limit": 1},
                headers={"Authorization": f"Bearer {token}"},
            )
            items = search_resp.json().get("tracks", {}).get("items", [])
            if items:
                return items[0]["external_urls"]["spotify"]
    except Exception:
        pass

    return fallback


async def identify_songs(mp3_path: str) -> list[dict]:
    """
    Sample the audio file at regular intervals and identify any songs found.

    Sampling strategy:
      - Videos ≤ 5 min  → sample every 30 s
      - Videos > 5 min  → sample every 60 s
      - Hard cap of MAX_SAMPLES samples

    Deduplicates by (title, artist) and keeps the earliest timestamp.

    Returns a list of dicts:
      timestamp_s, title, artist, album, cover_url, spotify_url
    """
    audio = AudioSegment.from_mp3(mp3_path)
    duration_s = int(len(audio) / 1000)

    interval_s = 30 if duration_s <= 300 else 60
    timestamps = list(range(0, duration_s, interval_s))[:MAX_SAMPLES]

    shazam = Shazam()
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()
    tmp = tempfile.mkdtemp()

    try:
        for ts in timestamps:
            start_ms = ts * 1000
            end_ms = min(start_ms + CHUNK_MS, len(audio))
            chunk = audio[start_ms:end_ms]

            chunk_path = os.path.join(tmp, f"chunk_{ts}.mp3")
            chunk.export(chunk_path, format="mp3")

            try:
                out = await shazam.recognize(chunk_path)
            except Exception:
                continue

            if not out.get("matches"):
                continue

            track = out.get("track", {})
            title = track.get("title", "")
            artist = track.get("subtitle", "")
            if not title:
                continue

            key = (title.lower(), artist.lower())
            if key in seen:
                continue
            seen.add(key)

            # Extract album name from sections metadata
            album = ""
            for section in track.get("sections", []):
                for meta in section.get("metadata", []):
                    if meta.get("title") == "Album":
                        album = meta.get("text", "")
                        break
                if album:
                    break

            # Prefer high-res cover art
            images = track.get("images", {})
            cover_url = images.get("coverarthq") or images.get("coverart", "")

            spotify_url = await _get_spotify_url(title, artist)

            results.append(
                {
                    "timestamp_s": ts,
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "cover_url": cover_url,
                    "spotify_url": spotify_url,
                }
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return results
