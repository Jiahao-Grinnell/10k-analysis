"""Model clients. The real client talks to any OpenAI-compatible server on this machine."""
from __future__ import annotations

import json
import socket
import threading
import time
from typing import Callable
import urllib.error
import urllib.request


class ClientError(Exception):
    """A request failed; `code` is a short machine-readable reason."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class OpenAICompatibleClient:
    """POST /v1/chat/completions with a JSON-schema response format (llama.cpp llama-server)."""

    def __init__(self, base_url: str, tag: str, model: str = "", timeout: float = 300,
                 max_output_tokens: int = 256, chat_template_kwargs: dict | None = None):
        self.url = base_url.rstrip("/") + "/v1/chat/completions"
        self.tag, self.model, self.timeout = tag, model or tag, timeout
        self.max_output_tokens = max_output_tokens
        self.chat_template_kwargs = chat_template_kwargs or {}
        # Never route local requests through a system proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def describe(self) -> dict:
        """Settings that change model output (stored with results and used for resume)."""
        return {"backend": "openai", "model_tag": self.tag, "model": self.model, "temperature": 0,
                "max_output_tokens": self.max_output_tokens, "chat_template_kwargs": self.chat_template_kwargs}

    def complete(self, messages: list[dict], schema: dict) -> str:
        body = {"model": self.model, "messages": messages, "temperature": 0,
                "max_tokens": self.max_output_tokens,
                "response_format": {"type": "json_schema", "schema": schema}}
        if self.chat_template_kwargs:
            body["chat_template_kwargs"] = self.chat_template_kwargs
        request = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ClientError(f"http_{exc.code}", exc.read().decode("utf-8", "replace")[:300]) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ClientError("timeout", str(exc)) from exc
        except urllib.error.URLError as exc:
            code = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "unreachable"
            raise ClientError(code, str(exc.reason)) from exc
        except (ValueError, OSError) as exc:
            raise ClientError("bad_response", str(exc)) from exc
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ClientError("bad_response", "no message content") from exc
        if choice.get("finish_reason") == "length":
            raise ClientError("truncated", "output hit max_output_tokens")
        return content


class FakeClient:
    """Test stand-in: no GPU, no model. `respond(messages)` returns text or raises."""

    def __init__(self, respond: Callable[[list[dict]], str] | None = None, tag: str = "fake", delay: float = 0.0):
        self.tag, self.delay = tag, delay  # delay: seconds per request, to watch progress in the monitor
        self.respond = respond or (lambda messages: json.dumps(
            {"reason": "fake client: no model judgement", "label": "UNCERTAIN", "categories": []}))
        self.calls: list[list[dict]] = []
        self._lock = threading.Lock()

    def describe(self) -> dict:
        return {"backend": "fake", "model_tag": self.tag}

    def complete(self, messages: list[dict], schema: dict) -> str:
        with self._lock:
            self.calls.append(messages)
        if self.delay:
            time.sleep(self.delay)
        return self.respond(messages)


def from_config(cfg, fake: bool = False, fake_delay: float = 0.0):
    settings = cfg.classify
    if fake or settings["backend"] == "fake":
        return FakeClient(delay=fake_delay)
    if not settings["model_tag"]:
        raise SystemExit("No local model configured: set [classify] model_tag in config.toml "
                         "(see TASK-06), or use --fake-model to test the pipeline without a model.")
    return OpenAICompatibleClient(settings["base_url"], settings["model_tag"], settings["model"],
                                  float(settings["timeout_seconds"]), int(settings["max_output_tokens"]),
                                  dict(settings["chat_template_kwargs"]))
