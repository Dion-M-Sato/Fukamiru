"""Configuration follows this checkout unless explicitly overridden."""

from pathlib import Path

from fukamiru.config import default_config_path, load_settings, validate_settings

ROOT = Path(__file__).resolve().parents[1]


def test_default_config_follows_checkout_not_current_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("FUKAMIRU_CONFIG", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "different-user"))
    monkeypatch.chdir(tmp_path)

    assert default_config_path() == ROOT / "config/local.toml"


def test_explicit_config_override(monkeypatch, tmp_path):
    config = tmp_path / "custom.toml"
    monkeypatch.setenv("FUKAMIRU_CONFIG", str(config))
    assert default_config_path() == config


def test_example_paths_do_not_depend_on_current_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings = load_settings(ROOT / "config/local.example.toml")

    assert settings.paths.state_db == ROOT / "data/state.db"
    assert settings.paths.knowledge_db == ROOT / "data/knowledge.db"
    assert settings.paths.state_db != settings.paths.knowledge_db
    assert settings.paths.runtime_root == ROOT / "runtime"
    assert settings.retrieval.embedding_cache_dir == ROOT / "runtime/cache/huggingface"
    assert not settings.retrieval.semantic_enabled
    assert not settings.retrieval.require_cuda
    assert not validate_settings(settings)


def test_model_and_cache_paths_resolve_from_checkout(tmp_path):
    config = tmp_path / "custom.toml"
    example = (ROOT / "config/local.example.toml").read_text(encoding="utf-8")
    example = example.replace('embedding_cache_dir = ""', 'embedding_cache_dir = "models/cache"')
    example = example.replace('path = ""', 'path = "models/local.gguf"', 1)
    config.write_text(example, encoding="utf-8")

    settings = load_settings(config)
    assert settings.llm_jp.path == ROOT / "models/local.gguf"
    assert settings.retrieval.embedding_cache_dir == ROOT / "models/cache"
