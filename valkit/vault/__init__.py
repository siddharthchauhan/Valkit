"""Immutable, content-addressed evidence storage.

:class:`EvidenceVault` stores evidence on a local filesystem;
:class:`S3EvidenceVault` stores it on S3 under Object Lock. Both satisfy the
:class:`EvidenceStore` protocol, so nothing upstream needs to know which is in
use. ``S3EvidenceVault`` imports boto3 lazily and is safe to reference on a
machine that does not have it.
"""

from __future__ import annotations

from .s3 import S3EvidenceVault
from .store import (
    DEFAULT_RETENTION_YEARS,
    EvidenceManifest,
    EvidenceStore,
    EvidenceVault,
    VaultVerification,
)

__all__ = [
    "EvidenceVault",
    "EvidenceStore",
    "S3EvidenceVault",
    "VaultVerification",
    "EvidenceManifest",
    "DEFAULT_RETENTION_YEARS",
]
