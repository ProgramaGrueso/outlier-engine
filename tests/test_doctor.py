from types import SimpleNamespace

import httpx
from typer.testing import CliRunner

from engine import db, doctor
from engine.cli import app


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_env_missing_keys(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("")
    monkeypatch.setattr(doctor, "ENV_FILE", env)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    doctor.get_settings.cache_clear()
    c = doctor.check_env()
    assert not c.ok and "ANTHROPIC_API_KEY" in c.detail


def test_env_file_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "ENV_FILE", tmp_path / "nope")
    assert not doctor.check_env().ok


def test_db_ok():
    assert doctor.check_db().ok


def test_ollama_down(monkeypatch):
    def boom(*a, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(doctor.httpx, "get", boom)
    assert not doctor.check_ollama().ok


def test_ollama_missing_role_model(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL_WRITE", "qwen3:14b")
    doctor.get_settings.cache_clear()
    monkeypatch.setattr(doctor.httpx, "get", lambda *a, **kw: FakeResponse({"models": [{"name": "qwen2.5:7b"}]}))
    c = doctor.check_ollama()
    assert not c.ok and "qwen3:14b" in c.detail


def test_youtube_ok_records_usage(monkeypatch):
    fake_yt = SimpleNamespace(
        videos=lambda: SimpleNamespace(list=lambda **kw: SimpleNamespace(execute=lambda: {"items": []}))
    )
    monkeypatch.setattr(doctor, "build", lambda *a, **kw: fake_yt)
    c = doctor.check_youtube()
    assert c.ok and "1/10000" in c.detail
    assert db.api_units_today(db.get_conn()) == 1


def test_youtube_bad_key(monkeypatch):
    def bad(*a, **kw):
        raise RuntimeError("API key not valid")

    monkeypatch.setattr(doctor, "build", bad)
    assert not doctor.check_youtube().ok
    assert db.api_units_today(db.get_conn()) == 0


def test_anthropic_bad_key(monkeypatch):
    class FakeClient:
        def __init__(self, api_key):
            self.models = SimpleNamespace(retrieve=self._fail)

        def _fail(self, model):
            raise RuntimeError("authentication_error")

    monkeypatch.setattr(doctor.anthropic, "Anthropic", FakeClient)
    assert not doctor.check_anthropic().ok


def test_cli_exit_code(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr(doctor, "ALL_CHECKS", [lambda: doctor.Check("a", True, "")])
    assert runner.invoke(app, ["doctor"]).exit_code == 0
    monkeypatch.setattr(doctor, "ALL_CHECKS", [lambda: doctor.Check("a", False, "")])
    assert runner.invoke(app, ["doctor"]).exit_code == 1
