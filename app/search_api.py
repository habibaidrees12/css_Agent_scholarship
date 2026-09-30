"""Configured web search for official university pages.

Bing Web Search and Google Programmable Search are the supported providers.
Keys are read from the environment. This module does not call keyless search
engines and does not invent result links.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from app.config import PROJECT_ROOT, load_environment
from app.web_discovery import SearchResponse, SearchResult
from app.web_fetcher import USER_AGENT

NO_SEARCH_API = "No configured search API."
SENDS_EMAIL = False


@dataclass(frozen=True)
class SearchSettings:
    """Which search API can be used. Empty strings mean the value is unset."""

    bing_api_key: str = ""
    google_api_key: str = ""
    google_engine_id: str = ""
    tavily_api_key: str = ""
    provider: str = ""

    def bing_ready(self) -> bool:
        return bool(self.bing_api_key)

    def google_ready(self) -> bool:
        return bool(self.google_api_key and self.google_engine_id)

    def tavily_ready(self) -> bool:
        return bool(self.tavily_api_key)

    def selected_name(self) -> str | None:
        choice = self.provider.casefold()
        if choice == "tavily":
            return "tavily" if self.tavily_ready() else None
        if choice == "bing":
            return "bing" if self.bing_ready() else None
        if choice == "google":
            return "google" if self.google_ready() else None
        if self.tavily_ready():
            return "tavily"
        if self.bing_ready():
            return "bing"
        if self.google_ready():
            return "google"
        return None

    def status_message(self) -> str:
        name = self.selected_name()
        if name == "tavily":
            return "Tavily Basic Search is configured."
        if name == "bing":
            return "Bing Web Search API is configured."
        if name == "google":
            return "Google Programmable Search API is configured."
        if self.provider.casefold() == "tavily":
            return "TAVILY_API_KEY is missing."
        return NO_SEARCH_API


def read_search_settings(environ: dict[str, str] | None = None) -> SearchSettings:
    """Read search credentials. A missing mapping loads the process environment."""

    if environ is None:
        load_environment()
        source = os.environ
        tavily_api_key = source.get("TAVILY_API_KEY", "").strip() or _dotenv_value("TAVILY_API_KEY")
    else:
        source = environ
        tavily_api_key = source.get("TAVILY_API_KEY", "").strip()
    return SearchSettings(
        bing_api_key=source.get("BING_SEARCH_API_KEY", "").strip(),
        google_api_key=source.get("GOOGLE_SEARCH_API_KEY", "").strip(),
        google_engine_id=source.get("GOOGLE_SEARCH_ENGINE_ID", "").strip(),
        tavily_api_key=tavily_api_key,
        provider=source.get("SEARCH_PROVIDER", "").strip(),
    )


class SearchCache:
    """Remember one search response so the same query is not sent twice."""

    def get(self, key: str) -> SearchResponse | None:
        return None

    def put(self, key: str, response: SearchResponse) -> None:
        return None


class MemorySearchCache(SearchCache):
    def __init__(self) -> None:
        self._items: dict[str, SearchResponse] = {}

    def get(self, key: str) -> SearchResponse | None:
        return self._items.get(key)

    def put(self, key: str, response: SearchResponse) -> None:
        self._items[key] = response


class ApiSearchProvider:
    """One configured search API. The key stays on this object and is not logged."""

    name = "search-api"

    def __init__(self, *, timeout: float = 12, opener=None, cache: SearchCache | None = None) -> None:
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen
        self.cache = cache or MemorySearchCache()

    def search(self, query: str, *, limit: int = 5) -> SearchResponse:
        key = f"{self.name}|{query}|{limit}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        try:
            body, status = self._fetch(query, limit)
        except TimeoutError:
            response = SearchResponse(query=query, error=f"{self.name} timed out.", engine=self.name)
            self.cache.put(key, response)
            return response
        except urllib.error.HTTPError as exc:
            response = SearchResponse(
                query=query,
                error=f"{self.name} returned HTTP {getattr(exc, 'code', 'error')}.",
                engine=self.name,
            )
            self.cache.put(key, response)
            return response
        except urllib.error.URLError as exc:
            response = SearchResponse(
                query=query,
                error=f"{self.name} connection error: {getattr(exc, 'reason', exc)}.",
                engine=self.name,
            )
            self.cache.put(key, response)
            return response
        except OSError as exc:
            response = SearchResponse(
                query=query,
                error=f"{self.name} connection error: {exc}.",
                engine=self.name,
            )
            self.cache.put(key, response)
            return response
        if status >= 400:
            response = SearchResponse(query=query, error=f"{self.name} returned HTTP {status}.", engine=self.name)
        else:
            response = self._parse(query, body, limit)
        self.cache.put(key, response)
        return response

    def _fetch(self, query: str, limit: int) -> tuple[str, int]:
        raise NotImplementedError

    def _parse(self, query: str, body: str, limit: int) -> SearchResponse:
        raise NotImplementedError

    def _read(self, request: urllib.request.Request) -> tuple[str, int]:
        with self._opener(request, timeout=self.timeout) as response:
            status = getattr(response, "status", 200)
            raw = response.read()
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        return text, status


class BingSearchProvider(ApiSearchProvider):
    """Bing Web Search API v7. The subscription key is sent as a header."""

    name = "bing"

    def __init__(self, api_key: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.api_key = api_key

    def _fetch(self, query: str, limit: int) -> tuple[str, int]:
        params = urllib.parse.urlencode({"q": query, "count": str(limit), "responseFilter": "Webpages"})
        request = urllib.request.Request(
            f"https://api.bing.microsoft.com/v7.0/search?{params}",
            headers={
                "Ocp-Apim-Subscription-Key": self.api_key,
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        return self._read(request)

    def _parse(self, query: str, body: str, limit: int) -> SearchResponse:
        payload = _json_object(body)
        if payload is None:
            return SearchResponse(query=query, error="bing returned a response that was not JSON.", engine=self.name)
        pages = payload.get("webPages")
        values = pages.get("value") if isinstance(pages, dict) else None
        if not isinstance(values, list):
            return SearchResponse(query=query, results=[], engine=self.name)
        return SearchResponse(query=query, results=_hits(query, values, "url", "name", "snippet", limit, self.name), engine=self.name)


class GoogleSearchProvider(ApiSearchProvider):
    """Google Programmable Search JSON API."""

    name = "google"

    def __init__(self, api_key: str, engine_id: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.api_key = api_key
        self.engine_id = engine_id

    def _fetch(self, query: str, limit: int) -> tuple[str, int]:
        params = urllib.parse.urlencode(
            {
                "key": self.api_key,
                "cx": self.engine_id,
                "q": query,
                "num": str(min(limit, 10)),
            }
        )
        request = urllib.request.Request(
            f"https://www.googleapis.com/customsearch/v1?{params}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        return self._read(request)

    def _parse(self, query: str, body: str, limit: int) -> SearchResponse:
        payload = _json_object(body)
        if payload is None:
            return SearchResponse(query=query, error="google returned a response that was not JSON.", engine=self.name)
        items = payload.get("items")
        if items is None:
            return SearchResponse(query=query, results=[], engine=self.name)
        if not isinstance(items, list):
            return SearchResponse(query=query, error="google returned an unexpected result list.", engine=self.name)
        return SearchResponse(query=query, results=_hits(query, items, "link", "title", "snippet", limit, self.name), engine=self.name)


def build_search_provider(
    settings: SearchSettings | None = None,
    *,
    timeout: float = 12,
    opener=None,
    cache: SearchCache | None = None,
):
    """Return the configured provider, or None when credentials are incomplete."""

    chosen = settings or read_search_settings()
    name = chosen.selected_name()
    if name == "tavily":
        from app.tavily_search import TavilySearchProvider

        return TavilySearchProvider(chosen.tavily_api_key, timeout=timeout, opener=opener)
    if name == "bing":
        return BingSearchProvider(chosen.bing_api_key, timeout=timeout, opener=opener, cache=cache)
    if name == "google":
        return GoogleSearchProvider(
            chosen.google_api_key,
            chosen.google_engine_id,
            timeout=timeout,
            opener=opener,
            cache=cache,
        )
    return None


def _dotenv_value(name: str) -> str:
    """Read one value from .env when python-dotenv is not installed."""

    path = PROJECT_ROOT / ".env"
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        if key.strip() == name:
            return raw.strip().strip('"').strip("'")
    return ""


def _json_object(body: str) -> dict | None:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _hits(
    query: str,
    rows: list,
    url_key: str,
    title_key: str,
    snippet_key: str,
    limit: int,
    engine: str,
) -> list[SearchResult]:
    found: list[SearchResult] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = row.get(url_key)
        if not isinstance(url, str) or not url.strip():
            continue
        title = row.get(title_key)
        snippet = row.get(snippet_key)
        found.append(
            SearchResult(
                query=query,
                title=title.strip() if isinstance(title, str) else "",
                url=url.strip(),
                snippet=snippet.strip() if isinstance(snippet, str) else "",
                engine=engine,
            )
        )
        if len(found) >= limit:
            break
    return found
