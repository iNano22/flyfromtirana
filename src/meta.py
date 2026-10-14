"""Posts to Instagram and the Facebook Page through Meta's Graph API.

One Page access token does everything: it belongs to the Facebook Page, and the
Instagram business account is linked to that Page. The token is made once in
Meta's Graph API Explorer (README: "Instagram and Facebook posts") and does not
expire. If Meta ever rejects it, every call raises MetaError, and the run tells
the owner on Telegram.

Instagram publishes in two steps: the picture's public address is given to
Instagram, which fetches it into a "container"; once that is ready, the
container is published. Facebook takes the address and the text in one call.
Docs: https://developers.facebook.com/docs/instagram-platform/content-publishing
      https://developers.facebook.com/docs/graph-api/reference/page/photos
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import requests

from src.http_client import request_with_retry

log = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com/v26.0/{path}"
# Instagram usually has a picture ready within a second or two.
CONTAINER_CHECKS = 20
CONTAINER_WAIT_SECONDS = 3
TOKEN_ERROR_CODE = 190   # Meta's code for an invalid or expired token


class MetaError(Exception):
    """Meta refused the call, or could not be reached."""


class MetaClient:
    def __init__(self, page_token: str, session: requests.Session | None = None,
                 sleep: Callable[[float], None] | None = None):
        self.token = page_token
        self.session = session or requests.Session()
        self.sleep = sleep or time.sleep   # tests pass a fake sleep so they don't really wait
        self._ids: dict | None = None      # whoami(), fetched once

    # --- who the token belongs to --------------------------------------------------

    def whoami(self) -> dict:
        """The Page and Instagram account behind the token:
        {"page_id", "page_name", "instagram_id", "instagram_username"} (the last two may be None)."""
        if self._ids is None:
            try:
                me = self._call("GET", "me", fields="id,name,instagram_business_account{id,username}")
            except MetaError as exc:
                # A person has no instagram_business_account field: the token belongs to a user, not the Page.
                if "instagram_business_account" in str(exc):
                    raise MetaError("META_PAGE_TOKEN is a User token, not the Page's token. In the Graph API "
                                    "Explorer, with the extended token in the box, run "
                                    "me/accounts?fields=name,access_token and copy the access_token shown "
                                    "next to the Page's name (README: Instagram and Facebook posts)") from None
                raise
            instagram = me.get("instagram_business_account") or {}
            self._ids = {"page_id": me["id"], "page_name": me.get("name", ""),
                         "instagram_id": instagram.get("id"), "instagram_username": instagram.get("username")}
        return self._ids

    def publishing_quota(self) -> dict:
        """How many of Instagram's 100 posts a day (through the API) are used: {"quota_usage": n, "config": {...}}."""
        data = self._call("GET", f"{self._instagram_id()}/content_publishing_limit", fields="quota_usage,config")
        return (data.get("data") or [{}])[0]

    # --- posting ----------------------------------------------------------------------

    def post_instagram(self, image_url: str, caption: str | None = None, *, story: bool = False) -> str | None:
        """Publish a picture on Instagram: a feed post with the caption, or a story.

        Returns the post's address (a story has none: returns None). The picture
        must be a JPEG at a public https address; Instagram fetches it itself.
        """
        instagram = self._instagram_id()
        fields = {"image_url": image_url}
        if story:
            fields["media_type"] = "STORIES"
        else:
            fields["caption"] = caption or ""
        container = self._call("POST", f"{instagram}/media", **fields)["id"]
        self._wait_for_container(container)
        media = self._call("POST", f"{instagram}/media_publish", creation_id=container)["id"]
        if story:
            log.info("Instagram story published (media %s)", media)
            return None
        permalink = self._call("GET", media, fields="permalink").get("permalink")
        log.info("Instagram post published: %s", permalink)
        return permalink

    def post_facebook(self, image_url: str, message: str) -> str:
        """Publish a picture with text on the Page. Returns the post's address."""
        page = self.whoami()["page_id"]
        result = self._call("POST", f"{page}/photos", url=image_url, message=message)
        post_id = result.get("post_id") or result["id"]
        address = f"https://www.facebook.com/{post_id}"
        log.info("Facebook post published: %s", address)
        return address

    def _wait_for_container(self, container: str) -> None:
        """Instagram fetches the picture in the background: wait until it says FINISHED."""
        for check in range(CONTAINER_CHECKS):
            status = self._call("GET", container, fields="status_code,status")
            code = status.get("status_code")
            if code == "FINISHED":
                return
            if code in ("ERROR", "EXPIRED"):
                raise MetaError(f"Instagram could not take the picture ({code}): {status.get('status', 'no details')}")
            self.sleep(CONTAINER_WAIT_SECONDS)
        raise MetaError(f"Instagram did not finish preparing the picture in {CONTAINER_CHECKS * CONTAINER_WAIT_SECONDS}s")

    def _instagram_id(self) -> str:
        instagram = self.whoami()["instagram_id"]
        if not instagram:
            raise MetaError(f"No Instagram account is linked to the Page {self.whoami()['page_name']!r}")
        return instagram

    # --- one Graph API call ----------------------------------------------------------

    def _call(self, method: str, path: str, **params: str) -> dict:
        """GET or POST graph.facebook.com/<path>. Returns the JSON answer, or raises MetaError.

        The token travels in a header, never in the address, so it is in no log.
        """
        url = GRAPH_URL.format(path=path)
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            if method == "GET":
                response = request_with_retry(self.session, "GET", url, params=params, headers=headers)
            else:
                response = request_with_retry(self.session, "POST", url, data=params, headers=headers)
        except requests.RequestException as exc:
            raise MetaError(f"Could not reach Meta ({type(exc).__name__})") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        error = body.get("error") if isinstance(body, dict) else None
        if response.status_code != 200 or error:
            error = error or {}
            text = f"Meta refused the call (HTTP {response.status_code}, code {error.get('code', '?')}): " \
                   f"{error.get('message', 'no details')}"
            if error.get("code") == TOKEN_ERROR_CODE:
                text += " — META_PAGE_TOKEN is no longer valid; make a new one (README: Instagram and Facebook posts)"
            raise MetaError(text)
        return body
