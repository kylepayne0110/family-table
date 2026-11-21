# Talking to the household's local Ollama server for the Cooking Assistant.
# The server address comes from .env (OLLAMA_URL); browsers never talk to Ollama directly.

# json encodes the request and reads the answer.
import json

# urllib is the standard-library HTTP client (no extra dependency).
import urllib.error
import urllib.request


MAX_RESPONSE_BYTES = 1_000_000


class AssistantUnavailable(Exception):
    """Ollama isn't running, can't be reached, or doesn't have the model."""


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
            raise AssistantUnavailable(f"The model {settings.ollama_model} isn't installed in Ollama.") from None
        raise AssistantUnavailable(f"Ollama answered with an error ({exc.code}).") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise AssistantUnavailable("Ollama isn't answering.") from exc
    try:
        return str(json.loads(raw)["message"]["content"])
    except (ValueError, KeyError, TypeError):
        raise AssistantUnavailable("Ollama sent a reply the app couldn't read.") from None
