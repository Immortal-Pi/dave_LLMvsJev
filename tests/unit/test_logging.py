from dave_agent.logging_setup import REDACTED, redact


def test_env_secret_redacted(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "abcd1234secretvalue")
    assert redact("key=abcd1234secretvalue") == f"key={REDACTED}"


def test_bearer_redacted():
    assert redact("Authorization: Bearer xyz.123-abc") == f"Authorization: Bearer {REDACTED}"
