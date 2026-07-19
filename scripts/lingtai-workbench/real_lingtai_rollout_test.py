#!/usr/bin/env python3
# Copyright 2024-2026 The NoKV Authors.
# SPDX-License-Identifier: Apache-2.0

"""Fail-closed real-binary gate for the experimental LingTai MCP rollout.

This is a standalone acceptance runner, not an environment-skipped unit test.
It builds and stages the explicit NoKV checkout through the supported sync CLI,
starts that exact staged binary, validates the real reader ``tools/list``
against the checked-in contract, performs a real Agent sync, and then runs the
read-only ``--check`` path.

The workload does not read or write object bodies.  Its temporary metadata
server therefore uses an intentionally unavailable loopback RustFS endpoint,
disables metadata checkpoint archiving, and delays background object GC.  Any
unexpected object-store dependency still fails closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
SYNC_SCRIPT = SCRIPT_DIR / "sync_workbench_mcp.py"
CONTRACT_ASSET = SCRIPT_DIR / "lingtai_contract_schema.json"

AGENT_NAME = "coordinator"
WORKBENCH_ROOT_TEMPLATE = "/agents/{agent_id}/wb"
CONCRETE_WORKBENCH_ROOT = "/agents/coordinator/wb"
WORKSPACE_ID = "rollout-live-workspace"
WORKSPACE_ACTOR_ID = "rollout-live-reader"
GRANT_ID = "rollout-live-reader"
EXPECTED_READER_TOOL_COUNT = 20
EXPECTED_READER_RAW_CONTRACT_SHA256 = (
    "e008fc0a776c3348ec0ddae3db9eebc01ea37eed3b723a86004eae110d94fc2f"
)
SERVER_GC_INTERVAL_MS = "3600000"
PROCESS_TERMINATE_TIMEOUT_SECONDS = 5.0
PROCESS_KILL_TIMEOUT_SECONDS = 5.0


class AcceptanceError(RuntimeError):
    """The real rollout boundary did not satisfy its acceptance contract."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def tail(path: Path, lines: int = 80) -> str:
    try:
        values = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return "<server log was not created>"
    except OSError as error:
        return f"<server log could not be read: {error}>"
    return "\n".join(values[-lines:]) or "<server log is empty>"


def output_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def command_detail(stdout: str | None, stderr: str | None) -> str:
    values = []
    if stdout and stdout.strip():
        values.append(f"stdout:\n{stdout.strip()}")
    if stderr and stderr.strip():
        values.append(f"stderr:\n{stderr.strip()}")
    return "\n".join(values)


def signal_process_group(
    pgid: int,
    selected_signal: signal.Signals,
) -> str | None:
    try:
        os.killpg(pgid, selected_signal)
    except ProcessLookupError:
        return None
    except OSError as error:
        return f"could not send {selected_signal.name} to process group: {error}"
    return None


def process_group_status(pgid: int) -> tuple[bool, str | None]:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False, None
    except PermissionError:
        return True, None
    except OSError as error:
        return True, f"could not probe process group {pgid}: {error}"
    return True, None


def wait_process_group_exit(
    pgid: int,
    *,
    timeout: float,
) -> tuple[bool, str | None]:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        alive, error = process_group_status(pgid)
        if error is not None:
            return False, error
        if not alive:
            return True, None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, None
        time.sleep(min(0.05, remaining))


def remaining_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def cleanup_communicate(
    process: subprocess.Popen[str],
    *,
    timeout: float,
) -> tuple[str, str, bool, str | None]:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return stdout or "", stderr or "", True, None
    except subprocess.TimeoutExpired as error:
        return output_text(error.stdout), output_text(error.stderr), False, None
    except BaseException as error:
        return (
            "",
            "",
            False,
            "could not collect process-group output during cleanup: "
            f"{type(error).__name__}: {error}",
        )


