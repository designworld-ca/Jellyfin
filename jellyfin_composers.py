#!/usr/bin/env python3

"""
jellyfin_composers.py
created by ChatGpt, tested by me
REQUIREMENTS / BEHAVIOR
-----------------------
1. Scan MP3 files recursively and read the ID3 TCOM composer tag.
2. Split multiple composers separated by ';' into individual composer names.
3. Read composers from EVERY track so each album gets a complete composer set,
   including composers that occur only on single-composer tracks.
4. Add discovered composers to the album's album.nfo without removing or
   overwriting unrelated existing metadata.
5. Add composers to matching Jellyfin audio tracks as People with:
       Type = "Composer"
       Role = "Composer"
6. Add the union of all track composers to the matching Jellyfin MusicAlbum
   item as People with:
       Type = "Composer"
       Role = "Composer"
   Existing album People are preserved; only missing Composer links are added.
7. Never overwrite, append to, clean up, or otherwise modify an existing
   non-empty Jellyfin Person Overview.
8. Before querying Wikipedia, check whether the Jellyfin Person already has
   a non-empty Overview. If it does, skip Wikipedia entirely for that person.
9. Cache both successful and unsuccessful Wikimedia lookups locally.
10. Persist a mapping of:
       composer name -> Wikipedia page -> Wikidata QID -> biography summary
   so a previously resolved composer does not require another search.
11. Rate-limit ALL Wikimedia requests globally to at most 1 request/second.
12. Retry transient HTTP 5xx errors with bounded exponential backoff.
13. If Wikimedia returns HTTP 429, stop ALL further Wikipedia/Wikidata
    enrichment for the current run. Do not automatically continue.
14. Respect/log Retry-After if Wikimedia provides it.
15. Use a descriptive User-Agent for Wikimedia requests.
16. Use Wikidata QIDs as persistent external identities in the local cache.
    Do not write Wikidata IDs into Jellyfin ProviderIds unless support for
    that provider has been explicitly verified.
17. Back up an existing album.nfo once as album.nfo.bak before modifying it.
18. Support --dry-run so all intended changes can be inspected first.
19. Translate host filesystem paths to Jellyfin container paths so audio
    items can be matched reliably when Jellyfin runs in Docker.
20. Request a Jellyfin library refresh after metadata changes.
21. Do not overwrite existing Jellyfin Overview metadata under any condition.
22. Retrieve full track/album DTOs through the Jellyfin user-scoped endpoint
    /Users/{userId}/Items/{itemId}; this avoids HTTP 400 responses seen with
    the bare GET /Items/{itemId} endpoint on Jellyfin 10.11.x.
23. Requirements:
       Python 3.10+
       pip install mutagen requests

Typical environment:
    JELLYFIN_URL defaults to http://127.0.0.1:8096
    export JELLYFIN_API_KEY="your-api-key"
    export HOST_MUSIC_ROOT="/srv/media/music"
    export JELLYFIN_MUSIC_ROOT="/media/music"

Wikimedia identification:
    User-Agent contact: music@designworld.ca

Local files written beside this program:
    jellyfin-composer-wikipedia-cache.json
    jellyfin-composers.log

Usage:
    python3 jellyfin_composers.py --dry-run
    python3 jellyfin_composers.py
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET

from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import requests
from mutagen.id3 import ID3, ID3NoHeaderError


# ---------------------------------------------------------------------------
# USER CONFIGURATION
# ---------------------------------------------------------------------------
# Replace these dummy values with the settings for your Jellyfin server.
#
# HOST_MUSIC_ROOT:
#   The music path on the Ubuntu host.
#
# JELLYFIN_MUSIC_ROOT:
#   The same music directory as Jellyfin sees it inside the Docker container.
#
JELLYFIN_URL = "http://127.0.0.1:8096"
JELLYFIN_API_KEY = "PUT_YOUR_JELLYFIN_API_KEY_HERE"
HOST_MUSIC_ROOT = "/path/on/ubuntu/to/music"
JELLYFIN_MUSIC_ROOT = "/path/inside/jellyfin/container"
WIKIMEDIA_CONTACT = "music@designworld.ca"

VERSION = "1.4.0"

LOG = logging.getLogger("jellyfin-composers")

WIKIPEDIA_SEARCH_URL = "https://en.wikipedia.org/w/rest.php/v1/search/page"
WIKIPEDIA_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{}"
WIKIPEDIA_ACTION_API = "https://en.wikipedia.org/w/api.php"

PROGRAM_DIR = Path(__file__).resolve().parent
WIKI_CACHE_FILE = PROGRAM_DIR / "jellyfin-composer-wikipedia-cache.json"
LOG_FILE = PROGRAM_DIR / "jellyfin-composers.log"

COMPOSER_HINTS = (
    "composer",
    "musician",
    "songwriter",
    "conductor",
    "pianist",
    "violinist",
    "music",
    "singer",
    "producer",
    "arranger",
)


# ---------------------------------------------------------------------------
# General utilities
# ---------------------------------------------------------------------------

def split_composers(values: list[str]) -> list[str]:
    result: list[str] = []

    for value in values:
        for part in re.split(r"\s*;\s*", value):
            part = part.strip()

            if not part:
                continue

            if part.casefold() not in {x.casefold() for x in result}:
                result.append(part)

    return result


def read_mp3_composers(path: Path) -> list[str]:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        LOG.warning("No ID3 header: %s", path)
        return []
    except Exception as exc:
        LOG.error("Cannot read tags from %s: %s", path, exc)
        return []

    frame = tags.get("TCOM")

    if frame is None:
        return []

    values = [str(x).strip() for x in frame.text if str(x).strip()]
    return split_composers(values)


def normalized_path(path: str) -> str:
    return str(PurePosixPath(path)).rstrip("/").casefold()


# ---------------------------------------------------------------------------
# album.nfo handling
# ---------------------------------------------------------------------------

def indent_xml(element: ET.Element, level: int = 0) -> None:
    indentation = "\n" + ("  " * level)

    if len(element):
        if not element.text or not element.text.strip():
            element.text = indentation + "  "

        for child in element:
            indent_xml(child, level + 1)

        if not child.tail or not child.tail.strip():
            child.tail = indentation

    if level and (not element.tail or not element.tail.strip()):
        element.tail = indentation


def load_album_nfo(nfo: Path) -> tuple[ET.ElementTree, ET.Element]:
    if nfo.exists():
        try:
            tree = ET.parse(nfo)
            root = tree.getroot()
            return tree, root
        except ET.ParseError as exc:
            raise RuntimeError(f"Invalid XML in {nfo}: {exc}") from exc

    root = ET.Element("album")
    return ET.ElementTree(root), root


def update_album_nfo(
    nfo: Path,
    composers: list[str],
    dry_run: bool,
) -> bool:
    tree, root = load_album_nfo(nfo)

    existing = [
        (node.text or "").strip()
        for node in root.findall("composer")
        if (node.text or "").strip()
    ]

    combined = existing[:]

    for composer in composers:
        if composer.casefold() not in {x.casefold() for x in combined}:
            combined.append(composer)

    if [x.casefold() for x in existing] == [x.casefold() for x in combined]:
        return False

    LOG.info(
        "%s %s",
        "Would update" if dry_run else "Updating",
        nfo,
    )

    if dry_run:
        return True

    if nfo.exists():
        backup = nfo.with_suffix(nfo.suffix + ".bak")
        if not backup.exists():
            shutil.copy2(nfo, backup)

    for node in list(root.findall("composer")):
        root.remove(node)

    for composer in combined:
        node = ET.SubElement(root, "composer")
        node.text = composer

    indent_xml(root)

    tree.write(
        nfo,
        encoding="utf-8",
        xml_declaration=True,
    )

    return True


# ---------------------------------------------------------------------------
# Wikimedia
# ---------------------------------------------------------------------------

class WikimediaRateLimitError(RuntimeError):
    """Raised when Wikimedia explicitly asks this run to stop/slow down."""


class Wikipedia:
    """
    Conservative Wikipedia/Wikidata client.

    All requests made by this class share one limiter. A new composer may
    require multiple requests (search, summary, Wikidata QID), but each HTTP
    request is separated from the previous one by at least one second.
    """

    MIN_REQUEST_INTERVAL = 1.0
    MAX_5XX_RETRIES = 4
    INITIAL_BACKOFF = 2.0

    def __init__(self, cache_path: Path, user_agent: str):
        self.cache_path = cache_path
        self.session = requests.Session()

        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "application/json",
        })

        self.last_request_time = 0.0
        self.stopped = False
        self.cache: dict[str, dict[str, Any]] = {}

        if cache_path.exists():
            try:
                raw = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.cache = raw
            except Exception as exc:
                LOG.warning(
                    "Ignoring invalid Wikimedia cache %s: %s",
                    cache_path,
                    exc,
                )

    def save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

        tmp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")

        tmp.write_text(
            json.dumps(
                self.cache,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        tmp.replace(self.cache_path)

    def wait_for_rate_limit(self) -> None:
        elapsed = time.monotonic() - self.last_request_time
        remaining = self.MIN_REQUEST_INTERVAL - elapsed

        if remaining > 0:
            time.sleep(remaining)

    @staticmethod
    def _retry_after_seconds(response: requests.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None

        try:
            return float(value)
        except ValueError:
            return None

    def request(
        self,
        method: str,
        url: str,
        **kwargs,
    ) -> requests.Response:
        if self.stopped:
            raise WikimediaRateLimitError(
                "Wikimedia enrichment is already disabled for this run "
                "because a previous request returned HTTP 429."
            )

        backoff = self.INITIAL_BACKOFF

        for attempt in range(self.MAX_5XX_RETRIES + 1):
            self.wait_for_rate_limit()
            request_started = time.monotonic()

            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=30,
                    **kwargs,
                )
            except requests.RequestException as exc:
                self.last_request_time = request_started

                if attempt >= self.MAX_5XX_RETRIES:
                    raise

                LOG.warning(
                    "Wikimedia network error: %s. Retrying in %.1f seconds.",
                    exc,
                    backoff,
                )

                time.sleep(backoff)
                backoff *= 2
                continue

            self.last_request_time = request_started

            if response.status_code == 429:
                self.stopped = True
                retry_after = response.headers.get("Retry-After")

                if retry_after:
                    message = (
                        "Wikimedia returned HTTP 429 with Retry-After=%s. "
                        "Stopping all Wikipedia/Wikidata enrichment for "
                        "this run."
                    ) % retry_after
                else:
                    message = (
                        "Wikimedia returned HTTP 429. Stopping all "
                        "Wikipedia/Wikidata enrichment for this run."
                    )

                LOG.error(message)
                raise WikimediaRateLimitError(message)

            if 500 <= response.status_code <= 599:
                if attempt >= self.MAX_5XX_RETRIES:
                    response.raise_for_status()

                retry_after = self._retry_after_seconds(response)
                delay = retry_after if retry_after is not None else backoff

                LOG.warning(
                    "Wikimedia returned HTTP %d. Retrying in %.1f seconds "
                    "(attempt %d/%d).",
                    response.status_code,
                    delay,
                    attempt + 1,
                    self.MAX_5XX_RETRIES,
                )

                time.sleep(delay)
                backoff *= 2
                continue

            response.raise_for_status()
            return response

        raise RuntimeError("Unexpected Wikimedia request retry state")

    @staticmethod
    def score_result(composer: str, result: dict[str, Any]) -> int:
        title = str(result.get("title") or "")
        description = str(result.get("description") or "")
        excerpt = html.unescape(
            re.sub(
                r"<[^>]+>",
                "",
                str(result.get("excerpt") or ""),
            )
        )

        text = f"{title} {description} {excerpt}".casefold()
        score = 0

        if title.casefold() == composer.casefold():
            score += 100
        elif composer.casefold() in title.casefold():
            score += 50

        for word in COMPOSER_HINTS:
            if word in text:
                score += 10

        if "disambiguation" in text:
            score -= 100

        return score

    def search_wikipedia_page(
        self,
        composer: str,
    ) -> dict[str, Any] | None:
        response = self.request(
            "GET",
            WIKIPEDIA_SEARCH_URL,
            params={
                "q": f'"{composer}" composer',
                "limit": 8,
            },
        )

        pages = response.json().get("pages", [])

        if not pages:
            response = self.request(
                "GET",
                WIKIPEDIA_SEARCH_URL,
                params={
                    "q": composer,
                    "limit": 8,
                },
            )
            pages = response.json().get("pages", [])

        if not pages:
            return None

        pages.sort(
            key=lambda p: self.score_result(composer, p),
            reverse=True,
        )

        best = pages[0]

        if self.score_result(composer, best) <= 0:
            LOG.warning(
                "Wikipedia candidates for %s were not convincing enough.",
                composer,
            )
            return None

        return best

    def get_summary(
        self,
        title: str,
    ) -> dict[str, Any] | None:
        url = WIKIPEDIA_SUMMARY_URL.format(
            quote(title.replace(" ", "_"), safe="")
        )

        try:
            response = self.request("GET", url)
        except requests.HTTPError as exc:
            if (
                exc.response is not None
                and exc.response.status_code == 404
            ):
                return None
            raise

        data = response.json()

        if data.get("type") == "disambiguation":
            return None

        overview = (data.get("extract") or "").strip()

        if not overview:
            return None

        wikipedia_url = (
            data.get("content_urls", {})
            .get("desktop", {})
            .get("page", "")
        )

        return {
            "overview": overview,
            "wikipedia_url": wikipedia_url,
        }

    def get_wikidata_id(
        self,
        wikipedia_title: str,
    ) -> str | None:
        response = self.request(
            "GET",
            WIKIPEDIA_ACTION_API,
            params={
                "action": "query",
                "format": "json",
                "prop": "pageprops",
                "ppprop": "wikibase_item",
                "titles": wikipedia_title,
                "redirects": "1",
            },
        )

        pages = (
            response.json()
            .get("query", {})
            .get("pages", {})
        )

        for page in pages.values():
            wikidata_id = (
                page.get("pageprops", {})
                .get("wikibase_item")
            )

            if wikidata_id:
                return str(wikidata_id)

        return None

    def lookup(
        self,
        composer: str,
    ) -> dict[str, Any] | None:
        key = composer.casefold()

        if key in self.cache:
            cached = self.cache[key]

            if cached.get("status") == "not_found":
                LOG.debug("Negative Wikimedia cache hit: %s", composer)
                return None

            if cached.get("status") == "found":
                LOG.debug(
                    "Wikimedia cache hit: %s -> %s / %s",
                    composer,
                    cached.get("wikipedia_title"),
                    cached.get("wikidata_id"),
                )
                return cached

        LOG.info("Searching Wikipedia for %s", composer)

        page = self.search_wikipedia_page(composer)

        if not page:
            self.cache[key] = {
                "status": "not_found",
                "composer": composer,
            }
            self.save_cache()
            return None

        title = str(page.get("title") or "").strip()

        if not title:
            self.cache[key] = {
                "status": "not_found",
                "composer": composer,
            }
            self.save_cache()
            return None

        summary = self.get_summary(title)

        if not summary:
            self.cache[key] = {
                "status": "not_found",
                "composer": composer,
                "wikipedia_title": title,
            }
            self.save_cache()
            return None

        wikidata_id = self.get_wikidata_id(title)

        result = {
            "status": "found",
            "composer": composer,
            "wikipedia_title": title,
            "wikipedia_url": summary["wikipedia_url"],
            "wikidata_id": wikidata_id,
            "overview": summary["overview"],
        }

        self.cache[key] = result
        self.save_cache()

        LOG.info(
            "Resolved %s -> Wikipedia=%s, Wikidata=%s",
            composer,
            title,
            wikidata_id or "none",
        )

        return result


# ---------------------------------------------------------------------------
# Jellyfin
# ---------------------------------------------------------------------------

class Jellyfin:
    def __init__(
        self,
        url: str,
        api_key: str,
        host_root: Path,
        jellyfin_root: str,
        dry_run: bool,
    ):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.host_root = host_root.resolve()
        self.jellyfin_root = PurePosixPath(jellyfin_root)
        self.dry_run = dry_run

        self.session = requests.Session()
        self.session.headers.update({
            "X-Emby-Token": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

        self.audio_by_path: dict[str, dict[str, Any]] = {}
        self.album_by_path: dict[str, dict[str, Any]] = {}
        self.album_by_id: dict[str, dict[str, Any]] = {}
        self.user_id: str | None = None

    def request(
        self,
        method: str,
        endpoint: str,
        **kwargs,
    ) -> requests.Response:
        response = self.session.request(
            method,
            f"{self.url}{endpoint}",
            timeout=60,
            **kwargs,
        )

        if not response.ok:
            LOG.error(
                "Jellyfin %s %s returned %s:\n%s",
                method,
                endpoint,
                response.status_code,
                response.text[:2000],
            )

        response.raise_for_status()
        return response

    def host_to_jellyfin_path(self, host_path: Path) -> str:
        relative = host_path.resolve().relative_to(self.host_root)
        result = self.jellyfin_root.joinpath(*relative.parts)
        return str(result)

    def load_audio_items(self) -> None:
        LOG.info("Loading Jellyfin audio item index...")

        start = 0
        limit = 500

        fields = ",".join([
            "Path",
            "People",
            "Overview",
            "Genres",
            "SortName",
            "Studios",
            "Writer",
            "ProviderIds",
            "Tags",
            "DateCreated",
        ])

        while True:
            data = self.request(
                "GET",
                "/Items",
                params={
                    "Recursive": "true",
                    "IncludeItemTypes": "Audio",
                    "Fields": fields,
                    "StartIndex": start,
                    "Limit": limit,
                    "EnableTotalRecordCount": "true",
                },
            ).json()

            items = data.get("Items", [])

            for item in items:
                path = item.get("Path")

                if path:
                    self.audio_by_path[normalized_path(path)] = item

            start += len(items)
            total = int(data.get("TotalRecordCount", 0))

            LOG.info(
                "Loaded %d / %d audio items",
                start,
                total,
            )

            if not items or start >= total:
                break

    def load_album_items(self) -> None:
        LOG.info("Loading Jellyfin music album index...")

        start = 0
        limit = 500

        fields = ",".join([
            "Path",
            "People",
            "Overview",
            "Genres",
            "SortName",
            "Studios",
            "ProviderIds",
            "Tags",
            "DateCreated",
        ])

        while True:
            data = self.request(
                "GET",
                "/Items",
                params={
                    "Recursive": "true",
                    "IncludeItemTypes": "MusicAlbum",
                    "Fields": fields,
                    "StartIndex": start,
                    "Limit": limit,
                    "EnableTotalRecordCount": "true",
                },
            ).json()

            items = data.get("Items", [])

            for item in items:
                item_id = item.get("Id")
                path = item.get("Path")

                if item_id:
                    self.album_by_id[str(item_id)] = item

                if path:
                    self.album_by_path[normalized_path(path)] = item

            start += len(items)
            total = int(data.get("TotalRecordCount", 0))

            LOG.info("Loaded %d / %d music albums", start, total)

            if not items or start >= total:
                break

    def find_album_item(
        self,
        host_album_dir: Path,
    ) -> dict[str, Any] | None:
        jf_path = self.host_to_jellyfin_path(host_album_dir)
        return self.album_by_path.get(normalized_path(jf_path))

    def resolve_user_id(self) -> str:
        """
        Resolve a Jellyfin user for user-scoped item retrieval.

        Jellyfin 10.11 can return HTTP 400 for GET /Items/{itemId} even
        when the item ID is valid. Jellyfin's own Python API client retrieves
        item metadata through /Users/{userId}/Items/{itemId}, so we do the
        same here.
        """
        if self.user_id:
            return self.user_id

        users = self.request("GET", "/Users").json()

        if not isinstance(users, list) or not users:
            raise RuntimeError(
                "Jellyfin returned no users; cannot retrieve full item metadata."
            )

        selected = None
        for user in users:
            policy = user.get("Policy") or {}
            if policy.get("IsAdministrator"):
                selected = user
                break

        if selected is None:
            selected = users[0]

        user_id = str(selected.get("Id") or "").strip()
        if not user_id:
            raise RuntimeError("Could not determine a valid Jellyfin user ID.")

        self.user_id = user_id
        LOG.info(
            "Using Jellyfin user %s (%s) for full item metadata retrieval",
            selected.get("Name") or "unknown",
            user_id,
        )
        return user_id

    def get_item(self, item_id: str) -> dict[str, Any]:
        user_id = self.resolve_user_id()
        return self.request(
            "GET",
            (
                f"/Users/{quote(user_id, safe='')}/Items/"
                f"{quote(str(item_id), safe='')}"
            ),
        ).json()

    def find_audio_item(
        self,
        host_mp3: Path,
    ) -> dict[str, Any] | None:
        jf_path = self.host_to_jellyfin_path(host_mp3)
        return self.audio_by_path.get(normalized_path(jf_path))

    @staticmethod
    def merge_composers_into_people(
        item: dict[str, Any],
        composers: list[str],
    ) -> bool:
        people = list(item.get("People") or [])
        changed = False

        existing = {
            (
                str(person.get("Name") or "").casefold(),
                str(person.get("Type") or "").casefold(),
            )
            for person in people
        }

        for composer in composers:
            key = (composer.casefold(), "composer")

            if key in existing:
                continue

            people.append({
                "Name": composer,
                "Role": "Composer",
                "Type": "Composer",
            })

            existing.add(key)
            changed = True

        if changed:
            item["People"] = people

        return changed

    def update_item(self, item: dict[str, Any]) -> None:
        if self.dry_run:
            LOG.info(
                "Would update Jellyfin item %s (%s)",
                item.get("Name"),
                item.get("Id"),
            )
            return

        self.request(
            "POST",
            f"/Items/{item['Id']}",
            json=item,
        )

    def get_person(self, name: str) -> dict[str, Any] | None:
        try:
            response = self.request(
                "GET",
                f"/Persons/{quote(name, safe='')}",
            )
            return response.json()

        except requests.HTTPError as exc:
            if (
                exc.response is not None
                and exc.response.status_code == 404
            ):
                return None
            raise

    def update_person_overview(
        self,
        composer: str,
        overview: str,
        wikipedia_url: str = "",
        wikidata_id: str | None = None,
    ) -> bool:
        person = self.get_person(composer)

        if person is None:
            LOG.warning(
                "Jellyfin Person not found yet for %s",
                composer,
            )
            return False

        current = (person.get("Overview") or "").strip()

        # Absolute rule: never modify an existing non-empty Overview.
        if current:
            LOG.info(
                "Existing Overview for %s; leaving it unchanged.",
                composer,
            )
            return False

        if not overview.strip():
            return False

        person["Overview"] = overview.strip()

        if wikipedia_url and not (person.get("HomePageUrl") or "").strip():
            person["HomePageUrl"] = wikipedia_url

        # wikidata_id intentionally remains only in the local cache.

        if self.dry_run:
            LOG.info(
                "Would add biography for %s%s",
                composer,
                f" [{wikidata_id}]" if wikidata_id else "",
            )
            return True

        self.request(
            "POST",
            f"/Items/{person['Id']}",
            json=person,
        )

        LOG.info(
            "Added biography for %s%s",
            composer,
            f" [{wikidata_id}]" if wikidata_id else "",
        )

        return True

    def scan_library(self) -> None:
        if self.dry_run:
            LOG.info("Would request a Jellyfin library scan")
            return

        self.request("POST", "/Library/Refresh")
        LOG.info("Requested Jellyfin library scan")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Import multi-composer MP3 metadata into Jellyfin and enrich "
            "empty composer biographies from Wikipedia."
        )
    )

    parser.add_argument(
        "--music-root",
        default=HOST_MUSIC_ROOT,
        help=(
            "Host filesystem music directory. "
            "Env: HOST_MUSIC_ROOT or MUSIC_ROOT."
        ),
    )

    parser.add_argument(
        "--jellyfin-root",
        default=JELLYFIN_MUSIC_ROOT,
        help=(
            "Corresponding path inside Jellyfin container, e.g. /media/music. "
            "Env: JELLYFIN_MUSIC_ROOT."
        ),
    )

    parser.add_argument(
        "--jellyfin-url",
        default=JELLYFIN_URL,
    )

    parser.add_argument(
        "--api-key",
        default=JELLYFIN_API_KEY,
    )

    parser.add_argument(
        "--wikimedia-user-agent",
        default=os.environ.get(
            "WIKIMEDIA_USER_AGENT",
            (
                f"JellyfinComposerMetadata/{VERSION} "
                "(personal Jellyfin metadata utility; "
                f"contact: {WIKIMEDIA_CONTACT})"
            ),
        ),
        help=(
            "User-Agent sent to Wikimedia. Prefer including a contact "
            "email or URL. Env: WIKIMEDIA_USER_AGENT."
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show intended changes without modifying anything.",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    log_format = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s"
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(log_format)
    root_logger.addHandler(console_handler)

    file_handler = logging.FileHandler(
        LOG_FILE,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(log_format)
    root_logger.addHandler(file_handler)

    LOG.info("Log file: %s", LOG_FILE)
    LOG.info("Wikimedia cache: %s", WIKI_CACHE_FILE)

    if not args.music_root:
        LOG.error("Specify --music-root or HOST_MUSIC_ROOT.")
        return 2

    if not args.api_key:
        LOG.error("Specify --api-key or JELLYFIN_API_KEY.")
        return 2

    music_root = Path(args.music_root).resolve()

    if not music_root.is_dir():
        LOG.error("Music directory does not exist: %s", music_root)
        return 2

    jellyfin_root = (
        args.jellyfin_root
        if args.jellyfin_root
        else str(music_root)
    )

    wiki = Wikipedia(
        WIKI_CACHE_FILE,
        user_agent=args.wikimedia_user_agent,
    )

    jellyfin = Jellyfin(
        url=args.jellyfin_url,
        api_key=args.api_key,
        host_root=music_root,
        jellyfin_root=jellyfin_root,
        dry_run=args.dry_run,
    )

    album_composers: dict[Path, set[str]] = {}
    track_composers: dict[Path, list[str]] = {}

    LOG.info("Scanning MP3 files under %s", music_root)

    for mp3 in music_root.rglob("*.mp3"):
        composers = read_mp3_composers(mp3)

        if not composers:
            continue

        track_composers[mp3] = composers
        album_composers.setdefault(mp3.parent, set()).update(composers)

    LOG.info(
        "Found %d relevant tracks in %d album directories",
        len(track_composers),
        len(album_composers),
    )

    if not track_composers:
        LOG.info("Nothing to do.")
        return 0

    nfo_updates = 0

    for album_dir, composers in album_composers.items():
        if update_album_nfo(
            album_dir / "album.nfo",
            sorted(composers, key=str.casefold),
            args.dry_run,
        ):
            nfo_updates += 1

    jellyfin.load_audio_items()
    jellyfin.load_album_items()

    all_composers: set[str] = set()
    tracks_updated = 0
    tracks_not_found = 0

    for mp3, composers in track_composers.items():
        all_composers.update(composers)

        item = jellyfin.find_audio_item(mp3)

        if item is None:
            tracks_not_found += 1

            LOG.warning(
                "Could not match Jellyfin item for %s "
                "(expected Jellyfin path: %s)",
                mp3,
                jellyfin.host_to_jellyfin_path(mp3),
            )
            continue

        # Fetch the complete user-scoped DTO before changing People.
        # Jellyfin metadata updates are not PATCH operations; posting a
        # partial DTO can fail or unintentionally overwrite fields.
        full_track_item = jellyfin.get_item(str(item["Id"]))

        if jellyfin.merge_composers_into_people(
            full_track_item,
            composers,
        ):
            jellyfin.update_item(full_track_item)
            tracks_updated += 1

    albums_updated = 0
    albums_not_found = 0

    for album_dir, composers in album_composers.items():
        album_item = jellyfin.find_album_item(album_dir)

        if album_item is None:
            albums_not_found += 1
            LOG.warning(
                "Could not match Jellyfin MusicAlbum for %s "
                "(expected Jellyfin path: %s)",
                album_dir,
                jellyfin.host_to_jellyfin_path(album_dir),
            )
            continue

        # Fetch the current item before posting an update so we preserve
        # unrelated album metadata and existing People relationships.
        full_album_item = jellyfin.get_item(str(album_item["Id"]))

        if jellyfin.merge_composers_into_people(
            full_album_item,
            sorted(composers, key=str.casefold),
        ):
            jellyfin.update_item(full_album_item)
            albums_updated += 1

    if tracks_updated or albums_updated:
        jellyfin.scan_library()

        if not args.dry_run:
            # Allow Jellyfin a moment to materialize Person records.
            time.sleep(2)

    biographies_updated = 0
    wikipedia_skipped_existing = 0
    wikipedia_not_found = 0
    persons_not_found = 0
    wikimedia_stopped = False

    for composer in sorted(all_composers, key=str.casefold):
        # Most important optimization/safety rule:
        # inspect Jellyfin first and never query Wikimedia for a person
        # who already has an Overview.
        person = jellyfin.get_person(composer)

        if person is None:
            persons_not_found += 1
            LOG.warning(
                "Jellyfin Person not found for %s; skipping biography lookup.",
                composer,
            )
            continue

        existing_overview = (person.get("Overview") or "").strip()

        if existing_overview:
            wikipedia_skipped_existing += 1

            LOG.info(
                "Overview already exists for %s; "
                "leaving it untouched and skipping Wikipedia.",
                composer,
            )
            continue

        try:
            result = wiki.lookup(composer)

        except WikimediaRateLimitError as exc:
            LOG.error("Stopping Wikimedia enrichment: %s", exc)
            wikimedia_stopped = True
            break

        except requests.RequestException as exc:
            LOG.error(
                "Wikimedia lookup failed for %s: %s",
                composer,
                exc,
            )
            continue

        if not result:
            wikipedia_not_found += 1
            continue

        LOG.info(
            "Wikipedia match: %s -> %s [%s]",
            composer,
            result.get("wikipedia_title", "unknown"),
            result.get("wikidata_id") or "no Wikidata ID",
        )

        if jellyfin.update_person_overview(
            composer=composer,
            overview=result["overview"],
            wikipedia_url=result.get("wikipedia_url", ""),
            wikidata_id=result.get("wikidata_id"),
        ):
            biographies_updated += 1

    if biographies_updated:
        jellyfin.scan_library()

    LOG.info(
        "Finished: "
        "%d album.nfo file(s) updated; "
        "%d track(s) updated; "
        "%d album Composer relationship(s) updated; "
        "%d new biography/biographies added; "
        "%d existing biography/biographies preserved; "
        "%d Wikipedia lookup(s) had no result; "
        "%d Jellyfin Person record(s) not found; "
        "%d Jellyfin track(s) not matched; "
        "%d Jellyfin album(s) not matched; "
        "Wikimedia stopped=%s",
        nfo_updates,
        tracks_updated,
        albums_updated,
        biographies_updated,
        wikipedia_skipped_existing,
        wikipedia_not_found,
        persons_not_found,
        tracks_not_found,
        albums_not_found,
        wikimedia_stopped,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
