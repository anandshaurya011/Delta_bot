import config, crypto


def test_get_or_create_secret_persists(tmp_path):
    env = tmp_path / ".env"
    a = config.get_or_create_secret("X_KEY", lambda: "v1", env)
    b = config.get_or_create_secret("X_KEY", lambda: "v2", env)
    assert a == b == "v1"
    assert oct(env.stat().st_mode & 0o777) == "0o600"


def test_env_var_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("X_KEY", "fromenv")
    assert config.get_or_create_secret("X_KEY", lambda: "v", tmp_path / ".env") == "fromenv"


def test_roundtrip_and_ciphertext_differs(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("CREDENTIAL_ENC_KEY", Fernet.generate_key().decode())
    token = crypto.encrypt("s3cret")
    assert token != "s3cret" and "s3cret" not in token
    assert crypto.decrypt(token) == "s3cret"
