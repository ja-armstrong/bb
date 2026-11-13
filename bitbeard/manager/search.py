"""Deep Search module: query Jackett's Torznab API and return one owned result shape.

Everything a caller must know lives in two names: ``SearchResult`` (the single
owned definition of a result) and ``search`` (builds the Torznab URL, fetches,
parses). The XML parsing is an implementation detail, exposed as
``parse_torznab`` only so tests can exercise it directly.
"""
import logging
import urllib.parse
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass

import requests

TORZNAB_API_URL = "http://127.0.0.1:9117/api/v2.0/indexers/all/results/torznab/api"
TORZNAB_NAMESPACES = {"torznab": "http://torznab.com/schemas/2015/feed"}


@dataclass
class SearchResult:
    """The one owned definition of a search result."""

    title: str
    magnet_link: str
    seeders: int
    size_gb: float

    def as_dict(self):
        """The shape handed to JSON consumers (FastAPI /search)."""
        return {
            "title": self.title,
            "magnet_link": self.magnet_link,
            "seeders": self.seeders,
            "size_gb": self.size_gb,
        }


def _default_transport(url):
    """Thin requests-based adapter over the transport seam."""
    return requests.get(url)


def parse_torznab(xml_bytes):
    """Parse a Torznab RSS feed into SearchResults. Implementation detail."""
    root = ElementTree.fromstring(xml_bytes)
    results = []

    for item in root.findall("channel/item"):
        seeders_element = item.find(".//torznab:attr[@name='seeders']", TORZNAB_NAMESPACES)
        seeders = int(seeders_element.attrib["value"]) if seeders_element is not None else 0
        size_element = item.find("size")
        size_gb = int(size_element.text) / (1024 ** 3) if size_element is not None else 0.0

        results.append(SearchResult(
            title=item.find("title").text,
            magnet_link=item.find("link").text,
            seeders=seeders,
            size_gb=size_gb,
        ))

    return results


def search(query, *, config, transport=None):
    """Search all Jackett indexers for ``query``; return a list of SearchResults.

    ``transport`` is the seam for fetching: a callable taking the request URL
    and returning a response with ``status_code`` and ``content``. Defaults to
    a thin ``requests`` adapter; tests inject a fake.
    """
    fetch = transport if transport is not None else _default_transport

    url = (
        f"{TORZNAB_API_URL}"
        f"?apikey={config.jackett_api_key}&t=search&cat=&q={urllib.parse.quote(query)}"
    )
    response = fetch(url)

    if response.status_code != 200:
        logging.error(f"Jackett search failed: {response.status_code}")
        return []

    return parse_torznab(response.content)
