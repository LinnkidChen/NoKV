#!/usr/bin/env python3
# Copyright 2024-2026 The NoKV Authors.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import nokv_runtime as runtime  # noqa: E402
import generate_nokv_build_info as generator  # noqa: E402


NOKV_REVISION = "a" * 40
HOLT_REVISION = "b" * 40
IDENTITY = runtime.SourceIdentity(
    schema=runtime.BUILD_INFO_SCHEMA,
    nokv_version="0.1.0",
    nokv_git_commit=NOKV_REVISION,
    source_dirty=False,
    cargo_lock_sha256="c" * 64,
    holt_crate_version="0.8.2",
    holt_git_commit=HOLT_REVISION,
)


class NokvRuntimeTest(unittest.TestCase):
    def make_project(self, root: Path) -> Path:
        project = root / "project"
        (project / ".lingtai").mkdir(parents=True)
        return project

    def make_binary(self, root: Path, content: bytes = b"candidate nokv\n") -> Path:
        binary = root / "nokv"
        binary.write_bytes(content)
        os.chmod(binary, 0o755)
        return binary

    def registry_identity(self) -> runtime.SourceIdentity:
        return runtime.SourceIdentity(
            schema="nokv.build_info.v2",
            nokv_version="0.1.0",
            nokv_git_commit=NOKV_REVISION,
            source_dirty=False,
            cargo_lock_sha256="c" * 64,
            holt_crate_version="0.8.2",
            holt_registry="registry+https://github.com/rust-lang/crates.io-index",
            holt_checksum_sha256="d" * 64,
        )

    def make_registry_source(
        self,
        root: Path,
        *,
        manifest_version: str = "=0.8.2",
        lock_version: str = "0.8.2",
        lock_source: str = "registry+https://github.com/rust-lang/crates.io-index",
        checksum: str | None = "d" * 64,
        extra_manifest_fields: str = "",
    ) -> tuple[Path, str]:
        source_root = root / "source"
        (source_root / "crates/nokv").mkdir(parents=True)
        extra = f", {extra_manifest_fields}" if extra_manifest_fields else ""
        (source_root / "Cargo.toml").write_text(
            "[workspace.dependencies]\n"
            f'holt = {{ version = "{manifest_version}"{extra}, '
            'default-features = false }\n',
            encoding="utf-8",
        )
        checksum_line = f'checksum = "{checksum}"\n' if checksum is not None else ""
        (source_root / "Cargo.lock").write_text(
            "[[package]]\n"
            'name = "holt"\n'
            f'version = "{lock_version}"\n'
            f'source = "{lock_source}"\n'
            f"{checksum_line}",
            encoding="utf-8",
        )
        (source_root / "crates/nokv/Cargo.toml").write_text(
            '[package]\nname = "nokv"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=source_root, check=True)
        subprocess.run(["git", "add", "."], cwd=source_root, check=True)
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
            cwd=source_root,
            check=True,
        )
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source_root, text=True
        ).strip()
        return source_root, revision

    def test_stage_runtime_is_content_addressed_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.make_project(root)
            binary = self.make_binary(root)

            first = runtime.stage_runtime(project, binary, IDENTITY)
            command_inode = first.command.stat().st_ino
            build_info = first.command.parent / "build-info.json"
            build_info_bytes = build_info.read_bytes()

            second = runtime.stage_runtime(project, binary, IDENTITY)

            self.assertEqual(second, first)
            self.assertEqual(second.command.stat().st_ino, command_inode)
            self.assertEqual(second.command.read_bytes(), binary.read_bytes())
            self.assertEqual(build_info.read_bytes(), build_info_bytes)
            self.assertFalse(list(first.command.parent.parent.glob(".nokv.*")))

    def test_candidate_change_between_hash_and_copy_is_rejected_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.make_project(root)
            original = b"original candidate bytes\n"
            replacement = b"replacement candidate bytes\n"
            binary = self.make_binary(root, original)
            resolved_binary = binary.resolve()
            original_digest = hashlib.sha256(original).hexdigest()
            real_sha256_file = runtime.sha256_file
            changed = False

            def hash_then_change(path: Path) -> str:
                nonlocal changed
                digest = real_sha256_file(path)
                if Path(path) == resolved_binary and not changed:
                    binary.write_bytes(replacement)
                    changed = True
                return digest

            with mock.patch.object(
                runtime, "sha256_file", side_effect=hash_then_change
            ):
                with self.assertRaisesRegex(ValueError, "changed while staging"):
                    runtime.stage_runtime(project, binary, IDENTITY)

            revision_dir = project / ".lingtai" / "runtime" / "nokv" / NOKV_REVISION
            self.assertTrue(changed)
            self.assertFalse((revision_dir / original_digest).exists())
            self.assertFalse(list(revision_dir.glob(".nokv.*")))
            self.assertFalse(list(revision_dir.rglob("build-info.json")))

    def test_existing_symlink_in_managed_runtime_path_is_rejected(self):
        components = (".lingtai", "runtime", "nokv", "revision", "digest")
        for component in components:
            with (
                self.subTest(component=component),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                project = root / "project"
                project.mkdir()
                external = root / "external"
                external.mkdir()
                binary = self.make_binary(root)
                digest = runtime.sha256_file(binary)

                if component == ".lingtai":
                    (project / ".lingtai").symlink_to(
                        external, target_is_directory=True
                    )
                else:
                    lingtai_root = project / ".lingtai"
                    lingtai_root.mkdir()
                    if component == "runtime":
                        (lingtai_root / "runtime").symlink_to(
                            external, target_is_directory=True
                        )
                    else:
                        runtime_root = lingtai_root / "runtime"
                        runtime_root.mkdir()
                        if component == "nokv":
                            (runtime_root / "nokv").symlink_to(
                                external, target_is_directory=True
                            )
                        else:
                            nokv_root = runtime_root / "nokv"
                            nokv_root.mkdir()
                            if component == "revision":
                                (nokv_root / NOKV_REVISION).symlink_to(
                                    external, target_is_directory=True
                                )
                            else:
                                revision_dir = nokv_root / NOKV_REVISION
                                revision_dir.mkdir()
                                (revision_dir / digest).symlink_to(
                                    external, target_is_directory=True
                                )

                with self.assertRaisesRegex(ValueError, "symlink component"):
                    runtime.stage_runtime(project, binary, IDENTITY)

                self.assertEqual(list(external.iterdir()), [])

    def test_existing_runtime_file_symlink_is_rejected_without_following_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.make_project(root)
            binary = self.make_binary(root)
            digest = runtime.sha256_file(binary)
            runtime_dir = (
                project / ".lingtai" / "runtime" / "nokv" / NOKV_REVISION / digest
            )
            runtime_dir.mkdir(parents=True)
            external = root / "external-nokv"
            external.write_bytes(b"do not replace\n")
            (runtime_dir / "nokv").symlink_to(external)

            with self.assertRaisesRegex(ValueError, "not a regular file"):
                runtime.stage_runtime(project, binary, IDENTITY)

            self.assertEqual(external.read_bytes(), b"do not replace\n")
            self.assertTrue((runtime_dir / "nokv").is_symlink())
            self.assertFalse(list((runtime_dir.parent).glob(".nokv.*")))

    def test_existing_build_info_symlink_is_rejected_without_following_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.make_project(root)
            binary = self.make_binary(root)
            staged = runtime.stage_runtime(project, binary, IDENTITY)
            build_info = staged.command.parent / "build-info.json"
            external = root / "external-build-info.json"
            external.write_text("do not replace\n", encoding="utf-8")
            build_info.unlink()
            build_info.symlink_to(external)

            with self.assertRaisesRegex(ValueError, "not a regular file"):
                runtime.stage_runtime(project, binary, IDENTITY)

            self.assertEqual(external.read_text(encoding="utf-8"), "do not replace\n")
            self.assertTrue(build_info.is_symlink())

    def test_managed_directory_swap_cannot_redirect_staging_writes(self):
        components = (".lingtai", "runtime", "nokv", "revision", "digest")
        for component in components:
            with (
                self.subTest(component=component),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                project = self.make_project(root)
                binary = self.make_binary(root)
                digest = runtime.sha256_file(binary)
                names = {
                    ".lingtai": ".lingtai",
                    "runtime": "runtime",
                    "nokv": "nokv",
                    "revision": NOKV_REVISION,
                    "digest": digest,
                }
                external = root / f"external-{component}"
                external.mkdir()
                held = root / f"held-{component}"
                real_open_directory_at = runtime._open_directory_at
                swapped = False

                def open_then_swap(
                    directory_fd: int,
                    name: str,
                    path: Path,
                    *,
                    create: bool,
                ) -> int:
                    nonlocal swapped
                    descriptor = real_open_directory_at(
                        directory_fd, name, path, create=create
                    )
                    if name == names[component] and not swapped:
                        path.rename(held)
                        path.symlink_to(external, target_is_directory=True)
                        swapped = True
                    return descriptor

                with mock.patch.object(
                    runtime, "_open_directory_at", side_effect=open_then_swap
                ):
                    with self.assertRaisesRegex(
                        ValueError, "symlink component|changed while staging"
                    ):
                        runtime.stage_runtime(project, binary, IDENTITY)

                self.assertTrue(swapped)
                self.assertEqual(list(external.iterdir()), [])
                self.assertFalse(list(external.rglob("nokv")))
                self.assertFalse(list(external.rglob("build-info.json")))

    def test_holt_lock_source_with_deceptive_host_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp)
            (source_root / "Cargo.toml").write_text(
                "[workspace.dependencies]\n"
                f'holt = {{ git = "https://github.com/NoKV-Lab/holt.git", '
                f'rev = "{HOLT_REVISION}" }}\n',
                encoding="utf-8",
            )
            (source_root / "Cargo.lock").write_text(
                "[[package]]\n"
                'name = "holt"\n'
                'version = "0.8.2"\n'
                'source = "git+https://evil.invalid/'
                f'github.com/NoKV-Lab/holt.git#{HOLT_REVISION}"\n',
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=source_root, check=True)
            subprocess.run(["git", "add", "."], cwd=source_root, check=True)
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
                cwd=source_root,
                check=True,
            )
            revision = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=source_root, text=True
            ).strip()

            with self.assertRaisesRegex(ValueError, "not pinned to NoKV-Lab/holt"):
                runtime.source_identity(source_root, revision=revision)

    def test_source_identity_requires_a_real_git_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp)
            (source_root / "crates/nokv").mkdir(parents=True)
            (source_root / "Cargo.toml").write_text(
                "[workspace.dependencies]\n"
                f'holt = {{ git = "https://github.com/NoKV-Lab/holt.git", '
                f'rev = "{HOLT_REVISION}" }}\n',
                encoding="utf-8",
            )
            (source_root / "Cargo.lock").write_text(
                "[[package]]\n"
                'name = "holt"\n'
                'version = "0.8.2"\n'
                'source = "git+https://github.com/NoKV-Lab/holt.git'
                f'?rev={HOLT_REVISION}#{HOLT_REVISION}"\n',
                encoding="utf-8",
            )
            (source_root / "crates/nokv/Cargo.toml").write_text(
                '[package]\nname = "nokv"\nversion = "0.1.0"\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "requires a git checkout"):
                runtime.source_identity(source_root, revision=NOKV_REVISION)

    def test_registry_holt_source_identity_and_staged_build_info_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root, revision = self.make_registry_source(root)

            identity = runtime.source_identity(source_root, revision=revision)

            self.assertEqual(identity.schema, runtime.BUILD_INFO_SCHEMA_V2)
            self.assertEqual(
                identity.as_dict(),
                {
                    "schema": "nokv.build_info.v2",
                    "nokv_version": "0.1.0",
                    "nokv_git_commit": revision,
                    "source_dirty": False,
                    "cargo_lock_sha256": runtime.sha256_file(
                        source_root / "Cargo.lock"
                    ),
                    "holt_crate_version": "0.8.2",
                    "holt_registry": runtime.CRATES_IO_REGISTRY,
                    "holt_checksum_sha256": "d" * 64,
                },
            )
            self.assertIsNone(identity.holt_git_commit)

            project = self.make_project(root)
            binary = self.make_binary(root)
            staged = runtime.stage_runtime(project, binary, identity)
            loaded = runtime.load_build_info(
                staged.command.parent / "build-info.json"
            )
            self.assertEqual(loaded.identity, identity)
            self.assertEqual(loaded.identity.as_dict(), identity.as_dict())

    def test_invalid_schema_specific_identity_fails_before_staging_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.make_project(root)
            binary = self.make_binary(root)
            ambiguous = runtime.SourceIdentity(
                schema="nokv.build_info.v2",
                nokv_version="0.1.0",
                nokv_git_commit=NOKV_REVISION,
                source_dirty=False,
                cargo_lock_sha256="c" * 64,
                holt_crate_version="0.8.2",
                holt_git_commit=HOLT_REVISION,
                holt_registry="registry+https://github.com/rust-lang/crates.io-index",
                holt_checksum_sha256="d" * 64,
            )

            with self.assertRaisesRegex(ValueError, "conflict with schema"):
                runtime.stage_runtime(project, binary, ambiguous)

            self.assertFalse((project / ".lingtai/runtime").exists())

    def test_v1_identity_mapping_and_build_info_remain_byte_compatible(self):
        expected = {
            "schema": "nokv.build_info.v1",
            "nokv_version": "0.1.0",
            "nokv_git_commit": NOKV_REVISION,
            "source_dirty": False,
            "cargo_lock_sha256": "c" * 64,
            "holt_crate_version": "0.8.2",
            "holt_git_commit": HOLT_REVISION,
        }
        self.assertEqual(IDENTITY.as_dict(), expected)
        self.assertEqual(
            runtime.identity_from_mapping(expected, context="old v1 lock"),
            IDENTITY,
        )

        build_info_mapping = {
            **expected,
            "binary_sha256": "e" * 64,
            "binary_size_bytes": 123,
        }
        parsed = runtime.build_info_from_mapping(
            build_info_mapping,
            context="old v1 build-info",
        )
        self.assertEqual(parsed.identity, IDENTITY)
        self.assertEqual(parsed.as_dict(), build_info_mapping)
        self.assertEqual(
            json.dumps(parsed.as_dict(), indent=2, sort_keys=True) + "\n",
            json.dumps(build_info_mapping, indent=2, sort_keys=True) + "\n",
        )

    def test_registry_holt_source_fails_closed_on_invalid_lock_or_manifest(self):
        cases = (
            (
                "wrong registry",
                {"lock_source": "registry+https://evil.invalid/index"},
                "recognized crates.io registry",
            ),
            ("missing checksum", {"checksum": None}, "checksum"),
            ("bad checksum", {"checksum": "not-a-checksum"}, "checksum"),
            (
                "unpinned manifest",
                {"manifest_version": "0.8.2"},
                "exactly pin Holt",
            ),
            (
                "version mismatch",
                {"manifest_version": "=0.8.3"},
                "version differs",
            ),
            (
                "ambiguous manifest source",
                {
                    "extra_manifest_fields": (
                        'git = "https://github.com/NoKV-Lab/holt.git"'
                    )
                },
                "ambiguous",
            ),
            (
                "renamed manifest package",
                {"extra_manifest_fields": 'package = "holt-fork"'},
                "ambiguous",
            ),
        )
        for label, kwargs, message in cases:
            with (
                self.subTest(label=label),
                tempfile.TemporaryDirectory() as tmp,
            ):
                source_root, revision = self.make_registry_source(
                    Path(tmp),
                    **kwargs,
                )
                with self.assertRaisesRegex(ValueError, message):
                    runtime.source_identity(source_root, revision=revision)

    def test_python310_fallback_parses_supported_holt_dependency_forms(self):
        inline = (
            "[workspace.dependencies]\n"
            'holt = { version = "=0.8.2", default-features = false }\n'
        )
        dedicated = (
            "[workspace.dependencies.holt]\n"
            'version = "=0.8.2"\n'
            "default-features = false\n"
        )
        with mock.patch.object(runtime, "tomllib", None):
            self.assertEqual(runtime._registry_holt_version(inline), "0.8.2")
            self.assertEqual(runtime._registry_holt_version(dedicated), "0.8.2")
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                runtime._registry_holt_version(
                    inline
                    + "[workspace.dependencies.holt]\n"
                    + 'version = "=0.8.2"\n'
                )
            with self.assertRaisesRegex(ValueError, "exactly pin"):
                runtime._registry_holt_version(
                    "[workspace.dependencies]\n"
                    'holt = { version = "0.8.2" }\n'
                )
            with self.assertRaisesRegex(ValueError, "exactly pin"):
                runtime._registry_holt_version(
                    "[workspace.dependencies]\n"
                    "holt = { version = =0.8.2 }\n"
                )
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                runtime._registry_holt_version(
                    "[workspace.dependencies]\n"
                    'holt = { version = "=0.8.2", package = "holt-fork" }\n'
                )

    def test_identity_mappings_require_canonical_lowercase_hex(self):
        for identity in (IDENTITY, self.registry_identity()):
            source_fields = ["nokv_git_commit", "cargo_lock_sha256"]
            if identity.schema == runtime.BUILD_INFO_SCHEMA_V1:
                source_fields.append("holt_git_commit")
            else:
                source_fields.append("holt_checksum_sha256")

            for field in source_fields:
                with self.subTest(schema=identity.schema, field=field):
                    mapping = identity.as_dict()
                    mapping[field] = mapping[field].upper()
                    with self.assertRaisesRegex(ValueError, "lowercase hex"):
                        runtime.identity_from_mapping(
                            mapping,
                            context="noncanonical source identity",
                        )

            with self.subTest(schema=identity.schema, field="binary_sha256"):
                build_info = {
                    **identity.as_dict(),
                    "binary_sha256": "E" * 64,
                    "binary_size_bytes": 123,
                }
                with self.assertRaisesRegex(ValueError, "lowercase hex"):
                    runtime.build_info_from_mapping(
                        build_info,
                        context="noncanonical build-info",
                    )

    def test_identity_mapping_rejects_cross_schema_source_fields(self):
        v1 = IDENTITY.as_dict()
        v1["holt_registry"] = "registry+https://github.com/rust-lang/crates.io-index"
        v1["holt_checksum_sha256"] = "d" * 64
        with self.assertRaisesRegex(ValueError, "ambiguous Holt source"):
            runtime.identity_from_mapping(v1, context="ambiguous v1")

        v2 = {
            "schema": "nokv.build_info.v2",
            "nokv_version": "0.1.0",
            "nokv_git_commit": NOKV_REVISION,
            "source_dirty": False,
            "cargo_lock_sha256": "c" * 64,
            "holt_crate_version": "0.8.2",
            "holt_registry": "registry+https://github.com/rust-lang/crates.io-index",
            "holt_checksum_sha256": "d" * 64,
            "holt_git_commit": HOLT_REVISION,
        }
        with self.assertRaisesRegex(ValueError, "ambiguous Holt source"):
            runtime.identity_from_mapping(v2, context="ambiguous v2")

        unknown = IDENTITY.as_dict()
        unknown["holt_source"] = "guess"
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            runtime.identity_from_mapping(unknown, context="unknown v1")

    def test_context_specific_identity_fields_fail_closed(self):
        for identity in (IDENTITY, self.registry_identity()):
            with self.subTest(schema=identity.schema, container="pure"):
                contaminated = {
                    **identity.as_dict(),
                    "binary_sha256": "e" * 64,
                    "binary_size_bytes": 123,
                }
                with self.assertRaisesRegex(ValueError, "unsupported.*fields"):
                    runtime.identity_from_mapping(
                        contaminated,
                        context="contaminated pure identity",
                    )

            with self.subTest(schema=identity.schema, container="lock"):
                contaminated = {
                    "distribution": "path",
                    **identity.as_dict(),
                    "binary_sha256": "e" * 64,
                    "binary_size_bytes": 123,
                }
                with self.assertRaisesRegex(ValueError, "unsupported.*fields"):
                    runtime.lock_source_identity_from_mapping(
                        contaminated,
                        context="contaminated lock source",
                    )

            with self.subTest(schema=identity.schema, container="build-info"):
                contaminated = {
                    **identity.as_dict(),
                    "binary_sha256": "e" * 64,
                    "binary_size_bytes": 123,
                    "distribution": "path",
                }
                with self.assertRaisesRegex(ValueError, "unsupported.*fields"):
                    runtime.build_info_from_mapping(
                        contaminated,
                        context="contaminated build-info",
                    )

    def test_lock_source_parser_requires_exact_valid_distribution(self):
        for identity in (IDENTITY, self.registry_identity()):
            with self.subTest(schema=identity.schema, missing="distribution"):
                with self.assertRaisesRegex(ValueError, "missing fields"):
                    runtime.lock_source_identity_from_mapping(
                        identity.as_dict(),
                        context="incomplete lock source",
                    )

            for distribution in ("path", "brew", "release", "source"):
                with self.subTest(
                    schema=identity.schema,
                    distribution=distribution,
                ):
                    mapping = {"distribution": distribution, **identity.as_dict()}
                    self.assertEqual(
                        runtime.lock_source_identity_from_mapping(
                            mapping,
                            context="valid lock source",
                        ),
                        identity,
                    )

            for distribution in (None, "", "unknown", 7):
                with self.subTest(
                    schema=identity.schema,
                    invalid_distribution=distribution,
                ):
                    mapping = {
                        "distribution": distribution,
                        **identity.as_dict(),
                    }
                    with self.assertRaisesRegex(ValueError, "distribution"):
                        runtime.lock_source_identity_from_mapping(
                            mapping,
                            context="invalid lock source",
                        )

    def test_pure_identity_and_build_info_require_exact_v1_v2_key_sets(self):
        for identity in (IDENTITY, self.registry_identity()):
            with self.subTest(schema=identity.schema, valid="pure"):
                self.assertEqual(
                    runtime.identity_from_mapping(
                        identity.as_dict(),
                        context="valid pure identity",
                    ),
                    identity,
                )

            build_info = {
                **identity.as_dict(),
                "binary_sha256": "e" * 64,
                "binary_size_bytes": 123,
            }
            with self.subTest(schema=identity.schema, valid="build-info"):
                self.assertEqual(
                    runtime.build_info_from_mapping(
                        build_info,
                        context="valid build-info",
                    ).as_dict(),
                    build_info,
                )

            for missing in ("nokv_version", "binary_size_bytes"):
                with self.subTest(schema=identity.schema, missing=missing):
                    malformed = dict(build_info)
                    del malformed[missing]
                    with self.assertRaisesRegex(ValueError, "missing.*fields"):
                        runtime.build_info_from_mapping(
                            malformed,
                            context="incomplete build-info",
                        )

    def test_generate_build_info_prints_schema_specific_holt_identity(self):
        v2 = runtime.SourceIdentity(
            schema="nokv.build_info.v2",
            nokv_version="0.1.0",
            nokv_git_commit=NOKV_REVISION,
            source_dirty=False,
            cargo_lock_sha256="c" * 64,
            holt_crate_version="0.8.2",
            holt_registry="registry+https://github.com/rust-lang/crates.io-index",
            holt_checksum_sha256="d" * 64,
        )
        for identity, expected, absent in (
            (
                IDENTITY,
                f"holt_revision: {HOLT_REVISION}",
                "holt_registry:",
            ),
            (
                v2,
                (
                    "holt_registry: "
                    "registry+https://github.com/rust-lang/crates.io-index\n"
                    f"holt_checksum_sha256: {'d' * 64}"
                ),
                "holt_revision:",
            ),
        ):
            with self.subTest(schema=identity.schema):
                output = io.StringIO()
                with (
                    mock.patch.object(generator, "source_identity", return_value=identity),
                    mock.patch.object(generator, "write_build_info", return_value=True),
                    contextlib.redirect_stdout(output),
                ):
                    result = generator.main(
                        [
                            "--source-root",
                            ".",
                            "--revision",
                            NOKV_REVISION,
                            "--nokv-bin",
                            "/tmp/nokv",
                            "--output",
                            "/tmp/build-info.json",
                        ]
                    )
                self.assertEqual(result, 0)
                self.assertIn(expected, output.getvalue())
                self.assertNotIn(absent, output.getvalue())


if __name__ == "__main__":
    unittest.main()
