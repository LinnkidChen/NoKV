#!/usr/bin/env python3
# Copyright 2024-2026 The NoKV Authors.
# SPDX-License-Identifier: Apache-2.0

import importlib.util
import base64
import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path


sys.dont_write_bytecode = True
MODULE_PATH = Path(__file__).with_name("install_workbench_mcp.py")
sys.path.insert(0, str(MODULE_PATH.parent))


def load_module():
    spec = importlib.util.spec_from_file_location("install_workbench_mcp", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallWorkbenchMcpTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def make_agent(
        self,
        root: Path,
        name: str = "coordinator",
        init: dict | None = None,
        registry: list[dict] | None = None,
        running: bool | None = None,
    ):
        agent_dir = root / ".lingtai" / name
        agent_dir.mkdir(parents=True)
        (agent_dir / "init.json").write_text(
            json.dumps(init or {"mcp": {}}, indent=2) + "\n",
            encoding="utf-8",
        )
        if running is not None:
            (agent_dir / ".status.json").write_text(
                json.dumps(
                    {
                        "identity": {"agent_name": name},
                        "runtime": {"running": running},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        if registry is not None:
            (agent_dir / "mcp_registry.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in registry),
                encoding="utf-8",
            )
        return agent_dir

    def config(self):
        return self.module.InstallConfig(
            nokv_bin="/repo/target/debug/nokv",
            server_bind="127.0.0.1:7799",
            object_backend="rustfs",
            s3_endpoint="http://127.0.0.1:9000",
            s3_bucket="nokv-lingtai-workbench",
            workbench_root="/workbenches",
        )

    def canonical_grant(
        self,
        *,
        workspace_id="team-alpha",
        actor_id="agent-7",
        role="writer",
        issued_at_unix_ms=None,
        expires_at_unix_ms=None,
    ):
        now = time.time_ns() // 1_000_000
        grant = {
            "schema": "nokv.lingtai.workspace_grant.v1",
            "grant_id": "grant_1",
            "issuer": "lingtai-workbench-sync",
            "audience": "nokv-mcp:lingtai",
            "workspace_id": workspace_id,
            "actor_id": actor_id,
            "role": role,
            "issued_at_unix_ms": (
                now - 1_000 if issued_at_unix_ms is None else issued_at_unix_ms
            ),
            "expires_at_unix_ms": (
                now + 60_000 if expires_at_unix_ms is None else expires_at_unix_ms
            ),
        }
        raw = json.dumps(
            grant,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def lingtai_config(self, **overrides):
        values = {
            "nokv_bin": "/repo/target/debug/nokv",
            "server_bind": "127.0.0.1:7799",
            "object_backend": "rustfs",
            "s3_endpoint": "http://127.0.0.1:9000",
            "s3_bucket": "nokv-lingtai-workbench",
            "workbench_root": "/workbenches",
            "profile": "lingtai",
            "workspace_id": "team-alpha",
            "workspace_actor_id": "agent-7",
            "workspace_grant": self.canonical_grant(),
        }
        values.update(overrides)
        return self.module.InstallConfig(**values)

    def test_default_profile_preserves_exact_workbench_arguments(self):
        self.assertEqual(
            self.module.mcp_args(self.config()),
            [
                "--server-bind",
                "127.0.0.1:7799",
                "--object-backend",
                "rustfs",
                "--s3-endpoint",
                "http://127.0.0.1:9000",
                "--s3-bucket",
                "nokv-lingtai-workbench",
                "mcp",
                "--profile",
                "workbench",
                "--workbench-root",
                "/workbenches",
            ],
        )

    def test_lingtai_arguments_have_stable_profile_root_identity_grant_order(self):
        config = self.lingtai_config()

        args = self.module.mcp_args(config)

        self.assertEqual(
            args[-10:],
            [
                "--profile",
                "lingtai",
                "--workbench-root",
                "/workbenches",
                "--workspace-id",
                "team-alpha",
                "--workspace-actor-id",
                "agent-7",
                "--workspace-grant",
                config.workspace_grant,
            ],
        )
        self.assertNotIn("--workspace-dev-membership", args)

    def test_lingtai_configures_registry_and_init_with_the_same_exact_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = self.make_agent(Path(tmp))
            config = self.lingtai_config()

            self.module.configure_agent(agent_dir, config)

            registry = json.loads(
                (agent_dir / "mcp_registry.jsonl").read_text(encoding="utf-8")
            )
            init = json.loads((agent_dir / "init.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["args"], self.module.mcp_args(config))
            self.assertEqual(init["mcp"]["nokv-workbench"]["args"], registry["args"])
            self.assertEqual(registry["template_arg_indices"], [])
            self.assertEqual(
                init["mcp"]["nokv-workbench"]["template_arg_indices"],
                registry["template_arg_indices"],
            )

    def test_only_workbench_root_is_selected_for_agent_template_expansion(self):
        config = self.lingtai_config(
            workbench_root="/agents/{agent_id}/wb/{agent_address}/{agent_dir}",
            workspace_id="literal-{agent_id}",
            workspace_actor_id="literal-{agent_dir}",
            workspace_grant=self.canonical_grant(
                workspace_id="literal-{agent_id}",
                actor_id="literal-{agent_dir}",
            ),
        )
        args = self.module.mcp_args(config)
        root_index = args.index("--workbench-root") + 1

        self.assertEqual(self.module.template_arg_indices(config), [root_index])
        self.assertEqual(
            self.module.mcp_launch_semantics(config),
            {
                "args": args,
                "template_arg_indices": [root_index],
            },
        )
        self.assertNotIn(args.index("--workspace-id") + 1, [root_index])
        self.assertNotIn(args.index("--workspace-actor-id") + 1, [root_index])

    def test_literal_workbench_root_emits_explicit_empty_template_indices(self):
        config = self.lingtai_config(
            workbench_root="/literal/workbench",
            workspace_id="literal-{agent_id}",
            workspace_actor_id="literal-{agent_dir}",
            workspace_grant=self.canonical_grant(
                workspace_id="literal-{agent_id}",
                actor_id="literal-{agent_dir}",
            ),
        )

        self.assertEqual(self.module.template_arg_indices(config), [])
        self.assertEqual(
            self.module.registry_record(config)["template_arg_indices"], []
        )
        self.assertEqual(self.module.init_spec(config)["template_arg_indices"], [])

    def test_profile_and_workspace_tuple_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "unsupported MCP profile"):
            self.module.InstallConfig(
                **{**self.config().__dict__, "profile": "developer"}
            )

        for field, value in (
            ("workspace_id", "team-alpha"),
            ("workspace_actor_id", "agent-7"),
            ("workspace_grant", self.canonical_grant()),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "workbench"):
                    self.module.InstallConfig(
                        **{**self.config().__dict__, field: value}
                    )

        for field in ("workspace_id", "workspace_actor_id", "workspace_grant"):
            with self.subTest(missing=field):
                with self.assertRaisesRegex(ValueError, "complete"):
                    self.lingtai_config(**{field: None})

    def test_lingtai_rejects_noncanonical_expired_or_conflicting_grant(self):
        now = time.time_ns() // 1_000_000
        invalid = (
            "not-base64",
            self.canonical_grant(
                expires_at_unix_ms=now,
                issued_at_unix_ms=now - 1_000,
            ),
            self.canonical_grant(actor_id="other-agent"),
        )
        for grant in invalid:
            with self.subTest(grant=grant):
                with self.assertRaises(ValueError):
                    self.lingtai_config(workspace_grant=grant)

    def test_raw_cli_accepts_supported_tuple_and_has_no_dev_membership(self):
        grant = self.canonical_grant()

        parsed = self.module.parse_args(
            [
                "--profile",
                "lingtai",
                "--workspace-id",
                "team-alpha",
                "--workspace-actor-id",
                "agent-7",
                "--workspace-grant",
                grant,
            ]
        )

        self.assertEqual(parsed.profile, "lingtai")
        self.assertEqual(parsed.workspace_grant, grant)
        self.assertFalse(
            hasattr(parsed, self.module._SINGLETON_OPTIONS_SEEN),
            "internal duplicate-tracking state must not leak into parsed CLI API",
        )
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.module.parse_args(["--workspace-dev-membership", "writer"])

    def test_identity_critical_cli_flags_reject_same_and_conflicting_duplicates(self):
        cases = (
            ("--profile", "workbench", "workbench"),
            ("--profile", "workbench", "lingtai"),
            ("--workspace-id", "team-alpha", "team-alpha"),
            ("--workspace-id", "team-alpha", "team-beta"),
            ("--workspace-actor-id", "agent-7", "agent-7"),
            ("--workspace-actor-id", "agent-7", "agent-8"),
            ("--workspace-grant", "grant-secret-one", "grant-secret-one"),
            ("--workspace-grant", "grant-secret-one", "grant-secret-two"),
        )
        for option, first, second in cases:
            with self.subTest(option=option, first=first, second=second):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        self.module.parse_args([option, first, option, second])

                self.assertEqual(raised.exception.code, 2)
                error = stderr.getvalue()
                self.assertIn(option, error)
                self.assertIn("may be specified at most once", error)
                if option == "--workspace-grant":
                    self.assertNotIn(first, error)
                    self.assertNotIn(second, error)

    def test_install_adds_registry_and_init_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = self.make_agent(Path(tmp))

            result = self.module.configure_agent(agent_dir, self.config())

            self.assertTrue(result.registry_changed)
            self.assertTrue(result.init_changed)
            registry = [
                json.loads(line)
                for line in (agent_dir / "mcp_registry.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                [record["name"] for record in registry], ["nokv-workbench"]
            )
            self.assertEqual(registry[0]["transport"], "stdio")
            self.assertEqual(registry[0]["template_arg_indices"], [])
            self.assertEqual(
                registry[0]["args"],
                [
                    "--server-bind",
                    "127.0.0.1:7799",
                    "--object-backend",
                    "rustfs",
                    "--s3-endpoint",
                    "http://127.0.0.1:9000",
                    "--s3-bucket",
                    "nokv-lingtai-workbench",
                    "mcp",
                    "--profile",
                    "workbench",
                    "--workbench-root",
                    "/workbenches",
                ],
            )
            init = json.loads((agent_dir / "init.json").read_text())
            self.assertEqual(
                init["mcp"]["nokv-workbench"]["command"], "/repo/target/debug/nokv"
            )
            self.assertEqual(init["mcp"]["nokv-workbench"]["args"], registry[0]["args"])
            self.assertEqual(
                init["mcp"]["nokv-workbench"]["template_arg_indices"], []
            )

    def test_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = self.make_agent(Path(tmp))

            first = self.module.configure_agent(agent_dir, self.config())
            registry_before = (agent_dir / "mcp_registry.jsonl").read_text()
            init_before = (agent_dir / "init.json").read_text()
            second = self.module.configure_agent(agent_dir, self.config())

            self.assertTrue(first.registry_changed)
            self.assertTrue(first.init_changed)
            self.assertFalse(second.registry_changed)
            self.assertFalse(second.init_changed)
            self.assertEqual(
                (agent_dir / "mcp_registry.jsonl").read_text(), registry_before
            )
            self.assertEqual((agent_dir / "init.json").read_text(), init_before)

    def test_install_replaces_existing_nokv_workbench_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_record = {
                "name": "nokv-workbench",
                "summary": "old",
                "transport": "stdio",
                "command": "/old/nokv",
                "args": ["mcp"],
                "source": "old",
            }
            other_record = {
                "name": "imap",
                "summary": "mail",
                "transport": "stdio",
                "command": "python",
                "args": ["-m", "imap"],
                "source": "local",
            }
            agent_dir = self.make_agent(
                Path(tmp),
                init={
                    "mcp": {
                        "nokv-workbench": {
                            "type": "stdio",
                            "command": "/old/nokv",
                            "args": ["mcp"],
                        },
                        "imap": {
                            "type": "stdio",
                            "command": "python",
                            "args": ["-m", "imap"],
                        },
                    }
                },
                registry=[old_record, other_record, old_record],
            )

            result = self.module.configure_agent(agent_dir, self.config())

            self.assertTrue(result.registry_changed)
            self.assertTrue(result.init_changed)
            registry = [
                json.loads(line)
                for line in (agent_dir / "mcp_registry.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                [record["name"] for record in registry], ["nokv-workbench", "imap"]
            )
            self.assertEqual(registry[0]["command"], "/repo/target/debug/nokv")
            init = json.loads((agent_dir / "init.json").read_text())
            self.assertEqual(
                init["mcp"]["nokv-workbench"]["command"], "/repo/target/debug/nokv"
            )
            self.assertEqual(init["mcp"]["imap"]["command"], "python")

    def test_invalid_init_fails_before_registry_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent_dir = self.make_agent(
                root,
                registry=[
                    {
                        "name": "other",
                        "transport": "stdio",
                        "command": "other",
                    }
                ],
            )
            registry = agent_dir / "mcp_registry.jsonl"
            registry_before = registry.read_bytes()
            (agent_dir / "init.json").write_text('{"mcp": []}\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "mcp must be a JSON object"):
                self.module.configure_agent(agent_dir, self.config())

            self.assertEqual(registry.read_bytes(), registry_before)

    def test_resolve_agent_dir_selects_running_coordinator_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            coordinator = self.make_agent(
                root, "coordinator(codex-gpt-5.4)", running=True
            )
            self.make_agent(root, "scribe", running=True)

            resolved = self.module.resolve_agent_dir(root, None, None)

            self.assertEqual(resolved, coordinator.resolve())

    def test_resolve_agent_dir_selects_single_agent_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            only_agent = self.make_agent(root, "scout", running=False)

            resolved = self.module.resolve_agent_dir(root, None, None)

            self.assertEqual(resolved, only_agent.resolve())

    def test_default_workbench_root_matches_lingtai_per_agent_contract(self):
        # Contract with lingtai-kernel: the kernel expands {agent_id} at MCP
        # launch (Agent._expand_agent_placeholders), and its bundled
        # nokv-workbench skill assets use this exact template. Both delivery
        # paths must install the same per-agent root.
        self.assertEqual(self.module.DEFAULT_WORKBENCH_ROOT, "/agents/{agent_id}/wb")

    def test_install_with_default_root_preserves_agent_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = self.make_agent(Path(tmp))
            config = self.module.InstallConfig(
                nokv_bin="/repo/target/debug/nokv",
                server_bind="127.0.0.1:7799",
                object_backend="rustfs",
                s3_endpoint="http://127.0.0.1:9000",
                s3_bucket="nokv-lingtai-workbench",
                workbench_root=self.module.DEFAULT_WORKBENCH_ROOT,
            )

            self.module.configure_agent(agent_dir, config)

            registry = [
                json.loads(line)
                for line in (agent_dir / "mcp_registry.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                registry[0]["args"][-2:],
                ["--workbench-root", "/agents/{agent_id}/wb"],
            )
            root_index = registry[0]["args"].index("--workbench-root") + 1
            self.assertEqual(registry[0]["template_arg_indices"], [root_index])
            init = json.loads((agent_dir / "init.json").read_text())
            self.assertEqual(init["mcp"]["nokv-workbench"]["args"], registry[0]["args"])
            self.assertEqual(
                init["mcp"]["nokv-workbench"]["template_arg_indices"],
                [root_index],
            )

    def test_resolve_agent_dir_rejects_ambiguous_non_coordinator_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_agent(root, "scout", running=True)
            self.make_agent(root, "scribe", running=True)

            with self.assertRaisesRegex(ValueError, "multiple LingTai agents"):
                self.module.resolve_agent_dir(root, None, None)

    def test_named_agent_cannot_escape_project_or_follow_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = self.make_agent(root)
            outside = root / "outside"
            outside.mkdir()
            (outside / "init.json").write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "one directory name"):
                self.module.resolve_agent_dir(root, "../outside", None)

            linked = agent.parent / "linked"
            linked.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                self.module.resolve_agent_dir(root, "linked", None)


if __name__ == "__main__":
    unittest.main()
