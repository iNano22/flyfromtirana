import pytest
import requests

from src.telegram import TelegramClient, TelegramError
from tests.conftest import FakeResponse, FakeSession

TOKEN = "123:secret"


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
