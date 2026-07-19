#!/usr/bin/env python3
# Copyright 2024-2026 The NoKV Authors.
# SPDX-License-Identifier: Apache-2.0

import base64
import hashlib
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import workspace_grant as grant_module


class WorkspaceGrantTest(unittest.TestCase):
    NOW = 1_000_000

    def grant(self, **overrides):
        values = {
            "schema": grant_module.GRANT_SCHEMA,
            "grant_id": "grant_1",
            "issuer": grant_module.GRANT_ISSUER,
            "audience": grant_module.GRANT_AUDIENCE,
            "workspace_id": "team-alpha",
            "actor_id": "agent-7",
            "role": "writer",
            "issued_at_unix_ms": self.NOW - 1,
            "expires_at_unix_ms": self.NOW + 10_000,
        }
        values.update(overrides)
        return grant_module.WorkspaceGrant(**values)

    def encode_json(self, value, *, canonical=True):
        if canonical:
            raw = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        else:
            raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def parse(self, encoded):
        return grant_module.parse_workspace_grant(
            encoded,
            workspace_id="team-alpha",
            actor_id="agent-7",
            now_unix_ms=self.NOW,
        )

    def test_canonical_base64url_round_trip_and_sha256(self):
        grant = self.grant()

        encoded = grant_module.encode_workspace_grant(grant)
        parsed = self.parse(encoded)

        self.assertEqual(parsed, grant)
        self.assertNotIn("=", encoded)
        self.assertNotIn("+", encoded)
        self.assertNotIn("/", encoded)
        self.assertEqual(
            encoded,
            "eyJhY3Rvcl9pZCI6ImFnZW50LTciLCJhdWRpZW5jZSI6Im5va3YtbWNwOmxpbmd0"
            "YWkiLCJleHBpcmVzX2F0X3VuaXhfbXMiOjEwMTAwMDAsImdyYW50X2lkIjoiZ3Jhbn"
            "RfMSIsImlzc3VlZF9hdF91bml4X21zIjo5OTk5OTksImlzc3VlciI6Imxpbmd0YWkt"
            "d29ya2JlbmNoLXN5bmMiLCJyb2xlIjoid3JpdGVyIiwic2NoZW1hIjoibm9rdi5saW"
            "5ndGFpLndvcmtzcGFjZV9ncmFudC52MSIsIndvcmtzcGFjZV9pZCI6InRlYW0tYWxw"
            "aGEifQ",
        )
        canonical = grant_module.canonical_workspace_grant_bytes(grant)
        self.assertEqual(
            canonical,
            b'{"actor_id":"agent-7","audience":"nokv-mcp:lingtai",'
            b'"expires_at_unix_ms":1010000,"grant_id":"grant_1",'
            b'"issued_at_unix_ms":999999,"issuer":"lingtai-workbench-sync",'
            b'"role":"writer","schema":"nokv.lingtai.workspace_grant.v1",'
            b'"workspace_id":"team-alpha"}',
        )
        self.assertEqual(
            grant_module.workspace_grant_sha256(grant),
            hashlib.sha256(canonical).hexdigest(),
        )
        self.assertEqual(
            grant_module.workspace_grant_sha256(grant),
            "226085e4cbd6b50b4d512730ce52c03805f7e4179509eec1f0c8d8b3846c411f",
        )

    def test_parser_rejects_duplicate_unknown_and_noncanonical_json(self):
        grant = self.grant()
        canonical = grant_module.canonical_workspace_grant_bytes(grant)
        duplicate = canonical.replace(
            b'{"actor_id":"agent-7",',
            b'{"actor_id":"agent-7","actor_id":"agent-7",',
        )
        unknown = json.loads(canonical)
        unknown["extra"] = "no"
        noncanonical = json.loads(canonical)

        for encoded in (
            base64.urlsafe_b64encode(duplicate).decode("ascii").rstrip("="),
            self.encode_json(unknown),
            self.encode_json(noncanonical, canonical=False),
        ):
            with self.subTest(encoded=encoded):
                with self.assertRaises(ValueError):
                    self.parse(encoded)

    def test_parser_rejects_noncanonical_or_invalid_base64url(self):
        encoded = grant_module.encode_workspace_grant(self.grant())
        for invalid in (encoded + "=", encoded + "+", "***", "", "e30"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.parse(invalid)

    def test_constants_grant_id_and_role_are_exact(self):
        invalid_grants = (
            replace(self.grant(), schema="v2"),
            replace(self.grant(), issuer="other"),
            replace(self.grant(), audience="other"),
            replace(self.grant(), grant_id=""),
            replace(self.grant(), grant_id="bad.id"),
            replace(self.grant(), grant_id="a" * 65),
            replace(self.grant(), role="admin"),
        )
        for invalid in invalid_grants:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.parse(grant_module.encode_workspace_grant(invalid))

    def test_identity_is_utf8_byte_bounded_trimmed_and_control_free(self):
        maximum = "😀" * 16
        encoded = grant_module.encode_workspace_grant(
            self.grant(workspace_id=maximum, actor_id="代理")
        )
        parsed = grant_module.parse_workspace_grant(
            encoded,
            workspace_id=maximum,
            actor_id="代理",
            now_unix_ms=self.NOW,
        )
        self.assertEqual(parsed.workspace_id, maximum)

        for invalid in ("", " x", "x ", "x\n", "😀" * 17):
            with self.subTest(invalid=invalid):
                candidate = self.grant(workspace_id=invalid)
                with self.assertRaises(ValueError):
                    grant_module.parse_workspace_grant(
                        grant_module.encode_workspace_grant(candidate),
                        workspace_id=invalid,
                        actor_id="agent-7",
                        now_unix_ms=self.NOW,
                    )

    def test_times_are_u64_not_bool_current_and_at_most_thirty_days(self):
        max_lifetime = grant_module.MAX_GRANT_LIFETIME_MS
        valid = self.grant(
            issued_at_unix_ms=self.NOW,
            expires_at_unix_ms=self.NOW + max_lifetime,
        )
        self.assertEqual(self.parse(grant_module.encode_workspace_grant(valid)), valid)

        invalid_grants = (
            self.grant(issued_at_unix_ms=True),
            self.grant(expires_at_unix_ms=True),
            self.grant(issued_at_unix_ms=-1),
            self.grant(expires_at_unix_ms=grant_module.MAX_U64 + 1),
            self.grant(issued_at_unix_ms=self.NOW + 1),
            self.grant(expires_at_unix_ms=self.NOW),
            self.grant(
                issued_at_unix_ms=self.NOW,
                expires_at_unix_ms=self.NOW,
            ),
            self.grant(
                issued_at_unix_ms=self.NOW,
                expires_at_unix_ms=self.NOW + max_lifetime + 1,
            ),
        )
        for invalid in invalid_grants:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.parse(grant_module.encode_workspace_grant(invalid))

    def test_identity_binding_is_byte_exact(self):
        encoded = grant_module.encode_workspace_grant(self.grant())
        for workspace_id, actor_id in (
            ("TEAM-ALPHA", "agent-7"),
            ("team-alpha", "Agent-7"),
        ):
            with self.subTest(workspace_id=workspace_id, actor_id=actor_id):
                with self.assertRaisesRegex(ValueError, "identity"):
                    grant_module.parse_workspace_grant(
                        encoded,
                        workspace_id=workspace_id,
                        actor_id=actor_id,
                        now_unix_ms=self.NOW,
                    )

    def test_lock_fields_are_exact_and_reencode_the_same_grant(self):
        grant = self.grant()
        fields = grant_module.workspace_grant_lock_fields(grant)

        restored = grant_module.workspace_grant_from_lock_fields(
            fields,
            workspace_id="team-alpha",
            actor_id="agent-7",
            now_unix_ms=self.NOW,
        )

        self.assertEqual(restored, grant)
        self.assertEqual(
            grant_module.encode_workspace_grant(restored),
            grant_module.encode_workspace_grant(grant),
        )
        self.assertEqual(set(fields), set(grant_module.WORKSPACE_GRANT_FIELDS))

        malformed = (
            {key: value for key, value in fields.items() if key != "role"},
            {**fields, "extra": "no"},
            {**fields, "issued_at_unix_ms": True},
            {**fields, "grant_id": 7},
            {**fields, 7: "not-a-field-name"},
        )
        for candidate in malformed:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    grant_module.workspace_grant_from_lock_fields(
                        candidate,
                        workspace_id="team-alpha",
                        actor_id="agent-7",
                        now_unix_ms=self.NOW,
                    )


if __name__ == "__main__":
    unittest.main()
