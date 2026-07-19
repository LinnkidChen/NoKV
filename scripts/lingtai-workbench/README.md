<!--
Copyright 2024-2026 The NoKV Authors.
SPDX-License-Identifier: Apache-2.0
-->

# LingTai Workbench Maintainer Reference

The single user-facing setup and update path is
[`docs/lingtai-workbench-preflight.md`](../../docs/lingtai-workbench-preflight.md).
Do not duplicate that tutorial here. This page documents the scripts, artifact
contract, manual diagnostics, and acceptance gates used to maintain the
NoKV-to-LingTai Workbench handoff.

These helpers target LingTai. Historical benchmark or Yanex paths must not be
used to configure the Workbench MCP.

## Script Responsibilities

| Script | Responsibility |
| --- | --- |
| `up.sh` | Environment-only orchestration for one guarded update. It resolves one exact Agent before preflight and pins it across every phase; validates one profile and, for `lingtai`, one complete workspace/actor/canonical-grant tuple before staging; starts or verifies RustFS and the helper-owned metadata server; switches the Agent registration; marks the commit boundary; and finishes with a lock-driven read-only check. It accepts no CLI arguments. |
| `sync_workbench_mcp.py` | Lower-level source build or artifact staging, content-addressed runtime selection, profile-indexed offline/live preflight, per-Agent locking, journaled registry update, and read-only lock verification. |
| `nokv_runtime.py` | NoKV/Holt/Cargo.lock identity, artifact-bound build-info parsing, SHA-256 verification, and symlink-safe immutable runtime staging. |
| `managed_nokv_server.py` | Records and verifies the helper-owned server PID, process start identity, listener ownership, binary digest, complete argv, metadata path, and object-store configuration before reuse or termination. |
| `workbench_contract.py` | Profile-indexed semantic validation and evidence. It preserves the exact ordered 18-tool Workbench surface and validates the role-specific `lingtai` prefix/suffix contract independently. |
| `workbench_contract_schema.json` | Checked-in canonical `inputSchema` and `toolOrder` snapshot owned by `workbench_mcp.rs`. |
| `lingtai_contract_schema.json` | Checked-in canonical five-tool Shared Workspace definitions, reader/writer suffix order, and exact full-profile evidence. |
| `workspace_grant.py` | Canonical launcher-grant parsing, byte-exact re-encoding, tuple/role/time validation, and stable grant evidence. |
| `real_lingtai_rollout_test.py` | Standalone, fail-closed acceptance runner that builds and stages one explicit NoKV checkout, launches that exact binary, validates the real reader profile, performs a real Agent sync, and finishes through the lock-driven `--check` path. |
| `generate_nokv_build_info.py` | Produces the source-appropriate versioned build identity for a Release or future Brew artifact. |
| `install_workbench_mcp.py` | Raw idempotent registry primitive. It intentionally performs no binary, owner, capability, or schema gate. |
| `start_rustfs.sh` | Starts or reuses the dedicated local RustFS container and creates the selected bucket. |
| `durable_restore_live_e2e.py` | Real RustFS, NoKV, LingTai reconnect, crash/replay, COW, index, and lifecycle merge gate. |

The adjacent `*_test.py` files cover the corresponding Python module or
script. They are maintainer tests, not downstream installation steps.

## Runtime and Lock Layout

The selected binary is copied before registration to:

```text
<project>/.lingtai/runtime/nokv/<nokv-commit>/<binary-sha256>/nokv
```

Its artifact-bound `build-info.json` is stored beside it. The selected Agent
then owns:

```text
<agent>/mcp_registry.jsonl
<agent>/init.json
<agent>/nokv-workbench.lock.json
<agent>/.nokv-workbench.sync.lock
<agent>/.nokv-workbench.transaction.json   # present only during/recovering a write
```

