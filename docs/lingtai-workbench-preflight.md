<!--
Copyright 2024-2026 The NoKV Authors.
SPDX-License-Identifier: Apache-2.0
-->

# Configure and Update NoKV for LingTai Workbench

This is the user guide for connecting one existing LingTai Agent to NoKV and
keeping that connection current. The supported user path is a clean NoKV source
checkout plus `scripts/lingtai-workbench/up.sh`. Brew distribution is not yet
available.

## Know Which MCP Surface You Need

NoKV has three MCP profiles:

- The generic Agent profile exposes seven read-only namespace tools: `ls`,
  `stat`, `catalog`, `read`, `aggregate`, `find`, and `grep`. It is suitable for
  a general MCP client, but it is not the LingTai workbench integration.
- The stable `workbench` profile exposes exactly 18 `workbench_*` tools, including
  writes, checkpoints, indexed queries, and durable `workbench_restore`. It is
  jailed below `/agents/{agent_id}/wb` and is registered in LingTai as
  `nokv-workbench`.
- The opt-in `lingtai` profile preserves those 18 tools in the same order and
  adds a role-scoped Shared Workspace surface. A reader receives
  `workspace_list` and `workspace_read` (20 tools total); a writer also receives
  `workspace_put_file`, `workspace_edit`, and `workspace_append` (23 tools
  total). It requires an explicit workspace id, actor id, and canonical launcher
  grant. None is inferred from a path, project, current directory, or Agent name.

The supported path defaults to `workbench`. Each profile has an independent,
checked-in exact contract; selecting `lingtai` does not relax or replace the
frozen Workbench contract. Restore is advertised only when every metadata owner
that can serve the selected Agent root supports `restore_to_fork_v1`; setup
fails closed if the fleet is mixed or the selected schema or tools/list order
differs.

## Before the First Configuration

Prepare all of the following:

1. A LingTai project that already contains at least one Agent directory and
   `init.json` below `/path/to/project/.lingtai/`. The NoKV helper registers MCP
   for an Agent; it does not create the Agent.
2. A LingTai TUI runtime that already contains the `nokv-workbench` intrinsic
   skill and a kernel that supports the optional MCP
   `template_arg_indices` contract described below.
3. A clean NoKV source checkout on the revision you intend to deploy.
4. `python3`, `git`, Cargo/Rust, `lsof`, and the AWS CLI. Docker is also needed
   when the configured RustFS endpoint is not already running.
5. A persistent metadata directory. Do not use a disposable checkout, `/tmp`,
   or a directory removed by `cargo clean`. Keep using the same directory on
   every update.

Verify the LingTai skill before changing NoKV:

```bash
~/.lingtai-tui/runtime/venv/bin/python - <<'PY'
from pathlib import Path
import lingtai.intrinsic_skills as skills

root = Path(skills.__file__).parent
print((root / "nokv-workbench" / "SKILL.md").exists())
PY
```

The command must print `True`. Install a workbench-enabled LingTai release
before continuing if it does not.

### Kernel compatibility gate

The minimum compatible LingTai kernel is one that validates
`template_arg_indices` in both `mcp_registry.jsonl` and `init.json`, expands
placeholders only in the selected argument positions on initial activation and
retry/refresh, and preserves the legacy expand-all behavior only when the field
is absent. Until a LingTai release explicitly advertises that capability, do
not infer compatibility from a package version alone. Before the first update,
run the source-bound companion gate against the exact kernel and NoKV checkouts
you intend to deploy:

```bash
cd /path/to/lingtai-kernel
uv run --with pytest python scripts/run_nokv_lingtai_mcp_smoke.py \
  --nokv-source /path/to/NoKV \
  --junit-xml /tmp/nokv-lingtai-mcp-smoke.xml
```

The NoKV writer dynamically finds the value after `--workbench-root`. It lists
only that argv index when the root contains `{agent_id}`, `{agent_address}`, or
`{agent_dir}`; otherwise it writes an explicit empty list. Workspace ids, actor
ids, grants, endpoints, and every other argv value remain literal even if their
opaque bytes happen to contain the same brace-delimited text. Do not run this
writer with an older kernel that ignores the field.

Choose stable local storage once. This example keeps both Holt metadata and
local RustFS objects outside the source checkout:

