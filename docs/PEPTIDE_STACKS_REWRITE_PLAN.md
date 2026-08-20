# peptide_stacks Full Rewrite — Plan

> Tested end-to-end against your actual schema (schema.sql + the 29 seeded peptides)
> before being handed to you. Full DDL is in `stacks_rewrite.sql` alongside this plan.

---

## 0. What was wrong with the old table

```sql
-- OLD (6 columns, pairwise only)
peptide_stacks (peptide_id, partner_id, compatibility, rationale, evidence_level, citation_refs)
```

Pairwise-only (`peptide_id` + `partner_id`) can't represent Klow (4 components) or any
N-ary combination without ugly workarounds. It also has no place to put a composition
ratio, so there was nowhere honest to put "vendor says this vial is roughly 2:1:1:1"
without it looking like a dosing instruction.

## 1. The new shape — three tables + one view

```
peptide_stacks       (1 row per stack/blend — the "card")
  └─ stack_components  (N rows — who's in it, and at what ratio if it's a product)
  └─ stack_references   (0..N rows — citations for claims made in rationale/cautions)

stack_documents  (view — reassembles a stack into one nested JSON document,
                  pulling each component's OWN dose ranges + frequency LIVE
                  from peptide_dose_ranges, never duplicated)
```

One table now cleanly handles both cases from the earlier plan:

- **`research_pairing`** — e.g. Ipamorelin + CJC-1295 (no DAC). No vial, no ratio.
  `ratio_parts` stays NULL on every component.
- **`commercial_blend`** — e.g. Klow. Has a vial, has a documented ratio, and a
  CHECK constraint makes it *impossible* to save a ratio without a source.

## 2. Full schema

See `stacks_rewrite.sql` for the complete, tested DDL. Key design decisions:

**`peptide_stacks` (parent)**
- `stack_type` (`research_pairing` | `commercial_blend`) — drives everything downstream.
- `evidence_level` defaults to `anecdotal` and reuses your existing `evidence_level_enum`
  — no combination product ever earns `established` in this table; that would misrepresent
  what's actually been studied.
- `is_recommendation boolean NOT NULL DEFAULT false` with `CHECK (is_recommendation = false)`
  — same pattern you already use on `peptide_protocols`. This table can never become a
  dosing source, by construction, not just convention.
- **Two CHECK constraints enforce the Layer C rule from the earlier spec, at the database
  level, not just in application code:**
  - `commercial_blend` rows **must** have a `ratio_source_type` — you cannot save a ratio
    without saying where it came from. Tested: attempting to insert a blend with no source
    fails with `peptide_stacks_blend_needs_source`.
  - `research_pairing` rows **must not** carry ratio/source fields at all — keeps the two
    types structurally distinct so the UI layer can't accidentally render a ratio for a
    pairing that never had a real product behind it.
- `ratio_source_type` enum: `vendor-listing` | `manufacturer-label` | `community-convention`
  — lets you be precise about how strong that Layer C claim actually is.

**`stack_components` (child, N-ary)**
- One row per peptide in the stack. `ratio_parts` is the raw ratio unit (e.g. `2`, `1`,
  `1`, `1` for Klow) — NULL for research pairings.
- `typical_mg_share` is optional, purely informational vendor-stated mg at one of the
  `common_total_mg_options` — **never** the source of truth for the calculator, which
  always derives mg from `ratio_parts` + whatever vial size the user actually has.
