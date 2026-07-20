"""Shared GitHub REST plumbing for docs_source and issues_source.

Resilience ported from linc-llm's github_source: timeouts, backoff on 5xx and
transport errors, SSO hint on 403, polite pacing.
"""
import functools
import os
import sys
import time

import requests

from .. import common

API = "https://api.github.com"


@functools.cache
def headers() -> dict:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": common.user_agent(),
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    else:
        print("note: GITHUB_TOKEN not set — unauthenticated (public repos only, "
              "60 req/hr limit). Fine for a small dry run; set it in .env for "
              "harvest.", file=sys.stderr)
    return h


def get(path: str, **params):
    for attempt in range(4):
        try:
            resp = requests.get(f"{API}{path}", headers=headers(),
                                params=params or None, timeout=30)
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)  # transient network hiccup; retry
            continue
        time.sleep(0.05)  # be gentle
        if resp.status_code >= 500 and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 403 and "X-GitHub-SSO" in resp.headers:
            sys.exit("403: token isn't authorized for the org's SAML SSO — "
                     "authorize it in the token's settings.")
        resp.raise_for_status()
        return resp.json()


def paginate(path: str, **params) -> list:
    """Every item from a paged list endpoint (per_page=100)."""
    items, page = [], 1
    while True:
        batch = get(path, per_page=100, page=page, **params)
        if not batch:
            return items
        items.extend(batch)
        page += 1