def collect_after_timeout(
    process: subprocess.Popen[str],
    *,
    cleanup_timeout: float,
) -> tuple[str, str, str | None]:
    cleanup_errors: list[str] = []
    # The leader may exit before a descendant.  Retain the session PGID and
    # probe it independently instead of treating communicate() as group exit.
    pgid = process.pid
    term_deadline = time.monotonic() + cleanup_timeout
    term_error = signal_process_group(pgid, signal.SIGTERM)
    if term_error is not None:
        cleanup_errors.append(term_error)
    stdout = ""
    stderr = ""
    stdout, stderr, communication_complete, communicate_error = cleanup_communicate(
        process,
        timeout=remaining_seconds(term_deadline),
    )
    if communicate_error is not None:
        cleanup_errors.append(communicate_error)

    term_gone, probe_error = wait_process_group_exit(
        pgid,
        timeout=remaining_seconds(term_deadline),
    )
    if probe_error is not None:
        cleanup_errors.append(probe_error)

    kill_deadline = time.monotonic() + PROCESS_KILL_TIMEOUT_SECONDS
    if not term_gone:
        kill_error = signal_process_group(pgid, signal.SIGKILL)
        if kill_error is not None:
            cleanup_errors.append(kill_error)
    if not communication_complete:
        final_stdout, final_stderr, communication_complete, communicate_error = (
            cleanup_communicate(
                process,
                timeout=remaining_seconds(kill_deadline),
            )
        )
        stdout = final_stdout or stdout
        stderr = final_stderr or stderr
        if communicate_error is not None:
            cleanup_errors.append(communicate_error)
        if not communication_complete:
            cleanup_errors.append("process leader or output pipes survived SIGKILL")

    group_gone, probe_error = wait_process_group_exit(
        pgid,
        timeout=remaining_seconds(kill_deadline),
    )
    if probe_error is not None:
        cleanup_errors.append(probe_error)
    if not group_gone:
        cleanup_errors.append("process group survived bounded SIGTERM/SIGKILL")
    return stdout, stderr, "; ".join(cleanup_errors) or None


def guarded_collect_after_timeout(
    process: subprocess.Popen[str],
    *,
    cleanup_timeout: float,
) -> tuple[str, str, str | None]:
    try:
        return collect_after_timeout(process, cleanup_timeout=cleanup_timeout)
    except BaseException as error:
        errors = [
            "unexpected process-group cleanup error: "
            f"{type(error).__name__}: {error}"
        ]
        try:
            kill_error = signal_process_group(process.pid, signal.SIGKILL)
            if kill_error is not None:
                errors.append(kill_error)
        except BaseException as kill_error:
            errors.append(
                "emergency SIGKILL failed: "
                f"{type(kill_error).__name__}: {kill_error}"
            )
        stdout, stderr, complete, communicate_error = cleanup_communicate(
            process,
            timeout=PROCESS_KILL_TIMEOUT_SECONDS,
        )
        if communicate_error is not None:
            errors.append(communicate_error)
        if not complete:
            errors.append("process leader or output pipes survived emergency SIGKILL")
        try:
            group_gone, probe_error = wait_process_group_exit(
                process.pid,
                timeout=PROCESS_KILL_TIMEOUT_SECONDS,
            )
            if probe_error is not None:
                errors.append(probe_error)
            if not group_gone:
                errors.append("process group survived emergency SIGKILL")
        except BaseException as probe_error:
            errors.append(
                "emergency process-group probe failed: "
                f"{type(probe_error).__name__}: {probe_error}"
            )
        return stdout, stderr, "; ".join(errors)


def attach_exception_diagnostics(
    error: BaseException,
    *,
    stdout: str,
    stderr: str,
    cleanup_error: str | None,
) -> None:
    detail = command_detail(stdout, stderr)
    try:
        setattr(error, "rollout_command_detail", detail)
        setattr(error, "rollout_cleanup_error", cleanup_error)
    except (AttributeError, TypeError):
        pass


