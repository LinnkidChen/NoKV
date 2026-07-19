#!/usr/bin/env python3
# Copyright 2024-2026 The NoKV Authors.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import nokv_runtime as runtime  # noqa: E402
import sync_workbench_mcp as sync  # noqa: E402
import workbench_contract as contract  # noqa: E402


HOLT_REVISION = "b" * 40
HOLT_CHECKSUM = "d" * 64


def tool_surface(*, include_restore: bool = True) -> list[dict]:
    return [
        {
            "name": name,
            "description": f"description for {name}",
            "inputSchema": copy.deepcopy(contract.FROZEN_INPUT_SCHEMAS[name]),
        }
        for name in contract.FROZEN_TOOL_ORDER
        if include_restore or name != contract.RESTORE_TOOL
    ]


def lingtai_tool_surface(role: str) -> list[dict]:
    tools = tool_surface()
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


class SyncWorkbenchMcpTest(unittest.TestCase):
    def canonical_grant(
        self,
        *,
        workspace_id: str = "team-alpha",
        actor_id: str = "agent-7",
        role: str = "writer",
        grant_id: str = "grant_1",
        issued_at_unix_ms: int | None = None,
        expires_at_unix_ms: int | None = None,
    ) -> str:
        now = time.time_ns() // 1_000_000
        fields = {
            "schema": "nokv.lingtai.workspace_grant.v1",
            "grant_id": grant_id,
            "issuer": "lingtai-workbench-sync",
            "audience": "nokv-mcp:lingtai",
            "workspace_id": workspace_id,
            "actor_id": actor_id,
            "role": role,
            "issued_at_unix_ms": issued_at_unix_ms or now - 1_000,
            "expires_at_unix_ms": expires_at_unix_ms or now + 60_000,
        }
        canonical = json.dumps(
            fields,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(canonical).decode("ascii").rstrip("=")

    def make_source(self, root: Path, *, lock_revision: str = HOLT_REVISION) -> Path:
        source = root / "source"
        (source / "crates" / "nokv").mkdir(parents=True)
        (source / "Cargo.toml").write_text(
            "[workspace]\n"
            'members = ["crates/nokv"]\n'
            "[workspace.dependencies]\n"
            f'holt = {{ git = "https://github.com/NoKV-Lab/holt.git", rev = "{HOLT_REVISION}" }}\n',
            encoding="utf-8",
        )
        (source / "Cargo.lock").write_text(
            "version = 4\n\n"
            "[[package]]\n"
            'name = "holt"\n'
            'version = "0.8.1"\n'
            'source = "git+https://github.com/NoKV-Lab/holt.git'
            f'?rev={lock_revision}#{lock_revision}"\n',
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

    def make_registry_source(self, root: Path) -> Path:
        source = root / "registry-source"
        (source / "crates" / "nokv").mkdir(parents=True)
        (source / "Cargo.toml").write_text(
            "[workspace.dependencies]\n"
            'holt = { version = "=0.8.2", default-features = false }\n',
            encoding="utf-8",
        )
        (source / "Cargo.lock").write_text(
            "version = 4\n\n"
            "[[package]]\n"
            'name = "holt"\n'
            'version = "0.8.2"\n'
            f'source = "{runtime.CRATES_IO_REGISTRY}"\n'
            f'checksum = "{HOLT_CHECKSUM}"\n',
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
                "registry fixture",
            ],
            cwd=source,
            check=True,
        )
        return source

    def source_revision(self, source: Path) -> str:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True
        ).strip()

    def make_project(self, root: Path) -> tuple[Path, Path]:
        project = root / "project"
        agent = project / ".lingtai" / "coordinator"
        agent.mkdir(parents=True)
        (agent / "init.json").write_text('{"mcp": {}}\n', encoding="utf-8")
        return project, agent

    def make_binary(self, root: Path, tools: list[dict], name: str = "nokv") -> Path:
        binary = root / name
        binary.parent.mkdir(parents=True, exist_ok=True)
        response = {"jsonrpc": "2.0", "id": 1, "result": {"tools": tools}}
        binary.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import sys\n"
            "for line in sys.stdin:\n"
            "    json.loads(line)\n"
            f"    print(json.dumps({response!r}, separators=(',', ':')))\n",
            encoding="utf-8",
        )
        os.chmod(binary, 0o755)
        return binary

    def run_sync(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = sync.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def rewrite_installed_state_as_v1(self, agent: Path) -> dict:
        lock_path = agent / sync.LOCK_NAME
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["schema"] = sync.LOCK_SCHEMA_V1
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
        return lock

    def sync_args(
        self,
        project: Path,
        source: Path,
        binary: Path,
        *,
        profile: str = "workbench",
        workspace_id: str = "team-alpha",
        actor_id: str = "agent-7",
        grant: str | None = None,
        role: str = "writer",
    ) -> tuple[str, ...]:
        build_info = binary.with_name(f"{binary.name}.build-info.json")
        runtime.write_build_info(
            build_info,
            runtime.source_identity(source, self.source_revision(source)),
            binary,
        )
        args = (
            "--project",
            str(project),
            "--agent",
            "coordinator",
            "--nokv-bin",
            str(binary),
            "--build-info",
            str(build_info),
            "--revision",
            self.source_revision(source),
            "--timeout-seconds",
            "5",
        )
        if profile == "lingtai":
            return (
                *args,
                "--profile",
                profile,
                "--workspace-id",
                workspace_id,
                "--workspace-actor-id",
                actor_id,
                "--workspace-grant",
                grant
                or self.canonical_grant(
                    workspace_id=workspace_id,
                    actor_id=actor_id,
                    role=role,
                ),
            )
        if profile != "workbench":
            return (*args, "--profile", profile)
        return args

    def test_sync_stages_gates_locks_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            binary = self.make_binary(root, tool_surface())

            first = self.run_sync(*self.sync_args(project, source, binary))
            self.assertEqual(first[0], 0, first[2])
            self.assertIn(f"holt_revision: {HOLT_REVISION}", first[1])
            self.assertNotIn("holt_registry:", first[1])
            self.assertNotIn("holt_checksum_sha256:", first[1])
            lock_path = agent / sync.LOCK_NAME
            lock_before = lock_path.read_bytes()
            init_before = (agent / "init.json").read_bytes()
            registry_before = (agent / "mcp_registry.jsonl").read_bytes()

            lock = json.loads(lock_before)
            registry = json.loads(registry_before)
            init = json.loads(init_before)
            command = Path(lock["artifact"]["command"])
            self.assertTrue(command.is_file())
            self.assertIn(self.source_revision(source), command.parts)
            self.assertEqual(runtime.sha256_file(command), lock["artifact"]["sha256"])
            self.assertEqual(lock["source"]["holt_git_commit"], HOLT_REVISION)
            self.assertEqual(lock["contract"]["tool_count"], 18)
            self.assertEqual(
                lock["contract"]["tool_order"], list(contract.FROZEN_TOOL_ORDER)
            )
            self.assertEqual(
                lock["contract"]["tool_order_sha256"],
                contract.expected_contract_evidence()["tool_order_sha256"],
            )
            self.assertEqual(
                lock["contract"]["contract_sha256"],
                contract.expected_contract_evidence()["contract_sha256"],
            )
            self.assertEqual(lock["launch"]["workbench_root"], "/agents/coordinator/wb")
            self.assertEqual(lock["launch"]["profile"], "workbench")
            self.assertEqual(lock["schema"], sync.LOCK_SCHEMA_V2)
            root_index = registry["args"].index("--workbench-root") + 1
            self.assertEqual(registry["template_arg_indices"], [root_index])
            self.assertEqual(
                init["mcp"]["nokv-workbench"]["template_arg_indices"],
                [root_index],
            )
            self.assertEqual(
                lock["launch"]["template_arg_indices"], [root_index]
            )
            self.assertEqual(
                lock["launch"]["launch_semantics_sha256"],
                contract.json_sha256(
                    {
                        "args": registry["args"],
                        "template_arg_indices": [root_index],
                    }
                ),
            )
            self.assertNotIn("workspace_id", lock["launch"])
            self.assertNotIn("workspace_actor_id", lock["launch"])
            self.assertNotIn("workspace_grant", lock["launch"])

            second = self.run_sync(*self.sync_args(project, source, binary))
            self.assertEqual(second[0], 0, second[2])
            self.assertEqual(lock_path.read_bytes(), lock_before)
            self.assertEqual((agent / "init.json").read_bytes(), init_before)
            self.assertEqual(
                (agent / "mcp_registry.jsonl").read_bytes(), registry_before
            )
            self.assertIn("already synchronized", second[1])

    def test_token_bearing_project_path_fails_before_candidate_or_agent_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project = root / "project-{agent_id}"
            agent = project / ".lingtai" / "coordinator"
            agent.mkdir(parents=True)
            init_path = agent / "init.json"
            init_path.write_text('{"mcp": {}}\n', encoding="utf-8")
            init_before = init_path.read_bytes()
            binary = self.make_binary(root, tool_surface())
            args = self.sync_args(project, source, binary)

            with (
                mock.patch.object(sync, "discover_nokv_binary") as discovery,
                mock.patch.object(sync, "raw_tools_list") as live_probe,
            ):
                rejected = self.run_sync(*args)

            self.assertEqual(rejected[0], 1)
            self.assertIn("project path must be a literal path", rejected[2])
            self.assertNotIn(str(project), rejected[2])
            discovery.assert_not_called()
            live_probe.assert_not_called()
            self.assertEqual(init_path.read_bytes(), init_before)
            self.assertFalse((agent / "mcp_registry.jsonl").exists())
            self.assertFalse((agent / sync.LOCK_NAME).exists())
            self.assertFalse((project / ".lingtai" / "runtime").exists())

    def test_check_rejects_lexical_token_command_before_symlink_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            binary = self.make_binary(root, tool_surface())
            installed = self.run_sync(*self.sync_args(project, source, binary))
            self.assertEqual(installed[0], 0, installed[2])

            lock_path = agent / sync.LOCK_NAME
            registry_path = agent / "mcp_registry.jsonl"
            init_path = agent / "init.json"
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            safe_command = Path(lock["artifact"]["command"])
            token_parent = root / "private-runtime-{agent_id}"
            token_parent.mkdir()
            token_command = token_parent / "nokv"
            token_command.symlink_to(safe_command)

            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["command"] = str(token_command)
            registry_path.write_text(
                json.dumps(registry, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            init = json.loads(init_path.read_text(encoding="utf-8"))
            init["mcp"][registry["name"]]["command"] = str(token_command)
            init_path.write_text(
                json.dumps(init, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            lock["artifact"]["command"] = str(token_command)
            lock_path.write_text(
                json.dumps(lock, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            managed_paths = (registry_path, init_path, lock_path)
            before = {path: path.read_bytes() for path in managed_paths}

            with mock.patch.object(sync, "raw_tools_list") as live_probe:
                rejected = self.run_sync(
                    "--project",
                    str(project),
                    "--agent",
                    "coordinator",
                    "--check",
                )

            self.assertEqual(rejected[0], 1)
            self.assertIn("locked MCP command path must be a literal path", rejected[2])
            self.assertNotIn(str(token_command), rejected[2])
            self.assertNotIn(str(safe_command), rejected[2])
            live_probe.assert_not_called()
            self.assertEqual(
                {path: path.read_bytes() for path in managed_paths},
                before,
            )

    def test_v1_lock_remains_checkable_then_normal_sync_upgrades_to_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            binary = self.make_binary(root, tool_surface())

            installed = self.run_sync(*self.sync_args(project, source, binary))
            self.assertEqual(installed[0], 0, installed[2])
            lock_path = agent / sync.LOCK_NAME
            self.rewrite_installed_state_as_v1(agent)
            registry_path = agent / "mcp_registry.jsonl"
            v1_lock_before = lock_path.read_bytes()

            checked = self.run_sync(
                "--project",
                str(project),
                "--agent",
                "coordinator",
                "--check",
            )

            self.assertEqual(checked[0], 0, checked[2])
            self.assertEqual(lock_path.read_bytes(), v1_lock_before)

            upgraded = self.run_sync(*self.sync_args(project, source, binary))

            self.assertEqual(upgraded[0], 0, upgraded[2])
            upgraded_lock = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(upgraded_lock["schema"], sync.LOCK_SCHEMA_V2)
            self.assertIn("template_arg_indices", upgraded_lock["launch"])
            self.assertIn("launch_semantics_sha256", upgraded_lock["launch"])
            self.assertIn(
                "template_arg_indices",
                json.loads(registry_path.read_text(encoding="utf-8")),
            )

    def test_v1_lingtai_identity_migration_requires_concrete_tuple_and_new_grant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            tools = lingtai_tool_surface("writer")
            binary = self.make_binary(root, tools)
            workspace_id = "team-{agent_id}"
            actor_id = "actor-{agent_dir}"
            grant = self.canonical_grant(
                workspace_id=workspace_id,
                actor_id=actor_id,
                role="writer",
            )
            args = self.sync_args(
                project,
                source,
                binary,
                profile="lingtai",
                workspace_id=workspace_id,
                actor_id=actor_id,
                grant=grant,
            )
            digest = contract.raw_tool_definitions_sha256(tools)
            with mock.patch.dict(
                contract.FROZEN_LINGTAI_PROFILE_DIGESTS,
                {"writer": digest},
            ):
                installed = self.run_sync(*args)
                self.assertEqual(installed[0], 0, installed[2])
                self.rewrite_installed_state_as_v1(agent)

                with mock.patch.object(sync, "raw_tools_list") as live_probe:
                    rejected = self.run_sync(
                        "--project",
                        str(project),
                        "--agent",
                        "coordinator",
                        "--check",
                    )

                self.assertEqual(rejected[0], 1)
                self.assertIn("legacy v1", rejected[2])
                self.assertIn("concrete workspace/actor tuple", rejected[2])
                self.assertIn("canonical reissued grant", rejected[2])
                self.assertNotIn(workspace_id, rejected[2])
                self.assertNotIn(actor_id, rejected[2])
                self.assertNotIn(grant, rejected[2])
                live_probe.assert_not_called()

                state_paths = (
                    agent / "mcp_registry.jsonl",
                    agent / "init.json",
                    agent / sync.LOCK_NAME,
                )
                before_rejected_sync = {
                    path: path.read_bytes() for path in state_paths
                }
                with mock.patch.object(sync, "raw_tools_list") as omitted_probe:
                    omitted = self.run_sync(
                        *self.sync_args(project, source, binary)
                    )

                self.assertEqual(omitted[0], 1)
                self.assertIn("--profile workbench", omitted[2])
                self.assertNotIn(workspace_id, omitted[2])
                self.assertNotIn(actor_id, omitted[2])
                self.assertNotIn(grant, omitted[2])
                omitted_probe.assert_not_called()
                self.assertEqual(
                    {path: path.read_bytes() for path in state_paths},
                    before_rejected_sync,
                )
                with mock.patch.object(sync, "raw_tools_list") as migration_probe:
                    unchanged = self.run_sync(*args)

                self.assertEqual(unchanged[0], 1)
                self.assertIn("identity migration refused", unchanged[2])
                self.assertIn("canonical reissued workspace grant", unchanged[2])
                self.assertNotIn(workspace_id, unchanged[2])
                self.assertNotIn(actor_id, unchanged[2])
                self.assertNotIn(grant, unchanged[2])
                migration_probe.assert_not_called()
                self.assertEqual(
                    {path: path.read_bytes() for path in state_paths},
                    before_rejected_sync,
                )

                replacement_workspace_id = "team-concrete"
                replacement_actor_id = "actor-concrete"
                replacement_grant = self.canonical_grant(
                    workspace_id=replacement_workspace_id,
                    actor_id=replacement_actor_id,
                    role="writer",
                    grant_id="grant_reissued",
                )
                replacement_args = self.sync_args(
                    project,
                    source,
                    binary,
                    profile="lingtai",
                    workspace_id=replacement_workspace_id,
                    actor_id=replacement_actor_id,
                    grant=replacement_grant,
                )
                upgraded = self.run_sync(*replacement_args)
                checked = self.run_sync(
                    "--project",
                    str(project),
                    "--agent",
                    "coordinator",
                    "--check",
                )

            self.assertEqual(upgraded[0], 0, upgraded[2])
            self.assertEqual(checked[0], 0, checked[2])
            lock = json.loads(
                (agent / sync.LOCK_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(lock["schema"], sync.LOCK_SCHEMA_V2)
            registry = json.loads(
                (agent / "mcp_registry.jsonl").read_text(encoding="utf-8")
            )
            root_index = registry["args"].index("--workbench-root") + 1
            workspace_index = registry["args"].index("--workspace-id") + 1
            actor_index = registry["args"].index("--workspace-actor-id") + 1
            self.assertEqual(registry["template_arg_indices"], [root_index])
            self.assertNotIn(workspace_index, registry["template_arg_indices"])
            self.assertNotIn(actor_index, registry["template_arg_indices"])
            self.assertEqual(
                registry["args"][workspace_index], replacement_workspace_id
            )
            self.assertEqual(registry["args"][actor_index], replacement_actor_id)
            self.assertIn(replacement_grant, registry["args"])
            self.assertNotIn(grant, registry["args"])

    def test_unsafe_v1_lingtai_identity_allows_only_explicit_workbench_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            lingtai_tools = lingtai_tool_surface("reader")
            lingtai_binary = self.make_binary(root, lingtai_tools, "nokv-lingtai")
            workspace_id = "team-{agent_id}"
            actor_id = "actor-{agent_dir}"
            grant = self.canonical_grant(
                workspace_id=workspace_id,
                actor_id=actor_id,
                role="reader",
            )
            lingtai_args = self.sync_args(
                project,
                source,
                lingtai_binary,
                profile="lingtai",
                workspace_id=workspace_id,
                actor_id=actor_id,
                grant=grant,
            )
            digest = contract.raw_tool_definitions_sha256(lingtai_tools)
            with mock.patch.dict(
                contract.FROZEN_LINGTAI_PROFILE_DIGESTS,
                {"reader": digest},
            ):
                installed = self.run_sync(*lingtai_args)
            self.assertEqual(installed[0], 0, installed[2])
            self.rewrite_installed_state_as_v1(agent)
            state_paths = (
                agent / "mcp_registry.jsonl",
                agent / "init.json",
                agent / sync.LOCK_NAME,
            )
            before = {path: path.read_bytes() for path in state_paths}
            workbench_binary = self.make_binary(
                root,
                tool_surface(),
                "nokv-workbench",
            )
            implicit_args = self.sync_args(project, source, workbench_binary)

            with mock.patch.object(sync, "raw_tools_list") as implicit_probe:
                implicit = self.run_sync(*implicit_args)

            self.assertEqual(implicit[0], 1)
            self.assertIn("explicit --profile workbench", implicit[2])
            self.assertNotIn(workspace_id, implicit[2])
            self.assertNotIn(actor_id, implicit[2])
            self.assertNotIn(grant, implicit[2])
            implicit_probe.assert_not_called()
            self.assertEqual(
                {path: path.read_bytes() for path in state_paths},
                before,
            )

            rolled_back = self.run_sync(*implicit_args, "--profile", "workbench")
            checked = self.run_sync(
                "--project",
                str(project),
                "--agent",
                "coordinator",
                "--check",
            )

            self.assertEqual(rolled_back[0], 0, rolled_back[2])
            self.assertEqual(checked[0], 0, checked[2])
            lock = json.loads(
                (agent / sync.LOCK_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(lock["schema"], sync.LOCK_SCHEMA_V2)
            self.assertEqual(lock["launch"]["profile"], "workbench")
            self.assertNotIn("workspace_id", lock["launch"])
            self.assertNotIn("workspace_actor_id", lock["launch"])
            self.assertNotIn("workspace_grant", lock["launch"])

    def test_v1_workbench_migration_requires_concrete_bucket_and_endpoint(self):
        cases = (
            (
                "--s3-bucket",
                "bucket-{agent_address}",
                sync.installer.DEFAULT_BUCKET,
            ),
            (
                "--s3-endpoint",
                "http://{agent_address}:9000",
                sync.installer.DEFAULT_ENDPOINT,
            ),
        )
        for option, unsafe_value, concrete_value in cases:
            with self.subTest(option=option), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = self.make_source(root)
                project, agent = self.make_project(root)
                binary = self.make_binary(root, tool_surface())
                unsafe_args = (
                    *self.sync_args(project, source, binary),
                    option,
                    unsafe_value,
                )
                installed = self.run_sync(*unsafe_args)
                self.assertEqual(installed[0], 0, installed[2])
                self.rewrite_installed_state_as_v1(agent)
                state_paths = (
                    agent / "mcp_registry.jsonl",
                    agent / "init.json",
                    agent / sync.LOCK_NAME,
                )
                before_rejected_sync = {
                    path: path.read_bytes() for path in state_paths
                }

                with mock.patch.object(sync, "raw_tools_list") as omitted_probe:
                    omitted = self.run_sync(
                        *self.sync_args(project, source, binary)
                    )
                self.assertEqual(omitted[0], 1)
                self.assertIn("explicit reviewed replacement", omitted[2])
                self.assertIn(option, omitted[2])
                self.assertNotIn(unsafe_value, omitted[2])
                omitted_probe.assert_not_called()
                self.assertEqual(
                    {path: path.read_bytes() for path in state_paths},
                    before_rejected_sync,
                )

                with mock.patch.object(sync, "raw_tools_list") as empty_probe:
                    empty_preflight = self.run_sync(
                        "--project",
                        str(project),
                        "--agent",
                        "coordinator",
                        "--preflight-only",
                        option,
                        "",
                    )
                    empty_sync = self.run_sync(
                        *self.sync_args(project, source, binary),
                        option,
                        "",
                    )

                for rejected in (empty_preflight, empty_sync):
                    self.assertEqual(rejected[0], 1)
                    self.assertIn(
                        "non-empty concrete literal replacements",
                        rejected[2],
                    )
                    self.assertIn(option, rejected[2])
                    self.assertNotIn(unsafe_value, rejected[2])
                empty_probe.assert_not_called()
                self.assertEqual(
                    {path: path.read_bytes() for path in state_paths},
                    before_rejected_sync,
                )

                preflight = self.run_sync(
                    "--project",
                    str(project),
                    "--agent",
                    "coordinator",
                    "--preflight-only",
                    option,
                    unsafe_value,
                )
                checked = self.run_sync(
                    "--project",
                    str(project),
                    "--agent",
                    "coordinator",
                    "--check",
                )
                with mock.patch.object(sync, "raw_tools_list") as migration_probe:
                    unchanged = self.run_sync(*unsafe_args)

                self.assertEqual(preflight[0], 1)
                self.assertIn("launch migration refused", preflight[2])
                self.assertNotIn(unsafe_value, preflight[2])
                self.assertEqual(checked[0], 1)
                self.assertIn("review and replace", checked[2])
                self.assertNotIn(unsafe_value, checked[2])
                self.assertEqual(unchanged[0], 1)
                self.assertIn("launch migration refused", unchanged[2])
                self.assertIn("concrete literal replacements", unchanged[2])
                self.assertNotIn(unsafe_value, unchanged[2])
                migration_probe.assert_not_called()
                self.assertEqual(
                    {path: path.read_bytes() for path in state_paths},
                    before_rejected_sync,
                )

                if option == "--s3-endpoint":
                    replacement_args = (
                        *self.sync_args(project, source, binary),
                        f"{option}={concrete_value}",
                    )
                else:
                    replacement_args = (
                        *self.sync_args(project, source, binary),
                        option,
                        concrete_value,
                    )
                upgraded = self.run_sync(*replacement_args)
                checked_v2 = self.run_sync(
                    "--project",
                    str(project),
                    "--agent",
                    "coordinator",
                    "--check",
                )

                self.assertEqual(upgraded[0], 0, upgraded[2])
                self.assertEqual(checked_v2[0], 0, checked_v2[2])
                lock = json.loads(
                    (agent / sync.LOCK_NAME).read_text(encoding="utf-8")
                )
                self.assertEqual(lock["schema"], sync.LOCK_SCHEMA_V2)
                registry = json.loads(
                    (agent / "mcp_registry.jsonl").read_text(encoding="utf-8")
                )
                self.assertIn(concrete_value, registry["args"])
                self.assertNotIn(unsafe_value, registry["args"])

    def test_v1_migration_requires_provenance_for_every_unsafe_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            binary = self.make_binary(root, tool_surface())
            unsafe_bucket = "bucket-{agent_id}"
            unsafe_endpoint = "http://{agent_address}:9000"
            unsafe_args = (
                *self.sync_args(project, source, binary),
                "--s3-bucket",
                unsafe_bucket,
                "--s3-endpoint",
                unsafe_endpoint,
            )
            installed = self.run_sync(*unsafe_args)
            self.assertEqual(installed[0], 0, installed[2])
            self.rewrite_installed_state_as_v1(agent)
            state_paths = (
                agent / "mcp_registry.jsonl",
                agent / "init.json",
                agent / sync.LOCK_NAME,
            )
            before = {path: path.read_bytes() for path in state_paths}
            partial_args = (
                *self.sync_args(project, source, binary),
                "--s3-bucket",
                sync.installer.DEFAULT_BUCKET,
            )

            with mock.patch.object(sync, "raw_tools_list") as migration_probe:
                partial = self.run_sync(*partial_args)

            self.assertEqual(partial[0], 1)
            self.assertIn("--s3-endpoint", partial[2])
            self.assertNotIn(unsafe_bucket, partial[2])
            self.assertNotIn(unsafe_endpoint, partial[2])
            migration_probe.assert_not_called()
            self.assertEqual(
                {path: path.read_bytes() for path in state_paths},
                before,
            )

            complete_args = (
                *partial_args,
                f"--s3-endpoint={sync.installer.DEFAULT_ENDPOINT}",
            )
            upgraded = self.run_sync(*complete_args)
            checked = self.run_sync(
                "--project",
                str(project),
                "--agent",
                "coordinator",
                "--check",
            )

            self.assertEqual(upgraded[0], 0, upgraded[2])
            self.assertEqual(checked[0], 0, checked[2])
            lock = json.loads(
                (agent / sync.LOCK_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(lock["schema"], sync.LOCK_SCHEMA_V2)

    def test_v2_check_rejects_template_index_and_semantics_digest_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            binary = self.make_binary(root, tool_surface())
            installed = self.run_sync(*self.sync_args(project, source, binary))
            self.assertEqual(installed[0], 0, installed[2])
            lock_path = agent / sync.LOCK_NAME
            original = json.loads(lock_path.read_text(encoding="utf-8"))

            tampered_indices = copy.deepcopy(original)
            tampered_indices["launch"]["template_arg_indices"] = []
            lock_path.write_text(
                json.dumps(tampered_indices, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            rejected_indices = self.run_sync(
                "--project",
                str(project),
                "--agent",
                "coordinator",
                "--check",
            )

            self.assertEqual(rejected_indices[0], 1)
            self.assertIn("template_arg_indices", rejected_indices[2])

            tampered_digest = copy.deepcopy(original)
            tampered_digest["launch"]["launch_semantics_sha256"] = "f" * 64
            lock_path.write_text(
                json.dumps(tampered_digest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            rejected_digest = self.run_sync(
                "--project",
                str(project),
                "--agent",
                "coordinator",
                "--check",
            )

            self.assertEqual(rejected_digest[0], 1)
            self.assertIn("launch semantics", rejected_digest[2])

    def test_registry_holt_v2_output_uses_registry_and_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_registry_source(root)
            project, agent = self.make_project(root)
            binary = self.make_binary(root, tool_surface())

            installed = self.run_sync(*self.sync_args(project, source, binary))
            checked = self.run_sync(
                "--project",
                str(project),
                "--agent",
                "coordinator",
                "--check",
            )

            self.assertEqual(installed[0], 0, installed[2])
            self.assertEqual(checked[0], 0, checked[2])
            self.assertIn(
                f"holt_registry: {runtime.CRATES_IO_REGISTRY}", installed[1]
            )
            self.assertIn(
                f"holt_checksum_sha256: {HOLT_CHECKSUM}", installed[1]
            )
            self.assertNotIn("holt_revision:", installed[1])
            self.assertNotIn("None", installed[1])
            lock = json.loads((agent / sync.LOCK_NAME).read_text(encoding="utf-8"))
            self.assertEqual(lock["source"]["schema"], runtime.BUILD_INFO_SCHEMA_V2)
            self.assertEqual(
                lock["source"]["holt_registry"], runtime.CRATES_IO_REGISTRY
            )
            self.assertEqual(
                lock["source"]["holt_checksum_sha256"], HOLT_CHECKSUM
            )
            self.assertNotIn("holt_git_commit", lock["source"])

    def test_missing_restore_fails_before_agent_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            binary = self.make_binary(root, tool_surface(include_restore=False))
            init_before = (agent / "init.json").read_bytes()

            result = self.run_sync(*self.sync_args(project, source, binary))

            self.assertEqual(result[0], 1)
            self.assertIn("missing=['workbench_restore']", result[2])
            self.assertEqual((agent / "init.json").read_bytes(), init_before)
            self.assertFalse((agent / "mcp_registry.jsonl").exists())
            self.assertFalse((agent / sync.LOCK_NAME).exists())

    def test_wrong_tools_list_order_fails_before_agent_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            tools = tool_surface()
            tools[0], tools[1] = tools[1], tools[0]
            binary = self.make_binary(root, tools)
            init_before = (agent / "init.json").read_bytes()

            result = self.run_sync(*self.sync_args(project, source, binary))

            self.assertEqual(result[0], 1)
            self.assertIn("tools/list order differs", result[2])
            self.assertEqual((agent / "init.json").read_bytes(), init_before)
            self.assertFalse((agent / "mcp_registry.jsonl").exists())
            self.assertFalse((agent / sync.LOCK_NAME).exists())

    def test_probe_only_never_changes_agent_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            installed_binary = self.make_binary(root, tool_surface(), "nokv-a")
            installed = self.run_sync(
                *self.sync_args(project, source, installed_binary)
            )
            self.assertEqual(installed[0], 0, installed[2])
            paths = (
                agent / "mcp_registry.jsonl",
                agent / "init.json",
                agent / sync.LOCK_NAME,
            )
            before = {path: path.read_bytes() for path in paths}

            candidate_tools = tool_surface()
            for tool in candidate_tools:
                tool["description"] += " candidate"
            candidate = self.make_binary(root, candidate_tools, "nokv-b")
            accepted = self.run_sync(
                *self.sync_args(project, source, candidate), "--probe-only"
            )

            self.assertEqual(accepted[0], 0, accepted[2])
            self.assertIn("live_contract_valid: true", accepted[1])
            self.assertEqual({path: path.read_bytes() for path in paths}, before)

            rejected_candidate = self.make_binary(
                root, tool_surface(include_restore=False), "nokv-c"
            )
            rejected = self.run_sync(
                *self.sync_args(project, source, rejected_candidate), "--probe-only"
            )

            self.assertEqual(rejected[0], 1)
            self.assertIn("missing=['workbench_restore']", rejected[2])
            self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_contract_identity_change_requires_explicit_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            binary = self.make_binary(root, tool_surface())
            first = self.run_sync(*self.sync_args(project, source, binary))
            self.assertEqual(first[0], 0, first[2])
            lock_path = agent / sync.LOCK_NAME
            old_lock = json.loads(lock_path.read_text(encoding="utf-8"))
            old_lock["contract"]["contract_sha256"] = "c" * 64
            lock_path.write_text(
                json.dumps(old_lock, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            lock_before = lock_path.read_bytes()
            init_before = (agent / "init.json").read_bytes()

            rejected = self.run_sync(*self.sync_args(project, source, binary))

            self.assertEqual(rejected[0], 1)
            new_digest = contract.expected_contract_evidence()["contract_sha256"]
            self.assertIn(f"--accept-contract-sha256 {new_digest}", rejected[2])
            self.assertEqual(lock_path.read_bytes(), lock_before)
            self.assertEqual((agent / "init.json").read_bytes(), init_before)

            accepted = self.run_sync(
                *self.sync_args(project, source, binary),
                "--accept-contract-sha256",
                new_digest,
            )
            self.assertEqual(accepted[0], 0, accepted[2])
            self.assertNotEqual(lock_path.read_bytes(), lock_before)

    def test_order_only_contract_transition_requires_explicit_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            binary = self.make_binary(root, tool_surface())
            first = self.run_sync(*self.sync_args(project, source, binary))
            self.assertEqual(first[0], 0, first[2])
            lock_path = agent / sync.LOCK_NAME
            old_lock = json.loads(lock_path.read_text(encoding="utf-8"))
            old_order = list(reversed(old_lock["contract"]["tool_order"]))
            old_lock["contract"]["tool_order"] = old_order
            old_lock["contract"]["tool_order_sha256"] = contract.json_sha256(old_order)
            old_lock["contract"]["contract_sha256"] = contract.json_sha256(
                {
                    "inputSchemas": contract.contract_payload(tool_surface()),
                    "toolOrder": old_order,
                }
            )
            self.assertEqual(
                old_lock["contract"]["tools_schema_sha256"],
                contract.expected_contract_evidence()["tools_schema_sha256"],
            )
            lock_path.write_text(
                json.dumps(old_lock, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            lock_before = lock_path.read_bytes()

            rejected = self.run_sync(
                "--project",
                str(project),
                "--agent",
                "coordinator",
                "--preflight-only",
            )

            new_digest = contract.expected_contract_evidence()["contract_sha256"]
            self.assertEqual(rejected[0], 1)
            self.assertIn("input schemas and/or tools/list order", rejected[2])
            self.assertIn(f"--accept-contract-sha256 {new_digest}", rejected[2])
            self.assertEqual(lock_path.read_bytes(), lock_before)

            accepted_preflight = self.run_sync(
                "--project",
                str(project),
                "--agent",
                "coordinator",
                "--preflight-only",
                "--accept-contract-sha256",
                new_digest,
            )
            self.assertEqual(accepted_preflight[0], 0, accepted_preflight[2])
            self.assertEqual(lock_path.read_bytes(), lock_before)

            accepted = self.run_sync(
                *self.sync_args(project, source, binary),
                "--accept-contract-sha256",
                new_digest,
            )
            self.assertEqual(accepted[0], 0, accepted[2])
            updated_lock = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(
                updated_lock["contract"]["tool_order"],
                list(contract.FROZEN_TOOL_ORDER),
            )

    def test_legacy_schema_only_lock_requires_order_aware_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            binary = self.make_binary(root, tool_surface())
            first = self.run_sync(*self.sync_args(project, source, binary))
            self.assertEqual(first[0], 0, first[2])
            lock_path = agent / sync.LOCK_NAME
            old_lock = json.loads(lock_path.read_text(encoding="utf-8"))
            for field in ("tool_order", "tool_order_sha256", "contract_sha256"):
                del old_lock["contract"][field]
            lock_path.write_text(
                json.dumps(old_lock, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            lock_before = lock_path.read_bytes()
            new_digest = contract.expected_contract_evidence()["contract_sha256"]

            rejected = self.run_sync(
                "--project",
                str(project),
                "--agent",
                "coordinator",
                "--preflight-only",
            )

            self.assertEqual(rejected[0], 1)
            self.assertIn(f"--accept-contract-sha256 {new_digest}", rejected[2])
            self.assertEqual(lock_path.read_bytes(), lock_before)

            accepted = self.run_sync(
                *self.sync_args(project, source, binary),
                "--accept-contract-sha256",
                new_digest,
            )
            self.assertEqual(accepted[0], 0, accepted[2])
            updated_lock = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(
                updated_lock["contract"], contract.expected_contract_evidence()
            )

    def test_lock_write_failure_rolls_back_both_lingtai_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            first_binary = self.make_binary(root, tool_surface(), "nokv-a")
            second_tools = tool_surface()
            for tool in second_tools:
                tool["description"] += " updated"
            second_binary = self.make_binary(root, second_tools, "nokv-b")
            installed = self.run_sync(*self.sync_args(project, source, first_binary))
            self.assertEqual(installed[0], 0, installed[2])
            paths = (
                agent / "mcp_registry.jsonl",
                agent / "init.json",
                agent / sync.LOCK_NAME,
            )
            before = {path: path.read_bytes() for path in paths}
            real_write = sync.installer.write_text_if_changed
            failed = False

            def fail_first_lock_write(path: Path, text: str) -> bool:
                nonlocal failed
                if path.name == sync.LOCK_NAME and not failed:
                    failed = True
                    raise OSError("injected lock write failure")
                return real_write(path, text)

            with mock.patch.object(
                sync.installer,
                "write_text_if_changed",
                side_effect=fail_first_lock_write,
            ):
                rejected = self.run_sync(
                    *self.sync_args(project, source, second_binary)
                )

            self.assertEqual(rejected[0], 1)
            self.assertIn("injected lock write failure", rejected[2])
            self.assertTrue(failed)
            self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_check_detects_locked_binary_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            binary = self.make_binary(root, tool_surface())
            installed = self.run_sync(*self.sync_args(project, source, binary))
            self.assertEqual(installed[0], 0, installed[2])
            lock = json.loads((agent / sync.LOCK_NAME).read_text(encoding="utf-8"))
            command = Path(lock["artifact"]["command"])
            healthy = self.run_sync(
                "--project",
                str(project),
                "--agent",
                "coordinator",
                "--check",
            )
            self.assertEqual(healthy[0], 0, healthy[2])
            self.assertIn("live_contract_valid: true", healthy[1])

            os.chmod(command, 0o755)
            with command.open("a", encoding="utf-8") as handle:
                handle.write("# replaced\n")

            checked = self.run_sync(
                "--project",
                str(project),
                "--agent",
                "coordinator",
                "--check",
            )

            self.assertEqual(checked[0], 1)
            self.assertIn("replaced in place", checked[2])

    def test_build_info_candidate_is_recorded_as_brew_distribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            binary = self.make_binary(
                root / "Cellar" / "nokv" / "0.1.0" / "bin",
                tool_surface(),
            )
            identity = runtime.source_identity(source, self.source_revision(source))
            build_info = (
                root
                / "Cellar"
                / "nokv"
                / "0.1.0"
                / "share"
                / "nokv"
                / "build-info.json"
            )
            runtime.write_build_info(build_info, identity, binary)

            wrong_tools = tool_surface()
            wrong_tools[0]["description"] += " wrong binary"
            wrong_binary = self.make_binary(root, wrong_tools, "wrong-nokv")
            rejected = self.run_sync(
                "--project",
                str(project),
                "--agent",
                "coordinator",
                "--nokv-bin",
                str(wrong_binary),
                "--build-info",
                str(build_info),
            )
            self.assertEqual(rejected[0], 1)
            self.assertIn("does not match build-info SHA-256", rejected[2])
            self.assertFalse((agent / "mcp_registry.jsonl").exists())

            result = self.run_sync(
                "--project",
                str(project),
                "--agent",
                "coordinator",
                "--nokv-bin",
                str(binary),
                "--build-info",
                str(build_info),
            )

            self.assertEqual(result[0], 0, result[2])
            lock = json.loads((agent / sync.LOCK_NAME).read_text(encoding="utf-8"))
            self.assertEqual(lock["source"]["distribution"], "brew")

    def test_build_info_digest_is_rechecked_during_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            binary = self.make_binary(root, tool_surface())
            args = self.sync_args(project, source, binary)
            real_stage = sync.stage_runtime

            def replace_before_stage(*stage_args, **stage_kwargs):
                with binary.open("a", encoding="utf-8") as handle:
                    handle.write("# replaced after build-info verification\n")
                return real_stage(*stage_args, **stage_kwargs)

            with mock.patch.object(
                sync, "stage_runtime", side_effect=replace_before_stage
            ):
                rejected = self.run_sync(*args)

            self.assertEqual(rejected[0], 1)
            self.assertIn("binary SHA-256 mismatch", rejected[2])
            self.assertFalse((agent / "mcp_registry.jsonl").exists())
            self.assertFalse((agent / sync.LOCK_NAME).exists())

    def test_holt_manifest_and_lock_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root, lock_revision="c" * 40)
            with self.assertRaisesRegex(ValueError, "Holt revision differs"):
                runtime.source_identity(source, self.source_revision(source))

    def test_build_source_owns_the_locked_cargo_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            source_revision = self.source_revision(source)
            ambient_target = root / "ambient-cargo-target"
            stale_candidate = source / "target/lingtai-workbench-source/release/nokv"
            self.make_binary(stale_candidate.parent, tool_surface())
            real_run = subprocess.run

            def complete_build(command, **kwargs):
                if command[0] != "cargo":
                    return real_run(command, **kwargs)
                self.assertFalse(kwargs["check"])
                self.assertIn("--locked", command)
                self.assertIn("--release", command)
                target_dir = Path(command[command.index("--target-dir") + 1])
                self.assertEqual(
                    target_dir, source.resolve() / "target/lingtai-workbench-source"
                )
                self.assertFalse((target_dir / "release/nokv").exists())
                self.make_binary(target_dir / "release", tool_surface())
                return subprocess.CompletedProcess(command, 0)

            with (
                mock.patch.dict(os.environ, {"CARGO_TARGET_DIR": str(ambient_target)}),
                mock.patch.object(
                    sync.subprocess, "run", side_effect=complete_build
                ) as run,
            ):
                candidate, identity = sync.build_source_candidate(
                    source,
                    revision=source_revision,
                    allow_dirty=False,
                )

            self.assertEqual(candidate, stale_candidate.resolve())
            self.assertEqual(identity.nokv_git_commit, source_revision)
            cargo_calls = [
                call
                for call in run.call_args_list
                if call.args and call.args[0][0] == "cargo"
            ]
            self.assertEqual(len(cargo_calls), 1)

    def test_interrupted_agent_update_is_recovered_before_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, agent = self.make_project(root)
            paths = sync._transaction_files(agent)
            original = {
                name: path.read_text(encoding="utf-8") if path.exists() else None
                for name, path in paths.items()
            }
            desired = {
                "mcp_registry.jsonl": '{"name":"nokv-workbench"}\n',
                "init.json": '{"mcp":{"nokv-workbench":{}}}\n',
                sync.LOCK_NAME: '{"schema":"desired"}\n',
            }
            transaction = {
                "schema": sync.TRANSACTION_SCHEMA,
                "original": original,
                "desired": desired,
            }
            (agent / sync.TRANSACTION_NAME).write_text(
                json.dumps(transaction), encoding="utf-8"
            )
            paths["mcp_registry.jsonl"].write_text(
                desired["mcp_registry.jsonl"], encoding="utf-8"
            )

            self.assertTrue(sync.recover_interrupted_update(agent))
            self.assertFalse((agent / sync.TRANSACTION_NAME).exists())
            for name, path in paths.items():
                if original[name] is None:
                    self.assertFalse(path.exists())
                else:
                    self.assertEqual(path.read_text(encoding="utf-8"), original[name])

    def test_failed_rollback_retains_journal_for_next_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            first_binary = self.make_binary(root, tool_surface(), "nokv-a")
            second_tools = tool_surface()
            for tool in second_tools:
                tool["description"] += " updated"
            second_binary = self.make_binary(root, second_tools, "nokv-b")
            installed = self.run_sync(*self.sync_args(project, source, first_binary))
            self.assertEqual(installed[0], 0, installed[2])
            paths = sync._transaction_files(agent)
            before = {name: path.read_bytes() for name, path in paths.items()}
            real_write = sync.installer.write_text_if_changed
            registry_writes = 0

            def fail_update_and_registry_rollback(path: Path, text: str) -> bool:
                nonlocal registry_writes
                if path.name == "mcp_registry.jsonl":
                    registry_writes += 1
                    if registry_writes == 2:
                        raise OSError("injected registry rollback failure")
                if path.name == sync.LOCK_NAME:
                    raise OSError("injected lock update failure")
                return real_write(path, text)

            with mock.patch.object(
                sync.installer,
                "write_text_if_changed",
                side_effect=fail_update_and_registry_rollback,
            ):
                rejected = self.run_sync(
                    *self.sync_args(project, source, second_binary)
                )

            journal = agent / sync.TRANSACTION_NAME
            self.assertEqual(rejected[0], 1)
            self.assertIn("recovery journal retained", rejected[2])
            self.assertTrue(journal.is_file())
            self.assertNotEqual(
                paths["mcp_registry.jsonl"].read_bytes(),
                before["mcp_registry.jsonl"],
            )

            self.assertTrue(sync.recover_interrupted_update(agent))
            self.assertFalse(journal.exists())
            self.assertEqual(
                {name: path.read_bytes() for name, path in paths.items()}, before
            )

    def test_lingtai_install_check_and_tuple_lock_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            tools = lingtai_tool_surface("writer")
            binary = self.make_binary(root, tools)
            grant = self.canonical_grant(role="writer")
            args = self.sync_args(
                project,
                source,
                binary,
                profile="lingtai",
                grant=grant,
            )
            digest = contract.raw_tool_definitions_sha256(tools)

            with mock.patch.dict(
                contract.FROZEN_LINGTAI_PROFILE_DIGESTS,
                {"writer": digest},
            ):
                installed = self.run_sync(*args)
                checked = self.run_sync(
                    "--project",
                    str(project),
                    "--agent",
                    "coordinator",
                    "--check",
                )

            self.assertEqual(installed[0], 0, installed[2])
            self.assertEqual(checked[0], 0, checked[2])
            lock = json.loads((agent / sync.LOCK_NAME).read_text(encoding="utf-8"))
            launch = lock["launch"]
            self.assertEqual(launch["profile"], "lingtai")
            self.assertEqual(launch["workspace_id"], "team-alpha")
            self.assertEqual(launch["workspace_actor_id"], "agent-7")
            grant_lock = launch["workspace_grant"]
            self.assertEqual(grant_lock["role"], "writer")
            self.assertEqual(grant_lock["grant_id"], "grant_1")
            self.assertEqual(grant_lock["issuer"], "lingtai-workbench-sync")
            self.assertEqual(grant_lock["audience"], "nokv-mcp:lingtai")
            self.assertRegex(grant_lock["canonical_sha256"], r"^[0-9a-f]{64}$")
            registry = json.loads(
                (agent / "mcp_registry.jsonl").read_text(encoding="utf-8")
            )
            self.assertIn("--workspace-id", registry["args"])
            self.assertIn(grant, registry["args"])
            self.assertNotIn("--workspace-dev-membership", registry["args"])

    def test_lingtai_contract_is_selected_by_grant_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            binary = self.make_binary(root, tool_surface())

            rejected = self.run_sync(
                *self.sync_args(
                    project,
                    source,
                    binary,
                    profile="lingtai",
                    role="reader",
                )
            )

            self.assertEqual(rejected[0], 1)
            self.assertIn("lingtai reader tool surface differs", rejected[2])
            self.assertFalse((agent / "mcp_registry.jsonl").exists())
            self.assertFalse((agent / sync.LOCK_NAME).exists())

    def test_profile_migrations_are_atomic_idempotent_and_preserve_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            binary = self.make_binary(root, tool_surface())
            installed = self.run_sync(*self.sync_args(project, source, binary))
            self.assertEqual(installed[0], 0, installed[2])
            product_data = project / ".lingtai" / "shared-data" / "keep.txt"
            product_data.parent.mkdir()
            product_data.write_text("keep\n", encoding="utf-8")

            lingtai_tools = lingtai_tool_surface("reader")
            lingtai_binary = self.make_binary(root, lingtai_tools, "nokv-lingtai")
            lingtai_args = self.sync_args(
                project,
                source,
                lingtai_binary,
                profile="lingtai",
                role="reader",
            )
            digest = contract.raw_tool_definitions_sha256(lingtai_tools)
            with mock.patch.dict(
                contract.FROZEN_LINGTAI_PROFILE_DIGESTS,
                {"reader": digest},
            ):
                migrated = self.run_sync(*lingtai_args)
                stable = self.run_sync(*lingtai_args)
            self.assertEqual(migrated[0], 0, migrated[2])
            self.assertEqual(stable[0], 0, stable[2])
            self.assertIn("already synchronized", stable[1])
            lingtai_lock = json.loads(
                (agent / sync.LOCK_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(lingtai_lock["launch"]["profile"], "lingtai")

            rollback_args = (
                *self.sync_args(project, source, binary),
                "--profile",
                "workbench",
            )
            rolled_back = self.run_sync(*rollback_args)
            second_rollback = self.run_sync(*rollback_args)
            self.assertEqual(rolled_back[0], 0, rolled_back[2])
            self.assertEqual(second_rollback[0], 0, second_rollback[2])
            workbench_lock = json.loads(
                (agent / sync.LOCK_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(workbench_lock["launch"]["profile"], "workbench")
            self.assertNotIn("workspace_id", workbench_lock["launch"])
            self.assertNotIn("workspace_actor_id", workbench_lock["launch"])
            self.assertNotIn("workspace_grant", workbench_lock["launch"])
            registry = json.loads(
                (agent / "mcp_registry.jsonl").read_text(encoding="utf-8")
            )
            self.assertNotIn("--workspace-id", registry["args"])
            self.assertNotIn("--workspace-actor-id", registry["args"])
            self.assertNotIn("--workspace-grant", registry["args"])
            self.assertEqual(product_data.read_text(encoding="utf-8"), "keep\n")

    def test_existing_lingtai_is_not_downgraded_by_omitted_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            lingtai_tools = lingtai_tool_surface("reader")
            lingtai_binary = self.make_binary(root, lingtai_tools, "nokv-lingtai")
            workbench_binary = self.make_binary(root, tool_surface(), "nokv-workbench")
            digest = contract.raw_tool_definitions_sha256(lingtai_tools)
            with mock.patch.dict(
                contract.FROZEN_LINGTAI_PROFILE_DIGESTS,
                {"reader": digest},
            ):
                installed = self.run_sync(
                    *self.sync_args(
                        project,
                        source,
                        lingtai_binary,
                        profile="lingtai",
                        role="reader",
                    )
                )
            self.assertEqual(installed[0], 0, installed[2])
            paths = sync._transaction_files(agent)
            before = {name: path.read_bytes() for name, path in paths.items()}

            implicit_preflight = self.run_sync(
                "--project",
                str(project),
                "--agent",
                "coordinator",
                "--preflight-only",
            )
            explicit_preflight = self.run_sync(
                "--project",
                str(project),
                "--agent",
                "coordinator",
                "--preflight-only",
                "--profile",
                "workbench",
            )
            self.assertEqual(implicit_preflight[0], 1)
            self.assertIn("refusing implicit", implicit_preflight[2])
            self.assertEqual(explicit_preflight[0], 0, explicit_preflight[2])
            self.assertEqual(
                {name: path.read_bytes() for name, path in paths.items()}, before
            )

            probe_args = (
                *self.sync_args(project, source, workbench_binary),
                "--probe-only",
            )
            implicit_probe = self.run_sync(*probe_args)
            explicit_probe = self.run_sync(
                *probe_args,
                "--profile",
                "workbench",
            )
            self.assertEqual(implicit_probe[0], 1)
            self.assertIn("refusing implicit", implicit_probe[2])
            self.assertEqual(explicit_probe[0], 0, explicit_probe[2])
            self.assertEqual(
                {name: path.read_bytes() for name, path in paths.items()}, before
            )

            implicit = self.run_sync(
                *self.sync_args(project, source, workbench_binary)
            )

            self.assertEqual(implicit[0], 1)
            self.assertIn("refusing implicit default workbench transition", implicit[2])
            self.assertIn("--profile workbench", implicit[2])
            self.assertEqual(
                {name: path.read_bytes() for name, path in paths.items()}, before
            )

            explicit = self.run_sync(
                *self.sync_args(project, source, workbench_binary),
                "--profile",
                "workbench",
            )

            self.assertEqual(explicit[0], 0, explicit[2])
            lock = json.loads((agent / sync.LOCK_NAME).read_text(encoding="utf-8"))
            self.assertEqual(lock["launch"]["profile"], "workbench")

    def test_lock_profile_and_workspace_fields_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            binary = self.make_binary(root, tool_surface())
            installed = self.run_sync(*self.sync_args(project, source, binary))
            self.assertEqual(installed[0], 0, installed[2])
            lock_path = agent / sync.LOCK_NAME
            healthy = json.loads(lock_path.read_text(encoding="utf-8"))

            for profile in (None, 7, "agent"):
                with self.subTest(profile=profile):
                    changed = copy.deepcopy(healthy)
                    if profile is None:
                        del changed["launch"]["profile"]
                    else:
                        changed["launch"]["profile"] = profile
                    lock_path.write_text(
                        json.dumps(changed, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    checked = self.run_sync(
                        "--project",
                        str(project),
                        "--agent",
                        "coordinator",
                        "--check",
                    )
                    self.assertEqual(checked[0], 1)
                    self.assertIn("lock profile", checked[2])

            changed = copy.deepcopy(healthy)
            changed["launch"]["workspace_id"] = "unexpected"
            lock_path.write_text(
                json.dumps(changed, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            checked = self.run_sync(
                "--project", str(project), "--agent", "coordinator", "--check"
            )
            self.assertEqual(checked[0], 1)
            self.assertIn("workbench lock", checked[2])

    def test_lingtai_lock_tuple_hash_and_expiry_fail_before_live_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            tools = lingtai_tool_surface("reader")
            binary = self.make_binary(root, tools)
            digest = contract.raw_tool_definitions_sha256(tools)
            with mock.patch.dict(
                contract.FROZEN_LINGTAI_PROFILE_DIGESTS,
                {"reader": digest},
            ):
                installed = self.run_sync(
                    *self.sync_args(
                        project,
                        source,
                        binary,
                        profile="lingtai",
                        role="reader",
                    )
                )
            self.assertEqual(installed[0], 0, installed[2])
            lock_path = agent / sync.LOCK_NAME
            healthy = json.loads(lock_path.read_text(encoding="utf-8"))

            mutations = {
                "hash": lambda lock: lock["launch"]["workspace_grant"].__setitem__(
                    "canonical_sha256", "0" * 64
                ),
                "tuple": lambda lock: lock["launch"].__setitem__(
                    "workspace_actor_id", "other-agent"
                ),
                "ambiguous-extra": lambda lock: lock["launch"].__setitem__(
                    "workspace", {"workspace_id": "shadow"}
                ),
                "expiry": lambda lock: lock["launch"]["workspace_grant"].update(
                    {
                        "issued_at_unix_ms": 1,
                        "expires_at_unix_ms": 2,
                    }
                ),
            }
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    changed = copy.deepcopy(healthy)
                    mutate(changed)
                    lock_path.write_text(
                        json.dumps(changed, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    with mock.patch.object(sync, "raw_tools_list") as live_probe:
                        checked = self.run_sync(
                            "--project",
                            str(project),
                            "--agent",
                            "coordinator",
                            "--check",
                        )
                    self.assertEqual(checked[0], 1)
                    live_probe.assert_not_called()

    def test_expiry_is_revalidated_immediately_before_journal_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, agent = self.make_project(root)
            now_unix_ms = time.time_ns() // 1_000_000
            grant = self.canonical_grant(
                role="reader",
                issued_at_unix_ms=now_unix_ms - 1_000,
                expires_at_unix_ms=now_unix_ms + 60_000,
            )
            config = sync.installer.InstallConfig(
                nokv_bin="/immutable/nokv",
                server_bind="127.0.0.1:7799",
                object_backend="rustfs",
                s3_endpoint="http://127.0.0.1:9000",
                s3_bucket="bucket",
                workbench_root="/agents/{agent_id}/wb",
                profile="lingtai",
                workspace_id="team-alpha",
                workspace_actor_id="agent-7",
                workspace_grant=grant,
            )
            parsed = sync.installer.parsed_workspace_grant(config)
            assert parsed is not None
            paths = sync._transaction_files(agent)
            before = {
                name: sync.installer.read_regular_text(path, missing_ok=True)
                for name, path in paths.items()
            }

            with self.assertRaisesRegex(ValueError, "not current"):
                sync.apply_agent_update(
                    agent,
                    config,
                    agent / sync.LOCK_NAME,
                    "{}\n",
                    now_unix_ms=parsed.expires_at_unix_ms,
                )

            self.assertEqual(
                {
                    name: sync.installer.read_regular_text(path, missing_ok=True)
                    for name, path in paths.items()
                },
                before,
            )
            self.assertFalse((agent / sync.TRANSACTION_NAME).exists())

    def test_stale_preflight_state_cannot_overwrite_newer_agent_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, agent = self.make_project(root)
            config = sync.installer.InstallConfig(
                nokv_bin="/immutable/nokv",
                server_bind="127.0.0.1:7799",
                object_backend="rustfs",
                s3_endpoint="http://127.0.0.1:9000",
                s3_bucket="bucket",
                workbench_root="/agents/{agent_id}/wb",
            )
            preflight_state = sync.read_agent_state(agent)
            precondition = sync.agent_state_sha256(preflight_state)
            (agent / "init.json").write_text(
                '{"mcp": {}, "newer": true}\n', encoding="utf-8"
            )
            newer_state = sync.read_agent_state(agent)

            with self.assertRaisesRegex(
                RuntimeError, "changed after rollout preflight"
            ):
                sync.apply_agent_update(
                    agent,
                    config,
                    agent / sync.LOCK_NAME,
                    "{}\n",
                    expected_original_state_sha256=precondition,
                )

            self.assertEqual(sync.read_agent_state(agent), newer_state)
            self.assertFalse((agent / sync.TRANSACTION_NAME).exists())

    def test_old_journal_recovers_before_invalid_new_grant_is_evaluated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            binary = self.make_binary(root, lingtai_tool_surface("reader"))
            paths = sync._transaction_files(agent)
            original = {
                name: path.read_text(encoding="utf-8") if path.exists() else None
                for name, path in paths.items()
            }
            desired = {
                "mcp_registry.jsonl": '{"old":"workbench"}\n',
                "init.json": '{"old":"workbench"}\n',
                sync.LOCK_NAME: '{"old":"workbench"}\n',
            }
            (agent / sync.TRANSACTION_NAME).write_text(
                json.dumps(
                    {
                        "schema": sync.TRANSACTION_SCHEMA,
                        "original": original,
                        "desired": desired,
                    }
                ),
                encoding="utf-8",
            )
            paths["mcp_registry.jsonl"].write_text(
                desired["mcp_registry.jsonl"], encoding="utf-8"
            )

            rejected = self.run_sync(
                *self.sync_args(
                    project,
                    source,
                    binary,
                    profile="lingtai",
                    grant="not-a-canonical-grant",
                )
            )

            self.assertEqual(rejected[0], 1)
            self.assertFalse((agent / sync.TRANSACTION_NAME).exists())
            for name, path in paths.items():
                if original[name] is None:
                    self.assertFalse(path.exists())
                else:
                    self.assertEqual(path.read_text(encoding="utf-8"), original[name])

    def test_recovery_all_partial_combinations_and_all_desired_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for direction in ("workbench-to-lingtai", "lingtai-to-workbench"):
                for mask in range(8):
                    with self.subTest(direction=direction, mask=mask):
                        _, agent = self.make_project(root / f"{direction}-{mask}")
                        paths = sync._transaction_files(agent)
                        original = {
                            name: f"{direction}:original:{name}\n"
                            for name in paths
                        }
                        desired = {
                            name: f"{direction}:desired:{name}\n" for name in paths
                        }
                        for name, path in paths.items():
                            path.write_text(original[name], encoding="utf-8")
                        (agent / sync.TRANSACTION_NAME).write_text(
                            json.dumps(
                                {
                                    "schema": sync.TRANSACTION_SCHEMA,
                                    "original": original,
                                    "desired": desired,
                                }
                            ),
                            encoding="utf-8",
                        )
                        for index, (name, path) in enumerate(paths.items()):
                            if mask & (1 << index):
                                path.write_text(desired[name], encoding="utf-8")

                        self.assertTrue(sync.recover_interrupted_update(agent))

                        expected = desired if mask == 7 else original
                        self.assertEqual(
                            {
                                name: path.read_text(encoding="utf-8")
                                for name, path in paths.items()
                            },
                            expected,
                        )
                        self.assertFalse((agent / sync.TRANSACTION_NAME).exists())

    def test_each_target_failpoint_rolls_back_both_profile_directions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            workbench_binary = self.make_binary(root, tool_surface(), "nokv-wb")
            lingtai_tools = lingtai_tool_surface("reader")
            lingtai_binary = self.make_binary(root, lingtai_tools, "nokv-lingtai")
            lingtai_digest = contract.raw_tool_definitions_sha256(lingtai_tools)
            for direction in ("workbench-to-lingtai", "lingtai-to-workbench"):
                for failed_name in (
                    "mcp_registry.jsonl",
                    "init.json",
                    sync.LOCK_NAME,
                ):
                    with self.subTest(direction=direction, failed_name=failed_name):
                        project, agent = self.make_project(
                            root / f"{direction}-{failed_name}"
                        )
                        with mock.patch.dict(
                            contract.FROZEN_LINGTAI_PROFILE_DIGESTS,
                            {"reader": lingtai_digest},
                        ):
                            if direction == "workbench-to-lingtai":
                                initial_args = self.sync_args(
                                    project, source, workbench_binary
                                )
                                next_args = self.sync_args(
                                    project,
                                    source,
                                    lingtai_binary,
                                    profile="lingtai",
                                    role="reader",
                                )
                            else:
                                initial_args = self.sync_args(
                                    project,
                                    source,
                                    lingtai_binary,
                                    profile="lingtai",
                                    role="reader",
                                )
                                next_args = (
                                    *self.sync_args(
                                        project, source, workbench_binary
                                    ),
                                    "--profile",
                                    "workbench",
                                )
                            installed = self.run_sync(*initial_args)
                            self.assertEqual(installed[0], 0, installed[2])
                            paths = sync._transaction_files(agent)
                            before = {
                                name: path.read_bytes() for name, path in paths.items()
                            }
                            real_write = sync.installer.write_text_if_changed
                            failed = False

                            def fail_target(path: Path, text: str) -> bool:
                                nonlocal failed
                                if path.name == failed_name and not failed:
                                    failed = True
                                    raise OSError(f"injected {failed_name} failure")
                                return real_write(path, text)

                            with mock.patch.object(
                                sync.installer,
                                "write_text_if_changed",
                                side_effect=fail_target,
                            ):
                                rejected = self.run_sync(*next_args)

                        self.assertEqual(rejected[0], 1)
                        self.assertTrue(failed)
                        self.assertEqual(
                            {name: path.read_bytes() for name, path in paths.items()},
                            before,
                        )
                        self.assertFalse((agent / sync.TRANSACTION_NAME).exists())

    def test_lingtai_manual_registry_drift_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            tools = lingtai_tool_surface("reader")
            binary = self.make_binary(root, tools)
            digest = contract.raw_tool_definitions_sha256(tools)
            with mock.patch.dict(
                contract.FROZEN_LINGTAI_PROFILE_DIGESTS,
                {"reader": digest},
            ):
                installed = self.run_sync(
                    *self.sync_args(
                        project,
                        source,
                        binary,
                        profile="lingtai",
                        role="reader",
                    )
                )
            self.assertEqual(installed[0], 0, installed[2])
            registry_path = agent / "mcp_registry.jsonl"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["args"][registry["args"].index("team-alpha")] = "other-team"
            registry_path.write_text(json.dumps(registry) + "\n", encoding="utf-8")

            with mock.patch.object(sync, "raw_tools_list") as live_probe:
                checked = self.run_sync(
                    "--project",
                    str(project),
                    "--agent",
                    "coordinator",
                    "--check",
                )

            self.assertEqual(checked[0], 1)
            self.assertIn("registry does not match", checked[2])
            live_probe.assert_not_called()

    def test_grant_replacement_changes_one_digest_and_all_three_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            tools = lingtai_tool_surface("reader")
            binary = self.make_binary(root, tools)
            digest = contract.raw_tool_definitions_sha256(tools)
            old_grant = self.canonical_grant(role="reader", grant_id="grant_old")
            new_grant = self.canonical_grant(role="reader", grant_id="grant_new")
            with mock.patch.dict(
                contract.FROZEN_LINGTAI_PROFILE_DIGESTS,
                {"reader": digest},
            ):
                first = self.run_sync(
                    *self.sync_args(
                        project,
                        source,
                        binary,
                        profile="lingtai",
                        grant=old_grant,
                    )
                )
                self.assertEqual(first[0], 0, first[2])
                old_lock = json.loads(
                    (agent / sync.LOCK_NAME).read_text(encoding="utf-8")
                )
                second = self.run_sync(
                    *self.sync_args(
                        project,
                        source,
                        binary,
                        profile="lingtai",
                        grant=new_grant,
                    )
                )
                checked = self.run_sync(
                    "--project",
                    str(project),
                    "--agent",
                    "coordinator",
                    "--check",
                )

            self.assertEqual(second[0], 0, second[2])
            self.assertEqual(checked[0], 0, checked[2])
            new_lock = json.loads(
                (agent / sync.LOCK_NAME).read_text(encoding="utf-8")
            )
            self.assertNotEqual(
                old_lock["launch"]["args_sha256"],
                new_lock["launch"]["args_sha256"],
            )
            self.assertEqual(
                new_lock["launch"]["workspace_grant"]["grant_id"], "grant_new"
            )
            registry = json.loads(
                (agent / "mcp_registry.jsonl").read_text(encoding="utf-8")
            )
            init = json.loads((agent / "init.json").read_text(encoding="utf-8"))
            self.assertIn(new_grant, registry["args"])
            self.assertNotIn(old_grant, registry["args"])
            self.assertEqual(
                registry["args"], init["mcp"][registry["name"]]["args"]
            )

    def test_lingtai_role_contract_transition_requires_exact_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            reader_tools = lingtai_tool_surface("reader")
            writer_tools = lingtai_tool_surface("writer")
            reader_binary = self.make_binary(root, reader_tools, "nokv-reader")
            writer_binary = self.make_binary(root, writer_tools, "nokv-writer")
            reader_digest = contract.raw_tool_definitions_sha256(reader_tools)
            writer_digest = contract.raw_tool_definitions_sha256(writer_tools)
            with mock.patch.dict(
                contract.FROZEN_LINGTAI_PROFILE_DIGESTS,
                {"reader": reader_digest, "writer": writer_digest},
            ):
                installed = self.run_sync(
                    *self.sync_args(
                        project,
                        source,
                        reader_binary,
                        profile="lingtai",
                        role="reader",
                    )
                )
                self.assertEqual(installed[0], 0, installed[2])
                before = {
                    name: path.read_bytes()
                    for name, path in sync._transaction_files(agent).items()
                }
                writer_args = self.sync_args(
                    project,
                    source,
                    writer_binary,
                    profile="lingtai",
                    role="writer",
                )
                rejected = self.run_sync(*writer_args)
                after_rejected = {
                    name: path.read_bytes()
                    for name, path in sync._transaction_files(agent).items()
                }
                expected = contract.expected_profile_contract_evidence(
                    "lingtai", role="writer"
                )["contract_sha256"]
                accepted = self.run_sync(
                    *writer_args,
                    "--accept-contract-sha256",
                    expected,
                )

            self.assertEqual(rejected[0], 1)
            self.assertIn(f"--accept-contract-sha256 {expected}", rejected[2])
            self.assertEqual(after_rejected, before)
            self.assertNotEqual(
                {
                    name: path.read_bytes()
                    for name, path in sync._transaction_files(agent).items()
                },
                before,
            )
            self.assertEqual(accepted[0], 0, accepted[2])
            lock = json.loads((agent / sync.LOCK_NAME).read_text(encoding="utf-8"))
            self.assertEqual(lock["contract"]["role"], "writer")

    def test_sync_lock_is_held_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, agent = self.make_project(root)
            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys; from pathlib import Path; "
                        f"sys.path.insert(0, {str(SCRIPT_DIR)!r}); "
                        "import sync_workbench_mcp as s; "
                        f"a=Path({str(agent)!r}); "
                        "c=s.agent_sync_lock(a, exclusive=True); c.__enter__(); "
                        "print('locked', flush=True); sys.stdin.readline(); "
                        "c.__exit__(None, None, None)"
                    ),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert holder.stdout is not None
                self.assertEqual(holder.stdout.readline().strip(), "locked")
                blocked = self.run_sync(
                    "--project",
                    str(project),
                    "--agent",
                    "coordinator",
                    "--preflight-only",
                )
                self.assertEqual(blocked[0], 1)
                self.assertIn("another NoKV workbench sync is active", blocked[2])
            finally:
                if holder.stdin is not None:
                    holder.stdin.write("release\n")
                    holder.stdin.flush()
                    holder.stdin.close()
                holder.wait(timeout=5)
                if holder.stdout is not None:
                    holder.stdout.close()
                if holder.stderr is not None:
                    holder.stderr.close()

    def test_orchestration_identity_token_mismatch_fails_before_agent_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, agent = self.make_project(root)
            _, token = sync.capture_agent_identity(project, "coordinator")
            identity = sync.parse_agent_identity_token(token)
            mismatched = sync.encode_agent_identity_token(
                sync.AgentIdentityToken(
                    **{
                        **identity.__dict__,
                        "agent_ino": identity.agent_ino + 1,
                    }
                )
            )
            before = (agent / "init.json").read_bytes()

            result = self.run_sync(
                "--project",
                str(project),
                "--agent",
                "coordinator",
                "--orchestration-agent-identity",
                mismatched,
                "--preflight-only",
            )

            self.assertEqual(result[0], 1)
            self.assertIn("Agent directory identity changed", result[2])
            self.assertEqual((agent / "init.json").read_bytes(), before)
            self.assertFalse((agent / sync.SYNC_LOCK_NAME).exists())

    def test_held_agent_descriptor_cannot_be_retargeted_by_name_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, agent = self.make_project(root)
            _, token = sync.capture_agent_identity(project, "coordinator")
            moved = agent.with_name("coordinator-original")

            with sync.open_agent_directory(
                project, "coordinator", None, token
            ) as handle:
                with sync.agent_sync_lock(handle.state_path, exclusive=True):
                    handle.verify_current()
                    agent.rename(moved)
                    agent.mkdir()
                    (agent / "init.json").write_text(
                        '{"replacement": true}\n', encoding="utf-8"
                    )
                    # Models a replacement after the production final identity
                    # check: relative writes still use the held Agent descriptor.
                    sync.installer.write_text_if_changed(
                        handle.state_path / "anchored.txt", "original\n"
                    )

            self.assertEqual(
                (moved / "anchored.txt").read_text(encoding="utf-8"), "original\n"
            )
            self.assertFalse((agent / "anchored.txt").exists())

    def test_check_rejects_agent_replacement_during_live_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, agent = self.make_project(root)
            (agent / sync.SYNC_LOCK_NAME).touch()
            _, token = sync.capture_agent_identity(project, "coordinator")
            moved = agent.with_name("coordinator-original")

            def replace_agent(*args, **kwargs):
                agent.rename(moved)
                agent.mkdir()
                (agent / "init.json").write_text(
                    '{"replacement": true}\n', encoding="utf-8"
                )
                return {"launch": {"profile": "workbench"}}

            with mock.patch.object(sync, "check_lock", side_effect=replace_agent):
                checked = self.run_sync(
                    "--project",
                    str(project),
                    "--agent",
                    "coordinator",
                    "--orchestration-agent-identity",
                    token,
                    "--check",
                )

            self.assertEqual(checked[0], 1)
            self.assertIn("replaced after selection", checked[2])
            self.assertNotIn("lock_valid: true", checked[1])
            self.assertNotIn("live_contract_valid: true", checked[1])

    def test_check_rejects_agent_rename_before_live_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            project, agent = self.make_project(root)
            binary = self.make_binary(root, tool_surface())
            installed = self.run_sync(*self.sync_args(project, source, binary))
            self.assertEqual(installed[0], 0, installed[2])

            renamed = agent.with_name("coordinator-renamed")
            agent.rename(renamed)
            with mock.patch.object(sync, "raw_tools_list") as live_probe:
                checked = self.run_sync(
                    "--project",
                    str(project),
                    "--agent",
                    renamed.name,
                    "--check",
                )

            self.assertEqual(checked[0], 1)
            self.assertIn("root expanded for the selected Agent", checked[2])
            live_probe.assert_not_called()
            self.assertNotIn("lock_valid: true", checked[1])
            self.assertNotIn("live_contract_valid: true", checked[1])

    def test_check_resolves_relative_candidate_from_invocation_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, agent = self.make_project(root)
            (agent / sync.SYNC_LOCK_NAME).touch()
            candidate = root / "candidate"
            candidate.write_text("candidate\n", encoding="utf-8")
            checked_lock = {"launch": {"profile": "workbench"}}
            original_cwd = Path.cwd()
            try:
                os.chdir(root)
                with mock.patch.object(
                    sync, "check_lock", return_value=checked_lock
                ) as check_lock:
                    checked = self.run_sync(
                        "--project",
                        str(project),
                        "--agent",
                        "coordinator",
                        "--check",
                        "--nokv-bin",
                        candidate.name,
                    )
            finally:
                os.chdir(original_cwd)

            self.assertEqual(checked[0], 0, checked[2])
            self.assertIn("lock_valid: true", checked[1])
            check_lock.assert_called_once_with(
                Path("."),
                agent_display_path=agent.resolve(),
                candidate_binary=str(candidate.resolve()),
                timeout_seconds=20.0,
            )

    def test_orchestration_identity_option_rejects_duplicate_and_malformed_tokens(self):
        for argv, expected in (
            (
                [
                    "--orchestration-agent-identity",
                    "bad",
                    "--orchestration-agent-identity",
                    "also-bad",
                ],
                "may be specified only once",
            ),
            (
                ["--orchestration-agent-identity", "not-a-token"],
                "identity token is malformed",
            ),
            (
                [
                    "--orchestration-agent-state-sha256",
                    "a" * 64,
                    "--orchestration-agent-state-sha256",
                    "b" * 64,
                ],
                "may be specified only once",
            ),
            (
                ["--orchestration-agent-state-sha256", "not-a-digest"],
                "SHA-256",
            ),
        ):
            with self.subTest(argv=argv):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        sync.parse_args(argv)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(expected, stderr.getvalue())

    def test_check_rejects_explicitly_empty_workspace_overrides(self):
        for option in (
            "--workspace-id",
            "--workspace-actor-id",
            "--workspace-grant",
        ):
            with self.subTest(option=option):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        sync.parse_args(["--check", option, ""])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("reconstructed from the lock", stderr.getvalue())

    def test_profile_and_workspace_singletons_reject_all_duplicates(self):
        duplicate_values = {
            "--profile": ("workbench", "lingtai"),
            "--workspace-id": ("team-alpha", "team-beta"),
            "--workspace-actor-id": ("agent-7", "agent-8"),
            "--workspace-grant": (
                "secret-grant-first",
                "secret-grant-second",
            ),
        }
        for option, (first, conflicting) in duplicate_values.items():
            for second in (first, conflicting):
                with self.subTest(option=option, conflict=second != first):
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        with self.assertRaises(SystemExit) as raised:
                            sync.parse_args([option, first, option, second])
                    diagnostic = stderr.getvalue()
                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn(f"{option} may be specified only once", diagnostic)
                    if option == "--workspace-grant":
                        self.assertNotIn(first, diagnostic)
                        self.assertNotIn(second, diagnostic)

    def test_contract_evidence_ignores_descriptions_but_rejects_order(self):
        original = tool_surface()
        changed = copy.deepcopy(original)
        for tool in changed:
            tool["description"] = "new prose"

        self.assertEqual(
            contract.contract_evidence(original),
            contract.contract_evidence(changed),
        )
        with self.assertRaisesRegex(
            contract.WorkbenchContractError, "tools/list order differs"
        ):
            contract.contract_evidence(list(reversed(original)))

    def test_contract_rejects_duplicate_tools_and_nullable_restore(self):
        duplicate = tool_surface()
        duplicate.append(copy.deepcopy(duplicate[0]))
        with self.assertRaisesRegex(contract.WorkbenchContractError, "duplicate"):
            contract.validate_tool_contract(duplicate)

        nullable = tool_surface()
        restore = next(
            tool for tool in nullable if tool["name"] == contract.RESTORE_TOOL
        )
        restore["inputSchema"]["properties"]["at_snapshot"]["anyOf"].append(
            {"type": "null"}
        )
        with self.assertRaisesRegex(
            contract.WorkbenchContractError, "workbench_restore inputSchema differs"
        ):
            contract.validate_tool_contract(nullable)


if __name__ == "__main__":
    unittest.main()