The lock records the binary digest and size, NoKV commit, `Cargo.lock` digest,
the schema-specific exact Holt source identity, selected profile, launch
arguments, the exact `template_arg_indices`, their combined launch-semantics
digest, concrete Agent root, and canonical MCP contract evidence including the
exact `tools/list` order. New writes use
`nokv.lingtai.workbench_lock.v2`. A v1 lock remains readable with its historical
expand-all semantics. Its operational `--check` fails closed when any
non-Workbench-root argument contains a supported Agent token. Normal sync also
rejects an unchanged unsafe value before Agent mutation; it upgrades all three
Agent files to v2 only after every affected option has a reviewed concrete
replacement explicitly present in the direct CLI invocation. Parser defaults,
omitted `up.sh` launch variables, and explicitly empty variables are not
migration provenance, even when the resulting value equals the desired
default. A `lingtai` identity replacement requires the complete concrete
workspace/actor tuple and a canonical reissued grant bound to it; an explicit
`--profile workbench` remains the supported removal of the old tuple. `--check`
never rewrites state. Current
`nokv.build_info.v2` identities record the crates.io registry identifier, exact
crate version, and Holt package checksum from `Cargo.lock`; legacy
`nokv.build_info.v1` identities retain the git commit and remain checkable
without fabricated v2 fields. A
`lingtai` lock also records the validated workspace and actor identities, role,
grant id, issuer/audience, issue/expiry times, and canonical grant SHA-256. It
never treats an Agent basename as workspace identity. A rebuild or package
upgrade cannot replace the registered binary in place.

The helper's default process state is below
`<NoKV checkout>/target/lingtai-workbench`. Metadata is durable product state,
not process scratch: deployments must override `LINGTAI_WORKBENCH_META_DIR` to
a persistent location and keep that location stable across updates.

The v2 writer requires a LingTai kernel that validates and honors optional
`template_arg_indices` on initial activation and retry/refresh. It dynamically
selects only the value after `--workbench-root` when that value contains a
supported Agent token; all other arguments remain literal. Until a LingTai
release explicitly advertises this capability, the source-bound companion
smoke in the user preflight guide is the minimum-version gate. Do not run a v2
`lingtai` registration on an older expand-all kernel. Before an unavoidable
kernel downgrade, use the compatible kernel to roll the profile back to
`workbench`, complete `--check` and `/refresh`, verify that no non-root argument
contains a supported Agent token, and only then change kernels.

The kernel expands tokens in the MCP command independently of argv selection.
The resolved LingTai project and immutable staged NoKV command paths must not
contain `{agent_id}`, `{agent_address}`, or `{agent_dir}`. The sync helper
rejects such a project before candidate discovery, staging, live probing, or
Agent-file mutation.

## Environment Reference

`up.sh` accepts no positional or option arguments. Its primary environment is:

