"""A short video of a city, for the background of its Reels (src/reels.py).

Where a city's video comes from, in this order:

  1. assets/reel_videos/<iata>.mp4   your own clip, committed to the repository. It always wins.
  2. data/reel_videos/<iata>.mp4     a video this module found on pexels.com earlier (the server
                                     keeps data/ between deploys), with <iata>.json next to it:
                                     who filmed it and its page on Pexels.
  3. pexels.com                      searched by the city's English name, upright videos only
                                     (needs PEXELS_API_KEY, free: pexels.com/api). Of the first
                                     results that fit, Claude looks at each one's preview picture
                                     and picks the most beautiful (needs ANTHROPIC_API_KEY; without
                                     it, the first one). It is downloaded into data/reel_videos/.

A city with no video gets None, and its Reel shows the photo as before. A city whose search
found nothing, or nothing Claude found good enough, is remembered (<iata>.none) and not searched
again for RETRY_DAYS. A video saved before Claude did the picking is chosen again, once.

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
from typing import Callable

import anthropic
import requests

from src.photos import ASSETS_DIR

log = logging.getLogger(__name__)

OWN_VIDEOS_DIR = ASSETS_DIR / "reel_videos"
SEARCH_URL = "https://api.pexels.com/v1/videos/search"
MIN_SECONDS = 6            # shorter clips are skipped (a Reel shows the city for 11 seconds; a shorter clip plays again)
MAX_SECONDS = 60           # and so are long films: a big download for 11 seconds of it
MIN_HEIGHT = 1280          # the smallest picture worth stretching to a Reel's 1920
MAX_BYTES = 80 * 1024 * 1024
RETRY_DAYS = 30            # a city without a video is searched again after this long
SHARP_HEIGHT = 1920        # a file this tall fills a Reel without stretching: taken when there is one
MAX_CANDIDATES = 8         # how many of the search's results Claude looks at
JUDGE_MODEL = "claude-opus-5-5"   # the Claude model that picks the video (one small request per city)
PICKED_BY_CLAUDE = 2       # written into <iata>.json: videos saved without it are chosen again, once

JUDGE_PROMPT = """\
These are the preview pictures of {count} stock videos found for "{city}", numbered in the order shown.
One of them will play as the background of a short Instagram travel video that advertises cheap flights to {city}.
White and yellow text covers the lower half of the frame, so what matters must be in the upper half.

Pick the one that makes people want to go there:
- bright daylight or golden hour, clear weather, strong colours;
- a place people recognise as {city}: a landmark, the skyline, the old town, the waterfront;
- a wide, calm view (aerial and establishing shots are best).

Reject videos that are dark, at night, grey, rainy or foggy; close-ups of people or faces; interiors, food,
traffic or objects that could be anywhere; and anything with text, logos or watermarks.

