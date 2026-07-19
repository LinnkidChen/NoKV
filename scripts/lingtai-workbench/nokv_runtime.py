#!/usr/bin/env python3
# Copyright 2024-2026 The NoKV Authors.
# SPDX-License-Identifier: Apache-2.0

"""Content-addressed NoKV runtime identity and staging helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 remains supported by the helper CLI.
    tomllib = None  # type: ignore[assignment]


BUILD_INFO_SCHEMA_V1 = "nokv.build_info.v1"
BUILD_INFO_SCHEMA_V2 = "nokv.build_info.v2"
# Backward-compatible public alias used by existing v1 fixtures and callers.
BUILD_INFO_SCHEMA = BUILD_INFO_SCHEMA_V1
CRATES_IO_REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
LOCK_SOURCE_DISTRIBUTIONS = frozenset({"brew", "path", "release", "source"})
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CRATE_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
EXACT_CRATE_VERSION_RE = re.compile(r"^=([0-9]+\.[0-9]+\.[0-9]+)$")


@dataclass(frozen=True)
class SourceIdentity:
    schema: str
    nokv_version: str
    nokv_git_commit: str
    source_dirty: bool
    cargo_lock_sha256: str
    holt_crate_version: str
    holt_git_commit: str | None = None
    holt_registry: str | None = None
    holt_checksum_sha256: str | None = None

    def as_dict(self) -> dict[str, Any]:
        common = {
            "schema": self.schema,
            "nokv_version": self.nokv_version,
            "nokv_git_commit": self.nokv_git_commit,
            "source_dirty": self.source_dirty,
            "cargo_lock_sha256": self.cargo_lock_sha256,
            "holt_crate_version": self.holt_crate_version,
        }
        if self.schema == BUILD_INFO_SCHEMA_V1:
            mapping = {**common, "holt_git_commit": self.holt_git_commit}
        elif self.schema == BUILD_INFO_SCHEMA_V2:
            mapping = {
                **common,
                "holt_registry": self.holt_registry,
                "holt_checksum_sha256": self.holt_checksum_sha256,
            }
        else:
            raise ValueError(f"unsupported NoKV build-info schema: {self.schema!r}")
        if identity_from_mapping(mapping, context="source identity") != self:
            raise ValueError(
                f"source identity fields conflict with schema {self.schema}"
            )
        return mapping


@dataclass(frozen=True)
class StagedRuntime:
    command: Path
    sha256: str
    size_bytes: int
    identity: SourceIdentity


@dataclass(frozen=True)
class BuildInfo:
    identity: SourceIdentity
    binary_sha256: str
    binary_size_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.identity.as_dict(),
            "binary_sha256": self.binary_sha256,
            "binary_size_bytes": self.binary_size_bytes,
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def validate_revision(value: str) -> str:
    normalized = value.lower()
    if not REVISION_RE.fullmatch(normalized):
        raise ValueError("NoKV revision must be a full 40-character git commit")
    return normalized


def validate_sha256(value: str, *, label: str = "binary SHA-256") -> str:
    normalized = value.lower()
    if not SHA256_RE.fullmatch(normalized):
        raise ValueError(f"{label} must contain exactly 64 hex characters")
    return normalized


def _run(command: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(f"{' '.join(command)} failed: {detail}")
    return completed.stdout.strip()


def discover_nokv_binary(explicit: str | None = None) -> Path:
    candidate = explicit or os.environ.get("NOKV_BIN") or shutil.which("nokv")
    if candidate is None and shutil.which("brew"):
        completed = subprocess.run(
            ["brew", "--prefix", "nokv"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            candidate = str(Path(completed.stdout.strip()) / "bin" / "nokv")
    if candidate is None:
        raise FileNotFoundError(
            "cannot find nokv; pass --nokv-bin, set NOKV_BIN, or install it on PATH"
        )
    path = Path(candidate).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"nokv binary does not exist: {path}")
    if not os.access(path, os.X_OK):
        raise PermissionError(f"nokv binary is not executable: {path}")
    return path


def _package_block(lock_text: str, name: str) -> dict[str, str]:
    matches: list[dict[str, str]] = []
    for block in re.split(r"(?m)^\[\[package\]\]\s*$", lock_text):
        values = dict(
            re.findall(
                r'(?m)^(name|version|source|checksum) = "([^"]+)"$',
                block,
            )
        )
        if values.get("name") == name:
            matches.append(values)
    if not matches:
        raise ValueError(f"Cargo.lock does not contain package {name}")
    if len(matches) != 1:
        raise ValueError(f"Cargo.lock contains ambiguous package {name} entries")
    return matches[0]


def _strip_toml_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if character in {'"', "'"}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if character == "#" and quote is None:
            return line[:index]
    return line


def _split_inline_toml_table(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    depth = 0
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if character in {'"', "'"}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if quote is not None:
            continue
        if character in "[{(":
            depth += 1
        elif character in "]})":
            depth -= 1
            if depth < 0:
                raise ValueError("Cargo.toml Holt dependency table is malformed")
        elif character == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    if quote is not None or depth != 0:
        raise ValueError("Cargo.toml Holt dependency table is malformed")
    parts.append(value[start:].strip())
    return [part for part in parts if part]


def _fallback_toml_string(value: str) -> str | None:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("Cargo.toml Holt dependency string is malformed") from error
        return parsed if isinstance(parsed, str) else None
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return None


def _fallback_holt_dependency(manifest_text: str) -> dict[str, Any]:
    """Parse only the supported Holt dependency forms on Python 3.10.

    This intentionally is not a general TOML parser. It accepts a one-line
    inline table under [workspace.dependencies] or a dedicated
    [workspace.dependencies.holt] table and fails closed on ambiguity.
    """

    section = ""
    inline: dict[str, Any] | None = None
    dedicated: dict[str, Any] | None = None
    key_re = re.compile(r"^[A-Za-z0-9_-]+$")

    def record(target: dict[str, Any], assignment: str) -> None:
        if "=" not in assignment:
            raise ValueError("Cargo.toml Holt dependency assignment is malformed")
        key, raw_value = (part.strip() for part in assignment.split("=", 1))
        if key_re.fullmatch(key) is None or key in target:
            raise ValueError("Cargo.toml Holt dependency keys are ambiguous")
        parsed_string = _fallback_toml_string(raw_value)
        raw_value = raw_value.strip()
        if parsed_string is not None:
            target[key] = parsed_string
        elif raw_value == "true":
            target[key] = True
        elif raw_value == "false":
            target[key] = False
        else:
            # Do not turn invalid or unsupported unquoted TOML into a string:
            # version identity is accepted only from a syntactically quoted
            # TOML string on the Python 3.10 fallback path.
            target[key] = None

    for raw_line in manifest_text.splitlines():
        line = _strip_toml_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("["):
            match = re.fullmatch(r"\[\s*([^\]]+)\s*\]", line)
            if match is None:
                raise ValueError("Cargo.toml section header is malformed")
            section = match.group(1).strip()
            if section == "workspace.dependencies.holt":
                if dedicated is not None:
                    raise ValueError("Cargo.toml declares Holt more than once")
                dedicated = {}
            continue
        if section == "workspace.dependencies":
            match = re.fullmatch(r"holt\s*=\s*\{(.*)\}", line)
            if match is not None:
                if inline is not None:
                    raise ValueError("Cargo.toml declares Holt more than once")
                inline = {}
                for assignment in _split_inline_toml_table(match.group(1)):
                    record(inline, assignment)
        elif section == "workspace.dependencies.holt":
            assert dedicated is not None
            record(dedicated, line)

    if inline is not None and dedicated is not None:
        raise ValueError("Cargo.toml declares ambiguous Holt dependency tables")
    holt = inline if inline is not None else dedicated
    if holt is None:
        raise ValueError("Cargo.toml must declare Holt as a workspace dependency table")
    return holt


def _workspace_holt_dependency(manifest_text: str) -> dict[str, Any]:
    if tomllib is None:
        return _fallback_holt_dependency(manifest_text)
    try:
        manifest = tomllib.loads(manifest_text)
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"Cargo.toml is not valid TOML: {error}") from error
    workspace = manifest.get("workspace")
    dependencies = workspace.get("dependencies") if isinstance(workspace, dict) else None
    holt = dependencies.get("holt") if isinstance(dependencies, dict) else None
    if not isinstance(holt, dict):
        raise ValueError("Cargo.toml must declare Holt as a workspace dependency table")
    return holt


def _registry_holt_version(manifest_text: str) -> str:
    holt = _workspace_holt_dependency(manifest_text)
    source_keys = {
        "git",
        "rev",
        "branch",
        "tag",
        "path",
        "registry",
        "package",
    }
    ambiguous = sorted(source_keys.intersection(holt))
    if ambiguous:
        raise ValueError(
            "Cargo.toml has ambiguous Holt registry source fields: "
            f"{ambiguous}"
        )
    version = holt.get("version")
    if not isinstance(version, str):
        raise ValueError("Cargo.toml must exactly pin Holt as version = \"=x.y.z\"")
    match = EXACT_CRATE_VERSION_RE.fullmatch(version)
    if match is None:
        raise ValueError("Cargo.toml must exactly pin Holt as version = \"=x.y.z\"")
    return match.group(1)


def _nokv_version(source_root: Path) -> str:
    manifest = source_root / "crates" / "nokv" / "Cargo.toml"
    text = manifest.read_text(encoding="utf-8")
    match = re.search(r'(?m)^version = "([^"]+)"$', text)
    if match is None:
        raise ValueError(f"cannot resolve nokv package version from {manifest}")
    return match.group(1)


def source_identity(source_root: Path, revision: str | None = None) -> SourceIdentity:
    root = source_root.expanduser().resolve()
    cargo_toml = root / "Cargo.toml"
    cargo_lock = root / "Cargo.lock"
    if not cargo_toml.is_file() or not cargo_lock.is_file():
        raise FileNotFoundError(f"not a NoKV source root: {root}")

    git_dir = root / ".git"
    if not git_dir.exists():
        raise ValueError(f"NoKV source identity requires a git checkout: {root}")
    head = validate_revision(_run(["git", "rev-parse", "HEAD"], cwd=root))
    if revision is None:
        revision = head
    revision = validate_revision(revision)
    if head != revision:
        raise ValueError(f"source HEAD {head} does not match revision {revision}")
    dirty = bool(
        _run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=root,
        )
    )

    lock_text = cargo_lock.read_text(encoding="utf-8")
    holt = _package_block(lock_text, "holt")
    source = holt.get("source", "")
    holt_version = holt.get("version")
    if not holt_version:
        raise ValueError("Cargo.lock Holt package has no version")
    manifest_text = cargo_toml.read_text(encoding="utf-8")
    def common_identity() -> dict[str, Any]:
        return {
            "nokv_version": _nokv_version(root),
            "nokv_git_commit": revision,
            "source_dirty": dirty,
            "cargo_lock_sha256": sha256_file(cargo_lock),
            "holt_crate_version": holt_version,
        }

    if source.startswith("git+"):
        parsed_source = urllib.parse.urlsplit(
            source.removeprefix("git+").split("#", 1)[0]
        )
        if (
            parsed_source.scheme != "https"
            or (parsed_source.hostname or "").lower() != "github.com"
            or parsed_source.path.rstrip("/").lower() != "/nokv-lab/holt.git"
        ):
            raise ValueError(
                "Cargo.lock Holt package is not pinned to NoKV-Lab/holt"
            )
        source_commit = source.rsplit("#", 1)[-1]
        holt_commit = validate_revision(source_commit)
        manifest_match = re.search(
            r'(?m)^holt\s*=\s*\{[^\n]*\brev\s*=\s*"([0-9a-fA-F]{40})"',
            manifest_text,
        )
        if manifest_match is None:
            raise ValueError("Cargo.toml must pin Holt with a full git rev")
        manifest_commit = validate_revision(manifest_match.group(1))
        if manifest_commit != holt_commit:
            raise ValueError(
                "Holt revision differs between Cargo.toml and Cargo.lock: "
                f"{manifest_commit} != {holt_commit}"
            )
        return SourceIdentity(
            schema=BUILD_INFO_SCHEMA_V1,
            **common_identity(),
            holt_git_commit=holt_commit,
        )

    if source.startswith("registry+"):
        if source != CRATES_IO_REGISTRY:
            raise ValueError(
                "Cargo.lock Holt package is not from the recognized crates.io "
                f"registry {CRATES_IO_REGISTRY}: {source!r}"
            )
        manifest_version = _registry_holt_version(manifest_text)
        if not CRATE_VERSION_RE.fullmatch(holt_version):
            raise ValueError("Cargo.lock Holt package has an invalid crate version")
        if manifest_version != holt_version:
            raise ValueError(
                "Holt version differs between Cargo.toml and Cargo.lock: "
                f"{manifest_version} != {holt_version}"
            )
        checksum = holt.get("checksum")
        if not isinstance(checksum, str) or not checksum:
            raise ValueError("Cargo.lock registry Holt package has no checksum")
        normalized_checksum = validate_sha256(
            checksum,
            label="Cargo.lock Holt checksum",
        )
        if normalized_checksum != checksum:
            raise ValueError("Cargo.lock Holt checksum must use lowercase hex")
        return SourceIdentity(
            schema=BUILD_INFO_SCHEMA_V2,
            **common_identity(),
            holt_git_commit=None,
            holt_registry=source,
            holt_checksum_sha256=checksum,
        )

    raise ValueError(
        "Cargo.lock Holt package has an ambiguous or unsupported source identity"
    )


def _identity_from_mapping(
    data: Any,
    *,
    context: str,
    container_fields: frozenset[str],
) -> SourceIdentity:
    if not isinstance(data, dict) or data.get("schema") not in {
        BUILD_INFO_SCHEMA_V1,
        BUILD_INFO_SCHEMA_V2,
    }:
        raise ValueError(
            f"{context} is not a supported {BUILD_INFO_SCHEMA_V1} or "
            f"{BUILD_INFO_SCHEMA_V2} object"
        )
    schema = data["schema"]
    common_fields = frozenset(
        {
            "schema",
            "nokv_version",
            "nokv_git_commit",
            "source_dirty",
            "cargo_lock_sha256",
            "holt_crate_version",
        }
    )
    if schema == BUILD_INFO_SCHEMA_V1:
        if "holt_registry" in data or "holt_checksum_sha256" in data:
            raise ValueError(f"{context}: ambiguous Holt source fields for v1")
        source_fields = frozenset({"holt_git_commit"})
    else:
        if "holt_git_commit" in data:
            raise ValueError(f"{context}: ambiguous Holt source fields for v2")
        source_fields = frozenset({"holt_registry", "holt_checksum_sha256"})

    expected_fields = common_fields | source_fields | container_fields
    actual_fields = set(data)
    missing_fields = expected_fields - actual_fields
    unsupported_fields = actual_fields - expected_fields
    if missing_fields or unsupported_fields:
        raise ValueError(
            f"{context}: build identity key set differs; "
            f"missing fields={sorted(missing_fields)}, "
            f"unsupported fields={sorted(unsupported_fields)}"
        )
    required_strings = (
        "nokv_version",
        "nokv_git_commit",
        "cargo_lock_sha256",
        "holt_crate_version",
    )
    for field in required_strings:
        if not isinstance(data.get(field), str) or not data[field]:
            raise ValueError(f"{context}: {field} must be a non-empty string")
    if not isinstance(data.get("source_dirty"), bool):
        raise ValueError(f"{context}: source_dirty must be a boolean")
    normalized_nokv_revision = validate_revision(data["nokv_git_commit"])
    if normalized_nokv_revision != data["nokv_git_commit"]:
        raise ValueError(f"{context}: nokv_git_commit must use lowercase hex")
    normalized_cargo_lock_sha256 = validate_sha256(
        data["cargo_lock_sha256"],
        label=f"{context}: cargo_lock_sha256",
    )
    if normalized_cargo_lock_sha256 != data["cargo_lock_sha256"]:
        raise ValueError(f"{context}: cargo_lock_sha256 must use lowercase hex")
    common = {
        "schema": schema,
        "nokv_version": data["nokv_version"],
        "nokv_git_commit": data["nokv_git_commit"],
        "source_dirty": data["source_dirty"],
        "cargo_lock_sha256": data["cargo_lock_sha256"],
        "holt_crate_version": data["holt_crate_version"],
    }
    if schema == BUILD_INFO_SCHEMA_V1:
        holt_git_commit = data.get("holt_git_commit")
        if not isinstance(holt_git_commit, str) or not holt_git_commit:
            raise ValueError(f"{context}: holt_git_commit must be a non-empty string")
        normalized_holt_revision = validate_revision(holt_git_commit)
        if normalized_holt_revision != holt_git_commit:
            raise ValueError(f"{context}: holt_git_commit must use lowercase hex")
        return SourceIdentity(
            **common,
            holt_git_commit=holt_git_commit,
        )

    holt_registry = data.get("holt_registry")
    if holt_registry != CRATES_IO_REGISTRY:
        raise ValueError(
            f"{context}: holt_registry must equal {CRATES_IO_REGISTRY}"
        )
    holt_checksum = data.get("holt_checksum_sha256")
    if not isinstance(holt_checksum, str) or not holt_checksum:
        raise ValueError(
            f"{context}: holt_checksum_sha256 must be a non-empty string"
        )
    normalized_checksum = validate_sha256(
        holt_checksum,
        label=f"{context}: holt_checksum_sha256",
    )
    if normalized_checksum != holt_checksum:
        raise ValueError(f"{context}: holt_checksum_sha256 must use lowercase hex")
    if not CRATE_VERSION_RE.fullmatch(data["holt_crate_version"]):
        raise ValueError(f"{context}: holt_crate_version must be x.y.z")
    return SourceIdentity(
        **common,
        holt_git_commit=None,
        holt_registry=holt_registry,
        holt_checksum_sha256=holt_checksum,
    )


def identity_from_mapping(data: Any, *, context: str) -> SourceIdentity:
    """Parse an exact standalone v1 or v2 source identity object."""
    return _identity_from_mapping(
        data,
        context=context,
        container_fields=frozenset(),
    )


def lock_source_identity_from_mapping(data: Any, *, context: str) -> SourceIdentity:
    """Parse the exact source object stored in a Workbench deployment lock."""
    identity = _identity_from_mapping(
        data,
        context=context,
        container_fields=frozenset({"distribution"}),
    )
    distribution = data["distribution"]
    if (
        not isinstance(distribution, str)
        or distribution not in LOCK_SOURCE_DISTRIBUTIONS
    ):
        raise ValueError(
            f"{context}: distribution must be one of "
            f"{sorted(LOCK_SOURCE_DISTRIBUTIONS)}"
        )
    return identity


def load_build_info(path: Path) -> BuildInfo:
    data = json.loads(path.expanduser().read_text(encoding="utf-8"))
    return build_info_from_mapping(data, context=str(path))


def build_info_from_mapping(data: Any, *, context: str) -> BuildInfo:
    identity = _identity_from_mapping(
        data,
        context=context,
        container_fields=frozenset({"binary_sha256", "binary_size_bytes"}),
    )
    binary_sha256 = data.get("binary_sha256")
    binary_size_bytes = data.get("binary_size_bytes")
    if not isinstance(binary_sha256, str):
        raise ValueError(f"{context}: binary_sha256 must be a string")
    normalized_binary_sha256 = validate_sha256(binary_sha256)
    if normalized_binary_sha256 != binary_sha256:
        raise ValueError(f"{context}: binary_sha256 must use lowercase hex")
    if not isinstance(binary_size_bytes, int) or isinstance(binary_size_bytes, bool):
        raise ValueError(f"{context}: binary_size_bytes must be an integer")
    if binary_size_bytes < 1:
        raise ValueError(f"{context}: binary_size_bytes must be positive")
    return BuildInfo(identity, binary_sha256, binary_size_bytes)


def infer_distribution(binary: Path) -> str:
    parts = {part.lower() for part in binary.parts}
    if "cellar" in parts or "homebrew" in parts:
        return "brew"
    return "path"


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _directory_open_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _regular_file_open_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _entry_metadata_at(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _raise_directory_open_error(
    directory_fd: int, name: str, path: Path, error: OSError
) -> None:
    metadata = _entry_metadata_at(directory_fd, name)
    if metadata is not None and stat.S_ISLNK(metadata.st_mode):
        raise ValueError(
            f"managed runtime path contains a symlink component: {path}"
        ) from error
    if metadata is not None and not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(
            f"managed runtime path component is not a directory: {path}"
        ) from error
    raise error


def _open_directory_at(
    directory_fd: int, name: str, path: Path, *, create: bool
) -> int:
    try:
        return os.open(name, _directory_open_flags(), dir_fd=directory_fd)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, 0o755, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except FileExistsError:
            pass
    except OSError as error:
        _raise_directory_open_error(directory_fd, name, path, error)

    try:
        return os.open(name, _directory_open_flags(), dir_fd=directory_fd)
    except OSError as error:
        _raise_directory_open_error(directory_fd, name, path, error)
    raise AssertionError("unreachable")


def _open_project_directory(path: Path) -> int:
    try:
        descriptor = os.open(path, _directory_open_flags())
    except OSError as error:
        metadata = _lstat(path)
        if metadata is not None and stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"project path is a symlink: {path}") from error
        if metadata is not None and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"project path is not a directory: {path}") from error
        raise
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError(f"project path is not a directory: {path}")
    return descriptor


def _create_temp_file_at(directory_fd: int, prefix: str) -> tuple[int, str]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _ in range(128):
        name = f"{prefix}{secrets.token_hex(12)}"
        try:
            return os.open(name, flags, 0o600, dir_fd=directory_fd), name
        except FileExistsError:
            continue
    raise FileExistsError("cannot allocate a unique managed runtime temporary file")


def _unlink_at(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def _open_regular_file_at(directory_fd: int, name: str, path: Path) -> int:
    try:
        descriptor = os.open(name, _regular_file_open_flags(), dir_fd=directory_fd)
    except OSError as error:
        metadata = _entry_metadata_at(directory_fd, name)
        if metadata is not None and (
            stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode)
        ):
            raise ValueError(
                f"content-addressed runtime is not a regular file: {path}"
            ) from error
        raise
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError(f"content-addressed runtime is not a regular file: {path}")
    return descriptor


def _sha256_descriptor(descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1 << 20):
        digest.update(chunk)
        size_bytes += len(chunk)
    return digest.hexdigest(), size_bytes


def _read_descriptor(descriptor: int) -> bytes:
    chunks = []
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1 << 20):
        chunks.append(chunk)
    return b"".join(chunks)


def _load_build_info_descriptor(descriptor: int, *, context: str) -> BuildInfo:
    try:
        data = json.loads(_read_descriptor(descriptor).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{context} is not valid UTF-8 JSON") from error
    return build_info_from_mapping(data, context=context)


def _build_info_text(build_info: BuildInfo) -> str:
    return (
        json.dumps(build_info.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )


def _verify_directory_binding(path: Path, descriptor: int) -> None:
    metadata = _lstat(path)
    if metadata is None:
        raise ValueError(f"managed runtime path changed while staging: {path}")
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"managed runtime path contains a symlink component: {path}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"managed runtime path component is not a directory: {path}")
    opened_metadata = os.fstat(descriptor)
    if (metadata.st_dev, metadata.st_ino) != (
        opened_metadata.st_dev,
        opened_metadata.st_ino,
    ):
        raise ValueError(f"managed runtime path changed while staging: {path}")


def _verify_regular_binding_at(
    directory_fd: int, name: str, descriptor: int, path: Path
) -> None:
    metadata = _entry_metadata_at(directory_fd, name)
    opened_metadata = os.fstat(descriptor)
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"content-addressed runtime changed while staging: {path}")
    if (metadata.st_dev, metadata.st_ino) != (
        opened_metadata.st_dev,
        opened_metadata.st_ino,
    ):
        raise ValueError(f"content-addressed runtime changed while staging: {path}")


def _copy_with_sha256(source: Path, target: Any) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    with source.open("rb") as source_handle:
        while chunk := source_handle.read(1 << 20):
            target.write(chunk)
            digest.update(chunk)
            size_bytes += len(chunk)
    return digest.hexdigest(), size_bytes


def stage_runtime(
    project: Path,
    binary: Path,
    identity: SourceIdentity,
    *,
    expected_sha256: str | None = None,
) -> StagedRuntime:
    project = project.expanduser().resolve()
    lingtai_root = project / ".lingtai"
    binary = binary.expanduser().resolve()
    # Validate the complete schema-specific source identity before creating any
    # managed runtime directory or copying artifact bytes.
    identity.as_dict()
    digest = sha256_file(binary)
    if expected_sha256 is not None and digest != validate_sha256(expected_sha256):
        raise ValueError(
            f"nokv binary SHA-256 mismatch: expected {expected_sha256}, got {digest}"
        )

    revision = validate_revision(identity.nokv_git_commit)
    runtime_root = lingtai_root / "runtime"
    nokv_root = runtime_root / "nokv"
    revision_dir = nokv_root / revision
    runtime_dir = revision_dir / digest
    command = runtime_dir / "nokv"
    build_info = runtime_dir / "build-info.json"

    directory_descriptors: list[tuple[Path, int]] = []
    revision_descriptor: int | None = None
    digest_descriptor: int | None = None
    command_descriptor: int | None = None
    build_info_descriptor: int | None = None
    command_temp_name = ""
    build_info_temp_name = ""
    try:
        project_descriptor = _open_project_directory(project)
        directory_descriptors.append((project, project_descriptor))
        try:
            lingtai_descriptor = _open_directory_at(
                project_descriptor, ".lingtai", lingtai_root, create=False
            )
        except FileNotFoundError as error:
            raise FileNotFoundError(
                f"LingTai project has no .lingtai directory: {project}"
            ) from error
        directory_descriptors.append((lingtai_root, lingtai_descriptor))
        runtime_descriptor = _open_directory_at(
            lingtai_descriptor, "runtime", runtime_root, create=True
        )
        directory_descriptors.append((runtime_root, runtime_descriptor))
        nokv_descriptor = _open_directory_at(
            runtime_descriptor, "nokv", nokv_root, create=True
        )
        directory_descriptors.append((nokv_root, nokv_descriptor))
        revision_descriptor = _open_directory_at(
            nokv_descriptor, revision, revision_dir, create=True
        )
        directory_descriptors.append((revision_dir, revision_descriptor))

        temp_descriptor, command_temp_name = _create_temp_file_at(
            revision_descriptor, ".nokv."
        )
        with os.fdopen(temp_descriptor, "wb") as target:
            copied_digest, copied_size = _copy_with_sha256(binary, target)
            os.fchmod(
                target.fileno(),
                stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP,
            )
            target.flush()
            os.fsync(target.fileno())
        if copied_digest != digest:
            raise ValueError(
                "nokv binary changed while staging: "
                f"initial SHA-256 {digest}, copied SHA-256 {copied_digest}"
            )

        digest_descriptor = _open_directory_at(
            revision_descriptor, digest, runtime_dir, create=True
        )
        directory_descriptors.append((runtime_dir, digest_descriptor))

        if _entry_metadata_at(digest_descriptor, "nokv") is None:
            try:
                os.link(
                    command_temp_name,
                    "nokv",
                    src_dir_fd=revision_descriptor,
                    dst_dir_fd=digest_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                pass
            else:
                os.fsync(digest_descriptor)
        command_descriptor = _open_regular_file_at(digest_descriptor, "nokv", command)
        staged_digest, staged_size = _sha256_descriptor(command_descriptor)
        if staged_digest != digest:
            raise ValueError(
                f"content-addressed runtime was modified in place: {command}"
            )
        if staged_size != copied_size:
            raise ValueError(
                f"content-addressed runtime size differs from copied binary: {command}"
            )

        expected_build_info = BuildInfo(identity, digest, staged_size)
        if _entry_metadata_at(digest_descriptor, "build-info.json") is None:
            build_info_temp_descriptor, build_info_temp_name = _create_temp_file_at(
                digest_descriptor, ".build-info.json."
            )
            with os.fdopen(build_info_temp_descriptor, "w", encoding="utf-8") as handle:
                handle.write(_build_info_text(expected_build_info))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(
                    build_info_temp_name,
                    "build-info.json",
                    src_dir_fd=digest_descriptor,
                    dst_dir_fd=digest_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                pass
            else:
                os.fsync(digest_descriptor)

        build_info_descriptor = _open_regular_file_at(
            digest_descriptor, "build-info.json", build_info
        )
        if (
            _load_build_info_descriptor(build_info_descriptor, context=str(build_info))
            != expected_build_info
        ):
            raise ValueError(
                f"content-addressed build identity conflicts with {build_info}"
            )

        for path, descriptor in directory_descriptors:
            _verify_directory_binding(path, descriptor)
        _verify_regular_binding_at(
            digest_descriptor, "nokv", command_descriptor, command
        )
        _verify_regular_binding_at(
            digest_descriptor,
            "build-info.json",
            build_info_descriptor,
            build_info,
        )
    finally:
        if build_info_temp_name and digest_descriptor is not None:
            _unlink_at(digest_descriptor, build_info_temp_name)
        if command_temp_name and revision_descriptor is not None:
            _unlink_at(revision_descriptor, command_temp_name)
        if build_info_descriptor is not None:
            os.close(build_info_descriptor)
        if command_descriptor is not None:
            os.close(command_descriptor)
        for _, descriptor in reversed(directory_descriptors):
            os.close(descriptor)

    return StagedRuntime(
        command=command,
        sha256=digest,
        size_bytes=staged_size,
        identity=identity,
    )


def write_build_info(path: Path, identity: SourceIdentity, binary: Path) -> bool:
    path = path.expanduser().resolve()
    binary = binary.expanduser().resolve()
    build_info = BuildInfo(
        identity=identity,
        binary_sha256=sha256_file(binary),
        binary_size_bytes=binary.stat().st_size,
    )
    text = _build_info_text(build_info)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return True
