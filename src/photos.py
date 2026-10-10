"""The destination photos: where they live, and which one goes on a Telegram post.

assets/img/dest/<iata>.webp     the website's photo cards (src/website.py)
assets/telegram/<iata>.jpg      the same photo as a JPEG, sent with the route's posts
assets/img/credits.json         author, source and licence of every photo

Telegram gets a JPEG because that is the one format its "send photo" method
is sure to accept (a WebP can end up as a sticker). A route without a JPEG
simply gets a post without a photo.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from src.config import PROJECT_ROOT

ASSETS_DIR = PROJECT_ROOT / "assets"
CREDITS_FILE = ASSETS_DIR / "img" / "credits.json"
POST_PHOTOS_DIR = ASSETS_DIR / "telegram"

# Licences that ask for no credit (the Unsplash License says so in as many
# words). Every other one (CC BY, CC BY-SA) does, so the post names the
# author and the licence under the photo.
NO_CREDIT_NEEDED = ("cc0", "public domain", "pd", "unsplash")


def needs_credit(licence: str) -> bool:
    """Does this licence require naming the photo's author? CC BY and CC BY-SA do."""
    return not licence.lower().startswith(NO_CREDIT_NEEDED)


@dataclass(frozen=True)
class PostPhoto:
    path: Path                # the JPEG to upload
    credit: str | None        # "Jane Doe, CC BY 2.0", or None when the licence asks for none
    source_url: str | None    # where the photo comes from: the credit links to it


def post_photo(iata: str) -> PostPhoto | None:
    """The photo for a route's posts, or None when it has no assets/telegram/<iata>.jpg."""
    path = POST_PHOTOS_DIR / f"{iata.lower()}.jpg"
    if not path.exists():
        return None
    entry = _credit_entry(iata)
    if entry is None or not needs_credit(entry["license"]):
        return PostPhoto(path=path, credit=None, source_url=None)
    return PostPhoto(path=path, credit=f'{entry["creator"]}, {entry["license"]}',
                     source_url=entry.get("source_url") or entry.get("license_url") or None)


def _credit_entry(iata: str) -> dict | None:
    """The route's entry in credits.json (its "slot" is the airport code), if there is one."""
    if not CREDITS_FILE.exists():
        return None
    for entry in json.loads(CREDITS_FILE.read_text(encoding="utf-8")):
        if entry.get("slot") == iata.upper():
            return entry
    return None