Answer with the number of the best one. If none of them is good enough to advertise the city, answer 0:
the still photo we already have is then used instead, and that is better than an ugly video."""


@dataclass(frozen=True)
class PlaceVideo:
    path: Path                # the MP4 on disk
    credit: str | None        # "Jane Doe / Pexels", or None for your own clip
    source_url: str | None    # the video's page on Pexels


@dataclass(frozen=True)
class Candidate:
    """One video the search found that fits a Reel."""

    link: str          # the MP4 to download
    credit: str        # "Jane Doe / Pexels"
    source_url: str    # the video's page on Pexels
    preview: str       # a still picture of it (what Claude looks at); "" when Pexels gave none


# Given the city's name and the candidates' preview pictures, the number of the best one
# (0 = the first), or None when none is good enough.
Judge = Callable[[str, list[str]], "int | None"]


def place_video(iata: str, city: str, cache: Path, *, api_key: str = "", judge: Judge | None = None,
                session: requests.Session | None = None) -> PlaceVideo | None:
    """The video for a city's Reels, or None. city: its English name ("Milan"), the search words.
    cache: the folder for downloaded videos (data/reel_videos). judge: picks among the videos
    found (claude_judge); None = the first one. Never raises: a Reel without a video is still a Reel."""
    name = iata.lower()
    own = OWN_VIDEOS_DIR / f"{name}.mp4"
    if own.exists():
        return PlaceVideo(own, None, None)
    saved, details = cache / f"{name}.mp4", cache / f"{name}.json"
    kept = None
    if saved.exists():
        info = json.loads(details.read_text(encoding="utf-8")) if details.exists() else {}
        kept = PlaceVideo(saved, info.get("credit"), info.get("source_url"))
        if not (api_key and judge) or info.get("picked") == PICKED_BY_CLAUDE:
            return kept
        # Saved before Claude did the picking: choose again, once (below).
    if not api_key:
        return None
    nothing = cache / f"{name}.none"
    if nothing.exists() and time.time() - nothing.stat().st_mtime < RETRY_DAYS * 86400:
        return None
    session = session or requests.Session()
    try:
        cache.mkdir(parents=True, exist_ok=True)
        candidates = search(city, api_key, session)
        choice = choose(city, candidates, judge) if candidates else None
        if choice is None:
            why = "none of the videos is good enough" if candidates else "no video found on Pexels"
            nothing.write_text(why + "\n", encoding="utf-8")
            saved.unlink(missing_ok=True)
            details.unlink(missing_ok=True)
            log.info("[reel] %s for %s: its Reels keep the photo", why.capitalize(), city)
            return None
        picked = {"picked": PICKED_BY_CLAUDE} if judge else {}
        if kept is None or choice.source_url != kept.source_url:
            fresh = saved.with_suffix(".part")
            download(choice.link, fresh, session)
            fresh.replace(saved)   # the old video stays until the new one is whole
        details.write_text(json.dumps({"credit": choice.credit, "source_url": choice.source_url} | picked,
                                      ensure_ascii=False), encoding="utf-8")
        log.info("[reel] Saved a video of %s for its Reels: %s (%s)", city, choice.source_url, choice.credit)
        return PlaceVideo(saved, choice.credit, choice.source_url)
    except (requests.RequestException, OSError, ValueError, KeyError) as exc:
        log.warning("[reel] Could not get a video of %s (%s): its Reel keeps %s", city, type(exc).__name__,
                    "the video it had" if kept else "the photo")
        saved.with_suffix(".part").unlink(missing_ok=True)   # half a download is no video
        return kept


def search(city: str, api_key: str, session: requests.Session) -> list[Candidate]:
    """Ask Pexels for upright videos of the city: the first MAX_CANDIDATES that fit, in Pexels' order."""
    response = session.get(SEARCH_URL, headers={"Authorization": api_key}, timeout=30,
                           params={"query": city, "orientation": "portrait", "size": "medium", "per_page": 40})
    response.raise_for_status()
    found: list[Candidate] = []
    for video in response.json().get("videos") or []:
        if not MIN_SECONDS <= (video.get("duration") or 0) <= MAX_SECONDS:
            continue
        if not about(video.get("url") or "", city):
            continue
        link = best_file(video.get("video_files") or [])
        if link:
            author = (video.get("user") or {}).get("name") or "Pexels"
            found.append(Candidate(link, f"{author} / Pexels", video.get("url") or "", video.get("image") or ""))
            if len(found) == MAX_CANDIDATES:
                break
    return found


def choose(city: str, candidates: list[Candidate], judge: Judge | None) -> Candidate | None:
    """The candidate to use: the judge's pick among those with a preview picture, or None when it
    finds none good enough. Without a judge (or without any preview), the first one."""
    shown = [candidate for candidate in candidates if candidate.preview]
    if judge is None or not shown:
        return candidates[0]
    number = judge(city, [candidate.preview for candidate in shown])
    return None if number is None else shown[number]


def claude_judge(api_key: str, *, client: anthropic.Anthropic | None = None) -> Judge:
    """A judge that asks Claude to look at the preview pictures and pick the most beautiful one.
    If Claude can't be asked (no credit, a refusal, a network problem), the first video is taken,
    as it was before there was a judge."""
    client = client or anthropic.Anthropic(api_key=api_key)

    def judge(city: str, previews: list[str]) -> int | None:
        content: list[dict] = []
        for number, url in enumerate(previews, start=1):
            content.append({"type": "text", "text": f"Video {number}:"})
            content.append({"type": "image", "source": {"type": "url", "url": url}})
        content.append({"type": "text", "text": JUDGE_PROMPT.format(count=len(previews), city=city)})
        try:
            response = client.messages.create(
                model=JUDGE_MODEL, max_tokens=4000, messages=[{"role": "user", "content": content}],
                # The answer comes back as JSON of exactly this shape.
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": {
                    "type": "object",
                    "properties": {"best": {"type": "integer"}, "why": {"type": "string"}},
                    "required": ["best", "why"], "additionalProperties": False}}})
            if response.stop_reason != "end_turn":
                raise ValueError(f"Claude stopped with {response.stop_reason}")
            answer = json.loads(next(block.text for block in response.content if block.type == "text"))
            best = int(answer["best"])
        except (anthropic.APIError, ValueError, KeyError, StopIteration) as exc:
            log.warning("[reel] Claude could not pick a video of %s (%s): taking the first", city, type(exc).__name__)
            return 0
        log.info("[reel] Claude's pick for %s: video %d of %d (%s)", city, best, len(previews), answer.get("why"))
        return best - 1 if 1 <= best <= len(previews) else None

    return judge


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
    """Of a video's versions, the link of the smallest upright MP4 that fills a Reel (1920 tall);
    without one, the smallest that is tall enough to stretch."""
    fitting = [f for f in files
               if f.get("file_type") == "video/mp4" and f.get("link")
               and (f.get("height") or 0) >= MIN_HEIGHT and (f.get("height") or 0) > (f.get("width") or 0)]
    if not fitting:
        return None
    sharp = [f for f in fitting if f["height"] >= SHARP_HEIGHT]   # full size when there is one
    return min(sharp or fitting, key=lambda f: f["height"])["link"]


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