| Variable | Default | Meaning |
| --- | --- | --- |
| `LINGTAI_WORKBENCH_PROJECT` | current project with `.lingtai`, then `~/lingtai-demo` | LingTai project to update. |
| `LINGTAI_WORKBENCH_AGENT` | automatic selection | Exact directory name below `.lingtai`; automatic selection occurs once before preflight and is pinned across the whole run. Set only after an ambiguity error. |
| `LINGTAI_WORKBENCH_MCP_PROFILE` | implicit `workbench` | Exact supported MCP profile: `workbench` or `lingtai`. An explicitly empty value is invalid. Omission cannot downgrade an existing `lingtai` lock; that rollback requires explicit `workbench`. |
| `LINGTAI_WORKBENCH_WORKSPACE_ID` | unset | Exact opaque Shared Workspace identity; required and non-empty only for `lingtai`. |
| `LINGTAI_WORKBENCH_WORKSPACE_ACTOR_ID` | unset | Exact opaque actor identity; required and non-empty only for `lingtai`. |
| `LINGTAI_WORKBENCH_WORKSPACE_GRANT` | unset | Canonical base64url-no-padding launcher grant bound byte-for-byte to the two identities; required only for `lingtai`. Never log the raw value. |
| `LINGTAI_WORKBENCH_WORKSPACE_DEV_MEMBERSHIP` | must remain unset | Explicitly rejected even when empty; the development self-assertion is not supported rollout authorization. |
| `LINGTAI_TUI_PYTHON` | `~/.lingtai-tui/runtime/venv/bin/python` | Python used to verify the intrinsic skill. |
| `LINGTAI_WORKBENCH_META_DIR` | `target/lingtai-workbench/meta` | Holt metadata directory. Production/downstream use must override this with persistent storage. |
| `LINGTAI_WORKBENCH_SERVER_BIND` | `127.0.0.1:7799` | Metadata RPC listen/client address. |
| `LINGTAI_WORKBENCH_SERVER_LOG` | `target/lingtai-workbench/nokv-server.log` | Helper-managed server log. |
| `LINGTAI_WORKBENCH_SERVER_PID` | `target/lingtai-workbench/nokv-server.pid` | Helper-managed process id. |
| `LINGTAI_WORKBENCH_SERVER_STATE` | `target/lingtai-workbench/nokv-server.json` | Managed server identity and launch state. |
| `LINGTAI_WORKBENCH_ROOT` | `/agents/{agent_id}/wb` | Per-Agent Workbench root template. |
| `LINGTAI_WORKBENCH_OBJECT_BACKEND` | `rustfs` | NoKV object backend. |
| `LINGTAI_WORKBENCH_S3_ENDPOINT` | `http://127.0.0.1:9000` | S3-compatible endpoint. |
| `LINGTAI_WORKBENCH_S3_BUCKET` | `nokv-lingtai-workbench` | Object bucket. |
| `LINGTAI_WORKBENCH_S3_ACCESS_KEY_ID` | `rustfsadmin` | Lower-level RustFS bootstrap credential. `up.sh` rejects a non-default value because custom credentials are not propagated into the LingTai MCP registration. |
| `LINGTAI_WORKBENCH_S3_SECRET_ACCESS_KEY` | `rustfsadmin` | Lower-level RustFS bootstrap credential. `up.sh` rejects a non-default value because custom credentials are not propagated into the LingTai MCP registration. |
| `LINGTAI_WORKBENCH_ACCEPT_CONTRACT_SHA256` | unset | Accept exactly one reviewed new canonical contract digest covering schemas and `tools/list` order. It is not a Boolean bypass. |
| `LINGTAI_WORKBENCH_ALLOW_DIRTY` | `0` | Set to `1` only for an explicitly dirty local maintainer build. |

Both an omitted profile and explicit `workbench` have the same effective tool
contract on a fresh or existing Workbench installation, but only explicit
`workbench` authorizes a downgrade from an existing `lingtai` lock.
`workbench` rejects all three tuple variables based on presence, including an
explicit empty assignment. `lingtai` requires all three non-empty values. The
supported script also rejects `LINGTAI_WORKBENCH_WORKSPACE_DEV_MEMBERSHIP`;
the development self-assertion is never a rollout credential. Successful
`lingtai` output contains only stable SHA-256
fingerprints of the exact UTF-8 workspace/actor bytes and decoded canonical
grant JSON; the grant fingerprint matches `launch.workspace_grant.canonical_sha256`
in the lock.

Local RustFS-specific controls are:

| Variable | Default |
| --- | --- |
| `LINGTAI_WORKBENCH_DATA_ROOT` | `target/lingtai-workbench` |
| `LINGTAI_WORKBENCH_RUSTFS_DATA_DIR` | `<data-root>/rustfs` |
| `LINGTAI_WORKBENCH_RUSTFS_CONTAINER` | `lingtai-workbench-rustfs` |
| `LINGTAI_WORKBENCH_RUSTFS_IMAGE` | `rustfs/rustfs:latest` |
| `LINGTAI_WORKBENCH_RUSTFS_HOST` | `127.0.0.1` |
| `LINGTAI_WORKBENCH_RUSTFS_PORT` | `9000` |
| `LINGTAI_WORKBENCH_RUSTFS_CONSOLE_PORT` | `9001` |

An external Release or future Brew artifact is passed through `up.sh` with:

| Variable | Meaning |
| --- | --- |
| `NOKV_BIN` | Exact packaged native executable. |
| `NOKV_BUILD_INFO` | Matching artifact-bound supported build identity; mandatory with `NOKV_BIN`. Current registry-sourced artifacts use `nokv.build_info.v2`; legacy git-sourced `nokv.build_info.v1` remains checkable. |
| `NOKV_REVISION` | Optional expected full 40-character NoKV commit. |
| `NOKV_DISTRIBUTION` | Optional `release`, `brew`, `source`, or `path` label recorded in the lock. |
| `NOKV_EXPECTED_SHA256` | Optional checksum from an independently trusted release channel. |

## Lower-Level Source Handoff

