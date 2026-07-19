#!/usr/bin/env python3
# Copyright 2024-2026 The NoKV Authors.
# SPDX-License-Identifier: Apache-2.0

"""Frozen semantic contract for LingTai's NoKV workbench MCP surface."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


RESTORE_TOOL = "workbench_restore"
REQUIRED_CAPABILITY = "restore_to_fork_v1"
BASE_WORKBENCH_TOOLS = {
    "workbench_create",
    "workbench_put_file",
    "workbench_append",
    "workbench_edit",
    "workbench_list",
    "workbench_stat",
    "workbench_read",
    "workbench_grep",
    "workbench_search",
    "workbench_aggregate",
    "workbench_catalog",
    "workbench_find",
    "workbench_commit",
    "workbench_snapshot",
    "workbench_snapshot_renew",
    "workbench_snapshot_retire",
    "workbench_snapshot_list",
}
WORKBENCH_TOOLS = BASE_WORKBENCH_TOOLS | {RESTORE_TOOL}
CONTRACT_SNAPSHOT_SCHEMA = "nokv.workbench.mcp_input_schemas.v1"
CONTRACT_SNAPSHOT_PATH = Path(__file__).with_name("workbench_contract_schema.json")
LINGTAI_CONTRACT_SNAPSHOT_SCHEMA = "nokv.lingtai.mcp_profile_contract.v1"
LINGTAI_CONTRACT_SNAPSHOT_PATH = Path(__file__).with_name(
    "lingtai_contract_schema.json"
)
WORKBENCH_PROFILE = "workbench"
LINGTAI_PROFILE = "lingtai"
LINGTAI_ROLES = frozenset({"reader", "writer"})

# JSON Schema annotations never change which tool arguments are accepted. Keep
# them out of the deployment digest so wording-only releases do not require a
# contract override. Validation keywords, including format and content*, stay
# in the comparison because clients may enforce them.
ANNOTATION_KEYWORDS = frozenset(
    {
        "$comment",
        "default",
        "deprecated",
        "description",
        "example",
        "examples",
        "readOnly",
        "title",
        "writeOnly",
    }
)
UNORDERED_SCHEMA_ARRAY_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf"})
UNORDERED_LITERAL_ARRAY_KEYWORDS = frozenset({"enum", "required", "type"})
SCHEMA_MAP_KEYWORDS = frozenset(
    {"$defs", "definitions", "dependentSchemas", "patternProperties", "properties"}
)
SCHEMA_VALUE_KEYWORDS = frozenset(
    {
        "additionalProperties",
        "contains",
        "contentSchema",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)


class WorkbenchContractError(ValueError):
    """A live MCP surface cannot satisfy the LingTai workbench contract."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_literal(value: Any) -> Any:
    """Canonicalize JSON data without treating its keys as schema keywords."""
    if isinstance(value, dict):
        return {key: _normalize_literal(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_literal(item) for item in value]
    return value


def _normalize_schema_value(value: Any) -> Any:
    if isinstance(value, list):
        return [normalize_schema(item) for item in value]
    return normalize_schema(value)


def normalize_schema(value: Any) -> Any:
    """Return a semantic JSON Schema form used for exact contract comparison.

    The normalizer deliberately does not resolve references or simplify schema
    logic. It only removes standard annotations and canonicalizes keywords whose
    array order has no validation meaning. Every remaining keyword must match
    the Rust-owned frozen contract exactly.
    """
    if isinstance(value, bool):
        return value
    if not isinstance(value, dict):
        return _normalize_literal(value)

    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if key in ANNOTATION_KEYWORDS:
            continue
        if key in UNORDERED_SCHEMA_ARRAY_KEYWORDS:
            branches = [normalize_schema(branch) for branch in item]
            normalized[key] = sorted(branches, key=canonical_json)
        elif key in UNORDERED_LITERAL_ARRAY_KEYWORDS and isinstance(item, list):
            values = [_normalize_literal(element) for element in item]
            normalized[key] = sorted(values, key=canonical_json)
        elif key in SCHEMA_MAP_KEYWORDS and isinstance(item, dict):
            # Map keys are user property/definition names. A property literally
            # named "description" must not be mistaken for an annotation.
            normalized[key] = {
                name: normalize_schema(schema) for name, schema in item.items()
            }
        elif key in SCHEMA_VALUE_KEYWORDS:
            normalized[key] = _normalize_schema_value(item)
        else:
            normalized[key] = _normalize_literal(item)
    return normalized


def _load_frozen_contract() -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
    try:
        snapshot = json.loads(CONTRACT_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise RuntimeError(
            f"cannot load frozen workbench contract {CONTRACT_SNAPSHOT_PATH}: {err}"
        ) from err
    if not isinstance(snapshot, dict):
        raise RuntimeError("frozen workbench contract must be a JSON object")
    if snapshot.get("schema") != CONTRACT_SNAPSHOT_SCHEMA:
        raise RuntimeError("frozen workbench contract has the wrong schema marker")
    schemas = snapshot.get("inputSchemas")
    if not isinstance(schemas, dict) or not all(
        isinstance(name, str) and isinstance(schema, dict)
        for name, schema in schemas.items()
    ):
        raise RuntimeError("frozen workbench contract has invalid inputSchemas")
    if set(schemas) != WORKBENCH_TOOLS:
        raise RuntimeError(
            "frozen workbench contract tool names differ from WORKBENCH_TOOLS"
        )
    order = snapshot.get("toolOrder")
    if (
        not isinstance(order, list)
        or not all(isinstance(name, str) for name in order)
        or len(order) != len(set(order))
        or set(order) != WORKBENCH_TOOLS
    ):
        raise RuntimeError("frozen workbench contract has invalid toolOrder")
    return (
        {name: normalize_schema(schema) for name, schema in schemas.items()},
        tuple(order),
    )


FROZEN_INPUT_SCHEMAS, FROZEN_TOOL_ORDER = _load_frozen_contract()


def _load_frozen_lingtai_contract() -> tuple[
    dict[str, dict[str, Any]],
    dict[str, tuple[str, ...]],
    dict[str, str],
]:
    try:
        snapshot = json.loads(
            LINGTAI_CONTRACT_SNAPSHOT_PATH.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as err:
        raise RuntimeError(
            "cannot load frozen LingTai profile contract "
            f"{LINGTAI_CONTRACT_SNAPSHOT_PATH}: {err}"
        ) from err
    if not isinstance(snapshot, dict):
        raise RuntimeError("frozen LingTai profile contract must be a JSON object")
    if snapshot.get("schema") != LINGTAI_CONTRACT_SNAPSHOT_SCHEMA:
        raise RuntimeError("frozen LingTai profile contract has the wrong schema marker")
    if snapshot.get("workbenchPrefixCount") != len(FROZEN_TOOL_ORDER):
        raise RuntimeError(
            "frozen LingTai profile contract has the wrong Workbench prefix count"
        )

    role_order = snapshot.get("roleToolOrder")
    if not isinstance(role_order, dict) or set(role_order) != LINGTAI_ROLES:
        raise RuntimeError("frozen LingTai profile contract has invalid roleToolOrder")
    parsed_order: dict[str, tuple[str, ...]] = {}
    for role in sorted(LINGTAI_ROLES):
        names = role_order.get(role)
        if (
            not isinstance(names, list)
            or not names
            or not all(isinstance(name, str) and name for name in names)
            or len(names) != len(set(names))
        ):
            raise RuntimeError(
                f"frozen LingTai profile contract has invalid {role} tool order"
            )
        parsed_order[role] = tuple(names)
    if not set(parsed_order["reader"]).issubset(parsed_order["writer"]):
        raise RuntimeError(
            "frozen LingTai reader tools are not a subset of writer tools"
        )

    definitions = snapshot.get("sharedToolDefinitions")
    if not isinstance(definitions, dict) or set(definitions) != set(
        parsed_order["writer"]
    ):
        raise RuntimeError(
            "frozen LingTai shared definitions differ from the writer tool order"
        )
    parsed_definitions: dict[str, dict[str, Any]] = {}
    for name, definition in definitions.items():
        if not isinstance(definition, dict) or set(definition) != {
            "description",
            "inputSchema",
        }:
            raise RuntimeError(f"frozen LingTai definition for {name} is invalid")
        description = definition.get("description")
        input_schema = definition.get("inputSchema")
        if not isinstance(description, str) or not description:
            raise RuntimeError(
                f"frozen LingTai definition for {name} has no description"
            )
        if not isinstance(input_schema, dict):
            raise RuntimeError(
                f"frozen LingTai definition for {name} has no inputSchema"
            )
        parsed_definitions[name] = {
            "description": description,
            "inputSchema": input_schema,
        }

    digests = snapshot.get("profileDigests")
    if not isinstance(digests, dict) or set(digests) != LINGTAI_ROLES:
        raise RuntimeError("frozen LingTai profile contract has invalid digests")
    parsed_digests: dict[str, str] = {}
    for role, digest in digests.items():
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise RuntimeError(
                f"frozen LingTai profile contract has invalid {role} digest"
            )
        parsed_digests[role] = digest
    return parsed_definitions, parsed_order, parsed_digests


(
    FROZEN_SHARED_TOOL_DEFINITIONS,
    FROZEN_LINGTAI_ROLE_TOOL_ORDER,
    FROZEN_LINGTAI_PROFILE_DIGESTS,
) = _load_frozen_lingtai_contract()


def extract_raw_tools(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        raise WorkbenchContractError("tools/list response must be a JSON object")
    if response.get("jsonrpc") != "2.0" or response.get("id") != 1:
        raise WorkbenchContractError(
            "tools/list response has the wrong JSON-RPC envelope"
        )
    result = response.get("result")
    if not isinstance(result, dict):
        raise WorkbenchContractError("tools/list response lacks a result object")
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise WorkbenchContractError("tools/list result lacks a tools array")
    if not all(isinstance(tool, dict) for tool in tools):
        raise WorkbenchContractError("tools/list contains a non-object tool")
    return tools


def _schema(tool: dict[str, Any], schema_key: str) -> dict[str, Any]:
    schema = tool.get(schema_key)
    if not isinstance(schema, dict):
        name = tool.get("name", "<unknown>")
        raise WorkbenchContractError(f"{name} lacks {schema_key}")
    return schema


def validate_tool_contract(
    tools: list[dict[str, Any]],
    *,
    schema_key: str = "inputSchema",
) -> None:
    names = [tool.get("name") for tool in tools]
    if any(not isinstance(name, str) or not name for name in names):
        raise WorkbenchContractError("tools/list contains a tool without a string name")
    if len(set(names)) != len(names):
        raise WorkbenchContractError("tools/list contains duplicate tool names")

    actual = set(names)
    if actual != WORKBENCH_TOOLS:
        raise WorkbenchContractError(
            "unexpected workbench tool surface; "
            f"missing={sorted(WORKBENCH_TOOLS - actual)}, "
            f"extra={sorted(actual - WORKBENCH_TOOLS)}"
        )

    by_name = {tool["name"]: tool for tool in tools}
    for name in sorted(WORKBENCH_TOOLS):
        actual_schema = normalize_schema(_schema(by_name[name], schema_key))
        expected_schema = FROZEN_INPUT_SCHEMAS[name]
        if actual_schema != expected_schema:
            raise WorkbenchContractError(
                f"{name} inputSchema differs from the frozen Rust contract; "
                f"expected_sha256={json_sha256(expected_schema)}, "
                f"actual_sha256={json_sha256(actual_schema)}"
            )


def validate_tool_order(tools: list[dict[str, Any]]) -> None:
    """Require the exact Rust-owned tools/list order from the frozen contract."""
    names = [tool.get("name") for tool in tools]
    if names != list(FROZEN_TOOL_ORDER):
        raise WorkbenchContractError(
            "workbench tools/list order differs from the frozen Rust contract; "
            f"expected={list(FROZEN_TOOL_ORDER)}, actual={names}"
        )


def contract_payload(
    tools: list[dict[str, Any]],
    *,
    schema_key: str = "inputSchema",
) -> list[dict[str, Any]]:
    """Return the semantic invocation schemas after enforcing the frozen order."""
    validate_tool_contract(tools, schema_key=schema_key)
    validate_tool_order(tools)
    return sorted(
        (
            {
                "name": tool["name"],
                "inputSchema": normalize_schema(_schema(tool, schema_key)),
            }
            for tool in tools
        ),
        key=lambda item: item["name"],
    )


def contract_evidence(
    tools: list[dict[str, Any]],
    *,
    schema_key: str = "inputSchema",
) -> dict[str, Any]:
    payload = contract_payload(tools, schema_key=schema_key)
    tool_order = [tool["name"] for tool in tools]
    restore = next(item for item in payload if item["name"] == RESTORE_TOOL)
    tools_schema_sha256 = json_sha256(payload)
    tool_order_sha256 = json_sha256(tool_order)
    evidence = {
        "required_capabilities": [REQUIRED_CAPABILITY],
        "tool_count": len(payload),
        "tool_names": [item["name"] for item in payload],
        "tool_order": tool_order,
        "tools_schema_sha256": tools_schema_sha256,
        "tool_order_sha256": tool_order_sha256,
        "restore_schema_sha256": json_sha256(restore["inputSchema"]),
    }
    # Transition approval is keyed by this digest, so it must cover every
    # separately recorded field rather than only the schema/order inputs.
    return {**evidence, "contract_sha256": json_sha256(evidence)}


def expected_contract_evidence() -> dict[str, Any]:
    """Return evidence for the checked-in Rust-owned 18-tool contract."""
    tools = [
        {"name": name, "inputSchema": FROZEN_INPUT_SCHEMAS[name]}
        for name in FROZEN_TOOL_ORDER
    ]
    return contract_evidence(tools)


def raw_tool_definitions_payload(
    tools: list[dict[str, Any]],
    *,
    schema_key: str = "inputSchema",
) -> list[dict[str, Any]]:
    """Return the exact ordered MCP definition payload owned by Rust.

    Unlike the legacy Workbench semantic evidence, this payload intentionally
    retains descriptions and every input-schema annotation. The reviewed
    LingTai profile digest is over these exact three fields in tools/list order.
    """
    payload: list[dict[str, Any]] = []
    for tool in tools:
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise WorkbenchContractError(
                "tools/list contains a tool without a string name"
            )
        description = tool.get("description")
        if not isinstance(description, str):
            raise WorkbenchContractError(f"{name} lacks a string description")
        payload.append(
            {
                "name": name,
                "description": description,
                "inputSchema": _schema(tool, schema_key),
            }
        )
    return payload


def raw_tool_definitions_sha256(
    tools: list[dict[str, Any]],
    *,
    schema_key: str = "inputSchema",
) -> str:
    return json_sha256(
        raw_tool_definitions_payload(tools, schema_key=schema_key)
    )


def _validate_profile_and_role(profile: str, role: str | None) -> None:
    if not isinstance(profile, str) or profile not in {
        WORKBENCH_PROFILE,
        LINGTAI_PROFILE,
    }:
        raise WorkbenchContractError(f"unsupported MCP profile: {profile!r}")
    if profile == WORKBENCH_PROFILE:
        if role is not None:
            raise WorkbenchContractError(
                "workbench profile does not accept a workspace role"
            )
        return
    if not isinstance(role, str) or role not in LINGTAI_ROLES:
        raise WorkbenchContractError(
            "lingtai profile requires workspace role reader or writer"
        )


def _lingtai_contract_payload(
    tools: list[dict[str, Any]],
    *,
    schema_key: str,
) -> list[dict[str, Any]]:
    return sorted(
        (
            {
                "name": tool["name"],
                "inputSchema": normalize_schema(_schema(tool, schema_key)),
            }
            for tool in tools
        ),
        key=lambda item: item["name"],
    )


def _lingtai_contract_evidence(
    tools: list[dict[str, Any]],
    *,
    role: str,
    raw_contract_sha256: str,
    schema_key: str,
) -> dict[str, Any]:
    payload = _lingtai_contract_payload(tools, schema_key=schema_key)
    tool_order = [tool["name"] for tool in tools]
    restore = next(item for item in payload if item["name"] == RESTORE_TOOL)
    evidence = {
        "required_capabilities": [REQUIRED_CAPABILITY],
        "profile": LINGTAI_PROFILE,
        "role": role,
        "tool_count": len(payload),
        "tool_names": [item["name"] for item in payload],
        "tool_order": tool_order,
        "tools_schema_sha256": json_sha256(payload),
        "tool_order_sha256": json_sha256(tool_order),
        "restore_schema_sha256": json_sha256(restore["inputSchema"]),
        "raw_contract_sha256": raw_contract_sha256,
    }
    return {**evidence, "contract_sha256": json_sha256(evidence)}


def _validate_lingtai_contract(
    tools: list[dict[str, Any]],
    *,
    role: str,
    schema_key: str,
) -> str:
    suffix_order = FROZEN_LINGTAI_ROLE_TOOL_ORDER[role]
    expected_order = (*FROZEN_TOOL_ORDER, *suffix_order)
    names = [tool.get("name") for tool in tools]
    if any(not isinstance(name, str) or not name for name in names):
        raise WorkbenchContractError(
            f"lingtai {role} tools/list contains a tool without a string name"
        )
    if len(set(names)) != len(names):
        raise WorkbenchContractError(
            f"lingtai {role} tools/list contains duplicate tool names"
        )
    actual = set(names)
    expected = set(expected_order)
    if len(names) != len(expected_order) or actual != expected:
        raise WorkbenchContractError(
            f"lingtai {role} tool surface differs; "
            f"missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )
    if names != list(expected_order):
        raise WorkbenchContractError(
            f"lingtai {role} tools/list order differs from the frozen contract; "
            f"expected={list(expected_order)}, actual={names}"
        )

    prefix_count = len(FROZEN_TOOL_ORDER)
    prefix = tools[:prefix_count]
    validate_tool_contract(prefix, schema_key=schema_key)
    validate_tool_order(prefix)

    for tool, name in zip(tools[prefix_count:], suffix_order, strict=True):
        expected_definition = FROZEN_SHARED_TOOL_DEFINITIONS[name]
        description = tool.get("description")
        if description != expected_definition["description"]:
            raise WorkbenchContractError(
                f"{name} description differs from the frozen Shared Workspace contract"
            )
        actual_schema = _schema(tool, schema_key)
        expected_schema = expected_definition["inputSchema"]
        if actual_schema != expected_schema:
            raise WorkbenchContractError(
                f"{name} inputSchema differs from the frozen Shared Workspace contract; "
                f"expected_sha256={json_sha256(expected_schema)}, "
                f"actual_sha256={json_sha256(actual_schema)}"
            )

    raw_digest = raw_tool_definitions_sha256(tools, schema_key=schema_key)
    expected_digest = FROZEN_LINGTAI_PROFILE_DIGESTS[role]
    if raw_digest != expected_digest:
        raise WorkbenchContractError(
            f"lingtai {role} raw tool definitions differ from the frozen contract; "
            f"expected_sha256={expected_digest}, actual_sha256={raw_digest}"
        )
    return raw_digest


def profile_contract_evidence(
    tools: list[dict[str, Any]],
    profile: str,
    *,
    role: str | None = None,
    schema_key: str = "inputSchema",
) -> dict[str, Any]:
    """Validate and describe one exact profile-specific MCP surface.

    The Workbench branch is deliberately a direct call to the legacy evidence
    function so existing lock bytes remain unchanged. LingTai uses a distinct,
    role-indexed contract and never widens the frozen 18-tool Workbench gate.
    """
    _validate_profile_and_role(profile, role)
    if profile == WORKBENCH_PROFILE:
        return contract_evidence(tools, schema_key=schema_key)
    assert role is not None
    raw_digest = _validate_lingtai_contract(
        tools,
        role=role,
        schema_key=schema_key,
    )
    return _lingtai_contract_evidence(
        tools,
        role=role,
        raw_contract_sha256=raw_digest,
        schema_key=schema_key,
    )


def expected_profile_contract_evidence(
    profile: str,
    *,
    role: str | None = None,
) -> dict[str, Any]:
    """Return checked-in evidence for a supported profile and workspace role."""
    _validate_profile_and_role(profile, role)
    if profile == WORKBENCH_PROFILE:
        return expected_contract_evidence()
    assert role is not None
    suffix_order = FROZEN_LINGTAI_ROLE_TOOL_ORDER[role]
    tools = [
        {"name": name, "inputSchema": FROZEN_INPUT_SCHEMAS[name]}
        for name in FROZEN_TOOL_ORDER
    ]
    tools.extend(
        {
            "name": name,
            "inputSchema": FROZEN_SHARED_TOOL_DEFINITIONS[name]["inputSchema"],
        }
        for name in suffix_order
    )
    return _lingtai_contract_evidence(
        tools,
        role=role,
        raw_contract_sha256=FROZEN_LINGTAI_PROFILE_DIGESTS[role],
        schema_key="inputSchema",
    )
