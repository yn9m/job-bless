from src.config import Config


def test_config_load_defaults():
    cfg = Config.load()
    assert cfg.db.driver in ("sqlite", "postgres")
    assert cfg.browser.provider in ("local_process", "docker", "external")
