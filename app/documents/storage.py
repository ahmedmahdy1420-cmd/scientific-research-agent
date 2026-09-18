"""Object storage abstraction: local disk or S3.

Local is the default so the project runs with no AWS account. The S3 backend is
the real one and is what ECS uses, reached through the task role rather than
access keys — boto3 picks up the role automatically, so there is no credential
handling code here at all.

Keys are content-addressed (`documents/<sha256>/<safe-name>.pdf`). Uploading the
same bytes twice therefore produces the same key, which makes ingestion
idempotent for free: a retried Celery task cannot create a duplicate object.
"""

from __future__ import annotations

import abc
import hashlib
import re
import shutil
from pathlib import Path
from typing import BinaryIO

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import get_logger

log = get_logger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class StorageError(AppError):
    code = "storage_error"
    http_status = 503
    retryable = True


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_filename(name: str) -> str:
    """Strip path separators and anything exotic.

    Defends against a filename like `../../etc/passwd` or a null byte being
    used to escape the storage prefix.
    """
    cleaned = _UNSAFE.sub("_", Path(name).name).strip("._") or "document"
    return cleaned[:120]


def build_key(content_hash: str, filename: str) -> str:
    return f"documents/{content_hash[:2]}/{content_hash}/{safe_filename(filename)}"


class ObjectStorage(abc.ABC):
    backend: str = "abstract"

    @abc.abstractmethod
    def put(self, key: str, data: bytes, content_type: str = "application/pdf") -> str: ...

    @abc.abstractmethod
    def get(self, key: str) -> bytes: ...

    @abc.abstractmethod
    def exists(self, key: str) -> bool: ...

    @abc.abstractmethod
    def delete(self, key: str) -> None: ...

    @abc.abstractmethod
    def open_stream(self, key: str) -> BinaryIO: ...


class LocalObjectStorage(ObjectStorage):
    """Filesystem-backed storage for local development and tests."""

    backend = "local"

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        candidate = (self._root / key).resolve()
        # Refuse to read or write outside the storage root, whatever the key says.
        if not str(candidate).startswith(str(self._root)):
            raise StorageError("Refusing to access a path outside the storage root")
        return candidate

    def put(self, key: str, data: bytes, content_type: str = "application/pdf") -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        log.info("storage.put", backend=self.backend, key=key, bytes=len(data))
        return key

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise StorageError(f"Object not found: {key}")
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()
            parent = path.parent
            if parent != self._root and not any(parent.iterdir()):
                shutil.rmtree(parent, ignore_errors=True)

    def open_stream(self, key: str) -> BinaryIO:
        path = self._path(key)
        if not path.exists():
            raise StorageError(f"Object not found: {key}")
        return path.open("rb")


class S3ObjectStorage(ObjectStorage):
    """S3 (or any S3-compatible endpoint, e.g. MinIO)."""

    backend = "s3"

    def __init__(self, bucket: str, region: str, endpoint_url: str | None = None) -> None:
        import boto3
        from botocore.config import Config

        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url or None,
            config=Config(
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=30,
            ),
        )

    def put(self, key: str, data: bytes, content_type: str = "application/pdf") -> str:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                # Bucket policy also enforces this; belt and braces.
                ServerSideEncryption="AES256",
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"S3 put failed for {key}: {exc}") from exc
        log.info("storage.put", backend=self.backend, key=key, bytes=len(data))
        return key

    def get(self, key: str) -> bytes:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            return bytes(response["Body"].read())
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"S3 get failed for {key}: {exc}") from exc

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError:
            return False

    def delete(self, key: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"S3 delete failed for {key}: {exc}") from exc

    def open_stream(self, key: str) -> BinaryIO:
        import io

        return io.BytesIO(self.get(key))


_storage: ObjectStorage | None = None


def get_storage(settings: Settings | None = None) -> ObjectStorage:
    global _storage
    if _storage is None:
        cfg = settings or get_settings()
        if cfg.storage_backend == "s3":
            _storage = S3ObjectStorage(cfg.s3_bucket, cfg.aws_region, cfg.s3_endpoint_url)
        else:
            _storage = LocalObjectStorage(cfg.local_storage_path)
        log.info("storage.backend_selected", backend=_storage.backend)
    return _storage


def set_storage(storage: ObjectStorage | None) -> None:
    """Test hook."""
    global _storage
    _storage = storage
