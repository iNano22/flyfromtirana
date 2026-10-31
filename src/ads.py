"""Paid ads on Facebook and Instagram (Meta Ads Manager): the links they point to.

The ads themselves are made by hand in Ads Manager; scripts/ad_kit.py draws their
pictures (src/ad_cards.py) and writes the texts and links to paste there.

Every ad link carries UTM tags (?utm_source=meta&utm_medium=paid&utm_campaign=...&utm_content=...),
so Google Analytics shows the ad visitors apart from everyone else. The site also
looks for utm_source=meta: for those visitors (for the rest of their visit) its
Telegram buttons use the ads' own invite link (invite_links.meta_ads in config.yaml)
instead of the website's, so the weekly report counts the members the ads brought
(templates/site_ads.html).
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

import yaml

from src.config import PROJECT_ROOT, Config, ConfigError

ADS_FILE = PROJECT_ROOT / "content" / "ads.yaml"
UTM_SOURCE = "meta"       # the site's switch to the ads' invite link looks for this exact value
UTM_MEDIUM = "paid"


def ad_url(config: Config, *, campaign: str, content: str, page: str = "", pick: str = "") -> str:
    """The site's address with the ad's UTM tags: page "" is the main page, "milan" a city page,
    "en/" the English main page. campaign names the Ads Manager campaign, content the ad.

    pick is an airport code ("MXP"): the main page then puts that route at the top of its
    board and marks it in the list, so an ad about one deal lands on that deal (the page's
    script reads ?pick=; a code the page doesn't have is ignored)."""
    base = config.website.url.rstrip("/") + "/"
    page = page.strip("/")
    tags = {"utm_source": UTM_SOURCE, "utm_medium": UTM_MEDIUM, "utm_campaign": campaign, "utm_content": content}
    if pick:
        tags["pick"] = pick.upper()
    return f"{base}{page + '/' if page else ''}?{urlencode(tags)}"


def load_ads(path: Path = ADS_FILE) -> dict:
    """content/ads.yaml: {"campaign": "...", "ads": [{name, card, ...}, ...]}."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    ads = list(data.get("ads") or [])
    if not ads:
        raise ConfigError(f"{path.name} has no ads")
    names = [str(ad.get("name") or "") for ad in ads]
    if "" in names or len(set(names)) != len(names):
        raise ConfigError(f"{path.name}: every ad needs its own name (it becomes utm_content and the file name)")
    return {"campaign": str(data.get("campaign") or "launch"), "ads": ads}
