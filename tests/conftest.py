import pytest

from engine.config import get_settings

TEST_ENV = {
    "YOUTUBE_API_KEY": "yt-test",
    "ANTHROPIC_API_KEY": "sk-test",
    "OLLAMA_HOST": "http://ollama.test",
    "OLLAMA_MODEL": "qwen2.5:7b",
    "OLLAMA_MODEL_CLASSIFY": "",
    "OLLAMA_MODEL_WRITE": "",
    "OLLAMA_MODEL_CRITIC": "",
    "OLLAMA_MODEL_SCENES": "",
    "ANTHROPIC_MODEL": "claude-sonnet-5",
    "YT_DAILY_QUOTA": "10000",
}


@pytest.fixture(autouse=True)
def settings(tmp_path, monkeypatch):
    """Aísla cada test: variables de entorno (ganan sobre .env) y DB temporal."""
    for k, v in TEST_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()