`--build-source` is the only trusted lower-level source build path. It runs
`cargo build --locked --release`, checks that source identity did not change
during the build, creates build-info for the exact output bytes, and stages the
binary. It is mutually exclusive with `--nokv-bin` and `--build-info`.

When a compatible metadata server and object store are already running, build,
gate, and switch one Agent directly with:

```bash
python3 ./scripts/lingtai-workbench/sync_workbench_mcp.py \
  --project /path/to/lingtai-project \
  --build-source . \
  --distribution source \
  --server-bind 127.0.0.1:7799 \
  --object-backend rustfs \
  --s3-endpoint http://127.0.0.1:9000 \
  --s3-bucket nokv-lingtai-workbench \
  --workbench-root '/agents/{agent_id}/wb' \
  --profile workbench
```

Omit `--agent` for normal automatic selection in this single lower-level
invocation. If selection is ambiguous, pass one exact directory name with
`--agent`. The supported `up.sh` wrapper resolves the Agent once before its
first preflight, pins the canonical `.lingtai` and Agent directory identities,
and passes the opaque orchestration token to every lower-level phase. State I/O
is descriptor-anchored, so rename/recreation of the same name fails closed
instead of retargeting the update.

To build and stage without probing or changing the Agent:

```bash
python3 ./scripts/lingtai-workbench/sync_workbench_mcp.py \
  --project /path/to/lingtai-project \
  --build-source . \
  --distribution source \
  --stage-only
```

The only stdout line is the immutable executable path. The sibling
`build-info.json` must travel with that staged executable. The supported
orchestration keeps staging profile-neutral and passes no profile or workspace
tuple to this phase.

To validate a staged candidate against the current metadata endpoint without
changing Agent files, replace `--stage-only` with `--probe-only` and include the
same server/object-store/workbench-root/profile options as the deployment. For
`lingtai`, also include `--workspace-id`, `--workspace-actor-id`, and
`--workspace-grant` with the same exact tuple used by final sync. `up.sh` runs
this read-only live probe automatically before it replaces an existing server.

## Release Artifact Contract

A Release must build from a clean checkout of the exact advertised commit and
ship both the native executable and its matching versioned build identity.
Generate the identity only after the final binary exists:

```bash
cargo build --locked --release -p nokv --bin nokv

python3 ./scripts/lingtai-workbench/generate_nokv_build_info.py \
  --source-root . \
  --revision "$(git rev-parse HEAD)" \
  --nokv-bin ./target/release/nokv \
  --output ./dist/build-info.json
```

The build-info binds the exact binary SHA-256 and size to the NoKV commit and
`Cargo.lock`. For the current registry-sourced Holt dependency,
`nokv.build_info.v2` also records the exact crates.io registry identifier, Holt
crate version, and lowercase 64-hex Cargo checksum. The reader continues to
accept old `nokv.build_info.v1` git locks with their exact Holt commit; it does
not mix source fields across schemas. Release installation should place the
identity at `share/nokv/build-info.json` or otherwise provide its path
explicitly.

Exercise a packaged artifact through the same orchestration and live gate:

```bash
LINGTAI_WORKBENCH_PROJECT=/path/to/lingtai-project \
LINGTAI_WORKBENCH_META_DIR=/persistent/path/to/nokv-meta \
NOKV_BIN=/opt/nokv/bin/nokv \
NOKV_BUILD_INFO=/opt/nokv/share/nokv/build-info.json \
NOKV_DISTRIBUTION=release \
./scripts/lingtai-workbench/up.sh
```

Do not call the raw installer as a package post-install hook: owner capability
and live schema still have to be checked at deployment time.

The Brew tap is not published yet. When it is available, the formula must
install the same binary/build-info pair, and downstream activation must still
pass `NOKV_BIN` plus `NOKV_BUILD_INFO` to `up.sh`. npm and pip wrappers are not
the distribution boundary for the native NoKV server.

## Manual Layer-by-Layer Diagnostics

Use these commands to isolate one layer. They are not an alternative user
installation path.

### 1. LingTai Skill and Agent Files

