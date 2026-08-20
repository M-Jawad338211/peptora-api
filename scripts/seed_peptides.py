#!/usr/bin/env python3
"""
Seed peptide knowledge-base JSON files into the database.

Each JSON file must follow the same structure as docs/dsip.json:
  { "peptide": {...}, "references": [...], "dose_ranges": [...],
    "protocols": [...], "related": [...] }

Peptide combinations (research pairings / commercial blends) are no longer
embedded here — see scripts/seed_stacks.py and docs/stacks/*.json.

Usage (from peptora-api/ directory):
    python scripts/seed_peptides.py                       # all *.json in docs/
    python scripts/seed_peptides.py docs/dsip.json         # one file
    python scripts/seed_peptides.py docs/*.json            # glob (shell expands)

Two-pass design:
  Pass 1 — peptides, references, dose_ranges, protocols  (no cross-FK deps)
  Pass 2 — related  (references other peptide ids; requires pass 1 done)
"""
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

# Allow `from app.xxx import yyy` when run from peptora-api/scripts/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models import (
    Peptide,
    PeptideDoseRange,
    PeptideProtocol,
    PeptideReference,
    PeptideRelated,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(val) -> date | None:
    if val is None:
        return None
    if isinstance(val, date):
        return val
    return date.fromisoformat(str(val))


# ---------------------------------------------------------------------------
# Builders — JSON → ORM objects
# ---------------------------------------------------------------------------

def _build_peptide(data: dict) -> Peptide:
    p = data["peptide"]
    return Peptide(
        id=p["id"],
        name=p["name"],
        aliases=p.get("aliases", []),
        tags=p.get("tags", []),
        category=p["category"],
        usage_category=p.get("usage_category"),
        approval_category=p.get("approval_category"),
        summary=p["summary"],
        description=p.get("description"),
        mechanism_of_action=p.get("mechanism_of_action"),
        mechanism_citation_refs=p.get("mechanism_citation_refs", []),
        molecular_weight=p.get("molecular_weight"),
        molecular_formula=p.get("molecular_formula"),
        cas_number=p.get("cas_number"),
        pubchem_cid=p.get("pubchem_cid"),
        sequence=p.get("sequence"),
        sequence_type=p.get("sequence_type"),
        half_life=p.get("half_life"),
        bioavailability=p.get("bioavailability"),
        routes=p.get("routes", []),
        default_dose_unit=p.get("default_dose_unit"),
        iu_per_mg=p.get("iu_per_mg"),
        evidence_level=p.get("evidence_level", "unknown"),
        human_trials=p.get("human_trials", False),
        clinical_trials_count=p.get("clinical_trials_count", 0),
        evidence_note=p.get("evidence_note"),
        fda_status=p.get("fda_status", "unknown"),
        fda_status_note=p.get("fda_status_note"),
        compounding_status=p.get("compounding_status"),
        compounding_note=p.get("compounding_note"),
        wada_status=p.get("wada_status"),
        scheduled_controlled=p.get("scheduled_controlled", False),
        research_only=p.get("research_only", True),
        regulatory_citation_refs=p.get("regulatory_citation_refs", []),
        benefits=p.get("benefits", []),
        risks=p.get("risks", []),
        side_effects=p.get("side_effects", []),
        contraindications=p.get("contraindications", []),
        interactions=p.get("interactions", []),
        reconstitution=p.get("reconstitution"),
        storage=p.get("storage"),
        last_reviewed=_parse_date(p.get("last_reviewed")) or date.today(),
        reviewed_by=p.get("reviewed_by"),
        content_version=p.get("content_version", 1),
        data_completeness=p.get("data_completeness", "stub"),
        disclaimer=p.get("disclaimer"),
        # search_tsv is maintained by a DB trigger — never set here
    )


