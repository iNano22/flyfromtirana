"""The city's video for its Reels: your own clip first, then the saved one, then a search on Pexels."""
import json

from src import place_video as module
from src.place_video import about, best_file, claude_judge, place_video


def pexels_video(url, *, duration=12, files=None, image="https://images/preview.jpg"):
    files = files or [{"file_type": "video/mp4", "width": 1080, "height": 1920, "link": "https://files/hd.mp4"}]
    return {"url": url, "duration": duration, "user": {"name": "Jane Doe"}, "video_files": files, "image": image}


class FakePexels:
    """Stands in for requests.Session: answers the search with `videos`, and any download with bytes."""

    def __init__(self, videos):
        self.videos, self.calls = videos, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url == module.SEARCH_URL:
            return FakeDownload({"videos": self.videos})
        return FakeDownload()


class FakeDownload:
    """An answer from Pexels: the search's JSON, or a file's bytes."""

    def __init__(self, body=None):
        self.body = body

    def json(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        return [b"video", b"bytes"]


def test_a_video_is_found_once_saved_and_then_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "OWN_VIDEOS_DIR", tmp_path / "own")
    session = FakePexels([pexels_video("https://www.pexels.com/video/a-street-in-paris-11/"),          # not Dublin
                          pexels_video("https://www.pexels.com/video/dublin-in-two-seconds-12/", duration=2),
                          pexels_video("https://www.pexels.com/video/the-river-liffey-in-dublin-13/")])
    cache = tmp_path / "cache"

    found = place_video("DUB", "Dublin", cache, api_key="key", session=session)
    assert found.path == cache / "dub.mp4" and found.path.read_bytes() == b"videobytes"
    assert found.credit == "Jane Doe / Pexels" and found.source_url.endswith("in-dublin-13/")
    url, kwargs = session.calls[0]
    assert kwargs["headers"] == {"Authorization": "key"} and kwargs["params"]["query"] == "Dublin"
    assert kwargs["params"]["orientation"] == "portrait"

    again = place_video("DUB", "Dublin", cache, api_key="key", session=session)   # from the folder: no new search
    assert again == found and len(session.calls) == 2

    # Your own clip in assets/reel_videos wins over the saved one, and needs no credit.
    (tmp_path / "own").mkdir()
    (tmp_path / "own" / "dub.mp4").write_bytes(b"mine")
    own = place_video("DUB", "Dublin", cache)
    assert own.path == tmp_path / "own" / "dub.mp4" and own.credit is None


def test_a_city_without_a_video_keeps_its_photo_and_is_not_searched_again_soon(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "OWN_VIDEOS_DIR", tmp_path / "own")
    session = FakePexels([pexels_video("https://www.pexels.com/video/a-street-in-paris-11/")])
    assert place_video("NRN", "Weeze", tmp_path, api_key="key", session=session) is None
    assert place_video("NRN", "Weeze", tmp_path, api_key="key", session=session) is None
    assert len(session.calls) == 1                                           # remembered: one search only
    assert place_video("FMM", "Memmingen", tmp_path, api_key="", session=session) is None   # no key: no search
    assert len(session.calls) == 1


def test_only_videos_about_the_city_and_upright_files_are_taken():
    assert about("https://www.pexels.com/video/aerial-view-of-tel-aviv-99/", "Tel Aviv")
    assert about("https://www.pexels.com/video/malmo-at-night-5/", "Malmö")
    assert not about("https://www.pexels.com/video/people-walking-in-a-city-7/", "Dublin")
    assert about("https://www.pexels.com/video/2499611/", "Dublin")           # no title in the address: taken as it comes
    files = [{"file_type": "video/mp4", "width": 2160, "height": 3840, "link": "4k"},
             {"file_type": "video/mp4", "width": 1080, "height": 1920, "link": "hd"},
             {"file_type": "video/mp4", "width": 540, "height": 960, "link": "small"},
             {"file_type": "video/mp4", "width": 1920, "height": 1080, "link": "wide"}]
    assert best_file(files) == "hd" and best_file(files[2:]) is None
    # A file that fills the Reel beats a smaller one that would be stretched.
    assert best_file([{"file_type": "video/mp4", "width": 720, "height": 1280, "link": "soft"}] + files[:2]) == "hd"
    assert best_file([{"file_type": "video/mp4", "width": 720, "height": 1280, "link": "soft"}]) == "soft"


# --- Claude picks the most beautiful of the videos found --------------------------------------

