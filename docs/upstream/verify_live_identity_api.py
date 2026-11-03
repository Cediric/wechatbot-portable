"""Bounded read-only localhost probe. Never prints tokens, names, wxids or chat bodies.

Only GET health, identity snapshot, conditional snapshot and unauthenticated snapshot.
No config changes, SSE subscriptions, message/history endpoints, UI or publication.
Supply the existing API token via WECHAT_VERIFY_API_TOKEN or --token-stdin, not argv.
"""
from __future__ import annotations
import argparse
import http.client
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
from urllib.parse import urlsplit

DEFAULT_BASE = "http://127.0.0.1:5031"
MAX_BODY = 4 * 1024 * 1024
HASH = re.compile(r"[0-9a-f]{64}")
IDENTITY = re.compile(r"[A-Za-z0-9_.@:-]{1,256}")


def _base_address(base):
    # Do not follow redirects, environment proxies, DNS aliases or token URLs.
    try:
        parsed = urlsplit(base)
        if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError
        if parsed.path not in ("", "/"):
            raise ValueError
        host = parsed.hostname
        if host == "localhost":
            host = "127.0.0.1"
        if not host or not ipaddress.ip_address(host).is_loopback:
            raise ValueError
        port = parsed.port if parsed.port is not None else 80
        if not 1 <= port <= 65535:
            raise ValueError
        return host, port
    except (ValueError, TypeError):
        raise ValueError("base_url_must_be_http_loopback_without_query_or_credentials") from None


