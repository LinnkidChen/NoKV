#!/usr/bin/env python3
# Copyright 2024-2026 The NoKV Authors.
# SPDX-License-Identifier: Apache-2.0

"""Verify and safely switch one LingTai agent to an immutable NoKV MCP."""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import install_workbench_mcp as installer
from nokv_runtime import (
    BUILD_INFO_SCHEMA_V1,
    BUILD_INFO_SCHEMA_V2,
    BuildInfo,
    SourceIdentity,
    discover_nokv_binary,
    infer_distribution,
    load_build_info,
    lock_source_identity_from_mapping,
    sha256_file,
    source_identity,
    stage_runtime,
    validate_revision,
    validate_sha256,
)
from workbench_contract import (
    WorkbenchContractError,
    expected_profile_contract_evidence,
    extract_raw_tools,
    json_sha256,
    profile_contract_evidence,
)
from workspace_grant import (
    encode_workspace_grant,
    parse_workspace_grant,
    workspace_grant_from_lock_fields,
    workspace_grant_lock_fields,
    workspace_grant_sha256,
)


LOCK_SCHEMA_V1 = "nokv.lingtai.workbench_lock.v1"
LOCK_SCHEMA_V2 = "nokv.lingtai.workbench_lock.v2"
LOCK_SCHEMA = LOCK_SCHEMA_V2
SUPPORTED_LOCK_SCHEMAS = frozenset({LOCK_SCHEMA_V1, LOCK_SCHEMA_V2})
LOCK_NAME = "nokv-workbench.lock.json"
SYNC_LOCK_NAME = ".nokv-workbench.sync.lock"
TRANSACTION_NAME = ".nokv-workbench.transaction.json"
TRANSACTION_SCHEMA = "nokv.lingtai.workbench_transaction.v1"
AGENT_IDENTITY_SCHEMA = "nokv.lingtai.orchestration_agent_identity.v1"


