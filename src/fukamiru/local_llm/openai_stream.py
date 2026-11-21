"""Strict incremental OpenAI SSE assembly, independent of the network reader."""

from __future__ import annotations

import json
from typing import Any


class StreamProtocolError(RuntimeError):
    pass


class CompletionStream:
    def __init__(self) -> None:
        self.reasoning: list[str] = []
        self.content: list[str] = []
        self.usage: dict[str, Any] = {}
        self.finish_reason: str | None = None
        self.saw_done = False
        self.events = 0
        self.reasoning_characters = 0
        self.final_characters = 0
        self._data: list[str] = []
        self._event = ""

    @property
    def finished(self) -> bool:
        return self.finish_reason is not None

    def feed_line(self, line: str) -> None:
        line = line.rstrip("\r\n")
        if not line:
            self._dispatch()
        elif line.startswith(":"):
            return
        elif line.startswith("event:"):
            self._event = line[6:].strip()
        elif line.startswith("data:"):
            self._data.append(line[5:].lstrip(" "))

    def _dispatch(self) -> None:
        data, event = "\n".join(self._data), self._event
        self._data, self._event = [], ""
        if not data:
            return
        self.events += 1
        if data.strip() == "[DONE]":
            self.saw_done = True
            if not self.finished:
                raise StreamProtocolError("stream.DONE_without_finish_reason")
            return
        try:
            value = json.loads(data)
        except json.JSONDecodeError as exc:
            raise StreamProtocolError("stream.invalid_event_JSON") from exc
        if not isinstance(value, dict):
            raise StreamProtocolError("stream.invalid_event_object")
        if event == "error" or value.get("error") or value.get("type") == "error":
            raise StreamProtocolError(
                "stream.backend_error: " + json.dumps(value, ensure_ascii=False)
            )
        if value.get("usage"):
            self.usage = value["usage"]
        choices = value.get("choices", [])
        if not isinstance(choices, list):
            raise StreamProtocolError("stream.invalid_choices")
        for choice in choices:
            if choice.get("index", 0) != 0:
                raise StreamProtocolError("stream.unexpected_choice_index")
            if choice.get("finish_reason"):
                self.finish_reason = str(choice["finish_reason"])
            delta = choice.get("delta") or {}
            reason = delta.get("reasoning_content") or ""
            content = delta.get("content") or ""
            if not isinstance(reason, str) or not isinstance(content, str):
                raise StreamProtocolError("stream.nontext_delta")
            self.reasoning.append(reason)
            self.content.append(content)
            self.reasoning_characters += len(reason)
            self.final_characters += len(content)

    def result(self) -> dict[str, Any]:
        self._dispatch()
        if not self.finished:
            raise StreamProtocolError("stream.EOF_without_finish_reason")
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "".join(self.content),
                        "reasoning_content": "".join(self.reasoning),
                    },
                    "finish_reason": self.finish_reason,
                }
            ],
            "usage": self.usage,
            "stream_statistics": {
                "events": self.events,
                "saw_done": self.saw_done,
                "terminal_finish_received": True,
            },
        }
