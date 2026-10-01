"""List library entries that would be shown without any sources.

App Store guideline 1.4.1 expects health and medical information to cite where
it comes from, and both apps show a "Sources" section on an entry only when
the entry has references. An entry with none is published with no citation at
all. This script finds those entries so they can be given sources, or taken
out of the library, before a build goes to App Review.

It reads the public library endpoints, exactly as the apps do. It changes
nothing, needs no login and no database access.

Usage (from peptora-api/):
    python -m scripts.check_library_sources                         # production
    python -m scripts.check_library_sources http://localhost:8000   # a local API

Exit code 0 when every entry has at least one source with a link, 1 otherwise.
"""

import asyncio
import sys

import httpx

DEFAULT_API = "https://api.peptora.io"

# The library endpoints allow 60 requests a minute per IP. One request at a
# time with a short pause stays under that for a library of any size.
PAUSE_SECONDS = 1.1


def _has_link(ref: dict) -> bool:
    return bool(ref.get("url") or ref.get("pmid") or ref.get("doi"))


async def _get(client: httpx.AsyncClient, path: str):
    for _ in range(3):
        res = await client.get(path)
        if res.status_code == 429:
            await asyncio.sleep(20)
            continue
        res.raise_for_status()
        return res.json()
    raise RuntimeError(f"still rate limited on {path}")


async def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_API).rstrip("/")
    print(f"Checking {base}")
    missing: list[str] = []
    unlinked: list[str] = []

    async with httpx.AsyncClient(base_url=base, timeout=30) as client:
        peptides = await _get(client, "/peptides")
        stacks = await _get(client, "/stacks")
        print(f"{len(peptides)} peptides, {len(stacks)} stacks")

        for card in peptides:
            await asyncio.sleep(PAUSE_SECONDS)
            detail = await _get(client, f"/peptides/{card['id']}")
            refs = detail.get("references") or []
            if not refs:
                missing.append(f"peptide  {card['id']}  ({card.get('name')})")
            elif not any(_has_link(r) for r in refs):
                unlinked.append(f"peptide  {card['id']}  ({card.get('name')})")

        for card in stacks:
            await asyncio.sleep(PAUSE_SECONDS)
            detail = await _get(client, f"/stacks/{card['id']}")
            refs = detail.get("stack_references") or []
            urls = detail.get("ratio_source_urls") or []
            if not refs and not urls:
                missing.append(f"stack    {card['id']}  ({card.get('name')})")
            elif not urls and not any(_has_link(r) for r in refs):
                unlinked.append(f"stack    {card['id']}  ({card.get('name')})")

    if missing:
        print(f"\n{len(missing)} entries have NO sources and show no Sources section:")
        for line in missing:
            print(f"  {line}")
    if unlinked:
        print(f"\n{len(unlinked)} entries list sources, but none of them has a link:")
        for line in unlinked:
            print(f"  {line}")
    if not missing and not unlinked:
        print("\nEvery entry has at least one source with a link.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
