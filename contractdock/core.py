from __future__ import annotations

import hashlib
import copy
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class ContractError(Exception):
    """Unsafe recording, malformed fixture or request mismatch."""


MAX_BYTES = 1024 * 1024
SENSITIVE = re.compile(r"(?i)(password|passwd|secret|token|authorization|cookie|api.?key|credential)")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("Duplicate JSON keys")
        result[key] = value
    return result


def _reject_constant(value):
    raise ContractError("Nonfinite JSON literal")


def parse_json(content: bytes):
    if len(content) > MAX_BYTES:
        raise ContractError("JSON body exceeds 1 MiB")
    try:
        value = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique, parse_constant=_reject_constant)
        # json.loads may decode 1e999 as inf without invoking parse_constant.
        canonical(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ContractError("Invalid finite UTF-8 JSON body") from exc


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError) as exc:
        raise ContractError("Invalid JSON value") from exc


def redact(value, depth=0):
    if depth > 64:
        raise ContractError("JSON nesting exceeds 64 levels")
    if isinstance(value, dict):
        return {key: "[REDACTED]" if SENSITIVE.search(key) else redact(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, depth + 1) for item in value]
    return value


def request_key(method: str, target: str, body=None):
    if not isinstance(method, str):
        raise ContractError("Invalid request method")
    method = method.upper()
    if method not in ("GET", "POST"):
        raise ContractError("Only explicit GET/POST JSON requests are supported")
    if not isinstance(target, str) or target.startswith("//") or len(target) > 8192 or any(ord(char) < 32 for char in target):
        raise ContractError("Invalid request target")
    try:
        parsed = urlsplit(target)
    except ValueError as exc:
        raise ContractError("Invalid request target") from exc
    if parsed.fragment:
        raise ContractError("Fragments are not supported")
    path = parsed.path or "/"
    if not path.startswith("/") or path.startswith("//"):
        raise ContractError("Request path must start with one slash")
    try:
        query = sorted((key, "[REDACTED]" if SENSITIVE.search(key) else value)
                       for key, value in parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=100))
    except ValueError as exc:
        raise ContractError("Query exceeds parameter limit") from exc
    normalized = path + ("?" + urlencode(query) if query else "")
    value = {"method": method, "target": normalized, "body": redact(body)}
    value["key"] = hashlib.sha256(canonical(value)).hexdigest()
    return value


