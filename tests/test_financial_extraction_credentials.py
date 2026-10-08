from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from cli.main import main
from financial_extraction.runtime import (
    ExtractionConfigError,
    ModelRequest,
    OpenAICompatibleClient,
    ReplayStore,
    load_extraction_config,
)

CONFIG = '''[provider]
base_url = "https://provider.invalid"
model = "offline-model"
structured_mode = "json_object"
[extraction]
'''


def _files(tmp_path):
    config = tmp_path / "extraction.toml"
    secrets = tmp_path / "secrets.toml"
    config.write_text(CONFIG, encoding="utf-8")
    secrets.write_text('[secrets]\napi_key="file-test-secret"\n', encoding="utf-8")
    return config, secrets


def test_secrets_key_ignores_environment_and_is_not_persisted(tmp_path, monkeypatch):
    config_path, _ = _files(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-test-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "other-environment-secret")
    config = load_extraction_config(config_path)
    used_keys = []

    class FakeOpenAI:
        def __init__(self, *, api_key, **kwargs):
            used_keys.append(api_key)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def close(self):
            pass

        def create(self, **kwargs):
            assert all(set(m) == {"role", "content"} for m in kwargs["messages"])
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content='{"records": []}', refusal=None),
                )],
                usage=None,
                model_dump=lambda **kwargs: {"choices": []},
            )

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    client = OpenAICompatibleClient(
        config.base_url, config.model, config.api_key,
        structured_mode=config.structured_mode,
    )
    request = ModelRequest(
        client.descriptor, ({"role": "user", "content": "Synthetic JSON only"},),
        {"type": "object"}, 100,
    )
    store = ReplayStore(tmp_path / "work")
    identity, _ = store.begin(request)
    response = client.complete(request)
    store.complete(identity, response)
    assert used_keys == ["file-test-secret"]
    persisted = json.dumps(request.canonical_dict()) + repr(client) + repr(config)
    persisted += repr(client.descriptor) + store.journal.read_text(encoding="utf-8")
    for secret in ("file-test-secret", "environment-test-secret", "other-environment-secret"):
        assert secret not in persisted
    other = OpenAICompatibleClient(
        config.base_url, config.model, "different-test-secret",
        structured_mode=config.structured_mode,
    )
    assert ModelRequest(other.descriptor, request.messages, request.schema, 100).identity == identity


def test_explicit_secrets_path_overrides_sibling_file(tmp_path):
    config_path, _ = _files(tmp_path)
    alternate = tmp_path / "private" / "secrets.toml"
    alternate.parent.mkdir()
    alternate.write_text('[secrets]\napi_key="alternate-test-secret"\n')
    config = load_extraction_config(config_path, secrets_path=alternate)
    assert config.api_key == "alternate-test-secret"


@pytest.mark.parametrize("content", [
    None, '[secrets]\n', '[other]\napi_key="unused"\n',
    '[secrets]\napi_key=""\n', '[secrets]\napi_key="   "\n',
    '[secrets]\napi_key=123\n', '[secrets]\napi_key=false\n',
    '[secrets]\napi_key=[]\n', '[secrets]\napi_key="YOUR_API_KEY"\n',
    '[secrets]\napi_key="unterminated-test-secret\n',
])
def test_invalid_or_missing_secrets_fails_without_env_fallback(tmp_path, monkeypatch, content):
    config_path, secrets = _files(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-test-secret")
    if content is None:
        secrets.unlink()
    else:
        secrets.write_text(content, encoding="utf-8")
    with pytest.raises(ExtractionConfigError) as caught:
        load_extraction_config(config_path)
    assert "environment-test-secret" not in str(caught.value)
    assert "unterminated-test-secret" not in str(caught.value)


@pytest.mark.parametrize("field", ["api_key", "api_key_env"])
def test_legacy_provider_credentials_are_rejected_without_echo(tmp_path, field):
    config_path, _ = _files(tmp_path)
    config_path.write_text(CONFIG.replace('[provider]', f'[provider]\n{field}="misplaced-secret"'))
    with pytest.raises(ExtractionConfigError) as caught:
        load_extraction_config(config_path)
    assert "misplaced-secret" not in str(caught.value)
    assert "secrets.toml" in str(caught.value)


@pytest.mark.parametrize("key", [None, "", "   ", 123])
def test_client_requires_explicit_api_key(key):
    with pytest.raises(ValueError, match="api_key must be a non-empty string"):
        OpenAICompatibleClient("https://provider.invalid", "offline", key)


def test_sdk_error_redacts_file_key(monkeypatch):
    class FailingOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def close(self):
            pass

        def create(self, **kwargs):
            raise RuntimeError("unauthorized file-test-secret")

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FailingOpenAI))
    client = OpenAICompatibleClient("https://provider.invalid", "offline", "file-test-secret")
    request = ModelRequest(client.descriptor, ({"role": "user", "content": "Synthetic"},), {}, 100)
    with pytest.raises(RuntimeError) as caught:
        client.complete(request)
    assert str(caught.value) == "unauthorized [REDACTED]"


def test_cli_secrets_key_reaches_archive_validation_without_network(tmp_path, monkeypatch, capsys):
    config_path, secrets = _files(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code = main([
        "filings", "extract-financials", "--archive", str(tmp_path / "missing-archive"),
        "--work-dir", str(tmp_path / "work"), "--config", str(config_path),
        "--secrets", str(secrets), "--filing-id", "synthetic-frozen-id",
    ])
    assert code == 1
    output = capsys.readouterr()
    assert "financial extraction failed" in output.err
    assert "file-test-secret" not in output.err + output.out


def test_cli_missing_secrets_rejects_before_work_or_archive(tmp_path, monkeypatch, capsys):
    config_path, secrets = _files(tmp_path)
    secrets.unlink()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-test-secret")
    work = tmp_path / "work"
    code = main([
        "filings", "extract-financials", "--archive", str(tmp_path / "missing-archive"),
        "--work-dir", str(work), "--config", str(config_path),
        "--secrets", str(secrets), "--filing-id", "synthetic-frozen-id",
    ])
    assert code == 2
    assert not work.exists()
    output = capsys.readouterr()
    assert "cannot read secrets configuration" in output.err
    assert "environment-test-secret" not in output.err + output.out