```bash
~/.lingtai-tui/runtime/venv/bin/python - <<'PY'
from pathlib import Path
import lingtai.intrinsic_skills as skills

root = Path(skills.__file__).parent
print((root / "nokv-workbench" / "SKILL.md").exists())
PY

python3 ./scripts/lingtai-workbench/sync_workbench_mcp.py \
  --project /path/to/lingtai-project \
  --profile workbench \
  --preflight-only
```

`--preflight-only` recovers a valid interrupted local transaction when present,
then parses the Agent files and compares an old lock to the checked-in canonical
contract without building or probing. A `lingtai` preflight requires the same
three tuple flags as probe and final sync. Recovery finishes or restores the
recorded old transaction before a new tuple is applied.

### 2. RustFS and Bucket

```bash
LINGTAI_WORKBENCH_RUSTFS_DATA_DIR=/persistent/path/to/rustfs \
./scripts/lingtai-workbench/start_rustfs.sh

AWS_ACCESS_KEY_ID=rustfsadmin \
AWS_SECRET_ACCESS_KEY=rustfsadmin \
aws --endpoint-url http://127.0.0.1:9000 s3api head-bucket \
  --bucket nokv-lingtai-workbench
```

### 3. Immutable Source Candidate

```bash
STAGED_NOKV="$(python3 ./scripts/lingtai-workbench/sync_workbench_mcp.py \
  --project /path/to/lingtai-project \
  --build-source . \
  --distribution source \
  --stage-only)"

test -x "$STAGED_NOKV"
test -f "$(dirname "$STAGED_NOKV")/build-info.json"
```

### 4. Metadata Server and Connectivity

Use the exact staged binary and the same durable metadata directory and object
store as the deployment:

```bash
"$STAGED_NOKV" \
  --server-bind 127.0.0.1:7799 \
  --object-backend rustfs \
  --s3-endpoint http://127.0.0.1:9000 \
  --s3-bucket nokv-lingtai-workbench \
  --meta /persistent/path/to/nokv-meta \
  serve
```

From another terminal:

```bash
"$STAGED_NOKV" \
  --server-bind 127.0.0.1:7799 \
  --object-backend rustfs \
  --s3-endpoint http://127.0.0.1:9000 \
  --s3-bucket nokv-lingtai-workbench \
  ls /
```

Exit status zero proves the client can reach both metadata and object storage.

### 5. Raw Workbench Contract

Probe a concrete Agent root, not the literal `{agent_id}` template:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | \
  "$STAGED_NOKV" \
    --server-bind 127.0.0.1:7799 \
    --object-backend rustfs \
    --s3-endpoint http://127.0.0.1:9000 \
    --s3-bucket nokv-lingtai-workbench \
    mcp --profile workbench \
    --workbench-root '/agents/coordinator(codex-gpt-5.4)/wb'
```

The result must contain exactly 18 tools in the frozen `toolOrder`. Compare
`inputSchema` semantically to `workbench_contract_schema.json`; descriptions
and JSON Schema annotations do not affect the schema component, while missing
fields, added restrictions, or a different tools/list order change or fail the
contract.

For a raw `lingtai` probe, use the same command with `--profile lingtai` and the
complete `--workspace-id`, `--workspace-actor-id`, and `--workspace-grant`
tuple. The independent `lingtai_contract_schema.json` requires the unchanged 18
Workbench definitions as a prefix followed by the reader suffix (20 tools,
SHA-256 `e008fc0a776c3348ec0ddae3db9eebc01ea37eed3b723a86004eae110d94fc2f`)
or writer suffix (23 tools, SHA-256
`1e3f09616286dcd8069f91319bfaee356ce79bf705f0806a38b38b7b972989f1`).
Extra, missing, reordered, or role-incompatible definitions fail closed.

### 6. Gated Registration and Read-Only Verification

After the staged binary is serving successfully:

```bash
python3 ./scripts/lingtai-workbench/sync_workbench_mcp.py \
  --project /path/to/lingtai-project \
  --nokv-bin "$STAGED_NOKV" \
  --build-info "$(dirname "$STAGED_NOKV")/build-info.json" \
  --distribution source \
  --server-bind 127.0.0.1:7799 \
  --object-backend rustfs \
  --s3-endpoint http://127.0.0.1:9000 \
  --s3-bucket nokv-lingtai-workbench \
  --workbench-root '/agents/{agent_id}/wb' \
  --profile workbench

