"""Lazy OpenAI-compatible provider adapter; credentials never enter request records."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any

from .client import ModelContractError, ModelDescriptor, ModelRequest, ModelResponse, UnknownRequestError


def _unknown_transport_types() -> tuple[type, ...]:
    """Transport failures with an unknown request outcome (fake-SDK safe)."""
    types: list[type] = [TimeoutError, ConnectionError]
    try:
        import openai
    except ImportError:
        return tuple(types)
    for name in ("APIConnectionError", "APITimeoutError"):
        exc_type = getattr(openai, name, None)
        if isinstance(exc_type, type) and issubclass(exc_type, BaseException):
            if exc_type not in types:
                types.append(exc_type)
    return tuple(types)


@dataclass(slots=True)
class OpenAICompatibleClient:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 120
    structured_mode: str = "json_schema"
    _descriptor: ModelDescriptor = field(init=False, repr=False)
    _client: Any = field(init=False, repr=False, default=None)
    _lock: threading.Lock = field(init=False, repr=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not self.base_url.strip() or not self.model.strip():
            raise ValueError("base_url and model must be non-empty")
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("api_key must be a non-empty string")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.structured_mode not in {"json_schema", "json_object"}:
            raise ValueError("structured_mode must be json_schema or json_object")
        self._descriptor = ModelDescriptor(
            f"openai-compatible/{self.structured_mode}", self.model, self.base_url
        )

    @property
    def descriptor(self) -> ModelDescriptor:
        return self._descriptor

    def _redact(self, message: str) -> str:
        return message.replace(self.api_key, "[REDACTED]")

    def _sdk_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            api_key = self.api_key
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("OpenAI-compatible calls require the optional openai SDK") from exc
            try:
                client = OpenAI(
                    api_key=api_key,
                    base_url=self.base_url,
                    timeout=self.timeout_seconds,
                    max_retries=0,
                )
            except Exception as exc:
                raise RuntimeError(self._redact(str(exc))) from None
            self._client = client
            return client

    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.descriptor != self.descriptor:
            raise ModelContractError("request descriptor differs from configured provider")
        api_key = self.api_key
        client = self._sdk_client()
        messages = list(request.messages)
        if self.structured_mode == "json_object":
            schema_text = json.dumps(request.schema, ensure_ascii=False, sort_keys=True, indent=2)
            messages.insert(
                0,
                {
                    "role": "system",
                    "content": (
                        "Return one valid JSON object only (JSON output). The response must satisfy this JSON Schema:\n"
                        f"{schema_text}\n"
                        'Example JSON object: {"records": []}'
                    ),
                },
            )
            response_format = {"type": "json_object"}
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "financial_extraction",
                    "strict": True,
                    "schema": request.schema,
                },
            }
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=request.max_output_tokens,
                response_format=response_format,
            )
        except Exception as exc:
            if isinstance(exc, UnknownRequestError):
                raise UnknownRequestError(self._redact(str(exc))) from None
            message = str(exc).replace(api_key, "[REDACTED]")
            if isinstance(exc, _unknown_transport_types()):
                raise UnknownRequestError(message) from None
            raise RuntimeError(message) from None
        if not response.choices:
            raise RuntimeError("provider returned no choices")
        choice = response.choices[0]
        if choice.finish_reason != "stop":
            raise RuntimeError(f"provider response incomplete: finish_reason={choice.finish_reason!r}")
        if choice.message.refusal:
            raise RuntimeError("provider refused the extraction request")
        if not isinstance(choice.message.content, str) or not choice.message.content.strip():
            raise RuntimeError("provider returned empty or missing JSON content")
        usage = response.usage
        raw_response = response.model_dump(exclude_none=True)
        return ModelResponse(
            raw_json=choice.message.content,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            finish_reason=choice.finish_reason,
            raw_response=raw_response,
        )

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def __enter__(self) -> OpenAICompatibleClient:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()
