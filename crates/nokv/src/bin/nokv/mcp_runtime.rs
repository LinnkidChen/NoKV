use std::collections::{HashMap, HashSet};
use std::fmt;

use nokv_agent::AgentToolDefinition;
use nokv_client::NoKvFsClient;
use nokv_object::ObjectStore;
use serde_json::{json, Value};

use crate::workbench_mcp;

/// A binary-internal MCP tool provider. Providers describe their complete,
/// statically owned catalog once, advertise a capability-dependent subset for
/// each `tools/list`, and execute only tools routed to that static owner.
pub trait McpToolProvider<O>: Send + Sync
where
    O: ObjectStore + Send + Sync + 'static,
{
    fn name(&self) -> &'static str;

    fn complete_tool_definitions(&self) -> Vec<AgentToolDefinition>;

    /// Return the capability-dependent subset by name only. The runtime emits
    /// the definitions from `complete_tool_definitions` so a dynamic probe
    /// cannot alter the public schema, descriptions, or catalog order.
    fn advertised_tool_names(&self, client: &NoKvFsClient<O>) -> Vec<&'static str>;

    fn execute_tool(
        &self,
        client: &NoKvFsClient<O>,
        name: &str,
        args: &Value,
    ) -> Result<Value, Value>;
}

struct ProviderEntry<O>
where
    O: ObjectStore + Send + Sync + 'static,
{
    provider: Box<dyn McpToolProvider<O>>,
    complete_catalog: Vec<AgentToolDefinition>,
}

/// Ordered provider composition and its static ownership routing table.
pub struct McpRuntime<O>
where
    O: ObjectStore + Send + Sync + 'static,
{
    providers: Vec<ProviderEntry<O>>,
    owners: HashMap<&'static str, usize>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum McpRuntimeError {
    DuplicateTool {
        tool: &'static str,
        first_provider: &'static str,
        second_provider: &'static str,
    },
    DuplicateAdvertisement {
        tool: &'static str,
        provider: &'static str,
    },
    InvalidAdvertisement {
        tool: &'static str,
        provider: &'static str,
        owner: &'static str,
    },
}

impl fmt::Display for McpRuntimeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicateTool {
                tool,
                first_provider,
                second_provider,
            } => write!(
                f,
                "duplicate MCP tool ownership for {tool}: {first_provider} then {second_provider}"
            ),
            Self::DuplicateAdvertisement { tool, provider } => {
                write!(
                    f,
                    "MCP provider {provider} advertised {tool} more than once"
                )
            }
            Self::InvalidAdvertisement {
                tool,
                provider,
                owner,
            } => write!(
                f,
                "MCP provider {provider} advertised {tool}, but static ownership belongs to {owner}"
            ),
        }
    }
}

impl std::error::Error for McpRuntimeError {}

