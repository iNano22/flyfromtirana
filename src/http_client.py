"""One shared helper for HTTP calls: timeouts, retries and exponential backoff.

Both the Travelpayouts scanner and the Telegram client go through
request_with_retry(), so retry behaviour lives in exactly one place.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import requests

log = logging.getLogger(__name__)

# Worth retrying: rate limited (429) or a temporary server problem (5xx).
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_WAIT_SECONDS = 60


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    attempts: int = 4,
    backoff_seconds: float = 2.0,
    timeout: float = 30,
    sleep: Callable[[float], None] | None = None,
    **kwargs,
) -> requests.Response:
    """Send a request, retrying network errors, 429 and 5xx with exponential backoff.

    Waits 2s, 4s, 8s... between attempts, or as long as the server asks
    (Retry-After). Returns the last response, which may still be an error
    status: the caller decides what that means. Raises a
    requests.RequestException only if the network failed on every attempt.

    `url` is never logged, because the Telegram URL contains the bot token.
    """
    sleep = sleep or time.sleep  # tests pass a fake sleep so they don't really wait
    for attempt in range(1, attempts + 1):
        is_last = attempt == attempts
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if is_last:
                raise
            wait = backoff_seconds * 2 ** (attempt - 1)
            log.warning("Network error (%s), retrying in %.0fs (attempt %d/%d)",
                        type(exc).__name__, wait, attempt, attempts)
            sleep(wait)
            continue

        if response.status_code in RETRY_STATUSES and not is_last:
            wait = _server_requested_wait(response) or backoff_seconds * 2 ** (attempt - 1)
            wait = min(wait, MAX_WAIT_SECONDS)
            log.warning("HTTP %d, retrying in %.0fs (attempt %d/%d)",
                        response.status_code, wait, attempt, attempts)
            sleep(wait)
            continue
        return response

    raise AssertionError("unreachable: the loop always returns or raises")


def _server_requested_wait(response: requests.Response) -> float | None:
    """Seconds the server asked us to wait: Retry-After header, or Telegram's retry_after."""
    header = response.headers.get("Retry-After", "")
    if header.isdigit():
        return float(header)
    try:
        return float(response.json()["parameters"]["retry_after"])
    except (ValueError, KeyError, TypeError):
        return None
