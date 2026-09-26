from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..config import Config
from ..errors import LLMError
from ..utils.logging import get_logger

log = get_logger("agent.llm")

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = ""
    raw: str = ""


@dataclass
class Completion:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool(self) -> bool:
        return bool(self.tool_calls)


class LlmClient:
    """
    OpenAI-compatible chat client.

    Works with OpenAI, OpenRouter, Groq, Together, Ollama, LM Studio, vLLM,
    llama.cpp servers - anything that speaks /chat/completions.
    """

    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.base_url = str(config.llm.get("base_url", "")).rstrip("/")
        self.model = str(config.llm.get("model", ""))
        self.api_key = str(config.llm.get("api_key", ""))
        self.temperature = float(config.llm.get("temperature", 0.1))
        self.max_tokens = int(config.llm.get("max_tokens", 1200))
        self.timeout = float(config.llm.get("timeout", 90))
        self.max_retries = int(config.llm.get("max_retries", 4))
        self.native_tools = bool(config.llm.get("native_tools", True))
        self.usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        self._session: Any = None

    # ------------------------------------------------------------------ http
    def _client(self) -> Any:
        if self._session is None:
            import requests

            s = requests.Session()
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            s.headers.update(headers)
            self._session = s
        return self._session

    def _url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        import requests

        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._client().post(self._url(), json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                last = exc
                wait = min(20.0, 1.5 * attempt)
                log.warning("LLM request error (%s), retry %d/%d in %.1fs", exc, attempt, self.max_retries, wait)
                time.sleep(wait)
                continue
            if resp.status_code in RETRY_STATUS:
                wait = min(25.0, 2.0 * attempt)
                log.warning("LLM HTTP %d, retry %d/%d in %.1fs", resp.status_code, attempt, self.max_retries, wait)
                last = LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                body = resp.text[:600]
                hint = ""
                if resp.status_code == 401:
                    hint = " - check llm.api_key"
                elif resp.status_code == 404:
                    hint = f" - check llm.base_url ({self.base_url}) and llm.model ({self.model})"
                elif resp.status_code == 400 and "model" in resp.text.lower():
                    hint = " - the endpoint rejected that model name"
                raise LLMError(f"LLM HTTP {resp.status_code}{hint}: {body}")
            try:
                return resp.json()
            except ValueError as exc:
                raise LLMError(f"LLM returned non-JSON: {resp.text[:300]}") from exc
        raise LLMError(f"LLM unreachable after {self.max_retries} attempts: {last}")

    # -------------------------------------------------------------- messages
    @staticmethod
    def _trim(messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return [m for m in messages if m is not None]

    def _track(self, data: dict[str, Any]) -> None:
        usage = data.get("usage") or {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if isinstance(usage.get(key), int):
                self.usage[key] += usage[key]
        self.usage["calls"] += 1

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": self._trim(messages),
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        use_tools = bool(tools) and self.native_tools
        if use_tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        elif json_mode:
            payload["response_format"] = {"type": "json_object"}

        data = self._post(payload)
        self._track(data)
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"no choices in LLM response: {json.dumps(data)[:300]}")
        msg = choices[0].get("message") or {}
        comp = Completion(model=str(data.get("model", payload["model"])), raw=data)
        comp.text = str(msg.get("content") or "")
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
            except (json.JSONDecodeError, TypeError):
                args = {}
                log.warning("unparsable tool arguments: %r", raw_args)
            comp.tool_calls.append(ToolCall(str(fn.get("name", "")), args, str(tc.get("id", "")), str(raw_args)))
        if msg.get("reasoning_content"):
            comp.text = comp.text or ""
            log.debug("provider returned reasoning_content")
        return comp

    def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        return self.chat(messages, **kwargs).text

    def json_chat(
        self,
        messages: list[dict[str, Any]],
        system_hint: str = "Reply with a single JSON object and nothing else.",
        **kwargs: Any,
    ) -> Any:
        from ..perception.vlm import extract_json

        msgs = list(messages)
        if msgs and msgs[0].get("role") == "system":
            msgs[0] = {"role": "system", "content": str(msgs[0].get("content", "")) + "\n" + system_hint}
        else:
            msgs.insert(0, {"role": "system", "content": system_hint})
        text = self.chat(msgs, json_mode=True, **kwargs).text
        return extract_json(text)

    # ----------------------------------------------------------------- checks
    def ping(self) -> dict[str, Any]:
        """Cheap liveness probe so `doctor` can verify the endpoint."""
        try:
            comp = self.chat(
                [
                    {"role": "system", "content": "Reply with the single word: ok"},
                    {"role": "user", "content": "ping"},
                ],
                temperature=0.0,
                max_tokens=8,
            )
            return {
                "ok": True,
                "model": comp.model,
                "reply": comp.text.strip()[:40],
                "native_tools_supported": self.native_tools,
            }
        except LLMError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def list_models(self) -> list[str]:
        try:
            import requests

            r = self._client().get(f"{self.base_url}/models", timeout=min(30, self.timeout))
            if r.status_code >= 400:
                return []
            return sorted({m.get("id", "") for m in (r.json().get("data") or [])})
        except Exception:
            return []
