import pytest
import requests

from src.telegram import CAPTION_LIMIT, TelegramClient, TelegramError, TelegramRefused, visible_length
from tests.conftest import FakeResponse, FakeSession

TOKEN = "123:secret"
OK = FakeResponse(200, {"ok": True, "result": {"message_id": 42}})


@pytest.fixture
def photo(tmp_path):
    path = tmp_path / "bgy.jpg"
    path.write_bytes(b"jpeg bytes")
    return path


def methods(session):
    """Which Bot API methods were called, in order: ["sendPhoto", "sendMessage"]."""
    return [call["url"].rsplit("/", 1)[1] for call in session.calls]


def test_send_message_payload():
    session = FakeSession(lambda m, u, kw: FakeResponse(200, {"ok": True, "result": {"message_id": 42}}))
    client = TelegramClient(TOKEN, "@flyfromtirana", session)

    assert client.send_message("<b>hi</b>") == 42

    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert call["json"]["chat_id"] == "@flyfromtirana"
    assert call["json"]["parse_mode"] == "HTML"
    assert call["json"]["link_preview_options"] == {"is_disabled": True}


def test_send_photo_uploads_the_file_with_the_text_as_caption(photo):
    session = FakeSession(lambda m, u, kw: OK)
    client = TelegramClient(TOKEN, "@flyfromtirana", session)

    assert client.send_post("<b>hi</b>", photo) == 42

    call = session.calls[0]
    assert call["url"] == f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    assert call["data"] == {"chat_id": "@flyfromtirana", "caption": "<b>hi</b>", "parse_mode": "HTML"}
    assert call["files"]["photo"] == ("bgy.jpg", b"jpeg bytes", "image/jpeg")
    assert methods(session) == ["sendPhoto"]


def test_post_without_a_photo_is_a_plain_message():
    session = FakeSession(lambda m, u, kw: OK)
    assert TelegramClient(TOKEN, "@flyfromtirana", session).send_post("hi") == 42
    assert methods(session) == ["sendMessage"]


def test_refused_photo_falls_back_to_the_text(photo):
    def handler(method, url, kwargs):
        if url.endswith("/sendPhoto"):
            return FakeResponse(400, {"ok": False, "description": "Bad Request: IMAGE_PROCESS_FAILED"})
        return OK

    session = FakeSession(handler)
    assert TelegramClient(TOKEN, "@flyfromtirana", session).send_post("hi", photo) == 42
    assert methods(session) == ["sendPhoto", "sendMessage"]
    assert session.calls[1]["json"]["text"] == "hi"


def test_text_too_long_for_a_caption_goes_without_the_photo(photo):
    session = FakeSession(lambda m, u, kw: OK)
    TelegramClient(TOKEN, "@flyfromtirana", session).send_post("x" * (CAPTION_LIMIT + 1), photo)
    assert methods(session) == ["sendMessage"]


def test_network_error_on_the_photo_does_not_post_the_text_too(photo, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)  # skip real retry waits

    def handler(method, url, kwargs):
        raise requests.ConnectionError("no answer")

    session = FakeSession(handler)
    with pytest.raises(TelegramError) as excinfo:
        TelegramClient(TOKEN, "@flyfromtirana", session).send_post("hi", photo)
    assert not isinstance(excinfo.value, TelegramRefused)
    assert set(methods(session)) == {"sendPhoto"}          # retried, but never sent as text


def test_visible_length_ignores_tags_and_counts_emoji_as_two():
    assert visible_length('<b>nga €14</b> <a href="https://example.com/a?b=1&amp;c=2">Rezervo</a>') == 15
    assert visible_length("A &amp; B") == 5
    assert visible_length("✈️") == 2        # plane + variation selector
    assert visible_length("🔥") == 2        # one emoji outside the basic plane


def test_telegram_refusal_raises_with_description():
    body = {"ok": False, "error_code": 403, "description": "Forbidden: bot is not a member of the channel chat"}
    client = TelegramClient(TOKEN, "@flyfromtirana", FakeSession(lambda m, u, kw: FakeResponse(403, body)))
    with pytest.raises(TelegramError, match="not a member") as excinfo:
        client.send_message("hi")
    assert TOKEN not in str(excinfo.value)


def test_network_error_does_not_leak_token(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)  # skip real retry waits

    def handler(method, url, kwargs):
        raise requests.ConnectionError(f"failed to reach {url}")

    client = TelegramClient(TOKEN, "@flyfromtirana", FakeSession(handler))
    with pytest.raises(TelegramError) as excinfo:
        client.send_message("hi")
    assert TOKEN not in str(excinfo.value)
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__
