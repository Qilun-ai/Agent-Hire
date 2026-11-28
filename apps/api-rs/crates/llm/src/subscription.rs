//! Subscription CLI providers (Claude Code, Codex, Gemini CLI, Copilot CLI): whether each is
//! installed and logged in, and starting its own login flow. Authentication always comes from
//! the user's existing CLI login; no API key is involved.

use std::env;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use serde_json::{Map, Value, json};

/// Environment variables removed from the CLIs' environment, so a stray API key cannot switch
/// them to pay-per-token billing.
const SCRUBBED: [&str; 3] = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
];

/// The executable a provider runs.
pub fn executable_name(provider: &str) -> &'static str {
    match provider {
        "claude_cli" => "claude",
        "gemini_cli" => "gemini",
        "copilot_cli" => "copilot",
        _ => "codex",
    }
}

/// The executable on `PATH`, if any.
pub fn find_on_path(name: &str) -> Option<PathBuf> {
    let paths = env::var_os("PATH")?;
    let names: Vec<String> = if cfg!(windows) {
        vec![
            format!("{name}.exe"),
            format!("{name}.cmd"),
            format!("{name}.bat"),
            name.to_owned(),
        ]
    } else {
        vec![name.to_owned()]
    };
    env::split_paths(&paths).find_map(|dir| {
        names
            .iter()
            .map(|candidate| dir.join(candidate))
            .find(|path| is_executable(path))
    })
}

fn is_executable(path: &Path) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        path.metadata()
            .is_ok_and(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
    }
    #[cfg(not(unix))]
    {
        path.is_file()
    }
}

fn home() -> PathBuf {
    env::var_os("HOME")
        .or_else(|| env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_default()
}

fn env_set(name: &str) -> bool {
    env::var(name).is_ok_and(|value| !value.is_empty())
}

/// Runs a command with the scrubbed environment and a timeout; `(exit ok, stdout, stderr)`.
fn run(program: &Path, args: &[&str], timeout: Duration) -> Option<(bool, String, String)> {
    let mut command = Command::new(program);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for name in SCRUBBED {
        command.env_remove(name);
    }
    let mut child = command.spawn().ok()?;
    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if started.elapsed() > timeout => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(50)),
            Err(_) => return None,
        }
    }
    let output = child.wait_with_output().ok()?;
    Some((
        output.status.success(),
        String::from_utf8_lossy(&output.stdout).into_owned(),
        String::from_utf8_lossy(&output.stderr).into_owned(),
    ))
}

fn base64url_decode(text: &str) -> Option<Vec<u8>> {
    let mut bits: u32 = 0;
    let mut count = 0;
    let mut out = Vec::new();
    for byte in text.bytes() {
        let value = match byte {
            b'A'..=b'Z' => byte - b'A',
            b'a'..=b'z' => byte - b'a' + 26,
            b'0'..=b'9' => byte - b'0' + 52,
            b'-' | b'+' => 62,
            b'_' | b'/' => 63,
            b'=' => break,
            _ => return None,
        };
        bits = (bits << 6) | u32::from(value);
        count += 6;
        if count >= 8 {
            count -= 8;
            out.push((bits >> count) as u8);
            bits &= (1 << count) - 1;
        }
    }
    Some(out)
}

/// The claims of a JWT, without verifying it.
fn jwt_claims(token: &str) -> Map<String, Value> {
    token
        .split('.')
        .nth(1)
        .and_then(base64url_decode)
        .and_then(|payload| serde_json::from_slice::<Value>(&payload).ok())
        .and_then(|value| value.as_object().cloned())
        .unwrap_or_default()
}

fn non_empty(value: Option<&Value>) -> Value {
    match value {
        Some(Value::String(text)) if !text.is_empty() => json!(text),
        Some(Value::String(_)) | Some(Value::Null) | None => Value::Null,
        Some(other) => other.clone(),
    }
}

fn title_case(text: &str) -> String {
    let mut out = String::new();
    let mut start = true;
    for c in text.chars() {
        if c.is_alphabetic() {
            if start {
                out.extend(c.to_uppercase());
            } else {
                out.extend(c.to_lowercase());
            }
            start = false;
        } else {
            out.push(c);
            start = true;
        }
    }
    out
}

