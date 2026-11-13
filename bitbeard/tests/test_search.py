"""Tests for the deep Search module (bitbeard.search)."""
import urllib.parse
from xml.sax.saxutils import escape

from bitbeard.config import Config
from bitbeard.search import SearchResult, search


TORZNAB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>Jackett</title>
    <item>
      <title>{title_1}</title>
      <link>{link_1}</link>
      <size>3668473856</size>
      <torznab:attr name="seeders" value="87" />
      <torznab:attr name="peers" value="12" />
    </item>
    <item>
      <title>{title_2}</title>
      <link>{link_2}</link>
      <size>1073741824</size>
    </item>
    <item>
      <title>{title_3}</title>
      <link>{link_3}</link>
      <torznab:attr name="seeders" value="12" />
    </item>
  </channel>
</rss>""".format(
    title_1=escape("Ubuntu 24.04 LTS Desktop amd64"),
    link_1=escape(
        "magnet:?xt=urn:btih:abcdef1234567890"
        "&dn=ubuntu-24.04-desktop-amd64.iso"
        "&tr=udp%3A%2F%2Ftracker.example.org%3A1337%2Fannounce"
    ),
    title_2=escape("No Seeders Attr Ubuntu 22.04"),
    link_2=escape("magnet:?xt=urn:btih:1111111111111111&dn=ubuntu-22.04"),
    title_3=escape("Missing Size Some Linux Distro"),
    link_3=escape("magnet:?xt=urn:btih:2222222222222222&dn=some-distro"),
)


def make_config():
    return Config(
        qbit_username="admin",
        qbit_password="hunter2",
        base_download_dir="/media/downloads",
        announce_channel_id=123456789,
        discord_bot_token="discord-token",
        jackett_api_key="jackett-key",
    )


class FakeResponse:
    def __init__(self, status_code=200, content=b""):
        self.status_code = status_code
        self.content = content


class FakeTransport:
    """Records every URL it is called with; returns a canned response."""

    def __init__(self, response):
        self.response = response
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return self.response


def make_transport(status_code=200, content=TORZNAB_XML.encode("utf-8")):
    transport = FakeTransport(FakeResponse(status_code, content))
    return transport


def test_happy_path_parses_all_fields():
    transport = make_transport()

    results = search("linux iso", config=make_config(), transport=transport)

    assert len(results) == 3
    first = results[0]
    assert isinstance(first, SearchResult)
    assert first.title == "Ubuntu 24.04 LTS Desktop amd64"
    assert first.magnet_link == (
        "magnet:?xt=urn:btih:abcdef1234567890"
        "&dn=ubuntu-24.04-desktop-amd64.iso"
        "&tr=udp%3A%2F%2Ftracker.example.org%3A1337%2Fannounce"
    )
    assert first.seeders == 87
    assert isinstance(first.seeders, int)
    # 3668473856 bytes == 3.41796875 GiB
    assert first.size_gb == 3668473856 / (1024 ** 3)
    assert isinstance(first.size_gb, float)


def test_missing_seeders_defaults_to_zero():
    transport = make_transport()

    results = search("linux iso", config=make_config(), transport=transport)

    assert results[1].seeders == 0


def test_missing_size_defaults_to_zero():
    transport = make_transport()

    results = search("linux iso", config=make_config(), transport=transport)

    assert results[2].size_gb == 0.0


def test_as_dict_returns_the_owned_shape():
    result = SearchResult(
        title="t", magnet_link="magnet:?xt=urn:btih:x", seeders=5, size_gb=1.5
    )

    assert result.as_dict() == {
        "title": "t",
        "magnet_link": "magnet:?xt=urn:btih:x",
        "seeders": 5,
        "size_gb": 1.5,
    }


def test_non_200_returns_empty_list():
    transport = make_transport(status_code=502, content=b"<html>oops</html>")

    results = search("linux iso", config=make_config(), transport=transport)

    assert results == []


def test_query_is_url_encoded_in_request_url():
    transport = make_transport()
    query = "linux iso & fun"

    search(query, config=make_config(), transport=transport)

    assert len(transport.urls) == 1
    url = transport.urls[0]
    # The endpoint, apikey and params are unchanged from the previous inline call.
    assert url.startswith(
        "http://127.0.0.1:9117/api/v2.0/indexers/all/results/torznab/api"
        "?apikey=jackett-key&t=search&cat=&q="
    )
    assert urllib.parse.quote(query) in url
    # The raw (unencoded) query must not appear in the URL.
    assert query not in url


def test_empty_feed_returns_empty_list():
    empty_feed = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">'
        b"<channel><title>Jackett</title></channel></rss>"
    )
    transport = make_transport(content=empty_feed)

    results = search("nothing matches", config=make_config(), transport=transport)

    assert results == []
