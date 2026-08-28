#!/usr/bin/env python3
"""
Seed peptide stack/blend JSON files into the database.

Each JSON file follows:
  { "stack": {...}, "components": [...], "references": [...] }

`stack.id` is a slug (e.g. "klow", "ipamorelin-cjc-1295-no-dac"). Each entry
in `components` must reference a peptide_id that already exists (run
seed_peptides.py first). `stack_type` drives the Layer C ratio-source rule:
`commercial_blend` rows must carry `ratio_source_type`; `research_pairing`
rows must not carry any ratio/source fields — both are enforced at the DB
level by CHECK constraints, so a malformed file fails loudly here.

Usage (from peptora-api/ directory):
    python scripts/seed_stacks.py                          # all *.json in docs/stacks/
    python scripts/seed_stacks.py docs/stacks/klow.json     # one file
"""
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models import Peptide, PeptideStack, StackComponent, StackReference


def _parse_date(val) -> date | None:
    if val is None:
        return None
    if isinstance(val, date):
        return val
    return date.fromisoformat(str(val))


def _build_stack(data: dict) -> PeptideStack:
    s = data["stack"]
    return PeptideStack(
        id=s["id"],
        name=s["name"],
        aliases=s.get("aliases", []),
        stack_type=s["stack_type"],
        category=s.get("category"),
        positioning=s.get("positioning"),
        rationale=s.get("rationale"),
        evidence_level=s.get("evidence_level", "anecdotal"),
        is_recommendation=False,
        ratio_source_type=s.get("ratio_source_type"),
        ratio_source_note=s.get("ratio_source_note"),
        ratio_source_urls=s.get("ratio_source_urls", []),
        common_total_mg_options=s.get("common_total_mg_options", []),
        caution_notes=s.get("caution_notes", []),
        disclaimer=s.get("disclaimer"),
        last_reviewed=_parse_date(s.get("last_reviewed")) or date.today(),
        reviewed_by=s.get("reviewed_by"),
        content_version=s.get("content_version", 1),
        data_completeness=s.get("data_completeness", "stub"),
    )


def _build_components(stack_id: str, data: dict) -> list[StackComponent]:
    return [
        StackComponent(
            stack_id=stack_id,
            peptide_id=c["peptide_id"],
            sort_order=c.get("sort_order", 0),
            ratio_parts=c.get("ratio_parts"),
            typical_mg_share=c.get("typical_mg_share"),
            role=c.get("role"),
            dose_note=c.get("dose_note"),
        )
        for c in data.get("components", [])
    ]


def _build_references(stack_id: str, data: dict) -> list[StackReference]:
    return [
        StackReference(
            stack_id=stack_id,
            ref_id=r["ref_id"],
            type=r["type"],
            title=r["title"],
            first_author=r.get("first_author"),
            year=r.get("year"),
            source=r.get("source"),
            pmid=r.get("pmid"),
            doi=r.get("doi"),
            url=r.get("url"),
        )
        for r in data.get("references", [])
    ]


async def _seed_one(session: AsyncSession, path: Path, known_peptides: set[str]) -> bool:
    """Seed one stack file. Returns False when it was skipped.

    Components are checked against `known_peptides` up front: session.add()
    only stages the row, so a missing-FK error surfaces later at autoflush and
    takes the whole transaction with it. A stack is skipped whole rather than
    partially — a two-peptide blend seeded with one component is not a smaller
    blend, it is a wrong one.
    """
    data = json.loads(path.read_text())
    stack_id = data["stack"]["id"]

    missing = sorted(
        {
            c["peptide_id"]
            for c in data.get("components", [])
            if c.get("peptide_id") not in known_peptides
        }
    )
    if missing:
        print(f"  SKIP {stack_id:<30} missing peptide(s): {', '.join(missing)}")
        return False

    await session.merge(_build_stack(data))

    # components/references have no stable natural key in the JSON beyond the
    # parent stack, so delete-then-insert is the safest idempotent strategy.
    await session.execute(delete(StackComponent).where(StackComponent.stack_id == stack_id))
    for comp in _build_components(stack_id, data):
        session.add(comp)

    await session.execute(delete(StackReference).where(StackReference.stack_id == stack_id))
    for ref in _build_references(stack_id, data):
        session.add(ref)

    n = lambda key: len(data.get(key, []))
    print(f"  {stack_id:<30} components={n('components')}  references={n('references')}")
    return True


async def main(paths: list[Path]) -> None:
    if not paths:
        print("No JSON files to seed.")
        return

    print(f"\nSeeding {len(paths)} stack file(s)...\n")
    async with AsyncSessionLocal() as session:
        known_peptides = set((await session.scalars(select(Peptide.id))).all())
        seeded = 0
        for path in paths:
            if await _seed_one(session, path, known_peptides):
                seeded += 1
        await session.commit()

    skipped = len(paths) - seeded
    print(f"\nDone. {seeded} stack(s) seeded successfully." + (f" {skipped} skipped." if skipped else ""))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        file_paths = [Path(p).resolve() for p in sys.argv[1:]]
    else:
        stacks_dir = Path(__file__).resolve().parents[1] / "docs" / "stacks"
        file_paths = sorted(stacks_dir.glob("*.json"))
        if not file_paths:
            print(f"No *.json files found in {stacks_dir}")
            sys.exit(0)

    asyncio.run(main(file_paths))
