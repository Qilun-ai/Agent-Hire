//! Refusing LLM base URLs (and fetch targets) that point at this machine or a private network.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, ToSocketAddrs};

use url::Url;

/// Checks an LLM API base: it must be an absolute URL whose host is public.
pub fn validate_base_url(base: &str) -> Result<(), String> {
    let parsed = Url::parse(base).map_err(|_| format!("Invalid base URL: {base}"))?;
    let Some(host) = parsed.host_str().filter(|host| !host.is_empty()) else {
        return Err(format!("Invalid base URL: {base}"));
    };
    let host = host
        .trim_start_matches('[')
        .trim_end_matches(']')
        .to_lowercase();
    if matches!(host.as_str(), "localhost" | "127.0.0.1" | "0.0.0.0" | "::1") {
        return Err(format!("Cannot use localhost as LLM base URL: {base}"));
    }
    if !is_public_host(&host) {
        return Err(format!(
            "Cannot use private/loopback/internal host as LLM base URL: {base}"
        ));
    }
    Ok(())
}

/// Whether `host` (a name or an IP literal) is public. Names are resolved, and every address
/// they resolve to must be public; a name that does not resolve is not public.
pub fn is_public_host(host: &str) -> bool {
    let host = host.trim().trim_start_matches('[').trim_end_matches(']');
    if host.is_empty() {
        return false;
    }
    if let Ok(address) = host.parse::<IpAddr>() {
        return is_public_address(address);
    }
    let Ok(addresses) = (host, 0).to_socket_addrs() else {
        return false;
    };
    let addresses: Vec<IpAddr> = addresses.map(|address| address.ip()).collect();
    !addresses.is_empty() && addresses.into_iter().all(is_public_address)
}

fn is_public_address(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(v4) => is_public_v4(v4),
        IpAddr::V6(v6) => {
            if let Some(v4) = v6.to_ipv4_mapped() {
                return is_public_v4(v4);
            }
            if let Some(v4) = nat64_embedded(v6) {
                return is_public_v4(v4);
            }
            is_public_v6(v6)
        }
    }
}

/// The IPv4 address inside a NAT64 address (`64:ff9b::/96` or `64:ff9b:1::/48`).
fn nat64_embedded(address: Ipv6Addr) -> Option<Ipv4Addr> {
    let segments = address.segments();
    let well_known = segments[..6] == [0x64, 0xff9b, 0, 0, 0, 0];
    let local_use = segments[..3] == [0x64, 0xff9b, 1];
    if !(well_known || local_use) {
        return None;
    }
    let bits = u128::from(address);
    Some(Ipv4Addr::from(
        u32::try_from(bits & 0xffff_ffff).unwrap_or(0),
    ))
}

/// IPv4 ranges that are private, loopback, link-local, multicast, reserved or unspecified.
fn is_public_v4(address: Ipv4Addr) -> bool {
    const BLOCKED: [(u32, u32); 16] = [
        (0x0000_0000, 8),  // 0.0.0.0/8 "this network"
        (0x0a00_0000, 8),  // 10.0.0.0/8
        (0x7f00_0000, 8),  // 127.0.0.0/8 loopback
        (0xa9fe_0000, 16), // 169.254.0.0/16 link-local
        (0xac10_0000, 12), // 172.16.0.0/12
        (0xc000_0000, 29), // 192.0.0.0/29
        (0xc000_00aa, 31), // 192.0.0.170/31
        (0xc000_0200, 24), // 192.0.2.0/24 documentation
        (0xc0a8_0000, 16), // 192.168.0.0/16
        (0xc612_0000, 15), // 198.18.0.0/15 benchmarking
        (0xc633_6400, 24), // 198.51.100.0/24 documentation
        (0xcb00_7100, 24), // 203.0.113.0/24 documentation
        (0xe000_0000, 4),  // 224.0.0.0/4 multicast
        (0xf000_0000, 4),  // 240.0.0.0/4 reserved
        (0xffff_ffff, 32), // broadcast
        (0xc000_0000, 24), // 192.0.0.0/24 IETF protocol assignments
    ];
    let bits = u32::from(address);
    !BLOCKED.iter().any(|(network, prefix)| {
        let mask = if *prefix == 0 {
            0
        } else {
            u32::MAX << (32 - prefix)
        };
        bits & mask == *network
    })
}

/// IPv6 ranges that are private, loopback, link-local, multicast, reserved or unspecified.
fn is_public_v6(address: Ipv6Addr) -> bool {
    const BLOCKED: [(u128, u32); 9] = [
        (0, 8),                            // ::/8 reserved (loopback, unspecified)
        (0x0100_0000_0000_0000 << 64, 64), // 100::/64 discard
        (0x2001_0000_0000_0000 << 64, 23), // 2001::/23 IETF protocol assignments
        (0x2001_0db8_0000_0000 << 64, 32), // 2001:db8::/32 documentation
        (0xfc00_0000_0000_0000 << 64, 7),  // fc00::/7 unique local
        (0xfe80_0000_0000_0000 << 64, 10), // fe80::/10 link-local
        (0xfec0_0000_0000_0000 << 64, 10), // fec0::/10 site-local (reserved)
        (0xff00_0000_0000_0000 << 64, 8),  // ff00::/8 multicast
        (0x0064_ff9b_0001_0000 << 64, 48), // 64:ff9b:1::/48 (handled above)
    ];
    let bits = u128::from(address);
    !BLOCKED.iter().any(|(network, prefix)| {
        let mask = if *prefix == 0 {
            0
        } else {
            u128::MAX << (128 - prefix)
        };
        bits & mask == *network
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn literals() {
        for host in [
            "127.0.0.1",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",
            "0.0.0.0",
            "::1",
            "fe80::1",
            "fd00::1",
            "::ffff:127.0.0.1",
            "64:ff9b::a00:1",
            "224.0.0.1",
        ] {
            assert!(!is_public_host(host), "{host} is not public");
        }
        for host in [
            "8.8.8.8",
            "1.1.1.1",
            "::ffff:8.8.8.8",
            "64:ff9b::808:808",
            "2606:4700::1111",
        ] {
            assert!(is_public_host(host), "{host} is public");
        }
    }

    #[test]
    fn base_url_messages() {
        assert_eq!(
            validate_base_url("not a url"),
            Err("Invalid base URL: not a url".to_owned())
        );
        assert_eq!(
            validate_base_url("http://localhost:11434/v1"),
            Err("Cannot use localhost as LLM base URL: http://localhost:11434/v1".to_owned())
        );
        assert_eq!(
            validate_base_url("http://10.1.2.3/v1"),
            Err(
                "Cannot use private/loopback/internal host as LLM base URL: http://10.1.2.3/v1"
                    .to_owned()
            )
        );
    }
}
