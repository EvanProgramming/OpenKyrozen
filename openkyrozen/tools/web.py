from __future__ import annotations

import re
import http.client
import ipaddress
import socket
import ssl
from typing import Any
from urllib.parse import urljoin, urlsplit

from openkyrozen.tools.models import CommandResult
from openkyrozen.security.command_policy import _BLOCKED_RE

_MAX_WEBPAGE_BYTES = 1_000_000


_MAX_WEBPAGE_REDIRECTS = 5


_CLOUD_METADATA_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "169.254.169.254/32",  # AWS/GCP/Azure and compatible metadata APIs
        "169.254.170.2/32",    # ECS task metadata
        "100.100.100.200/32",  # Alibaba Cloud metadata
        "fd00:ec2::254/128",   # AWS IPv6 metadata
    )
)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection to a fixed IP while retaining hostname TLS validation."""

    def __init__(self, address: str, port: int, hostname: str, timeout: float) -> None:
        self._requested_hostname = hostname
        super().__init__(address, port, timeout=timeout, context=ssl.create_default_context())

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=self._requested_hostname)


def _is_blocked_web_address(self, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return whether an address must never be reached by the web tool."""
    candidates = [address]
    if address.version == 6 and address.ipv4_mapped is not None:
        candidates.append(address.ipv4_mapped)
    for candidate in candidates:
        if (
            candidate.is_loopback
            or candidate.is_private
            or candidate.is_link_local
            or candidate.is_multicast
            or candidate.is_unspecified
            or candidate.is_reserved
            or any(candidate in network for network in _CLOUD_METADATA_NETWORKS)
        ):
            return True
    return False


def _resolve_web_destination(self, url: str) -> tuple[Any, str, int, str]:
    """Parse a URL and return its parsed form, canonical host, port, and pinned IP."""
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("only http and https URLs are supported")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname")
    if parsed.username or parsed.password:
        raise ValueError("credentials in URLs are not allowed")
    if any(char.isspace() or ord(char) < 32 for char in parsed.hostname):
        raise ValueError("hostname contains invalid characters")
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("URL contains an invalid port") from exc

    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise ValueError("hostname is not valid IDNA") from exc
    if not hostname:
        raise ValueError("URL must include a hostname")

    try:
        direct_address = ipaddress.ip_address(hostname)
        addresses = [direct_address]
    except ValueError:
        try:
            infos = socket.getaddrinfo(
                hostname, port, family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
            )
        except OSError as exc:
            raise ValueError(f"could not resolve hostname: {exc}") from exc
        addresses = []
        for _, _, _, _, sockaddr in infos:
            try:
                address = ipaddress.ip_address(sockaddr[0])
            except ValueError:
                continue
            if address not in addresses:
                addresses.append(address)
    if not addresses:
        raise ValueError("hostname did not resolve to an IP address")
    blocked = next((address for address in addresses if self._is_blocked_web_address(address)), None)
    if blocked is not None:
        raise ValueError(f"destination resolves to a blocked address: {blocked}")
    # Validate every answer, then pin one answer for the connection.  The
    # request never resolves the hostname again, preventing DNS rebinding.
    return parsed, hostname, port, str(addresses[0])


def _read_pinned_web_response(self, parsed: Any, hostname: str, port: int, address: str) -> tuple[int, str, Any]:
    """Perform one GET against the already-validated destination."""
    if parsed.scheme.lower() == "https":
        connection: http.client.HTTPConnection = _PinnedHTTPSConnection(address, port, hostname, 10)
    else:
        connection = http.client.HTTPConnection(address, port, timeout=10)
    host_header = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    if port != default_port:
        host_header = f"{host_header}:{port}"
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    try:
        connection.request("GET", path, headers={
            "Host": host_header,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/133.0.6943.126 Safari/537.36"
            ),
            "Accept-Encoding": "identity",
            "Connection": "close",
        })
        response = connection.getresponse()
        content_length = response.getheader("Content-Length")
        if content_length:
            try:
                if int(content_length) > _MAX_WEBPAGE_BYTES:
                    response.close()
                    raise ValueError(f"response exceeds {_MAX_WEBPAGE_BYTES} bytes")
            except ValueError as exc:
                if "response exceeds" in str(exc):
                    raise
        body = bytearray()
        while len(body) <= _MAX_WEBPAGE_BYTES:
            chunk = response.read(min(64 * 1024, _MAX_WEBPAGE_BYTES + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        if len(body) > _MAX_WEBPAGE_BYTES:
            raise ValueError(f"response exceeds {_MAX_WEBPAGE_BYTES} bytes")
        charset = response.headers.get_content_charset() or "utf-8"
        return response.status, response.getheader("Location", ""), body.decode(charset, errors="replace")
    finally:
        connection.close()


def read_webpage(self, args: str) -> str:
    """
    Fetch the content of a web page and return its plain‑text body.
    Args format: "URL" (e.g., "https://example.com/page")
    """
    import html
    import re
    url = str(args or "").strip()
    if not url:
        return "Error: read_webpage requires a valid URL."
    try:
        current_url = url
        for redirect_count in range(_MAX_WEBPAGE_REDIRECTS + 1):
            parsed, hostname, port, address = self._resolve_web_destination(current_url)
            status, location, body = self._read_pinned_web_response(parsed, hostname, port, address)
            if status in {301, 302, 303, 307, 308}:
                if not location:
                    return f"Error fetching webpage: HTTP {status} redirect has no Location header"
                if redirect_count >= _MAX_WEBPAGE_REDIRECTS:
                    return f"Error fetching webpage: too many redirects (maximum {_MAX_WEBPAGE_REDIRECTS})"
                current_url = urljoin(current_url, location)
                continue
            if status < 200 or status >= 400:
                return f"Error fetching webpage: HTTP {status}"
            text = re.sub(r"<[^>]*>", "", body)
            text = html.unescape(text)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 5000:
                text = text[:5000] + "\n[...truncated...]"
            return text
        return f"Error fetching webpage: too many redirects (maximum {_MAX_WEBPAGE_REDIRECTS})"
    except (OSError, http.client.HTTPException, ValueError) as e:
        message = str(e)
        if "blocked address" in message or "only http" in message or "credentials in URLs" in message:
            return f"Error: URL blocked: {message}"
        return f"Error fetching webpage: {message}"
