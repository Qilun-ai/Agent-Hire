//! The providers the app can talk to.

/// One hosted provider: where its key comes from, its default model and its API base.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Provider {
    /// The id stored in `llm_provider`.
    pub id: &'static str,
    /// The setting that holds the user's key.
    pub key_setting: &'static str,
    /// The environment variable used when the setting is empty.
    pub key_env: &'static str,
    /// The model used when none is chosen.
    pub default_model: &'static str,
    /// The OpenAI-compatible API base, for providers with a fixed one.
    pub base_url: Option<&'static str>,
}

const fn hosted(
    id: &'static str,
    key_setting: &'static str,
    key_env: &'static str,
    default_model: &'static str,
    base_url: Option<&'static str>,
) -> Provider {
    Provider {
        id,
        key_setting,
        key_env,
        default_model,
        base_url,
    }
}

/// Providers that need an API key, in the order settings validation lists them.
pub const KEYED_PROVIDERS: [Provider; 21] = [
    hosted(
        "anthropic",
        "anthropic_key",
        "ANTHROPIC_API_KEY",
        "claude-sonnet-4-6",
        None,
    ),
    hosted(
        "gemini",
        "gemini_api_key",
        "GEMINI_API_KEY",
        "gemini-2.5-flash",
        Some("https://generativelanguage.googleapis.com/v1beta/openai/"),
    ),
    hosted(
        "openai",
        "openai_api_key",
        "OPENAI_API_KEY",
        "gpt-4o-mini",
        None,
    ),
    hosted(
        "groq",
        "groq_api_key",
        "GROQ_API_KEY",
        "llama-3.3-70b-versatile",
        Some("https://api.groq.com/openai/v1"),
    ),
    hosted(
        "nvidia",
        "nvidia_api_key",
        "NVIDIA_API_KEY",
        "z-ai/glm-5.1",
        Some("https://integrate.api.nvidia.com/v1"),
    ),
    hosted(
        "deepseek",
        "deepseek_api_key",
        "DEEPSEEK_API_KEY",
        "deepseek-chat",
        Some("https://api.deepseek.com"),
    ),
    hosted(
        "xai",
        "xai_api_key",
        "XAI_API_KEY",
        "grok-4",
        Some("https://api.x.ai/v1"),
    ),
    hosted(
        "kimi",
        "kimi_api_key",
        "MOONSHOT_API_KEY",
        "kimi-k2.6",
        Some("https://api.moonshot.ai/v1"),
    ),
    hosted(
        "mistral",
        "mistral_api_key",
        "MISTRAL_API_KEY",
        "mistral-large-latest",
        Some("https://api.mistral.ai/v1"),
    ),
    hosted(
        "openrouter",
        "openrouter_api_key",
        "OPENROUTER_API_KEY",
        "openrouter/auto",
        Some("https://openrouter.ai/api/v1"),
    ),
    hosted(
        "together",
        "together_api_key",
        "TOGETHER_API_KEY",
        "openai/gpt-oss-120b",
        Some("https://api.together.xyz/v1"),
    ),
    hosted(
        "fireworks",
        "fireworks_api_key",
        "FIREWORKS_API_KEY",
        "accounts/fireworks/models/llama-v3p1-70b-instruct",
        Some("https://api.fireworks.ai/inference/v1"),
    ),
    hosted(
        "cerebras",
        "cerebras_api_key",
        "CEREBRAS_API_KEY",
        "llama-3.3-70b",
        Some("https://api.cerebras.ai/v1"),
    ),
    hosted(
        "perplexity",
        "perplexity_api_key",
        "PERPLEXITY_API_KEY",
        "sonar",
        Some("https://api.perplexity.ai"),
    ),
    hosted(
        "huggingface",
        "huggingface_api_key",
        "HF_TOKEN",
        "openai/gpt-oss-120b",
        Some("https://router.huggingface.co/v1"),
    ),
    hosted(
        "cohere",
        "cohere_api_key",
        "COHERE_API_KEY",
        "command-a-03-2025",
        Some("https://api.cohere.ai/compatibility/v1"),
    ),
    hosted(
        "sambanova",
        "sambanova_api_key",
        "SAMBANOVA_API_KEY",
        "Meta-Llama-3.3-70B-Instruct",
        Some("https://api.sambanova.ai/v1"),
    ),
    hosted(
        "qwen",
        "qwen_api_key",
        "DASHSCOPE_API_KEY",
        "qwen-plus",
        Some("https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
    ),
    hosted(
        "azure",
        "azure_openai_api_key",
        "AZURE_OPENAI_API_KEY",
        "gpt-4o-mini",
        None,
    ),
    hosted(
        "opencode",
        "opencode_api_key",
        "OPENCODE_API_KEY",
        "muse-spark-1.3-contributor",
        None,
    ),
    hosted(
        "custom",
        "custom_api_key",
        "OPENAI_COMPAT_API_KEY",
        "model-id",
        None,
    ),
];

/// Providers that run the user's own logged-in CLI instead of an HTTP API.
pub const SUBSCRIPTION_CLI_PROVIDERS: [&str; 4] =
    ["claude_cli", "codex_cli", "copilot_cli", "gemini_cli"];

impl Provider {
    /// The keyed provider with this id.
    pub fn find(id: &str) -> Option<&'static Provider> {
        KEYED_PROVIDERS.iter().find(|provider| provider.id == id)
    }
}

/// The model used for `provider` when no model is configured.
pub fn default_model(provider: &str) -> &'static str {
    if let Some(found) = Provider::find(provider) {
        return found.default_model;
    }
    match provider {
        "claude_cli" => "claude-sonnet-4-6",
        "codex_cli" => "gpt-5.5",
        "gemini_cli" | "copilot_cli" => "",
        _ => "llama3",
    }
}

/// Whether calls to `provider` need an API key (everything but Ollama and the CLIs).
pub fn provider_needs_key(provider: &str) -> bool {
    provider != "ollama" && !SUBSCRIPTION_CLI_PROVIDERS.contains(&provider)
}