python3 ./scripts/lingtai-workbench/sync_workbench_mcp.py \
  --project /path/to/lingtai-project \
  --check
```

When a reviewed schema or tools/list-order contract transition is intentional,
pass the exact digest from the error as `--accept-contract-sha256 <digest>`.
Never add a Boolean force flag. Selecting the other checked-in exact profile is
not drift and does not require an acceptance digest. `--check` reconstructs
profile and any tuple only from the durable lock; it rejects selection
overrides. A reader/writer role change remains within `lingtai`, changes its
ordered contract, and therefore requires the exact reviewed same-profile
acceptance digest reported by the tool.

### Raw Registration Repair

`install_workbench_mcp.py` only renders and upserts the two LingTai MCP files.
It does not create a lock or validate the runtime. Reserve it for tests or
manual repair where all gates have already been performed:

```bash
python3 ./scripts/lingtai-workbench/install_workbench_mcp.py \
  --agent-dir '/path/to/project/.lingtai/coordinator(codex-gpt-5.4)' \
  --nokv-bin /immutable/path/to/nokv \
  --server-bind 127.0.0.1:7799 \
  --object-backend rustfs \
  --s3-endpoint http://127.0.0.1:9000 \
  --s3-bucket nokv-lingtai-workbench \
  --workbench-root '/agents/{agent_id}/wb' \
  --profile workbench