def schema(value, depth=0):
    if depth > 64:
        raise ContractError("JSON nesting exceeds 64 levels")
    if value is None:
        return {"type": "null"}
    if type(value) is bool:
        return {"type": "boolean"}
    if type(value) in (int, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        unique = {canonical(schema(item, depth + 1)): schema(item, depth + 1) for item in value}
        return {"type": "array", "items": [unique[key] for key in sorted(unique)]}
    if isinstance(value, dict):
        return {"type": "object", "properties": {key: schema(item, depth + 1) for key, item in sorted(value.items())}}
    raise ContractError("Unsupported JSON value")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _origin(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ContractError("Recording URL must be explicit http/https without userinfo or fragments")
        return f"{parsed.scheme}://{parsed.hostname.lower()}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"
    except ValueError as exc:
        raise ContractError("Invalid URL") from exc


def _content_length(headers):
    """Reject ambiguous/invalid framing before integer conversion or reading."""
    values = headers.get_all("Content-Length")
    if values is None:
        return None
    if len(values) != 1:
        raise ContractError("Invalid Content-Length")
    value = values[0].strip()
    if len(value) > 10 or not value.isascii() or not value.isdigit():
        raise ContractError("Invalid Content-Length")
    return int(value)


def _response_length(headers):
    length = _content_length(headers)
    transfer = headers.get_all("Transfer-Encoding")
    if transfer is not None:
        if length is not None or len(transfer) != 1 or transfer[0].strip().lower() != "chunked":
            raise ContractError("Unsupported or ambiguous response framing")
    if length is not None and length > MAX_BYTES:
        raise ContractError("Response exceeds 1 MiB")
    return length


def record(url: str, *, allowed_origins, method="GET", body=None, timeout=5):
    """One explicitly requested network call, no redirects, proxies or headers saved."""
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ContractError("Timeout must be finite and in (0, 60] seconds")
    if _origin(url) not in {_origin(item) for item in allowed_origins}:
        raise ContractError("Recording origin is not explicitly allowed")
    request = request_key(method, url, body)
    data = None if body is None else canonical(body)
    if data is not None and len(data) > MAX_BYTES:
        raise ContractError("Request exceeds 1 MiB")
    if method.upper() == "GET" and body is not None:
        raise ContractError("GET body is not supported")
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        outgoing = Request(url, method=method.upper(), data=data,
                           headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            response = opener.open(outgoing, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            if 300 <= response.status < 400:
                raise ContractError("Redirect recording refused")
            content_type = response.headers.get_content_type()
            if content_type != "application/json" and not content_type.endswith("+json"):
                raise ContractError("Only JSON responses can be recorded")
            expected = _response_length(response.headers)
            content = response.read(MAX_BYTES + 1)
            if expected is not None and len(content) != expected:
                raise ContractError("Incomplete JSON response")
            value = redact(parse_json(content))
            packet = {"version": 1, "request": request,
                      "response": {"status": response.status, "body": value, "schema": schema(value)}}
            packet["sha256"] = hashlib.sha256(canonical(packet)).hexdigest()
            return packet
    except (URLError, OSError, ValueError, HTTPException) as exc:
        # Do not include upstream errors/URL/query or response body in failure text.
        raise ContractError("Recording network operation failed") from exc


def validate(packet):
    if not isinstance(packet, dict) or type(packet.get("version")) is not int or packet["version"] != 1:
        raise ContractError("Unsupported fixture version")
    request, response = packet.get("request"), packet.get("response")
    if not isinstance(request, dict) or not isinstance(response, dict):
        raise ContractError("Malformed fixture")
    if not isinstance(request.get("target"), str) or not request["target"].startswith("/"):
        raise ContractError("Fixture target must be a relative request path")
    if request_key(request.get("method", ""), request.get("target", ""), request.get("body")) != request:
        raise ContractError("Fixture request key or redaction mismatch")
    if type(response.get("status")) is not int or not 100 <= response["status"] <= 599:
        raise ContractError("Invalid fixture HTTP status")
    if "body" not in response or redact(response["body"]) != response["body"] or schema(response["body"]) != response.get("schema"):
        raise ContractError("Fixture response schema or redaction mismatch")
    content = {key: value for key, value in packet.items() if key != "sha256"}
    if hashlib.sha256(canonical(content)).hexdigest() != packet.get("sha256"):
        raise ContractError("Fixture checksum mismatch")
    return packet


def write_fixture(path: Path, packet):
    validate(packet)
    content = canonical(packet) + b"\n"
    if len(content) > MAX_BYTES:
        raise ContractError("Redacted fixture exceeds 1 MiB limit")
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=".fixture-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except OSError as exc:
        raise ContractError("Fixture publication failed or output already exists") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_fixture(path: Path):
    try:
        if path.is_symlink() or not path.is_file():
            raise ContractError("Fixture must be a regular file")
        with path.open("rb") as stream:
            packet = parse_json(stream.read(MAX_BYTES + 1))
        return validate(packet)
    except OSError as exc:
        raise ContractError("Cannot read fixture") from exc


def compare(before, after):
    validate(before)
    validate(after)
    violations = []
    if before["request"] != after["request"]:
        violations.append("Request identity changed")
    if before["response"]["status"] != after["response"]["status"]:
        violations.append("HTTP status changed")
    def visit(old, new, path):
        issues, unknown = [], []
        if old["type"] != new["type"]:
            issues.append(f"{path}: observed type changed")
        elif old["type"] == "object":
            for key, value in old["properties"].items():
                child = path + "/" + key.replace("~", "~0").replace("/", "~1")
                if key not in new["properties"]:
                    issues.append(f"{child}: observed field disappeared")
                else:
                    nested_issues, nested_unknown = visit(value, new["properties"][key], child)
                    issues.extend(nested_issues)
                    unknown.extend(nested_unknown)
        elif old["type"] == "array":
            if bool(old["items"]) != bool(new["items"]):
                unknown.append(f"{path}: empty sample cannot establish array item compatibility")
            for item in new["items"]:
                if not old["items"]:
                    continue
                compatible = [prior for prior in old["items"] if prior["type"] == item["type"]]
                if not compatible:
                    issues.append(f"{path}/*: new observed item type")
                    continue
                choices = [visit(prior, item, path + "/*") for prior in compatible]
                valid = [choice for choice in choices if not choice[0]]
                if not valid:
                    issues.append(f"{path}/*: no compatible observed item shape")
                else:
                    # Prefer fully evidenced shape over uncertain array variants.
                    best = min(valid, key=lambda choice: len(choice[1]))
                    unknown.extend(best[1])
        return issues, unknown
    issues, uncertainties = visit(before["response"]["schema"], after["response"]["schema"], "$response")
    violations.extend(issues)
    return {"version": 1, "passed": not violations, "violations": violations,
            "uncertainties": sorted(set(uncertainties)),
            "compatible": False if violations else None if uncertainties else True,
            "scope": "observed-sample-drift-not-full-api-proof"}


def replay_server(packets, *, port=0):
    """Bind ONLY IPv4 loopback. Unknown request never falls through to network."""
    fixtures = {}
    for packet in packets:
        validate(packet)
        packet = copy.deepcopy(packet)
        key = packet["request"]["key"]
        if key in fixtures:
            raise ContractError("Duplicate replay request; choose one fixture per request")
        fixtures[key] = packet
    if not fixtures:
        raise ContractError("Replay requires a fixture")
    def select(key):
        packet = fixtures.get(key)
        if packet is None:
            return 404, {"error": "unrecorded request"}, 0
        return packet['response']['status'], packet['response']['body'], 0
    return _replay_server(select, port=port)


def _replay_server(select, *, port=0):
    """Shared loopback transport; selectors never fall through to a network."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Requests/query strings may contain secrets.
        def send_json(self, status, value):
            payload = canonical(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def handle_request(self):
            self.connection.settimeout(5)
            try:
                if self.headers.get_all("Transfer-Encoding") is not None:
                    raise ContractError("Streaming requests unsupported")
                length = _content_length(self.headers)
                length = 0 if length is None else length
                if self.command == "GET" and length:
                    raise ContractError("GET body is not supported")
                if not self.path.startswith("/") or urlsplit(self.path).netloc:
                    raise ContractError("Offline request needs a relative path")
                if length > MAX_BYTES:
                    self.send_json(413, {"error": "request too large"})
                    return
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ContractError("Incomplete request")
                body = parse_json(raw) if raw else None
                key = request_key(self.command, self.path, body)["key"]
                status, value, delay_ms = select(key)
                if delay_ms:
                    time.sleep(delay_ms / 1000)
                self.send_json(status, value)
            except (ContractError, OSError):
                try:
                    self.send_json(400, {"error": "invalid request"})
                except OSError:
                    pass
        do_GET = handle_request
        do_POST = handle_request
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
