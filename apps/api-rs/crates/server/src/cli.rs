//! Command-line options.

use std::env;

/// Options from the command line and the environment.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Options {
    /// The port to bind on `127.0.0.1`; `0` lets the OS choose.
    pub port: u16,
    /// Whether to print `AGENT_HIRE_TOKEN=<token>` before the port line.
    pub print_token: bool,
}

impl Options {
    /// Parses `--port <n>`, `--no-token-stdout` and `--no-services` (accepted, no effect).
    ///
    /// Without `--port`, the port comes from `APP_PORT`, then `API_PORT`, then `0`. An empty
    /// variable counts as unset.
    pub fn parse(args: impl IntoIterator<Item = String>) -> Result<Options, String> {
        let mut port: Option<u16> = None;
        let mut print_token = true;
        let mut args = args.into_iter();
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--port" => {
                    let value = args.next().ok_or("--port needs a value")?;
                    port = Some(parse_port("--port", &value)?);
                }
                "--no-token-stdout" => print_token = false,
                "--no-services" => {}
                "-h" | "--help" => {
                    return Err(
                        "usage: agent-hire-api [--port PORT] [--no-token-stdout] [--no-services]"
                            .to_owned(),
                    );
                }
                other => {
                    if let Some(value) = other.strip_prefix("--port=") {
                        port = Some(parse_port("--port", value)?);
                    } else {
                        return Err(format!("unrecognized argument: {other}"));
                    }
                }
            }
        }
        let port = match port {
            Some(port) => port,
            None => port_from_env()?,
        };
        Ok(Options { port, print_token })
    }
}

fn port_from_env() -> Result<u16, String> {
    for name in ["APP_PORT", "API_PORT"] {
        if let Ok(value) = env::var(name)
            && !value.is_empty()
        {
            return parse_port(name, &value);
        }
    }
    Ok(0)
}

fn parse_port(name: &str, value: &str) -> Result<u16, String> {
    value
        .trim()
        .parse::<u16>()
        .map_err(|_| format!("{name} must be a port number, got {value:?}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<Options, String> {
        Options::parse(args.iter().map(|arg| (*arg).to_owned()))
    }

    #[test]
    fn flags() {
        let options = parse(&["--port", "8110", "--no-token-stdout", "--no-services"]);
        assert_eq!(
            options,
            Ok(Options {
                port: 8110,
                print_token: false
            })
        );
        assert_eq!(parse(&["--port=0"]).map(|options| options.port), Ok(0));
    }

    #[test]
    fn rejects_bad_input() {
        assert!(parse(&["--port"]).is_err());
        assert!(parse(&["--port", "x"]).is_err());
        assert!(parse(&["--bogus"]).is_err());
    }
}
