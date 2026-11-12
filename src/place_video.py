"""A short video of a city, for the background of its Reels (src/reels.py).

Where a city's video comes from, in this order:

  1. assets/reel_videos/<iata>.mp4   your own clip, committed to the repository. It always wins.
  2. data/reel_videos/<iata>.mp4     a video this module found on pexels.com earlier (the server
                                     keeps data/ between deploys), with <iata>.json next to it:
                                     who filmed it and its page on Pexels.
  3. pexels.com                      searched by the city's English name, upright videos only
                                     (needs PEXELS_API_KEY, free: pexels.com/api). The first
                                     result that fits is downloaded into data/reel_videos/.

A city with no video gets None, and its Reel shows the photo as before. A city whose search
found nothing is remembered (<iata>.none) and not searched again for RETRY_DAYS.

Pexels' licence allows free use without naming the author; the caption names them anyway
("🎥 Video: Jane Doe / Pexels"), as Pexels asks of programs that use its API.
"""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import requests

from src.photos import ASSETS_DIR

log = logging.getLogger(__name__)

OWN_VIDEOS_DIR = ASSETS_DIR / "reel_videos"
SEARCH_URL = "https://api.pexels.com/v1/videos/search"
MIN_SECONDS = 6            # shorter clips are skipped (a Reel shows the city for about 7 seconds)
MAX_SECONDS = 60           # and so are long films: a big download for 7 seconds of it
MIN_HEIGHT = 1280          # the smallest picture worth stretching to a Reel's 1920
MAX_BYTES = 80 * 1024 * 1024
RETRY_DAYS = 30            # a city without a video is searched again after this long


@dataclass(frozen=True)
class PlaceVideo:
    path: Path                # the MP4 on disk
    credit: str | None        # "Jane Doe / Pexels", or None for your own clip
    source_url: str | None    # the video's page on Pexels


def place_video(iata: str, city: str, cache: Path, *, api_key: str = "",
                session: requests.Session | None = None) -> PlaceVideo | None:
    """The video for a city's Reels, or None. city: its English name ("Milan"), the search words.
    cache: the folder for downloaded videos (data/reel_videos). Never raises: a Reel without a
    video is still a Reel."""
    name = iata.lower()
    own = OWN_VIDEOS_DIR / f"{name}.mp4"
    if own.exists():
        return PlaceVideo(own, None, None)
    saved, details = cache / f"{name}.mp4", cache / f"{name}.json"
    if saved.exists():
        info = json.loads(details.read_text(encoding="utf-8")) if details.exists() else {}
        return PlaceVideo(saved, info.get("credit"), info.get("source_url"))
    if not api_key:
        return None
    nothing = cache / f"{name}.none"
    if nothing.exists() and time.time() - nothing.stat().st_mtime < RETRY_DAYS * 86400:
        return None
    try:
        cache.mkdir(parents=True, exist_ok=True)
        found = search(city, api_key, session or requests.Session())
        if found is None:
            nothing.write_text("no video found on Pexels\n", encoding="utf-8")
            log.info("[reel] No video of %s on Pexels: its Reels keep the photo", city)
            return None
        link, credit, source_url = found
        download(link, saved, session or requests.Session())
        details.write_text(json.dumps({"credit": credit, "source_url": source_url}, ensure_ascii=False),
                           encoding="utf-8")
        log.info("[reel] Saved a video of %s for its Reels: %s (%s)", city, source_url, credit)
        return PlaceVideo(saved, credit, source_url)
    except (requests.RequestException, OSError, ValueError, KeyError) as exc:
        log.warning("[reel] Could not get a video of %s (%s): its Reel keeps the photo", city, type(exc).__name__)
        saved.unlink(missing_ok=True)   # half a download is no video
        return None


def search(city: str, api_key: str, session: requests.Session) -> tuple[str, str, str] | None:
    """Ask Pexels for upright videos of the city: (the file's link, the credit, the video's page)
    of the first one that fits, or None."""
    response = session.get(SEARCH_URL, headers={"Authorization": api_key}, timeout=30,
                           params={"query": city, "orientation": "portrait", "size": "medium", "per_page": 30})
    response.raise_for_status()
    for video in response.json().get("videos") or []:
        if not MIN_SECONDS <= (video.get("duration") or 0) <= MAX_SECONDS:
            continue
        if not about(video.get("url") or "", city):
            continue
        link = best_file(video.get("video_files") or [])
        if link:
            author = (video.get("user") or {}).get("name") or "Pexels"
            return link, f"{author} / Pexels", video.get("url") or ""
    return None


def about(page_url: str, city: str) -> bool:
    """Whether a video's page is about the city. Pexels answers a search for a small town with
    whatever is nearest, and a Reel of Weeze must not show Paris. A page's address usually has
    the video's title in it (".../video/aerial-view-of-dublin-city-123/"): then the city's name
    must be there. An address with only a number can't be checked, and is taken as it comes."""
    title = re.sub(r"[^a-z]+", " ", page_url.lower().rsplit("/video/", 1)[-1]).strip()
    if not title:
        return True
    plain = unicodedata.normalize("NFKD", city.lower()).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z]+", " ", plain).split()
    return bool(words) and all(word in title.split() for word in words)


def best_file(files: list[dict]) -> str | None:
    """Of a video's versions, the link of the smallest upright MP4 that is tall enough."""
    fitting = [f for f in files
               if f.get("file_type") == "video/mp4" and f.get("link")
               and (f.get("height") or 0) >= MIN_HEIGHT and (f.get("height") or 0) > (f.get("width") or 0)]
    if not fitting:
        return None
    return min(fitting, key=lambda f: f["height"])["link"]


def download(link: str, path: Path, session: requests.Session) -> None:
    """Save the file, giving up (ValueError) on one bigger than MAX_BYTES."""
    with session.get(link, stream=True, timeout=120) as response:
        response.raise_for_status()
        size = 0
        with path.open("wb") as out:
            for chunk in response.iter_content(chunk_size=1 << 20):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("the video is too big")
                out.write(chunk)