def _build_references(peptide_id: str, data: dict) -> list[PeptideReference]:
    return [
        PeptideReference(
            peptide_id=peptide_id,
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


def _build_dose_ranges(peptide_id: str, data: dict) -> list[PeptideDoseRange]:
    return [
        PeptideDoseRange(
            peptide_id=peptide_id,
            context=d["context"],
            low=d.get("low"),
            high=d.get("high"),
            unit=d["unit"],
            route=d.get("route"),
            frequency=d.get("frequency"),
            note=d.get("note"),
            citation_refs=d.get("citation_refs", []),
        )
        for d in data.get("dose_ranges", [])
    ]


def _build_protocols(peptide_id: str, data: dict) -> list[PeptideProtocol]:
    return [
        PeptideProtocol(
            id=proto["id"],
            peptide_id=peptide_id,
            name=proto["name"],
            description=proto.get("description"),
            phase=proto.get("phase"),
            duration_weeks=proto.get("duration_weeks"),
            dosing=proto.get("dosing"),
            cycling_notes=proto.get("cycling_notes"),
            is_recommendation=proto.get("is_recommendation", False),
            disclaimer=proto.get("disclaimer"),
            citation_refs=proto.get("citation_refs", []),
        )
        for proto in data.get("protocols", [])
    ]


def _build_related(peptide_id: str, data: dict) -> list[PeptideRelated]:
    return [
        PeptideRelated(
            peptide_id=peptide_id,
            related_peptide_id=r["related_peptide_id"],
            relation_type=r["relationship"],   # JSON key → ORM attr (col: "relationship")
            note=r.get("note"),
        )
        for r in data.get("related", [])
    ]


# ---------------------------------------------------------------------------
# Seed passes
# ---------------------------------------------------------------------------

async def _seed_core(session: AsyncSession, path: Path) -> str:
    """Pass 1: peptide + references + dose_ranges + protocols."""
    data = json.loads(path.read_text())
    peptide_id = data["peptide"]["id"]

    # Upsert peptide row (merge = insert-or-update by PK)
    await session.merge(_build_peptide(data))

    # Upsert references (composite PK: peptide_id + ref_id)
    for ref in _build_references(peptide_id, data):
        await session.merge(ref)

    # Dose ranges have an autoincrement PK with no stable natural key in the JSON,
    # so delete-then-insert is the safest idempotent strategy.
    await session.execute(
        delete(PeptideDoseRange).where(PeptideDoseRange.peptide_id == peptide_id)
    )
    for dr in _build_dose_ranges(peptide_id, data):
        session.add(dr)

    # Upsert protocols (string PK: id)
    for proto in _build_protocols(peptide_id, data):
        await session.merge(proto)

    n = lambda key: len(data.get(key, []))
    print(
        f"  [pass 1] {peptide_id:<20} "
        f"refs={n('references')}  doses={n('dose_ranges')}  protocols={n('protocols')}"
    )
    return peptide_id


async def _seed_relations(session: AsyncSession, path: Path) -> None:
    """Pass 2: related (may reference other peptide ids)."""
    data = json.loads(path.read_text())
    peptide_id = data["peptide"]["id"]

    related = _build_related(peptide_id, data)
    skipped_related = 0

    for rel in related:
        try:
            await session.merge(rel)
        except Exception as exc:
            skipped_related += 1
            print(
                f"  [pass 2] WARN: skipped related "
                f"{peptide_id} → {rel.related_peptide_id}: {exc}"
            )

    n = lambda key: len(data.get(key, []))
    ok_related = n("related") - skipped_related
    print(f"  [pass 2] {peptide_id:<20} related={ok_related}/{n('related')}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(paths: list[Path]) -> None:
    if not paths:
        print("No JSON files to seed.")
        return

    print(f"\nSeeding {len(paths)} peptide file(s)...\n")

    async with AsyncSessionLocal() as session:
        print("=== Pass 1: core peptide data ===")
        for path in paths:
            await _seed_core(session, path)
        await session.commit()

        print("\n=== Pass 2: cross-peptide relationships ===")
        for path in paths:
            await _seed_relations(session, path)
        await session.commit()  

    print(f"\nDone. {len(paths)} peptide(s) seeded successfully.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        file_paths = [Path(p).resolve() for p in sys.argv[1:]]
    else:
        # Default: all JSON files in peptora-api/docs/ (inside the repo, so it deploys)
        docs_dir = Path(__file__).resolve().parents[1] / "docs"
        file_paths = sorted(docs_dir.glob("*.json"))
        if not file_paths:
            print(f"No *.json files found in {docs_dir}")
            sys.exit(0)

    asyncio.run(main(file_paths))