```bash
export LINGTAI_WORKBENCH_META_DIR="$HOME/.local/share/nokv/lingtai-workbench/meta"
export LINGTAI_WORKBENCH_RUSTFS_DATA_DIR="$HOME/.local/share/nokv/lingtai-workbench/rustfs"
```

The guarded one-command path intentionally uses its dedicated local RustFS
credentials. Custom credential and secret-manager integration is outside this
helper and requires a separately reviewed deployment; it is not a supported
`up.sh` override. The metadata directory and object-store identity are durable
deployment state, so do not silently point an update at a new empty location.
All ordinary LingTai paths and `.lingtai/` runtime state remain local-first.
Profile selection changes only the explicit NoKV MCP launch and its durable
Agent-local registration/lock identity.

## First Configuration

From the NoKV checkout:

```bash
git switch main
git pull --ff-only

LINGTAI_WORKBENCH_PROJECT=/path/to/lingtai-project \
LINGTAI_WORKBENCH_META_DIR="$HOME/.local/share/nokv/lingtai-workbench/meta" \
LINGTAI_WORKBENCH_RUSTFS_DATA_DIR="$HOME/.local/share/nokv/lingtai-workbench/rustfs" \
./scripts/lingtai-workbench/up.sh
```

`up.sh` accepts no command-line arguments. Configure it only with environment
variables. On a fresh installation, or when the durable lock is already
`workbench`, omitting `LINGTAI_WORKBENCH_MCP_PROFILE` selects the implicit
`workbench` default. Omission is not rollback syntax: an existing `lingtai`
lock fails before staging or mutation unless `workbench` is selected
explicitly. The script performs the following guarded handoff:

- resolves one exact Agent before preflight, then pins the canonical `.lingtai`
  and Agent directory identities through preflight, candidate probe, sync, and
  the post-commit check; descriptor-anchored state I/O cannot be retargeted by
  rename/recreation of the same directory name;
- validates the pinned LingTai Agent files and Workbench skill;
- builds NoKV with the locked `Cargo.lock` and records the exact Holt source
  identity;
- stages an immutable binary under the LingTai project by NoKV commit and
  binary SHA-256;
- probes the candidate's exact ordered contract for the selected profile
  against the current metadata endpoint before replacing a running server,
  then starts or verifies RustFS and the helper-managed NoKV metadata server;
- rechecks the selected Agent's concrete root after the server handoff;
- updates `mcp_registry.jsonl`, `init.json`, and the NoKV lock under a per-Agent
  lock and recovery journal, but only if their exact post-preflight presence
  and bytes still match the pinned non-secret state digest;
- runs a lock-driven read-only `--check` of the durable files, immutable binary,
  launch identity, and selected live contract before reporting success.

For the current registry-sourced Holt checkout, the artifact-bound identity is
`nokv.build_info.v2`: it records the exact crates.io registry identifier, Holt
crate version, and Holt package checksum from `Cargo.lock`. Existing
`nokv.build_info.v1` artifacts and locks that identify a git-sourced Holt
revision remain readable and checkable; the helper does not rewrite them into
a fabricated registry identity.

New synchronization writes `nokv.lingtai.workbench_lock.v2`, which binds both
the exact ordered argv and `template_arg_indices` in one launch-semantics
digest. Existing v1 locks remain readable under their legacy expand-all
semantics. A v1 operational `--check` succeeds only when no non-Workbench-root
argument contains a supported Agent token; otherwise it fails before launching
the MCP without printing the argument. Do not rerun unchanged normal sync: it
also fails before Agent mutation rather than silently converting an old
expand-all token into a v2 literal. Review and provide concrete replacements
for every affected option. For `lingtai` workspace or actor identities, supply
the complete concrete replacement tuple and a canonical reissued grant bound
to that tuple; never derive it by generically expanding the old values. Normal
sync then upgrades the registration, init specification, and lock together to
v2; a read-only `--check` never performs that migration.

By default the helper selects, in order, the only running coordinator, the only
coordinator, or the only Agent. It performs that selection once; a later
`.status.json` change cannot retarget any phase of the same run. The output
prints the pinned directory name. Do not set an Agent name unless selection is
ambiguous. If the error lists multiple candidates, rerun with the complete
directory name exactly as printed:

```bash
LINGTAI_WORKBENCH_PROJECT=/path/to/lingtai-project \
LINGTAI_WORKBENCH_AGENT='coordinator(codex-gpt-5.4)' \
LINGTAI_WORKBENCH_META_DIR="$HOME/.local/share/nokv/lingtai-workbench/meta" \
LINGTAI_WORKBENCH_RUSTFS_DATA_DIR="$HOME/.local/share/nokv/lingtai-workbench/rustfs" \
./scripts/lingtai-workbench/up.sh
```

After a successful handoff, run this command in the selected LingTai Agent:

```text
/refresh
```

This restarts the MCP stdio child with the newly locked runtime.

## Activate Shared Workspace

Use `lingtai` only when the trusted LingTai launcher has issued a canonical,
unexpired `nokv.lingtai.workspace_grant.v1` grant for the exact workspace and
actor. Do not mint a substitute, derive either identity from the Agent name, or
copy a grant issued for another tuple. Load the grant from the trusted launcher
without printing it, then run the same supported entry point:

```bash
export LINGTAI_WORKBENCH_MCP_PROFILE=lingtai
export LINGTAI_WORKBENCH_WORKSPACE_ID='<exact-workspace-id>'
export LINGTAI_WORKBENCH_WORKSPACE_ACTOR_ID='<exact-actor-id>'
export LINGTAI_WORKBENCH_WORKSPACE_GRANT='<canonical-base64url-grant>'

LINGTAI_WORKBENCH_PROJECT=/path/to/lingtai-project \
LINGTAI_WORKBENCH_META_DIR="$HOME/.local/share/nokv/lingtai-workbench/meta" \
LINGTAI_WORKBENCH_RUSTFS_DATA_DIR="$HOME/.local/share/nokv/lingtai-workbench/rustfs" \
./scripts/lingtai-workbench/up.sh
```

The script validates the same tuple during preflight, any candidate probe, and
final sync before it reports success. Its success output prints the locked
profile and SHA-256 fingerprints of the exact workspace-id bytes, actor-id
bytes, and decoded canonical grant JSON; the grant fingerprint exactly matches
the durable lock and no raw tuple is printed. Compare those fingerprints with
the launcher record, then run `/refresh` in the selected Agent. The grant role
selects the independent checked-in 20-tool reader or 23-tool writer contract.
The explicit `workbench -> lingtai` profile switch does not need a
contract-accept override. Changing the grant role while staying on `lingtai`
changes that profile's ordered contract; review it and use only the exact
acceptance digest printed by the failure.

The supported path rejects `LINGTAI_WORKBENCH_WORKSPACE_DEV_MEMBERSHIP` even
when explicitly set to empty. Development membership is not a credential and
cannot be used for deployment.
`workbench` rejects the three tuple variables even when one is explicitly set
to an empty string; unset them instead of blanking them.

## Daily Update

Use the same path for every NoKV update. Preserve the metadata directory,
object store, and project used during the first configuration. `up.sh` pins one
Agent per run; if status changes could alter automatic selection between runs,
set `LINGTAI_WORKBENCH_AGENT` to the directory name printed by the prior run:

```bash
cd /path/to/NoKV
git switch main
git pull --ff-only

LINGTAI_WORKBENCH_PROJECT=/path/to/lingtai-project \
LINGTAI_WORKBENCH_META_DIR="$HOME/.local/share/nokv/lingtai-workbench/meta" \
LINGTAI_WORKBENCH_RUSTFS_DATA_DIR="$HOME/.local/share/nokv/lingtai-workbench/rustfs" \
./scripts/lingtai-workbench/up.sh
```

Run `/refresh` only after the script reports success. The Agent registration
uses the immutable staged copy, not mutable `target/release/nokv`, so a later
Cargo build cannot silently change the launched executable.

For an installed `lingtai` profile, explicitly set
`LINGTAI_WORKBENCH_MCP_PROFILE=lingtai` and re-supply the exact workspace and
actor plus a currently valid canonical grant on every `up.sh` run. If the grant
has expired or was revoked, obtain a replacement from the trusted launcher;
the script does not invent, refresh, or substitute credentials. Omitting the
profile does not silently downgrade the existing lock.

## Read-Only Check

Check the installed state without writing any file:

