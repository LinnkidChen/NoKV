use std::collections::BTreeMap;
use std::fmt;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use base64::engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD};
use base64::Engine as _;
use nokv_agent::AgentToolDefinition;
use nokv_client::{
    artifact_write_commit_status, is_artifact_write_conflict, is_metadata_not_found,
    ArtifactMetadata, ArtifactWriteCommitStatus, ClientError, NoKvFsClient,
};
use nokv_meta::{
    MetadError, NamespaceCardKind, NamespaceReadFormat, NamespaceReadOptions, NamespaceRecordType,
};
use nokv_object::ObjectStore;
use nokv_types::{FileType, PathMetadata};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::mcp_runtime::McpToolProvider;

const PROVIDER_NAME: &str = "shared_workspace";
const GRANT_SCHEMA: &str = "nokv.lingtai.workspace_grant.v1";
const GRANT_ISSUER: &str = "lingtai-workbench-sync";
const GRANT_AUDIENCE: &str = "nokv-mcp:lingtai";
const MAX_GRANT_LIFETIME_MS: u64 = 2_592_000_000;
const PRODUCER_PREFIX: &str = "nokv-shared-workspace-mcp/v1";
const EDIT_REQUEST_MANIFEST_PREFIX: &str = "nokv-shared-workspace-edit-request/v1";
const DIRECTORY_MODE: u32 = 0o755;
const FILE_MODE: u32 = 0o644;
const DEFAULT_PAGE_LIMIT: usize = 100;
const MAX_LIST_LIMIT: usize = 100;
const MAX_READ_LIMIT: usize = 300;
const WRITE_RECONCILIATION: &str = "Do not automatically retry. Read the canonical relative path and compare the complete operation identity and persisted result metadata. If the exact producer and result fields prove the operation committed, treat it as success. Otherwise compare full content or recover to a new path before issuing a new operation_id.";

const WORKSPACE_LIST: &str = "workspace_list";
const WORKSPACE_READ: &str = "workspace_read";
const WORKSPACE_PUT_FILE: &str = "workspace_put_file";
const WORKSPACE_EDIT: &str = "workspace_edit";
const WORKSPACE_APPEND: &str = "workspace_append";
const READER_TOOLS: [&str; 2] = [WORKSPACE_LIST, WORKSPACE_READ];
const WRITER_TOOLS: [&str; 5] = [
    WORKSPACE_LIST,
    WORKSPACE_READ,
    WORKSPACE_PUT_FILE,
    WORKSPACE_EDIT,
    WORKSPACE_APPEND,
];

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SharedWorkspaceProviderOptions {
    pub workspace_id: String,
    pub actor_id: String,
    pub dev_membership: Option<String>,
    pub launcher_grant: Option<String>,
    pub max_bytes: usize,
    pub uid: u32,
    pub gid: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SharedWorkspaceConfigError {
    InvalidIdentity { field: &'static str, reason: String },
    MissingMembership,
    ConflictingMembership,
    InvalidDevelopmentRole,
    InvalidLauncherGrant(String),
}

impl fmt::Display for SharedWorkspaceConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidIdentity { field, reason } => {
                write!(f, "invalid {field}: {reason}")
            }
            Self::MissingMembership => write!(f, "workspace membership is required"),
            Self::ConflictingMembership => {
                write!(
                    f,
                    "development membership and launcher grant are mutually exclusive"
                )
            }
            Self::InvalidDevelopmentRole => {
                write!(
                    f,
                    "workspace development membership must be reader or writer"
                )
            }
            Self::InvalidLauncherGrant(reason) => write!(f, "invalid workspace grant: {reason}"),
        }
    }
}

impl std::error::Error for SharedWorkspaceConfigError {}

pub trait WorkspaceClock: Send + Sync {
    fn now_unix_ms(&self) -> u64;
}

#[derive(Clone, Copy, Debug, Default)]
struct SystemWorkspaceClock;

impl WorkspaceClock for SystemWorkspaceClock {
    fn now_unix_ms(&self) -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| u64::try_from(duration.as_millis()).unwrap_or(u64::MAX))
            .unwrap_or(0)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum WorkspaceRole {
    Reader,
    Writer,
}

impl WorkspaceRole {
    fn parse(value: &str) -> Option<Self> {
        match value {
            "reader" => Some(Self::Reader),
            "writer" => Some(Self::Writer),
            _ => None,
        }
    }

    fn permits(self, tool: &str) -> bool {
        matches!(self, Self::Writer) || READER_TOOLS.contains(&tool)
    }

