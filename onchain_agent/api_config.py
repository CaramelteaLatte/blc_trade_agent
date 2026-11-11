"""Explicit API routing and a credential-safe model catalog check."""

import json
import os
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from dotenv import load_dotenv

from .data_tools import ROOT

MODEL = "gpt-6.1-sol"
DEFAULT_BASE_URL = "https://sing.hku.icu/v1"
HARNESS_PROVIDER = "research-api"


def load_credentials() -> bool:
    load_dotenv(ROOT / ".env", override=False)
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def checked_url(value: str, name: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError(f"{name} must be an HTTPS URL without credentials, query or fragment.")
    # Accessing port also rejects malformed port numbers before a request.
    _ = parsed.port
    return value


@dataclass(frozen=True)
class ApiSettings:
    base_url: str
    model_catalog_url: str
    model: str = MODEL
    api_key: str = field(default="", repr=False)

    @classmethod
    def from_environment(cls) -> "ApiSettings":
        load_credentials()
        base = checked_url(os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL), "OPENAI_BASE_URL")
        catalog = checked_url(os.environ.get("OPENAI_MODEL_CATALOG_URL", base + "/models"), "OPENAI_MODEL_CATALOG_URL")
        a, b = urlsplit(base), urlsplit(catalog)
        if (a.hostname, a.port or 443) != (b.hostname, b.port or 443):
            raise ValueError("Model catalog and inference API must use the same HTTPS origin.")
        model = os.environ.get("OPENAI_MODEL", MODEL).strip()
        if not model or any(character.isspace() for character in model):
            raise ValueError("OPENAI_MODEL must be a non-empty model ID without whitespace.")
        return cls(base, catalog, model, os.environ.get("OPENAI_API_KEY", "").strip())

    def public_dict(self) -> dict:
        return {"provider": HARNESS_PROVIDER, "api": "responses", "base_url": self.base_url,
                "model_catalog_url": self.model_catalog_url, "model": self.model,
                "api_key_configured": bool(self.api_key)}

    def require_key(self) -> None:
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is missing. Set the API gateway key in the project .env file.")


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the gateway credential to a redirected endpoint.
        return None


def check_model_catalog(settings: ApiSettings) -> dict:
    settings.require_key()
    request = Request(settings.model_catalog_url,
                      headers={"Authorization": "Bearer " + settings.api_key, "Accept": "application/json"})
    try:
        with build_opener(NoRedirects()).open(request, timeout=30) as response:
            body = response.read(2 * 1024 * 1024 + 1)
            if len(body) > 2 * 1024 * 1024:
                raise ValueError("Model catalog exceeds the 2 MiB response limit.")
            payload = json.loads(body)
    except HTTPError as exc:
        hint = {401: "Check the gateway API key.", 403: "Check gateway access permissions.",
                404: "Check OPENAI_MODEL_CATALOG_URL.", 429: "Gateway rate limit or quota reached."}.get(exc.code, "Check the gateway status and URL.")
        raise ValueError(f"Model catalog HTTP {exc.code}. {hint}") from None
    except (URLError, TimeoutError) as exc:
        raise ValueError("Cannot reach model catalog: " + str(exc).replace(settings.api_key, "[REDACTED]")) from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Model catalog did not return valid JSON.") from None
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError("Model catalog must contain a data array of model IDs.")
    ids = sorted({item["id"] for item in entries if isinstance(item, dict) and isinstance(item.get("id"), str)})
    return {"url": settings.model_catalog_url, "model_count": len(ids),
            "requested_model": settings.model, "model_available": settings.model in ids,
            "model_ids": ids, "scope": "Gateway-advertised IDs; not independent verification of upstream model identity."}
