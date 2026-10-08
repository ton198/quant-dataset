"""Single-model OpenAI-compatible request and response contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class ModelContractError(ValueError):
    """A request or response violates the model contract."""


class UnknownRequestError(RuntimeError):
    """The model request outcome is unknown; no response was received."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    provider: str
    model: str
    base_url: str | None = None


@dataclass(frozen=True, slots=True)
class ModelRequest:
    descriptor: ModelDescriptor
    messages: tuple[dict[str, str], ...]
    schema: dict[str, Any]
    max_output_tokens: int
    reference_map: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, ModelDescriptor):
            raise ModelContractError("descriptor must be a ModelDescriptor")
        if not isinstance(self.max_output_tokens, int) or isinstance(self.max_output_tokens, bool) or self.max_output_tokens <= 0:
            raise ModelContractError("max_output_tokens must be a positive integer")
        if any(not isinstance(message, dict) or set(message) != {"role", "content"} for message in self.messages):
            raise ModelContractError("messages must contain only role/content objects")
        if not isinstance(self.schema, dict):
            raise ModelContractError("schema must be an object")
        if not isinstance(self.reference_map, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.reference_map.items()
        ):
            raise ModelContractError("reference_map must map short-ID strings to long-ref strings")

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "descriptor": {"provider": self.descriptor.provider, "model": self.descriptor.model, "base_url": self.descriptor.base_url},
            "messages": list(self.messages), "schema": self.schema, "max_output_tokens": self.max_output_tokens,
            "reference_map": dict(self.reference_map),
        }

    @property
    def identity(self) -> str:
        return hashlib.sha256(canonical_json(self.canonical_dict()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ModelResponse:
    raw_json: str
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = "stop"
    raw_response: dict[str, Any] | None = None


@runtime_checkable
class ModelClient(Protocol):
    @property
    def descriptor(self) -> ModelDescriptor: ...
    def complete(self, request: ModelRequest) -> ModelResponse: ...


__all__ = ["ModelClient", "ModelContractError", "ModelDescriptor", "ModelRequest", "ModelResponse", "UnknownRequestError", "canonical_json"]
