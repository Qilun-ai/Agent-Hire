//! LLM provider configuration for the Agent-Hire backend: which provider, key, model and
//! endpoint a call uses, resolved from the user's settings and the environment.

mod catalog;
mod client;
mod config;
mod endpoint;
mod environment_keys;
mod http;
mod json_text;
mod media;
mod models;
mod probe;
mod provider;
mod subscription;
mod tool_proxy;
mod url_guard;

pub use catalog::Catalog;
pub use client::{LlmError, LlmRequest, OutputSchema, call_raw, call_structured, missing_key};
pub use config::{ResolvedConfig, Settings};
pub use endpoint::{EndpointConfig, EndpointType, Endpoints, resolve_endpoints};
pub use environment_keys::{
    EnvironmentKey, candidate_variables, detected_providers, environment_key,
};
pub use http::{http_client, opencode_session_id};
pub use json_text::extract_json;
pub use media::{
    Capability, PageImage, PdfMedia, base64_encode, content_parts, model_capabilities,
    render_pdf_media,
};
pub use models::list_model_ids;
pub use probe::{probe_provider, providers_to_validate};
pub use provider::{Provider, SUBSCRIPTION_CLI_PROVIDERS, provider_needs_key};
pub use subscription::{install_hint, login as subscription_login, status as subscription_status};
pub use tool_proxy::{ToolProxyError, chat_completions as relay_chat_completions};
pub use url_guard::{is_public_host, validate_base_url};