```bash
python3 ./scripts/lingtai-workbench/sync_workbench_mcp.py \
  --project /path/to/lingtai-project \
  --agent '<directory-name-printed-by-up.sh>' \
  --check
```

The check validates the lock, immutable binary and build identity, LingTai
registry and `init.json`, launch arguments (including the locked tuple and grant
digest for `lingtai`), and the selected live ordered contract. It reconstructs
profile and tuple only from the lock, so do not pass profile or tuple overrides
to `--check`. For a check associated with an `up.sh` run, pass the exact Agent
directory name printed as pinned by that run, as above. A standalone check may
omit `--agent` and perform automatic selection at that later time, but a status
change could make that a different Agent. If the project is ambiguous, an
exact `--agent` directory name is mandatory:

```bash
python3 ./scripts/lingtai-workbench/sync_workbench_mcp.py \
  --project /path/to/lingtai-project \
  --agent 'coordinator(codex-gpt-5.4)' \
  --check
```

## Handle a Failed Update

Do not hand-edit the LingTai MCP files. Before the script prints
`configuration committed`, a failed handoff has not intentionally committed
the new three-file state; fix the reported cause and rerun the same `up.sh`
command. The sync transaction recovers an interrupted Agent-file update on the
next normal run. Do not run `/refresh` for that failure.

The exact diagnostic
`configuration committed but post-commit verification failed; not rolled back`
has a different boundary: registry, init, and lock were already committed and
the transaction journal was removed. The old configuration was not restored.
Do not assume the previous profile or launch arguments are still durable, and
do not run `/refresh` until verification succeeds. Fix the reported runtime or
contract problem, run lock-driven `--check` with the pinned `--agent`, and then
rerun the same explicit `up.sh` command if needed.

Common failures:

- **No Agent or ambiguous Agent:** create the Agent first, or set
  `LINGTAI_WORKBENCH_AGENT` to one complete directory name from the error.
- **Missing `nokv-workbench` skill:** update the LingTai runtime; registering
  NoKV cannot install or patch the skill.
- **Dirty NoKV checkout:** commit or stash the changes. Dirty builds are for
  explicit maintainer testing, not a downstream update.
- **Agent configuration changed after preflight:** another sync committed newer
  registry/init/lock bytes while this run was building or probing. Do not force
  the stale update; rerun `up.sh` so preflight starts from the new state.
- **Port already owned by an unknown process:** inspect it with
  `lsof -nP -iTCP@127.0.0.1:7799 -sTCP:LISTEN`. The helper deliberately refuses
  to stop a server it cannot prove it owns.
- **macOS reports that exact process argv cannot be proved:** keep the NoKV,
  LingTai project, metadata, and local RustFS paths free of whitespace. The
  managed-server gate fails closed when `ps` cannot represent an argument
  unambiguously.
- **RustFS or bucket failure:** verify the configured endpoint with the AWS CLI
  and inspect `docker logs lingtai-workbench-rustfs` for the default local
  container.
- **Selected profile contract changed:** review the reported canonical schema
  and tools/list-order change for that same profile.
  The error prints the exact new SHA-256. Accept only that reviewed digest:

  ```bash
  LINGTAI_WORKBENCH_ACCEPT_CONTRACT_SHA256=<new-digest-from-error> \
  LINGTAI_WORKBENCH_PROJECT=/path/to/lingtai-project \
  LINGTAI_WORKBENCH_META_DIR="$HOME/.local/share/nokv/lingtai-workbench/meta" \
  LINGTAI_WORKBENCH_RUSTFS_DATA_DIR="$HOME/.local/share/nokv/lingtai-workbench/rustfs" \
  ./scripts/lingtai-workbench/up.sh
  ```

  This is not a Boolean bypass: any other or later digest still fails. Normal
  switching between the checked-in `workbench` and `lingtai` contracts does not
  use this override.
- **Workspace tuple or grant rejected:** obtain the exact tuple and a new
  canonical, unexpired grant from the trusted launcher. Do not edit registry,
  init, lock, or grant bytes by hand.

Use these files when diagnosing a failure:

- server log, by default:
  `/path/to/NoKV/target/lingtai-workbench/nokv-server.log`;
- selected Agent lock:
  `/path/to/lingtai-project/.lingtai/<agent>/nokv-workbench.lock.json`;
- interrupted transaction marker, when present:
  `/path/to/lingtai-project/.lingtai/<agent>/.nokv-workbench.transaction.json`.

