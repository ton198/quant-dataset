from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from cli.main import main
from financial_extraction.domain import ExtractionRun
from financial_extraction.runtime import ExtractionConfigError, load_extraction_config


def _config_text(*, model: str = "offline-model") -> str:
    return f'''[provider]
base_url = "https://provider.invalid/v1"
model = "{model}"
timeout_seconds = 9
structured_mode = "json_object"

[extraction]
max_request_bytes = 4321
max_output_tokens = 987
workers = 32
[numeric_policies.eu]
decimal_separator = ","
group_separator = "."
allow_parentheses_negative = true
'''


def test_config_loads_provider_limits_and_explicit_numeric_policy(tmp_path: Path):
    path = tmp_path / "extraction.toml"
    path.write_text(_config_text(), encoding="utf-8")
    (tmp_path / "secrets.toml").write_text('[secrets]\napi_key="test-only-placeholder"\n')

    config = load_extraction_config(path)

    assert config.base_url == "https://provider.invalid/v1"
    assert config.model == "offline-model"
    assert config.api_key == "test-only-placeholder"
    assert config.timeout_seconds == 9
    assert config.limits.max_request_bytes == 4321
    assert config.limits.max_output_tokens == 987
    assert config.workers == 32
    assert config.numeric_policies[0].policy_id == "eu"

def test_config_fails_closed_when_provider_or_policy_is_malformed(tmp_path: Path):
    path = tmp_path / "bad.toml"
    path.write_text('[provider]\nmodel="m"\n', encoding="utf-8")
    with pytest.raises(ExtractionConfigError, match="provider.*extraction"):
        load_extraction_config(path)

    path.write_text(
        _config_text().replace('decimal_separator = ","', 'decimal_separator = ".."'),
        encoding="utf-8",
    )
    with pytest.raises(ExtractionConfigError, match="numeric policy"):
        load_extraction_config(path)

    path.write_text(
        _config_text().replace('structured_mode = "json_object"', 'structured_mode = "guess"'),
        encoding="utf-8",
    )
    with pytest.raises(ExtractionConfigError, match="structured_mode"):
        load_extraction_config(path)

    for bad_workers in ("workers = 0", "workers = -4", 'workers = "32"', "workers = true"):
        path.write_text(_config_text().replace("workers = 32", bad_workers), encoding="utf-8")
        with pytest.raises(ExtractionConfigError, match="workers"):
            load_extraction_config(path)


def test_cli_uses_config_and_writes_non_publishable_run_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import financial_extraction.runtime as runtime
    import filings.extraction as extraction

    config = tmp_path / "config.toml"
    config.write_text(_config_text(), encoding="utf-8")
    secrets = tmp_path / "secrets.toml"
    secrets.write_text('[secrets]\napi_key="test-only-placeholder"\n')
    client_args = {}
    call_args = {}

    class FakeClient:
        def __init__(self, base_url, model, api_key, *, timeout_seconds, structured_mode):
            client_args.update(
                base_url=base_url,
                model=model,
                timeout_seconds=timeout_seconds,
                structured_mode=structured_mode,
                api_key=api_key,
            )

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            pass
    def fake_extract(archive, filing_ids, **kwargs):
        call_args.update(archive=archive, filing_ids=filing_ids, **kwargs)
        return ExtractionRun("run-1", "complete", (), (), ())

    monkeypatch.setattr(runtime, "OpenAICompatibleClient", FakeClient)
    monkeypatch.setattr(extraction, "extract_selected_filings", fake_extract)
    work = tmp_path / "private-work"
    result = main(
        [
            "filings", "extract-financials", "--archive", str(tmp_path / "archive"),
            "--work-dir", str(work), "--config", str(config), "--filing-id", "frozen-1",
            "--secrets", str(secrets),
        ]
    )

    assert result == 0
    assert client_args == {
        "base_url": "https://provider.invalid/v1",
        "model": "offline-model",
        "timeout_seconds": 9.0,
        "structured_mode": "json_object",
        "api_key": "test-only-placeholder",
    }
    assert call_args["filing_ids"] == ("frozen-1",)
    assert call_args["task"].numeric_policies[0].policy_id == "eu"
    assert call_args["workers"] == 32
    report = json.loads((work / "result.json").read_text(encoding="utf-8"))
    assert report["publishable"] is False
    summary = json.loads(capsys.readouterr().out)
    original_result = (work / "result.json").read_bytes()
    second_result = main(
        [
            "filings", "extract-financials", "--archive", str(tmp_path / "archive"),
            "--work-dir", str(work), "--config", str(config), "--filing-id", "frozen-1",
            "--secrets", str(secrets),
        ]
    )
    assert second_result == 0
    assert (work / "result.json").read_bytes() == original_result
    assert summary["result_path"] == str(work / "result.json")


def test_deepseek_provider_uses_json_object_and_rejects_empty_content(
    monkeypatch: pytest.MonkeyPatch,
):
    import types

    from financial_extraction.runtime import ModelRequest, OpenAICompatibleClient

    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=self.create)
            )

        def close(self):
            pass

        def create(self, **kwargs):
            captured["request"] = kwargs
            choice = types.SimpleNamespace(
                finish_reason="stop",
                message=types.SimpleNamespace(content="", refusal=None),
            )
            return types.SimpleNamespace(
                choices=[choice],
                usage=None,
                model_dump=lambda **kwargs: {"choices": []},
            )

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))
    client = OpenAICompatibleClient(
        "https://api.deepseek.com",
        "deepseek-flash",
        "placeholder-only",
        structured_mode="json_object",
    )
    request = ModelRequest(
        descriptor=client.descriptor,
        messages=({"role": "user", "content": "Extract JSON."},),
        schema={"type": "object", "properties": {"records": {"type": "array"}}},
        max_output_tokens=500,
    )

    with pytest.raises(RuntimeError, match="empty.*JSON"):
        client.complete(request)

    sent = captured["request"]
    assert sent["messages"][0]["role"] == "system"
    assert sent["response_format"] == {"type": "json_object"}
    assert "strict" not in str(sent["response_format"])
    assert "JSON Schema" in sent["messages"][0]["content"]
    assert '"records": []' in sent["messages"][0]["content"]


def test_cli_rejects_missing_config_without_reading_any_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    result = main(
        [
            "filings", "extract-financials", "--archive", str(tmp_path / "archive"),
            "--work-dir", str(tmp_path / "work"), "--config", str(tmp_path / "missing.toml"),
            "--filing-id", "frozen-1",
        ]
    )
    assert result == 2
    assert "extraction config" in capsys.readouterr().err