def run_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    label: str,
    input_text: str | None = None,
    cleanup_timeout: float = PROCESS_TERMINATE_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise AcceptanceError(f"could not start {label}: {error}") from error
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        stdout, stderr, cleanup_error = guarded_collect_after_timeout(
            process,
            cleanup_timeout=cleanup_timeout,
        )
        detail = command_detail(stdout, stderr)
        message = f"{label} timed out after {timeout:g}s"
        if cleanup_error is not None:
            message += f"; timeout cleanup failed: {cleanup_error}"
        if detail:
            message += f"\n{detail}"
        raise AcceptanceError(message) from error
    except BaseException as error:
        stdout, stderr, cleanup_error = guarded_collect_after_timeout(
            process,
            cleanup_timeout=cleanup_timeout,
        )
        attach_exception_diagnostics(
            error,
            stdout=stdout,
            stderr=stderr,
            cleanup_error=cleanup_error,
        )
        raise
    completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if completed.returncode != 0:
        cleanup_stdout, cleanup_stderr, cleanup_error = guarded_collect_after_timeout(
            process,
            cleanup_timeout=cleanup_timeout,
        )
        completed.stdout = cleanup_stdout or completed.stdout
        completed.stderr = cleanup_stderr or completed.stderr
        detail = command_detail(completed.stdout, completed.stderr)
        message = f"{label} exited with {completed.returncode}"
        if cleanup_error is not None:
            message += f"; process-group cleanup failed: {cleanup_error}"
        if detail:
            message += f"\n{detail}"
        raise AcceptanceError(message)
    return completed


def reserve_loopback_port() -> tuple[socket.socket, int]:
    reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reservation.bind(("127.0.0.1", 0))
    return reservation, int(reservation.getsockname()[1])


def wait_until_ready(
    process: subprocess.Popen[str],
    *,
    server_bind: str,
    server_log: Path,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://{server_bind}/readyz"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AcceptanceError(
                "staged NoKV server exited during startup\n" + tail(server_log)
            )
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                body = response.read().decode("utf-8", errors="replace").strip()
                if response.status == 200 and body == "ready":
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    raise AcceptanceError(
        f"staged NoKV server did not become ready within {timeout:g}s\n"
        + tail(server_log)
    )


def stop_server(
    process: subprocess.Popen[str] | None,
    log_handle: Any,
    *,
    terminate_timeout: float = 10,
) -> str | None:
    errors: list[str] = []
    if process is not None:
        pgid = process.pid
        try:
            process.poll()
        except (OSError, ValueError) as error:
            errors.append(f"could not inspect NoKV server state: {error}")
        group_alive, probe_error = process_group_status(pgid)
        if probe_error is not None:
            errors.append(probe_error)
        if group_alive:
            term_deadline = time.monotonic() + terminate_timeout
            term_error = signal_process_group(pgid, signal.SIGTERM)
            if term_error is not None:
                errors.append(term_error)
            try:
                process.wait(timeout=remaining_seconds(term_deadline))
            except subprocess.TimeoutExpired:
                pass
            except (OSError, ValueError) as error:
                errors.append(f"could not wait for NoKV server leader: {error}")
            term_gone, probe_error = wait_process_group_exit(
                pgid,
                timeout=remaining_seconds(term_deadline),
            )
            if probe_error is not None:
                errors.append(probe_error)
            if not term_gone:
                kill_deadline = time.monotonic() + PROCESS_KILL_TIMEOUT_SECONDS
                kill_error = signal_process_group(pgid, signal.SIGKILL)
                if kill_error is not None:
                    errors.append(kill_error)
                try:
                    process.wait(timeout=remaining_seconds(kill_deadline))
                except subprocess.TimeoutExpired:
                    pass
                except (OSError, ValueError) as error:
                    errors.append(
                        f"could not wait for killed NoKV server leader: {error}"
                    )
                group_gone, probe_error = wait_process_group_exit(
                    pgid,
                    timeout=remaining_seconds(kill_deadline),
                )
                if probe_error is not None:
                    errors.append(probe_error)
                if not group_gone:
                    errors.append(
                        f"NoKV server process group {pgid} survived terminate and kill"
                    )
        try:
            if process.poll() is None:
                errors.append(
                    f"NoKV server leader pid {process.pid} is still alive after cleanup"
                )
        except (OSError, ValueError) as error:
            errors.append(f"could not confirm NoKV server exit: {error}")
    if log_handle is not None:
        try:
            log_handle.flush()
        except (OSError, ValueError) as error:
            errors.append(f"could not flush NoKV server log: {error}")
        try:
            log_handle.close()
        except (OSError, ValueError) as error:
            errors.append(f"could not close NoKV server log: {error}")
    return "; ".join(errors) or None


