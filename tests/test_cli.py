import os

from ademly import cli, config


def test_env_file_is_loaded(tmp_path, monkeypatch):
    name = "ADEMLY_TEST_ENV_VALUE"
    monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(f"{name}=loaded\n")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ademly.db")
    try:
        assert cli.main(["status"]) == 0
        assert os.environ[name] == "loaded"
    finally:
        os.environ.pop(name, None)
