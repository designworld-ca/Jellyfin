#!/usr/bin/env python3

"""
Created by Chat GPT, tested by me
delete_orphan_malformed_jellyfin_people_v5.py

Deletes orphan malformed Jellyfin Person records.

A Person qualifies only when ALL of these are true:
1. Returned by Jellyfin /Items filtered with IncludeItemTypes=Person.
2. Name contains ';' OR '/'.
3. ImageTags is empty, missing, or null.
4. No scanned library item's People list still references the Person.

IMPORTANT:
Jellyfin Person items normally have metadata Paths such as:
    /config/data/metadata/People/A/Andre Prevert John Merder Joseph Kosma

Those are Jellyfin metadata paths, not music/media files, so this version DOES
NOT reject Person records simply because Path is populated.

--dry-run logs exactly what would be deleted and sends no DELETE requests.

Deletion endpoint:
    DELETE /Items/{person_id}

Requirements:
    Python 3.10+
    pip install requests
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


# ---------------------------------------------------------------------------
# USER CONFIGURATION
# ---------------------------------------------------------------------------

JELLYFIN_URL = "http://127.0.0.1:8091"
JELLYFIN_API_KEY = "PUT_YOUR_JELLYFIN_API_KEY_HERE"

PROGRAM_DIR = Path(__file__).resolve().parent
LOG_FILE = PROGRAM_DIR / "delete-orphan-malformed-jellyfin-people-v5.log"
PAGE_SIZE = 500

LOG = logging.getLogger("delete-orphan-malformed-jellyfin-people-v2")


class Jellyfin:
    def __init__(self, url: str, api_key: str, dry_run: bool):
        self.url = url.rstrip("/")
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.headers.update({
            "X-Emby-Token": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        response = self.session.request(
            method,
            f"{self.url}{endpoint}",
            timeout=120,
            **kwargs,
        )

        if not response.ok:
            LOG.error(
                "Jellyfin %s %s returned HTTP %s:\n%s",
                method,
                endpoint,
                response.status_code,
                response.text[:2000],
            )

        response.raise_for_status()
        return response

    def iter_persons(self):
        """
        Enumerate Person items through the general /Items endpoint.

        This avoids relying on /Persons, which on this server returns only a
        subset of Person records.
        """
        start = 0

        while True:
            data = self.request(
                "GET",
                "/Items",
                params={
                    "Recursive": "true",
                    "IncludeItemTypes": "Person",
                    "StartIndex": start,
                    "Limit": PAGE_SIZE,
                    "Fields": "Path,Overview,People",
                    "EnableImages": "true",
                    "EnableTotalRecordCount": "true",
                },
            ).json()

            items = data.get("Items", [])

            for person in items:
                yield person

            start += len(items)
            total = int(data.get("TotalRecordCount", 0))

            LOG.info(
                "Loaded %d / %d Person item(s) via /Items",
                start,
                total,
            )

            if not items or start >= total:
                break

    def iter_library_items_with_people(self):
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
                yield item

            start += len(items)
            total = int(data.get("TotalRecordCount", 0))

            LOG.info(
                "Checked People references in %d / %d library item(s)",
                start,
                total,
            )

            if not items or start >= total:
                break

    def delete_person(self, person: dict[str, Any]) -> None:
        person_id = str(person.get("Id") or "").strip()
        name = str(person.get("Name") or "").strip()

        if not person_id:
            raise RuntimeError(f"Cannot delete Person without ID: {name!r}")

        if self.dry_run:
            LOG.info(
                "DRY RUN: would DELETE Person '%s' (%s), Path=%s",
                name,
                person_id,
                person.get("Path"),
            )
            return

        # Delete one Person item using Jellyfin's single-item endpoint.
        # This is the route supported by the server used with this utility.
        response = self.session.delete(
            f"{self.url}/Items/{quote(person_id, safe='')}",
            timeout=120,
        )

        if not response.ok:
            LOG.error(
                "DELETE failed for Person '%s' (%s): HTTP %s; response=%s",
                name,
                person_id,
                response.status_code,
                response.text[:4000] or "<empty>",
            )
            response.raise_for_status()

        LOG.info(
            "DELETED Person '%s' (%s), Path=%s",
            name,
            person_id,
            person.get("Path"),
        )


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

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Delete orphan malformed Jellyfin Person records whose names "
            "contain ';' or '/' and whose ImageTags are empty."
        )
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log qualifying records without deleting them.",
    )

    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def malformed_name(person: dict[str, Any]) -> bool:
    name = str(person.get("Name") or "").strip()
    return ";" in name or "/" in name


def image_tags_empty(person: dict[str, Any]) -> bool:
    tags = person.get("ImageTags")

    if tags is None:
        return True

    if isinstance(tags, dict):
        return len(tags) == 0

    # Fail safe for unexpected values.
    return False


def build_references(
    jellyfin: Jellyfin,
) -> tuple[set[str], set[str]]:
    referenced_ids: set[str] = set()
    referenced_names: set[str] = set()

    for item in jellyfin.iter_library_items_with_people():
        for person in item.get("People") or []:
            pid = str(person.get("Id") or "").strip()
            name = str(person.get("Name") or "").strip()

            if pid:
                referenced_ids.add(pid.casefold())

            if name:
                referenced_names.add(name.casefold())

    return referenced_ids, referenced_names


def is_referenced(
    person: dict[str, Any],
    referenced_ids: set[str],
    referenced_names: set[str],
) -> bool:
    pid = str(person.get("Id") or "").strip()
    name = str(person.get("Name") or "").strip()

    if pid and pid.casefold() in referenced_ids:
        return True

    if name and name.casefold() in referenced_names:
        return True

    return False


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)

    LOG.info("Jellyfin URL: %s", JELLYFIN_URL)
    LOG.info("Log file: %s", LOG_FILE)
    LOG.info("Mode: %s", "DRY RUN" if args.dry_run else "DELETE")

    if (
        not JELLYFIN_API_KEY
        or JELLYFIN_API_KEY == "PUT_YOUR_JELLYFIN_API_KEY_HERE"
    ):
        LOG.error("Set JELLYFIN_API_KEY in USER CONFIGURATION first.")
        return 2

    jellyfin = Jellyfin(
        JELLYFIN_URL,
        JELLYFIN_API_KEY,
        args.dry_run,
    )

    persons_scanned = 0
    malformed_count = 0
    images_skipped = 0
    candidates: list[dict[str, Any]] = []

    LOG.info("Scanning Jellyfin Person items via /Items...")

    for person in jellyfin.iter_persons():
        persons_scanned += 1

        if str(person.get("Type") or "").casefold() != "person":
            LOG.warning(
                "SKIP unexpected item type from Person query: %s '%s' (%s)",
                person.get("Type"),
                person.get("Name"),
                person.get("Id"),
            )
            continue

        if not malformed_name(person):
            continue

        malformed_count += 1

        name = str(person.get("Name") or "").strip()
        pid = str(person.get("Id") or "").strip()

        LOG.info(
            "MALFORMED Person found: '%s' (%s), Type=%s, ImageTags=%s, Path=%s",
            name,
            pid,
            person.get("Type"),
            person.get("ImageTags"),
            person.get("Path"),
        )

        if not image_tags_empty(person):
            images_skipped += 1
            LOG.info(
                "SKIP image present: '%s' (%s), ImageTags=%s",
                name,
                pid,
                person.get("ImageTags"),
            )
            continue

        LOG.info(
            "CANDIDATE malformed + no image: '%s' (%s), Path=%s",
            name,
            pid,
            person.get("Path"),
        )

        candidates.append(person)

    LOG.info(
        "Preliminary candidates: %d malformed Person record(s) with empty ImageTags",
        len(candidates),
    )

    if not candidates:
        LOG.info("Nothing qualifies.")
        return 0

    LOG.info(
        "Scanning library People relationships to verify orphan status..."
    )

    referenced_ids, referenced_names = build_references(jellyfin)

    orphans: list[dict[str, Any]] = []
    referenced_skipped = 0

    for person in candidates:
        name = str(person.get("Name") or "").strip()
        pid = str(person.get("Id") or "").strip()

        if is_referenced(
            person,
            referenced_ids,
            referenced_names,
        ):
            referenced_skipped += 1
            LOG.warning(
                "SKIP still referenced: '%s' (%s)",
                name,
                pid,
            )
            continue

        orphans.append(person)

    LOG.info(
        "%d qualifying orphan malformed Person record(s) found",
        len(orphans),
    )

    deleted = 0
    failed = 0

    for person in orphans:
        name = str(person.get("Name") or "").strip()
        pid = str(person.get("Id") or "").strip()

        LOG.info(
            "QUALIFIES: '%s' (%s), ImageTags=%s, Path=%s",
            name,
            pid,
            person.get("ImageTags"),
            person.get("Path"),
        )

        try:
            jellyfin.delete_person(person)
            deleted += 1

        except requests.RequestException as exc:
            failed += 1
            LOG.error(
                "Failed to delete '%s' (%s): %s",
                name,
                pid,
                exc,
            )

    LOG.info(
        "Finished: %d Person(s) scanned; "
        "%d malformed name(s); "
        "%d skipped because images exist; "
        "%d skipped because still referenced; "
        "%d qualifying orphan(s); "
        "%d %s; "
        "%d failure(s).",
        persons_scanned,
        malformed_count,
        images_skipped,
        referenced_skipped,
        len(orphans),
        deleted,
        "would be deleted" if args.dry_run else "deleted",
        failed,
    )

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
