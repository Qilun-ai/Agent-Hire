//! The custom and OpenCode endpoints, which can speak three different APIs.

/// Which API an endpoint speaks.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EndpointType {
    /// OpenAI Chat Completions.
    ChatCompletions,
    /// OpenAI Responses.
    Responses,
    /// Anthropic Messages.
    Messages,
}

impl EndpointType {
    /// The setting value and URL suffix: `chat/completions`, `responses` or `messages`.
    pub fn as_str(self) -> &'static str {
        match self {
            EndpointType::ChatCompletions => "chat/completions",
            EndpointType::Responses => "responses",
            EndpointType::Messages => "messages",
        }
    }

    fn parse(value: &str) -> Option<EndpointType> {
        match value {
            "chat/completions" => Some(EndpointType::ChatCompletions),
            "responses" => Some(EndpointType::Responses),
            "messages" => Some(EndpointType::Messages),
            _ => None,
        }
    }
}

/// A configured endpoint.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EndpointConfig {
    /// The URL as configured (it may already end with an endpoint path).
    pub url: String,
    /// `openai` or `anthropic`.
    pub protocol: String,
    /// The API to call.
    pub endpoint_type: EndpointType,
    /// `low`, `medium`, `high` or `""`.
    pub reasoning: String,
}

impl EndpointConfig {
    /// The custom endpoint: an explicit type wins, else the URL decides (`/responses`, then
    /// `/messages`, else Chat Completions). The protocol is Anthropic for Messages endpoints and
    /// URLs containing `/anthropic`.
    pub fn custom(
        url: &str,
        protocol: &str,
        endpoint_type: &str,
        reasoning: &str,
    ) -> EndpointConfig {
        let endpoint_type =
            EndpointType::parse(&endpoint_type.to_lowercase()).unwrap_or_else(|| {
                if url.contains("/responses") {
                    EndpointType::Responses
                } else if url.contains("/messages") {
                    EndpointType::Messages
                } else {
                    EndpointType::ChatCompletions
                }
            });
        let mut protocol = protocol.to_lowercase();
        if protocol.is_empty() {
            let anthropic = endpoint_type == EndpointType::Messages || url.contains("/anthropic");
            protocol = if anthropic { "anthropic" } else { "openai" }.to_owned();
        }
        EndpointConfig {
            url: url.to_owned(),
            protocol,
            endpoint_type,
            reasoning: reasoning.to_lowercase(),
        }
    }

    /// The OpenCode endpoint: an explicit type wins, else a URL ending in `/chat/completions`
    /// uses Chat Completions, one ending in `/messages` (or the Anthropic protocol) Messages,
    /// and anything else Responses.
    pub fn opencode(
        url: &str,
        protocol: &str,
        endpoint_type: &str,
        reasoning: &str,
    ) -> EndpointConfig {
        let protocol_lower = protocol.to_lowercase();
        let endpoint_type =
            EndpointType::parse(&endpoint_type.to_lowercase()).unwrap_or_else(|| {
                let lowered = url.to_lowercase();
                let trimmed = lowered.trim_end_matches('/');
                if trimmed.ends_with("/chat/completions") {
                    EndpointType::ChatCompletions
                } else if trimmed.ends_with("/messages") || protocol_lower == "anthropic" {
                    EndpointType::Messages
                } else {
                    EndpointType::Responses
                }
            });
        let protocol = if protocol_lower.is_empty() {
            if endpoint_type == EndpointType::Messages {
                "anthropic"
            } else {
                "openai"
            }
            .to_owned()
        } else {
            protocol_lower
        };
        EndpointConfig {
            url: url.to_owned(),
            protocol,
            endpoint_type,
            reasoning: reasoning.to_lowercase(),
        }
    }
}

/// The URL to POST to and the API root it hangs off.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Endpoints {
    /// The full endpoint URL.
    pub target_url: String,
    /// The root (`.../v1`) with any endpoint path removed.
    pub base_url: String,
}

/// Splits a configured URL into its root and the endpoint for `endpoint_type`.
pub fn resolve_endpoints(url: &str, endpoint_type: EndpointType) -> Endpoints {
    let mut base = url.trim().trim_end_matches('/');
    for suffix in ["/chat/completions", "/responses", "/messages"] {
        if let Some(stripped) = base.strip_suffix(suffix) {
            base = stripped;
            break;
        }
    }
    Endpoints {
        target_url: format!("{base}/{}", endpoint_type.as_str()),
        base_url: base.to_owned(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn endpoint_detection() {
        let custom = EndpointConfig::custom("https://x.ai/v1/messages", "", "", "HIGH");
        assert_eq!(custom.endpoint_type, EndpointType::Messages);
        assert_eq!(custom.protocol, "anthropic");
        assert_eq!(custom.reasoning, "high");
        let opencode = EndpointConfig::opencode("https://opencode.ai/zen/go/v1", "", "", "");
        assert_eq!(opencode.endpoint_type, EndpointType::Responses);
        assert_eq!(opencode.protocol, "openai");
        let endpoints = resolve_endpoints("https://h/v1/chat/completions/", EndpointType::Messages);
        assert_eq!(endpoints.base_url, "https://h/v1");
        assert_eq!(endpoints.target_url, "https://h/v1/messages");
    }
}
