# Talking to the household's local Ollama server for the Cooking Assistant.
# The server address comes from .env (OLLAMA_URL); browsers never talk to Ollama directly.

# json encodes the request and reads the answer; logging tells the maintainer when Ollama is down.
import json
import logging
import time

# urllib is the standard-library HTTP client (no extra dependency).
import urllib.error
import urllib.request


MAX_RESPONSE_BYTES = 1_000_000


class AssistantUnavailable(Exception):
    """Ollama isn't running, can't be reached, or doesn't have the model."""


log = logging.getLogger("family_table.ollama")
_last_warning = 0.0


def _warn(reason: str, url: str) -> None:
    """At most once a minute: say why the model can't be reached (settings only, never what was asked)."""
    global _last_warning
    if time.monotonic() - _last_warning > 60:
        _last_warning = time.monotonic()
        log.warning("Ollama unreachable at %s: %s (the app keeps working without the model)", url, reason)


# Ignore any HTTP proxy settings: Ollama is a local service.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def chat(settings, messages: list[dict], response_format=None) -> str:
    """Send a conversation to Ollama and return the reply text.

    `response_format` is "json" or a JSON schema, so the reply can be read by the app.
    """
    body = {
        "model": settings.ollama_model,
        "messages": messages,
        "stream": False,
        "think": False,                # qwen3: answer directly, no hidden reasoning pass
        "options": {"temperature": 0.2},
        # Keep the model loaded between turns (a cold start takes several seconds, too slow for voice).
        "keep_alive": getattr(settings, "ollama_keep_alive", "30m"),
    }
    if response_format is not None:
        body["format"] = response_format
    request = urllib.request.Request(
        f"{settings.ollama_url}/api/chat", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with _opener.open(request, timeout=settings.ollama_timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            _warn(f"model {settings.ollama_model} isn't installed (ollama pull {settings.ollama_model})", settings.ollama_url)
            raise AssistantUnavailable(f"The model {settings.ollama_model} isn't installed in Ollama.") from None
        _warn(f"HTTP {exc.code}", settings.ollama_url)
        raise AssistantUnavailable(f"Ollama answered with an error ({exc.code}).") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _warn(type(exc).__name__, settings.ollama_url)
        raise AssistantUnavailable("Ollama isn't answering.") from exc
    try:
        return str(json.loads(raw)["message"]["content"])
    except (ValueError, KeyError, TypeError):
        raise AssistantUnavailable("Ollama sent a reply the app couldn't read.") from None
