"""A stand-in for `anthropic.Anthropic` so the tagging tests stay offline.

Mirrors just the surface `services/llm.py` touches: `client.messages.create(**kw)`
returning an object with `.content` (text blocks), `.usage` and `.stop_reason`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeBlock:
    text: str
    type: str = "text"


@dataclass
class FakeUsage:
    input_tokens: int = 1000
    output_tokens: int = 400
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class FakeResponse:
    content: list[FakeBlock]
    usage: FakeUsage = field(default_factory=FakeUsage)
    stop_reason: str = "end_turn"


def reply(text: str, **usage: int) -> FakeResponse:
    return FakeResponse(content=[FakeBlock(text=text)], usage=FakeUsage(**usage))


def json_reply(results: list[dict[str, Any]], **usage: int) -> FakeResponse:
    return reply(json.dumps({"results": results}, ensure_ascii=False), **usage)


def tag_for(item_id: int, **over: Any) -> dict[str, Any]:
    """A well-formed result object for `item_id`."""
    base: dict[str, Any] = {
        "id": item_id,
        "tickers": ["SNTS"],
        "relevance": 7,
        "category": "earnings",
        "summary_fr": "Résultats semestriels en hausse.",
        "summary_en": "Half-year results up.",
    }
    base.update(over)
    return base


class FakeAnthropic:
    """Replays a scripted list of responses, recording every request.

    Each entry is either a `FakeResponse`, a callable taking the request
    kwargs and returning one, or an exception instance to raise.
    """

    def __init__(self, replies: list[Any] | None = None) -> None:
        self._replies = list(replies or [])
        self.calls: list[dict[str, Any]] = []
        self.messages = _Messages(self)

    def _create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if not self._replies:
            raise AssertionError(f"FakeAnthropic ran out of replies (call #{len(self.calls)})")
        nxt = self._replies.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        if isinstance(nxt, Callable):  # type: ignore[arg-type]
            return nxt(kwargs)
        return nxt

    @property
    def call_count(self) -> int:
        return len(self.calls)


class _Messages:
    def __init__(self, parent: FakeAnthropic) -> None:
        self._parent = parent

    def create(self, **kwargs: Any) -> FakeResponse:
        return self._parent._create(**kwargs)


def echoing_client(**usage: int) -> FakeAnthropic:
    """A client that tags every id it is asked about, forever.

    Useful for end-to-end worker tests where the exact tag content doesn't
    matter but the id round-trip does.
    """

    def handler(kwargs: dict[str, Any]) -> FakeResponse:
        payload = kwargs["messages"][0]["content"].split("INPUT:\n", 1)[1]
        ids = [it["id"] for it in json.loads(payload)]
        return json_reply([tag_for(i) for i in ids], **usage)

    client = FakeAnthropic()
    client._replies = _Endless(handler)  # type: ignore[assignment]
    return client


class _Endless(list):
    """A list that always yields the same handler on pop(0)."""

    def __init__(self, handler: Callable[[dict[str, Any]], FakeResponse]) -> None:
        super().__init__()
        self._handler = handler

    def __bool__(self) -> bool:
        return True

    def pop(self, index: int = -1):  # signature parity with list
        return self._handler
