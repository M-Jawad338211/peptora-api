"""Object storage for payment receipts.

Two backends behind one interface. S3 (Railway Buckets, which are Tigris and
fully S3-compatible) in deployed environments; a local directory when the
bucket credentials are absent, so the claim flow can be developed offline
without sharing production credentials around.

Railway Buckets are private-only — there is no public-bucket mode to enable by
accident — so nothing here ever returns a durable URL. Reads go out as
short-lived presigned URLs and nothing else.
"""

import logging
import mimetypes
import os
import uuid
from pathlib import Path

from app.config import settings

logger = logging.getLogger("peptora.storage")

# Deliberately narrow. SVG is absent on purpose: it is an image by extension
# and a script host in practice, which is the classic stored-XSS route into an
# admin panel that renders whatever a stranger uploaded.
ALLOWED_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}

MAX_UPLOAD_BYTES = 8 * 1024 * 1024

# Presigned reads are for one admin looking at one receipt right now. Long
# enough to open the image, short enough that a link pasted into a chat is
# dead by the time anyone else clicks it.
PRESIGN_TTL_SECONDS = 300


class StorageError(Exception):
    pass


def _sniff_mime(head: bytes) -> str | None:
    """Identify a file from its leading bytes.

    The client-supplied content-type and the filename extension are both
    attacker-controlled and are never consulted. Only these four types exist
    as far as this module is concerned.
    """
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def normalise_receipt(raw: bytes) -> tuple[bytes, str, str]:
    """Validate and sanitise an uploaded receipt.

    Returns (bytes_to_store, mime, extension).

    Images are re-encoded rather than stored as received. That strips EXIF —
    a phone photo of a bank slip carries GPS coordinates — and destroys
    polyglot files, which are valid in two formats at once and are how an
    "image" ends up being parsed as something executable further down the
    line. PDFs cannot be re-encoded without a heavyweight dependency, so they
    pass through and are only ever served as an attachment.
    """
    if not raw:
        raise StorageError("The file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise StorageError("That file is larger than 8 MB. Please upload a smaller image.")

    mime = _sniff_mime(raw[:16])
    if mime is None:
        raise StorageError(
            "That file type is not supported. Upload a JPG, PNG, WebP or PDF."
        )

    if mime == "application/pdf":
        return raw, mime, ".pdf"

    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            # Drops the alpha channel along with everything else we do not
            # want to keep; receipts do not need transparency.
            rgb = img.convert("RGB")
            out = io.BytesIO()
            rgb.save(out, format="JPEG", quality=85, optimize=True)
            return out.getvalue(), "image/jpeg", ".jpg"
    except StorageError:
        raise
    except Exception as exc:
        logger.warning("receipt_reencode_failed bytes=%d error=%s", len(raw), exc)
        raise StorageError("That image could not be read. Try re-taking the screenshot.") from exc


def build_key(user_id, claim_id, ext: str) -> str:
    """Object key. The user's own filename is discarded — it is
    attacker-controlled and carries no information we need."""
    return f"receipts/{user_id}/{claim_id}/{uuid.uuid4().hex}{ext}"


# ── Backends ────────────────────────────────────────────────────────────────

class _LocalBackend:
    """Development backend. Writes under a git-ignored directory and hands back
    a path rather than a URL — the admin API streams these instead."""

    def __init__(self, root: str):
        self.root = Path(root)

    def put(self, key: str, data: bytes, mime: str) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        path = self.root / key
        if not path.exists():
            raise StorageError("Receipt file is missing.")
        return path.read_bytes()

    def presign(self, key: str, filename: str) -> str | None:
        # No signing locally; the caller falls back to streaming the bytes.
        return None

    def delete(self, key: str) -> None:
        path = self.root / key
        if path.exists():
            path.unlink()


class _S3Backend:
    def __init__(self):
        import boto3
        from botocore.config import Config

        self.bucket = settings.RECEIPTS_BUCKET
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.RECEIPTS_ENDPOINT,
            region_name=settings.RECEIPTS_REGION or "auto",
            aws_access_key_id=settings.RECEIPTS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.RECEIPTS_SECRET_ACCESS_KEY,
            # Railway Buckets use virtual-hosted-style URLs, which is also
            # boto3's default; pinned to sigv4 so presigning is deterministic.
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )

    def put(self, key: str, data: bytes, mime: str) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=mime,
            # Belt and braces: even if a presigned URL is opened directly, the
            # browser is told to save the file rather than render it.
            ContentDisposition="attachment",
        )

    def get(self, key: str) -> bytes:
        obj = self.client.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read()

    def presign(self, key: str, filename: str) -> str | None:
        return self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
            },
            ExpiresIn=PRESIGN_TTL_SECONDS,
        )

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


_backend = None


def backend():
    global _backend
    if _backend is None:
        if settings.receipts_configured:
            _backend = _S3Backend()
            logger.info("receipt storage: s3 bucket=%s", settings.RECEIPTS_BUCKET)
        else:
            root = os.environ.get("RECEIPTS_LOCAL_DIR", ".receipts")
            _backend = _LocalBackend(root)
            logger.warning(
                "receipt storage: LOCAL (%s) — bucket credentials absent", root
            )
    return _backend


def put(key: str, data: bytes, mime: str) -> None:
    backend().put(key, data, mime)


def get(key: str) -> bytes:
    return backend().get(key)


def presign(key: str, filename: str = "receipt") -> str | None:
    return backend().presign(key, filename)


def delete(key: str) -> None:
    backend().delete(key)


def extension_for(mime: str) -> str:
    return ALLOWED_MIME.get(mime) or mimetypes.guess_extension(mime) or ".bin"