impl<O> McpRuntime<O>
where
    O: ObjectStore + Send + Sync + 'static,
{
    pub fn new(providers: Vec<Box<dyn McpToolProvider<O>>>) -> Result<Self, McpRuntimeError> {
        let mut entries: Vec<ProviderEntry<O>> = Vec::with_capacity(providers.len());
        let mut owners: HashMap<&'static str, usize> = HashMap::new();

        for provider in providers {
            let provider_name = provider.name();
            let complete_catalog = provider.complete_tool_definitions();
            let provider_index = entries.len();
            let mut catalog_names = HashSet::new();
            for definition in &complete_catalog {
                if !catalog_names.insert(definition.name) {
                    return Err(McpRuntimeError::DuplicateTool {
                        tool: definition.name,
                        first_provider: provider_name,
                        second_provider: provider_name,
                    });
                }
                if let Some(&first_index) = owners.get(definition.name) {
                    return Err(McpRuntimeError::DuplicateTool {
                        tool: definition.name,
                        first_provider: entries[first_index].provider.name(),
                        second_provider: provider_name,
                    });
                }
                owners.insert(definition.name, provider_index);
            }
            entries.push(ProviderEntry {
                provider,
                complete_catalog,
            });
        }

        Ok(Self {
            providers: entries,
            owners,
        })
    }

    pub fn tool_definitions(
        &self,
        client: &NoKvFsClient<O>,
    ) -> Result<Vec<AgentToolDefinition>, McpRuntimeError> {
        let mut definitions = Vec::new();
        for (provider_index, entry) in self.providers.iter().enumerate() {
            let mut advertised_names = HashSet::new();
            for name in entry.provider.advertised_tool_names(client) {
                if !advertised_names.insert(name) {
                    return Err(McpRuntimeError::DuplicateAdvertisement {
                        tool: name,
                        provider: entry.provider.name(),
                    });
                }
                let owner_index = self.owners.get(name).copied();
                let is_static_member = entry
                    .complete_catalog
                    .iter()
                    .any(|owned| owned.name == name);
                if owner_index != Some(provider_index) || !is_static_member {
                    let owner = self
                        .providers
                        .get(owner_index.unwrap_or(self.providers.len()))
                        .map(|owner| owner.provider.name())
                        .unwrap_or("no provider");
                    return Err(McpRuntimeError::InvalidAdvertisement {
                        tool: name,
                        provider: entry.provider.name(),
                        owner,
                    });
                }
            }
            definitions.extend(
                entry
                    .complete_catalog
                    .iter()
                    .filter(|definition| advertised_names.contains(definition.name))
                    .cloned(),
            );
        }
        Ok(definitions)
    }

    pub fn execute_tool(
        &self,
        client: &NoKvFsClient<O>,
        name: &str,
        args: &Value,
    ) -> Result<Value, Value> {
        let Some(&provider_index) = self.owners.get(name) else {
            return Err(json!({
                "code": "UnknownMcpTool",
                "message": format!("unknown MCP tool {name}"),
                "retryable": false,
                "details": {},
            }));
        };
        self.providers[provider_index]
            .provider
            .execute_tool(client, name, args)
    }
}

pub struct AgentMcpProvider;

impl<O> McpToolProvider<O> for AgentMcpProvider
where
    O: ObjectStore + Send + Sync + 'static,
{
    fn name(&self) -> &'static str {
        "agent"
    }

    fn complete_tool_definitions(&self) -> Vec<AgentToolDefinition> {
        nokv_agent::agent_tool_definitions()
    }

    fn advertised_tool_names(&self, _client: &NoKvFsClient<O>) -> Vec<&'static str> {
        nokv_agent::agent_tool_definitions()
            .into_iter()
            .map(|definition| definition.name)
            .collect()
    }

    fn execute_tool(
        &self,
        client: &NoKvFsClient<O>,
        name: &str,
        args: &Value,
    ) -> Result<Value, Value> {
        nokv_agent::execute_agent_tool(client, name, args).map_err(|err| {
            json!({
                "code": "AgentToolError",
                "message": err.to_string(),
                "retryable": false,
                "details": {},
            })
        })
    }
}

pub struct WorkbenchMcpProvider {
    options: workbench_mcp::WorkbenchMcpOptions,
}

impl WorkbenchMcpProvider {
    pub fn new(options: workbench_mcp::WorkbenchMcpOptions) -> Self {
        Self { options }
    }
}

impl<O> McpToolProvider<O> for WorkbenchMcpProvider
where
    O: ObjectStore + Send + Sync + 'static,
{
    fn name(&self) -> &'static str {
        "workbench"
    }

    fn complete_tool_definitions(&self) -> Vec<AgentToolDefinition> {
        workbench_mcp::complete_tool_definitions()
    }

    fn advertised_tool_names(&self, client: &NoKvFsClient<O>) -> Vec<&'static str> {
        // `tools/list` has no destination arguments, so fleet advertisement
        // covers every owner that can win below the configured root. Any
        // enumeration or probe failure remains unsupported, as before.
        let restore_to_fork_v1 = client
            .metadata()
            .metadata_capabilities_for_subtree_owners(&self.options.root)
            .ok()
            .is_some_and(|owners| {
                owners
                    .iter()
                    .all(|capabilities| capabilities.restore_to_fork_v1)
            });
        workbench_mcp::tool_definitions_for_capabilities(restore_to_fork_v1)
            .into_iter()
            .map(|definition| definition.name)
            .collect()
    }

    fn execute_tool(
        &self,
        client: &NoKvFsClient<O>,
        name: &str,
        args: &Value,
    ) -> Result<Value, Value> {
        workbench_mcp::execute_tool(client, &self.options, name, args).map_err(|err| err.as_value())
    }
}
