"""Consent evidence (signed forms) in S3-compatible object storage.

Garage in OpenG2P commons. boto3 is synchronous, so every call runs in a
worker thread. With no endpoint configured the store is absent and callers
answer 503. The bucket is created on first use if it does not exist.
"""
import asyncio
import hashlib
import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Optional

from openg2p_fastapi_common.service import BaseService

from ..config import Settings
from ..models import ConsentEvidence

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

# Leading bytes of each allowed type: the declared content type must match
# what the file actually is.
_MAGIC = {
    "application/pdf": (b"%PDF-",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
}


class EvidenceError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class S3EvidenceStore:
    """put/get/delete of evidence objects in one bucket (sync; call in a thread)."""

    def __init__(self):
        self._client = None
        self._bucket_ready = False
        self._lock = threading.Lock()

    def _s3(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            options = {"signature_version": "s3v4", "s3": {"addressing_style": "path"}}
            try:
                # Garage does not take the newer default (CRC) request checksums.
                config = Config(
                    **options,
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                )
            except TypeError:  # botocore < 1.36
                config = Config(**options)
            self._client = boto3.client(
                "s3",
                endpoint_url=_config.evidence_s3_endpoint,
                aws_access_key_id=_config.evidence_s3_access_key or None,
                aws_secret_access_key=_config.evidence_s3_secret_key or None,
                region_name=_config.evidence_s3_region,
                config=config,
            )
        return self._client

    def _ensure_bucket(self):
        if self._bucket_ready:
            return
        with self._lock:
            if self._bucket_ready:
                return
            from botocore.exceptions import ClientError

            s3 = self._s3()
            bucket = _config.evidence_s3_bucket
            try:
                s3.head_bucket(Bucket=bucket)
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code"))
                if code not in ("404", "NoSuchBucket", "NotFound"):
                    raise
                _logger.info("Creating evidence bucket %s", bucket)
                try:
                    s3.create_bucket(Bucket=bucket)
                except ClientError as create_exc:
                    code = str(create_exc.response.get("Error", {}).get("Code"))
                    if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                        raise
            self._bucket_ready = True

    def put(self, key: str, data: bytes, content_type: str, sha256: str) -> None:
        self._ensure_bucket()
        self._s3().put_object(
            Bucket=_config.evidence_s3_bucket, Key=key, Body=data,
            ContentType=content_type, Metadata={"sha256": sha256},
        )

    def get(self, key: str) -> bytes:
        self._ensure_bucket()
        response = self._s3().get_object(Bucket=_config.evidence_s3_bucket, Key=key)
        return response["Body"].read()

    def delete(self, key: str) -> None:
        self._ensure_bucket()
        self._s3().delete_object(Bucket=_config.evidence_s3_bucket, Key=key)


def safe_filename(name: Optional[str]) -> str:
    """The file's base name, stripped of path parts and control characters."""
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = re.sub(r"[\x00-\x1f\x7f]", "", base)
    return (base or "evidence")[:255]


class EvidenceService(BaseService):
    """Validates and stores evidence files; rows are written by the caller in
    the same transaction as its request checks."""

    def __init__(self, name="", **kwargs):
        super().__init__(name, **kwargs)
        self._s3_store: Optional[S3EvidenceStore] = None
        # Tests (or another backend) may set a store with put/get/delete.
        self.store_override = None

    def store(self):
        if self.store_override is not None:
            return self.store_override
        if not _config.evidence_s3_endpoint:
            return None
        if self._s3_store is None:
            self._s3_store = S3EvidenceStore()
        return self._s3_store

    def require_store(self):
        store = self.store()
        if store is None:
            raise EvidenceError(
                503, "evidence storage is not configured on this Consent Manager"
            )
        return store

    @staticmethod
    def check_file(data: bytes, content_type: Optional[str]) -> str:
        """Validate type and size; return the normalised content type."""
        ctype = (content_type or "").split(";")[0].strip().lower()
        if ctype not in [t.lower() for t in _config.evidence_allowed_types]:
            raise EvidenceError(
                415,
                f"content type '{ctype or 'unknown'}' not allowed; allowed: "
                f"{', '.join(_config.evidence_allowed_types)}",
            )
        if not data:
            raise EvidenceError(422, "the file is empty")
        if len(data) > _config.evidence_max_bytes:
            raise EvidenceError(
                413, f"the file is larger than {_config.evidence_max_bytes} bytes"
            )
        magic = _MAGIC.get(ctype)
        if magic and not any(data.startswith(m) for m in magic):
            raise EvidenceError(415, f"the file content is not {ctype}")
        return ctype

    async def put(self, request_id: str, data: bytes, content_type: str, filename: str,
                  kind: str, uploaded_by: str) -> ConsentEvidence:
        """Store the bytes; return the (unsaved) evidence row describing them."""
        store = self.require_store()
        evidence = ConsentEvidence(
            consent_request_id=request_id, kind=kind, filename=safe_filename(filename),
            content_type=content_type, size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(), uploaded_by=uploaded_by,
            uploaded_at=datetime.now(timezone.utc), storage_key="",
        )
        evidence.storage_key = f"consent-requests/{request_id}/{evidence.id}"
        try:
            await asyncio.to_thread(
                store.put, evidence.storage_key, data, content_type, evidence.sha256
            )
        except Exception as exc:
            _logger.error("Evidence upload to object storage failed: %s", exc)
            raise EvidenceError(503, "evidence storage is unavailable") from exc
        return evidence

    async def get(self, evidence: ConsentEvidence) -> bytes:
        store = self.require_store()
        try:
            return await asyncio.to_thread(store.get, evidence.storage_key)
        except Exception as exc:
            _logger.error("Evidence download from object storage failed: %s", exc)
            raise EvidenceError(503, "evidence storage is unavailable") from exc

    async def delete_object(self, storage_key: str) -> None:
        """Best-effort: an orphaned object is logged, not an error for the caller."""
        store = self.store()
        if store is None:
            return
        try:
            await asyncio.to_thread(store.delete, storage_key)
        except Exception as exc:
            _logger.warning("Could not delete evidence object %s: %s", storage_key, exc)
