#!/usr/bin/env python3
# Copyright 2024-2026 The NoKV Authors.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
UP_SCRIPT = SCRIPT_DIR / "up.sh"
sys.path.insert(0, str(SCRIPT_DIR))

import nokv_runtime as runtime  # noqa: E402
import workbench_contract as contract  # noqa: E402


HOLT_REVISION = "b" * 40


class UpScriptTest(unittest.TestCase):
    def _run_bash(
        self,
        command: str,
        *args: str,
        env: dict[str, str] | None = None,
        unset: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        process_env = os.environ.copy()
        for name in unset:
            process_env.pop(name, None)
        if env:
            process_env.update(env)
        return subprocess.run(
            ["bash", "-c", command, "up-test", str(UP_SCRIPT), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=process_env,
        )

    def _read_calls(self, path: Path) -> list[list[str]]:
        fields = path.read_bytes().split(b"\0")
        calls: list[list[str]] = []
        current: list[str] | None = None
        for field in fields:
            value = field.decode()
            if value == "CALL":
                self.assertIsNone(current)
                current = []
            elif value == "END":
                self.assertIsNotNone(current)
                calls.append(current or [])
                current = None
            elif value and current is not None:
                current.append(value)
        self.assertIsNone(current)
        return calls

    def _assert_option(self, args: list[str], name: str, value: str) -> None:
        index = args.index(name)
        self.assertEqual(args[index + 1], value)

    def _make_source(self, root: Path) -> Path:
        source = root / "source"
        (source / "crates" / "nokv").mkdir(parents=True)
        (source / "Cargo.toml").write_text(
            "[workspace]\n"
            'members = ["crates/nokv"]\n'
            "[workspace.dependencies]\n"
            "holt = { git = \"https://github.com/NoKV-Lab/holt.git\", "
            f'rev = "{HOLT_REVISION}" }}\n',
            encoding="utf-8",
        )
        (source / "Cargo.lock").write_text(
            "version = 4\n\n"
            "[[package]]\n"
            'name = "holt"\n'
            'version = "0.8.1"\n'
            'source = "git+https://github.com/NoKV-Lab/holt.git'
            f'?rev={HOLT_REVISION}#{HOLT_REVISION}"\n',
            encoding="utf-8",
        )
        (source / "crates" / "nokv" / "Cargo.toml").write_text(
            '[package]\nname = "nokv"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
        (source / ".gitignore").write_text("/target/\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=source, check=True)
        subprocess.run(["git", "add", "."], cwd=source, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=NoKV Test",
                "-c",
                "user.email=nokv-test@example.invalid",
                "commit",
                "-q",
                "-m",
                "test fixture",
            ],
            cwd=source,
            check=True,
        )
        return source

    def _source_revision(self, source: Path) -> str:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True
        ).strip()

    def _profile_tools(self, role: str | None) -> list[dict]:
        tools = [
            {
                "name": name,
                "description": f"fixture description for {name}",
                "inputSchema": copy.deepcopy(contract.FROZEN_INPUT_SCHEMAS[name]),
            }
            for name in contract.FROZEN_TOOL_ORDER
        ]
        if role is not None:
            tools.extend(
                {
                    "name": name,
                    "description": contract.FROZEN_SHARED_TOOL_DEFINITIONS[name][
                        "description"
                    ],
                    "inputSchema": copy.deepcopy(
                        contract.FROZEN_SHARED_TOOL_DEFINITIONS[name]["inputSchema"]
                    ),
                }
                for name in contract.FROZEN_LINGTAI_ROLE_TOOL_ORDER[role]
            )
        return tools

    def _make_profile_binary(
        self, root: Path, workbench_tools: list[dict], lingtai_tools: list[dict]
    ) -> Path:
        binary = root / "nokv"
        responses = {
            "workbench": {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"tools": workbench_tools},
            },
            "lingtai": {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"tools": lingtai_tools},
            },
        }
        binary.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import sys\n"
            f"responses = {responses!r}\n"
            "profile = sys.argv[sys.argv.index('--profile') + 1]\n"
            "for line in sys.stdin:\n"
            "    json.loads(line)\n"
            "    print(json.dumps(responses[profile], separators=(',', ':')))\n",
            encoding="utf-8",
        )
        os.chmod(binary, 0o755)
        return binary

    def _canonical_grant(
        self, workspace: str, actor: str, *, role: str = "reader"
    ) -> tuple[str, bytes]:
        now_ms = time.time_ns() // 1_000_000
        canonical = json.dumps(
            {
                "actor_id": actor,
                "audience": "nokv-mcp:lingtai",
                "expires_at_unix_ms": now_ms + 60_000,
                "grant_id": "grant-1",
                "issued_at_unix_ms": now_ms - 1_000,
                "issuer": "lingtai-workbench-sync",
                "role": role,
                "schema": "nokv.lingtai.workspace_grant.v1",
                "workspace_id": workspace,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return base64.urlsafe_b64encode(canonical).decode().rstrip("="), canonical

    def test_term_exits_and_releases_update_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            command = r"""
source "$1"
STATE_DIR="$2"
UP_LOCK_DIR="${STATE_DIR}/up.lock"
acquire_up_lock
kill -TERM $$
printf 'continued-after-term\n'
"""
            completed = subprocess.run(
                ["bash", "-c", command, "up-test", str(UP_SCRIPT), str(state_dir)],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 143, completed.stderr)
            self.assertNotIn("continued-after-term", completed.stdout)
            self.assertFalse((state_dir / "up.lock").exists())

    def test_custom_credentials_fail_closed(self) -> None:
        command = r"""
source "$1"
LINGTAI_WORKBENCH_S3_ACCESS_KEY_ID=custom
LINGTAI_WORKBENCH_S3_SECRET_ACCESS_KEY=custom
validate_guarded_credentials
"""
        completed = subprocess.run(
            ["bash", "-c", command, "up-test", str(UP_SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("dedicated local RustFS credentials only", completed.stderr)

    def test_lingtai_tuple_is_identical_across_all_launch_phases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "calls.bin"
            command = r"""
source "$1"
CAPTURE="$2"
python3() {
  local argument=""
  local preflight=0
  printf 'CALL\0' >>"${CAPTURE}"
  for argument in "$@"; do
    printf '%s\0' "${argument}" >>"${CAPTURE}"
    [[ "${argument}" == "--preflight-only" ]] && preflight=1
  done
  printf 'END\0' >>"${CAPTURE}"
  if [[ "${preflight}" -eq 1 ]]; then
    printf '%s\n' 'agent_state_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
  fi
}
validate_profile_selection
PINNED_AGENT=coordinator
PINNED_AGENT_IDENTITY=agent-identity-token
NOKV_BIN=/immutable/nokv
RUNTIME_IDENTITY_ARGS=(--build-info /immutable/build-info.json --distribution source)
preflight_agent /project
probe_candidate_contract /project
sync_agent /project
check_agent /project
"""
            grant = "canonical-launcher-grant"
            completed = self._run_bash(
                command,
                str(capture),
                env={
                    "LINGTAI_WORKBENCH_MCP_PROFILE": "lingtai",
                    "LINGTAI_WORKBENCH_WORKSPACE_ID": "workspace-123",
                    "LINGTAI_WORKBENCH_WORKSPACE_ACTOR_ID": "actor-456",
                    "LINGTAI_WORKBENCH_WORKSPACE_GRANT": grant,
                    "LINGTAI_WORKBENCH_SERVER_BIND": "127.0.0.1:7799",
                    "LINGTAI_WORKBENCH_OBJECT_BACKEND": "rustfs",
                    "LINGTAI_WORKBENCH_S3_ENDPOINT": "http://127.0.0.1:9000",
                    "LINGTAI_WORKBENCH_S3_BUCKET": "nokv-lingtai-workbench",
                    "LINGTAI_WORKBENCH_ROOT": "/agents/{agent_id}/wb",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotIn(grant, completed.stdout)
            self.assertNotIn(grant, completed.stderr)
            calls = self._read_calls(capture)
            self.assertEqual(len(calls), 4)
            self.assertIn("--preflight-only", calls[0])
            self.assertIn("--probe-only", calls[1])
            self.assertNotIn("--preflight-only", calls[2])
            self.assertNotIn("--probe-only", calls[2])
            self.assertIn("--check", calls[3])
            for args in calls[:3]:
                self._assert_option(args, "--profile", "lingtai")
                self._assert_option(args, "--workspace-id", "workspace-123")
                self._assert_option(args, "--workspace-actor-id", "actor-456")
                self._assert_option(args, "--workspace-grant", grant)
                self._assert_option(args, "--server-bind", "127.0.0.1:7799")
                self._assert_option(args, "--object-backend", "rustfs")
                self._assert_option(
                    args,
                    "--s3-endpoint",
                    "http://127.0.0.1:9000",
                )
                self._assert_option(
                    args,
                    "--s3-bucket",
                    "nokv-lingtai-workbench",
                )
                self._assert_option(
                    args,
                    "--workbench-root",
                    "/agents/{agent_id}/wb",
                )
                self._assert_option(args, "--agent", "coordinator")
                self._assert_option(
                    args,
                    "--orchestration-agent-identity",
                    "agent-identity-token",
                )
                self.assertNotIn("--workspace-dev-membership", args)
            self.assertNotIn("--profile", calls[3])
            self.assertNotIn("--workspace-id", calls[3])
            self.assertNotIn("--workspace-actor-id", calls[3])
            self.assertNotIn("--workspace-grant", calls[3])
            self._assert_option(calls[3], "--agent", "coordinator")
            self._assert_option(
                calls[3],
                "--orchestration-agent-identity",
                "agent-identity-token",
            )
            for args in (calls[0], calls[1], calls[3]):
                self.assertNotIn("--orchestration-agent-state-sha256", args)
            self._assert_option(
                calls[2],
                "--orchestration-agent-state-sha256",
                "a" * 64,
            )

    def test_omitted_profile_defaults_all_launch_phases_to_workbench(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "calls.bin"
            command = r"""
source "$1"
CAPTURE="$2"
python3() {
  local argument=""
  local preflight=0
  printf 'CALL\0' >>"${CAPTURE}"
  for argument in "$@"; do
    printf '%s\0' "${argument}" >>"${CAPTURE}"
    [[ "${argument}" == "--preflight-only" ]] && preflight=1
  done
  printf 'END\0' >>"${CAPTURE}"
  if [[ "${preflight}" -eq 1 ]]; then
    printf '%s\n' 'agent_state_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
  fi
}
validate_profile_selection
PINNED_AGENT=coordinator
PINNED_AGENT_IDENTITY=agent-identity-token
NOKV_BIN=/immutable/nokv
RUNTIME_IDENTITY_ARGS=(--build-info /immutable/build-info.json --distribution source)
preflight_agent /project
probe_candidate_contract /project
sync_agent /project
check_agent /project
"""
            launch_env = (
                "LINGTAI_WORKBENCH_SERVER_BIND",
                "LINGTAI_WORKBENCH_OBJECT_BACKEND",
                "LINGTAI_WORKBENCH_S3_ENDPOINT",
                "LINGTAI_WORKBENCH_S3_BUCKET",
                "LINGTAI_WORKBENCH_ROOT",
            )
            completed = self._run_bash(
                command,
                str(capture),
                unset=launch_env,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            calls = self._read_calls(capture)
            self.assertEqual(len(calls), 4)
            for args in calls[:3]:
                self.assertNotIn("--profile", args)
                self.assertNotIn("--workspace-id", args)
                self.assertNotIn("--workspace-actor-id", args)
                self.assertNotIn("--workspace-grant", args)
                self._assert_option(args, "--agent", "coordinator")
                self._assert_option(
                    args,
                    "--orchestration-agent-identity",
                    "agent-identity-token",
                )
            self.assertIn("--check", calls[3])
            self.assertNotIn("--profile", calls[3])
            for option in (
                "--server-bind",
                "--object-backend",
                "--s3-endpoint",
                "--s3-bucket",
                "--workbench-root",
            ):
                self.assertNotIn(option, calls[0])
            for args in calls[1:3]:
                self._assert_option(args, "--server-bind", "127.0.0.1:7799")
                self._assert_option(args, "--object-backend", "rustfs")
                self._assert_option(
                    args,
                    "--s3-endpoint",
                    "http://127.0.0.1:9000",
                )
                self._assert_option(
                    args,
                    "--s3-bucket",
                    "nokv-lingtai-workbench",
                )
                self._assert_option(
                    args,
                    "--workbench-root",
                    "/agents/{agent_id}/wb",
                )

    def test_explicit_workbench_profile_is_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "calls.bin"
            command = r"""
source "$1"
CAPTURE="$2"
python3() {
  local argument=""
  local preflight=0
  printf 'CALL\0' >>"${CAPTURE}"
  for argument in "$@"; do
    printf '%s\0' "${argument}" >>"${CAPTURE}"
    [[ "${argument}" == "--preflight-only" ]] && preflight=1
  done
  printf 'END\0' >>"${CAPTURE}"
  if [[ "${preflight}" -eq 1 ]]; then
    printf '%s\n' 'agent_state_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
  fi
}
validate_profile_selection
PINNED_AGENT=coordinator
PINNED_AGENT_IDENTITY=agent-identity-token
preflight_agent /project
"""
            completed = self._run_bash(
                command,
                str(capture),
                env={"LINGTAI_WORKBENCH_MCP_PROFILE": "workbench"},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            calls = self._read_calls(capture)
            self.assertEqual(len(calls), 1)
            self._assert_option(calls[0], "--profile", "workbench")
            self._assert_option(calls[0], "--agent", "coordinator")

    def test_empty_launch_environment_does_not_claim_preflight_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "calls.bin"
            command = r"""
source "$1"
CAPTURE="$2"
python3() {
  local argument=""
  printf 'CALL\0' >>"${CAPTURE}"
  for argument in "$@"; do
    printf '%s\0' "${argument}" >>"${CAPTURE}"
  done
  printf 'END\0' >>"${CAPTURE}"
  printf '%s\n' 'agent_state_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
}
validate_profile_selection
PINNED_AGENT=coordinator
PINNED_AGENT_IDENTITY=agent-identity-token
preflight_agent /project
"""
            completed = self._run_bash(
                command,
                str(capture),
                env={
                    "LINGTAI_WORKBENCH_SERVER_BIND": "",
                    "LINGTAI_WORKBENCH_OBJECT_BACKEND": "",
                    "LINGTAI_WORKBENCH_S3_ENDPOINT": "",
                    "LINGTAI_WORKBENCH_S3_BUCKET": "",
                    "LINGTAI_WORKBENCH_ROOT": "",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            calls = self._read_calls(capture)
            self.assertEqual(len(calls), 1)
            for option in (
                "--server-bind",
                "--object-backend",
                "--s3-endpoint",
                "--s3-bucket",
                "--workbench-root",
            ):
                self.assertNotIn(option, calls[0])

    def test_automatic_agent_selection_is_resolved_once_and_pinned_across_phases(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            first = project / ".lingtai" / "coordinator-a"
            second = project / ".lingtai" / "coordinator-b"
            for agent in (first, second):
                agent.mkdir(parents=True)
                (agent / "init.json").write_text(
                    '{"mcp": {}}\n', encoding="utf-8"
                )
            first_status = first / ".status.json"
            second_status = second / ".status.json"
            first_status.write_text(
                '{"runtime": {"running": true}}\n', encoding="utf-8"
            )
            second_status.write_text(
                '{"runtime": {"running": false}}\n', encoding="utf-8"
            )
            capture = root / "calls.bin"
            command = r"""
source "$1"
validate_profile_selection
resolve_agent_once "$2"
printf '%s\n' '{"runtime":{"running":false}}' >"$4"
printf '%s\n' '{"runtime":{"running":true}}' >"$5"
CAPTURE="$3"
python3() {
  local argument=""
  local preflight=0
  printf 'CALL\0' >>"${CAPTURE}"
  for argument in "$@"; do
    printf '%s\0' "${argument}" >>"${CAPTURE}"
    [[ "${argument}" == "--preflight-only" ]] && preflight=1
  done
  printf 'END\0' >>"${CAPTURE}"
  if [[ "${preflight}" -eq 1 ]]; then
    printf '%s\n' 'agent_state_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
  fi
}
NOKV_BIN=/immutable/nokv
RUNTIME_IDENTITY_ARGS=(--build-info /immutable/build-info.json --distribution source)
preflight_agent "$2"
probe_candidate_contract "$2"
sync_agent "$2"
check_agent "$2"
"""
            completed = self._run_bash(
                command,
                str(project),
                str(capture),
                str(first_status),
                str(second_status),
                unset=("LINGTAI_WORKBENCH_AGENT",),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("pinned Agent: coordinator-a", completed.stdout)
            calls = self._read_calls(capture)
            self.assertEqual(len(calls), 4)
            for args in calls:
                self._assert_option(args, "--agent", "coordinator-a")
            self.assertTrue(
                json.loads(second_status.read_text(encoding="utf-8"))["runtime"][
                    "running"
                ]
            )

    def test_stage_only_does_not_receive_profile_or_workspace_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "calls.bin"
            staged = root / "staged-nokv"
            candidate = root / "candidate-nokv"
            build_info = root / "build-info.json"
            staged.touch(mode=0o755)
            candidate.touch(mode=0o755)
            build_info.write_text("{}\n")
            command = r"""
source "$1"
CAPTURE="$2"
STAGED="$3"
python3() {
  local argument=""
  printf 'CALL\0' >>"${CAPTURE}"
  for argument in "$@"; do
    printf '%s\0' "${argument}" >>"${CAPTURE}"
  done
  printf 'END\0' >>"${CAPTURE}"
  printf '%s\n' "${STAGED}"
}
validate_profile_selection
prepare_runtime /project
"""
            completed = self._run_bash(
                command,
                str(capture),
                str(staged),
                env={
                    "NOKV_BIN": str(candidate),
                    "NOKV_BUILD_INFO": str(build_info),
                    "LINGTAI_WORKBENCH_MCP_PROFILE": "lingtai",
                    "LINGTAI_WORKBENCH_WORKSPACE_ID": "workspace-123",
                    "LINGTAI_WORKBENCH_WORKSPACE_ACTOR_ID": "actor-456",
                    "LINGTAI_WORKBENCH_WORKSPACE_GRANT": "canonical-launcher-grant",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            calls = self._read_calls(capture)
            self.assertEqual(len(calls), 1)
            self.assertIn("--stage-only", calls[0])
            self.assertNotIn("--profile", calls[0])
            self.assertNotIn("--workspace-id", calls[0])
            self.assertNotIn("--workspace-actor-id", calls[0])
            self.assertNotIn("--workspace-grant", calls[0])

    def test_invalid_profile_fails_before_staging_and_releases_update_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            staged_marker = Path(tmp) / "staged"
            command = r"""
source "$1"
STATE_DIR="$2"
UP_LOCK_DIR="${STATE_DIR}/up.lock"
STAGED_MARKER="$3"
require_cmd() { :; }
prepare_runtime() { touch "${STAGED_MARKER}"; }
main
"""
            completed = self._run_bash(
                command,
                str(state_dir),
                str(staged_marker),
                env={"LINGTAI_WORKBENCH_MCP_PROFILE": "unsupported"},
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn("must be exactly workbench or lingtai", completed.stderr)
            self.assertFalse(staged_marker.exists())
            self.assertFalse((state_dir / "up.lock").exists())

    def test_explicit_empty_profile_is_rejected(self) -> None:
        completed = self._run_bash(
            'source "$1"; validate_profile_selection',
            env={"LINGTAI_WORKBENCH_MCP_PROFILE": ""},
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("must be exactly workbench or lingtai", completed.stderr)

    def test_workbench_rejects_even_an_explicit_empty_tuple_field(self) -> None:
        completed = self._run_bash(
            'source "$1"; validate_profile_selection',
            env={"LINGTAI_WORKBENCH_WORKSPACE_ID": ""},
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("workbench MCP profile rejects", completed.stderr)

    def test_lingtai_requires_every_non_empty_tuple_field(self) -> None:
        completed = self._run_bash(
            'source "$1"; validate_profile_selection',
            env={
                "LINGTAI_WORKBENCH_MCP_PROFILE": "lingtai",
                "LINGTAI_WORKBENCH_WORKSPACE_ID": "workspace-123",
                "LINGTAI_WORKBENCH_WORKSPACE_ACTOR_ID": "actor-456",
                "LINGTAI_WORKBENCH_WORKSPACE_GRANT": "",
            },
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("requires non-empty", completed.stderr)

    def test_supported_workspace_dev_membership_variable_is_rejected_when_set(
        self,
    ) -> None:
        completed = self._run_bash(
            'source "$1"; validate_profile_selection',
            env={"LINGTAI_WORKBENCH_WORKSPACE_DEV_MEMBERSHIP": ""},
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("development workspace membership", completed.stderr)
        self.assertIn(
            "LINGTAI_WORKBENCH_WORKSPACE_DEV_MEMBERSHIP", completed.stderr
        )

    def test_lingtai_success_prints_only_stable_tuple_fingerprints(self) -> None:
        workspace = "private-workspace"
        actor = "private-actor"
        now_ms = time.time_ns() // 1_000_000
        canonical_grant_json = json.dumps(
            {
                "actor_id": actor,
                "audience": "nokv-mcp:lingtai",
                "expires_at_unix_ms": now_ms + 60_000,
                "grant_id": "grant-1",
                "issued_at_unix_ms": now_ms - 1_000,
                "issuer": "lingtai-workbench-sync",
                "role": "reader",
                "schema": "nokv.lingtai.workspace_grant.v1",
                "workspace_id": workspace,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        grant = base64.urlsafe_b64encode(canonical_grant_json).decode().rstrip("=")
        completed = self._run_bash(
            'source "$1"; validate_profile_selection; '
            "cache_profile_fingerprints; python3() { return 97; }; "
            "print_locked_profile",
            env={
                "LINGTAI_WORKBENCH_MCP_PROFILE": "lingtai",
                "LINGTAI_WORKBENCH_WORKSPACE_ID": workspace,
                "LINGTAI_WORKBENCH_WORKSPACE_ACTOR_ID": actor,
                "LINGTAI_WORKBENCH_WORKSPACE_GRANT": grant,
            },
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("locked MCP profile: lingtai", completed.stdout)
        for label, value in (("workspace id", workspace), ("workspace actor", actor)):
            digest = hashlib.sha256(value.encode()).hexdigest()
            self.assertIn(f"{label} SHA-256: {digest}", completed.stdout)
            self.assertNotIn(value, completed.stdout)
            self.assertNotIn(value, completed.stderr)
        grant_digest = hashlib.sha256(canonical_grant_json).hexdigest()
        self.assertIn(f"workspace grant SHA-256: {grant_digest}", completed.stdout)
        self.assertNotIn(grant, completed.stdout)
        self.assertNotIn(grant, completed.stderr)

    def test_supported_path_installs_checks_and_rolls_back_profile_atomically(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture_root = root / "fixture-repo"
            fixture_scripts = fixture_root / "scripts" / "lingtai-workbench"
            shutil.copytree(
                SCRIPT_DIR,
                fixture_scripts,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            fixture_up = fixture_scripts / "up.sh"

            workbench_tools = self._profile_tools(None)
            lingtai_tools = self._profile_tools("reader")
            fixture_contract_path = fixture_scripts / "lingtai_contract_schema.json"
            fixture_contract = json.loads(
                fixture_contract_path.read_text(encoding="utf-8")
            )
            fixture_contract["profileDigests"]["reader"] = (
                contract.raw_tool_definitions_sha256(lingtai_tools)
            )
            fixture_contract_path.write_text(
                json.dumps(fixture_contract, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            source = self._make_source(root)
            revision = self._source_revision(source)
            binary = self._make_profile_binary(root, workbench_tools, lingtai_tools)
            build_info = root / "build-info.json"
            runtime.write_build_info(
                build_info,
                runtime.source_identity(source, revision),
                binary,
            )
            project = root / "project"
            agent = project / ".lingtai" / "coordinator"
            agent.mkdir(parents=True)
            (agent / "init.json").write_text('{"mcp": {}}\n', encoding="utf-8")
            retained_data = project / ".lingtai" / "shared-data" / "keep.txt"
            retained_data.parent.mkdir()
            retained_data.write_text("keep\n", encoding="utf-8")

            state_dir = root / "up-state"
            command = r"""
source "$2"
STATE_DIR="$3"
UP_LOCK_DIR="${STATE_DIR}/up.lock"
require_cmd() { :; }
check_runtime_skill() { :; }
ensure_rustfs() { :; }
port_in_use() { return 0; }
ensure_nokv_server() { :; }
main
"""
            workspace = "workspace-supported-path"
            actor = "actor-supported-path"
            grant, canonical_grant = self._canonical_grant(workspace, actor)
            common_env = {
                "NOKV_BIN": str(binary),
                "NOKV_BUILD_INFO": str(build_info),
                "NOKV_REVISION": revision,
                "NOKV_DISTRIBUTION": "source",
                "LINGTAI_WORKBENCH_PROJECT": str(project),
                "LINGTAI_WORKBENCH_AGENT": "coordinator",
                "LINGTAI_WORKBENCH_META_DIR": str(root / "metadata"),
                "LINGTAI_WORKBENCH_RUSTFS_DATA_DIR": str(root / "rustfs"),
            }
            installed = self._run_bash(
                command,
                str(fixture_up),
                str(state_dir),
                env={
                    **common_env,
                    "LINGTAI_WORKBENCH_MCP_PROFILE": "lingtai",
                    "LINGTAI_WORKBENCH_WORKSPACE_ID": workspace,
                    "LINGTAI_WORKBENCH_WORKSPACE_ACTOR_ID": actor,
                    "LINGTAI_WORKBENCH_WORKSPACE_GRANT": grant,
                },
            )

            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertIn("profile: lingtai", installed.stdout)
            self.assertIn("locked MCP profile: lingtai", installed.stdout)
            self.assertIn(
                "workspace grant SHA-256: "
                + hashlib.sha256(canonical_grant).hexdigest(),
                installed.stdout,
            )
            self.assertNotIn(grant, installed.stdout)
            self.assertNotIn(grant, installed.stderr)
            lock_path = agent / "nokv-workbench.lock.json"
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(lock["launch"]["profile"], "lingtai")
            self.assertEqual(
                lock["launch"]["workbench_root"], "/agents/coordinator/wb"
            )
            self.assertEqual(lock["launch"]["workspace_id"], workspace)
            self.assertEqual(lock["launch"]["workspace_actor_id"], actor)
            self.assertEqual(
                lock["launch"]["workspace_grant"]["canonical_sha256"],
                hashlib.sha256(canonical_grant).hexdigest(),
            )
            registry = json.loads(
                (agent / "mcp_registry.jsonl").read_text(encoding="utf-8")
            )
            self.assertIn("--workspace-grant", registry["args"])
            self.assertFalse((agent / ".nokv-workbench.transaction.json").exists())

            target_paths = (
                agent / "mcp_registry.jsonl",
                agent / "init.json",
                lock_path,
            )
            before_implicit = {path: path.read_bytes() for path in target_paths}
            staged_marker = root / "implicit-profile-staged"
            implicit_command = r"""
source "$2"
STATE_DIR="$3"
UP_LOCK_DIR="${STATE_DIR}/up.lock"
STAGED_MARKER="$4"
require_cmd() { :; }
check_runtime_skill() { :; }
prepare_runtime() { touch "${STAGED_MARKER}"; }
main
"""
            implicit = self._run_bash(
                implicit_command,
                str(fixture_up),
                str(state_dir),
                str(staged_marker),
                env=common_env,
                unset=(
                    "LINGTAI_WORKBENCH_MCP_PROFILE",
                    "LINGTAI_WORKBENCH_WORKSPACE_ID",
                    "LINGTAI_WORKBENCH_WORKSPACE_ACTOR_ID",
                    "LINGTAI_WORKBENCH_WORKSPACE_GRANT",
                    "LINGTAI_WORKBENCH_WORKSPACE_DEV_MEMBERSHIP",
                ),
            )

            self.assertEqual(implicit.returncode, 1)
            self.assertIn("--profile workbench", implicit.stderr)
            self.assertFalse(staged_marker.exists())
            self.assertEqual(
                {path: path.read_bytes() for path in target_paths}, before_implicit
            )

            rolled_back = self._run_bash(
                command,
                str(fixture_up),
                str(state_dir),
                env={
                    **common_env,
                    "LINGTAI_WORKBENCH_MCP_PROFILE": "workbench",
                },
                unset=(
                    "LINGTAI_WORKBENCH_WORKSPACE_ID",
                    "LINGTAI_WORKBENCH_WORKSPACE_ACTOR_ID",
                    "LINGTAI_WORKBENCH_WORKSPACE_GRANT",
                    "LINGTAI_WORKBENCH_WORKSPACE_DEV_MEMBERSHIP",
                ),
            )

            self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
            self.assertIn("profile: workbench", rolled_back.stdout)
            self.assertIn("locked MCP profile: workbench", rolled_back.stdout)
            rolled_back_lock = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(rolled_back_lock["launch"]["profile"], "workbench")
            self.assertNotIn("workspace_id", rolled_back_lock["launch"])
            self.assertNotIn("workspace_actor_id", rolled_back_lock["launch"])
            self.assertNotIn("workspace_grant", rolled_back_lock["launch"])
            rolled_back_registry = json.loads(
                (agent / "mcp_registry.jsonl").read_text(encoding="utf-8")
            )
            self.assertNotIn("--workspace-id", rolled_back_registry["args"])
            self.assertNotIn("--workspace-actor-id", rolled_back_registry["args"])
            self.assertNotIn("--workspace-grant", rolled_back_registry["args"])
            self.assertEqual(retained_data.read_text(encoding="utf-8"), "keep\n")
            self.assertFalse((agent / ".nokv-workbench.transaction.json").exists())

    def test_supported_path_unsafe_v1_omission_fails_before_prepare_or_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._make_source(root)
            revision = self._source_revision(source)
            binary = self._make_profile_binary(
                root,
                self._profile_tools(None),
                self._profile_tools("reader"),
            )
            build_info = root / "build-info.json"
            runtime.write_build_info(
                build_info,
                runtime.source_identity(source, revision),
                binary,
            )
            project = root / "project"
            agent = project / ".lingtai" / "coordinator"
            agent.mkdir(parents=True)
            (agent / "init.json").write_text('{"mcp": {}}\n', encoding="utf-8")
            unsafe_bucket = "bucket-{agent_id}"
            installed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "sync_workbench_mcp.py"),
                    "--project",
                    str(project),
                    "--agent",
                    "coordinator",
                    "--nokv-bin",
                    str(binary),
                    "--build-info",
                    str(build_info),
                    "--revision",
                    revision,
                    "--s3-bucket",
                    unsafe_bucket,
                    "--timeout-seconds",
                    "5",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            lock_path = agent / "nokv-workbench.lock.json"
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            lock["schema"] = "nokv.lingtai.workbench_lock.v1"
            del lock["launch"]["template_arg_indices"]
            del lock["launch"]["launch_semantics_sha256"]
            lock_path.write_text(
                json.dumps(lock, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            registry_path = agent / "mcp_registry.jsonl"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            del registry["template_arg_indices"]
            registry_path.write_text(
                json.dumps(registry, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            init_path = agent / "init.json"
            init = json.loads(init_path.read_text(encoding="utf-8"))
            del init["mcp"]["nokv-workbench"]["template_arg_indices"]
            init_path.write_text(
                json.dumps(init, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            target_paths = (registry_path, init_path, lock_path)
            before = {path: path.read_bytes() for path in target_paths}
            prepared = root / "prepared"
            probed = root / "probed"
            state_dir = root / "up-state"
            command = r"""
source "$1"
STATE_DIR="$2"
UP_LOCK_DIR="${STATE_DIR}/up.lock"
PREPARED="$3"
PROBED="$4"
require_cmd() { :; }
check_runtime_skill() { :; }
prepare_runtime() { touch "${PREPARED}"; }
ensure_rustfs() { :; }
port_in_use() { return 0; }
probe_candidate_contract() { touch "${PROBED}"; }
ensure_nokv_server() { :; }
main
"""
            completed = self._run_bash(
                command,
                str(state_dir),
                str(prepared),
                str(probed),
                env={
                    "LINGTAI_WORKBENCH_PROJECT": str(project),
                    "LINGTAI_WORKBENCH_AGENT": "coordinator",
                },
                unset=(
                    "LINGTAI_WORKBENCH_SERVER_BIND",
                    "LINGTAI_WORKBENCH_OBJECT_BACKEND",
                    "LINGTAI_WORKBENCH_S3_ENDPOINT",
                    "LINGTAI_WORKBENCH_S3_BUCKET",
                    "LINGTAI_WORKBENCH_ROOT",
                    "LINGTAI_WORKBENCH_MCP_PROFILE",
                ),
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn("--s3-bucket", completed.stderr)
            self.assertNotIn(unsafe_bucket, completed.stderr)
            self.assertFalse(prepared.exists())
            self.assertFalse(probed.exists())
            self.assertEqual(
                {path: path.read_bytes() for path in target_paths},
                before,
            )

    def test_supported_path_token_project_fails_before_prepare_or_candidate_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project-{agent_address}"
            agent = project / ".lingtai" / "coordinator"
            agent.mkdir(parents=True)
            init_path = agent / "init.json"
            init_path.write_text('{"mcp": {}}\n', encoding="utf-8")
            init_before = init_path.read_bytes()
            prepared = root / "prepared"
            probed = root / "probed"
            state_dir = root / "up-state"
            command = r"""
source "$1"
STATE_DIR="$2"
UP_LOCK_DIR="${STATE_DIR}/up.lock"
PREPARED="$3"
PROBED="$4"
require_cmd() { :; }
check_runtime_skill() { :; }
prepare_runtime() { touch "${PREPARED}"; }
ensure_rustfs() { :; }
port_in_use() { return 0; }
probe_candidate_contract() { touch "${PROBED}"; }
ensure_nokv_server() { :; }
main
"""
            completed = self._run_bash(
                command,
                str(state_dir),
                str(prepared),
                str(probed),
                env={
                    "LINGTAI_WORKBENCH_PROJECT": str(project),
                    "LINGTAI_WORKBENCH_AGENT": "coordinator",
                },
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn("project path must be a literal path", completed.stderr)
            self.assertNotIn(str(project), completed.stderr)
            self.assertFalse(prepared.exists())
            self.assertFalse(probed.exists())
            self.assertEqual(init_path.read_bytes(), init_before)
            self.assertFalse((agent / "mcp_registry.jsonl").exists())
            self.assertFalse((agent / "nokv-workbench.lock.json").exists())

    def test_post_commit_check_failure_reports_committed_not_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            agent = project / ".lingtai" / "coordinator"
            agent.mkdir(parents=True)
            (agent / "init.json").write_text('{"mcp": {}}\n', encoding="utf-8")
            state_dir = root / "state"
            committed_marker = root / "committed"
            command = r"""
source "$1"
STATE_DIR="$2"
UP_LOCK_DIR="${STATE_DIR}/up.lock"
COMMITTED_MARKER="$3"
require_cmd() { :; }
resolve_agent_once() {
  PINNED_AGENT=coordinator
  PINNED_AGENT_IDENTITY=agent-identity-token
}
check_runtime_skill() { :; }
preflight_agent() { :; }
prepare_runtime() { NOKV_BIN=/immutable/nokv; }
ensure_rustfs() { :; }
port_in_use() { return 1; }
ensure_nokv_server() { :; }
sync_agent() { touch "${COMMITTED_MARKER}"; }
check_agent() { return 41; }
main
"""
            completed = self._run_bash(
                command,
                str(state_dir),
                str(committed_marker),
                env={
                    "LINGTAI_WORKBENCH_PROJECT": str(project),
                    "LINGTAI_WORKBENCH_MCP_PROFILE": "workbench",
                },
            )

            self.assertEqual(completed.returncode, 1)
            self.assertTrue(committed_marker.exists())
            self.assertIn(
                "configuration committed but post-commit verification failed; "
                "not rolled back",
                completed.stderr,
            )
            self.assertNotIn("LingTai workbench is ready", completed.stdout)
            self.assertNotIn("locked MCP profile", completed.stdout)
            self.assertFalse((state_dir / "up.lock").exists())

    def test_agent_replacement_between_preflight_and_sync_fails_before_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            agent = project / ".lingtai" / "coordinator"
            agent.mkdir(parents=True)
            (agent / "init.json").write_text('{"original": true}\n', encoding="utf-8")
            moved = agent.with_name("coordinator-original")
            state_dir = root / "state"
            mutation_marker = agent / "replacement-mutated"
            command = r"""
source "$1"
STATE_DIR="$2"
UP_LOCK_DIR="${STATE_DIR}/up.lock"
PROJECT="$3"
MOVED="$4"
MUTATION_MARKER="$5"
require_cmd() { :; }
check_runtime_skill() { :; }
preflight_agent() {
  mv "${PROJECT}/.lingtai/${PINNED_AGENT}" "${MOVED}"
  mkdir "${PROJECT}/.lingtai/${PINNED_AGENT}"
  printf '%s\n' '{"replacement": true}' >"${PROJECT}/.lingtai/${PINNED_AGENT}/init.json"
}
prepare_runtime() { NOKV_BIN=/immutable/nokv; }
ensure_rustfs() { :; }
port_in_use() { return 1; }
ensure_nokv_server() { :; }
sync_agent() {
  python3 "${SCRIPT_DIR}/sync_workbench_mcp.py" \
    --project "$1" \
    --agent "${PINNED_AGENT}" \
    --orchestration-agent-identity "${PINNED_AGENT_IDENTITY}" \
    --preflight-only || return
  touch "${MUTATION_MARKER}"
}
main
"""
            completed = self._run_bash(
                command,
                str(state_dir),
                str(project),
                str(moved),
                str(mutation_marker),
                env={
                    "LINGTAI_WORKBENCH_PROJECT": str(project),
                    "LINGTAI_WORKBENCH_AGENT": "coordinator",
                },
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn("Agent directory identity changed", completed.stderr)
            self.assertFalse(mutation_marker.exists())
            self.assertEqual(
                (agent / "init.json").read_text(encoding="utf-8"),
                '{"replacement": true}\n',
            )
            self.assertEqual(
                (moved / "init.json").read_text(encoding="utf-8"),
                '{"original": true}\n',
            )


if __name__ == "__main__":
    unittest.main()
