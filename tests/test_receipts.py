"""Receipt upload validation.

This is the one endpoint that accepts a file from anyone with an account,
paid or not, and the bytes end up rendered in an admin panel. Everything here
is a case where getting it wrong hands someone else's browser to a stranger.

Run: pytest tests/ -q
"""

import io

import pytest

from app.utils import storage


def _png(size=(4, 4)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (12, 34, 56)).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 10, 10)).save(buf, format="JPEG")
    return buf.getvalue()


# ── what is accepted ────────────────────────────────────────────────────────

def test_png_is_accepted_and_reencoded_to_jpeg():
    data, mime, ext = storage.normalise_receipt(_png())
    # Re-encoded rather than stored as received: that is what strips EXIF and
    # destroys polyglots.
    assert mime == "image/jpeg"
    assert ext == ".jpg"
    assert data.startswith(b"\xff\xd8\xff")


def test_jpeg_is_accepted():
    data, mime, ext = storage.normalise_receipt(_jpeg())
    assert mime == "image/jpeg"
    assert data.startswith(b"\xff\xd8\xff")


def test_pdf_passes_through_unmodified():
    raw = b"%PDF-1.4\n%fake pdf body\ntrailer\n%%EOF\n"
    data, mime, ext = storage.normalise_receipt(raw)
    # PDFs cannot be re-encoded without a heavyweight dependency, so they are
    # stored as-is and only ever served as an attachment.
    assert (data, mime, ext) == (raw, "application/pdf", ".pdf")


# ── what is refused ─────────────────────────────────────────────────────────

def test_svg_is_refused():
    """SVG is an image by extension and a script host in practice — the
    classic stored-XSS route into a panel that renders stranger-uploaded
    files."""
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    with pytest.raises(storage.StorageError):
        storage.normalise_receipt(svg)


def test_html_disguised_as_an_image_is_refused():
    with pytest.raises(storage.StorageError):
        storage.normalise_receipt(b"<!doctype html><script>alert(1)</script>")


def test_executable_is_refused():
    with pytest.raises(storage.StorageError):
        storage.normalise_receipt(b"\x7fELF\x02\x01\x01\x00 not an image")


def test_empty_file_is_refused():
    with pytest.raises(storage.StorageError):
        storage.normalise_receipt(b"")


def test_oversized_file_is_refused():
    with pytest.raises(storage.StorageError):
        storage.normalise_receipt(b"\xff\xd8\xff" + b"0" * (storage.MAX_UPLOAD_BYTES + 1))


def test_content_type_header_is_never_trusted():
    """The type comes from the leading bytes, never from what the client
    claims. A .jpg name and an image/jpeg header on an HTML payload must not
    get through."""
    with pytest.raises(storage.StorageError):
        storage.normalise_receipt(b"GIF89a" + b"<script>alert(1)</script>")


def test_truncated_image_is_refused_not_stored():
    """A JPEG magic number with no decodable image behind it. Sniffing alone
    would let this through; the re-encode is what actually rejects it."""
    with pytest.raises(storage.StorageError):
        storage.normalise_receipt(b"\xff\xd8\xff\xe0" + b"\x00" * 64)


# ── keys ────────────────────────────────────────────────────────────────────

def test_key_discards_the_user_supplied_filename():
    import uuid

    uid, cid = uuid.uuid4(), uuid.uuid4()
    key = storage.build_key(uid, cid, ".jpg")
    assert key.startswith(f"receipts/{uid}/{cid}/")
    assert key.endswith(".jpg")
    # Scoped per user and per claim, so one claim's key can never address
    # another user's object.
    assert str(uid) in key and str(cid) in key


def test_keys_are_unique_per_upload():
    import uuid

    uid, cid = uuid.uuid4(), uuid.uuid4()
    assert storage.build_key(uid, cid, ".jpg") != storage.build_key(uid, cid, ".jpg")
