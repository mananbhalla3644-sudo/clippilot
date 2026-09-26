from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from ..utils.logging import get_logger

log = get_logger("agent.memory")


class Memory:
    """
    Conversation state with compaction and a tiny persistent fact store.

    Facts let the agent remember things across runs ("CapCut's import button is
    called 'Import' in the top-left", "the user's renders go to Desktop").
    """

    def __init__(self, state_path: str | Path | None = None, history_limit: int = 24) -> None:
        self.messages: list[dict[str, Any]] = []
        self.history_limit = history_limit
        self.state_path = Path(state_path) if state_path else None
        self.facts: dict[str, Any] = {}
        self.summaries: list[str] = []
        if self.state_path and self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.facts = data.get("facts", {})
            except Exception as exc:  # noqa: BLE001
                log.debug("could not read state: %s", exc)

    # ----------------------------------------------------------------- facts
    def remember(self, key: str, value: Any) -> None:
        self.facts[key] = value
        self.save()

    def recall(self, key: str, default: Any = None) -> Any:
        return self.facts.get(key, default)

    def forget(self, key: str) -> None:
        self.facts.pop(key, None)
        self.save()

    def save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps({"facts": self.facts, "updated": time.time()}, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------- messages
    def set_system(self, content: str) -> None:
        self.messages = [{"role": "system", "content": content}]

    def add(self, role: str, content: Any, **extra: Any) -> None:
        self.messages.append({"role": role, "content": content, **extra})

    def add_tool_result(self, call_id: str, name: str, content: str) -> None:
        self.messages.append(
            {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}
        )

    @property
    def turn_count(self) -> int:
        return sum(1 for m in self.messages if m.get("role") == "user")

    def needs_compaction(self) -> bool:
        return len(self.messages) > self.history_limit * 2

    def compact(self, llm: Any = None) -> None:
        """
        Keep the system prompt, the first task, and the recent tail.
        Older turns get folded into a running summary so the agent does not
        lose track of what it already tried.
        """
        if len(self.messages) <= 6:
            return
        system = self.messages[0] if self.messages[0].get("role") == "system" else None
        first_task = next(
            (m for m in self.messages[1:] if m.get("role") == "user" and isinstance(m.get("content"), str)),
            None,
        )
        keep = 8
        head = [m for m in self.messages if system is None or m is not first_task][: 0]
        tail = self.messages[-keep:]
        middle = self.messages[: len(self.messages) - keep]

        summary_text = self._summarize(middle, llm)
        if summary_text:
            self.summaries.append(summary_text)

        new: list[dict[str, Any]] = []
        if system:
            new.append(system)
        if self.summaries:
            new.append(
                {
                    "role": "user",
                    "content": "RECAP OF EARLIER WORK (already done, do not repeat):\n"
                    + "\n".join(f"- {s}" for s in self.summaries[-3:]),
                }
            )
        if first_task is not None and first_task not in new:
            new.append(first_task)
        new.extend(tail)
        dropped = len(self.messages) - len(new)
        self.messages = new
        log.info("compacted history: %d messages -> %d (dropped %d)", len(middle) + len(tail) + 1, len(new), dropped)
        del head

    def _summarize(self, messages: list[dict[str, Any]], llm: Any) -> str:
        if not messages:
            return ""
        lines: list[str] = []
        for m in messages:
            role = m.get("role")
            if role == "user" and isinstance(m.get("content"), str):
                first = m["content"].strip().splitlines()
                lines.append("USER/OBS: " + (first[0][:160] if first else ""))
            elif role == "assistant":
                if m.get("tool_calls"):
                    for tc in m["tool_calls"]:
                        fn = (tc.get("function") or {})
                        lines.append(f"ACTION: {fn.get('name')} {str(fn.get('arguments'))[:120]}")
                elif isinstance(m.get("content"), str) and m["content"].strip():
                    lines.append("SAID: " + m["content"][:160])
            elif role == "tool":
                body = str(m.get("content", "")).replace("\n", " ")[:140]
                lines.append(f"RESULT({m.get('name')}): {body}")
        if not lines:
            return ""
        if llm is not None:
            try:
                text = llm.complete(
                    [
                        {
                            "role": "system",
                            "content": "Compress this agent log into at most 3 short bullets. Keep: what was done, "
                            "what failed and why, and any file paths. No preamble.",
                        },
                        {"role": "user", "content": "\n".join(lines)},
                    ],
                    temperature=0.0,
                    max_tokens=220,
                )
                return " ".join(text.strip().split())[:600]
            except Exception as exc:  # noqa: BLE001
                log.debug("summary failed, using raw lines: %s", exc)
        return " | ".join(lines[-8:])[:600]

    def facts_for_prompt(self, limit: int = 12) -> str:
        if not self.facts:
            return ""
        items = list(self.facts.items())[:limit]
        return "\n".join(f"- {k}: {v}" for k, v in items)


def make_state_path(config_root: Path, name: str = "state.json") -> Path:
    return config_root / ".clippilot" / name
