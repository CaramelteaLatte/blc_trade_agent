"""Bounded, read-only Ethereum JSON-RPC requests; secrets never enter errors."""

import json
import http.client
import os
import threading
import time
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from dotenv import dotenv_values

from .api_config import NoRedirects
from .data_tools import ROOT


class RpcError(ValueError):
    def __init__(self, message: str, *, range_limit: bool = False):
        super().__init__(message)
        self.range_limit = range_limit


def provider_error_flags(error) -> tuple[bool, bool]:
    """Classify provider text without including authenticated URLs in diagnostics."""
    if not isinstance(error, dict):
        return False, False
    message = str(error.get("message", "")).lower()
    rate = error.get("code") == 429 or any(x in message for x in
        ("rate limit", "throughput", "too many requests"))
    ranged = not rate and any(x in message for x in
        ("block range", "too many results", "response size", "log response size", "query returned more"))
    return rate, ranged


@dataclass(frozen=True)
class RpcSettings:
    url: str = field(repr=False)
    deadline_seconds: float = 180
    max_requests: int = 2000
    block_span: int = 10
    workers: int = 4

    @classmethod
    def from_environment(cls):
        value = os.environ.get("ALCHEMY_RPC_URL")
        if value is None:
            value = dotenv_values(ROOT / ".env").get("ALCHEMY_RPC_URL", "")
        url = (value or "").strip()
        if not url:
            raise RpcError("Missing ALCHEMY_RPC_URL. Configure an Alchemy Ethereum Mainnet HTTPS endpoint in .env.")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise RpcError("Malformed Alchemy RPC endpoint.") from None
        if (parsed.scheme != "https" or parsed.hostname != "eth-mainnet.g.alchemy.com"
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or port not in (None, 443) or not parsed.path.startswith("/v2/")
                or not parsed.path.removeprefix("/v2/").strip("/")):
            raise RpcError("ALCHEMY_RPC_URL must be an Alchemy Ethereum Mainnet HTTPS /v2/<key> endpoint.")
        return cls(url)

    def public_dict(self):
        return {"configured": True, "provider": "Alchemy Ethereum Mainnet",
                "host": "eth-mainnet.g.alchemy.com", "log_block_span": self.block_span,
                "workers": self.workers, "deadline_seconds": self.deadline_seconds,
                "max_requests": self.max_requests}


def rpc_configuration_status() -> dict:
    try:
        return RpcSettings.from_environment().public_dict()
    except ValueError as exc:
        return {"configured": False, "reason": str(exc)}


class RpcClient:
    READ_METHODS = {"eth_chainId", "eth_getBlockByNumber", "eth_getLogs", "eth_call"}

    def __init__(self, settings: RpcSettings):
        self.settings = settings
        self.deadline = time.monotonic() + settings.deadline_seconds
        self.request_count = 0
        self._lock = threading.Lock()

    def remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise RpcError("RPC collection deadline reached; no partial metrics will be returned. Try a shorter window.")
        return left

    def call(self, method: str, params: list):
        if method not in self.READ_METHODS:
            raise RpcError("Only read-only Ethereum RPC methods are enabled.")
        for attempt in range(3):
            with self._lock:
                if self.request_count >= self.settings.max_requests:
                    raise RpcError("RPC request budget reached; try a shorter window.")
                self.request_count += 1
                request_id = self.request_count
            timeout = min(15, self.remaining())
            body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode()
            request = Request(self.settings.url, body, {"Content-Type": "application/json"})
            retry = False
            retry_kind = "RPC server error"
            try:
                with build_opener(NoRedirects()).open(request, timeout=timeout) as response:
                    raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise RpcError("RPC response too large.", range_limit=method == "eth_getLogs")
                payload = json.loads(raw)
                if not isinstance(payload, dict) or payload.get("id") != request_id:
                    raise RpcError("Invalid JSON-RPC response envelope.")
                if "error" in payload:
                    error = payload["error"]
                    code = error.get("code") if isinstance(error, dict) else None
                    retry, ranged = provider_error_flags(error)
                    ranged = method == "eth_getLogs" and ranged
                    retry_kind = "provider rate limit"
                    if not retry:
                        # Provider error text can contain the authenticated URL; do not forward it.
                        label = str(code) if isinstance(code, int) else "unknown"
                        raise RpcError(f"{method} RPC error ({label}); check endpoint permissions or requested range.", range_limit=ranged)
                elif "result" in payload:
                    return payload["result"]
                else:
                    raise RpcError("JSON-RPC response has no result.")
            except HTTPError as exc:
                retry = exc.code in (429, 500, 502, 503, 504)
                retry_kind = "provider rate limit (HTTP 429)" if exc.code == 429 else f"RPC server error (HTTP {exc.code})"
                if exc.code == 400 and method == "eth_getLogs":
                    try:
                        payload = json.loads(exc.read(65536))
                        _, ranged = provider_error_flags(payload.get("error") if isinstance(payload, dict) else None)
                    except (ValueError, OSError, http.client.HTTPException):
                        ranged = False
                    if ranged:
                        raise RpcError("eth_getLogs HTTP 400: provider block-range limit; split into smaller ranges.", range_limit=True) from None
                if not retry:
                    hint = "Check the Alchemy key and Ethereum Mainnet access." if exc.code in (401, 403) else "Check the endpoint configuration."
                    raise RpcError(f"{method} HTTP {exc.code}. {hint}", range_limit=exc.code == 413 and method == "eth_getLogs") from None
            except (URLError, OSError, http.client.HTTPException):
                retry = True
                retry_kind = "network connection/timeout error"
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise RpcError("RPC returned invalid JSON.") from None
            if retry:
                if attempt == 2:
                    raise RpcError(f"{method} failed after 3 attempts: {retry_kind}.")
                time.sleep(min(0.5 * 2 ** attempt, self.remaining()))


def quantity(value, name="quantity") -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise RpcError(f"Invalid RPC {name}.")
    try:
        result = int(value, 16)
    except ValueError:
        raise RpcError(f"Invalid RPC {name}.") from None
    if result < 0:
        raise RpcError(f"Invalid RPC {name}.")
    return result


def block_header(rpc, number_or_tag) -> dict:
    tag = hex(number_or_tag) if isinstance(number_or_tag, int) else number_or_tag
    result = rpc.call("eth_getBlockByNumber", [tag, False])
    if not isinstance(result, dict):
        raise RpcError(f"RPC could not return the requested {tag} block.")
    number = quantity(result.get("number"), "block number")
    if isinstance(number_or_tag, int) and number != number_or_tag:
        raise RpcError("RPC returned a different block number.")
    digest = result.get("hash")
    if not isinstance(digest, str) or len(digest) != 66 or not digest.startswith("0x"):
        raise RpcError("RPC block hash is missing or malformed.")
    return {"number": number, "hash": digest.lower(), "timestamp": quantity(result.get("timestamp"), "block timestamp")}