- `dose_note` is text-only commentary space (e.g. "in this pairing, ipamorelin is
  typically timed to coincide with CJC-1295's pulse") — deliberately NOT a numeric
  field, so it can never masquerade as an override of the peptide's own cited dose.
- `UNIQUE (stack_id, peptide_id)` — a peptide can't appear twice in the same stack.
- Indexed on `peptide_id` too, enabling the reverse lookup in §4.

**`stack_references` (child)**
- Only for claims made in the stack's own `rationale` or `caution_notes` text that need
  a citation beyond what each component peptide's own page already cites. Mirrors
  `peptide_references` in shape.

**`stack_documents` (view)**
- This is the important one. It joins `stack_components` → `peptides` →
  `peptide_dose_ranges` **live**, so every component in the returned JSON carries its
  own real, current `reference_dose_ranges` array — including `frequency` — with zero
  duplication. If you ever correct a dose range on BPC-157's own page, every stack that
  includes BPC-157 reflects it automatically on the next read.

## 3. Verified output (tested against your real 29-peptide database)

Inserted `klow` (commercial_blend, 4 components: GHK-Cu 2 parts / BPC-157 1 part /
TB-500 1 part / KPV 1 part) and `ipamorelin-cjc1295-no-dac` (research_pairing, 2
components). Querying `stack_documents` for Klow returns, per component, its role,
ratio, and its own cited dose ranges verbatim — for example GHK-Cu's real topical
entry came through as:

```json
{
  "peptide_id": "ghk-cu",
  "ratio_parts": 2,
  "role": "Cosmetic copper peptide for skin/collagen support",
  "reference_dose_ranges": [
    {
      "context": "Topical cosmetic formulations (concentration in product)",
      "low": 0.05, "high": 2.0, "unit": "% w/w",
      "frequency": "1-2x daily (per product)",
      "note": "Typical cosmetic serum/cream concentrations...",
      "citation_refs": [4]
    },
    { "...": "the anecdotal injectable range entry, similarly complete" }
  ]
}
```

Nothing here was written by hand into the stack — it's pulled straight from GHK-Cu's
existing encyclopedia entry. The constraint tests also passed: a blend inserted without
`ratio_source_type` was rejected by the database itself, and reverse lookup ("which
stacks is BPC-157 featured in?") returned the Klow entry correctly via the rebuilt
`peptide_documents` view.

## 4. Side effect you need to know about: `peptide_documents` changes

Your existing `peptide_documents` view aggregated the old `peptide_stacks` table into a
`stack_compatibility` field. Since that table is gone, this field is replaced with
`featured_in_stacks` — a lightweight reverse-lookup list (which stacks include this
peptide, its role, its ratio) rather than the old pairwise compatibility rows. Full
stack detail (with every OTHER component's own dose data) is one query away via
`stack_documents WHERE id = ANY(...)`, so `peptide_documents` stays lightweight and
`stack_documents` stays the place for full stack detail. The corrected view definition
is included at the bottom of `stacks_rewrite.sql`'s companion — see §6.

## 5. Presentation logic (for the stack/blend detail screen)

Pseudocode for what the screen does with one `stack_documents` row:

```
render_stack(doc):
    show name, positioning

    if doc.stack_type == 'commercial_blend':
        show_section("Commonly Documented Composition")   # NEVER "Recommended Ratio"
            render ratio as parts (e.g. "2 : 1 : 1 : 1 — GHK-Cu : BPC-157 : TB-500 : KPV")
            show doc.ratio_source_type + doc.ratio_source_note, with source URLs visible
            if doc.common_total_mg_options: show as "commonly sold as: 10mg, 20mg vials"

    show_section("Reference Ranges — Individually Studied")
        for component in doc.components (ordered by sort_order):
            render peptide_name, role
            for range in component.reference_dose_ranges:
                render range.context, low–high range.unit, range.frequency, citation badges
            # this section visually contrasts with the ratio section above —
            # citation badges vs. "vendor listing" framing — so a user can never
            # confuse the two kinds of source

    if doc.stack_type == 'commercial_blend':
        show_calculator(doc)   # see §6, extends the single-peptide reconstitution engine

    if doc.caution_notes: show_section("Cautions", doc.caution_notes)
    show doc.disclaimer + standard research-use banner
```

## 6. Calculator hook-in (unchanged from the earlier spec, now data-complete)

The blend reconstitution math from the earlier stacks/blends spec (`calc_blend_forward`
/ `calc_blend_inverse_by_component`) now has exactly the inputs it needs straight from
this schema: `ratio_parts` per component from `stack_components`, and — for the
optional "does this land inside what's separately documented?" flag — each component's
own `reference_dose_ranges` from the same `stack_documents` row. No new data model
needed there; this table was the missing piece.

## 7. Build order

1. Run `stacks_rewrite.sql` against a staging copy of your DB (it `DROP...CASCADE`s the
   old table — take a backup or run on staging first).
2. Apply the corrected `peptide_documents` view (§4 / bottom of the SQL file).
3. Seed `klow` and `glow` the same way the 29 peptides were seeded — research each
   blend's commonly-listed ratio across 3-4 vendor pages, write the `ratio_source_note`
   honestly (including if vendors disagree), insert via `stack_components`.
4. Migrate your existing research pairings (the ones currently embedded as `stacks[]`
   arrays inside each peptide's own seed JSON, e.g. ipamorelin ↔ cjc-1295-no-dac) into
   proper `research_pairing` rows in the new table — one row per pairing, not duplicated
   on both peptides' JSON anymore. This becomes the single source of truth going forward;
   the embedded per-peptide `stacks[]` field can be deprecated from new seed files.
5. Build the detail screen per §5, and wire the calculator per §6.
