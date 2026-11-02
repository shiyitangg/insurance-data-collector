"""Tests for how robots.txt responses are interpreted.

The regression these cover: the standard library fetches robots.txt with a
``Python-urllib`` user agent, and a site that answers 403 to that agent makes
the parser report every URL as disallowed, even when the published rules
allow it. Parsing is therefore separated from fetching and tested directly.
"""

from __future__ import annotations

import pytest

from collector.http_client import USER_AGENT, build_robots_parser

ROBOTS_URL = "https://example.gov/robots.txt"
API_URL = "https://example.gov/api/open/v1/DataSets"
ADMIN_URL = "https://example.gov/admin/secret"

# Shaped like the real file: several groups, one of which blocks a named bot.
SAMPLE = """
User-agent: *
Disallow: /admin/
Disallow: /search/

User-agent: PetalBot
Disallow: /

User-agent: *
Disallow: /es/
Disallow: /fr/
"""


@pytest.mark.parametrize("status", [200, 204])
def test_rules_are_parsed_on_success(status: int) -> None:
    parser = build_robots_parser(ROBOTS_URL, status, SAMPLE)
    assert parser.can_fetch(USER_AGENT, API_URL) is True
    assert parser.can_fetch(USER_AGENT, ADMIN_URL) is False


def test_blocking_a_named_bot_does_not_block_everyone() -> None:
    parser = build_robots_parser(ROBOTS_URL, 200, SAMPLE)
    assert parser.can_fetch("PetalBot", API_URL) is False
    assert parser.can_fetch(USER_AGENT, API_URL) is True


def test_missing_robots_allows_everything() -> None:
    parser = build_robots_parser(ROBOTS_URL, 404, "")
    assert parser.can_fetch(USER_AGENT, API_URL) is True


@pytest.mark.parametrize("status", [401, 403, 500, 599])
def test_unreadable_robots_blocks_everything(status: int) -> None:
    parser = build_robots_parser(ROBOTS_URL, status, "")
    assert parser.can_fetch(USER_AGENT, API_URL) is False


def test_empty_robots_allows_everything() -> None:
    parser = build_robots_parser(ROBOTS_URL, 200, "")
    assert parser.can_fetch(USER_AGENT, API_URL) is True
