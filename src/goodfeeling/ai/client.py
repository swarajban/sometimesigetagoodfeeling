"""Claude wrapper: one forced tool per call, every prompt and response logged, tokens and cost metered."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from PIL import Image

from goodfeeling.ai.pricing import cost_usd

log = logging.getLogger(__name__)

Transport = Callable[[dict[str, Any]], dict[str, Any]]


class AiError(RuntimeError):
    pass


class Logger(Protocol):
    def record(self, step: str, model: str, request: dict, response: dict, usage: dict, cost: float) -> None: ...


def anthropic_transport(api_key: str) -> Transport:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, max_retries=4)

    def send(request: dict[str, Any]) -> dict[str, Any]:
        message = client.messages.create(**request)
        return message.model_dump(mode="json", exclude_none=True)

    return send


def image_block(image: Image.Image, quality: int = 85) -> dict[str, Any]:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=quality)
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()},
    }


def redact_images(value: Any) -> Any:
    """Keep logs readable: swap base64 image data for its size and hash."""
    if isinstance(value, dict):
        if value.get("type") == "base64" and "data" in value:
            raw = value["data"]
            return {
                "type": "base64",
                "media_type": value.get("media_type"),
                "bytes": len(raw) * 3 // 4,
                "sha256": hashlib.sha256(raw.encode()).hexdigest()[:16],
            }
        return {k: redact_images(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_images(v) for v in value]
    return value


@dataclass
class ToolCall:
    input: dict[str, Any]
    response: dict[str, Any]
    usage: dict[str, int]
    cost: float


@dataclass
class Meter:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float = 0.0
    by_step: dict[str, float] = field(default_factory=dict)

    def add(self, step: str, usage: dict[str, int], cost: float) -> None:
        self.calls += 1
        self.input_tokens += usage["input_tokens"]
        self.output_tokens += usage["output_tokens"]
        self.cache_read += usage["cache_read"]
        self.cache_write += usage["cache_write"]
        self.cost += cost
        self.by_step[step] = self.by_step.get(step, 0.0) + cost


class JsonlLogger:
    """Appends every request and response to logs/ai.jsonl and to the ai_log table."""

    def __init__(self, path: Path, db: Any = None, project_id: int | None = None):
        self.path = path
        self.db = db
        self.project_id = project_id
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, step: str, model: str, request: dict, response: dict, usage: dict, cost: float) -> None:
        entry = {
            "time": time.time(),
            "step": step,
            "model": model,
            "request": redact_images(request),
            "response": response,
            "usage": usage,
            "cost_usd": round(cost, 6),
        }
        with self.path.open("a") as handle:
            handle.write(json.dumps(entry) + "\n")
        if self.db is not None:
            self.db.execute(
                "INSERT INTO ai_log (project_id, step, model, request_json, response_json, input_tokens, output_tokens,"
                " cache_read_tokens, cache_write_tokens, cost_usd, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.project_id,
                    step,
                    model,
                    json.dumps(entry["request"]),
                    json.dumps(response),
                    usage["input_tokens"],
                    usage["output_tokens"],
                    usage["cache_read"],
                    usage["cache_write"],
                    cost,
                    time.time(),
                ),
            )


def usage_of(response: dict[str, Any]) -> dict[str, int]:
    usage = response.get("usage") or {}
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
        "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
    }


def find_tool_use(response: dict[str, Any], name: str) -> dict[str, Any] | None:
    for block in response.get("content") or []:
        if block.get("type") == "tool_use" and block.get("name") == name:
            return block
    return None


def assistant_content(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Response content as it must be sent back in a follow-up turn (thinking blocks included)."""
    keep = {"thinking", "redacted_thinking", "text", "tool_use"}
    allowed = {
        "thinking": {"type", "thinking", "signature"},
        "redacted_thinking": {"type", "data"},
        "text": {"type", "text"},
        "tool_use": {"type", "id", "name", "input"},
    }
    return [
        {k: v for k, v in block.items() if k in allowed[block["type"]]}
        for block in response.get("content") or []
        if block.get("type") in keep
    ]


class Claude:
    def __init__(
        self,
        transport: Transport,
        logger: Logger | None = None,
        meter: Meter | None = None,
        budget_usd: float | None = None,
    ):
        self.transport = transport
        self.logger = logger
        self.meter = meter or Meter()
        self.budget_usd = budget_usd

    def send(self, step: str, request: dict[str, Any]) -> dict[str, Any]:
        if self.budget_usd is not None and self.meter.cost >= self.budget_usd:
            raise AiError(f"spend limit reached: ${self.meter.cost:.2f} of ${self.budget_usd:.2f} (max_cost_usd)")
        response = self.transport(request)
        usage = usage_of(response)
        cost = cost_usd(
            request["model"], usage["input_tokens"], usage["output_tokens"], usage["cache_read"], usage["cache_write"]
        )
        self.meter.add(step, usage, cost)
        if self.logger:
            self.logger.record(step, request["model"], request, response, usage, cost)
        log.info(
            "%s: %s in=%d out=%d cache_read=%d cost=$%.4f",
            step,
            request["model"],
            usage["input_tokens"],
            usage["output_tokens"],
            usage["cache_read"],
            cost,
        )
        return response

    def call_tool(
        self,
        *,
        step: str,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tool: dict[str, Any],
        effort: str | None = None,
        max_tokens: int = 16000,
        cache_system: bool = False,
    ) -> tuple[ToolCall, list[dict[str, Any]]]:
        """Ask for one tool call. Returns the call and the conversation including Claude's turn."""
        system_block: dict[str, Any] = {"type": "text", "text": system}
        if cache_system:
            system_block["cache_control"] = {"type": "ephemeral"}
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [system_block],
            "tools": [tool],
            "tool_choice": {"type": "auto"},
            "messages": messages,
        }
        if effort:
            request["output_config"] = {"effort": effort}
        conversation = list(messages)
        for attempt in range(2):
            request["messages"] = conversation
            response = self.send(step, request)
            conversation = [*conversation, {"role": "assistant", "content": assistant_content(response)}]
            block = find_tool_use(response, tool["name"])
            if block is not None:
                usage = usage_of(response)
                cost = cost_usd(
                    model, usage["input_tokens"], usage["output_tokens"], usage["cache_read"], usage["cache_write"]
                )
                return ToolCall(input=block["input"], response=response, usage=usage, cost=cost), conversation
            if attempt == 0:
                conversation.append({"role": "user", "content": f"Please answer by calling the {tool['name']} tool."})
        raise AiError(f"{step}: Claude did not call {tool['name']} (stop_reason={response.get('stop_reason')})")


def tool_result(tool_use_id: str, text: str, is_error: bool = False) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": text, "is_error": is_error}
