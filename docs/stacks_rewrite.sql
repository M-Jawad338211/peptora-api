-- ============================================================
-- PEPTIDE_STACKS FULL REWRITE
-- Replaces the old 6-column pairwise peptide_stacks table.
-- Handles BOTH:
--   - research_pairing : N peptides commonly used alongside each other,
--                         no product, no ratio (e.g. ipamorelin + CJC-1295)
--   - commercial_blend  : N peptides in one vendor-sold vial with a
--                         documented composition ratio (e.g. Klow, Glow)
-- ============================================================

-- Drop the old table entirely (per requirement: full rewrite, not a migration)
DROP TABLE IF EXISTS peptide_stacks CASCADE;

-- ---------- new enums ----------
CREATE TYPE stack_type_enum AS ENUM ('research_pairing', 'commercial_blend');

CREATE TYPE ratio_source_type_enum AS ENUM (
  'vendor-listing',      -- ratio as commonly stated across vendor product pages
  'manufacturer-label',  -- ratio taken from a specific manufacturer's own label
  'community-convention' -- ratio as commonly described in user/community sources, no formal label
);

-- ---------- parent table: the stack/blend itself ----------
CREATE TABLE peptide_stacks (
  id                       text PRIMARY KEY,                 -- slug, e.g. 'klow', 'ipamorelin-cjc1295-no-dac'
  name                     text NOT NULL,                     -- 'Klow Blend', 'Ipamorelin + CJC-1295 (no DAC)'
  aliases                  text[] NOT NULL DEFAULT '{}',
  stack_type               stack_type_enum NOT NULL,
  category                 category_enum,                     -- reuse existing peptide category enum
  positioning              text,                               -- neutral description of what it's marketed/used for
  rationale                text,                               -- why these peptides are combined (mechanistic etc.)
  evidence_level           evidence_level_enum NOT NULL DEFAULT 'anecdotal',
  is_recommendation        boolean NOT NULL DEFAULT false,

  -- Layer C fields — ONLY populated for commercial_blend.
  -- The CHECK constraints below enforce that a ratio never appears without its source.
  ratio_source_type        ratio_source_type_enum,
  ratio_source_note        text,
  ratio_source_urls        text[] NOT NULL DEFAULT '{}',
  common_total_mg_options  numeric[] NOT NULL DEFAULT '{}',    -- e.g. {5, 10} vial sizes commonly sold

  caution_notes            text[] NOT NULL DEFAULT '{}',       -- e.g. "combined effects of multiple healing
                                                                 --       peptides used together have not been
                                                                 --       studied"
  disclaimer               text,
  last_reviewed            date NOT NULL DEFAULT CURRENT_DATE,
  reviewed_by              text,
  content_version          integer NOT NULL DEFAULT 1,
  data_completeness        text NOT NULL DEFAULT 'stub',
  search_tsv               tsvector,
  created_at               timestamptz NOT NULL DEFAULT now(),
  updated_at               timestamptz NOT NULL DEFAULT now(),

  -- never let this table itself become the dosing source
  CONSTRAINT peptide_stacks_never_recommendation CHECK (is_recommendation = false),

  -- a commercial blend MUST cite where its ratio came from (Layer C rule from the spec)
  CONSTRAINT peptide_stacks_blend_needs_source CHECK (
    stack_type <> 'commercial_blend' OR ratio_source_type IS NOT NULL
  ),

  -- a research pairing has no vial, so it must NOT carry ratio/source fields
  CONSTRAINT peptide_stacks_pairing_no_ratio_source CHECK (
    stack_type <> 'research_pairing' OR (
      ratio_source_type IS NULL AND
      ratio_source_note IS NULL AND
      common_total_mg_options = '{}'
    )
  )
);

CREATE INDEX idx_stacks_type ON peptide_stacks (stack_type);
CREATE INDEX idx_stacks_category ON peptide_stacks (category);
CREATE INDEX idx_stacks_search_tsv ON peptide_stacks USING gin (search_tsv);