def _request(address, endpoint, headers, timeout, max_body):
    """One GET; hard socket shutdown deadline also bounds slow-drip response reads."""
    host, port = address
    deadline = time.monotonic() + timeout
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    timer = None
    sock = None
    expired = threading.Event()

    def interrupt():
        expired.set()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    try:
        connection.connect()  # Numeric loopback only; connect itself has a socket timeout.
        sock = connection.sock
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"status": None, "error": "timeout"}
        timer = threading.Timer(remaining, interrupt)
        timer.daemon = True
        timer.start()
        connection.request("GET", endpoint, headers=headers)
        response = connection.getresponse()
        chunks, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or expired.is_set():
                return {"status": response.status, "error": "timeout"}
            sock.settimeout(remaining)
            chunk = response.read1(min(65536, max_body + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_body:
                return {"status": response.status, "error": "response_body_limit"}
        if expired.is_set():
            return {"status": response.status, "error": "timeout"}
        result = {"status": response.status, "etag": response.getheader("ETag"), "body_bytes": size}
        raw = b"".join(chunks)
        if raw:
            try:
                result["body"] = json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError):
                result["error"] = "invalid_json"
        return result
    except (TimeoutError, socket.timeout):
        return {"status": None, "error": "timeout"}
    except Exception:
        # Exception text can contain server content, URL paths or local information.
        return {"status": None, "error": "timeout" if expired.is_set() else "connection_error"}
    finally:
        if timer is not None:
            timer.cancel()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()


def _actual_id(value):
    return isinstance(value, str) and bool(IDENTITY.fullmatch(value)) and value not in ("unknown", "null", "None", "@chatroom")


def _identity_summary(result):
    summary = {"http_status": result.get("status"), "valid_contract": False}
    if result.get("error"):
        summary["error"] = result["error"]
    body = result.get("body")
    if not isinstance(body, dict):
        return summary
    revision = body.get("revision")
    valid_hash = isinstance(revision, str) and bool(HASH.fullmatch(revision))
    capabilities = body.get("capabilities")
    version_ok = type(body.get("version")) is int and body["version"] == 1
    capability_ok = isinstance(capabilities, dict) and type(capabilities.get("freshIdentitySnapshot")) is int and capabilities["freshIdentitySnapshot"] == 1
    summary.update(contract_version=1 if version_ok else None,
                   fresh=body.get("fresh") is True, fresh_capability=capability_ok,
                   revision_is_sha256=valid_hash)
    if valid_hash:
        summary["revision_hash"] = revision
    contacts, sessions = body.get("contacts"), body.get("sessions")
    if not isinstance(contacts, list) or not isinstance(sessions, list):
        return summary
    valid_contacts = [row for row in contacts if isinstance(row, dict) and _actual_id(row.get("username"))]
    valid_sessions = [row for row in sessions if isinstance(row, dict) and _actual_id(row.get("username"))]
    contact_ids = {row["username"] for row in valid_contacts}
    session_ids = {row["username"] for row in valid_sessions}
    contacts_schema = all(all(isinstance(row.get(field), str) for field in ("displayName", "remark", "nickname", "alias"))
                          and row.get("type") == ("group" if row["username"].endswith("@chatroom") else "friend") for row in valid_contacts)
    sessions_schema = all(isinstance(row.get("displayName"), str) and type(row.get("type")) is int
                          and row["type"] == (2 if row["username"].endswith("@chatroom") else 1) for row in valid_sessions)
    identities_ok = len(valid_contacts) == len(contacts) and len(contact_ids) == len(contacts) and len(valid_sessions) == len(sessions) and len(session_ids) == len(sessions)
    summary.update(contacts_count=len(contacts), sessions_count=len(sessions),
                   group_contacts_count=sum(row["username"].endswith("@chatroom") for row in valid_contacts),
                   group_sessions_count=sum(row["username"].endswith("@chatroom") for row in valid_sessions),
                   actual_ids_valid_and_unique=identities_ok,
                   contact_name_fields_present=contacts_schema,
                   session_name_fields_present=sessions_schema)
    summary["valid_contract"] = result.get("status") == 200 and not result.get("error") and body.get("success") is True and version_ok and capability_ok and body.get("fresh") is True and valid_hash and identities_ok and contacts_schema and sessions_schema
    return summary


def probe(base, token, timeout=4.0, max_body=MAX_BODY, request=_request):
    address = _base_address(base)
    if not isinstance(token, str) or not token.strip() or len(token) > 4096 or "\r" in token or "\n" in token:
        raise ValueError("existing_api_token_required_or_invalid_format")
    if not 0.1 <= timeout <= 15 or not 1024 <= max_body <= 16 * 1024 * 1024:
        raise ValueError("probe_limit_out_of_range")
    headers = {"Authorization": "Bearer " + token.strip(), "Accept": "application/json"}
    health = request(address, "/api/v1/health", {}, timeout, max_body)
    snapshot = request(address, "/api/v1/identity-snapshot", headers, timeout, max_body)
    summary = _identity_summary(snapshot)
    report = {"ok": False,
              "limits": {"loopback_only": True, "timeout_per_request_seconds": timeout, "max_response_bytes": max_body},
              "health": {"http_status": health.get("status"), "healthy": health.get("status") == 200 and isinstance(health.get("body"), dict) and health["body"].get("status") == "ok"},
              "identity": summary,
              "sse": {"requested": False, "runtime_fields_verified": False,
                      "expected_fields_from_adapter": ["senderUsername", "isSend", "localType", "localId"]}}
    # Only echo an ETag proven to be the endpoint's hex revision; never arbitrary headers.
    etag = '"' + summary["revision_hash"] + '"' if summary.get("revision_is_sha256") else None
    if summary["valid_contract"] and snapshot.get("etag") == etag:
        conditional_headers = dict(headers)
        conditional_headers["If-None-Match"] = etag
        conditional = request(address, "/api/v1/identity-snapshot", conditional_headers, timeout, max_body)
        same = conditional.get("status") == 304 and not conditional.get("error") and conditional.get("etag") == etag
        changed = _identity_summary(conditional)
        # A genuine concurrent rename can produce a new valid 200; it isn't an error.
        report["conditional"] = {"http_status": conditional.get("status"), "not_modified": same,
                                 "changed_valid_snapshot": changed["valid_contract"]}
        conditional_ok = same or changed["valid_contract"]
    else:
        report["conditional"] = {"requested": False, "reason": "initial_contract_or_etag_invalid"}
        conditional_ok = False
    unauthorized = request(address, "/api/v1/identity-snapshot", {}, timeout, max_body)
    auth_ok = unauthorized.get("status") in (401, 403)
    report["authentication"] = {"unauthenticated_http_status": unauthorized.get("status"), "unauthenticated_rejected": auth_ok}
    report["ok"] = report["health"]["healthy"] and summary["valid_contract"] and conditional_ok and auth_ok
    return report


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        # argparse normally echoes invalid argument values; an accidental token
        # in argv must not be copied into diagnostic output.
        self.exit(2, '{"ok": false, "error": "invalid_arguments"}\n')


def main(argv=None):
    parser = _SafeArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--token-env", default="WECHAT_VERIFY_API_TOKEN", help="Name of an existing environment variable; the token value is never an argv argument")
    parser.add_argument("--token-stdin", action="store_true", help="Read a single token line from piped stdin instead of environment")
    parser.add_argument("--timeout", type=float, default=4.0)
    parser.add_argument("--max-body-bytes", type=int, default=MAX_BODY)
    args = parser.parse_args(argv)
    token = sys.stdin.readline(4098).rstrip("\r\n") if args.token_stdin else os.environ.get(args.token_env, "")
    try:
        report = probe(args.base_url, token, args.timeout, args.max_body_bytes)
    except ValueError as error:
        # All ValueError labels above are constant, contain no user/server data.
        report = {"ok": False, "error": str(error)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
