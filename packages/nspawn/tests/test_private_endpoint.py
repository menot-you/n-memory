"""claude_restricted.private_endpoint: which routes count as private.

A run note names a route on this host or a private network when the order of a
review falls back to it.
"""

from __future__ import annotations

import pytest

from nspawn import claude_restricted


@pytest.mark.parametrize(
    ("url", "canonical"),
    [
        ("http://127.0.0.1:18090", "http://127.0.0.1:18090"),
        ("http://127.0.0.1:18090/", "http://127.0.0.1:18090"),
        ("http://localhost:8080", "http://localhost:8080"),
        ("http://[::1]:8080", "http://[::1]:8080"),
        ("http://10.0.0.5:8000", "http://10.0.0.5:8000"),
        ("http://172.16.0.1", "http://172.16.0.1"),
        ("https://192.168.1.10:8443", "https://192.168.1.10:8443"),
        ("http://100.100.1.2:8000", "http://100.100.1.2:8000"),
        ("http://[fd7a:115c:a1e0::1]:80", "http://[fd7a:115c:a1e0::1]:80"),
        ("http://172.32.0.1", None),
        ("http://8.8.8.8", None),
        ("http://169.254.169.254", None),
        ("http://0.0.0.0:80", None),
        ("http://[::ffff:127.0.0.1]:80", None),
        ("http://localhost.example.com", None),
        ("https://api.anthropic.com", None),
        ("ftp://127.0.0.1", None),
        ("http://127.0.0.1:99999", None),
        ("http://127.0.0.1:0", None),
        ("127.0.0.1:8080", None),
        ("http://attacker.example\\@127.0.0.1:8080", None),
        ("http://127.0.0.1\\@attacker.example", None),
        ("http://user@127.0.0.1:8080", None),
        ("http://127.0.0.1:8080/v1", None),
        ("http://127.0.0.1:8080#x", None),
        ("http://127.0.0.1 :8080", None),
        ("http://127.1:8080", None),
        ("http://2130706433:8080", None),
        ("HTTP://127.0.0.1:8080", None),
    ],
)
def test_a_private_endpoint_is_on_this_host_or_a_private_network(
    url: str, canonical: str | None
) -> None:
    """Only `scheme://host[:port]`, the host `localhost` or an address: a name
    can resolve anywhere, and anything more can parse to another host in the
    client. The answer is the URL rebuilt from those parts."""
    assert claude_restricted.private_endpoint(url) == canonical