/// `{provider, installed, logged_in, exe, email, plan[, method]}`.
pub fn status(provider: &str) -> Value {
    let mut out = json!({"provider": provider, "installed": false, "logged_in": false, "exe": null, "email": null, "plan": null});
    let Some(exe) = find_on_path(executable_name(provider)) else {
        return out;
    };
    out["installed"] = json!(true);
    out["exe"] = json!(exe.to_string_lossy());
    let home = home();
    match provider {
        "claude_cli" => {
            let rich = run(&exe, &["auth", "status"], Duration::from_secs(20))
                .and_then(|(_, stdout, _)| serde_json::from_str::<Value>(&stdout).ok())
                .filter(Value::is_object);
            match rich {
                Some(info) => {
                    out["logged_in"] = json!(
                        info["loggedIn"]
                            .as_bool()
                            .unwrap_or(!info["loggedIn"].is_null())
                    );
                    out["email"] = info.get("email").cloned().unwrap_or(Value::Null);
                    out["plan"] = info.get("subscriptionType").cloned().unwrap_or(Value::Null);
                    out["method"] = info.get("authMethod").cloned().unwrap_or(Value::Null);
                }
                None => {
                    out["logged_in"] = json!(
                        env_set("CLAUDE_CODE_OAUTH_TOKEN")
                            || home.join(".claude").join(".credentials.json").exists()
                            || home.join(".claude").is_dir()
                    );
                }
            }
        }
        "gemini_cli" => {
            let gemini = home.join(".gemini");
            out["logged_in"] = json!(
                gemini.join("oauth_creds.json").exists()
                    || gemini.join("google_accounts.json").exists()
                    || gemini.is_dir()
                    || env_set("GEMINI_API_KEY")
                    || env_set("GOOGLE_API_KEY")
            );
        }
        "copilot_cli" => {
            let mut logged_in = ["COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"]
                .iter()
                .any(|name| env_set(name))
                || home.join(".config").join("gh").join("hosts.yml").exists()
                || home.join(".copilot").is_dir();
            if !logged_in && let Some(gh) = find_on_path("gh") {
                logged_in = run(&gh, &["auth", "status"], Duration::from_secs(20))
                    .is_some_and(|(ok, _, _)| ok);
            }
            out["logged_in"] = json!(logged_in);
        }
        _ => {
            let auth = home.join(".codex").join("auth.json");
            let rich = std::fs::read_to_string(&auth)
                .ok()
                .and_then(|text| serde_json::from_str::<Value>(&text).ok());
            match rich {
                Some(info) => {
                    let tokens = info
                        .get("tokens")
                        .and_then(Value::as_object)
                        .cloned()
                        .unwrap_or_default();
                    let token = tokens
                        .get("id_token")
                        .or_else(|| tokens.get("access_token"))
                        .and_then(Value::as_str)
                        .unwrap_or("");
                    let claims = jwt_claims(token);
                    let plan = claims
                        .get("https://api.openai.com/auth")
                        .and_then(|auth| auth.get("chatgpt_plan_type"))
                        .and_then(Value::as_str)
                        .filter(|plan| !plan.is_empty())
                        .map(|plan| format!("ChatGPT {}", title_case(plan)));
                    out["logged_in"] = json!(true);
                    out["email"] = non_empty(claims.get("email"));
                    out["plan"] = json!(plan);
                    out["method"] = non_empty(info.get("auth_mode"));
                }
                None => {
                    let reported = run(&exe, &["login", "status"], Duration::from_secs(20))
                        .is_some_and(|(ok, stdout, stderr)| {
                            let combined = format!("{stdout}{stderr}").to_lowercase();
                            ok && ["logged in", "signed in", "authenticated"]
                                .iter()
                                .any(|word| combined.contains(word))
                        });
                    out["logged_in"] = json!(reported || auth.exists());
                }
            }
        }
    }
    out
}

/// What to install when a CLI is missing.
pub fn install_hint(provider: &str) -> Value {
    match provider {
        "claude_cli" => {
            json!({"name": "Claude Code", "cmd": "npm install -g @anthropic-ai/claude-code", "url": "https://docs.claude.com/en/docs/claude-code/setup", "after": "then click Sign in (runs: claude auth login)"})
        }
        "gemini_cli" => {
            json!({"name": "Gemini CLI", "cmd": "npm install -g @google/gemini-cli", "url": "https://github.com/google-gemini/gemini-cli", "after": "then click Sign in and authorize your Google account"})
        }
        "copilot_cli" => {
            json!({"name": "GitHub Copilot CLI", "cmd": "npm install -g @github/copilot", "url": "https://docs.github.com/copilot/concepts/agents/about-copilot-cli", "after": "then sign in with your GitHub Copilot account"})
        }
        _ => {
            json!({"name": "Codex CLI", "cmd": "npm install -g @openai/codex", "url": "https://developers.openai.com/codex/cli", "after": "then click Sign in (runs: codex login)"})
        }
    }
}

/// Starts the CLI's own login flow (not waited for). Errors with the not-installed message.
pub fn login(provider: &str) -> Result<Value, String> {
    let name = executable_name(provider);
    let exe =
        find_on_path(name).ok_or_else(|| format!("{name} CLI not found — install it first"))?;
    let args: &[&str] = match provider {
        "claude_cli" => &["auth", "login"],
        "codex_cli" => &["login"],
        _ => &[],
    };
    let mut command = Command::new(&exe);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    for variable in SCRUBBED {
        command.env_remove(variable);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;

        command.process_group(0);
    }
    let child = command.spawn().map_err(|error| error.to_string())?;
    let pid = child.id();
    std::thread::spawn(move || {
        let mut child = child;
        let _ = child.wait();
    });
    let cmd = std::iter::once(name)
        .chain(args.iter().copied())
        .collect::<Vec<_>>()
        .join(" ");
    Ok(json!({"provider": provider, "started": true, "pid": pid, "cmd": cmd}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn jwt_and_hints() {
        // {"email":"a@b.c","https://api.openai.com/auth":{"chatgpt_plan_type":"plus"}}
        let token = "x.eyJlbWFpbCI6ImFAYi5jIiwiaHR0cHM6Ly9hcGkub3BlbmFpLmNvbS9hdXRoIjp7ImNoYXRncHRfcGxhbl90eXBlIjoicGx1cyJ9fQ.y";
        let claims = jwt_claims(token);
        assert_eq!(claims["email"], json!("a@b.c"));
        assert_eq!(title_case("plus"), "Plus");
        assert_eq!(install_hint("nope")["name"], json!("Codex CLI"));
    }
}