    fn advertised(self) -> &'static [&'static str] {
        match self {
            Self::Reader => &READER_TOOLS,
            Self::Writer => &WRITER_TOOLS,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum Membership {
    Development {
        role: WorkspaceRole,
    },
    Launcher {
        role: WorkspaceRole,
        expires_at_unix_ms: u64,
    },
}

impl Membership {
    fn role_if_current(&self, now_unix_ms: u64) -> Option<WorkspaceRole> {
        match self {
            Self::Development { role } => Some(*role),
            Self::Launcher {
                role,
                expires_at_unix_ms,
            } if now_unix_ms < *expires_at_unix_ms => Some(*role),
            Self::Launcher { .. } => None,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct LauncherGrant {
    schema: String,
    grant_id: String,
    issuer: String,
    audience: String,
    workspace_id: String,
    actor_id: String,
    role: String,
    issued_at_unix_ms: u64,
    expires_at_unix_ms: u64,
}

pub struct SharedWorkspaceProvider {
    workspace_id: String,
    encoded_workspace_id: String,
    encoded_actor_id: String,
    membership: Membership,
    max_bytes: usize,
    uid: u32,
    gid: u32,
    clock: Arc<dyn WorkspaceClock>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ListArgs {
    path: Option<String>,
    offset: Option<u64>,
    if_read_version: Option<u64>,
    limit: Option<usize>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ReadArgs {
    path: String,
    format: Option<String>,
    offset: Option<u64>,
    if_generation: Option<u64>,
    limit: Option<usize>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PutArgs {
    path: String,
    operation_id: String,
    base_generation: Option<u64>,
    text: Option<String>,
    base64: Option<String>,
    content_type: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct EditArgs {
    path: String,
    operation_id: String,
    base_generation: u64,
    old_string: String,
    new_string: String,
    replace_all: Option<bool>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct AppendArgs {
    path: String,
    operation_id: String,
    base_generation: Option<u64>,
    text: Option<String>,
    base64: Option<String>,
    content_type: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct ProducerProof {
    workspace: String,
    path: String,
    actor: String,
    tool: String,
    operation: String,
    base_generation: Option<u64>,
    effect: u64,
    digest_hex: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ProofMatch {
    Exact,
    SameScopeMismatch,
    DifferentScope,
}

impl SharedWorkspaceProvider {
    pub fn new(
        options: SharedWorkspaceProviderOptions,
    ) -> Result<Self, SharedWorkspaceConfigError> {
        Self::new_with_clock(options, SystemWorkspaceClock)
    }

    pub fn new_with_clock<C>(
        options: SharedWorkspaceProviderOptions,
        clock: C,
    ) -> Result<Self, SharedWorkspaceConfigError>
    where
        C: WorkspaceClock + 'static,
    {
        validate_identity("workspace id", &options.workspace_id)?;
        validate_identity("workspace actor id", &options.actor_id)?;
        let clock: Arc<dyn WorkspaceClock> = Arc::new(clock);
        let membership = match (
            options.dev_membership.as_deref(),
            options.launcher_grant.as_deref(),
        ) {
            (None, None) => return Err(SharedWorkspaceConfigError::MissingMembership),
            (Some(_), Some(_)) => return Err(SharedWorkspaceConfigError::ConflictingMembership),
            (Some(role), None) => Membership::Development {
                role: WorkspaceRole::parse(role)
                    .ok_or(SharedWorkspaceConfigError::InvalidDevelopmentRole)?,
            },
            (None, Some(grant)) => validate_launcher_grant(
                grant,
                &options.workspace_id,
                &options.actor_id,
                clock.now_unix_ms(),
            )?,
        };
        Ok(Self {
            encoded_workspace_id: URL_SAFE_NO_PAD.encode(options.workspace_id.as_bytes()),
            encoded_actor_id: URL_SAFE_NO_PAD.encode(options.actor_id.as_bytes()),
            workspace_id: options.workspace_id,
            membership,
            max_bytes: options.max_bytes,
            uid: options.uid,
            gid: options.gid,
            clock,
        })
    }

    fn current_role(&self) -> Option<WorkspaceRole> {
        self.membership.role_if_current(self.clock.now_unix_ms())
    }

    fn workspace_root(&self) -> String {
        format!("/workspaces/{}/shared", self.encoded_workspace_id)
    }

    fn execute<O>(
        &self,
        client: &NoKvFsClient<O>,
        name: &str,
        args: &Value,
    ) -> Result<Value, WorkspaceToolError>
    where
        O: ObjectStore + Send + Sync + 'static,
    {
        let role = self
            .current_role()
            .ok_or_else(WorkspaceToolError::permission_denied)?;
        if !role.permits(name) {
            return Err(WorkspaceToolError::permission_denied());
        }
        match name {
            WORKSPACE_LIST => self.execute_list(client, args),
            WORKSPACE_READ => self.execute_read(client, args),
            WORKSPACE_PUT_FILE => self.execute_put(client, args),
            WORKSPACE_EDIT => self.execute_edit(client, args),
            WORKSPACE_APPEND => self.execute_append(client, args),
            _ => Err(invalid_argument("unknown shared workspace tool")),
        }
    }

    fn execute_list<O>(
        &self,
        client: &NoKvFsClient<O>,
        args: &Value,
    ) -> Result<Value, WorkspaceToolError>
    where
        O: ObjectStore + Send + Sync + 'static,
    {
        let args: ListArgs = parse_args(args, &[])?;
        let path = validate_relative_path(args.path.as_deref().unwrap_or_default(), true)?;
        let offset = args.offset.unwrap_or(0);
        let read_version = args.if_read_version;
        let limit = args.limit.unwrap_or(DEFAULT_PAGE_LIMIT);
        if !(1..=MAX_LIST_LIMIT).contains(&limit) {
            return Err(invalid_argument("limit must be between 1 and 100"));
        }
        if offset > 0 && read_version.is_none() {
            return Err(invalid_argument("positive offset requires if_read_version"));
        }

        let target = self.absolute_path(&path);
        let Some(metadata) = inspect_path(client, &target)? else {
            if path.is_empty() && offset == 0 && read_version.is_none() {
                return self.bound_success(json!({
                    "status": "success",
                    "workspace_id": self.workspace_id,
                    "path": "",
                    "relative_path": "",
                    "read_version": Value::Null,
                    "offset": 0,
                    "page_entry_count": 0,
                    "total_entry_count": 0,
                    "entries": [],
                    "next_offset": Value::Null,
                    "truncated": false,
                }));
            }
            if path.is_empty() {
                return Err(conflict(
                    "shared workspace root changed; restart at offset zero",
                ));
            }
            return Err(not_found());
        };
        require_directory(&metadata)?;

        let page = client
            .namespace_list_offset_page(&target, offset, limit)
            .map_err(map_read_error)?;
        let Some(observed_read_version) = page.snapshot_id else {
            return Err(storage_error(false));
        };
        if read_version.is_some_and(|expected| expected != observed_read_version) {
            return Err(conflict(
                "workspace list version changed; restart at offset zero",
            ));
        }
        let total_entry_count =
            u64::try_from(page.entry_count).map_err(|_| storage_error(false))?;
        if offset > total_entry_count {
            return Err(invalid_argument("offset exceeds total entry count"));
        }

        let mut entries = Vec::with_capacity(page.entries.len());
        for card in page.entries {
            let child_path = if path.is_empty() {
                card.name.clone()
            } else {
                format!("{path}/{}", card.name)
            };
            let kind = match card.kind {
                NamespaceCardKind::File => "file",
                NamespaceCardKind::Directory => "directory",
                NamespaceCardKind::Symlink => "symlink",
                NamespaceCardKind::Special => "special",
            };
            entries.push(json!({
                "name": card.name,
                "path": child_path,
                "relative_path": child_path,
                "kind": kind,
                "size_bytes": card.size_bytes,
                "entry_count": card.entry_count,
                "generation": card.generation,
            }));
        }
        let page_entry_count = u64::try_from(entries.len()).map_err(|_| storage_error(false))?;
        let next_offset = if page.truncated {
            Some(
                offset
                    .checked_add(page_entry_count)
                    .ok_or_else(|| storage_error(false))?,
            )
        } else {
            None
        };
        self.bound_success(json!({
            "status": "success",
            "workspace_id": self.workspace_id,
            "path": path,
            "relative_path": path,
            "read_version": observed_read_version,
            "offset": offset,
            "page_entry_count": page_entry_count,
            "total_entry_count": total_entry_count,
            "entries": entries,
            "next_offset": next_offset,
            "truncated": next_offset.is_some(),
        }))
    }

    fn execute_read<O>(
        &self,
        client: &NoKvFsClient<O>,
        args: &Value,
    ) -> Result<Value, WorkspaceToolError>
    where
        O: ObjectStore + Send + Sync + 'static,
    {
        let args: ReadArgs = parse_args(args, &["path"])?;
        let path = validate_relative_path(&args.path, false)?;
        let offset = args.offset.unwrap_or(0);
        let expected_generation = args.if_generation;
        let limit = args.limit.unwrap_or(DEFAULT_PAGE_LIMIT);
        if !(1..=MAX_READ_LIMIT).contains(&limit) {
            return Err(invalid_argument("limit must be between 1 and 300"));
        }
        if offset > 0 && expected_generation.is_none() {
            return Err(invalid_argument("positive offset requires if_generation"));
        }
        let (format, format_name) = match args.format.as_deref().unwrap_or("structured") {
            "structured" => (NamespaceReadFormat::Structured, "structured"),
            "bytes" => (NamespaceReadFormat::Bytes, "bytes"),
            _ => return Err(invalid_argument("format must be structured or bytes")),
        };

        let target = self.absolute_path(&path);
        let metadata = inspect_path(client, &target)?.ok_or_else(not_found)?;
        require_file(&metadata)?;
        if metadata.attr.size > self.max_bytes as u64 {
            return Err(payload_too_large());
        }
        if expected_generation.is_some_and(|expected| expected != metadata.attr.generation) {
            return Err(conflict(
                "workspace file generation changed; restart at offset zero",
            ));
        }
        if matches!(format, NamespaceReadFormat::Bytes) && offset > metadata.attr.size {
            return Err(invalid_argument("offset exceeds total byte count"));
        }

        let page = client
            .read_page(
                &target,
                NamespaceReadOptions {
                    format: format.clone(),
                    cursor: None,
                    offset,
                    limit,
                    expected_generation: Some(metadata.attr.generation),
                },
            )
            .map_err(map_read_error)?;
        if page.generation != metadata.attr.generation {
            return Err(conflict(
                "workspace file generation changed; restart at offset zero",
            ));
        }
        if matches!(format, NamespaceReadFormat::Structured)
            && page.record_count.is_some_and(|count| offset > count as u64)
        {
            return Err(invalid_argument("offset exceeds total record count"));
        }

        let record_type = page.record_type.as_ref().map(|kind| match kind {
            NamespaceRecordType::DirectoryEntries => "directory_entries",
            NamespaceRecordType::JsonArray => "json_array",
            NamespaceRecordType::JsonObject => "json_object",
            NamespaceRecordType::YamlMapping => "yaml_mapping",
            NamespaceRecordType::TextLines => "text_lines",
        });
        let mut items = Vec::with_capacity(page.items.len());
        for item in page.items {
            let value =
                serde_json::from_str(&item.value_json).unwrap_or(Value::String(item.value_json));
            items.push(json!({"index": item.index, "value": value}));
        }
        let bytes = page.bytes.unwrap_or_default();
        let returned_units = if matches!(format, NamespaceReadFormat::Bytes) {
            u64::try_from(bytes.len()).map_err(|_| storage_error(false))?
        } else {
            u64::try_from(items.len()).map_err(|_| storage_error(false))?
        };
        let next_offset = if page.truncated {
            Some(
                offset
                    .checked_add(returned_units)
                    .ok_or_else(|| storage_error(false))?,
            )
        } else {
            None
        };
        let (encoded_bytes, bytes_encoding) = if matches!(format, NamespaceReadFormat::Bytes) {
            (Value::String(STANDARD.encode(bytes)), json!("base64"))
        } else {
            (Value::Null, Value::Null)
        };
        self.bound_success(json!({
            "status": "success",
            "workspace_id": self.workspace_id,
            "path": path,
            "relative_path": path,
            "generation": page.generation,
            "total_size_bytes": page.total_size_bytes,
            "format": format_name,
            "record_type": record_type,
            "record_count": page.record_count,
            "offset": offset,
            "next_offset": next_offset,
            "truncated": next_offset.is_some(),
            "items": items,
            "bytes": encoded_bytes,
            "bytes_encoding": bytes_encoding,
        }))
    }

    fn execute_put<O>(
        &self,
        client: &NoKvFsClient<O>,
        args: &Value,
    ) -> Result<Value, WorkspaceToolError>
    where
        O: ObjectStore + Send + Sync + 'static,
    {
        let args: PutArgs = parse_args(args, &["path", "operation_id", "base_generation"])?;
        let path = validate_relative_path(&args.path, false)?;
        validate_operation_id(&args.operation_id)?;
        let (bytes, content_type) = decode_payload(
            args.text.as_deref(),
            args.base64.as_deref(),
            args.content_type.as_deref(),
            false,
            self.max_bytes,
        )?;
        let size_bytes = u64::try_from(bytes.len()).map_err(|_| payload_too_large())?;
        let digest_hex = sha256_hex(&bytes);
        let digest_uri = format!("sha256:{digest_hex}");
        let producer = producer_string(
            &self.encoded_workspace_id,
            &path,
            &self.encoded_actor_id,
            WORKSPACE_PUT_FILE,
            &args.operation_id,
            args.base_generation,
            size_bytes,
            &digest_hex,
        );
        let target = self.absolute_path(&path);
        let mut observed = inspect_path(client, &target)?;
        match proof_match(
            observed.as_ref(),
            &producer,
            &digest_uri,
            &content_type,
            Some(size_bytes),
        ) {
            ProofMatch::Exact => {
                return self.put_success(
                    &path,
                    &args.operation_id,
                    args.base_generation,
                    observed.as_ref().expect("exact proof requires metadata"),
                    &digest_uri,
                    &content_type,
                    true,
                );
            }
            ProofMatch::SameScopeMismatch => {
                return Err(write_outcome_unknown(
                    WORKSPACE_PUT_FILE,
                    &path,
                    &args.operation_id,
                    args.base_generation,
                    &digest_uri,
                ));
            }
            ProofMatch::DifferentScope => {}
        }

        match (args.base_generation, observed.as_ref()) {
            (None, Some(_)) => return Err(conflict("create-only target already exists")),
            (Some(_), None) => return Err(not_found()),
            (Some(expected), Some(metadata)) => {
                require_file(metadata)?;
                if metadata.body.is_none() {
                    return Err(storage_error(false));
                }
                if metadata.attr.generation != expected {
                    return Err(conflict("workspace file generation changed"));
                }
            }
            (None, None) => {
                ensure_parent_dirs(client, self, &path)?;
                observed = inspect_path(client, &target)?;
                match proof_match(
                    observed.as_ref(),
                    &producer,
                    &digest_uri,
                    &content_type,
                    Some(size_bytes),
                ) {
                    ProofMatch::Exact => {
                        return self.put_success(
                            &path,
                            &args.operation_id,
                            args.base_generation,
                            observed.as_ref().expect("exact proof requires metadata"),
                            &digest_uri,
                            &content_type,
                            true,
                        );
                    }
                    ProofMatch::SameScopeMismatch => {
                        return Err(write_outcome_unknown(
                            WORKSPACE_PUT_FILE,
                            &path,
                            &args.operation_id,
                            args.base_generation,
                            &digest_uri,
                        ));
                    }
                    ProofMatch::DifferentScope if observed.is_some() => {
                        return Err(conflict("create-only target already exists"));
                    }
                    ProofMatch::DifferentScope => {}
                }
            }
        }

        let artifact = self.artifact_metadata(
            &target,
            &producer,
            &digest_uri,
            &content_type,
            observed.as_ref(),
        );
        let mut replayed = false;
        loop {
            let result = match args.base_generation {
                None => client
                    .put_artifact(&target, bytes.clone(), artifact.clone())
                    .map(|entry| PathMetadata {
                        attr: entry.attr,
                        body: entry.body,
                    }),
                Some(expected) => client
                    .put_artifact_replace_if_generation(
                        &target,
                        bytes.clone(),
                        artifact.clone(),
                        expected,
                    )
                    .map(|result| PathMetadata {
                        attr: result.entry.attr,
                        body: result.entry.body,
                    }),
            };
            match result {
                Ok(metadata) => {
                    return self.put_success(
                        &path,
                        &args.operation_id,
                        args.base_generation,
                        &metadata,
                        &digest_uri,
                        &content_type,
                        false,
                    );
                }
                Err(err) => {
                    let certainty = artifact_write_commit_status(&err);
                    let reread = inspect_path(client, &target);
                    let current = match reread {
                        Ok(current) => current,
                        Err(read_err)
                            if certainty == ArtifactWriteCommitStatus::DefinitelyNotCommitted =>
                        {
                            return Err(read_err);
                        }
                        Err(_) => {
                            return Err(write_outcome_unknown(
                                WORKSPACE_PUT_FILE,
                                &path,
                                &args.operation_id,
                                args.base_generation,
                                &digest_uri,
                            ));
                        }
                    };
                    match proof_match(
                        current.as_ref(),
                        &producer,
                        &digest_uri,
                        &content_type,
                        Some(size_bytes),
                    ) {
                        ProofMatch::Exact => {
                            return self.put_success(
                                &path,
                                &args.operation_id,
                                args.base_generation,
                                current.as_ref().expect("exact proof requires metadata"),
                                &digest_uri,
                                &content_type,
                                true,
                            );
                        }
                        ProofMatch::SameScopeMismatch => {
                            return Err(write_outcome_unknown(
                                WORKSPACE_PUT_FILE,
                                &path,
                                &args.operation_id,
                                args.base_generation,
                                &digest_uri,
                            ));
                        }
                        ProofMatch::DifferentScope => {}
                    }
                    if certainty == ArtifactWriteCommitStatus::DefinitelyNotCommitted {
                        return Err(map_definite_write_error(&err));
                    }
                    if !replayed && base_is_unchanged(current.as_ref(), args.base_generation) {
                        replayed = true;
                        continue;
                    }
                    return Err(write_outcome_unknown(
                        WORKSPACE_PUT_FILE,
                        &path,
                        &args.operation_id,
                        args.base_generation,
                        &digest_uri,
                    ));
                }
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn put_success(
        &self,
        path: &str,
        operation_id: &str,
        base_generation: Option<u64>,
        metadata: &PathMetadata,
        digest_uri: &str,
        content_type: &str,
        deduplicated: bool,
    ) -> Result<Value, WorkspaceToolError> {
        Ok(json!({
            "status": "success",
            "workspace_id": self.workspace_id,
            "path": path,
            "relative_path": path,
            "operation_id": operation_id,
            "base_generation": base_generation,
            "size_bytes": metadata.attr.size,
            "generation": metadata.attr.generation,
            "digest_uri": digest_uri,
            "content_type": content_type,
            "created": base_generation.is_none(),
            "deduplicated": deduplicated,
        }))
    }

    fn execute_edit<O>(
        &self,
        client: &NoKvFsClient<O>,
        args: &Value,
    ) -> Result<Value, WorkspaceToolError>
    where
        O: ObjectStore + Send + Sync + 'static,
    {
        let args: EditArgs = parse_args(
            args,
            &[
                "path",
                "operation_id",
                "base_generation",
                "old_string",
                "new_string",
            ],
        )?;
        let path = validate_relative_path(&args.path, false)?;
        validate_operation_id(&args.operation_id)?;
        if args.old_string.is_empty() {
            return Err(invalid_argument("old_string must not be empty"));
        }
        let replace_all = args.replace_all.unwrap_or(false);
        let edit_request_manifest_id =
            edit_request_manifest_id(&args.old_string, &args.new_string, replace_all);
        let target = self.absolute_path(&path);
        let metadata = inspect_path(client, &target)?.ok_or_else(not_found)?;
        require_file(&metadata)?;
        if metadata.attr.size > self.max_bytes as u64 {
            return Err(payload_too_large());
        }

        let (preflight_match, prior_proof) = edit_proof_match(
            Some(&metadata),
            &self.encoded_workspace_id,
            &path,
            &self.encoded_actor_id,
            &args.operation_id,
            args.base_generation,
            &edit_request_manifest_id,
        );
        match preflight_match {
            ProofMatch::Exact => {
                let proof = prior_proof.expect("exact edit proof is parsed");
                let body = metadata.body.as_ref().ok_or_else(|| storage_error(false))?;
                return self.edit_success(
                    &path,
                    &args.operation_id,
                    args.base_generation,
                    proof.effect,
                    &metadata,
                    &body.digest_uri,
                    &body.content_type,
                    false,
                    true,
                );
            }
            ProofMatch::SameScopeMismatch => {
                let digest_uri = metadata
                    .body
                    .as_ref()
                    .map(|body| body.digest_uri.clone())
                    .unwrap_or_else(|| format!("sha256:{}", sha256_hex(&[])));
                return Err(write_outcome_unknown(
                    WORKSPACE_EDIT,
                    &path,
                    &args.operation_id,
                    Some(args.base_generation),
                    &digest_uri,
                ));
            }
            ProofMatch::DifferentScope => {}
        }
        if metadata.attr.generation != args.base_generation {
            return Err(conflict("workspace file generation changed"));
        }

        let source_len = usize::try_from(metadata.attr.size).map_err(|_| payload_too_large())?;
        let source = client
            .read_path(&target, 0, source_len, Some(args.base_generation))
            .map_err(map_read_error)?;
        let source_text = std::str::from_utf8(&source.bytes)
            .map_err(|_| conflict("workspace_edit requires a UTF-8 file"))?;
        let replacements = source_text.matches(&args.old_string).count();
        if replacements == 0 {
            return Err(invalid_argument("old_string was not found"));
        }
        if !replace_all && replacements != 1 {
            return Err(invalid_argument(
                "old_string must match exactly once when replace_all is false",
            ));
        }
        let edited = if replace_all {
            source_text.replace(&args.old_string, &args.new_string)
        } else {
            source_text.replacen(&args.old_string, &args.new_string, 1)
        };
        if edited.len() > self.max_bytes {
            return Err(payload_too_large());
        }
        let replacements = u64::try_from(replacements).map_err(|_| storage_error(false))?;
        let body = metadata.body.as_ref().ok_or_else(|| storage_error(false))?;
        if edited.as_bytes() == source.bytes {
            return self.edit_success(
                &path,
                &args.operation_id,
                args.base_generation,
                replacements,
                &metadata,
                &body.digest_uri,
                &body.content_type,
                true,
                false,
            );
        }

        let edited_bytes = edited.into_bytes();
        let digest_hex = sha256_hex(&edited_bytes);
        let digest_uri = format!("sha256:{digest_hex}");
        let producer = producer_string(
            &self.encoded_workspace_id,
            &path,
            &self.encoded_actor_id,
            WORKSPACE_EDIT,
            &args.operation_id,
            Some(args.base_generation),
            replacements,
            &digest_hex,
        );
        let mut artifact = self.artifact_metadata(
            &target,
            &producer,
            &digest_uri,
            &body.content_type,
            Some(&metadata),
        );
        artifact.manifest_id = edit_request_manifest_id.clone();
        let mut replayed = false;
        loop {
            let result = client.put_artifact_replace_if_generation(
                &target,
                edited_bytes.clone(),
                artifact.clone(),
                args.base_generation,
            );
            match result {
                Ok(result) => {
                    let committed = PathMetadata {
                        attr: result.entry.attr,
                        body: result.entry.body,
                    };
                    return self.edit_success(
                        &path,
                        &args.operation_id,
                        args.base_generation,
                        replacements,
                        &committed,
                        &digest_uri,
                        &artifact.content_type,
                        false,
                        false,
                    );
                }
                Err(err) => {
                    let certainty = artifact_write_commit_status(&err);
                    let current = match inspect_path(client, &target) {
                        Ok(current) => current,
                        Err(read_err)
                            if certainty == ArtifactWriteCommitStatus::DefinitelyNotCommitted =>
                        {
                            return Err(read_err);
                        }
                        Err(_) => {
                            return Err(write_outcome_unknown(
                                WORKSPACE_EDIT,
                                &path,
                                &args.operation_id,
                                Some(args.base_generation),
                                &digest_uri,
                            ));
                        }
                    };
                    match proof_match_with_manifest(
                        current.as_ref(),
                        &producer,
                        &digest_uri,
                        &artifact.content_type,
                        Some(u64::try_from(edited_bytes.len()).map_err(|_| payload_too_large())?),
                        &edit_request_manifest_id,
                    ) {
                        ProofMatch::Exact => {
                            return self.edit_success(
                                &path,
                                &args.operation_id,
                                args.base_generation,
                                replacements,
                                current.as_ref().expect("exact proof requires metadata"),
                                &digest_uri,
                                &artifact.content_type,
                                false,
                                true,
                            );
                        }
                        ProofMatch::SameScopeMismatch => {
                            return Err(write_outcome_unknown(
                                WORKSPACE_EDIT,
                                &path,
                                &args.operation_id,
                                Some(args.base_generation),
                                &digest_uri,
                            ));
                        }
                        ProofMatch::DifferentScope => {}
                    }
                    if certainty == ArtifactWriteCommitStatus::DefinitelyNotCommitted {
                        return Err(map_definite_write_error(&err));
                    }
                    if !replayed && base_is_unchanged(current.as_ref(), Some(args.base_generation))
                    {
                        replayed = true;
                        continue;
                    }
                    return Err(write_outcome_unknown(
                        WORKSPACE_EDIT,
                        &path,
                        &args.operation_id,
                        Some(args.base_generation),
                        &digest_uri,
                    ));
                }
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn edit_success(
        &self,
        path: &str,
        operation_id: &str,
        base_generation: u64,
        replacements: u64,
        metadata: &PathMetadata,
        digest_uri: &str,
        content_type: &str,
        no_change: bool,
        deduplicated: bool,
    ) -> Result<Value, WorkspaceToolError> {
        Ok(json!({
            "status": "success",
            "workspace_id": self.workspace_id,
            "path": path,
            "relative_path": path,
            "operation_id": operation_id,
            "base_generation": base_generation,
            "replacements": replacements,
            "size_bytes": metadata.attr.size,
            "generation": metadata.attr.generation,
            "digest_uri": digest_uri,
            "content_type": content_type,
            "no_change": no_change,
            "deduplicated": deduplicated,
        }))
    }

    fn execute_append<O>(
        &self,
        client: &NoKvFsClient<O>,
        args: &Value,
    ) -> Result<Value, WorkspaceToolError>
    where
        O: ObjectStore + Send + Sync + 'static,
    {
        let args: AppendArgs = parse_args(args, &["path", "operation_id", "base_generation"])?;
        let path = validate_relative_path(&args.path, false)?;
        validate_operation_id(&args.operation_id)?;
        let (delta, default_content_type) = decode_payload(
            args.text.as_deref(),
            args.base64.as_deref(),
            args.content_type.as_deref(),
            true,
            self.max_bytes,
        )?;
        let appended_bytes = u64::try_from(delta.len()).map_err(|_| payload_too_large())?;
        let digest_hex = sha256_hex(&delta);
        let digest_uri = format!("sha256:{digest_hex}");
        let producer = producer_string(
            &self.encoded_workspace_id,
            &path,
            &self.encoded_actor_id,
            WORKSPACE_APPEND,
            &args.operation_id,
            args.base_generation,
            appended_bytes,
            &digest_hex,
        );
        let target = self.absolute_path(&path);
        let mut observed = inspect_path(client, &target)?;
        let effective_content_type = match observed.as_ref() {
            Some(metadata) => {
                require_file(metadata)?;
                if metadata.attr.size > self.max_bytes as u64 {
                    return Err(payload_too_large());
                }
                let body = metadata.body.as_ref().ok_or_else(|| storage_error(false))?;
                args.content_type
                    .as_deref()
                    .unwrap_or(&body.content_type)
                    .to_owned()
            }
            None => default_content_type,
        };
        let expected_size = args.base_generation.is_none().then_some(appended_bytes);
        match proof_match(
            observed.as_ref(),
            &producer,
            &digest_uri,
            &effective_content_type,
            expected_size,
        ) {
            ProofMatch::Exact => {
                return self.append_success(
                    &path,
                    &args.operation_id,
                    args.base_generation,
                    appended_bytes,
                    observed.as_ref().expect("exact proof requires metadata"),
                    &digest_uri,
                    &effective_content_type,
                    true,
                );
            }
            ProofMatch::SameScopeMismatch => {
                return Err(write_outcome_unknown(
                    WORKSPACE_APPEND,
                    &path,
                    &args.operation_id,
                    args.base_generation,
                    &digest_uri,
                ));
            }
            ProofMatch::DifferentScope => {}
        }

        match (args.base_generation, observed.as_ref()) {
            (None, Some(_)) => return Err(conflict("append-create target already exists")),
            (Some(_), None) => return Err(not_found()),
            (Some(expected), Some(metadata)) => {
                require_file(metadata)?;
                if metadata.attr.generation != expected {
                    return Err(conflict("workspace file generation changed"));
                }
                let body = metadata.body.as_ref().ok_or_else(|| storage_error(false))?;
                if args
                    .content_type
                    .as_deref()
                    .is_some_and(|requested| requested != body.content_type)
                {
                    return Err(conflict("content_type differs from the existing file"));
                }
                if metadata
                    .attr
                    .size
                    .checked_add(appended_bytes)
                    .is_none_or(|size| size > self.max_bytes as u64)
                {
                    return Err(payload_too_large());
                }
            }
            (None, None) => {
                ensure_parent_dirs(client, self, &path)?;
                observed = inspect_path(client, &target)?;
                match proof_match(
                    observed.as_ref(),
                    &producer,
                    &digest_uri,
                    &effective_content_type,
                    Some(appended_bytes),
                ) {
                    ProofMatch::Exact => {
                        return self.append_success(
                            &path,
                            &args.operation_id,
                            args.base_generation,
                            appended_bytes,
                            observed.as_ref().expect("exact proof requires metadata"),
                            &digest_uri,
                            &effective_content_type,
                            true,
                        );
                    }
                    ProofMatch::SameScopeMismatch => {
                        return Err(write_outcome_unknown(
                            WORKSPACE_APPEND,
                            &path,
                            &args.operation_id,
                            args.base_generation,
                            &digest_uri,
                        ));
                    }
                    ProofMatch::DifferentScope if observed.is_some() => {
                        return Err(conflict("append-create target already exists"));
                    }
                    ProofMatch::DifferentScope => {}
                }
            }
        }

        let artifact = self.artifact_metadata(
            &target,
            &producer,
            &digest_uri,
            &effective_content_type,
            observed.as_ref(),
        );
        let mut replayed = false;
        loop {
            let result = match args.base_generation {
                None => client
                    .put_artifact(&target, delta.clone(), artifact.clone())
                    .map(|entry| PathMetadata {
                        attr: entry.attr,
                        body: entry.body,
                    }),
                Some(expected) => client
                    .append_artifact(&target, delta.clone(), artifact.clone(), Some(expected))
                    .map(|outcome| {
                        let mut attr = observed
                            .as_ref()
                            .expect("generation-pinned append has preflight metadata")
                            .attr
                            .clone();
                        attr.size = outcome.new_size;
                        attr.generation = outcome.generation;
                        PathMetadata { attr, body: None }
                    }),
            };
            match result {
                Ok(metadata) => {
                    return self.append_success(
                        &path,
                        &args.operation_id,
                        args.base_generation,
                        appended_bytes,
                        &metadata,
                        &digest_uri,
                        &effective_content_type,
                        false,
                    );
                }
                Err(err) => {
                    let certainty = artifact_write_commit_status(&err);
                    let current = match inspect_path(client, &target) {
                        Ok(current) => current,
                        Err(read_err)
                            if certainty == ArtifactWriteCommitStatus::DefinitelyNotCommitted =>
                        {
                            return Err(read_err);
                        }
                        Err(_) => {
                            return Err(write_outcome_unknown(
                                WORKSPACE_APPEND,
                                &path,
                                &args.operation_id,
                                args.base_generation,
                                &digest_uri,
                            ));
                        }
                    };
                    match proof_match(
                        current.as_ref(),
                        &producer,
                        &digest_uri,
                        &effective_content_type,
                        expected_size,
                    ) {
                        ProofMatch::Exact => {
                            return self.append_success(
                                &path,
                                &args.operation_id,
                                args.base_generation,
                                appended_bytes,
                                current.as_ref().expect("exact proof requires metadata"),
                                &digest_uri,
                                &effective_content_type,
                                true,
                            );
                        }
                        ProofMatch::SameScopeMismatch => {
                            return Err(write_outcome_unknown(
                                WORKSPACE_APPEND,
                                &path,
                                &args.operation_id,
                                args.base_generation,
                                &digest_uri,
                            ));
                        }
                        ProofMatch::DifferentScope => {}
                    }
                    if certainty == ArtifactWriteCommitStatus::DefinitelyNotCommitted {
                        return Err(map_definite_write_error(&err));
                    }
                    if !replayed && base_is_unchanged(current.as_ref(), args.base_generation) {
                        replayed = true;
                        continue;
                    }
                    return Err(write_outcome_unknown(
                        WORKSPACE_APPEND,
                        &path,
                        &args.operation_id,
                        args.base_generation,
                        &digest_uri,
                    ));
                }
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn append_success(
        &self,
        path: &str,
        operation_id: &str,
        base_generation: Option<u64>,
        appended_bytes: u64,
        metadata: &PathMetadata,
        delta_digest_uri: &str,
        content_type: &str,
        deduplicated: bool,
    ) -> Result<Value, WorkspaceToolError> {
        Ok(json!({
            "status": "success",
            "workspace_id": self.workspace_id,
            "path": path,
            "relative_path": path,
            "operation_id": operation_id,
            "base_generation": base_generation,
            "appended_bytes": appended_bytes,
            "size_bytes": metadata.attr.size,
            "generation": metadata.attr.generation,
            "created": base_generation.is_none(),
            "delta_digest_uri": delta_digest_uri,
            "content_type": content_type,
            "deduplicated": deduplicated,
        }))
    }

    fn absolute_path(&self, relative_path: &str) -> String {
        if relative_path.is_empty() {
            self.workspace_root()
        } else {
            format!("{}/{}", self.workspace_root(), relative_path)
        }
    }

    fn artifact_metadata(
        &self,
        target: &str,
        producer: &str,
        digest_uri: &str,
        content_type: &str,
        existing: Option<&PathMetadata>,
    ) -> ArtifactMetadata {
        ArtifactMetadata {
            producer: producer.to_owned(),
            digest_uri: digest_uri.to_owned(),
            content_type: content_type.to_owned(),
            manifest_id: target.trim_start_matches('/').to_owned(),
            mode: existing.map_or(FILE_MODE, |metadata| metadata.attr.mode),
            uid: existing.map_or(self.uid, |metadata| metadata.attr.uid),
            gid: existing.map_or(self.gid, |metadata| metadata.attr.gid),
        }
    }

    fn bound_success(&self, value: Value) -> Result<Value, WorkspaceToolError> {
        let encoded_len = serde_json::to_vec(&value)
            .map_err(|_| storage_error(false))?
            .len();
        if encoded_len > self.max_bytes {
            return Err(payload_too_large());
        }
        Ok(value)
    }
}

impl<O> McpToolProvider<O> for SharedWorkspaceProvider
where
    O: ObjectStore + Send + Sync + 'static,
{
    fn name(&self) -> &'static str {
        PROVIDER_NAME
    }

    fn complete_tool_definitions(&self) -> Vec<AgentToolDefinition> {
        complete_tool_definitions()
    }

    fn advertised_tool_names(&self, _client: &NoKvFsClient<O>) -> Vec<&'static str> {
        self.current_role()
            .map(|role| role.advertised().to_vec())
            .unwrap_or_default()
    }

    fn execute_tool(
        &self,
        client: &NoKvFsClient<O>,
        name: &str,
        args: &Value,
    ) -> Result<Value, Value> {
        self.execute(client, name, args)
            .map_err(WorkspaceToolError::into_value)
    }
}

#[derive(Clone, Debug, PartialEq)]
struct WorkspaceToolError {
    code: &'static str,
    message: String,
    retryable: bool,
    details: Value,
}

impl WorkspaceToolError {
    fn new(
        code: &'static str,
        message: impl Into<String>,
        retryable: bool,
        details: Value,
    ) -> Self {
        Self {
            code,
            message: message.into(),
            retryable,
            details,
        }
    }

    fn permission_denied() -> Self {
        Self::new(
            "WorkspacePermissionDenied",
            "workspace capability does not permit this tool",
            false,
            json!({}),
        )
    }

    fn into_value(self) -> Value {
        json!({
            "status": "error",
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        })
    }
}

fn parse_args<T>(args: &Value, required: &[&str]) -> Result<T, WorkspaceToolError>
where
    T: DeserializeOwned,
{
    let object = args
        .as_object()
        .ok_or_else(|| invalid_argument("arguments must be a JSON object"))?;
    if required.iter().any(|field| !object.contains_key(*field)) {
        return Err(invalid_argument("required argument is missing"));
    }
    serde_json::from_value(args.clone())
        .map_err(|_| invalid_argument("arguments do not match the v1 tool schema"))
}

fn invalid_argument(message: &'static str) -> WorkspaceToolError {
    WorkspaceToolError::new("WorkspaceInvalidArgument", message, false, json!({}))
}

fn not_found() -> WorkspaceToolError {
    WorkspaceToolError::new(
        "WorkspaceNotFound",
        "workspace path was not found",
        false,
        json!({}),
    )
}

fn conflict(message: &'static str) -> WorkspaceToolError {
    WorkspaceToolError::new("WorkspaceConflict", message, false, json!({}))
}

fn payload_too_large() -> WorkspaceToolError {
    WorkspaceToolError::new(
        "WorkspacePayloadTooLarge",
        "workspace payload or result exceeds the configured byte ceiling",
        false,
        json!({}),
    )
}

fn storage_error(retryable: bool) -> WorkspaceToolError {
    WorkspaceToolError::new(
        "WorkspaceStorageError",
        "NoKV workspace storage operation failed",
        retryable,
        json!({}),
    )
}

fn write_outcome_unknown(
    tool: &'static str,
    path: &str,
    operation_id: &str,
    base_generation: Option<u64>,
    digest_uri: &str,
) -> WorkspaceToolError {
    WorkspaceToolError::new(
        "WorkspaceWriteOutcomeUnknown",
        "workspace write outcome could not be proven",
        false,
        json!({
            "tool": tool,
            "path": path,
            "operation_id": operation_id,
            "base_generation": base_generation,
            "digest_uri": digest_uri,
            "reconciliation": WRITE_RECONCILIATION,
        }),
    )
}

fn map_read_error(err: ClientError) -> WorkspaceToolError {
    if is_metadata_not_found(&err) {
        return not_found();
    }
    if matches!(
        err,
        ClientError::Metadata(
            MetadError::StaleBodyGeneration { .. } | MetadError::NotFile | MetadError::NotDirectory
        )
    ) {
        return conflict("workspace metadata changed during the operation");
    }
    storage_error(matches!(err, ClientError::Io(_)))
}

fn map_definite_write_error(err: &ClientError) -> WorkspaceToolError {
    if is_artifact_write_conflict(err)
        || matches!(
            err,
            ClientError::ArtifactIsDirectory(_)
                | ClientError::ArtifactIsFile(_)
                | ClientError::Metadata(
                    MetadError::StaleBodyGeneration { .. }
                        | MetadError::NotFile
                        | MetadError::NotDirectory
                )
        )
    {
        return conflict("workspace write lost its generation guard");
    }
    if is_metadata_not_found(err) {
        return not_found();
    }
    storage_error(false)
}

fn validate_operation_id(value: &str) -> Result<(), WorkspaceToolError> {
    if valid_operation_id(value) {
        Ok(())
    } else {
        Err(invalid_argument(
            "operation_id must match [A-Za-z0-9_-]{1,64}",
        ))
    }
}

fn validate_content_type(value: &str) -> Result<(), WorkspaceToolError> {
    if (1..=255).contains(&value.len()) && value.bytes().all(|byte| (0x20..=0x7e).contains(&byte)) {
        Ok(())
    } else {
        Err(invalid_argument(
            "content_type must contain 1..255 visible ASCII bytes",
        ))
    }
}

fn decode_payload(
    text: Option<&str>,
    encoded: Option<&str>,
    content_type: Option<&str>,
    require_nonempty: bool,
    max_bytes: usize,
) -> Result<(Vec<u8>, String), WorkspaceToolError> {
    let (bytes, default_content_type) = match (text, encoded) {
        (Some(text), None) => (
            text.as_bytes().to_vec(),
            "text/plain; charset=utf-8".to_owned(),
        ),
        (None, Some(encoded)) => {
            let bytes = STANDARD
                .decode(encoded.as_bytes())
                .map_err(|_| invalid_argument("base64 payload is not canonical RFC 4648"))?;
            if STANDARD.encode(&bytes) != encoded {
                return Err(invalid_argument("base64 payload is not canonical RFC 4648"));
            }
            (bytes, "application/octet-stream".to_owned())
        }
        _ => {
            return Err(invalid_argument("provide exactly one of text or base64"));
        }
    };
    if require_nonempty && bytes.is_empty() {
        return Err(invalid_argument("append payload must not be empty"));
    }
    if bytes.len() > max_bytes {
        return Err(payload_too_large());
    }
    let content_type = content_type.unwrap_or(&default_content_type).to_owned();
    validate_content_type(&content_type)?;
    Ok((bytes, content_type))
}

fn stat_path_or_absent<O>(
    client: &NoKvFsClient<O>,
    path: &str,
) -> Result<Option<PathMetadata>, WorkspaceToolError>
where
    O: ObjectStore + Send + Sync + 'static,
{
    match client.metadata().stat_path(path) {
        Ok(metadata) => Ok(metadata),
        Err(err) if is_metadata_not_found(&err) => Ok(None),
        Err(err) => Err(map_read_error(err)),
    }
}

fn inspect_path<O>(
    client: &NoKvFsClient<O>,
    absolute_path: &str,
) -> Result<Option<PathMetadata>, WorkspaceToolError>
where
    O: ObjectStore + Send + Sync + 'static,
{
    let components: Vec<&str> = absolute_path
        .strip_prefix('/')
        .unwrap_or(absolute_path)
        .split('/')
        .filter(|component| !component.is_empty())
        .collect();
    let mut current = String::new();
    for (index, component) in components.iter().enumerate() {
        current.push('/');
        current.push_str(component);
        let Some(metadata) = stat_path_or_absent(client, &current)? else {
            return Ok(None);
        };
        let terminal = index + 1 == components.len();
        match metadata.attr.file_type {
            FileType::Symlink
            | FileType::NamedPipe
            | FileType::CharDevice
            | FileType::BlockDevice
            | FileType::Socket => return Err(path_violation()),
            FileType::Directory => {
                if terminal {
                    return Ok(Some(metadata));
                }
            }
            FileType::File => {
                if terminal {
                    return Ok(Some(metadata));
                }
                return Err(conflict("workspace path component is not a directory"));
            }
        }
    }
    Ok(None)
}

fn require_directory(metadata: &PathMetadata) -> Result<(), WorkspaceToolError> {
    match metadata.attr.file_type {
        FileType::Directory => Ok(()),
        FileType::Symlink
        | FileType::NamedPipe
        | FileType::CharDevice
        | FileType::BlockDevice
        | FileType::Socket => Err(path_violation()),
        FileType::File => Err(conflict("workspace path is not a directory")),
    }
}

fn require_file(metadata: &PathMetadata) -> Result<(), WorkspaceToolError> {
    match metadata.attr.file_type {
        FileType::File => Ok(()),
        FileType::Symlink
        | FileType::NamedPipe
        | FileType::CharDevice
        | FileType::BlockDevice
        | FileType::Socket => Err(path_violation()),
        FileType::Directory => Err(conflict("workspace path is not a file")),
    }
}

fn ensure_parent_dirs<O>(
    client: &NoKvFsClient<O>,
    provider: &SharedWorkspaceProvider,
    relative_path: &str,
) -> Result<(), WorkspaceToolError>
where
    O: ObjectStore + Send + Sync + 'static,
{
    client
        .metadata()
        .bootstrap_root(DIRECTORY_MODE, provider.uid, provider.gid)
        .map_err(map_read_error)?;
    let target = provider.absolute_path(relative_path);
    let parent = target
        .rsplit_once('/')
        .map(|(parent, _)| parent)
        .filter(|parent| !parent.is_empty())
        .ok_or_else(path_violation)?;
    let mut current = String::new();
    for component in parent.trim_start_matches('/').split('/') {
        current.push('/');
        current.push_str(component);
        match stat_path_or_absent(client, &current)? {
            Some(metadata) => require_directory(&metadata)?,
            None => {
                let create_result =
                    client
                        .metadata()
                        .mkdir(&current, DIRECTORY_MODE, provider.uid, provider.gid);
                match stat_path_or_absent(client, &current)? {
                    Some(metadata) => require_directory(&metadata)?,
                    None => {
                        return Err(create_result
                            .err()
                            .map(map_read_error)
                            .unwrap_or_else(|| storage_error(false)));
                    }
                }
            }
        }
    }
    Ok(())
}

fn base_is_unchanged(metadata: Option<&PathMetadata>, base_generation: Option<u64>) -> bool {
    match (metadata, base_generation) {
        (None, None) => true,
        (Some(metadata), Some(expected)) => {
            metadata.attr.file_type == FileType::File && metadata.attr.generation == expected
        }
        _ => false,
    }
}

fn proof_match(
    metadata: Option<&PathMetadata>,
    expected_producer: &str,
    expected_digest_uri: &str,
    expected_content_type: &str,
    expected_size: Option<u64>,
) -> ProofMatch {
    proof_match_inner(
        metadata,
        expected_producer,
        expected_digest_uri,
        expected_content_type,
        expected_size,
        None,
    )
}

fn proof_match_with_manifest(
    metadata: Option<&PathMetadata>,
    expected_producer: &str,
    expected_digest_uri: &str,
    expected_content_type: &str,
    expected_size: Option<u64>,
    expected_manifest_id: &str,
) -> ProofMatch {
    proof_match_inner(
        metadata,
        expected_producer,
        expected_digest_uri,
        expected_content_type,
        expected_size,
        Some(expected_manifest_id),
    )
}

fn proof_match_inner(
    metadata: Option<&PathMetadata>,
    expected_producer: &str,
    expected_digest_uri: &str,
    expected_content_type: &str,
    expected_size: Option<u64>,
    expected_manifest_id: Option<&str>,
) -> ProofMatch {
    let Some(metadata) = metadata.filter(|metadata| metadata.attr.file_type == FileType::File)
    else {
        return ProofMatch::DifferentScope;
    };
    let Some(body) = metadata.body.as_ref() else {
        return ProofMatch::DifferentScope;
    };
    let Some(expected) = parse_producer(expected_producer) else {
        return ProofMatch::DifferentScope;
    };
    let Some(current) = parse_producer(&body.producer) else {
        return ProofMatch::DifferentScope;
    };
    if !same_producer_scope(&current, &expected) {
        return ProofMatch::DifferentScope;
    }
    let size_matches = expected_size.is_none_or(|size| size == metadata.attr.size);
    let manifest_matches =
        expected_manifest_id.is_none_or(|manifest_id| body.manifest_id == manifest_id);
    if body.producer == expected_producer
        && body.digest_uri == expected_digest_uri
        && body.content_type == expected_content_type
        && body.size == metadata.attr.size
        && body.generation == metadata.attr.generation
        && size_matches
        && manifest_matches
    {
        ProofMatch::Exact
    } else {
        ProofMatch::SameScopeMismatch
    }
}

fn edit_proof_match(
    metadata: Option<&PathMetadata>,
    encoded_workspace_id: &str,
    relative_path: &str,
    encoded_actor_id: &str,
    operation_id: &str,
    base_generation: u64,
    expected_manifest_id: &str,
) -> (ProofMatch, Option<ProducerProof>) {
    let Some(metadata) = metadata.filter(|metadata| metadata.attr.file_type == FileType::File)
    else {
        return (ProofMatch::DifferentScope, None);
    };
    let Some(body) = metadata.body.as_ref() else {
        return (ProofMatch::DifferentScope, None);
    };
    let Some(proof) = parse_producer(&body.producer) else {
        return (ProofMatch::DifferentScope, None);
    };
    let scope_matches = proof.workspace == encoded_workspace_id
        && proof.path == sha256_hex(relative_path.as_bytes())
        && proof.actor == encoded_actor_id
        && proof.tool == WORKSPACE_EDIT
        && proof.operation == operation_id;
    if !scope_matches {
        return (ProofMatch::DifferentScope, None);
    }
    let digest_uri = format!("sha256:{}", proof.digest_hex);
    if proof.base_generation == Some(base_generation)
        && proof.effect > 0
        && body.digest_uri == digest_uri
        && body.size == metadata.attr.size
        && body.generation == metadata.attr.generation
        && body.manifest_id == expected_manifest_id
    {
        (ProofMatch::Exact, Some(proof))
    } else {
        (ProofMatch::SameScopeMismatch, Some(proof))
    }
}

fn same_producer_scope(left: &ProducerProof, right: &ProducerProof) -> bool {
    left.workspace == right.workspace
        && left.path == right.path
        && left.actor == right.actor
        && left.tool == right.tool
        && left.operation == right.operation
}

fn parse_producer(value: &str) -> Option<ProducerProof> {
    let parts: Vec<&str> = value.split(';').collect();
    if parts.len() != 9 || parts[0] != PRODUCER_PREFIX {
        return None;
    }
    let workspace = parts[1].strip_prefix("workspace=")?;
    let path = parts[2].strip_prefix("path=")?;
    let actor = parts[3].strip_prefix("actor=")?;
    let tool = parts[4].strip_prefix("tool=")?;
    let operation = parts[5].strip_prefix("operation=")?;
    let base = parts[6].strip_prefix("base=")?;
    let effect = parts[7].strip_prefix("effect=")?;
    let digest_hex = parts[8].strip_prefix("digest=")?;
    if URL_SAFE_NO_PAD
        .decode(workspace.as_bytes())
        .ok()
        .as_ref()
        .is_none_or(|bytes| URL_SAFE_NO_PAD.encode(bytes) != workspace)
        || URL_SAFE_NO_PAD
            .decode(actor.as_bytes())
            .ok()
            .as_ref()
            .is_none_or(|bytes| URL_SAFE_NO_PAD.encode(bytes) != actor)
        || !valid_lower_hex(path)
        || !valid_lower_hex(digest_hex)
        || !matches!(tool, WORKSPACE_PUT_FILE | WORKSPACE_EDIT | WORKSPACE_APPEND)
        || !valid_operation_id(operation)
    {
        return None;
    }
    let base_generation = if base == "none" {
        None
    } else {
        Some(parse_canonical_u64(base)?)
    };
    Some(ProducerProof {
        workspace: workspace.to_owned(),
        path: path.to_owned(),
        actor: actor.to_owned(),
        tool: tool.to_owned(),
        operation: operation.to_owned(),
        base_generation,
        effect: parse_canonical_u64(effect)?,
        digest_hex: digest_hex.to_owned(),
    })
}

fn parse_canonical_u64(value: &str) -> Option<u64> {
    if value.is_empty() || (value.len() > 1 && value.starts_with('0')) {
        return None;
    }
    let parsed = value.parse::<u64>().ok()?;
    (parsed.to_string() == value).then_some(parsed)
}

fn valid_lower_hex(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn validate_identity(field: &'static str, value: &str) -> Result<(), SharedWorkspaceConfigError> {
    let len = value.len();
    if !(1..=64).contains(&len) {
        return Err(SharedWorkspaceConfigError::InvalidIdentity {
            field,
            reason: "must contain 1..64 UTF-8 bytes".to_owned(),
        });
    }
    if value.trim() != value {
        return Err(SharedWorkspaceConfigError::InvalidIdentity {
            field,
            reason: "must not have trim-visible leading or trailing whitespace".to_owned(),
        });
    }
    if value.chars().any(char::is_control) {
        return Err(SharedWorkspaceConfigError::InvalidIdentity {
            field,
            reason: "must not contain Unicode control characters".to_owned(),
        });
    }
    Ok(())
}

fn validate_launcher_grant(
    encoded: &str,
    workspace_id: &str,
    actor_id: &str,
    now_unix_ms: u64,
) -> Result<Membership, SharedWorkspaceConfigError> {
    let decoded = URL_SAFE_NO_PAD.decode(encoded.as_bytes()).map_err(|_| {
        SharedWorkspaceConfigError::InvalidLauncherGrant(
            "must be canonical URL-safe Base64 without padding".to_owned(),
        )
    })?;
    if URL_SAFE_NO_PAD.encode(&decoded) != encoded {
        return Err(SharedWorkspaceConfigError::InvalidLauncherGrant(
            "must be canonical URL-safe Base64 without padding".to_owned(),
        ));
    }
    let grant: LauncherGrant = serde_json::from_slice(&decoded).map_err(|_| {
        SharedWorkspaceConfigError::InvalidLauncherGrant(
            "must be canonical grant JSON with exactly the v1 fields".to_owned(),
        )
    })?;
    let canonical = canonical_grant_json(&grant).map_err(|_| {
        SharedWorkspaceConfigError::InvalidLauncherGrant(
            "could not canonicalize grant JSON".to_owned(),
        )
    })?;
    if decoded != canonical {
        return Err(SharedWorkspaceConfigError::InvalidLauncherGrant(
            "decoded bytes are not canonical JSON".to_owned(),
        ));
    }
    if grant.schema != GRANT_SCHEMA
        || grant.issuer != GRANT_ISSUER
        || grant.audience != GRANT_AUDIENCE
    {
        return Err(SharedWorkspaceConfigError::InvalidLauncherGrant(
            "grant constants do not match the LingTai v1 audience".to_owned(),
        ));
    }
    if !valid_operation_id(&grant.grant_id) {
        return Err(SharedWorkspaceConfigError::InvalidLauncherGrant(
            "grant_id must match [A-Za-z0-9_-]{1,64}".to_owned(),
        ));
    }
    validate_identity("grant workspace id", &grant.workspace_id)?;
    validate_identity("grant actor id", &grant.actor_id)?;
    if grant.workspace_id != workspace_id || grant.actor_id != actor_id {
        return Err(SharedWorkspaceConfigError::InvalidLauncherGrant(
            "grant identity does not match the explicit workspace tuple".to_owned(),
        ));
    }
    let role = WorkspaceRole::parse(&grant.role).ok_or_else(|| {
        SharedWorkspaceConfigError::InvalidLauncherGrant(
            "grant role must be reader or writer".to_owned(),
        )
    })?;
    let lifetime = grant
        .expires_at_unix_ms
        .checked_sub(grant.issued_at_unix_ms)
        .filter(|lifetime| *lifetime > 0 && *lifetime <= MAX_GRANT_LIFETIME_MS)
        .ok_or_else(|| {
            SharedWorkspaceConfigError::InvalidLauncherGrant(
                "grant lifetime must be positive and at most 30 days".to_owned(),
            )
        })?;
    let _ = lifetime;
    if grant.issued_at_unix_ms > now_unix_ms || now_unix_ms >= grant.expires_at_unix_ms {
        return Err(SharedWorkspaceConfigError::InvalidLauncherGrant(
            "grant is not current".to_owned(),
        ));
    }
    Ok(Membership::Launcher {
        role,
        expires_at_unix_ms: grant.expires_at_unix_ms,
    })
}

fn canonical_grant_json(grant: &LauncherGrant) -> Result<Vec<u8>, serde_json::Error> {
    let mut fields = BTreeMap::new();
    fields.insert("actor_id", json!(grant.actor_id));
    fields.insert("audience", json!(grant.audience));
    fields.insert("expires_at_unix_ms", json!(grant.expires_at_unix_ms));
    fields.insert("grant_id", json!(grant.grant_id));
    fields.insert("issued_at_unix_ms", json!(grant.issued_at_unix_ms));
    fields.insert("issuer", json!(grant.issuer));
    fields.insert("role", json!(grant.role));
    fields.insert("schema", json!(grant.schema));
    fields.insert("workspace_id", json!(grant.workspace_id));
    serde_json::to_vec(&fields)
}

fn valid_operation_id(value: &str) -> bool {
    (1..=64).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
}

fn validate_relative_path(value: &str, allow_empty: bool) -> Result<String, WorkspaceToolError> {
    if value.is_empty() && allow_empty {
        return Ok(String::new());
    }
    if value.is_empty()
        || value.len() > 1024
        || value.starts_with('/')
        || value.ends_with('/')
        || value.contains("//")
        || value.contains('\\')
        || value.chars().any(char::is_control)
    {
        return Err(path_violation());
    }
    let mut count = 0_usize;
    for segment in value.split('/') {
        count += 1;
        if segment.is_empty() || segment.len() > 255 || segment == "." || segment == ".." {
            return Err(path_violation());
        }
    }
    if count > 64 {
        return Err(path_violation());
    }
    Ok(value.to_owned())
}

fn path_violation() -> WorkspaceToolError {
    WorkspaceToolError::new(
        "WorkspacePathViolation",
        "path violates the shared workspace jail contract",
        false,
        json!({}),
    )
}

fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn edit_request_manifest_id(old_string: &str, new_string: &str, replace_all: bool) -> String {
    let mut digest = Sha256::new();
    digest.update(EDIT_REQUEST_MANIFEST_PREFIX.as_bytes());
    digest.update([0]);
    digest.update((old_string.len() as u128).to_be_bytes());
    digest.update(old_string.as_bytes());
    digest.update((new_string.len() as u128).to_be_bytes());
    digest.update(new_string.as_bytes());
    digest.update([u8::from(replace_all)]);
    format!("{EDIT_REQUEST_MANIFEST_PREFIX}:{:x}", digest.finalize())
}

#[allow(clippy::too_many_arguments)]
fn producer_string(
    encoded_workspace_id: &str,
    relative_path: &str,
    encoded_actor_id: &str,
    tool: &str,
    operation_id: &str,
    base_generation: Option<u64>,
    effect: u64,
    digest_hex: &str,
) -> String {
    let base = base_generation
        .map(|generation| generation.to_string())
        .unwrap_or_else(|| "none".to_owned());
    format!(
        "{PRODUCER_PREFIX};workspace={encoded_workspace_id};path={};actor={encoded_actor_id};tool={tool};operation={operation_id};base={base};effect={effect};digest={digest_hex}",
        sha256_hex(relative_path.as_bytes())
    )
}

pub fn complete_tool_definitions() -> Vec<AgentToolDefinition> {
    vec![
        AgentToolDefinition {
            name: WORKSPACE_LIST,
            description: "List one non-recursive page of direct children under a workspace-relative directory.",
            parameters: json!({
                "type": "object",
                "additionalProperties": false,
                "properties": {
                    "path": {"description": "Missing, null, or empty means the shared root; otherwise a valid relative path.", "type": ["string", "null"]},
                    "offset": {"description": "Zero-based direct-child offset. A positive offset requires if_read_version from the preceding page.", "maximum": u64::MAX, "minimum": 0, "type": "integer"},
                    "if_read_version": {"description": "NoKV namespace read version returned by a preceding page, or null only for an initial offset-zero page.", "maximum": u64::MAX, "minimum": 0, "type": ["integer", "null"]},
                    "limit": {"description": "Maximum direct-child entries in this page.", "maximum": 100, "minimum": 1, "type": "integer"}
                }
            }),
        },
        AgentToolDefinition {
            name: WORKSPACE_READ,
            description: "Read one typed page from a workspace-relative file through the NoKV paged-read primitive.",
            parameters: json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["path"],
                "properties": {
                    "path": {"description": "Workspace-relative file path.", "type": "string"},
                    "format": {"description": "structured returns typed records; bytes returns raw bytes encoded in the success envelope.", "enum": ["structured", "bytes"], "type": "string"},
                    "offset": {"description": "Starting record index in structured format or starting byte offset in bytes format. A positive offset requires if_generation from the preceding page.", "maximum": u64::MAX, "minimum": 0, "type": "integer"},
                    "if_generation": {"description": "File generation returned by a preceding page, or null only for an initial offset-zero page.", "maximum": u64::MAX, "minimum": 0, "type": ["integer", "null"]},
                    "limit": {"description": "Maximum records in structured format or bytes in bytes format.", "maximum": 300, "minimum": 1, "type": "integer"}
                }
            }),
        },
        AgentToolDefinition {
            name: WORKSPACE_PUT_FILE,
            description: "Idempotent create-only or generation-pinned replace-only publication of one complete workspace file; never upsert.",
            parameters: json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["path", "operation_id", "base_generation"],
                "properties": {
                    "path": {"description": "Workspace-relative file path.", "type": "string"},
                    "operation_id": {"description": "Caller-stable idempotency identifier.", "pattern": "^[A-Za-z0-9_-]{1,64}$", "type": "string"},
                    "base_generation": {"description": "Null selects create-only after observing absence; an integer selects replace-only at exactly that observed generation.", "maximum": u64::MAX, "minimum": 0, "type": ["integer", "null"]},
                    "text": {"description": "Exact UTF-8 payload bytes; empty text is valid.", "type": "string"},
                    "base64": {"description": "Canonical RFC 4648 standard Base64 payload; an empty decoded payload is valid.", "type": "string"},
                    "content_type": {"description": "Visible ASCII content type. Defaults by selected payload field.", "maxLength": 255, "minLength": 1, "pattern": "^[ -~]+$", "type": "string"}
                },
                "oneOf": [
                    {"required": ["text"], "not": {"required": ["base64"]}},
                    {"required": ["base64"], "not": {"required": ["text"]}}
                ]
            }),
        },
        AgentToolDefinition {
            name: WORKSPACE_EDIT,
            description: "Idempotently replace exact UTF-8 text at one caller-observed generation.",
            parameters: json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["path", "operation_id", "base_generation", "old_string", "new_string"],
                "properties": {
                    "path": {"description": "Workspace-relative file path.", "type": "string"},
                    "operation_id": {"description": "Caller-stable idempotency identifier.", "pattern": "^[A-Za-z0-9_-]{1,64}$", "type": "string"},
                    "base_generation": {"description": "Exact existing generation observed by the caller.", "maximum": u64::MAX, "minimum": 0, "type": "integer"},
                    "old_string": {"description": "Non-empty exact UTF-8 string to find.", "minLength": 1, "type": "string"},
                    "new_string": {"description": "Exact UTF-8 replacement; empty is valid.", "type": "string"},
                    "replace_all": {"description": "False requires exactly one match; true replaces every match.", "type": "boolean"}
                }
            }),
        },
        AgentToolDefinition {
            name: WORKSPACE_APPEND,
            description: "Idempotently append or create one file, pinned to the caller-observed base generation.",
            parameters: json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["path", "operation_id", "base_generation"],
                "properties": {
                    "path": {"description": "Workspace-relative file path.", "type": "string"},
                    "operation_id": {"description": "Caller-stable idempotency identifier.", "pattern": "^[A-Za-z0-9_-]{1,64}$", "type": "string"},
                    "base_generation": {"description": "Generation observed by the caller, or null only when the caller observed the path absent.", "maximum": u64::MAX, "minimum": 0, "type": ["integer", "null"]},
                    "text": {"description": "Exact non-empty UTF-8 delta bytes.", "minLength": 1, "type": "string"},
                    "base64": {"description": "Canonical RFC 4648 standard Base64 encoding of a non-empty delta.", "minLength": 1, "type": "string"},
                    "content_type": {"description": "Visible ASCII content type. On an existing file it must equal the persisted content type; on create it defaults by payload field.", "maxLength": 255, "minLength": 1, "pattern": "^[ -~]+$", "type": "string"}
                },
                "oneOf": [
                    {"required": ["text"], "not": {"required": ["base64"]}},
                    {"required": ["base64"], "not": {"required": ["text"]}}
                ]
            }),
        },
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    #[derive(Clone)]
    struct FakeClock(Arc<AtomicU64>);

    impl WorkspaceClock for FakeClock {
        fn now_unix_ms(&self) -> u64 {
            self.0.load(Ordering::SeqCst)
        }
    }

    fn canonical_grant(role: &str, expires_at_unix_ms: u64) -> String {
        let grant = LauncherGrant {
            schema: GRANT_SCHEMA.to_owned(),
            grant_id: "grant_1".to_owned(),
            issuer: GRANT_ISSUER.to_owned(),
            audience: GRANT_AUDIENCE.to_owned(),
            workspace_id: "team-alpha".to_owned(),
            actor_id: "agent-7".to_owned(),
            role: role.to_owned(),
            issued_at_unix_ms: 10,
            expires_at_unix_ms,
        };
        URL_SAFE_NO_PAD.encode(canonical_grant_json(&grant).unwrap())
    }

    fn canonical_value(value: Value) -> Value {
        match value {
            Value::Array(values) => Value::Array(values.into_iter().map(canonical_value).collect()),
            Value::Object(values) => {
                let mut keys = values.into_iter().collect::<Vec<_>>();
                keys.sort_by(|left, right| left.0.as_bytes().cmp(right.0.as_bytes()));
                Value::Object(
                    keys.into_iter()
                        .map(|(key, value)| (key, canonical_value(value)))
                        .collect(),
                )
            }
            other => other,
        }
    }

    fn definitions_digest(definitions: &[AgentToolDefinition]) -> String {
        let payload = definitions
            .iter()
            .map(|definition| {
                json!({
                    "name": definition.name,
                    "description": definition.description,
                    "inputSchema": definition.parameters,
                })
            })
            .collect::<Vec<_>>();
        let bytes = serde_json::to_vec(&canonical_value(json!(payload))).unwrap();
        sha256_hex(&bytes)
    }

    fn options_with_grant(grant: String) -> SharedWorkspaceProviderOptions {
        SharedWorkspaceProviderOptions {
            workspace_id: "team-alpha".to_owned(),
            actor_id: "agent-7".to_owned(),
            dev_membership: None,
            launcher_grant: Some(grant),
            max_bytes: 1024,
            uid: 1000,
            gid: 1000,
        }
    }

    fn development_provider(max_bytes: usize) -> SharedWorkspaceProvider {
        SharedWorkspaceProvider::new(SharedWorkspaceProviderOptions {
            workspace_id: "team-alpha".to_owned(),
            actor_id: "agent-7".to_owned(),
            dev_membership: Some("writer".to_owned()),
            launcher_grant: None,
            max_bytes,
            uid: 1000,
            gid: 1000,
        })
        .unwrap()
    }

    #[test]
    fn definitions_preserve_frozen_order() {
        assert_eq!(
            complete_tool_definitions()
                .iter()
                .map(|definition| definition.name)
                .collect::<Vec<_>>(),
            WRITER_TOOLS
        );
    }

    #[test]
    fn definition_digests_match_frozen_contract() {
        let definitions = complete_tool_definitions();
        assert_eq!(
            definitions_digest(&definitions),
            "eba00ee41c6e31760470ba495274fa0a7c66a5580404017a4c67e688e1c1ba4e"
        );
        assert_eq!(
            definitions_digest(&definitions[..2]),
            "76f7a6cb9e106c0d7aa4ac8969ba909cdb22464fffcecf4ef87b71a2b04a2fb5"
        );
    }

    #[test]
    fn launcher_grant_expires_while_serving() {
        let now = Arc::new(AtomicU64::new(20));
        let clock = FakeClock(now.clone());
        let provider = SharedWorkspaceProvider::new_with_clock(
            options_with_grant(canonical_grant("writer", 30)),
            clock,
        )
        .unwrap();
        assert_eq!(provider.current_role(), Some(WorkspaceRole::Writer));
        now.store(30, Ordering::SeqCst);
        assert_eq!(provider.current_role(), None);

        let client = NoKvFsClient::connect(
            "127.0.0.1:1".parse().unwrap(),
            nokv_object::MemoryObjectStore::new(),
        );
        let denied = provider
            .execute(&client, WORKSPACE_LIST, &json!({}))
            .unwrap_err();
        assert_eq!(denied.code, "WorkspacePermissionDenied");
    }

    #[test]
    fn launcher_grant_rejects_noncanonical_and_duplicate_json() {
        let noncanonical = URL_SAFE_NO_PAD.encode(
            br#"{ "actor_id":"agent-7","audience":"nokv-mcp:lingtai","expires_at_unix_ms":30,"grant_id":"grant_1","issued_at_unix_ms":10,"issuer":"lingtai-workbench-sync","role":"reader","schema":"nokv.lingtai.workspace_grant.v1","workspace_id":"team-alpha" }"#,
        );
        assert!(SharedWorkspaceProvider::new_with_clock(
            options_with_grant(noncanonical),
            FakeClock(Arc::new(AtomicU64::new(20)))
        )
        .is_err());

        let duplicate = URL_SAFE_NO_PAD.encode(
            br#"{"actor_id":"agent-7","actor_id":"agent-7","audience":"nokv-mcp:lingtai","expires_at_unix_ms":30,"grant_id":"grant_1","issued_at_unix_ms":10,"issuer":"lingtai-workbench-sync","role":"reader","schema":"nokv.lingtai.workspace_grant.v1","workspace_id":"team-alpha"}"#,
        );
        assert!(SharedWorkspaceProvider::new_with_clock(
            options_with_grant(duplicate),
            FakeClock(Arc::new(AtomicU64::new(20)))
        )
        .is_err());
    }

    #[test]
    fn producer_goldens_match_contract() {
        let producer = producer_string(
            "dGVhbS1hbHBoYQ",
            "notes/log.txt",
            "YWdlbnQtNw",
            WORKSPACE_APPEND,
            "op_001",
            None,
            6,
            "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03",
        );
        assert_eq!(producer, "nokv-shared-workspace-mcp/v1;workspace=dGVhbS1hbHBoYQ;path=f32a78697076bfbe651273a83db97e4c0b64b9161d3aa7a49f1c580591bde70e;actor=YWdlbnQtNw;tool=workspace_append;operation=op_001;base=none;effect=6;digest=5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03");

        let identity = "😀".repeat(16);
        let encoded_identity = URL_SAFE_NO_PAD.encode(identity.as_bytes());
        let maximum = producer_string(
            &encoded_identity,
            "x",
            &URL_SAFE_NO_PAD.encode("🧠".repeat(16).as_bytes()),
            WORKSPACE_PUT_FILE,
            &"A".repeat(64),
            Some(u64::MAX),
            u64::MAX,
            "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03",
        );
        assert_eq!(maximum.len(), 513);
        assert_eq!(parse_producer(&maximum).unwrap().effect, u64::MAX);
    }

    #[test]
    fn edit_request_manifest_id_is_stable_and_binds_every_operand() {
        let manifest = edit_request_manifest_id("beta", "BETA", false);
        assert_eq!(
            manifest,
            "nokv-shared-workspace-edit-request/v1:a211d39940c3554c0ec6d3ede6098260cd54b7f639a56e5f136820118e4b00a6"
        );
        assert_eq!(manifest, edit_request_manifest_id("beta", "BETA", false));
        assert_ne!(manifest, edit_request_manifest_id("other", "BETA", false));
        assert_ne!(manifest, edit_request_manifest_id("beta", "other", false));
        assert_ne!(manifest, edit_request_manifest_id("beta", "BETA", true));
    }

    #[test]
    fn path_and_identity_limits_are_byte_exact() {
        assert!(validate_identity("workspace id", &("😀".repeat(16))).is_ok());
        assert!(validate_identity("workspace id", &("😀".repeat(17))).is_err());
        assert!(validate_relative_path("docs/a.txt", false).is_ok());
        assert!(validate_relative_path("../outside", false).is_err());
        assert!(validate_relative_path("a//b", false).is_err());
        assert!(validate_relative_path("", true).is_ok());
    }

    #[test]
    fn syntactic_failures_happen_before_any_storage_access() {
        let provider = development_provider(1024);
        let client = NoKvFsClient::connect(
            "127.0.0.1:1".parse().unwrap(),
            nokv_object::MemoryObjectStore::new(),
        );

        let positive_list = provider
            .execute(&client, WORKSPACE_LIST, &json!({"offset": 1, "limit": 1}))
            .unwrap_err();
        assert_eq!(positive_list.code, "WorkspaceInvalidArgument");

        let escaped_put = provider
            .execute(
                &client,
                WORKSPACE_PUT_FILE,
                &json!({
                    "path": "/outside",
                    "operation_id": "op",
                    "base_generation": null,
                    "text": "blocked",
                }),
            )
            .unwrap_err();
        assert_eq!(escaped_put.code, "WorkspacePathViolation");

        let noncanonical = provider
            .execute(
                &client,
                WORKSPACE_APPEND,
                &json!({
                    "path": "notes.txt",
                    "operation_id": "op",
                    "base_generation": null,
                    "base64": "Zg",
                }),
            )
            .unwrap_err();
        assert_eq!(noncanonical.code, "WorkspaceInvalidArgument");

        let unknown_field = provider
            .execute(
                &client,
                WORKSPACE_READ,
                &json!({"path": "notes.txt", "cursor": "opaque"}),
            )
            .unwrap_err();
        assert_eq!(unknown_field.code, "WorkspaceInvalidArgument");
    }

    #[test]
    fn launcher_grant_is_bound_and_lifetime_limited() {
        let wrong_actor = LauncherGrant {
            actor_id: "other-agent".to_owned(),
            ..LauncherGrant {
                schema: GRANT_SCHEMA.to_owned(),
                grant_id: "grant_1".to_owned(),
                issuer: GRANT_ISSUER.to_owned(),
                audience: GRANT_AUDIENCE.to_owned(),
                workspace_id: "team-alpha".to_owned(),
                actor_id: "agent-7".to_owned(),
                role: "reader".to_owned(),
                issued_at_unix_ms: 10,
                expires_at_unix_ms: 20,
            }
        };
        let wrong_actor = URL_SAFE_NO_PAD.encode(canonical_grant_json(&wrong_actor).unwrap());
        assert!(SharedWorkspaceProvider::new_with_clock(
            options_with_grant(wrong_actor),
            FakeClock(Arc::new(AtomicU64::new(15)))
        )
        .is_err());

        let too_long = LauncherGrant {
            schema: GRANT_SCHEMA.to_owned(),
            grant_id: "grant_1".to_owned(),
            issuer: GRANT_ISSUER.to_owned(),
            audience: GRANT_AUDIENCE.to_owned(),
            workspace_id: "team-alpha".to_owned(),
            actor_id: "agent-7".to_owned(),
            role: "reader".to_owned(),
            issued_at_unix_ms: 10,
            expires_at_unix_ms: 10 + MAX_GRANT_LIFETIME_MS + 1,
        };
        let too_long = URL_SAFE_NO_PAD.encode(canonical_grant_json(&too_long).unwrap());
        assert!(SharedWorkspaceProvider::new_with_clock(
            options_with_grant(too_long),
            FakeClock(Arc::new(AtomicU64::new(15)))
        )
        .is_err());
    }
}
