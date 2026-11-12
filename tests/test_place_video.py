"""The city's video for its Reels: your own clip first, then the saved one, then a search on Pexels."""
import json

from src import place_video as module
from src.place_video import about, best_file, place_video


def pexels_video(url, *, duration=12, files=None):
    files = files or [{"file_type": "video/mp4", "width": 1080, "height": 1920, "link": "https://files/hd.mp4"}]
    return {"url": url, "duration": duration, "user": {"name": "Jane Doe"}, "video_files": files}


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