-- ---------- child table: N-ary component list ----------
CREATE TABLE stack_components (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  stack_id          text NOT NULL REFERENCES peptide_stacks(id) ON DELETE CASCADE,
  peptide_id        text NOT NULL REFERENCES peptides(id) ON DELETE CASCADE,
  sort_order        integer NOT NULL DEFAULT 0,

  -- ratio_parts is the raw ratio unit (e.g. 2 / 1 / 1 / 1). NULL for research_pairing
  -- (no vial => no ratio to state). Normalize to a fraction at read time, not write time.
  ratio_parts       numeric CHECK (ratio_parts IS NULL OR ratio_parts > 0),

  -- OPTIONAL vendor-stated mg at whichever common_total_mg_options entry this refers to.
  -- Purely informational (Layer C) — never used as the source of truth for the calculator,
  -- which always derives mg from ratio_parts + the user's actual vial size.
  typical_mg_share  numeric CHECK (typical_mg_share IS NULL OR typical_mg_share > 0),

  role              text,        -- why this component is in the combination
  dose_note         text,        -- optional stack-specific framing commentary (text only,
                                  -- never a numeric override of the peptide's own dose_ranges)

  UNIQUE (stack_id, peptide_id)
);

CREATE INDEX idx_stack_components_stack ON stack_components (stack_id);
CREATE INDEX idx_stack_components_peptide ON stack_components (peptide_id); -- reverse lookup:
                                                                             -- "which stacks include BPC-157?"

-- ---------- child table: stack-level citations ----------
-- For claims made in `rationale` or `caution_notes` that need their own citation,
-- separate from each component peptide's own reference list.
CREATE TABLE stack_references (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  stack_id       text NOT NULL REFERENCES peptide_stacks(id) ON DELETE CASCADE,
  ref_id         integer NOT NULL,     -- local numbering within this stack, like peptide_references.ref_id
  type           ref_type_enum NOT NULL,
  title          text NOT NULL,
  first_author   text,
  year           text,
  source         text,
  pmid           text,
  doi            text,
  url            text,
  UNIQUE (stack_id, ref_id)
);

CREATE INDEX idx_stack_references_stack ON stack_references (stack_id);

-- ---------- tsvector trigger (mirrors peptides_tsv_update) ----------
CREATE OR REPLACE FUNCTION stacks_tsv_update() RETURNS trigger AS $$
BEGIN
  NEW.search_tsv :=
    setweight(to_tsvector('simple', coalesce(NEW.name, '')), 'A') ||
    setweight(to_tsvector('simple', array_to_string(coalesce(NEW.aliases, '{}'), ' ')), 'B') ||
    setweight(to_tsvector('simple', coalesce(NEW.positioning, '')), 'C');
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_stacks_tsv
BEFORE INSERT OR UPDATE ON peptide_stacks
FOR EACH ROW EXECUTE FUNCTION stacks_tsv_update();

-- ---------- RLS ----------
ALTER TABLE peptide_stacks ENABLE ROW LEVEL SECURITY;
ALTER TABLE stack_components ENABLE ROW LEVEL SECURITY;
ALTER TABLE stack_references ENABLE ROW LEVEL SECURITY;

CREATE POLICY "public read stacks" ON peptide_stacks FOR SELECT USING (true);
CREATE POLICY "public read stack_components" ON stack_components FOR SELECT USING (true);
CREATE POLICY "public read stack_references" ON stack_references FOR SELECT USING (true);
-- writes: service-role only (no policy needed here; default-deny for anon/authenticated
-- unless you add explicit write policies scoped to the service role, matching the
-- peptides table's existing write pattern)

-- ============================================================
-- READ VIEW — reassembles each stack into one nested JSON document,
-- pulling each component's OWN reference dose ranges (with frequency)
-- LIVE from peptide_dose_ranges. This is the single source of truth
-- for "reference dosages with frequency" — never duplicated into
-- stack_components itself, so it can never go stale relative to the
-- peptide's own encyclopedia entry.
-- ============================================================
CREATE OR REPLACE VIEW stack_documents AS
SELECT
  s.id,
  s.name,
  s.aliases,
  s.stack_type,
  s.category,
  s.positioning,
  s.rationale,
  s.evidence_level,
  s.is_recommendation,
  s.ratio_source_type,
  s.ratio_source_note,
  s.ratio_source_urls,
  s.common_total_mg_options,
  s.caution_notes,
  s.disclaimer,
  s.last_reviewed,
  s.content_version,
  (
    SELECT json_agg(comp ORDER BY (comp->>'sort_order')::int)
    FROM (
      SELECT json_build_object(
        'sort_order', sc.sort_order,
        'peptide_id', sc.peptide_id,
        'peptide_name', p.name,
        'peptide_category', p.category,
        'peptide_evidence_level', p.evidence_level,
        'ratio_parts', sc.ratio_parts,
        'typical_mg_share', sc.typical_mg_share,
        'role', sc.role,
        'dose_note', sc.dose_note,
        -- LIVE join: each component's OWN cited dose ranges, with frequency
        'reference_dose_ranges', (
          SELECT json_agg(json_build_object(
            'context', dr.context,
            'low', dr.low,
            'high', dr.high,
            'unit', dr.unit,
            'route', dr.route,
            'frequency', dr.frequency,
            'note', dr.note,
            'citation_refs', dr.citation_refs
          ))
          FROM peptide_dose_ranges dr
          WHERE dr.peptide_id = sc.peptide_id
        )
      ) AS comp
      FROM stack_components sc
      JOIN peptides p ON p.id = sc.peptide_id
      WHERE sc.stack_id = s.id
    ) sub
  ) AS components,
  (
    SELECT json_agg(json_build_object(
      'ref_id', r.ref_id, 'type', r.type, 'title', r.title,
      'first_author', r.first_author, 'year', r.year, 'source', r.source,
      'pmid', r.pmid, 'doi', r.doi, 'url', r.url
    ) ORDER BY r.ref_id)
    FROM stack_references r
    WHERE r.stack_id = s.id
  ) AS stack_references
FROM peptide_stacks s;

-- ============================================================
-- REQUIRED FOLLOW-UP: peptide_documents must be recreated.
-- The DROP...CASCADE above took it down because it referenced the old
-- peptide_stacks table. This restores it with the old pairwise
-- 'stack_compatibility' field replaced by a lightweight reverse-lookup
-- 'featured_in_stacks' field (full stack detail lives in stack_documents).
-- ============================================================
CREATE OR REPLACE VIEW peptide_documents AS
 SELECT p.id,
    to_jsonb(p.*) - 'search_tsv'::text AS core,
    COALESCE(( SELECT jsonb_agg(to_jsonb(r.*) - 'peptide_id'::text)
           FROM peptide_references r
          WHERE r.peptide_id = p.id), '[]'::jsonb) AS "references",
    COALESCE(( SELECT jsonb_agg(to_jsonb(d.*) - 'peptide_id'::text)
           FROM peptide_dose_ranges d
          WHERE d.peptide_id = p.id), '[]'::jsonb) AS studied_dose_ranges,
    COALESCE(( SELECT jsonb_agg(to_jsonb(pr.*) - 'peptide_id'::text)
           FROM peptide_protocols pr
          WHERE pr.peptide_id = p.id), '[]'::jsonb) AS protocols,
    COALESCE(( SELECT jsonb_agg(to_jsonb(rel.*) - 'peptide_id'::text)
           FROM peptide_related rel
          WHERE rel.peptide_id = p.id), '[]'::jsonb) AS related_peptides,
    COALESCE(( SELECT jsonb_agg(jsonb_build_object(
                 'stack_id', sc.stack_id,
                 'stack_name', s.name,
                 'stack_type', s.stack_type,
                 'role', sc.role,
                 'ratio_parts', sc.ratio_parts
               ))
           FROM stack_components sc
           JOIN peptide_stacks s ON s.id = sc.stack_id
          WHERE sc.peptide_id = p.id), '[]'::jsonb) AS featured_in_stacks
   FROM peptides p;
