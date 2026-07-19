#!/usr/bin/env python3
# Copyright 2024-2026 The NoKV Authors.
# SPDX-License-Identifier: Apache-2.0

"""Canonical LingTai Shared Workspace launcher grants.

The encoded grant is launch identity, not a general credential format.  This
module deliberately accepts exactly the v1 object emitted by the supported
LingTai Workbench launcher and keeps its Python validation aligned with the
native Shared Workspace Provider.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


GRANT_SCHEMA = "nokv.lingtai.workspace_grant.v1"
GRANT_ISSUER = "lingtai-workbench-sync"
GRANT_AUDIENCE = "nokv-mcp:lingtai"
MAX_GRANT_LIFETIME_MS = 30 * 24 * 60 * 60 * 1_000
MAX_U64 = (1 << 64) - 1
WORKSPACE_GRANT_FIELDS = (
    "schema",
    "grant_id",
    "issuer",
    "audience",
    "workspace_id",
    "actor_id",
    "role",
    "issued_at_unix_ms",
    "expires_at_unix_ms",
)

_GRANT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z", re.ASCII)
_BASE64URL_NO_PAD = re.compile(r"[A-Za-z0-9_-]+\Z", re.ASCII)


@dataclass(frozen=True)
class WorkspaceGrant:
    schema: str
    grant_id: str
    issuer: str
    audience: str
    workspace_id: str
    actor_id: str
    role: str
    issued_at_unix_ms: int
    expires_at_unix_ms: int


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"workspace grant {field} must be a string")
    return value


def _require_u64(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"workspace grant {field} must be an unsigned 64-bit integer")
    if value < 0 or value > MAX_U64:
        raise ValueError(f"workspace grant {field} must be an unsigned 64-bit integer")
    return value


def validate_workspace_identity(value: object, *, field: str) -> str:
    identity = _require_string(value, field)
    try:
        encoded = identity.encode("utf-8")
    except UnicodeEncodeError as err:
        raise ValueError(f"workspace grant {field} must be valid UTF-8") from err
    if not 1 <= len(encoded) <= 64:
        raise ValueError(f"workspace grant {field} must contain 1..64 UTF-8 bytes")
    if identity.strip() != identity:
        raise ValueError(
            f"workspace grant {field} must not have leading or trailing whitespace"
        )
    if any(unicodedata.category(character) == "Cc" for character in identity):
        raise ValueError(
            f"workspace grant {field} must not contain Unicode control characters"
        )
    return identity


def _validate_grant(
    grant: WorkspaceGrant,
    *,
    workspace_id: object | None = None,
    actor_id: object | None = None,
    now_unix_ms: object | None = None,
) -> None:
    schema = _require_string(grant.schema, "schema")
    grant_id = _require_string(grant.grant_id, "grant_id")
    issuer = _require_string(grant.issuer, "issuer")
    audience = _require_string(grant.audience, "audience")
    grant_workspace_id = validate_workspace_identity(
        grant.workspace_id, field="workspace_id"
    )
    grant_actor_id = validate_workspace_identity(grant.actor_id, field="actor_id")
    role = _require_string(grant.role, "role")
    issued_at = _require_u64(grant.issued_at_unix_ms, "issued_at_unix_ms")
    expires_at = _require_u64(grant.expires_at_unix_ms, "expires_at_unix_ms")

    if schema != GRANT_SCHEMA or issuer != GRANT_ISSUER or audience != GRANT_AUDIENCE:
        raise ValueError("workspace grant constants do not match the LingTai v1 audience")
    if _GRANT_ID.fullmatch(grant_id) is None:
        raise ValueError("workspace grant grant_id must match [A-Za-z0-9_-]{1,64}")
    if role not in {"reader", "writer"}:
        raise ValueError("workspace grant role must be reader or writer")

    lifetime = expires_at - issued_at
    if lifetime <= 0 or lifetime > MAX_GRANT_LIFETIME_MS:
        raise ValueError(
            "workspace grant lifetime must be positive and at most 30 days"
        )

    if workspace_id is not None or actor_id is not None:
        if workspace_id is None or actor_id is None:
            raise ValueError(
                "workspace grant validation requires both workspace_id and actor_id"
            )
        expected_workspace_id = validate_workspace_identity(
            workspace_id, field="explicit workspace_id"
        )
        expected_actor_id = validate_workspace_identity(
            actor_id, field="explicit actor_id"
        )
        if (
            grant_workspace_id != expected_workspace_id
            or grant_actor_id != expected_actor_id
        ):
            raise ValueError(
                "workspace grant identity does not match the explicit workspace tuple"
            )

    if now_unix_ms is not None:
        now = _require_u64(now_unix_ms, "current time")
        if issued_at > now or now >= expires_at:
            raise ValueError("workspace grant is not current")


def canonical_workspace_grant_bytes(grant: WorkspaceGrant) -> bytes:
    if not isinstance(grant, WorkspaceGrant):
        raise ValueError("workspace grant must be a WorkspaceGrant")
    _validate_grant(grant)
    fields = _workspace_grant_fields(grant)
    try:
        return json.dumps(
            fields,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError) as err:
        raise ValueError("workspace grant could not be encoded as canonical UTF-8 JSON") from err


def encode_workspace_grant(grant: WorkspaceGrant) -> str:
    encoded = base64.urlsafe_b64encode(canonical_workspace_grant_bytes(grant))
    return encoded.decode("ascii").rstrip("=")


def workspace_grant_sha256(grant: WorkspaceGrant) -> str:
    """Return SHA-256 of the decoded canonical UTF-8 JSON bytes."""

    return hashlib.sha256(canonical_workspace_grant_bytes(grant)).hexdigest()


def _workspace_grant_fields(grant: WorkspaceGrant) -> dict[str, str | int]:
    return {
        "schema": grant.schema,
        "grant_id": grant.grant_id,
        "issuer": grant.issuer,
        "audience": grant.audience,
        "workspace_id": grant.workspace_id,
        "actor_id": grant.actor_id,
        "role": grant.role,
        "issued_at_unix_ms": grant.issued_at_unix_ms,
        "expires_at_unix_ms": grant.expires_at_unix_ms,
    }


def workspace_grant_lock_fields(grant: WorkspaceGrant) -> dict[str, str | int]:
    if not isinstance(grant, WorkspaceGrant):
        raise ValueError("workspace grant must be a WorkspaceGrant")
    _validate_grant(grant)
    return _workspace_grant_fields(grant)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"workspace grant JSON contains invalid constant {value}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"workspace grant JSON contains duplicate field {key}")
        result[key] = value
    return result


def _grant_from_exact_fields(fields: Mapping[str, Any]) -> WorkspaceGrant:
    if any(not isinstance(key, str) for key in fields):
        raise ValueError("workspace grant field names must be strings")
    expected = set(WORKSPACE_GRANT_FIELDS)
    actual = set(fields)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unknown:
            details.append(f"unknown {', '.join(unknown)}")
        raise ValueError(
            "workspace grant must contain exactly the v1 fields: " + "; ".join(details)
        )
    return WorkspaceGrant(**{field: fields[field] for field in WORKSPACE_GRANT_FIELDS})


def parse_workspace_grant(
    encoded: object,
    *,
    workspace_id: object,
    actor_id: object,
    now_unix_ms: object | None = None,
) -> WorkspaceGrant:
    if not isinstance(encoded, str) or _BASE64URL_NO_PAD.fullmatch(encoded) is None:
        raise ValueError(
            "workspace grant must be canonical URL-safe Base64 without padding"
        )
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    try:
        decoded = base64.b64decode(
            (encoded + padding).encode("ascii"), altchars=b"-_", validate=True
        )
    except (UnicodeEncodeError, binascii.Error, ValueError) as err:
        raise ValueError(
            "workspace grant must be canonical URL-safe Base64 without padding"
        ) from err
    if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != encoded:
        raise ValueError(
            "workspace grant must be canonical URL-safe Base64 without padding"
        )
    try:
        fields = json.loads(
            decoded,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as err:
        raise ValueError(
            "workspace grant must be canonical grant JSON with exactly the v1 fields"
        ) from err
    if not isinstance(fields, dict):
        raise ValueError("workspace grant JSON must be an object")
    grant = _grant_from_exact_fields(fields)
    _validate_grant(
        grant,
        workspace_id=workspace_id,
        actor_id=actor_id,
        now_unix_ms=(time.time_ns() // 1_000_000 if now_unix_ms is None else now_unix_ms),
    )
    if decoded != canonical_workspace_grant_bytes(grant):
        raise ValueError("workspace grant decoded bytes are not canonical UTF-8 JSON")
    return grant


def workspace_grant_from_lock_fields(
    fields: object,
    *,
    workspace_id: object,
    actor_id: object,
    now_unix_ms: object | None = None,
) -> WorkspaceGrant:
    if not isinstance(fields, Mapping):
        raise ValueError("workspace grant lock fields must be a JSON object")
    grant = _grant_from_exact_fields(fields)
    _validate_grant(
        grant,
        workspace_id=workspace_id,
        actor_id=actor_id,
        now_unix_ms=(time.time_ns() // 1_000_000 if now_unix_ms is None else now_unix_ms),
    )
    return grant
