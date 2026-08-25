#!/usr/bin/env python3

"""
cleanup_jellyfin_composer_people.py
Created by ChatGpt, tested by me
PURPOSE
-------
Remove malformed Jellyfin Composer People relationships whose names contain
"/" or ";", while preserving all other People entries and all Overview data.

Processes:
- Audio tracks
- MusicAlbum items

Run with --dry-run first.

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
LOG_FILE = PROGRAM_DIR / "cleanup-jellyfin-composer-people.log"

LOG = logging.getLogger("cleanup-jellyfin-composer-people")


class Jellyfin:
    def __init__(self, url: str, api_key: str, dry_run: bool):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.dry_run = dry_run
        self.user_id: str | None = None

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

    def resolve_user_id(self) -> str:
        if self.user_id:
            return self.user_id

        users = self.request("GET", "/Users").json()

        if not isinstance(users, list) or not users:
            raise RuntimeError(
                "Jellyfin returned no users; cannot retrieve full item metadata."
            )

        selected = next(
            (
                user
                for user in users
                if (user.get("Policy") or {}).get("IsAdministrator")
            ),
            users[0],
        )

        user_id = str(selected.get("Id") or "").strip()

        if not user_id:
            raise RuntimeError("Could not determine a valid Jellyfin user ID.")

        self.user_id = user_id

        LOG.info(
            "Using Jellyfin user %s (%s)",
            selected.get("Name") or "unknown",
            user_id,
        )

        return user_id

    def get_item(self, item_id: str) -> dict[str, Any]:
        user_id = self.resolve_user_id()

        return self.request(
            "GET",
            f"/Users/{quote(user_id, safe='')}/Items/{quote(item_id, safe='')}",
        ).json()

    def update_item(self, item: dict[str, Any]) -> None:
        if self.dry_run:
            LOG.info(
                "DRY RUN: would update %s '%s' (%s)",
                item.get("Type") or "item",
                item.get("Name") or "unnamed",
                item.get("Id") or "unknown",
            )
            return

        self.request(
            "POST",
            f"/Items/{item['Id']}",
            json=item,
        )

    def iter_items(self, include_item_type: str):
        start = 0
        limit = 500

        while True:
            data = self.request(
                "GET",
                "/Items",
                params={
                    "Recursive": "true",
                    "IncludeItemTypes": include_item_type,
                    "Fields": "People,Path",
                    "StartIndex": start,
                    "Limit": limit,
                    "EnableTotalRecordCount": "true",
                },
            ).json()

            items = data.get("Items", [])

            for item in items:
                yield item

            start += len(items)
            total = int(data.get("TotalRecordCount", 0))

            LOG.info(
                "Indexed %d / %d %s item(s)",
                start,
                total,
                include_item_type,
            )

            if not items or start >= total:
                break

    @staticmethod
    def remove_malformed_composer_people(
        item: dict[str, Any],
    ) -> tuple[bool, list[str]]:
        people = list(item.get("People") or [])
        cleaned_people: list[dict[str, Any]] = []
        removed_names: list[str] = []

        for person in people:
            name = str(person.get("Name") or "").strip()
            person_type = str(person.get("Type") or "").strip()

            malformed = (
                person_type.casefold() == "composer"
                and ("/" in name or ";" in name)
            )

            if malformed:
                removed_names.append(name)
                continue

            cleaned_people.append(person)

        if len(cleaned_people) == len(people):
            return False, []

        item["People"] = cleaned_people
        return True, removed_names

    def scan_library(self) -> None:
        if self.dry_run:
            LOG.info("DRY RUN: would request Jellyfin library scan")
            return

        self.request("POST", "/Library/Refresh")
        LOG.info("Requested Jellyfin library scan")


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
            "Remove malformed Jellyfin Composer People entries whose names "
            "contain '/' or ';'."
        )
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without changing Jellyfin.",
    )
    parser.add_argument(
        "--tracks-only",
        action="store_true",
        help="Only clean Audio tracks.",
    )
    parser.add_argument(
        "--albums-only",
        action="store_true",
        help="Only clean MusicAlbum items.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
    )

    return parser.parse_args()


def clean_item_group(
    jellyfin: Jellyfin,
    item_type: str,
) -> tuple[int, int]:
    items_changed = 0
    people_removed = 0

    LOG.info("Scanning Jellyfin %s items...", item_type)

    for indexed_item in jellyfin.iter_items(item_type):
        item_id = str(indexed_item.get("Id") or "").strip()

        if not item_id:
            continue

        full_item = jellyfin.get_item(item_id)

        changed, removed = jellyfin.remove_malformed_composer_people(
            full_item
        )

        if not changed:
            continue

        LOG.info(
            "Removing malformed Composer People from %s '%s': %s",
            full_item.get("Type") or item_type,
            full_item.get("Name") or "unnamed",
            " | ".join(removed),
        )

        jellyfin.update_item(full_item)

        items_changed += 1
        people_removed += len(removed)

    return items_changed, people_removed


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)

    LOG.info("Log file: %s", LOG_FILE)
    LOG.info("Jellyfin URL: %s", JELLYFIN_URL)

    if (
        not JELLYFIN_API_KEY
        or JELLYFIN_API_KEY == "PUT_YOUR_JELLYFIN_API_KEY_HERE"
    ):
        LOG.error(
            "Set JELLYFIN_API_KEY in the USER CONFIGURATION section first."
        )
        return 2

    if args.tracks_only and args.albums_only:
        LOG.error("Use only one of --tracks-only or --albums-only.")
        return 2

    jellyfin = Jellyfin(
        url=JELLYFIN_URL,
        api_key=JELLYFIN_API_KEY,
        dry_run=args.dry_run,
    )

    total_items_changed = 0
    total_people_removed = 0

    if not args.albums_only:
        changed, removed = clean_item_group(jellyfin, "Audio")
        total_items_changed += changed
        total_people_removed += removed

    if not args.tracks_only:
        changed, removed = clean_item_group(jellyfin, "MusicAlbum")
        total_items_changed += changed
        total_people_removed += removed

    if total_items_changed:
        jellyfin.scan_library()

    LOG.info(
        "Finished: %d item(s) changed; %d malformed Composer People "
        "entr%s removed%s.",
        total_items_changed,
        total_people_removed,
        "y" if total_people_removed == 1 else "ies",
        " (dry run)" if args.dry_run else "",
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