Do not delete an interrupted transaction marker by hand; rerun the normal
update so the helper can recover it.

## Grant Rotation, Revocation, and Profile Rollback

A running `lingtai` Provider checks expiry on every `tools/list` and workspace
tool call. After expiry it stops advertising Shared Workspace tools and direct
calls fail before NoKV access. There is no in-process configuration watch: a
launcher revocation or replacement becomes durable only after the same
`up.sh` transaction succeeds and the old MCP child is terminated by `/refresh`.

To rotate or restore access, rerun the Shared Workspace activation command with
the same exact workspace/actor and the new canonical grant, run `--check`, then
run `/refresh`. A same-role rotation keeps the selected contract; a reader ↔
writer role change follows the exact same-profile acceptance-digest gate above.
To revoke Shared Workspace access locally or roll back to the stable profile,
use the same supported path, explicitly select `workbench`, and remove every
tuple variable. Merely omitting `LINGTAI_WORKBENCH_MCP_PROFILE` does not
authorize a rollback from an existing `lingtai` lock:

```bash
env \
  -u LINGTAI_WORKBENCH_WORKSPACE_ID \
  -u LINGTAI_WORKBENCH_WORKSPACE_ACTOR_ID \
  -u LINGTAI_WORKBENCH_WORKSPACE_GRANT \
  LINGTAI_WORKBENCH_MCP_PROFILE=workbench \
  LINGTAI_WORKBENCH_PROJECT=/path/to/lingtai-project \
  LINGTAI_WORKBENCH_META_DIR="$HOME/.local/share/nokv/lingtai-workbench/meta" \
  LINGTAI_WORKBENCH_RUSTFS_DATA_DIR="$HOME/.local/share/nokv/lingtai-workbench/rustfs" \
  ./scripts/lingtai-workbench/up.sh

python3 ./scripts/lingtai-workbench/sync_workbench_mcp.py \
  --project /path/to/lingtai-project \
  --agent '<directory-name-printed-by-up.sh>' \
  --check
```

After both commands succeed, run `/refresh`. The existing three-file journal
updates or restores registry, init, and lock as one transaction. Profile
rollback removes the workspace launch arguments and lock fields; it does not
delete Shared Workspace data, metadata, object bodies, snapshots, or any other
LingTai local state. Re-enabling `lingtai` later with the same workspace identity
and a valid grant exposes the retained data again.

## Downgrade and Recovery Boundary

The profile rollback above changes launch configuration, not stored data. The
immutable runtime directory and lock protect binary identity; they are not a
general binary rollback manager. Prefer fixing a binary issue and moving
forward to a known-good NoKV main revision.

Do not roll the LingTai kernel back below the `template_arg_indices` capability
while a v2 `lingtai` registration is installed. An older kernel expands every
placeholder-looking argument and can therefore rewrite a valid opaque
workspace or actor id while the grant remains bound to the original bytes. If
an old-kernel rollback is unavoidable, keep the compatible kernel running,
first use the explicit `lingtai -> workbench` rollback above, complete
`--check` and `/refresh`, verify from the v2 lock that no argument other than
the selected Workbench root contains a supported Agent token, and only then
change the kernel. That sequence removes the opaque workspace/actor arguments
before the old expand-all implementation can see them. The ability to read a
historical v1 lock is migration support; it is not evidence that an old kernel
is safe with a new v2 registration. For the same reason, an unsafe v1 lock can
be read by normal sync for migration but unchanged unsafe values are rejected.
It cannot receive an operationally valid `--check` result until reviewed
concrete replacements have been committed through the three-file transaction.

After the first durable restore operation activates `restore_to_fork_v1`, the
persistent metadata contains an active marker and allocator downgrade fence.
Never start a pre-restore NoKV metadata binary against that metadata directory.
A safe downgrade requires disabling restore routing, globally stopping or
fencing restore writers, using the typed drain procedure, running a clean full
fsck, and creating a fresh metadata checkpoint. This is an operator procedure,
not a normal LingTai update; see [Architecture](architecture.md) and involve the
NoKV maintainers.

For script internals, Release artifact identity, and manual layer-by-layer
diagnostics, use the
[LingTai Workbench maintainer reference](../scripts/lingtai-workbench/README.md).
