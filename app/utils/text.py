"""Punctuation clean-up for library content on its way out of the API.

The peptide and stack entries were written with a lot of dashes used as
punctuation, which reads as machine-written copy in the apps. Rewriting every
stored row is a content project; this normalises the punctuation where the
content is served instead, so the web app, the native app and any future
entry all get the same plain text without anyone having to remember the rule.

Only punctuation changes. No number, name, unit or citation is altered, and
identifiers and links are left exactly as stored.

The same pass removes a stray HTML line-break tag left in an entry. The apps
show library text as plain text, so a tag would be printed as it is written.
"""

import re

_EM = "—"
_EN = "–"
_DASHES = _EM + _EN

# "250 [en dash] 500": a numeric range. A plain hyphen keeps it a range.
_NUMERIC_RANGE = re.compile(rf"(?<=\d)\s?[{_DASHES}]\s?(?=\d)")
# A spaced dash doing the job of a comma or a colon, including the typewriter
# spelling with two or three hyphens ("rhGH -- a bioidentical copy").
_SPACED = re.compile(rf"\s+(?:[{_DASHES}]|-{{2,3}})\s+")
# An unspaced dash between words ("dose[en dash]response").
_TIGHT = re.compile(rf"[{_DASHES}]")

# A line-break tag pasted into an entry: <br>, <br/>, </br>.
_BREAK_TAG = re.compile(r"\s*</?br\s*/?>\s*", re.IGNORECASE)

# Never rewritten: identifiers, links and sequences are data, not prose.
_SKIP_KEYS = frozenset({
    "id", "peptide_id", "stack_id", "related_peptide_id", "ref_id",
    "url", "doi", "pmid", "cas_number", "sequence", "molecular_formula",
    "ratio_source_urls",
})
# Headline-like fields, where a spaced dash separates a name from a
# description and a colon reads better than a comma.
_TITLE_KEYS = frozenset({"title", "name", "label"})


def plain_punctuation(text: str, *, title: bool = False) -> str:
    """Replace dashes used as punctuation in one string with plain marks."""
    if _EM not in text and _EN not in text and " --" not in text:
        return text
    out = _NUMERIC_RANGE.sub("-", text)
    out = _SPACED.sub(": " if title else ", ", out)
    out = _TIGHT.sub("-", out)
    return out


def clean_content(value, key: str = ""):
    """Apply plain_punctuation to every prose string in a JSON-like value."""
    if isinstance(value, str):
        if key in _SKIP_KEYS:
            return value
        if "<" in value:
            value = _BREAK_TAG.sub(" ", value).strip()
        return plain_punctuation(value, title=key in _TITLE_KEYS)
    if isinstance(value, list):
        return [clean_content(item, key) for item in value]
    if isinstance(value, dict):
        return {k: clean_content(v, k) for k, v in value.items()}
    return value