def append_cleanup_error(current: str | None, new: str) -> str:
    return f"{current}; {new}" if current else new


def parse_single_json_line(stdout: str, *, label: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise AcceptanceError(
            f"{label} must return exactly one non-empty JSON line, got {len(lines)}"
        )
    try:
        value = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise AcceptanceError(f"{label} returned invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise AcceptanceError(f"{label} response must be a JSON object")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--nokv-source",
        required=True,
        type=Path,
        help="Explicit NoKV checkout to build, stage, launch, sync, and check.",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow an explicitly dirty source identity for development only.",
    )
    parser.add_argument("--command-timeout", type=float, default=1800)
    parser.add_argument("--tool-timeout", type=float, default=30)
    parser.add_argument("--startup-timeout", type=float, default=60)
    parser.add_argument(
        "--keep-state",
        action="store_true",
        help="Keep the temporary project, metadata, and server log after cleanup.",
    )
    args = parser.parse_args(argv)
    for name in ("command_timeout", "tool_timeout", "startup_timeout"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    return args


def make_state_dir() -> Path:
    # macOS commonly reports its temporary root through /var even though the
    # canonical path is /private/var.  Canonicalize once so staged paths
    # returned by the lower-level sync CLI have the same ancestry identity as
    # the runner's project path.
    return Path(tempfile.mkdtemp(prefix="nokv-lingtai-rollout-live-")).resolve()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = args.nokv_source.expanduser().resolve()
    state_dir = make_state_dir()
    server_log = state_dir / "nokv-server.log"
    process: subprocess.Popen[str] | None = None
    log_handle: Any = None
    server_port_guard: socket.socket | None = None
    dummy_endpoint_guard: socket.socket | None = None
    failure: BaseException | None = None
    cleanup_error: str | None = None
    summary: dict[str, Any] | None = None

    try:
        require(source == REPO_ROOT, "--nokv-source must name this runner's checkout")
        require((source / "Cargo.toml").is_file(), f"not a NoKV checkout: {source}")
        require((source / ".git").exists(), f"NoKV source is not a Git checkout: {source}")
        require(SYNC_SCRIPT.is_file(), f"missing supported sync CLI: {SYNC_SCRIPT}")
        require(CONTRACT_ASSET.is_file(), f"missing LingTai contract: {CONTRACT_ASSET}")
        require(shutil.which("cargo") is not None, "cargo is required")
        require(shutil.which("git") is not None, "git is required")

        contract_bytes_before = CONTRACT_ASSET.read_bytes()
        contract_asset_sha256 = sha256_bytes(contract_bytes_before)

        # Import only after resolving the real sibling asset.  The contract
        # module loads this exact file in place; no fixture copy is created.
        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))
        import install_workbench_mcp as installer
        from workbench_contract import (
            expected_profile_contract_evidence,
            extract_raw_tools,
            profile_contract_evidence,
        )
        from workspace_grant import (
            GRANT_AUDIENCE,
            GRANT_ISSUER,
            GRANT_SCHEMA,
            WorkspaceGrant,
            encode_workspace_grant,
            workspace_grant_lock_fields,
            workspace_grant_sha256,
        )

        expected = expected_profile_contract_evidence("lingtai", role="reader")
        require(
            expected.get("tool_count") == EXPECTED_READER_TOOL_COUNT,
            "checked-in reader contract does not contain exactly 20 tools",
        )
        require(
            expected.get("raw_contract_sha256")
            == EXPECTED_READER_RAW_CONTRACT_SHA256,
            "checked-in reader raw contract digest changed without updating the gate",
        )

        project = state_dir / "project"
        agent_dir = project / ".lingtai" / AGENT_NAME
        agent_dir.mkdir(parents=True)
        (agent_dir / "init.json").write_text('{"mcp": {}}\n', encoding="utf-8")

        env = os.environ.copy()
        env.update(
            {
                "AWS_ACCESS_KEY_ID": "rollout-test",
                "AWS_SECRET_ACCESS_KEY": "rollout-test",
                "AWS_DEFAULT_REGION": "us-east-1",
                "AWS_EC2_METADATA_DISABLED": "true",
            }
        )

        revision_result = run_command(
            ["git", "rev-parse", "HEAD"],
            cwd=source,
            env=env,
            timeout=30,
            label="NoKV revision lookup",
        )
        revision = revision_result.stdout.strip()
        require(
            len(revision) == 40
            and all(character in "0123456789abcdef" for character in revision),
            f"NoKV revision is not a full lowercase commit: {revision!r}",
        )

        stage_args = [
            sys.executable,
            str(SYNC_SCRIPT),
            "--stage-only",
            "--project",
            str(project),
            "--build-source",
            str(source),
            "--revision",
            revision,
            "--distribution",
            "source",
        ]
        if args.allow_dirty:
            stage_args.append("--allow-dirty")
        staged_result = run_command(
            stage_args,
            cwd=source,
            env=env,
            timeout=args.command_timeout,
            label="supported source build and staging",
        )
        staged_lines = [
            line.strip() for line in staged_result.stdout.splitlines() if line.strip()
        ]
        require(
            len(staged_lines) == 1,
            "stage-only must print exactly one staged NoKV path",
        )
        staged_nokv = Path(staged_lines[0]).expanduser().resolve()
        build_info = staged_nokv.parent / "build-info.json"
        require(staged_nokv.is_file(), f"staged NoKV binary is missing: {staged_nokv}")
        require(os.access(staged_nokv, os.X_OK), f"staged NoKV is not executable: {staged_nokv}")
        require(build_info.is_file(), f"staged build-info is missing: {build_info}")
        require(
            project in staged_nokv.parents,
            "supported staging placed NoKV outside the temporary project",
        )
        binary_sha256 = sha256_file(staged_nokv)

        server_port_guard, server_port = reserve_loopback_port()
        dummy_endpoint_guard, dummy_endpoint_port = reserve_loopback_port()
        server_bind = f"127.0.0.1:{server_port}"
        dummy_endpoint = f"http://127.0.0.1:{dummy_endpoint_port}"
        bucket = f"nokv-lingtai-rollout-{os.getpid()}-{int(time.time())}"
        meta_dir = state_dir / "meta"
        meta_dir.mkdir()
        server_command = [
            str(staged_nokv),
            "--server-bind",
            server_bind,
            "--object-backend",
            "rustfs",
            "--s3-endpoint",
            dummy_endpoint,
            "--s3-bucket",
            bucket,
            "--object-gc-interval-ms",
            SERVER_GC_INTERVAL_MS,
            "--history-gc-interval-ms",
            SERVER_GC_INTERVAL_MS,
            "--meta",
            str(meta_dir),
            "--no-metadata-checkpoint-archive",
            "serve",
        ]
        log_handle = server_log.open("w", encoding="utf-8")
        # Release the selected metadata port immediately before exec.  The
        # dummy endpoint remains bound but deliberately does not listen.
        server_port_guard.close()
        process = subprocess.Popen(
            server_command,
            cwd=source,
            env=env,
            text=True,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        wait_until_ready(
            process,
            server_bind=server_bind,
            server_log=server_log,
            timeout=args.startup_timeout,
        )

        now_unix_ms = int(time.time() * 1000)
        grant = WorkspaceGrant(
            schema=GRANT_SCHEMA,
            grant_id=GRANT_ID,
            issuer=GRANT_ISSUER,
            audience=GRANT_AUDIENCE,
            workspace_id=WORKSPACE_ID,
            actor_id=WORKSPACE_ACTOR_ID,
            role="reader",
            issued_at_unix_ms=now_unix_ms - 1_000,
            expires_at_unix_ms=now_unix_ms + 30 * 60 * 1_000,
        )
        encoded_grant = encode_workspace_grant(grant)
        grant_sha256 = workspace_grant_sha256(grant)

        live_config = installer.InstallConfig(
            nokv_bin=str(staged_nokv),
            server_bind=server_bind,
            object_backend="rustfs",
            s3_endpoint=dummy_endpoint,
            s3_bucket=bucket,
            workbench_root=CONCRETE_WORKBENCH_ROOT,
            profile="lingtai",
            workspace_id=WORKSPACE_ID,
            workspace_actor_id=WORKSPACE_ACTOR_ID,
            workspace_grant=encoded_grant,
        )
        request = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            separators=(",", ":"),
        )
        tools_result = run_command(
            [str(staged_nokv), *installer.mcp_args(live_config)],
            cwd=source,
            env=env,
            timeout=args.tool_timeout,
            label="real LingTai reader tools/list",
            input_text=request + "\n",
        )
        response = parse_single_json_line(
            tools_result.stdout, label="real LingTai reader tools/list"
        )
        tools = extract_raw_tools(response)
        live_evidence = profile_contract_evidence(tools, "lingtai", role="reader")
        require(
            len(tools) == EXPECTED_READER_TOOL_COUNT,
            "live reader did not expose exactly 20 tools",
        )
        require(
            [tool.get("name") for tool in tools] == expected.get("tool_order"),
            "live reader tools/list order differs from the checked-in contract",
        )
        require(
            live_evidence == expected,
            "live reader contract evidence differs from the checked-in contract",
        )
        require(
            live_evidence.get("raw_contract_sha256")
            == EXPECTED_READER_RAW_CONTRACT_SHA256,
            "live reader raw contract digest is not the reviewed digest",
        )

        sync_args = [
            sys.executable,
            str(SYNC_SCRIPT),
            "--project",
            str(project),
            "--agent",
            AGENT_NAME,
            "--nokv-bin",
            str(staged_nokv),
            "--build-info",
            str(build_info),
            "--revision",
            revision,
            "--expected-sha256",
            binary_sha256,
            "--distribution",
            "source",
            "--server-bind",
            server_bind,
            "--object-backend",
            "rustfs",
            "--s3-endpoint",
            dummy_endpoint,
            "--s3-bucket",
            bucket,
            "--workbench-root",
            WORKBENCH_ROOT_TEMPLATE,
            "--profile",
            "lingtai",
            "--workspace-id",
            WORKSPACE_ID,
            "--workspace-actor-id",
            WORKSPACE_ACTOR_ID,
            "--workspace-grant",
            encoded_grant,
            "--timeout-seconds",
            str(args.tool_timeout),
        ]
        if args.allow_dirty:
            sync_args.append("--allow-dirty")
        sync_result = run_command(
            sync_args,
            cwd=source,
            env=env,
            timeout=args.command_timeout,
            label="real LingTai Agent sync",
        )
        require(
            "profile: lingtai" in sync_result.stdout,
            "sync did not report the LingTai profile",
        )

        check_result = run_command(
            [
                sys.executable,
                str(SYNC_SCRIPT),
                "--project",
                str(project),
                "--agent",
                AGENT_NAME,
                "--nokv-bin",
                str(staged_nokv),
                "--timeout-seconds",
                str(args.tool_timeout),
                "--check",
            ],
            cwd=source,
            env=env,
            timeout=args.command_timeout,
            label="real LingTai lock check",
        )
        for marker in (
            "profile: lingtai",
            "lock_valid: true",
            "live_contract_valid: true",
        ):
            require(marker in check_result.stdout, f"--check did not report {marker!r}")

        lock_path = agent_dir / "nokv-workbench.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        launch = lock.get("launch")
        require(isinstance(launch, dict), "lock launch is not an object")
        require(launch.get("profile") == "lingtai", "lock profile is not lingtai")
        require(launch.get("workspace_id") == WORKSPACE_ID, "lock workspace id differs")
        require(
            launch.get("workspace_actor_id") == WORKSPACE_ACTOR_ID,
            "lock workspace actor id differs",
        )
        locked_grant = launch.get("workspace_grant")
        require(isinstance(locked_grant, dict), "lock workspace grant is not an object")
        expected_locked_grant = {
            **workspace_grant_lock_fields(grant),
            "canonical_sha256": grant_sha256,
        }
        require(locked_grant == expected_locked_grant, "lock workspace grant tuple/hash differs")
        require(lock.get("contract") == expected, "lock reader contract evidence differs")
        artifact = lock.get("artifact")
        require(isinstance(artifact, dict), "lock artifact is not an object")
        require(
            Path(str(artifact.get("command"))).resolve() == staged_nokv,
            "lock command is not the staged real NoKV binary",
        )
        require(artifact.get("sha256") == binary_sha256, "lock binary digest differs")
        require(
            not (agent_dir / ".nokv-workbench.transaction.json").exists(),
            "sync left a recovery journal behind",
        )
        require(
            CONTRACT_ASSET.read_bytes() == contract_bytes_before,
            "the checked-in LingTai contract asset changed during the live gate",
        )
        require(process.poll() is None, "NoKV server exited before final assertions")

        summary = {
            "status": "passed",
            "nokv_revision": revision,
            "binary_sha256": binary_sha256,
            "contract_asset_sha256": contract_asset_sha256,
            "profile": "lingtai",
            "role": "reader",
            "tool_count": len(tools),
            "raw_contract_sha256": live_evidence["raw_contract_sha256"],
            "contract_sha256": live_evidence["contract_sha256"],
            "workspace_id_sha256": sha256_bytes(WORKSPACE_ID.encode("utf-8")),
            "workspace_actor_id_sha256": sha256_bytes(
                WORKSPACE_ACTOR_ID.encode("utf-8")
            ),
            "workspace_grant_sha256": grant_sha256,
            "lock_valid": True,
            "live_contract_valid": True,
        }
    except (Exception, KeyboardInterrupt) as error:
        failure = error
    finally:
        try:
            cleanup_error = stop_server(process, log_handle)
        except BaseException as error:  # cleanup must not replace acceptance failure
            cleanup_error = f"unexpected NoKV server cleanup error: {error}"
        if server_port_guard is not None:
            try:
                server_port_guard.close()
            except OSError as error:
                cleanup_error = append_cleanup_error(
                    cleanup_error, f"could not close server port guard: {error}"
                )
        if dummy_endpoint_guard is not None:
            try:
                dummy_endpoint_guard.close()
            except OSError as error:
                cleanup_error = append_cleanup_error(
                    cleanup_error, f"could not close dummy endpoint guard: {error}"
                )

    if failure is not None:
        print(f"[real-lingtai-rollout] FAILED: {failure}", file=sys.stderr)
        command_detail_value = getattr(failure, "rollout_command_detail", "")
        if command_detail_value:
            print(
                "[real-lingtai-rollout] interrupted command output:\n"
                f"{command_detail_value}",
                file=sys.stderr,
            )
        command_cleanup_error = getattr(failure, "rollout_cleanup_error", None)
        if command_cleanup_error:
            print(
                "[real-lingtai-rollout] interrupted command cleanup failed: "
                f"{command_cleanup_error}",
                file=sys.stderr,
            )
        print("[real-lingtai-rollout] server log tail:", file=sys.stderr)
        print(tail(server_log), file=sys.stderr)
    if cleanup_error is not None:
        print(f"[real-lingtai-rollout] cleanup failed: {cleanup_error}", file=sys.stderr)

    if args.keep_state:
        print(f"[real-lingtai-rollout] state kept at {state_dir}", file=sys.stderr)
    else:
        try:
            shutil.rmtree(state_dir)
        except OSError as error:
            print(
                f"[real-lingtai-rollout] could not remove temporary state "
                f"{state_dir}: {error}",
                file=sys.stderr,
            )
            cleanup_error = append_cleanup_error(
                cleanup_error, f"could not remove temporary state: {error}"
            )

    if failure is not None or cleanup_error is not None:
        return 130 if isinstance(failure, KeyboardInterrupt) else 1
    assert summary is not None
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
