#!/usr/bin/env python3
"""
jellyfin_people_wikipedia_images.py

Populate missing Jellyfin Person primary images from English Wikipedia.

The program deliberately uses Jellyfin's image API instead of writing directly
to Jellyfin's metadata cache directory. Jellyfin then owns the storage,
database/cache tags, and image lifecycle.

SAFETY / MATCHING
-----------------
1. DRY RUN is the default. Use --apply to upload images.
2. Enumerates Person records via /Items?IncludeItemTypes=Person rather than
   relying on /Persons.
3. Skips any Person that already has a Primary image.
4. Scans Jellyfin People references once to learn context/roles for each Person.
5. Wikipedia selection requires identity first:
      Queen -> John Deacon               REJECTED
      Queen -> Queen (band)              ACCEPTED
      Arthur Blake -> Arthur Blake hurdler
                                           REJECTED for a music Person
      Arthur Blake -> Blind Blake        ACCEPTED when the lead identifies
                                         Blind Blake as Arthur Blake
6. When Jellyfin context says Composer/Artist/Musician/etc., the Wikipedia page
   must contain clear music-related context.
7. If Jellyfin already has a Wikipedia HomePageUrl, that page is tried first,
   but it still has to pass identity/context validation.
8. Uses the Wikipedia page-summary lead image (thumbnail preferred).
9. Never replaces an existing Jellyfin Primary image.
10. After upload, verifies Jellyfin reports a Primary image.
11. Caches successful and unsuccessful Wikipedia resolution by Jellyfin Person
    ID so reruns avoid unnecessary Wikimedia requests.
12. All Wikimedia requests share a 1 request/second limiter.
13. HTTP 429 stops further Wikimedia work for the current run.
14. Transient 5xx errors use bounded exponential backoff.

Requirements:
    Python 3.10+
    pip install requests

Typical usage:
    export JELLYFIN_API_KEY="..."
    python jellyfin_people_wikipedia_images.py
    python jellyfin_people_wikipedia_images.py --apply

Test one Person first:
    python jellyfin_people_wikipedia_images.py --name "Arthur Blake"
    python jellyfin_people_wikipedia_images.py --name "Arthur Blake" --apply
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

import requests


VERSION = "1.0.0"

DEFAULT_JELLYFIN_URL = "http://127.0.0.1:8091"
PAGE_SIZE = 500
WIKIMEDIA_CONTACT = "music@designworld.ca"
WIKIMEDIA_USER_AGENT = (
    f"JellyfinPeopleWikipediaImages/{VERSION} "
    f"(contact: {WIKIMEDIA_CONTACT})"
)

WIKIPEDIA_SEARCH_URL = "https://en.wikipedia.org/w/rest.php/v1/search/page"
WIKIPEDIA_SUMMARY_URL = (
    "https://en.wikipedia.org/api/rest_v1/page/summary/{}"
)

PROGRAM_DIR = Path(__file__).resolve().parent
CACHE_FILE = PROGRAM_DIR / "jellyfin-people-wikipedia-image-cache.json"
LOG_FILE = PROGRAM_DIR / "jellyfin-people-wikipedia-images.log"

LOG = logging.getLogger("jellyfin-people-wikipedia-images")


# ---------------------------------------------------------------------------
# Name/context normalization
# ---------------------------------------------------------------------------

def normalized_name(value: str) -> str:
    value = html.unescape(str(value or "")).strip()
    value = re.sub(r"\s*\([^)]*\)\s*$", "", value)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(
        char
        for char in value
        if not unicodedata.combining(char)
    )
    value = value.casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalized_lead(value: str) -> str:
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(
        char
        for char in value
        if not unicodedata.combining(char)
    )
    value = value.casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


MUSIC_KEYWORDS = (
    "musician",
    "composer",
    "songwriter",
    "singer",
    "vocalist",
    "guitarist",
    "pianist",
    "violinist",
    "instrumentalist",
    "multi-instrumentalist",
    "keyboardist",
    "drummer",
    "bassist",
    "conductor",
    "record producer",
    "music producer",
    "producer",
    "arranger",
    "rock band",
    "band",
    "musical group",
    "music group",
    "recording artist",
    "film score",
    "blues",
    "ragtime",
    "jazz",
    "classical music",
    "saxophonist",
    "trumpeter",
    "cellist",
    "organist",
    "rapper",
    "dj",
)

ACTOR_KEYWORDS = (
    "actor",
    "actress",
    "film actor",
    "television actor",
    "stage actor",
    "performer",
)

DIRECTOR_KEYWORDS = (
    "film director",
    "television director",
    "director",
    "filmmaker",
)

WRITER_KEYWORDS = (
    "writer",
    "author",
    "screenwriter",
    "novelist",
    "playwright",
    "poet",
)

ROLE_TO_CONTEXT = {
    "composer": "music",
    "artist": "music",
    "albumartist": "music",
    "album artist": "music",
    "musician": "music",
    "songwriter": "music",
    "singer": "music",
    "performer": "music",
    "conductor": "music",
    "producer": "music",
    "music producer": "music",
    "arranger": "music",
    "actor": "actor",
    "actress": "actor",
    "director": "director",
    "writer": "writer",
    "screenwriter": "writer",
    "author": "writer",
}


def context_kinds_from_roles(role_values: set[str]) -> set[str]:
    result: set[str] = set()

    for raw in role_values:
        value = str(raw or "").strip().casefold()

        if not value:
            continue

        if value in ROLE_TO_CONTEXT:
            result.add(ROLE_TO_CONTEXT[value])
            continue

        for token, context in ROLE_TO_CONTEXT.items():
            if token in value:
                result.add(context)

    return result


def text_has_context(text: str, context_kinds: set[str]) -> bool:
    folded = str(text or "").casefold()

    if not context_kinds:
        return True

    if "music" in context_kinds and any(
        keyword in folded
        for keyword in MUSIC_KEYWORDS
    ):
        return True

    if "actor" in context_kinds and any(
        keyword in folded
        for keyword in ACTOR_KEYWORDS
    ):
        return True

    if "director" in context_kinds and any(
        keyword in folded
        for keyword in DIRECTOR_KEYWORDS
    ):
        return True

    if "writer" in context_kinds and any(
        keyword in folded
        for keyword in WRITER_KEYWORDS
    ):
        return True

    return False


def identity_score(
    person_name: str,
    title: str,
    lead_or_excerpt: str,
) -> int:
    person_key = normalized_name(person_name)
    title_key = normalized_name(title)

    if not person_key or not title_key:
        return 0

    if title_key == person_key:
        return 200

    # Alias/stage-name rule: a differently titled article is accepted only
    # when its lead starts by explicitly identifying the subject with the
    # Jellyfin Person name.
    lead_key = normalized_lead(lead_or_excerpt)

    if lead_key.startswith(person_key + " "):
        return 160

    return 0


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s"
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = logging.FileHandler(
        LOG_FILE,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


# ---------------------------------------------------------------------------
# Wikimedia client
# ---------------------------------------------------------------------------

class WikimediaRateLimitError(RuntimeError):
    pass


class Wikipedia:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": WIKIMEDIA_USER_AGENT,
            "Accept": "application/json",
        })
        self.last_request_monotonic = 0.0
        self.stopped = False
        self.cache = self.load_cache()

    def load_cache(self) -> dict[str, Any]:
        if not CACHE_FILE.exists():
            return {}

        try:
            data = json.loads(
                CACHE_FILE.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning(
                "Could not read cache %s: %s",
                CACHE_FILE,
                exc,
            )
            return {}

        return data if isinstance(data, dict) else {}

    def save_cache(self) -> None:
        temporary = CACHE_FILE.with_suffix(
            CACHE_FILE.suffix + ".tmp"
        )

        temporary.write_text(
            json.dumps(
                self.cache,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(CACHE_FILE)

    def wait_for_rate_limit(self) -> None:
        elapsed = (
            time.monotonic()
            - self.last_request_monotonic
        )

        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)

    def request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        if self.stopped:
            raise WikimediaRateLimitError(
                "Wikimedia access has been stopped for this run."
            )

        delays = (1, 2, 4)

        for attempt in range(4):
            self.wait_for_rate_limit()

            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=120,
                    **kwargs,
                )
            finally:
                self.last_request_monotonic = time.monotonic()

            if response.status_code == 429:
                self.stopped = True
                retry_after = response.headers.get("Retry-After")

                if retry_after:
                    message = (
                        "Wikimedia HTTP 429 Retry-After="
                        f"{retry_after}; stopping Wikimedia work."
                    )
                else:
                    message = (
                        "Wikimedia HTTP 429; stopping Wikimedia work."
                    )

                LOG.error(message)
                raise WikimediaRateLimitError(message)

            if 500 <= response.status_code < 600 and attempt < 3:
                delay = delays[attempt]
                LOG.warning(
                    "Wikimedia HTTP %s from %s; retrying in %ss",
                    response.status_code,
                    url,
                    delay,
                )
                time.sleep(delay)
                continue

            response.raise_for_status()
            return response

        raise RuntimeError("Unreachable Wikimedia retry state.")

    def search_pages(
        self,
        query: str,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        response = self.request(
            "GET",
            WIKIPEDIA_SEARCH_URL,
            params={
                "q": query,
                "limit": limit,
            },
        )

        pages = response.json().get("pages", [])
        return pages if isinstance(pages, list) else []

    def summary(
        self,
        title: str,
    ) -> dict[str, Any] | None:
        url = WIKIPEDIA_SUMMARY_URL.format(
            quote(
                title.replace(" ", "_"),
                safe="",
            )
        )

        response = self.request("GET", url)

        if response.status_code == 404:
            return None

        data = response.json()

        if data.get("type") == "disambiguation":
            return None

        return data

    @staticmethod
    def wikipedia_title_from_homepage(
        homepage_url: str,
    ) -> str | None:
        value = str(homepage_url or "").strip()

        if not value:
            return None

        try:
            parsed = urlparse(value)
        except ValueError:
            return None

        host = parsed.netloc.casefold()

        if host not in {
            "en.wikipedia.org",
            "www.en.wikipedia.org",
        }:
            return None

        prefix = "/wiki/"

        if not parsed.path.startswith(prefix):
            return None

        title = unquote(
            parsed.path[len(prefix):]
        ).replace("_", " ").strip()

        return title or None

    @staticmethod
    def candidate_text(page: dict[str, Any]) -> str:
        excerpt = html.unescape(
            re.sub(
                r"<[^>]+>",
                " ",
                str(page.get("excerpt") or ""),
            )
        )

        return " ".join([
            str(page.get("title") or ""),
            str(page.get("description") or ""),
            excerpt,
        ])

    def page_search_score(
        self,
        person_name: str,
        page: dict[str, Any],
        contexts: set[str],
    ) -> int:
        title = str(page.get("title") or "")
        excerpt = str(page.get("excerpt") or "")
        identity = identity_score(
            person_name,
            title,
            excerpt,
        )

        if identity <= 0:
            return -10000

        combined = self.candidate_text(page)

        if "disambiguation" in combined.casefold():
            return -10000

        if not text_has_context(
            combined,
            contexts,
        ):
            return -10000

        return identity

    def validate_summary(
        self,
        person_name: str,
        title: str,
        summary: dict[str, Any],
        contexts: set[str],
    ) -> bool:
        overview = str(summary.get("extract") or "")
        description = str(summary.get("description") or "")

        if summary.get("type") == "disambiguation":
            return False

        identity = identity_score(
            person_name,
            title,
            overview,
        )

        if identity <= 0:
            return False

        combined = f"{title} {description} {overview}"

        return text_has_context(
            combined,
            contexts,
        )

    @staticmethod
    def image_url_from_summary(
        summary: dict[str, Any],
    ) -> str | None:
        # Prefer Wikipedia's thumbnail to avoid downloading unnecessarily
        # huge originals. It is sufficient for Jellyfin person cards.
        thumbnail = summary.get("thumbnail") or {}

        source = str(
            thumbnail.get("source")
            or thumbnail.get("url")
            or ""
        ).strip()

        if source:
            return source

        original = summary.get("originalimage") or {}

        source = str(
            original.get("source")
            or original.get("url")
            or ""
        ).strip()

        return source or None

    def query_suffixes_for_contexts(
        self,
        contexts: set[str],
    ) -> list[str]:
        result: list[str] = []

        if "music" in contexts:
            result.extend([
                "musician",
                "composer",
                "singer",
                "guitarist",
                "songwriter",
                "conductor",
            ])

        if "actor" in contexts:
            result.extend([
                "actor",
                "actress",
            ])

        if "director" in contexts:
            result.extend([
                "film director",
                "director",
            ])

        if "writer" in contexts:
            result.extend([
                "writer",
                "author",
                "screenwriter",
            ])

        # Deduplicate while preserving order.
        deduped: list[str] = []
        seen: set[str] = set()

        for value in result:
            key = value.casefold()

            if key not in seen:
                seen.add(key)
                deduped.append(value)

        return deduped

    def resolve(
        self,
        person: dict[str, Any],
        contexts: set[str],
    ) -> dict[str, Any] | None:
        person_id = str(person.get("Id") or "").strip()
        person_name = str(person.get("Name") or "").strip()

        if not person_id or not person_name:
            return None

        cache_key = person_id.casefold()
        cached = self.cache.get(cache_key)

        if isinstance(cached, dict):
            status = cached.get("status")

            if status == "found":
                return cached

            if status in {
                "not_found",
                "no_image",
                "rejected",
            }:
                LOG.debug(
                    "Wikipedia image cache hit for %s: %s",
                    person_name,
                    status,
                )
                return None

        # If Jellyfin already has a Wikipedia homepage, try it first, but
        # re-validate it against the current identity/context rules.
        homepage_title = self.wikipedia_title_from_homepage(
            str(person.get("HomePageUrl") or "")
        )

        candidate_titles: list[str] = []

        if homepage_title:
            candidate_titles.append(homepage_title)

        # Search exact name first, then role-aware fallbacks.
        queries = [
            f'"{person_name}"',
            person_name,
        ]

        for suffix in self.query_suffixes_for_contexts(contexts):
            queries.append(
                f'"{person_name}" {suffix}'
            )

        by_title: dict[str, dict[str, Any]] = {}

        for query in queries:
            pages = self.search_pages(query)

            for page in pages:
                score = self.page_search_score(
                    person_name,
                    page,
                    contexts,
                )

                if score <= 0:
                    continue

                title = str(
                    page.get("title") or ""
                ).strip()

                if not title:
                    continue

                current = by_title.get(
                    title.casefold()
                )

                if current is None:
                    by_title[title.casefold()] = page
                elif score > self.page_search_score(
                    person_name,
                    current,
                    contexts,
                ):
                    by_title[title.casefold()] = page

        ranked = sorted(
            by_title.values(),
            key=lambda page: self.page_search_score(
                person_name,
                page,
                contexts,
            ),
            reverse=True,
        )

        for page in ranked:
            title = str(
                page.get("title") or ""
            ).strip()

            if (
                title
                and title.casefold()
                not in {
                    value.casefold()
                    for value in candidate_titles
                }
            ):
                candidate_titles.append(title)

        for title in candidate_titles:
            summary = self.summary(title)

            if not summary:
                continue

            if not self.validate_summary(
                person_name,
                title,
                summary,
                contexts,
            ):
                LOG.info(
                    "Rejected Wikipedia summary for %s -> %s "
                    "(context=%s)",
                    person_name,
                    title,
                    ",".join(sorted(contexts)) or "unknown",
                )
                continue

            image_url = self.image_url_from_summary(
                summary
            )

            if not image_url:
                self.cache[cache_key] = {
                    "status": "no_image",
                    "person_id": person_id,
                    "person_name": person_name,
                    "wikipedia_title": title,
                }
                self.save_cache()
                return None

            result = {
                "status": "found",
                "person_id": person_id,
                "person_name": person_name,
                "wikipedia_title": title,
                "wikipedia_url": (
                    summary.get("content_urls", {})
                    .get("desktop", {})
                    .get("page", "")
                ),
                "description": summary.get("description") or "",
                "image_url": image_url,
            }

            self.cache[cache_key] = result
            self.save_cache()

            LOG.info(
                "Wikipedia image match: %s -> %s [%s]",
                person_name,
                title,
                image_url,
            )

            return result

        self.cache[cache_key] = {
            "status": "not_found",
            "person_id": person_id,
            "person_name": person_name,
            "contexts": sorted(contexts),
        }
        self.save_cache()

        LOG.info(
            "No safe Wikipedia image match for %s (context=%s)",
            person_name,
            ",".join(sorted(contexts)) or "unknown",
        )

        return None

    def download_image(
        self,
        image_url: str,
    ) -> tuple[bytes, str]:
        response = self.request(
            "GET",
            image_url,
            headers={
                "Accept": "image/*",
            },
        )

        content_type = (
            response.headers
            .get("Content-Type", "")
            .split(";", 1)[0]
            .strip()
            .casefold()
        )

        if not content_type.startswith("image/"):
            raise RuntimeError(
                f"Wikipedia image URL returned {content_type!r}, not image/*"
            )

        data = response.content

        if not data:
            raise RuntimeError(
                "Wikipedia image download returned zero bytes."
            )

        if len(data) > 25 * 1024 * 1024:
            raise RuntimeError(
                "Wikipedia image is larger than 25 MiB; refusing upload."
            )

        return data, content_type


# ---------------------------------------------------------------------------
# Jellyfin client
# ---------------------------------------------------------------------------

class Jellyfin:
    def __init__(
        self,
        url: str,
        api_key: str,
    ) -> None:
        self.url = url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "X-Emby-Token": api_key,
            "Accept": "application/json",
        })
        self.user_id: str | None = None

    def request(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> requests.Response:
        response = self.session.request(
            method,
            f"{self.url}{endpoint}",
            timeout=120,
            **kwargs,
        )

        if not response.ok:
            LOG.error(
                "Jellyfin %s %s returned HTTP %s: %s",
                method,
                endpoint,
                response.status_code,
                response.text[:2000] or "<empty>",
            )

        response.raise_for_status()
        return response

    def resolve_user_id(self) -> str:
        if self.user_id:
            return self.user_id

        users = self.request(
            "GET",
            "/Users",
        ).json()

        if not isinstance(users, list) or not users:
            raise RuntimeError(
                "Jellyfin returned no users."
            )

        selected = None

        for user in users:
            policy = user.get("Policy") or {}

            if policy.get("IsAdministrator"):
                selected = user
                break

        if selected is None:
            selected = users[0]

        user_id = str(
            selected.get("Id") or ""
        ).strip()

        if not user_id:
            raise RuntimeError(
                "Could not resolve Jellyfin user ID."
            )

        self.user_id = user_id

        LOG.info(
            "Using Jellyfin user %s (%s)",
            selected.get("Name") or "unknown",
            user_id,
        )

        return user_id

    def get_item(
        self,
        item_id: str,
    ) -> dict[str, Any]:
        user_id = self.resolve_user_id()

        return self.request(
            "GET",
            (
                f"/Users/{quote(user_id, safe='')}/Items/"
                f"{quote(item_id, safe='')}"
            ),
        ).json()

    def iter_persons(self):
        start = 0

        while True:
            data = self.request(
                "GET",
                "/Items",
                params={
                    "Recursive": "true",
                    "IncludeItemTypes": "Person",
                    "Fields": (
                        "Overview,ProviderIds,Path,"
                        "DateCreated"
                    ),
                    "EnableImages": "true",
                    "StartIndex": start,
                    "Limit": PAGE_SIZE,
                    "EnableTotalRecordCount": "true",
                },
            ).json()

            items = data.get("Items", [])

            for person in items:
                yield person

            start += len(items)
            total = int(
                data.get("TotalRecordCount", 0)
            )

            LOG.info(
                "Loaded %d / %d Person record(s)",
                start,
                total,
            )

            if not items or start >= total:
                break

    def build_person_role_context(
        self,
    ) -> tuple[
        dict[str, set[str]],
        dict[str, set[str]],
    ]:
        by_id: dict[str, set[str]] = {}
        by_name: dict[str, set[str]] = {}

        start = 0

        while True:
            data = self.request(
                "GET",
                "/Items",
                params={
                    "Recursive": "true",
                    "Fields": "People",
                    "StartIndex": start,
                    "Limit": PAGE_SIZE,
                    "EnableTotalRecordCount": "true",
                },
            ).json()

            items = data.get("Items", [])

            for item in items:
                if (
                    str(item.get("Type") or "")
                    .casefold()
                    == "person"
                ):
                    continue

                for person in item.get("People") or []:
                    person_id = str(
                        person.get("Id") or ""
                    ).strip().casefold()
                    name_key = normalized_name(
                        str(person.get("Name") or "")
                    )

                    role_values = {
                        str(person.get("Type") or "").strip(),
                        str(person.get("Role") or "").strip(),
                    }
                    role_values.discard("")

                    if person_id:
                        by_id.setdefault(
                            person_id,
                            set(),
                        ).update(role_values)

                    if name_key:
                        by_name.setdefault(
                            name_key,
                            set(),
                        ).update(role_values)

            start += len(items)
            total = int(
                data.get("TotalRecordCount", 0)
            )

            LOG.info(
                "Scanned People context in %d / %d library item(s)",
                start,
                total,
            )

            if not items or start >= total:
                break

        return by_id, by_name

    @staticmethod
    def has_primary_image(
        person: dict[str, Any],
    ) -> bool:
        tags = person.get("ImageTags") or {}

        if not isinstance(tags, dict):
            return False

        return bool(tags.get("Primary"))

    def get_image_infos(
        self,
        item_id: str,
    ) -> list[dict[str, Any]]:
        data = self.request(
            "GET",
            f"/Items/{quote(item_id, safe='')}/Images",
        ).json()

        return data if isinstance(data, list) else []

    def upload_primary_image(
        self,
        item_id: str,
        image_data: bytes,
        content_type: str,
    ) -> None:
        self.request(
            "POST",
            (
                f"/Items/{quote(item_id, safe='')}"
                "/Images/Primary"
            ),
            data=image_data,
            headers={
                "Content-Type": content_type,
                "Accept": "application/json",
            },
        )

    def verify_primary_image(
        self,
        item_id: str,
    ) -> bool:
        infos = self.get_image_infos(item_id)

        for info in infos:
            image_type = str(
                info.get("ImageType")
                or info.get("Type")
                or ""
            ).casefold()

            if image_type == "primary":
                return True

        # Fallback to the user-scoped DTO in case image-info casing differs.
        item = self.get_item(item_id)
        return self.has_primary_image(item)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Populate missing Jellyfin Person Primary images "
            "from Wikipedia using conservative identity/context matching."
        )
    )

    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Actually upload images. Without --apply this is a dry run."
        ),
    )

    parser.add_argument(
        "--name",
        action="append",
        default=[],
        help=(
            "Process only this exact Person name. Repeat for multiple names."
        ),
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help=(
            "Maximum number of no-image Person records to process. "
            "0 means no limit."
        ),
    )

    parser.add_argument(
        "--url",
        default=os.environ.get(
            "JELLYFIN_URL",
            DEFAULT_JELLYFIN_URL,
        ),
    )

    parser.add_argument(
        "--api-key",
        default=os.environ.get(
            "JELLYFIN_API_KEY",
            "",
        ),
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)

    LOG.info(
        "jellyfin_people_wikipedia_images.py v%s",
        VERSION,
    )
    LOG.info("Jellyfin URL: %s", args.url)
    LOG.info(
        "Mode: %s",
        "APPLY" if args.apply else "DRY RUN",
    )
    LOG.info("Cache: %s", CACHE_FILE)
    LOG.info("Log: %s", LOG_FILE)

    if not args.api_key:
        LOG.error(
            "Set JELLYFIN_API_KEY or pass --api-key."
        )
        return 2

    requested_names = {
        normalized_name(name)
        for name in args.name
        if name.strip()
    }

    jellyfin = Jellyfin(
        args.url,
        args.api_key,
    )
    wikipedia = Wikipedia()

    LOG.info(
        "Building Person role/context index from Jellyfin People references..."
    )
    roles_by_id, roles_by_name = (
        jellyfin.build_person_role_context()
    )

    persons = list(jellyfin.iter_persons())

    scanned = 0
    already_has_image = 0
    filtered_out = 0
    no_match = 0
    would_upload = 0
    uploaded = 0
    upload_failed = 0
    verification_failed = 0

    for person_summary in persons:
        person_name = str(
            person_summary.get("Name") or ""
        ).strip()
        person_id = str(
            person_summary.get("Id") or ""
        ).strip()

        if not person_name or not person_id:
            continue

        if requested_names and (
            normalized_name(person_name)
            not in requested_names
        ):
            filtered_out += 1
            continue

        if jellyfin.has_primary_image(person_summary):
            already_has_image += 1
            LOG.debug(
                "SKIP image exists: %s",
                person_name,
            )
            continue

        scanned += 1

        if args.limit > 0 and scanned > args.limit:
            break

        # Fetch the full Person DTO so HomePageUrl and current metadata are
        # available before Wikipedia selection.
        try:
            person = jellyfin.get_item(person_id)
        except requests.RequestException as exc:
            upload_failed += 1
            LOG.error(
                "Could not retrieve Person %s (%s): %s",
                person_name,
                person_id,
                exc,
            )
            continue

        if jellyfin.has_primary_image(person):
            already_has_image += 1
            continue

        role_values = set(
            roles_by_id.get(
                person_id.casefold(),
                set(),
            )
        )
        role_values.update(
            roles_by_name.get(
                normalized_name(person_name),
                set(),
            )
        )

        contexts = context_kinds_from_roles(
            role_values
        )

        LOG.info(
            "NO IMAGE: %s (%s), roles=%s, context=%s",
            person_name,
            person_id,
            sorted(role_values),
            sorted(contexts),
        )

        try:
            match = wikipedia.resolve(
                person,
                contexts,
            )
        except WikimediaRateLimitError:
            LOG.error(
                "Stopping run because Wikimedia requested rate limiting."
            )
            break
        except requests.RequestException as exc:
            no_match += 1
            LOG.error(
                "Wikipedia lookup failed for %s: %s",
                person_name,
                exc,
            )
            continue

        if not match:
            no_match += 1
            continue

        if not args.apply:
            would_upload += 1
            LOG.info(
                "DRY RUN: would upload Primary image for %s "
                "from Wikipedia page %s: %s",
                person_name,
                match.get("wikipedia_title"),
                match.get("image_url"),
            )
            continue

        try:
            image_data, content_type = (
                wikipedia.download_image(
                    str(match["image_url"])
                )
            )

            # Last-second race check: never overwrite a Primary image that
            # appeared while this run was working.
            latest_person = jellyfin.get_item(
                person_id
            )

            if jellyfin.has_primary_image(
                latest_person
            ):
                already_has_image += 1
                LOG.info(
                    "SKIP: Primary image appeared before upload for %s",
                    person_name,
                )
                continue

            jellyfin.upload_primary_image(
                person_id,
                image_data,
                content_type,
            )

            if not jellyfin.verify_primary_image(
                person_id
            ):
                verification_failed += 1
                LOG.error(
                    "Upload returned successfully but Primary image "
                    "verification failed for %s (%s)",
                    person_name,
                    person_id,
                )
                continue

            uploaded += 1
            LOG.info(
                "UPLOADED Primary image for %s from %s",
                person_name,
                match.get("wikipedia_url")
                or match.get("wikipedia_title"),
            )

        except WikimediaRateLimitError:
            LOG.error(
                "Stopping run because Wikimedia requested rate limiting."
            )
            break
        except (requests.RequestException, RuntimeError) as exc:
            upload_failed += 1
            LOG.error(
                "Image download/upload failed for %s: %s",
                person_name,
                exc,
            )

    LOG.info(
        "Finished: %d no-image Person(s) processed; "
        "%d already had Primary images; "
        "%d had no safe Wikipedia image match; "
        "%d would upload; "
        "%d uploaded; "
        "%d upload/download failure(s); "
        "%d verification failure(s).",
        scanned,
        already_has_image,
        no_match,
        would_upload,
        uploaded,
        upload_failed,
        verification_failed,
    )

    return 1 if (
        upload_failed
        or verification_failed
    ) else 0


if __name__ == "__main__":
    sys.exit(main())
