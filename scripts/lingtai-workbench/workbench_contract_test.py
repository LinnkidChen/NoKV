#!/usr/bin/env python3
# Copyright 2024-2026 The NoKV Authors.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import workbench_contract as contract  # noqa: E402


def frozen_tools(*, schema_key: str = "inputSchema") -> list[dict]:
    return [
        {
            "name": name,
            "description": f"description for {name}",
            schema_key: copy.deepcopy(contract.FROZEN_INPUT_SCHEMAS[name]),
        }
        for name in contract.FROZEN_TOOL_ORDER
    ]


def lingtai_tools(role: str, *, schema_key: str = "inputSchema") -> list[dict]:
    tools = frozen_tools(schema_key=schema_key)
    suffix_names = contract.FROZEN_LINGTAI_ROLE_TOOL_ORDER[role]
    tools.extend(
        {
            "name": name,
            "description": contract.FROZEN_SHARED_TOOL_DEFINITIONS[name][
                "description"
            ],
            schema_key: copy.deepcopy(
                contract.FROZEN_SHARED_TOOL_DEFINITIONS[name]["inputSchema"]
            ),
        }
        for name in suffix_names
    )
    return tools


def reverse_unordered_arrays(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {
                "allOf",
                "anyOf",
                "enum",
                "oneOf",
                "required",
                "type",
            } and isinstance(item, list):
                item.reverse()
            reverse_unordered_arrays(item)
    elif isinstance(value, list):
        for item in value:
            reverse_unordered_arrays(item)


class WorkbenchContractTest(unittest.TestCase):
    def test_frozen_surface_is_the_exact_eighteen_tools(self):
        tools = frozen_tools()
        self.assertEqual(len(contract.FROZEN_INPUT_SCHEMAS), 18)
        self.assertEqual(set(contract.FROZEN_INPUT_SCHEMAS), contract.WORKBENCH_TOOLS)
        contract.validate_tool_contract(tools)
        contract.validate_tool_order(tools)
        evidence = contract.contract_evidence(tools)
        self.assertEqual(evidence["tool_order"], list(contract.FROZEN_TOOL_ORDER))
        self.assertEqual(
            evidence["tool_order_sha256"],
            contract.json_sha256(list(contract.FROZEN_TOOL_ORDER)),
        )
        self.assertEqual(
            evidence["contract_sha256"],
            contract.json_sha256(
                {
                    key: value
                    for key, value in evidence.items()
                    if key != "contract_sha256"
                }
            ),
        )

    def test_contract_digest_covers_every_transition_evidence_field(self):
        evidence = contract.contract_evidence(frozen_tools())
        for field in evidence:
            if field == "contract_sha256":
                continue
            with self.subTest(field=field):
                changed = copy.deepcopy(evidence)
                changed[field] = f"changed-{field}"
                self.assertNotEqual(
                    evidence["contract_sha256"],
                    contract.json_sha256(
                        {
                            key: value
                            for key, value in changed.items()
                            if key != "contract_sha256"
                        }
                    ),
                )

    def test_contract_evidence_rejects_swapped_tool_order(self):
        tools = frozen_tools()
        tools[0], tools[1] = tools[1], tools[0]
        with self.assertRaisesRegex(
            contract.WorkbenchContractError,
            r"^workbench tools/list order differs",
        ):
            contract.contract_evidence(tools)

    def test_every_tool_schema_is_compared(self):
        for name in sorted(contract.WORKBENCH_TOOLS):
            with self.subTest(tool=name):
                tools = frozen_tools()
                tool = next(item for item in tools if item["name"] == name)
                tool["inputSchema"]["maxProperties"] = 999
                with self.assertRaisesRegex(
                    contract.WorkbenchContractError,
                    rf"^{name} inputSchema differs",
                ):
                    contract.validate_tool_contract(tools)

    def test_missing_field_and_extra_restriction_fail(self):
        missing = frozen_tools()
        create = next(tool for tool in missing if tool["name"] == "workbench_create")
        del create["inputSchema"]["properties"]["id"]
        with self.assertRaises(contract.WorkbenchContractError):
            contract.validate_tool_contract(missing)

        restricted = frozen_tools()
        create = next(tool for tool in restricted if tool["name"] == "workbench_create")
        create["inputSchema"]["properties"]["id"]["maxLength"] = 128
        with self.assertRaises(contract.WorkbenchContractError):
            contract.validate_tool_contract(restricted)

    def test_annotations_are_recursively_ignored(self):
        tools = frozen_tools()
        for tool in tools:
            schema = tool["inputSchema"]
            schema.update(
                {
                    "$comment": "generated from Rust",
                    "default": {},
                    "deprecated": False,
                    "description": "updated wording",
                    "example": {},
                    "examples": [],
                    "readOnly": False,
                    "title": "Workbench input",
                    "writeOnly": False,
                }
            )
        restore = next(tool for tool in tools if tool["name"] == contract.RESTORE_TOOL)
        restore["inputSchema"]["properties"]["at_snapshot"]["anyOf"][0][
            "description"
        ] = "nested wording"

        contract.validate_tool_contract(tools)
        self.assertEqual(
            contract.contract_evidence(tools), contract.expected_contract_evidence()
        )

    def test_unordered_schema_arrays_do_not_change_evidence(self):
        tools = frozen_tools()
        reverse_unordered_arrays(tools)

        contract.validate_tool_contract(tools)
        self.assertEqual(
            contract.contract_evidence(tools), contract.expected_contract_evidence()
        )

    def test_one_of_and_all_of_order_is_semantic(self):
        first = {
            "allOf": [{"type": "string"}, {"minLength": 1}],
            "oneOf": [{"const": "a"}, {"const": "b"}],
        }
        second = {
            "allOf": list(reversed(first["allOf"])),
            "oneOf": list(reversed(first["oneOf"])),
        }
        self.assertEqual(
            contract.normalize_schema(first), contract.normalize_schema(second)
        )

    def test_annotation_named_property_and_enum_data_are_preserved(self):
        schema = {
            "description": "outer annotation",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "inner annotation",
                }
            },
            "enum": [{"description": "literal data"}],
        }
        self.assertEqual(
            contract.normalize_schema(schema),
            {
                "properties": {"description": {"type": "string"}},
                "enum": [{"description": "literal data"}],
            },
        )

    def test_alternate_schema_key_uses_the_same_frozen_contract(self):
        tools = frozen_tools(schema_key="input_schema")
        contract.validate_tool_contract(tools, schema_key="input_schema")
        self.assertEqual(
            contract.contract_evidence(tools, schema_key="input_schema"),
            contract.expected_contract_evidence(),
        )

    def test_workbench_profile_api_is_byte_compatible(self):
        tools = frozen_tools()
        self.assertEqual(
            contract.profile_contract_evidence(
                tools,
                "workbench",
                role=None,
            ),
            contract.contract_evidence(tools),
        )
        self.assertEqual(
            contract.expected_profile_contract_evidence(
                "workbench",
                role=None,
            ),
            contract.expected_contract_evidence(),
        )

    def test_expected_lingtai_role_contracts_are_frozen(self):
        expected = {
            "reader": (
                20,
                "e008fc0a776c3348ec0ddae3db9eebc01ea37eed3b723a86004eae110d94fc2f",
            ),
            "writer": (
                23,
                "1e3f09616286dcd8069f91319bfaee356ce79bf705f0806a38b38b7b972989f1",
            ),
        }
        for role, (count, raw_digest) in expected.items():
            with self.subTest(role=role):
                evidence = contract.expected_profile_contract_evidence(
                    "lingtai",
                    role=role,
                )
                self.assertEqual(evidence["profile"], "lingtai")
                self.assertEqual(evidence["role"], role)
                self.assertEqual(evidence["tool_count"], count)
                self.assertEqual(evidence["raw_contract_sha256"], raw_digest)
                self.assertEqual(
                    evidence["tool_order"][:18],
                    list(contract.FROZEN_TOOL_ORDER),
                )
                self.assertEqual(
                    evidence["tool_order"][18:],
                    list(contract.FROZEN_LINGTAI_ROLE_TOOL_ORDER[role]),
                )

    def test_shared_suffix_contract_matches_provider_handoff(self):
        expected = {
            "reader": (
                ("workspace_list", "workspace_read"),
                "76f7a6cb9e106c0d7aa4ac8969ba909cdb22464fffcecf4ef87b71a2b04a2fb5",
            ),
            "writer": (
                (
                    "workspace_list",
                    "workspace_read",
                    "workspace_put_file",
                    "workspace_edit",
                    "workspace_append",
                ),
                "eba00ee41c6e31760470ba495274fa0a7c66a5580404017a4c67e688e1c1ba4e",
            ),
        }
        for role, (order, digest) in expected.items():
            with self.subTest(role=role):
                self.assertEqual(
                    contract.FROZEN_LINGTAI_ROLE_TOOL_ORDER[role],
                    order,
                )
                tools = [
                    {
                        "name": name,
                        **contract.FROZEN_SHARED_TOOL_DEFINITIONS[name],
                    }
                    for name in order
                ]
                self.assertEqual(
                    contract.raw_tool_definitions_sha256(tools),
                    digest,
                )

    def test_lingtai_profile_accepts_exact_prefix_suffix_and_raw_digest(self):
        for role in ("reader", "writer"):
            with self.subTest(role=role):
                tools = lingtai_tools(role)
                digest = contract.raw_tool_definitions_sha256(tools)
                with mock.patch.dict(
                    contract.FROZEN_LINGTAI_PROFILE_DIGESTS,
                    {role: digest},
                ):
                    evidence = contract.profile_contract_evidence(
                        tools,
                        "lingtai",
                        role=role,
                    )
                self.assertEqual(evidence["profile"], "lingtai")
                self.assertEqual(evidence["role"], role)
                self.assertEqual(evidence["raw_contract_sha256"], digest)

    def test_lingtai_profile_rejects_missing_extra_and_wrong_role(self):
        missing = lingtai_tools("reader")[:-1]
        with self.assertRaisesRegex(
            contract.WorkbenchContractError,
            r"^lingtai reader tool surface differs",
        ):
            contract.profile_contract_evidence(missing, "lingtai", role="reader")

        extra = lingtai_tools("writer") + [
            {"name": "unexpected", "description": "extra", "inputSchema": {}}
        ]
        with self.assertRaisesRegex(
            contract.WorkbenchContractError,
            r"^lingtai writer tool surface differs",
        ):
            contract.profile_contract_evidence(extra, "lingtai", role="writer")

        with self.assertRaisesRegex(
            contract.WorkbenchContractError,
            r"^lingtai reader tool surface differs",
        ):
            contract.profile_contract_evidence(
                lingtai_tools("writer"),
                "lingtai",
                role="reader",
            )

    def test_lingtai_profile_rejects_prefix_and_suffix_drift(self):
        prefix_schema = lingtai_tools("reader")
        prefix_schema[0]["inputSchema"]["maxProperties"] = 999
        with self.assertRaisesRegex(
            contract.WorkbenchContractError,
            r"^workbench_create inputSchema differs",
        ):
            contract.profile_contract_evidence(
                prefix_schema,
                "lingtai",
                role="reader",
            )

        suffix_schema = lingtai_tools("reader")
        suffix_schema[-1]["inputSchema"]["maxProperties"] = 999
        with self.assertRaisesRegex(
            contract.WorkbenchContractError,
            r"^workspace_read inputSchema differs",
        ):
            contract.profile_contract_evidence(
                suffix_schema,
                "lingtai",
                role="reader",
            )

        suffix_description = lingtai_tools("reader")
        suffix_description[-1]["description"] = "changed"
        with self.assertRaisesRegex(
            contract.WorkbenchContractError,
            r"^workspace_read description differs",
        ):
            contract.profile_contract_evidence(
                suffix_description,
                "lingtai",
                role="reader",
            )

    def test_lingtai_profile_rejects_order_and_prefix_description_drift(self):
        suffix_order = lingtai_tools("reader")
        suffix_order[-2], suffix_order[-1] = suffix_order[-1], suffix_order[-2]
        with self.assertRaisesRegex(
            contract.WorkbenchContractError,
            r"^lingtai reader tools/list order differs",
        ):
            contract.profile_contract_evidence(
                suffix_order,
                "lingtai",
                role="reader",
            )

        prefix_description = lingtai_tools("reader")
        prefix_description[0]["description"] = "changed"
        with self.assertRaisesRegex(
            contract.WorkbenchContractError,
            r"^lingtai reader raw tool definitions differ",
        ):
            contract.profile_contract_evidence(
                prefix_description,
                "lingtai",
                role="reader",
            )

    def test_profile_and_role_are_validated(self):
        with self.assertRaisesRegex(
            contract.WorkbenchContractError,
            r"^unsupported MCP profile",
        ):
            contract.profile_contract_evidence(frozen_tools(), "unknown", role=None)
        with self.assertRaisesRegex(
            contract.WorkbenchContractError,
            r"^unsupported MCP profile",
        ):
            contract.profile_contract_evidence(frozen_tools(), [], role=None)
        with self.assertRaisesRegex(
            contract.WorkbenchContractError,
            r"^workbench profile does not accept a workspace role",
        ):
            contract.profile_contract_evidence(
                frozen_tools(),
                "workbench",
                role="reader",
            )
        with self.assertRaisesRegex(
            contract.WorkbenchContractError,
            r"^lingtai profile requires workspace role reader or writer",
        ):
            contract.profile_contract_evidence(
                lingtai_tools("reader"),
                "lingtai",
                role=None,
            )


if __name__ == "__main__":
    unittest.main()
