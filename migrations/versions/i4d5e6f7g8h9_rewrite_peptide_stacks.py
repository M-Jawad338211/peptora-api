"""rewrite peptide_stacks (pairwise -> N-ary blends/pairings)

Replaces the old 6-column pairwise `peptide_stacks` table with three tables
(`peptide_stacks` parent card, `stack_components` N-ary, `stack_references`)
plus a `stack_documents` read view, per docs/PEPTIDE_STACKS_REWRITE_PLAN.md
and docs/stacks_rewrite.sql. Also recreates `peptide_documents`, replacing
its `stack_compatibility` field with a lightweight `featured_in_stacks`
reverse lookup.

NOTE: `stacks_rewrite.sql` declares `stack_references.type` as `ref_type_enum`,
but no such Postgres type exists in this schema — the real, existing type is
`peptide_ref_type_enum` (used by `peptide_references.type`). That correction
is applied below.

WARNING: `upgrade()` drops the old `peptide_stacks` table (CASCADE) — this is
destructive and not recoverable via `downgrade()`. Take a DB backup first.

Revision ID: i4d5e6f7g8h9
Revises: h3c4d5e6f7g8
Create Date: 2026-08-20
"""
from alembic import op

revision = 'i4d5e6f7g8h9'
down_revision = 'h3c4d5e6f7g8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS peptide_stacks CASCADE")

    op.execute("DROP TYPE IF EXISTS peptide_compatibility_enum")

    op.execute(
        "CREATE TYPE stack_type_enum AS ENUM ('research_pairing', 'commercial_blend')"
    )

    op.execute(
        """
        CREATE TYPE ratio_source_type_enum AS ENUM (
          'vendor-listing',
          'manufacturer-label',
          'community-convention'
        )
        """
    )

    op.execute(
        """
        CREATE TABLE peptide_stacks (
          id                       text PRIMARY KEY,
          name                     text NOT NULL,
          aliases                  text[] NOT NULL DEFAULT '{}',
          stack_type               stack_type_enum NOT NULL,
          category                 peptide_category_enum,
          positioning              text,
          rationale                text,
          evidence_level           peptide_evidence_level_enum NOT NULL DEFAULT 'anecdotal',
          is_recommendation        boolean NOT NULL DEFAULT false,

          ratio_source_type        ratio_source_type_enum,
          ratio_source_note        text,
          ratio_source_urls        text[] NOT NULL DEFAULT '{}',
          common_total_mg_options  numeric[] NOT NULL DEFAULT '{}',

          caution_notes            text[] NOT NULL DEFAULT '{}',
          disclaimer               text,
          last_reviewed            date NOT NULL DEFAULT CURRENT_DATE,
          reviewed_by              text,
          content_version          integer NOT NULL DEFAULT 1,
          data_completeness        text NOT NULL DEFAULT 'stub',
          search_tsv               tsvector,
          created_at               timestamptz NOT NULL DEFAULT now(),
          updated_at               timestamptz NOT NULL DEFAULT now(),

          CONSTRAINT peptide_stacks_never_recommendation CHECK (is_recommendation = false),

          CONSTRAINT peptide_stacks_blend_needs_source CHECK (
            stack_type <> 'commercial_blend' OR ratio_source_type IS NOT NULL
          ),

          CONSTRAINT peptide_stacks_pairing_no_ratio_source CHECK (
            stack_type <> 'research_pairing' OR (
              ratio_source_type IS NULL AND
              ratio_source_note IS NULL AND
              common_total_mg_options = '{}'
            )
          )
        )
        """
    )

    op.execute("CREATE INDEX idx_stacks_type ON peptide_stacks (stack_type)")
    op.execute("CREATE INDEX idx_stacks_category ON peptide_stacks (category)")
    op.execute("CREATE INDEX idx_stacks_search_tsv ON peptide_stacks USING gin (search_tsv)")

    op.execute(
        """
        CREATE TABLE stack_components (
          id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          stack_id          text NOT NULL REFERENCES peptide_stacks(id) ON DELETE CASCADE,
          peptide_id        text NOT NULL REFERENCES peptides(id) ON DELETE CASCADE,
          sort_order        integer NOT NULL DEFAULT 0,

          ratio_parts       numeric CHECK (ratio_parts IS NULL OR ratio_parts > 0),
          typical_mg_share  numeric CHECK (typical_mg_share IS NULL OR typical_mg_share > 0),

          role              text,
          dose_note         text,

          UNIQUE (stack_id, peptide_id)
        )
        """
    )

    op.execute("CREATE INDEX idx_stack_components_stack ON stack_components (stack_id)")
    op.execute("CREATE INDEX idx_stack_components_peptide ON stack_components (peptide_id)")

    op.execute(
        """
        CREATE TABLE stack_references (
          id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          stack_id       text NOT NULL REFERENCES peptide_stacks(id) ON DELETE CASCADE,
          ref_id         integer NOT NULL,
          type           peptide_ref_type_enum NOT NULL,
          title          text NOT NULL,
          first_author   text,
          year           text,
          source         text,
          pmid           text,
          doi            text,
          url            text,
          UNIQUE (stack_id, ref_id)
        )
        """
    )

    op.execute("CREATE INDEX idx_stack_references_stack ON stack_references (stack_id)")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION stacks_tsv_update() RETURNS trigger AS $$
        BEGIN
          NEW.search_tsv :=
            setweight(to_tsvector('simple', coalesce(NEW.name, '')), 'A') ||
            setweight(to_tsvector('simple', array_to_string(coalesce(NEW.aliases, '{}'), ' ')), 'B') ||
            setweight(to_tsvector('simple', coalesce(NEW.positioning, '')), 'C');
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    op.execute(
        """
        CREATE TRIGGER trg_stacks_tsv
        BEFORE INSERT OR UPDATE ON peptide_stacks
        FOR EACH ROW EXECUTE FUNCTION stacks_tsv_update()
        """
    )

    op.execute("ALTER TABLE peptide_stacks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE stack_components ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE stack_references ENABLE ROW LEVEL SECURITY")

    op.execute('CREATE POLICY "public read stacks" ON peptide_stacks FOR SELECT USING (true)')
    op.execute('CREATE POLICY "public read stack_components" ON stack_components FOR SELECT USING (true)')
    op.execute('CREATE POLICY "public read stack_references" ON stack_references FOR SELECT USING (true)')

    op.execute(
        """
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
        FROM peptide_stacks s
        """
    )

    op.execute(
        """
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
           FROM peptides p
        """
    )


