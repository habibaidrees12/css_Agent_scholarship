"""Tavily Basic Search provider.

This uses the existing SearchProvider interface. It reads TAVILY_API_KEY from
the environment, returns title, URL, and snippet, and does not send email.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from app.web_discovery import SearchProvider, SearchResponse, SearchResult
from app.web_fetcher import USER_AGENT

MISSING_KEY = "TAVILY_API_KEY is missing."
CREDITS_EXHAUSTED = "Tavily credits are exhausted. Search stopped."
_CREDIT_CODES = {429, 432, 433}


class TavilySearchProvider(SearchProvider):
    """One Tavily Basic Search request. Results are the links Tavily returned."""

    name = "tavily"

    def __init__(self, api_key: str, *, timeout: float = 20, opener=None) -> None:
        self.api_key = api_key.strip() if isinstance(api_key, str) else ""
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def search(self, query: str, *, limit: int = 5) -> SearchResponse:
        if not self.api_key:
            return SearchResponse(query=query, error=MISSING_KEY, engine=self.name)
        payload = json.dumps(
            {
                "query": query,
                "search_depth": "basic",
                "max_results": max(1, min(limit, 5)),
                "include_answer": False,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            "https://api.tavily.com/search",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                raw = response.read()
        except TimeoutError:
            return SearchResponse(query=query, error="Tavily timed out. Search stopped.", engine=self.name)
        except urllib.error.HTTPError as exc:
            return _http_error(query, exc)
        except urllib.error.URLError as exc:
            return SearchResponse(
                query=query,
                error=f"Tavily connection error: {getattr(exc, 'reason', exc)}.",
                engine=self.name,
            )
        except OSError as exc:
            return SearchResponse(query=query, error=f"Tavily connection error: {exc}.", engine=self.name)
        if status in _CREDIT_CODES:
            return SearchResponse(query=query, error=CREDITS_EXHAUSTED, engine=self.name)
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        return _parse(query, text)


def _http_error(query: str, exc: urllib.error.HTTPError) -> SearchResponse:
    code = getattr(exc, "code", None)
    detail = ""
    try:
        detail = exc.read().decode("utf-8", errors="replace")
    except Exception:
        detail = ""
    lowered = detail.casefold()
    if code in _CREDIT_CODES or any(phrase in lowered for phrase in ("credit", "usage limit", "quota", "exceed")):
        return SearchResponse(query=query, error=CREDITS_EXHAUSTED, engine="tavily")
    if code in {401, 403}:
        return SearchResponse(query=query, error="Tavily rejected the API key.", engine="tavily")
    return SearchResponse(query=query, error=f"Tavily returned HTTP {code}. Search stopped.", engine="tavily")


def _parse(query: str, body: str) -> SearchResponse:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return SearchResponse(query=query, error="Tavily returned a response that was not JSON.", engine="tavily")
    if not isinstance(payload, dict):
        return SearchResponse(query=query, error="Tavily returned an unexpected response.", engine="tavily")
    rows = payload.get("results")
    if not isinstance(rows, list):
        return SearchResponse(query=query, results=[], engine="tavily")
    results: list[SearchResult] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = row.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        title = row.get("title")
        content = row.get("content")
        if not isinstance(content, str) or not content.strip():
            content = row.get("snippet")
        results.append(
            SearchResult(
                query=query,
                title=title.strip() if isinstance(title, str) else "",
                url=url.strip(),
                snippet=content.strip() if isinstance(content, str) else "",
                engine="tavily",
            )
        )
    return SearchResponse(query=query, results=results, engine="tavily")
