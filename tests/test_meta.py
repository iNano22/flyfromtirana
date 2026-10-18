"""The Meta Graph API client: whose token it is, the two-step Instagram publish, the Page post, errors."""
import pytest

from src.meta import GRAPH_URL, MetaClient, MetaError
from tests.conftest import FakeResponse, FakeSession

TOKEN = "page-token-secret"
ME = {"id": "1348603305007068", "name": "flyfromtirana.al",
      "instagram_business_account": {"id": "17841417241858036", "username": "flyfromtirana"}}


def graph_handler(*, container_checks: int = 1, container_status: str = "FINISHED", facebook_ok: bool = True):
    """A fake graph.facebook.com: answers the calls the client makes, in order."""
    checks = iter(range(container_checks - 1))   # IN_PROGRESS answers before the final status

    def handler(method, url, kwargs):
        path = url.replace(GRAPH_URL.format(path=""), "")
        if path == "me":
            return FakeResponse(200, ME)
        if path.endswith("/media"):
            return FakeResponse(200, {"id": "CONTAINER1"})
        if path == "CONTAINER1":
            last = next(checks, None) is None
            return FakeResponse(200, {"status_code": container_status if last else "IN_PROGRESS", "status": "x"})
        if path.endswith("/media_publish"):
            return FakeResponse(200, {"id": "MEDIA1"})
        if path == "MEDIA1":
            return FakeResponse(200, {"permalink": "https://www.instagram.com/p/abc123/"})
        if path.endswith("/photos"):
            if not facebook_ok:
                return FakeResponse(400, {"error": {"message": "Invalid parameter", "code": 100}})
            return FakeResponse(200, {"id": "PHOTO1", "post_id": "1348603305007068_777"})
        if path.endswith("/feed"):   # a post carrying several uploaded pictures
            return FakeResponse(200, {"id": "1348603305007068_999"})
        return FakeResponse(404, {"error": {"message": f"unknown path {path}", "code": 803}})
    return handler


def paths(session) -> list[str]:
    return [f'{c["method"]} {c["url"].replace(GRAPH_URL.format(path=""), "")}' for c in session.calls]