```

The raw renderer can represent the `lingtai` tuple but remains unsuitable as a
deployment path: it validates no immutable runtime or live contract and creates
no recovery lock record. Never use it to bypass `up.sh`.

## Operational Failure State

Inspect these files before deleting or restarting anything:

```text
target/lingtai-workbench/nokv-server.log
target/lingtai-workbench/nokv-server.pid
target/lingtai-workbench/nokv-server.json
target/lingtai-workbench/up.lock/
<agent>/.nokv-workbench.sync.lock
<agent>/.nokv-workbench.transaction.json
<agent>/nokv-workbench.lock.json
```

The normal sync and `--preflight-only` take the exclusive per-Agent lock and
recover a valid interrupted transaction. The read-only `--check` refuses while
a transaction is pending; rerun the normal update rather than removing the
marker. The one existing journal always covers registry, init, and lock bytes;
profile switching introduces no second transaction, snapshot directory, or
configuration source.

The supported wrapper also pins a non-secret SHA-256 over the exact presence
and bytes of registry, init, and lock immediately after preflight recovery. The
final sync compares that precondition against the same originals it would put
in the journal. If another cooperating sync changes or rotates the Agent
configuration while `up.sh` is building or probing, the older run fails rather
than overwriting the newer state; rerun `up.sh` from preflight.

`up.sh` prints `configuration committed` only after sync has atomically
installed registry, init, and lock and removed the journal. A later
lock-driven check failure is reported exactly as
`configuration committed but post-commit verification failed; not rolled back`.
At that point the new durable configuration remains installed; do not describe
the failure as transaction recovery or assume the old files were restored.
Diagnose with `--check --agent <pinned-directory-name>` and withhold
`/refresh` until verification succeeds.

If port `7799` is occupied, identify the listener first:

```text
workbench_create
workbench_put_file
workbench_append
workbench_edit
workbench_list
workbench_stat
workbench_read
workbench_grep
workbench_search
workbench_aggregate
workbench_catalog
workbench_find
workbench_commit
workbench_snapshot
workbench_snapshot_renew
workbench_snapshot_retire
workbench_snapshot_list
workbench_restore
```

The helper must not terminate an unverified process. For the default local
object store, inspect `docker logs lingtai-workbench-rustfs` and verify the
bucket independently with the AWS CLI.

Once `restore_to_fork_v1_active` exists in metadata, never test an older,
pre-restore metadata server against that directory. The typed global drain,
full fsck, and post-drain metadata checkpoint are a separate controlled
downgrade procedure.

## Profile Rotation And Revocation Internals

The supported `workbench -> lingtai -> workbench` path always uses `up.sh` and
the same three-file transaction. Returning to `workbench` requires explicit
`LINGTAI_WORKBENCH_MCP_PROFILE=workbench` plus absence of the three tuple
environment variables; omission is only an implicit default and cannot
downgrade an existing `lingtai` lock. The rollback removes their launch
arguments and every grant lock field. It does not delete shared namespace data,
object bodies, metadata, snapshots, or LingTai's local-first runtime control
plane.

Grant expiry is checked on every `tools/list` and workspace call; an expired
running Provider advertises no Shared Workspace tools and rejects direct calls
before NoKV access. External grant rotation or revocation has no in-process
watch. Persist the new grant or the `workbench` rollback through `up.sh`, verify
with lock-driven `--check`, then use LingTai `/refresh` so activation terminates
the predecessor and launches the exact locked arguments. Re-enabling the same
workspace later with a new valid grant exposes retained data; it never implies
data deletion or a new authorization source.

## File Publication And Structured Read Contract

`workbench_put_file` has two exclusive modes; it is not upsert:

- `replace=false` (the default) is create-only and fails when the target
  already exists.
- `replace=true` is replace-only and fails when the target is missing. Use it
  only when replacing an existing whole file intentionally.

`workbench_append` is a separate operation and creates its target when missing.
Do not use `replace=true` as a create fallback after a speculative write: an
exists/not-found race is a coordination conflict that the caller must observe.

Live `workbench_read` with `format="structured"` parses JSON, YAML, and UTF-8
text records. It does not natively parse `application/x-ndjson`, and there is
no NDJSON record-pagination contract. A `.jsonl` suffix alone does not select a
parser: store the file with a `text/*` content type to receive raw
`record_type="text_lines"` records and parse each `value.text` yourself. For
`application/x-ndjson` or any other unsupported content type, use
`format="bytes"`. At a snapshot, non-bytes reads expose UTF-8 text lines as a
snapshot-specific raw-text mode; that is still not an NDJSON parser.

## Commit Identity Contract

`workbench_commit` publishes `metadata/run_manifest.json` with schema
`nokv.workbench.run_manifest.v1`. The call requires `content_digest_uri` in the
exact form `sha256:<64 lowercase hex>`. LingTai must compute this digest before
the call from the job outputs or another stable, application-owned content
description; a phase label alone is not content identity.

NoKV separately hashes the compact canonical JSON manifest (recursively sorted
object keys, array order preserved) as `manifest_digest_uri`, then derives
`commit_identity` from the workbench id, content digest, and manifest digest
under the `nokv.workbench.commit_identity.v1` domain. The server timestamp is
not part of either identity.

The identity byte stream is unambiguous and portable: start with
`b"nokv.workbench.commit_identity.v1\0"`, then append the workbench id,
`content_digest_uri`, and `manifest_digest_uri` in that order, each prefixed by
its unsigned 64-bit big-endian UTF-8 byte length. `commit_identity` is the
lowercase `sha256:` URI of that complete stream.

An exact retry returns the existing commit with `idempotent_replay=true`,
including after a committed response was lost. A different identity returns
`WorkbenchCommitConflict`, even when both manifests have the same phase.
Replacing a different commit or upgrading a legacy v0 manifest requires an
explicit `replace=true`; a concurrent identity change still fails closed.
Legacy v0 manifests remain readable by `workbench_find`, but they never count
as an identity match.

## Snapshot Annotation Contract

`workbench_snapshot` accepts optional `reason` and `metadata` fields. `reason`
is a non-empty human-readable string bounded to 256 Unicode characters and
1024 UTF-8 bytes. `metadata` is a JSON object bounded to 4096 canonical bytes,
8 container levels, and 64 object keys across the complete value. The returned
`annotation` is also preserved by checkpoint list and renew responses; these
fields are not encoded into the 64-character checkpoint name.

Annotations live in the workbench checkpoint registry, which is appended after
the authoritative snapshot pin is created. If that append fails, the MCP call
returns typed `SnapshotRegistryWritePartial` with the created snapshot id,
lease, annotation, and explicit retry/retire compensation. It does not report a
success that falsely claims the annotation is discoverable.

## Snapshot Retirement Contract

`workbench_snapshot_retire` is the MCP lifecycle endpoint for releasing a
checkpoint. Pass `id` and exactly one of `snapshot_id` or `name`; an optional
bounded `reason` records why retirement was requested. The operation calls the
existing path-bound metadata retirement API, so a foreign-root snapshot or a
snapshot whose fork retention is still active remains a typed error.

The operation is idempotent. The call that removes the pin returns
`retired=true`; an exact retry after the pin is already absent succeeds with
`retired=false`. NoKV never upgrades that false outcome into a fabricated
deletion. The checkpoint registry records retire lifecycle events, and
`workbench_snapshot_list` reports `state=retired` only when it has an
acknowledged `retired=true` event. An absent pin without that proof remains
`state=reaped`. The base surface therefore has 17 tools, or 18 when
`workbench_restore` is capability-enabled.

## Tests

Run the standalone experimental LingTai rollout acceptance gate from the NoKV
repository root. A clean checkout is required for completion evidence:

```bash
RUSTUP_TOOLCHAIN=nightly \
python3 ./scripts/lingtai-workbench/real_lingtai_rollout_test.py \
  --nokv-source . \
  --command-timeout 7200 \
  --startup-timeout 120
```

During local development only, add `--allow-dirty`. A dirty run can populate
the release-build cache and provide functional diagnostics, but it does not
bind the result to one reviewable source commit and is not final rollout
evidence.

Run the focused script suites from the NoKV repository root:

```bash
python3 ./scripts/lingtai-workbench/install_workbench_mcp_test.py
python3 ./scripts/lingtai-workbench/workbench_contract_test.py
python3 ./scripts/lingtai-workbench/nokv_runtime_test.py
python3 ./scripts/lingtai-workbench/managed_nokv_server_test.py
python3 ./scripts/lingtai-workbench/sync_workbench_mcp_test.py
python3 ./scripts/lingtai-workbench/up_test.py
python3 ./scripts/lingtai-workbench/workspace_grant_test.py
python3 ./scripts/lingtai-workbench/real_lingtai_rollout_test_test.py
python3 ./scripts/lingtai-workbench/durable_restore_live_e2e_test.py

python3 -m py_compile \
  ./scripts/lingtai-workbench/install_workbench_mcp.py \
  ./scripts/lingtai-workbench/workbench_contract.py \
  ./scripts/lingtai-workbench/nokv_runtime.py \
  ./scripts/lingtai-workbench/managed_nokv_server.py \
  ./scripts/lingtai-workbench/generate_nokv_build_info.py \
  ./scripts/lingtai-workbench/sync_workbench_mcp.py \
  ./scripts/lingtai-workbench/workspace_grant.py \
  ./scripts/lingtai-workbench/real_lingtai_rollout_test.py \
  ./scripts/lingtai-workbench/durable_restore_live_e2e.py

ruff check ./scripts/lingtai-workbench
ruff format --check ./scripts/lingtai-workbench
bash -n ./scripts/lingtai-workbench/up.sh
bash -n ./scripts/lingtai-workbench/start_rustfs.sh
```

## Durable Restore Live E2E

The merge gate must run from the LingTai companion checkout's environment:

```bash
uv run --project /path/to/lingtai-kernel \
  python /path/to/NoKV/scripts/lingtai-workbench/durable_restore_live_e2e.py \
  --lingtai-kernel-dir /path/to/lingtai-kernel \
  --profile full \
  --require-all
```

The full profile uses a real 1 GiB sparse fixture and validates the exact raw
MCP contract, LingTai registration and reconnect, 16-way restore idempotency,
COW object PUT counts, crash barriers across materialization/reference/index
and attach phases, metadata checkpoint plus log replay, source retirement,
borrower object lifetime, indexed queries, nested restore, rename/delete/
release cleanup, fsck, and final object inventory. `--require-all` has no skip
path for missing Docker, AWS CLI, LingTai dependencies, capability, or a stale
binary.

For local iteration only:

```bash
uv run --project /path/to/lingtai-kernel \
  python /path/to/NoKV/scripts/lingtai-workbench/durable_restore_live_e2e.py \
  --lingtai-kernel-dir /path/to/lingtai-kernel \
  --profile quick \
  --keep-state
```

The quick profile keeps the non-crash contract, indexing, restart, and object
lifecycle checks with a smaller fixture; it is not merge evidence.

The metadata HA companion gate remains:

```bash
NOKV_HA_STALE_OWNER_CHAOS=1 ./scripts/run-metadata-ha-smoke.sh
```
