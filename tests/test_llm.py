from types import SimpleNamespace

import httpx
import pytest

from engine import db, llm


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _rows():
    return db.get_conn().execute("SELECT * FROM llm_calls ORDER BY id").fetchall()


def test_cheap_uses_role_model_and_logs(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL_WRITE", "llama3.1:8b")
    llm.get_settings.cache_clear()
    sent = {}

    def fake_post(url, json, timeout):
        sent.update(url=url, payload=json)
        return FakeResponse({"message": {"content": '{"a": 1}'}, "prompt_eval_count": 12, "eval_count": 5})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    res = llm.complete("hola", task="guionista", role="write", json_mode=True)

    assert res.text == '{"a": 1}'
    assert sent["url"] == "http://ollama.test/api/chat"
    assert sent["payload"]["model"] == "llama3.1:8b"
    assert sent["payload"]["format"] == "json"
    (row,) = _rows()
    assert (row["tier"], row["model"], row["task"], row["tokens_in"], row["tokens_out"], row["ok"]) == (
        "cheap", "llama3.1:8b", "guionista", 12, 5, 1,
    )


def test_role_falls_back_to_default_model(settings):
    assert settings.ollama_model_for("critic") == "qwen2.5:7b"


def test_premium_logs_tokens(monkeypatch):
    msg = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="veredicto")],
        usage=SimpleNamespace(input_tokens=300, output_tokens=80),
    )
    captured = {}

    class FakeClient:
        def __init__(self, api_key):
            self.messages = SimpleNamespace(create=lambda **kw: captured.update(kw) or msg)

    monkeypatch.setattr(llm.anthropic, "Anthropic", FakeClient)
    res = llm.complete("elige uno", task="juez", tier="premium", system="eres juez")

    assert res.text == "veredicto"
    assert captured["model"] == "claude-sonnet-5"
    assert captured["system"] == "eres juez"
    (row,) = _rows()
    assert (row["tier"], row["model"], row["tokens_in"], row["tokens_out"]) == ("premium", "claude-sonnet-5", 300, 80)


def test_failed_call_is_logged(monkeypatch):
    def boom(*a, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(llm.httpx, "post", boom)
    with pytest.raises(httpx.ConnectError):
        llm.complete("x", task="analyze")
    (row,) = _rows()
    assert row["ok"] == 0 and "down" in row["error"]


def test_load_prompt_keeps_json_braces(tmp_path, monkeypatch):
    (tmp_path / "t.md").write_text('Formato: {{formato}}\n{"hook_2s": "..."}')
    monkeypatch.setattr(llm, "PROMPTS_DIR", tmp_path)
    assert llm.load_prompt("t", formato="POV") == 'Formato: POV\n{"hook_2s": "..."}'


def test_unload_ollama_all_loaded(monkeypatch):
    monkeypatch.setattr(llm.httpx, "get", lambda url, timeout: FakeResponse({"models": [{"name": "qwen2.5:7b"}]}))
    posts = []
    monkeypatch.setattr(llm.httpx, "post", lambda url, json, timeout: posts.append((url, json)) or FakeResponse({}))

    assert llm.unload_ollama() == ["qwen2.5:7b"]
    assert posts == [("http://ollama.test/api/generate", {"model": "qwen2.5:7b", "keep_alive": 0})]
