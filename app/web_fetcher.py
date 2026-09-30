"""Fetch one public page and return its title and text.

This module performs a normal HTTP GET. It does not solve CAPTCHA checks,
log in, pass a paywall, or ignore robots.txt. A blocked or failed request is
reported and left for the caller to skip.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

USER_AGENT = "CSCProfessorResearchAgent/0.1"
SENDS_EMAIL = False
_MAX_BYTES = 1_000_000


@dataclass
class FetchedPage:
    """The outcome of one page request. Missing text stays empty."""

    requested_url: str
    final_url: str = ""
    status_code: int | None = None
    title: str = ""
    text: str = ""
    error: str | None = None
    blocked: bool = False
    headers: dict[str, str] = field(default_factory=dict)


class OfficialPageFetcher:
    """Download a page that a search result already pointed to."""

    def __init__(self, *, timeout: float = 12, opener=None, check_robots: bool = True) -> None:
        self.timeout = timeout
        self.check_robots = check_robots
        self._opener = opener or urllib.request.urlopen

    def fetch(self, url: str) -> FetchedPage:
        """Request url and extract visible text. Failures stay on the result."""

        if not _is_http_url(url):
            return FetchedPage(requested_url=url or "", error="The URL is not an HTTP URL.")
        if self.check_robots:
            allowed, robots_error = self._robots_allows(url)
            if not allowed:
                return FetchedPage(
                    requested_url=url,
                    error=robots_error,
                    blocked=True,
                )
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                final_url = response.geturl() if hasattr(response, "geturl") else url
                raw = response.read(_MAX_BYTES)
                header_map = _header_map(response)
        except TimeoutError:
            return FetchedPage(requested_url=url, error="The request timed out.")
        except urllib.error.HTTPError as exc:
            code = getattr(exc, "code", None)
            return FetchedPage(
                requested_url=url,
                status_code=code if isinstance(code, int) else None,
                error=f"HTTP {code}.",
                blocked=code in {401, 403, 429},
            )
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                return FetchedPage(requested_url=url, error="The request timed out.")
            return FetchedPage(requested_url=url, error=f"Connection error: {reason}.")
        except OSError as exc:
            return FetchedPage(requested_url=url, error=f"Connection error: {exc}.")

        if not raw or not raw.strip():
            return FetchedPage(
                requested_url=url,
                final_url=final_url,
                status_code=status,
                error="The page was empty.",
                headers=header_map,
            )
        try:
            html = _decode_html(raw, header_map.get("content-type", ""))
            title, text = _visible_text(html)
        except Exception as exc:
            return FetchedPage(
                requested_url=url,
                final_url=final_url,
                status_code=status,
                error=f"The page HTML could not be read ({exc.__class__.__name__}).",
                headers=header_map,
            )
        blocked, block_reason = _blocked_page(status, text)
        if not text.strip():
            return FetchedPage(
                requested_url=url,
                final_url=final_url,
                status_code=status,
                title=title,
                error="The page was empty.",
                blocked=blocked,
                headers=header_map,
            )
        return FetchedPage(
            requested_url=url,
            final_url=final_url,
            status_code=status,
            title=title,
            text=text,
            error=block_reason,
            blocked=blocked,
            headers=header_map,
        )

    def _robots_allows(self, url: str) -> tuple[bool, str | None]:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = response.read(100_000)
        except urllib.error.HTTPError as exc:
            if getattr(exc, "code", None) == 404:
                return True, None
            return False, f"robots.txt returned HTTP {getattr(exc, 'code', 'error')}."
        except TimeoutError:
            return False, "robots.txt timed out, so the page was not fetched."
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                return False, "robots.txt timed out, so the page was not fetched."
            return False, f"robots.txt could not be read ({reason})."
        except OSError as exc:
            return False, f"robots.txt could not be read ({exc})."
        parser = RobotFileParser()
        parser.parse(_decode_html(body, "").splitlines())
        if parser.can_fetch(USER_AGENT, url):
            return True, None
        return False, "robots.txt disallows this page."


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        name = tag.casefold()
        if name in {"script", "style", "noscript", "svg"}:
            self._skip += 1
        if name == "title":
            self._in_title = True
        if name in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        if name in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1
        if name == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title_parts.append(data)
        self.parts.append(data)


def _visible_text(html: str) -> tuple[str, str]:
    parser = _VisibleTextParser()
    parser.feed(html)
    parser.close()
    title = " ".join("".join(parser.title_parts).split())
    text = re.sub(r"\n{3,}", "\n\n", "".join(parser.parts))
    text = "\n".join(line.strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return title, text


def _decode_html(raw: bytes, content_type: str) -> str:
    match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type or "", flags=re.IGNORECASE)
    if match is None:
        sample = raw[:1500].decode("ascii", errors="ignore")
        match = re.search(r"charset=[\"']?([A-Za-z0-9._-]+)", sample, flags=re.IGNORECASE)
    encoding = match.group(1) if match else "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _header_map(response: object) -> dict[str, str]:
    headers = getattr(response, "headers", None)
    if headers is None or not hasattr(headers, "items"):
        return {}
    return {str(key).casefold(): str(value) for key, value in headers.items()}


def _blocked_page(status: int, text: str) -> tuple[bool, str | None]:
    if status in {401, 403, 429}:
        return True, f"HTTP {status} blocked the page."
    sample = text.casefold()
    markers = ("captcha", "cf-browser-verification", "access denied", "just a moment")
    if any(marker in sample for marker in markers) and len(text) < 2500:
        return True, "The page was blocked by an anti-bot check."
    return False, None


def _is_http_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