def test_whoami_reads_the_page_and_its_instagram_account():
    session = FakeSession(graph_handler())
    client = MetaClient(TOKEN, session)

    assert client.whoami() == {"page_id": "1348603305007068", "page_name": "flyfromtirana.al",
                               "instagram_id": "17841417241858036", "instagram_username": "flyfromtirana"}
    client.whoami()   # cached: one call
    assert paths(session) == ["GET me"]
    # The token travels in a header, never in the address.
    call = session.calls[0]
    assert call["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    assert TOKEN not in call["url"] and TOKEN not in str(call.get("params"))


def test_instagram_post_creates_a_container_waits_and_publishes():
    waits = []
    session = FakeSession(graph_handler(container_checks=3))
    client = MetaClient(TOKEN, session, sleep=waits.append)

    link = client.post_instagram("https://flyfromtirana.al/social/bgy_feed.jpg", "✈️ TIRANA → MILAN nga €14")

    assert link == "https://www.instagram.com/p/abc123/"
    assert paths(session) == ["GET me", "POST 17841417241858036/media", "GET CONTAINER1", "GET CONTAINER1",
                              "GET CONTAINER1", "POST 17841417241858036/media_publish", "GET MEDIA1"]
    create = session.calls[1]
    assert create["data"] == {"image_url": "https://flyfromtirana.al/social/bgy_feed.jpg",
                              "caption": "✈️ TIRANA → MILAN nga €14"}
    assert session.calls[5]["data"] == {"creation_id": "CONTAINER1"}
    assert waits == [3, 3]   # waited between the checks, not after the last one


def test_instagram_story_has_no_caption_and_no_address():
    session = FakeSession(graph_handler())
    assert MetaClient(TOKEN, session).post_instagram("https://x/story.jpg", story=True) is None
    assert session.calls[1]["data"] == {"image_url": "https://x/story.jpg", "media_type": "STORIES"}
    assert "GET MEDIA1" not in paths(session)


def test_instagram_container_error_is_reported():
    session = FakeSession(graph_handler(container_status="ERROR"))
    with pytest.raises(MetaError, match="could not take the picture"):
        MetaClient(TOKEN, session, sleep=lambda s: None).post_instagram("https://x/feed.jpg", "hi")


def test_facebook_post_sends_the_picture_address_and_the_text():
    session = FakeSession(graph_handler())
    address = MetaClient(TOKEN, session).post_facebook("https://x/feed.jpg", "Rezervo: https://a.b/?x=1&y=2")

    assert address == "https://www.facebook.com/1348603305007068_777"
    assert paths(session) == ["GET me", "POST 1348603305007068/photos"]
    assert session.calls[1]["data"] == {"url": "https://x/feed.jpg", "message": "Rezervo: https://a.b/?x=1&y=2"}


def test_refusal_becomes_a_meta_error_with_metas_message():
    session = FakeSession(graph_handler(facebook_ok=False))
    with pytest.raises(MetaError, match=r"code 100\): Invalid parameter"):
        MetaClient(TOKEN, session).post_facebook("https://x/feed.jpg", "hi")


def test_bad_token_says_what_to_do():
    def handler(method, url, kwargs):
        return FakeResponse(400, {"error": {"message": "Error validating access token", "code": 190}})
    with pytest.raises(MetaError, match="META_PAGE_TOKEN is no longer valid"):
        MetaClient(TOKEN, FakeSession(handler)).whoami()


def test_page_without_instagram_account():
    def handler(method, url, kwargs):
        return FakeResponse(200, {"id": "1", "name": "Some Page"})
    with pytest.raises(MetaError, match="No Instagram account is linked"):
        MetaClient(TOKEN, FakeSession(handler)).post_instagram("https://x/feed.jpg", "hi")


def test_user_token_instead_of_page_token_says_so():
    def handler(method, url, kwargs):
        return FakeResponse(400, {"error": {"message": "(#100) Tried accessing nonexisting field (instagram_business_account)",
                                            "code": 100}})
    with pytest.raises(MetaError, match="User token, not the Page's token"):
        MetaClient(TOKEN, FakeSession(handler)).whoami()


def test_instagram_carousel_makes_a_container_per_picture_then_one_for_all():
    session = FakeSession(graph_handler())
    link = MetaClient(TOKEN, session, sleep=lambda s: None).post_instagram_carousel(
        ["https://x/cover.jpg", "https://x/1.jpg", "https://x/2.jpg"], "TOP 3")

    assert link == "https://www.instagram.com/p/abc123/"
    creates = [c["data"] for c in session.calls if c["url"].endswith("/media")]
    assert creates[:3] == [{"image_url": "https://x/cover.jpg", "is_carousel_item": "true"},
                           {"image_url": "https://x/1.jpg", "is_carousel_item": "true"},
                           {"image_url": "https://x/2.jpg", "is_carousel_item": "true"}]
    assert creates[3] == {"media_type": "CAROUSEL", "children": "CONTAINER1,CONTAINER1,CONTAINER1", "caption": "TOP 3"}
    assert paths(session).count("POST 17841417241858036/media_publish") == 1


def test_facebook_album_uploads_unpublished_photos_then_one_post():
    def handler(method, url, kwargs):
        path = url.replace(GRAPH_URL.format(path=""), "")
        if path == "me":
            return FakeResponse(200, ME)
        if path.endswith("/photos"):
            return FakeResponse(200, {"id": f"PHOTO{kwargs['data']['url'][-5]}"})
        if path.endswith("/feed"):
            return FakeResponse(200, {"id": "1348603305007068_999"})
        return FakeResponse(404, {"error": {"message": path, "code": 803}})
    session = FakeSession(handler)

    address = MetaClient(TOKEN, session).post_facebook_album(["https://x/a.jpg", "https://x/b.jpg"], "TOP 2")

    assert address == "https://www.facebook.com/1348603305007068_999"
    photos = [c["data"] for c in session.calls if c["url"].endswith("/photos")]
    assert photos == [{"url": "https://x/a.jpg", "published": "false"}, {"url": "https://x/b.jpg", "published": "false"}]
    feed, = [c["data"] for c in session.calls if c["url"].endswith("/feed")]
    assert feed == {"message": "TOP 2", "attached_media[0]": '{"media_fbid": "PHOTOa"}',
                    "attached_media[1]": '{"media_fbid": "PHOTOb"}'}


def test_reel_is_a_video_container_with_its_cover_and_waits_longer():
    waits = []
    session = FakeSession(graph_handler(container_checks=2))
    client = MetaClient(TOKEN, session, sleep=waits.append)

    link = client.post_instagram_reel("https://x/bgy_reel.mp4", "✈️ MILAN nga €14", cover_url="https://x/bgy_reel.jpg")

    assert link == "https://www.instagram.com/p/abc123/"
    assert session.calls[1]["data"] == {"media_type": "REELS", "video_url": "https://x/bgy_reel.mp4",
                                        "caption": "✈️ MILAN nga €14", "share_to_feed": "true",
                                        "cover_url": "https://x/bgy_reel.jpg"}
    assert waits == [5]   # a video gets longer between the checks


def test_reel_that_instagram_cannot_take_raises():
    session = FakeSession(graph_handler(container_status="ERROR"))
    with pytest.raises(MetaError, match="could not take the video"):
        MetaClient(TOKEN, session, sleep=lambda s: None).post_instagram_reel("https://x/v.mp4", "x")
