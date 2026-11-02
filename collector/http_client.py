"""HTTP access with robots.txt checks, rate limiting, and retries.

Every outbound request goes through PoliteClient so that collection stays
inside what the source allows: robots.txt is consulted before a URL is
fetched, requests are spaced by a fixed delay, and transient failures are
retried with exponential backoff.
"""

from __future__ import annotations

import logging
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

USER_AGENT = (
    "insurance-data-collector/0.1 "
    "(student project; contact: shiyi_tang611@outlook.com)"
)


class RobotsDisallowed(RuntimeError):
    """Raised when robots.txt does not allow fetching a URL."""


class ClientRequestError(RuntimeError):
    """Raised on a 4xx, which means the request itself needs fixing."""


def build_robots_parser(robots_url: str, status_code: int, text: str) -> robotparser.RobotFileParser:
    """Turn a robots.txt response into a parser, following the usual conventions.

    2xx  parse the rules.
    404  no robots.txt, so nothing is restricted.
    401/403  access to the rules is refused, so treat the site as off limits.
    other  unknown state, so stay on the cautious side.

    This is split out from fetching because the standard library's own
    ``RobotFileParser.read()`` requests robots.txt with a ``Python-urllib``
    user agent. Sites that block that agent answer 403, which the parser
    reads as "disallow everything" even when the published rules allow the
    request. Fetching robots.txt with the same identified client used for
    data avoids that false negative.
    """
    parser = robotparser.RobotFileParser()
    parser.set_url(robots_url)
    if 200 <= status_code < 300:
        parser.parse(text.splitlines())
    elif status_code == 404:
        parser.allow_all = True
    else:
        parser.disallow_all = True
    return parser


@dataclass
class PoliteClient:
    """A requests session that is deliberately slow and well behaved."""

    delay_seconds: float = 1.0
    timeout_seconds: float = 60.0
    max_retries: int = 4
    respect_robots: bool = True

    _session: requests.Session = field(default_factory=requests.Session, init=False)
    _robots: dict[str, robotparser.RobotFileParser] = field(default_factory=dict, init=False)
    _last_request_at: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self._session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    # ---------- robots.txt ----------

    def _robots_for(self, url: str) -> robotparser.RobotFileParser:
        parts = urlparse(url)
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in self._robots:
            robots_url = f"{root}/robots.txt"
            try:
                # Fetched with the same identified session as the data, not
                # with the standard library's default user agent.
                response = self._session.get(robots_url, timeout=self.timeout_seconds)
                parser = build_robots_parser(robots_url, response.status_code, response.text)
                log.info("Read robots.txt for %s (HTTP %s)", root, response.status_code)
            except requests.RequestException as exc:
                log.warning("Could not reach robots.txt for %s (%s); refusing", root, exc)
                parser = build_robots_parser(robots_url, 599, "")
            self._robots[root] = parser
        return self._robots[root]

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        return self._robots_for(url).can_fetch(USER_AGENT, url)

    def crawl_delay(self, url: str) -> float:
        """Honour a site-declared Crawl-delay when it is longer than ours."""
        if not self.respect_robots:
            return self.delay_seconds
        declared = self._robots_for(url).crawl_delay(USER_AGENT)
        if declared is None:
            return self.delay_seconds
        return max(self.delay_seconds, float(declared))

    # ---------- fetching ----------

    def _sleep_between_requests(self, url: str) -> None:
        wait = self.crawl_delay(url) - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def get(self, url: str, params: dict[str, Any] | None = None) -> requests.Response:
        if not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt does not allow fetching {url}")

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._sleep_between_requests(url)
            try:
                response = self._session.get(url, params=params, timeout=self.timeout_seconds)
                self._last_request_at = time.monotonic()
            except requests.RequestException as exc:
                last_error = exc
                backoff = 2 ** (attempt - 1)
                log.warning("Request failed (%s); retrying in %ss", exc, backoff)
                time.sleep(backoff)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                backoff = float(response.headers.get("Retry-After", 2 ** (attempt - 1)))
                log.warning(
                    "HTTP %s from server; retrying in %ss (attempt %s/%s)",
                    response.status_code,
                    backoff,
                    attempt,
                    self.max_retries,
                )
                last_error = requests.HTTPError(f"HTTP {response.status_code}")
                time.sleep(backoff)
                continue

            if 400 <= response.status_code < 500:
                # Not worth retrying, and the body usually says what was wrong
                # (a misspelled dataset name, an unknown field in $filter).
                detail = response.text.strip()[:300].replace("\n", " ")
                raise ClientRequestError(
                    f"HTTP {response.status_code} for {response.url}\n  {detail}"
                )

            response.raise_for_status()
            return response

        raise RuntimeError(f"Giving up on {url} after {self.max_retries} attempts") from last_error

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.get(url, params=params).json()