def downgrade() -> None:
    # Schema-only restore of the pre-rewrite shape. Data inserted into the new
    # tables (and the exact prior text of `peptide_documents`, which this
    # migration overwrote) are NOT recoverable this way — restore from a DB
    # backup if a true rollback with data is required.
    op.execute("DROP VIEW IF EXISTS peptide_documents")
    op.execute("DROP VIEW IF EXISTS stack_documents")

    op.execute("DROP TABLE IF EXISTS stack_references CASCADE")
    op.execute("DROP TABLE IF EXISTS stack_components CASCADE")
    op.execute("DROP TABLE IF EXISTS peptide_stacks CASCADE")

    op.execute("DROP TYPE IF EXISTS ratio_source_type_enum")
    op.execute("DROP TYPE IF EXISTS stack_type_enum")

    op.execute(
        "CREATE TYPE peptide_compatibility_enum AS ENUM "
        "('commonly-combined', 'caution', 'not-recommended', 'no-data')"
    )

    op.execute(
        """
        CREATE TABLE peptide_stacks (
          peptide_id      text NOT NULL REFERENCES peptides(id) ON DELETE CASCADE,
          partner_id      text NOT NULL REFERENCES peptides(id) ON DELETE CASCADE,
          compatibility   peptide_compatibility_enum NOT NULL,
          rationale       text,
          evidence_level  peptide_evidence_level_enum,
          citation_refs   integer[] NOT NULL DEFAULT '{}',
          PRIMARY KEY (peptide_id, partner_id),
          CONSTRAINT ck_peptide_stacks_no_self CHECK (peptide_id <> partner_id)
        )
        """
    )