class SingletonValueAction(argparse.Action):
    """Reject repeated identity/profile options instead of accepting last-wins."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        marker = f"_{self.dest}_explicit"
        if getattr(namespace, marker, False):
            parser.error(f"{option_string or self.dest} may be specified only once")
        setattr(namespace, marker, True)
        setattr(namespace, self.dest, values)


@dataclass(frozen=True)
class AgentIdentityToken:
    parent_path: str
    parent_dev: int
    parent_ino: int
    agent_name: str
    agent_dev: int
    agent_ino: int


@dataclass
class AgentDirectoryHandle:
    """One canonical Agent name held open for descriptor-relative state I/O."""

    display_path: Path
    parent_fd: int
    agent_fd: int
    identity: AgentIdentityToken
    supplied_identity: AgentIdentityToken | None

    @property
    def state_path(self) -> Path:
        # open_agent_directory changes this one-process helper's cwd with
        # fchdir(agent_fd), so relative state operations remain anchored to the
        # held directory even if its original name is concurrently moved.
        return Path(".")

    def verify_current(self) -> None:
        expected = self.supplied_identity or self.identity
        parent_stat = os.fstat(self.parent_fd)
        agent_stat = os.fstat(self.agent_fd)
        if (parent_stat.st_dev, parent_stat.st_ino) != (
            expected.parent_dev,
            expected.parent_ino,
        ):
            raise ValueError("LingTai .lingtai directory identity changed")
        if (agent_stat.st_dev, agent_stat.st_ino) != (
            expected.agent_dev,
            expected.agent_ino,
        ):
            raise ValueError("LingTai Agent directory identity changed")
        try:
            current_parent = os.stat(expected.parent_path, follow_symlinks=False)
            current_agent = os.stat(
                expected.agent_name,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError as err:
            raise ValueError(
                f"pinned LingTai Agent no longer exists: {self.display_path}"
            ) from err
        if (current_parent.st_dev, current_parent.st_ino) != (
            expected.parent_dev,
            expected.parent_ino,
        ):
            raise ValueError("LingTai .lingtai path was replaced after selection")
        if (current_agent.st_dev, current_agent.st_ino) != (
            expected.agent_dev,
            expected.agent_ino,
        ):
            raise ValueError(
                f"pinned LingTai Agent was replaced after selection: {self.display_path}"
            )


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _identity_mapping(identity: AgentIdentityToken) -> dict[str, Any]:
    return {
        "agent_dev": identity.agent_dev,
        "agent_ino": identity.agent_ino,
        "agent_name": identity.agent_name,
        "parent_dev": identity.parent_dev,
        "parent_ino": identity.parent_ino,
        "parent_path": identity.parent_path,
        "schema": AGENT_IDENTITY_SCHEMA,
    }


def encode_agent_identity_token(identity: AgentIdentityToken) -> str:
    payload = json.dumps(
        _identity_mapping(identity),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def parse_agent_identity_token(token: str) -> AgentIdentityToken:
    if not isinstance(token, str) or not token or "=" in token:
        raise ValueError("orchestration Agent identity token is malformed")
    try:
        payload = base64.b64decode(
            token + "=" * (-len(token) % 4), altchars=b"-_", validate=True
        )
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ValueError("orchestration Agent identity token is malformed") from err
    expected_keys = {
        "schema",
        "parent_path",
        "parent_dev",
        "parent_ino",
        "agent_name",
        "agent_dev",
        "agent_ino",
    }
    if not isinstance(data, dict) or set(data) != expected_keys:
        raise ValueError("orchestration Agent identity token is malformed")
    if data.get("schema") != AGENT_IDENTITY_SCHEMA:
        raise ValueError("orchestration Agent identity token has an unknown schema")
    parent_path = data.get("parent_path")
    agent_name = data.get("agent_name")
    integers = (
        data.get("parent_dev"),
        data.get("parent_ino"),
        data.get("agent_dev"),
        data.get("agent_ino"),
    )
    if (
        not isinstance(parent_path, str)
        or not parent_path
        or not Path(parent_path).is_absolute()
        or str(Path(parent_path)) != parent_path
        or not isinstance(agent_name, str)
        or not agent_name
        or Path(agent_name).name != agent_name
        or agent_name in {".", ".."}
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in integers
        )
    ):
        raise ValueError("orchestration Agent identity token is malformed")
    identity = AgentIdentityToken(
        parent_path=parent_path,
        parent_dev=integers[0],
        parent_ino=integers[1],
        agent_name=agent_name,
        agent_dev=integers[2],
        agent_ino=integers[3],
    )
    if encode_agent_identity_token(identity) != token:
        raise ValueError("orchestration Agent identity token is not canonical")
    return identity


@contextlib.contextmanager
def open_agent_directory(
    project: Path,
    agent: str | None,
    agent_dir: str | None,
    token: str | None,
) -> Iterator[AgentDirectoryHandle]:
    display_path = (
        installer.resolve_agent_dir(project, agent, agent_dir).expanduser().resolve()
    )
    parent_path = display_path.parent
    supplied = parse_agent_identity_token(token) if token is not None else None
    if supplied is not None and (
        supplied.parent_path != str(parent_path)
        or supplied.agent_name != display_path.name
    ):
        raise ValueError(
            "orchestration Agent identity token does not match the resolved Agent"
        )
    parent_fd = os.open(parent_path, _directory_open_flags())
    cwd_fd = os.open(".", _directory_open_flags())
    try:
        agent_fd = os.open(
            display_path.name,
            _directory_open_flags(),
            dir_fd=parent_fd,
        )
        try:
            parent_stat = os.fstat(parent_fd)
            agent_stat = os.fstat(agent_fd)
            identity = AgentIdentityToken(
                parent_path=str(parent_path),
                parent_dev=parent_stat.st_dev,
                parent_ino=parent_stat.st_ino,
                agent_name=display_path.name,
                agent_dev=agent_stat.st_dev,
                agent_ino=agent_stat.st_ino,
            )
            handle = AgentDirectoryHandle(
                display_path=display_path,
                parent_fd=parent_fd,
                agent_fd=agent_fd,
                identity=identity,
                supplied_identity=supplied,
            )
            handle.verify_current()
            os.fchdir(agent_fd)
            try:
                yield handle
            finally:
                os.fchdir(cwd_fd)
        finally:
            os.close(agent_fd)
    finally:
        os.close(cwd_fd)
        os.close(parent_fd)


def capture_agent_identity(project: Path, requested_agent: str | None) -> tuple[str, str]:
    with open_agent_directory(project, requested_agent, None, None) as handle:
        return handle.display_path.name, encode_agent_identity_token(handle.identity)


def resolve_build_info(binary: Path, explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser().resolve()
    candidates = [
        binary.parent / "build-info.json",
        binary.parent.parent / "share" / "nokv" / "build-info.json",
    ]
    return next((path for path in candidates if path.is_file()), None)


def resolve_artifact_identity(
    binary: Path,
    *,
    build_info: str | None,
    revision: str | None,
) -> BuildInfo:
    info_path = resolve_build_info(binary, build_info)
    if info_path is None:
        raise ValueError(
            "binary identity is unavailable; use --build-source for a source build "
            "or pass --build-info from its Brew/Release artifact"
        )
    build_info_value = load_build_info(info_path)
    candidate_sha256 = sha256_file(binary)
    candidate_size = binary.stat().st_size
    if candidate_sha256 != build_info_value.binary_sha256:
        raise ValueError(
            "candidate NoKV binary does not match build-info SHA-256: "
            f"{candidate_sha256} != {build_info_value.binary_sha256}"
        )
    if candidate_size != build_info_value.binary_size_bytes:
        raise ValueError(
            "candidate NoKV binary does not match build-info size: "
            f"{candidate_size} != {build_info_value.binary_size_bytes}"
        )
    identity = build_info_value.identity
    if revision and identity.nokv_git_commit != validate_revision(revision):
        raise ValueError(
            f"build-info revision {identity.nokv_git_commit} does not match {revision}"
        )
    return build_info_value


def build_source_candidate(
    source_root: Path,
    *,
    revision: str | None,
    allow_dirty: bool,
) -> tuple[Path, SourceIdentity]:
    root = source_root.expanduser().resolve()
    before = source_identity(root, revision)
    if before.source_dirty and not allow_dirty:
        raise ValueError(
            "NoKV source identity is dirty; commit/stash it or pass --allow-dirty "
            "for local testing"
        )
    target_dir = root / "target" / "lingtai-workbench-source"
    candidate = target_dir / "release" / "nokv"
    try:
        candidate.unlink()
    except FileNotFoundError:
        pass
    completed = subprocess.run(
        [
            "cargo",
            "build",
            "--locked",
            "--release",
            "--target-dir",
            str(target_dir),
            "--manifest-path",
            str(root / "Cargo.toml"),
            "-p",
            "nokv",
            "--bin",
            "nokv",
        ],
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(f"locked NoKV source build failed with {completed.returncode}")
    after = source_identity(root, before.nokv_git_commit)
    if after != before:
        raise ValueError("NoKV source identity changed while the binary was building")
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise FileNotFoundError(
            f"source build did not produce an executable: {candidate}"
        )
    return candidate.resolve(), after


def concrete_workbench_root(template: str, agent_dir: Path) -> str:
    concrete = (
        template.replace("{agent_id}", agent_dir.name)
        .replace("{agent_address}", agent_dir.name)
        .replace("{agent_dir}", str(agent_dir))
    )
    if "{" in concrete or "}" in concrete:
        raise ValueError(f"workbench root contains an unknown placeholder: {template}")
    return concrete


def workspace_role(config: installer.InstallConfig) -> str | None:
    grant = installer.parsed_workspace_grant(config)
    return None if grant is None else grant.role


def validate_config_is_current(
    config: installer.InstallConfig,
    *,
    now_unix_ms: int | None = None,
) -> None:
    """Recheck time-bound launch identity at its durable use boundary."""

    installer.validate_install_config(config)
    if config.profile == "lingtai":
        # InstallConfig construction is intentionally not the only time gate.
        # A grant may expire after a live probe but before the journal write.
        parse_workspace_grant(
            config.workspace_grant,
            workspace_id=config.workspace_id,
            actor_id=config.workspace_actor_id,
            now_unix_ms=now_unix_ms,
        )


def contract_evidence_for_config(
    tools: list[dict[str, Any]],
    config: installer.InstallConfig,
) -> dict[str, Any]:
    try:
        return profile_contract_evidence(
            tools,
            config.profile,
            role=workspace_role(config),
        )
    except WorkbenchContractError as err:
        raise ValueError(str(err)) from err


def raw_tools_list(
    config: installer.InstallConfig,
    *,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    request = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        separators=(",", ":"),
    )
    try:
        completed = subprocess.run(
            [config.nokv_bin, *installer.mcp_args(config)],
            input=request + "\n",
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as err:
        raise ValueError(
            f"NoKV tools/list timed out after {timeout_seconds:g}s"
        ) from err
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(
            f"NoKV tools/list exited with {completed.returncode}: {detail}"
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError(
            f"NoKV tools/list must return exactly one JSON line, got {len(lines)}"
        )
    try:
        response = json.loads(lines[0])
    except json.JSONDecodeError as err:
        raise ValueError(f"NoKV tools/list returned invalid JSON: {err}") from err
    try:
        tools = extract_raw_tools(response)
        contract_evidence_for_config(tools, config)
    except WorkbenchContractError as err:
        raise ValueError(str(err)) from err
    return tools


def registration_source(identity: SourceIdentity, binary_sha256: str) -> str:
    dirty = "+dirty" if identity.source_dirty else ""
    return f"NoKV-Lab/NoKV@{identity.nokv_git_commit}{dirty}#sha256:{binary_sha256}"


def holt_identity_output(identity: SourceIdentity) -> tuple[tuple[str, str], ...]:
    if identity.schema == BUILD_INFO_SCHEMA_V1:
        if not isinstance(identity.holt_git_commit, str):
            raise ValueError("v1 source identity lacks holt_git_commit")
        return (("holt_revision", identity.holt_git_commit),)
    if identity.schema == BUILD_INFO_SCHEMA_V2:
        if not isinstance(identity.holt_registry, str) or not isinstance(
            identity.holt_checksum_sha256, str
        ):
            raise ValueError("v2 source identity lacks Holt registry identity")
        return (
            ("holt_registry", identity.holt_registry),
            ("holt_checksum_sha256", identity.holt_checksum_sha256),
        )
    raise ValueError(f"unsupported source identity schema: {identity.schema!r}")


def build_lock(
    config: installer.InstallConfig,
    *,
    concrete_root: str,
    distribution: str,
    identity: SourceIdentity,
    binary_sha256: str,
    binary_size: int,
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    launch_semantics = installer.mcp_launch_semantics(config)
    launch: dict[str, Any] = {
        "transport": "stdio",
        "mcp_name": config.mcp_name,
        "profile": config.profile,
        "server_bind": config.server_bind,
        "object_backend": config.object_backend,
        "s3_endpoint": config.s3_endpoint,
        "s3_bucket": config.s3_bucket,
        "workbench_root_template": config.workbench_root,
        "workbench_root": concrete_root,
        "args_sha256": json_sha256(launch_semantics["args"]),
        "template_arg_indices": launch_semantics["template_arg_indices"],
        "launch_semantics_sha256": json_sha256(launch_semantics),
    }
    if config.profile == "lingtai":
        grant = installer.parsed_workspace_grant(config)
        if grant is None:  # pragma: no cover - guarded by InstallConfig
            raise ValueError("lingtai profile requires a canonical workspace grant")
        launch.update(
            {
                "workspace_id": config.workspace_id,
                "workspace_actor_id": config.workspace_actor_id,
                "workspace_grant": {
                    **workspace_grant_lock_fields(grant),
                    "canonical_sha256": workspace_grant_sha256(grant),
                },
            }
        )
    return {
        "schema": LOCK_SCHEMA,
        "artifact": {
            "command": config.nokv_bin,
            "sha256": binary_sha256,
            "size_bytes": binary_size,
        },
        "source": {
            "distribution": distribution,
            **identity.as_dict(),
        },
        "launch": launch,
        "contract": contract_evidence_for_config(tools, config),
    }


def read_lock(path: Path) -> dict[str, Any]:
    try:
        text = installer.read_regular_text(path, missing_ok=False)
    except FileNotFoundError as err:
        raise FileNotFoundError(
            f"NoKV workbench lock does not exist: {path}"
        ) from err
    assert text is not None
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("schema") not in SUPPORTED_LOCK_SCHEMAS:
        supported = ", ".join(sorted(SUPPORTED_LOCK_SCHEMAS))
        raise ValueError(f"{path} is not a supported workbench lock ({supported})")
    for field in ("artifact", "source", "launch", "contract"):
        if not isinstance(data.get(field), dict):
            raise ValueError(f"{path}: {field} must be a JSON object")
    return data


def lock_profile(lock: dict[str, Any]) -> str:
    launch = lock["launch"]
    profile = launch.get("profile")
    if not isinstance(profile, str) or profile not in {"workbench", "lingtai"}:
        raise ValueError(
            "workbench lock profile must be the supported string "
            f"'workbench' or 'lingtai', got {profile!r}"
        )
    return profile


def config_from_lock(lock: dict[str, Any]) -> installer.InstallConfig:
    schema = lock.get("schema")
    if schema not in SUPPORTED_LOCK_SCHEMAS:
        raise ValueError(f"unsupported workbench lock schema: {schema!r}")
    artifact = lock["artifact"]
    launch = lock["launch"]
    source = lock["source"]
    profile = lock_profile(lock)
    command = artifact.get("command")
    required_strings = {
        "command": command,
        "server_bind": launch.get("server_bind"),
        "object_backend": launch.get("object_backend"),
        "s3_bucket": launch.get("s3_bucket"),
        "workbench_root_template": launch.get("workbench_root_template"),
        "mcp_name": launch.get("mcp_name"),
        "nokv_git_commit": source.get("nokv_git_commit"),
        "binary_sha256": artifact.get("sha256"),
        "args_sha256": launch.get("args_sha256"),
    }
    for field, value in required_strings.items():
        if not isinstance(value, str) or not value:
            raise ValueError(f"workbench lock {field} must be a non-empty string")
    validate_revision(source["nokv_git_commit"])
    validate_sha256(artifact["sha256"])
    validate_sha256(launch["args_sha256"])
    if schema == LOCK_SCHEMA_V1:
        unexpected_semantics = {
            field
            for field in ("template_arg_indices", "launch_semantics_sha256")
            if field in launch
        }
        if unexpected_semantics:
            raise ValueError(
                "v1 workbench lock uses legacy expand-all launch semantics and "
                f"must not contain v2 fields: {sorted(unexpected_semantics)}"
            )
    else:
        template_indices = launch.get("template_arg_indices")
        if not isinstance(template_indices, list) or any(
            isinstance(index, bool) or not isinstance(index, int)
            for index in template_indices
        ):
            raise ValueError(
                "v2 workbench lock template_arg_indices must be a JSON integer array"
            )
        semantics_sha256 = launch.get("launch_semantics_sha256")
        if not isinstance(semantics_sha256, str) or not semantics_sha256:
            raise ValueError(
                "v2 workbench lock launch_semantics_sha256 must be a non-empty string"
            )
        validate_sha256(semantics_sha256)
    endpoint = launch.get("s3_endpoint")
    if endpoint is not None and not isinstance(endpoint, str):
        raise ValueError("workbench lock s3_endpoint must be a string or null")
    identity = lock_source_identity_from_mapping(
        source, context="workbench lock source"
    )
    workspace_fields = {
        field
        for field in launch
        if field == "workspace" or field.startswith("workspace_")
    }
    workspace_config: dict[str, Any] = {}
    if profile == "workbench":
        if workspace_fields:
            raise ValueError(
                "workbench lock profile rejects workspace identity or grant fields"
            )
    else:
        expected_workspace_fields = {
            "workspace_id",
            "workspace_actor_id",
            "workspace_grant",
        }
        if workspace_fields != expected_workspace_fields:
            raise ValueError(
                "lingtai workbench lock must contain exactly workspace_id, "
                "workspace_actor_id and workspace_grant; "
                f"missing={sorted(expected_workspace_fields - workspace_fields)}, "
                f"extra={sorted(workspace_fields - expected_workspace_fields)}"
            )
        workspace_id = launch.get("workspace_id")
        actor_id = launch.get("workspace_actor_id")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("workbench lock workspace_id must be a non-empty string")
        if not isinstance(actor_id, str) or not actor_id:
            raise ValueError(
                "workbench lock workspace_actor_id must be a non-empty string"
            )
        locked_grant = launch.get("workspace_grant")
        if not isinstance(locked_grant, dict):
            raise ValueError("workbench lock workspace_grant must be a JSON object")
        expected_keys = {
            "schema",
            "grant_id",
            "issuer",
            "audience",
            "workspace_id",
            "actor_id",
            "role",
            "issued_at_unix_ms",
            "expires_at_unix_ms",
            "canonical_sha256",
        }
        if set(locked_grant) != expected_keys:
            missing = sorted(expected_keys - set(locked_grant))
            extra = sorted(set(locked_grant) - expected_keys)
            raise ValueError(
                "workbench lock workspace_grant must contain exactly the canonical "
                f"fields; missing={missing}, extra={extra}"
            )
        canonical_sha256 = locked_grant["canonical_sha256"]
        if not isinstance(canonical_sha256, str):
            raise ValueError(
                "workbench lock workspace grant canonical_sha256 must be a string"
            )
        validate_sha256(canonical_sha256)
        grant = workspace_grant_from_lock_fields(
            {
                field: value
                for field, value in locked_grant.items()
                if field != "canonical_sha256"
            },
            workspace_id=workspace_id,
            actor_id=actor_id,
        )
        actual_sha256 = workspace_grant_sha256(grant)
        if actual_sha256 != canonical_sha256:
            raise ValueError(
                "workbench lock workspace grant canonical SHA-256 differs from "
                f"its fields: {canonical_sha256} != {actual_sha256}"
            )
        workspace_config = {
            "workspace_id": workspace_id,
            "workspace_actor_id": actor_id,
            "workspace_grant": encode_workspace_grant(grant),
        }
    config = installer.InstallConfig(
        nokv_bin=command,
        server_bind=launch["server_bind"],
        object_backend=launch["object_backend"],
        s3_endpoint=endpoint,
        s3_bucket=launch["s3_bucket"],
        workbench_root=launch["workbench_root_template"],
        mcp_name=launch["mcp_name"],
        source=registration_source(identity, artifact["sha256"]),
        profile=profile,
        **workspace_config,
    )
    if json_sha256(installer.mcp_args(config)) != launch["args_sha256"]:
        raise ValueError("workbench lock launch arguments do not match args_sha256")
    if schema == LOCK_SCHEMA_V2:
        semantics = installer.mcp_launch_semantics(config)
        if semantics["template_arg_indices"] != launch["template_arg_indices"]:
            raise ValueError(
                "workbench lock template_arg_indices do not match launch arguments"
            )
        if json_sha256(semantics) != launch["launch_semantics_sha256"]:
            raise ValueError(
                "workbench lock launch semantics do not match "
                "launch_semantics_sha256"
            )
    return config


def verify_agent_configuration(
    agent_dir: Path,
    config: installer.InstallConfig,
    *,
    legacy_expand_all: bool = False,
) -> None:
    expected_registry = installer.registry_record(config)
    expected_init = installer.init_spec(config)
    if legacy_expand_all:
        expected_registry.pop("template_arg_indices")
        expected_init.pop("template_arg_indices")
    records = installer.read_registry(agent_dir / "mcp_registry.jsonl")
    matches = [record for record in records if record.get("name") == config.mcp_name]
    if matches != [expected_registry]:
        raise ValueError("LingTai MCP registry does not match the NoKV workbench lock")
    init = installer.read_init(agent_dir / "init.json")
    mcp = init.get("mcp")
    if not isinstance(mcp, dict) or mcp.get(config.mcp_name) != expected_init:
        raise ValueError("LingTai init.json does not match the NoKV workbench lock")


@contextlib.contextmanager
def agent_sync_lock(agent_dir: Path, *, exclusive: bool) -> Iterator[None]:
    lock_path = agent_dir / SYNC_LOCK_NAME
    if exclusive:
        flags = os.O_CREAT | os.O_RDWR
    else:
        if not lock_path.is_file() or lock_path.is_symlink():
            raise FileNotFoundError(
                f"NoKV workbench sync lock does not exist: {lock_path}"
            )
        flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        try:
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
        except BlockingIOError as err:
            raise RuntimeError(
                f"another NoKV workbench sync is active for {agent_dir}"
            ) from err
        yield
    finally:
        os.close(descriptor)


def _restore_text(path: Path, text: str | None) -> None:
    if text is None:
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        else:
            path.unlink()
            installer.fsync_directory(path.parent)
        return
    installer.write_text_if_changed(path, text)


def _transaction_files(agent_dir: Path) -> dict[str, Path]:
    return {
        "mcp_registry.jsonl": agent_dir / "mcp_registry.jsonl",
        "init.json": agent_dir / "init.json",
        LOCK_NAME: agent_dir / LOCK_NAME,
    }


def agent_state_sha256(values: dict[str, str | None]) -> str:
    expected_names = set(_transaction_files(Path(".")))
    if set(values) != expected_names:
        raise ValueError("Agent state digest requires the exact managed file set")
    if any(value is not None and not isinstance(value, str) for value in values.values()):
        raise ValueError("Agent state digest values must be text or absent")
    # JSON preserves exact text bytes after the existing strict UTF-8 reads and
    # distinguishes a missing file (null) from every present string, including
    # an empty file. Names and separators are canonicalized by json_sha256.
    return json_sha256(values)


def read_agent_state(agent_dir: Path) -> dict[str, str | None]:
    return {
        name: installer.read_regular_text(path, missing_ok=True)
        for name, path in _transaction_files(agent_dir).items()
    }


def read_transaction(agent_dir: Path) -> dict[str, Any] | None:
    path = agent_dir / TRANSACTION_NAME
    text = installer.read_regular_text(path, missing_ok=True)
    if text is None:
        return None
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("schema") != TRANSACTION_SCHEMA:
        raise ValueError(f"invalid interrupted workbench transaction: {path}")
    expected_names = set(_transaction_files(agent_dir))
    for field in ("original", "desired"):
        values = data.get(field)
        if not isinstance(values, dict) or set(values) != expected_names:
            raise ValueError(
                f"invalid interrupted workbench transaction {field}: {path}"
            )
        if any(
            value is not None and not isinstance(value, str)
            for value in values.values()
        ):
            raise ValueError(
                f"invalid interrupted workbench transaction {field} values: {path}"
            )
    return data


def recover_interrupted_update(agent_dir: Path) -> bool:
    transaction = read_transaction(agent_dir)
    if transaction is None:
        return False
    paths = _transaction_files(agent_dir)
    desired_matches = all(
        installer.read_regular_text(path, missing_ok=True)
        == transaction["desired"][name]
        for name, path in paths.items()
    )
    if not desired_matches:
        for name, path in paths.items():
            _restore_text(path, transaction["original"][name])
    (agent_dir / TRANSACTION_NAME).unlink()
    installer.fsync_directory(agent_dir)
    return True


def validate_contract_transition(
    lock_path: Path,
    *,
    new_profile: str,
    profile_explicit: bool,
    new_contract: dict[str, Any],
    accepted_digest: str | None,
) -> None:
    if not lock_path.exists():
        return
    existing = read_lock(lock_path)
    old_profile = lock_profile(existing)
    new_contract_digest = new_contract.get("contract_sha256")
    if not isinstance(new_contract_digest, str):
        raise ValueError("new workbench contract lacks contract_sha256")
    old_contract = existing["contract"]
    old_contract_digest = old_contract.get("contract_sha256")
    # An explicit supported profile switch selects a separate checked-in exact
    # contract. The acceptance digest remains the same-profile drift guard.
    if old_profile != new_profile:
        if not profile_explicit:
            raise ValueError(
                f"existing lock profile is {old_profile}; refusing implicit "
                f"default {new_profile} transition; rerun with --profile "
                f"{new_profile} to select that profile explicitly"
            )
        return
    if old_contract == new_contract:
        return
    if accepted_digest != new_contract_digest:
        raise ValueError(
            f"{new_profile} contract changed (input schemas and/or tools/list order); "
            "review the canonical contract and rerun with "
            f"--accept-contract-sha256 {new_contract_digest} "
            f"(profile={new_profile}, old={old_contract_digest}, "
            f"new={new_contract_digest})"
        )


def offline_agent_preflight(
    agent_dir: Path,
    *,
    config: installer.InstallConfig,
    profile_explicit: bool,
    accepted_digest: str | None,
) -> None:
    if not agent_dir.is_dir():
        raise FileNotFoundError(f"LingTai agent directory does not exist: {agent_dir}")
    installer.read_registry(agent_dir / "mcp_registry.jsonl")
    installer.read_init(agent_dir / "init.json")
    expected_contract = expected_profile_contract_evidence(
        config.profile,
        role=workspace_role(config),
    )
    validate_contract_transition(
        agent_dir / LOCK_NAME,
        new_profile=config.profile,
        profile_explicit=profile_explicit,
        new_contract=expected_contract,
        accepted_digest=accepted_digest,
    )


def apply_agent_update(
    agent_dir: Path,
    config: installer.InstallConfig,
    lock_path: Path,
    lock_text: str,
    *,
    now_unix_ms: int | None = None,
    expected_original_state_sha256: str | None = None,
) -> tuple[installer.InstallResult, bool]:
    paths = _transaction_files(agent_dir)
    originals = read_agent_state(agent_dir)
    if (
        expected_original_state_sha256 is not None
        and agent_state_sha256(originals) != expected_original_state_sha256
    ):
        raise RuntimeError(
            "LingTai Agent configuration changed after rollout preflight; "
            "refusing to overwrite newer state; rerun up.sh"
        )
    desired = {
        "mcp_registry.jsonl": installer.render_registry(agent_dir, config),
        "init.json": installer.render_init(agent_dir, config),
        LOCK_NAME: lock_text,
    }
    transaction_path = agent_dir / TRANSACTION_NAME
    transaction_text = (
        json.dumps(
            {
                "schema": TRANSACTION_SCHEMA,
                "original": originals,
                "desired": desired,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    # This is the last tuple/time check before the journal and first mutation.
    # It catches a grant that expired after probing or desired-byte rendering.
    validate_config_is_current(config, now_unix_ms=now_unix_ms)
    installer.write_text_if_changed(transaction_path, transaction_text)
    try:
        registry_changed = installer.write_text_if_changed(
            paths["mcp_registry.jsonl"], desired["mcp_registry.jsonl"]
        )
        init_changed = installer.write_text_if_changed(
            paths["init.json"], desired["init.json"]
        )
        lock_changed = installer.write_text_if_changed(lock_path, desired[LOCK_NAME])
        transaction_path.unlink()
        installer.fsync_directory(agent_dir)
    except Exception as update_error:
        rollback_errors = []
        for name, path in paths.items():
            try:
                _restore_text(path, originals[name])
            except Exception as rollback_error:  # pragma: no cover - disk failure
                rollback_errors.append(f"{path}: {rollback_error}")
        if rollback_errors:
            raise RuntimeError(
                f"agent update failed ({update_error}); rollback also failed: "
                + "; ".join(rollback_errors)
                + f"; recovery journal retained at {transaction_path}"
            ) from update_error
        try:
            transaction_path.unlink()
            installer.fsync_directory(agent_dir)
        except Exception as rollback_error:  # pragma: no cover - disk failure
            raise RuntimeError(
                f"agent update failed ({update_error}); rollback completed but "
                f"the recovery journal could not be removed: "
                f"{transaction_path}: {rollback_error}"
            ) from update_error
        raise
    result = installer.InstallResult(
        agent_dir=agent_dir,
        registry_changed=registry_changed,
        init_changed=init_changed,
    )
    return result, lock_changed


def check_lock(
    agent_dir: Path,
    *,
    agent_display_path: Path,
    candidate_binary: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    lock_path = agent_dir / LOCK_NAME
    lock = read_lock(lock_path)
    config = config_from_lock(lock)
    command = Path(config.nokv_bin).expanduser().resolve()
    if not command.is_file():
        raise FileNotFoundError(f"locked NoKV binary does not exist: {command}")
    digest = sha256_file(command)
    if digest != lock["artifact"]["sha256"]:
        raise ValueError(
            "locked NoKV binary was replaced in place: "
            f"expected {lock['artifact']['sha256']}, got {digest}"
        )
    if command.stat().st_size != lock["artifact"].get("size_bytes"):
        raise ValueError("locked NoKV binary size does not match the lock")
    source = lock["source"]
    if (
        source.get("nokv_git_commit") not in command.parts
        or digest not in command.parts
    ):
        raise ValueError("locked NoKV command is not in its content-addressed path")
    staged_build_info = load_build_info(command.parent / "build-info.json")
    locked_identity = lock_source_identity_from_mapping(
        source, context="workbench lock source"
    )
    if (
        staged_build_info.identity != locked_identity
        or staged_build_info.binary_sha256 != digest
        or staged_build_info.binary_size_bytes != command.stat().st_size
    ):
        raise ValueError("staged build-info differs from the workbench lock")
    if candidate_binary:
        candidate = discover_nokv_binary(candidate_binary)
        candidate_digest = sha256_file(candidate)
        if candidate_digest != digest:
            raise ValueError(
                "candidate NoKV binary differs from the installed lock; run sync "
                "without --check after reviewing the update"
            )
    verify_agent_configuration(
        agent_dir,
        config,
        legacy_expand_all=lock["schema"] == LOCK_SCHEMA_V1,
    )
    if json_sha256(installer.mcp_args(config)) != lock["launch"].get("args_sha256"):
        raise ValueError("locked MCP launch arguments have drifted")
    if lock["schema"] == LOCK_SCHEMA_V2:
        semantics = installer.mcp_launch_semantics(config)
        if semantics["template_arg_indices"] != lock["launch"].get(
            "template_arg_indices"
        ):
            raise ValueError("locked MCP template argument indices have drifted")
        if json_sha256(semantics) != lock["launch"].get(
            "launch_semantics_sha256"
        ):
            raise ValueError("locked MCP launch semantics have drifted")

    locked_concrete_root = lock["launch"].get("workbench_root")
    if not isinstance(locked_concrete_root, str) or not locked_concrete_root:
        raise ValueError("workbench lock lacks a concrete preflight root")
    concrete_root = concrete_workbench_root(
        config.workbench_root,
        agent_display_path,
    )
    if concrete_root != locked_concrete_root:
        raise ValueError(
            "locked concrete workbench root differs from the root expanded for "
            f"the selected Agent: {locked_concrete_root!r} != {concrete_root!r}"
        )
    probe_config = installer.InstallConfig(
        **{
            **config.__dict__,
            "nokv_bin": str(command),
            "workbench_root": concrete_root,
        }
    )
    tools = raw_tools_list(probe_config, timeout_seconds=timeout_seconds)
    evidence = contract_evidence_for_config(tools, probe_config)
    if evidence != lock["contract"]:
        raise ValueError("live NoKV MCP contract differs from the installed lock")
    return lock


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Stage an immutable NoKV binary, gate its live workbench MCP contract, "
            "and switch one LingTai agent."
        )
    )
    parser.add_argument("--project", default=".", help="LingTai project directory.")
    parser.add_argument("--agent", help="Agent directory name under PROJECT/.lingtai.")
    parser.add_argument("--agent-dir", help="Explicit LingTai agent directory.")
    parser.add_argument(
        "--orchestration-agent-identity",
        action=SingletonValueAction,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--orchestration-agent-state-sha256",
        action=SingletonValueAction,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--nokv-bin", help="Candidate binary; defaults to NOKV_BIN, PATH, then Brew."
    )
    parser.add_argument(
        "--build-source",
        help=(
            "Build this NoKV checkout with cargo --locked --release and stage the "
            "result. Mutually exclusive with --nokv-bin/--build-info."
        ),
    )
    parser.add_argument(
        "--build-info", help="Build identity shipped with a Brew/Release candidate."
    )
    parser.add_argument("--revision", help="Expected full NoKV git commit.")
    parser.add_argument("--expected-sha256", help="Expected candidate binary SHA-256.")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Accept an explicitly dirty source identity for local testing only.",
    )
    parser.add_argument(
        "--distribution",
        choices=("source", "brew", "release", "path"),
        help="Artifact source recorded in the lock.",
    )
    parser.add_argument("--server-bind", default=installer.DEFAULT_SERVER_BIND)
    parser.add_argument("--object-backend", default="rustfs")
    parser.add_argument("--s3-endpoint", default=installer.DEFAULT_ENDPOINT)
    parser.add_argument("--s3-bucket", default=installer.DEFAULT_BUCKET)
    parser.add_argument("--workbench-root", default=installer.DEFAULT_WORKBENCH_ROOT)
    parser.add_argument("--mcp-name", default=installer.DEFAULT_MCP_NAME)
    parser.add_argument(
        "--profile",
        action=SingletonValueAction,
        choices=("workbench", "lingtai"),
        help="Exact MCP profile; defaults to the stable workbench profile.",
    )
    parser.add_argument(
        "--workspace-id",
        action=SingletonValueAction,
        help="Explicit Shared Workspace identity.",
    )
    parser.add_argument(
        "--workspace-actor-id",
        action=SingletonValueAction,
        help="Explicit Shared Workspace actor identity.",
    )
    parser.add_argument(
        "--workspace-grant",
        action=SingletonValueAction,
        help="Canonical LingTai launcher grant for the exact workspace tuple.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument(
        "--accept-contract-sha256",
        help=(
            "Accept exactly this reviewed canonical Workbench contract SHA-256 "
            "(input schemas and tools/list order)."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--stage-only",
        action="store_true",
        help="Only content-address and print the immutable binary path.",
    )
    mode.add_argument(
        "--probe-only",
        action="store_true",
        help=(
            "Stage the candidate and validate its live Workbench contract without "
            "changing Agent registration files."
        ),
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Verify the existing lock, files, binary, and live contract without writing.",
    )
    mode.add_argument(
        "--preflight-only",
        action="store_true",
        help=(
            "Validate Agent files and a contract transition without staging or "
            "probing; recover an interrupted local sync transaction when present."
        ),
    )
    args = parser.parse_args(argv)
    args.profile_explicit = getattr(args, "_profile_explicit", False)
    args.workspace_id_explicit = getattr(args, "_workspace_id_explicit", False)
    args.workspace_actor_id_explicit = getattr(
        args, "_workspace_actor_id_explicit", False
    )
    args.workspace_grant_explicit = getattr(
        args, "_workspace_grant_explicit", False
    )
    if args.orchestration_agent_identity is not None:
        try:
            parse_agent_identity_token(args.orchestration_agent_identity)
        except ValueError as err:
            parser.error(str(err))
    if args.orchestration_agent_state_sha256 is not None:
        try:
            args.orchestration_agent_state_sha256 = validate_sha256(
                args.orchestration_agent_state_sha256
            )
        except ValueError as err:
            parser.error(str(err))
    if args.profile is None:
        args.profile = "workbench"
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.accept_contract_sha256:
        try:
            args.accept_contract_sha256 = validate_sha256(args.accept_contract_sha256)
        except ValueError as err:
            parser.error(str(err))
    if args.build_source and (args.nokv_bin or args.build_info):
        parser.error(
            "--build-source is mutually exclusive with --nokv-bin/--build-info"
        )
    if args.build_source and args.distribution not in (None, "source"):
        parser.error("--build-source requires --distribution source when specified")
    if args.preflight_only and any(
        (
            args.build_source,
            args.nokv_bin,
            args.build_info,
            args.revision,
            args.expected_sha256,
            args.distribution,
            args.allow_dirty,
        )
    ):
        parser.error("--preflight-only does not accept artifact build/staging options")
    if args.check and args.build_source:
        parser.error("--check validates the installed lock and cannot build source")
    if args.check and any(
        (
            args.build_info,
            args.revision,
            args.expected_sha256,
            args.distribution,
            args.allow_dirty,
            args.accept_contract_sha256,
            args.profile_explicit,
            args.workspace_id_explicit,
            args.workspace_actor_id_explicit,
            args.workspace_grant_explicit,
            args.orchestration_agent_state_sha256,
        )
    ):
        parser.error(
            "--check accepts only project/Agent selection, --nokv-bin, and timeout; "
            "profile and workspace identity are reconstructed from the lock"
        )
    if args.orchestration_agent_state_sha256 is not None and any(
        (args.stage_only, args.probe_only, args.preflight_only)
    ):
        parser.error(
            "--orchestration-agent-state-sha256 is accepted only by normal sync"
        )
    return args


def install_config_from_args(
    args: argparse.Namespace,
    *,
    nokv_bin: str,
    workbench_root: str,
    source: str,
) -> installer.InstallConfig:
    return installer.InstallConfig(
        nokv_bin=nokv_bin,
        server_bind=args.server_bind,
        object_backend=args.object_backend,
        s3_endpoint=args.s3_endpoint or None,
        s3_bucket=args.s3_bucket,
        workbench_root=workbench_root,
        mcp_name=args.mcp_name,
        source=source,
        profile=args.profile,
        workspace_id=args.workspace_id,
        workspace_actor_id=args.workspace_actor_id,
        workspace_grant=args.workspace_grant,
    )


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    error_state_path: Path | None = None
    error_display_path: Path | None = None
    try:
        project = Path(args.project).expanduser().resolve()
        # open_agent_directory() intentionally changes cwd to the held Agent
        # descriptor. Preserve the public CLI contract by resolving an explicit
        # relative check candidate from the caller's invocation cwd first.
        check_candidate_binary = args.nokv_bin
        if args.check and check_candidate_binary is not None:
            check_candidate_binary = str(
                Path(check_candidate_binary).expanduser().resolve()
            )
        if args.preflight_only:
            with open_agent_directory(
                project,
                args.agent,
                args.agent_dir,
                args.orchestration_agent_identity,
            ) as agent_handle:
                agent_dir = agent_handle.state_path
                error_state_path = agent_dir
                error_display_path = agent_handle.display_path
                with agent_sync_lock(agent_dir, exclusive=True):
                    agent_handle.verify_current()
                    # Recovery must finish using only the recorded old bytes before
                    # any newly requested profile or grant tuple is evaluated.
                    recovered = recover_interrupted_update(agent_dir)
                    config = install_config_from_args(
                        args,
                        nokv_bin=installer.default_nokv_bin(),
                        workbench_root=args.workbench_root,
                        source="preflight-only",
                    )
                    offline_agent_preflight(
                        agent_dir,
                        config=config,
                        profile_explicit=args.profile_explicit,
                        accepted_digest=args.accept_contract_sha256,
                    )
                    expected_contract = expected_profile_contract_evidence(
                        config.profile,
                        role=workspace_role(config),
                    )
                    preflight_state_sha256 = agent_state_sha256(
                        read_agent_state(agent_dir)
                    )
                    agent_handle.verify_current()
                display_agent_dir = agent_handle.display_path
            print(f"agent_dir: {display_agent_dir}")
            print("agent_files_valid: true")
            print(f"interrupted_update_recovered: {str(recovered).lower()}")
            print(f"profile: {config.profile}")
            print(
                "expected_contract_sha256: "
                f"{expected_contract['contract_sha256']}"
            )
            print(f"agent_state_sha256: {preflight_state_sha256}")
            return 0

        if args.check:
            with open_agent_directory(
                project,
                args.agent,
                args.agent_dir,
                args.orchestration_agent_identity,
            ) as agent_handle:
                agent_dir = agent_handle.state_path
                error_state_path = agent_dir
                error_display_path = agent_handle.display_path
                with agent_sync_lock(agent_dir, exclusive=False):
                    agent_handle.verify_current()
                    if read_transaction(agent_dir) is not None:
                        raise RuntimeError(
                            "an interrupted NoKV workbench update is pending; rerun the "
                            "normal sync to recover it before using --check"
                        )
                    # Identity is rechecked at the final read-only use boundary.
                    agent_handle.verify_current()
                    checked_lock = check_lock(
                        agent_dir,
                        agent_display_path=agent_handle.display_path,
                        candidate_binary=check_candidate_binary,
                        timeout_seconds=args.timeout_seconds,
                    )
                    # The live tools/list probe can take long enough for an
                    # Agent lifecycle replacement. Never report a detached
                    # directory as the currently pinned Agent.
                    agent_handle.verify_current()
                display_agent_dir = agent_handle.display_path
            print(f"agent_dir: {display_agent_dir}")
            print(f"profile: {lock_profile(checked_lock)}")
            print("lock_valid: true")
            print("live_contract_valid: true")
            return 0

        if args.stage_only or args.probe_only:
            # Read-only staging/probing has no Agent journal to recover, so
            # reject a malformed selection before building or staging bytes.
            install_config_from_args(
                args,
                nokv_bin=installer.default_nokv_bin(),
                workbench_root=args.workbench_root,
                source="stage-only",
            )

        if args.build_source:
            candidate, identity = build_source_candidate(
                Path(args.build_source),
                revision=args.revision,
                allow_dirty=args.allow_dirty,
            )
            distribution = args.distribution or "source"
            artifact_sha256 = None
        else:
            candidate = discover_nokv_binary(args.nokv_bin)
            build_info = resolve_artifact_identity(
                candidate,
                build_info=args.build_info,
                revision=args.revision,
            )
            identity = build_info.identity
            artifact_sha256 = build_info.binary_sha256
            distribution = args.distribution or infer_distribution(candidate)
        if identity.source_dirty and not args.allow_dirty:
            raise ValueError(
                "NoKV source identity is dirty; commit/stash it or pass --allow-dirty "
                "for local testing"
            )
        if (
            artifact_sha256 is not None
            and args.expected_sha256 is not None
            and artifact_sha256 != validate_sha256(args.expected_sha256)
        ):
            raise ValueError(
                "artifact build-info SHA-256 differs from the independently "
                f"expected SHA-256: {artifact_sha256} != {args.expected_sha256}"
            )
        runtime = stage_runtime(
            project,
            candidate,
            identity,
            expected_sha256=artifact_sha256 or args.expected_sha256,
        )
        if args.stage_only:
            print(runtime.command)
            return 0

        if args.probe_only:
            with open_agent_directory(
                project,
                args.agent,
                args.agent_dir,
                args.orchestration_agent_identity,
            ) as agent_handle:
                agent_dir = agent_handle.state_path
                error_state_path = agent_dir
                error_display_path = agent_handle.display_path
                root = concrete_workbench_root(
                    args.workbench_root, agent_handle.display_path
                )
                config = install_config_from_args(
                    args,
                    nokv_bin=str(runtime.command),
                    workbench_root=root,
                    source=registration_source(identity, runtime.sha256),
                )
                tools = raw_tools_list(config, timeout_seconds=args.timeout_seconds)
                evidence = contract_evidence_for_config(tools, config)
                agent_handle.verify_current()
                validate_contract_transition(
                    agent_dir / LOCK_NAME,
                    new_profile=config.profile,
                    profile_explicit=args.profile_explicit,
                    new_contract=evidence,
                    accepted_digest=args.accept_contract_sha256,
                )
                agent_handle.verify_current()
                display_agent_dir = agent_handle.display_path
            print(f"agent_dir: {display_agent_dir}")
            print(f"binary_sha256: {runtime.sha256}")
            print(f"nokv_revision: {identity.nokv_git_commit}")
            print(f"profile: {config.profile}")
            print(f"tools_schema_sha256: {evidence['tools_schema_sha256']}")
            print(f"tool_order_sha256: {evidence['tool_order_sha256']}")
            print(f"contract_sha256: {evidence['contract_sha256']}")
            print("live_contract_valid: true")
            return 0

        with open_agent_directory(
            project,
            args.agent,
            args.agent_dir,
            args.orchestration_agent_identity,
        ) as agent_handle:
            agent_dir = agent_handle.state_path
            error_state_path = agent_dir
            error_display_path = agent_handle.display_path
            with agent_sync_lock(agent_dir, exclusive=True):
                agent_handle.verify_current()
                recovered = recover_interrupted_update(agent_dir)
                root = concrete_workbench_root(
                    args.workbench_root, agent_handle.display_path
                )
                source = registration_source(identity, runtime.sha256)
                config = install_config_from_args(
                    args,
                    nokv_bin=str(runtime.command),
                    workbench_root=args.workbench_root,
                    source=source,
                )
                probe_config = installer.InstallConfig(
                    **{**config.__dict__, "workbench_root": root}
                )
                tools = raw_tools_list(
                    probe_config, timeout_seconds=args.timeout_seconds
                )
                desired_lock = build_lock(
                    config,
                    concrete_root=root,
                    distribution=distribution,
                    identity=identity,
                    binary_sha256=runtime.sha256,
                    binary_size=runtime.size_bytes,
                    tools=tools,
                )
                lock_path = agent_dir / LOCK_NAME
                validate_contract_transition(
                    lock_path,
                    new_profile=config.profile,
                    profile_explicit=args.profile_explicit,
                    new_contract=desired_lock["contract"],
                    accepted_digest=args.accept_contract_sha256,
                )

                # Parse both files before the transaction marker and first mutation.
                installer.read_registry(agent_dir / "mcp_registry.jsonl")
                installer.read_init(agent_dir / "init.json")
                lock_text = (
                    json.dumps(
                        desired_lock,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                )
                # The held descriptor closes the final check-to-mutation race:
                # replacement of the original name cannot retarget these writes.
                agent_handle.verify_current()
                result, lock_changed = apply_agent_update(
                    agent_dir,
                    config,
                    lock_path,
                    lock_text,
                    expected_original_state_sha256=(
                        args.orchestration_agent_state_sha256
                    ),
                )
                try:
                    agent_handle.verify_current()
                except ValueError as err:
                    raise RuntimeError(
                        "configuration committed against the pinned Agent directory, "
                        "but its project path changed before verification; not rolled "
                        "back"
                    ) from err
            result = installer.InstallResult(
                agent_dir=agent_handle.display_path,
                registry_changed=result.registry_changed,
                init_changed=result.init_changed,
            )
    except Exception as err:
        detail = str(err)
        if (
            error_state_path is not None
            and error_state_path != Path(".")
            and error_display_path is not None
        ):
            detail = detail.replace(str(error_state_path), str(error_display_path))
        print(f"error: {detail}", file=sys.stderr)
        return 1

    print(f"agent_dir: {result.agent_dir}")
    print(f"binary_sha256: {runtime.sha256}")
    print(f"nokv_revision: {identity.nokv_git_commit}")
    for label, value in holt_identity_output(identity):
        print(f"{label}: {value}")
    print(f"profile: {config.profile}")
    print(f"tools_schema_sha256: {desired_lock['contract']['tools_schema_sha256']}")
    print(f"tool_order_sha256: {desired_lock['contract']['tool_order_sha256']}")
    print(f"contract_sha256: {desired_lock['contract']['contract_sha256']}")
    print(f"registry_changed: {str(result.registry_changed).lower()}")
    print(f"init_changed: {str(result.init_changed).lower()}")
    print(f"lock_changed: {str(lock_changed).lower()}")
    print(f"interrupted_update_recovered: {str(recovered).lower()}")
    if result.registry_changed or result.init_changed or lock_changed:
        print("next: run /refresh in the target LingTai agent")
    else:
        print("already synchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