DUBLIN = [pexels_video(f"https://www.pexels.com/video/dublin-{number}/", image=f"https://images/{number}.jpg")
          for number in (1, 2, 3)]


def test_the_judge_picks_among_the_videos_found(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "OWN_VIDEOS_DIR", tmp_path / "own")
    asked = []

    def judge(city, previews):
        asked.append((city, previews))
        return 2   # the third one

    found = place_video("DUB", "Dublin", tmp_path, api_key="key", judge=judge, session=FakePexels(DUBLIN))
    assert asked == [("Dublin", ["https://images/1.jpg", "https://images/2.jpg", "https://images/3.jpg"])]
    assert found.source_url.endswith("dublin-3/")
    assert json.loads((tmp_path / "dub.json").read_text())["picked"] == module.PICKED_BY_CLAUDE
    # Picked once: the next Reel takes it from the folder.
    assert place_video("DUB", "Dublin", tmp_path, api_key="key", judge=judge, session=FakePexels(DUBLIN)) == found
    assert len(asked) == 1


def test_when_no_video_is_good_enough_the_reel_keeps_its_photo(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "OWN_VIDEOS_DIR", tmp_path / "own")
    session = FakePexels(DUBLIN)
    assert place_video("DUB", "Dublin", tmp_path, api_key="key", judge=lambda city, previews: None,
                       session=session) is None
    assert not (tmp_path / "dub.mp4").exists() and (tmp_path / "dub.none").exists()
    assert len(session.calls) == 1   # searched, nothing downloaded


def test_a_video_saved_before_the_judge_is_chosen_again_once(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "OWN_VIDEOS_DIR", tmp_path / "own")
    first = place_video("DUB", "Dublin", tmp_path, api_key="key", session=FakePexels(DUBLIN))   # no judge: the first
    assert first.source_url.endswith("dublin-1/")
    again = place_video("DUB", "Dublin", tmp_path, api_key="key", judge=lambda city, previews: 1,
                        session=FakePexels(DUBLIN))
    assert again.source_url.endswith("dublin-2/") and again.path.read_bytes() == b"videobytes"
    assert not (tmp_path / "dub.part").exists()

    # A search that fails leaves the video it had.
    class Broken(FakePexels):
        def get(self, url, **kwargs):
            raise module.requests.ConnectionError("down")
    (tmp_path / "dub.json").write_text(json.dumps({"credit": "x", "source_url": "old"}))
    kept = place_video("DUB", "Dublin", tmp_path, api_key="key", judge=lambda city, previews: 0, session=Broken([]))
    assert kept.source_url == "old" and kept.path.exists()


class FakeClaude:
    """Stands in for anthropic.Anthropic: answers every request with `text`, or raises `error`."""

    def __init__(self, text='{"best": 2, "why": "bright skyline"}', stop_reason="end_turn", error=None):
        self.text, self.stop_reason, self.error, self.requests = text, stop_reason, error, []
        self.messages = self

    def create(self, **request):
        self.requests.append(request)
        if self.error:
            raise self.error
        block = type("Block", (), {"type": "text", "text": self.text})()
        return type("Response", (), {"stop_reason": self.stop_reason, "content": [block]})()


def test_claude_is_shown_each_preview_and_answers_with_a_number():
    claude = FakeClaude()
    judge = claude_judge("key", client=claude)
    assert judge("Dublin", ["https://images/1.jpg", "https://images/2.jpg"]) == 1   # "best": 2 -> the second
    request, = claude.requests
    assert request["model"] == "claude-opus-5-5"
    content = request["messages"][0]["content"]
    assert [part["source"]["url"] for part in content if part["type"] == "image"] == [
        "https://images/1.jpg", "https://images/2.jpg"]
    assert "Dublin" in content[-1]["text"] and "answer 0" in content[-1]["text"]
    assert request["output_config"]["format"]["type"] == "json_schema"

    assert claude_judge("key", client=FakeClaude('{"best": 0, "why": "all dark"}'))("Dublin", ["a", "b"]) is None
    assert claude_judge("key", client=FakeClaude('{"best": 9, "why": "?"}'))("Dublin", ["a", "b"]) is None


def test_when_claude_cannot_be_asked_the_first_video_is_taken(caplog):
    refused = claude_judge("key", client=FakeClaude(stop_reason="refusal"))
    assert refused("Dublin", ["a", "b"]) == 0
    broken = claude_judge("key", client=FakeClaude(error=module.anthropic.APIConnectionError(request=None)))
    assert broken("Dublin", ["a", "b"]) == 0
    assert "taking the first" in caplog.text
