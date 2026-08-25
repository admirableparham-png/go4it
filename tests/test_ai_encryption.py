"""Phase 9 (A) — dedicated AI content encryption: round-trip, rotation (previous key still decrypts), secret
redaction before persistence, and separation from SECRET_KEY / credential keys."""
import importlib


def _reload(monkeypatch, keys):
    import app.config
    monkeypatch.setattr(app.config, "AI_DATA_ENCRYPTION_KEYS", keys, raising=False)
    import app.ai_encryption as E
    importlib.reload(E)
    return E


def test_roundtrip_and_ciphertext_opaque(monkeypatch):
    E = _reload(monkeypatch, ["k-primary-strong-0001"])
    tok = E.ai_encrypt("buyer ACME wants 100 tonnes of zinc")
    assert tok and "ACME" not in tok and "zinc" not in tok
    assert E.ai_decrypt(tok) == "buyer ACME wants 100 tonnes of zinc"
    assert E.ai_encrypt("") == "" and E.ai_decrypt("") == ""


def test_rotation_previous_key_still_decrypts(monkeypatch):
    E = _reload(monkeypatch, ["old-key-strong-0001"])
    tok = E.ai_encrypt("secret sales note")
    # rotate: new key first, old key retained as decrypt-only fallback
    E2 = _reload(monkeypatch, ["new-key-strong-0002", "old-key-strong-0001"])
    assert E2.ai_decrypt(tok) == "secret sales note"           # old ciphertext still readable
    fresh = E2.ai_encrypt("new note")
    # after full rotation (old key dropped) the OLD token no longer decrypts, but the new one does
    E3 = _reload(monkeypatch, ["new-key-strong-0002"])
    assert E3.ai_decrypt(fresh) == "new note"
    assert E3.ai_decrypt(tok) == ""                            # unrecoverable once its key is gone


def test_secret_redaction_before_persistence(monkeypatch):
    E = _reload(monkeypatch, ["k-strong-0001"])
    dirty = "here is my api_key=sk-live-ABCDEF1234567890 and password: hunter2 and a BEGIN PRIVATE KEY blob"
    clean = E.redact_secrets(dirty)
    assert "sk-live-ABCDEF1234567890" not in clean and "hunter2" not in clean
    assert E.contains_secret("AI_API_KEY=sk-abcdef1234567890ghijkl") is True
    assert E.contains_secret("what needs my attention today?") is False


def test_keys_are_separate_from_secret_and_credentials(monkeypatch):
    # AI keys resolve from AI_DATA_ENCRYPTION_KEYS, not SECRET_KEY / CREDENTIAL_ENCRYPTION_KEYS
    import app.config
    monkeypatch.setattr(app.config, "AI_DATA_ENCRYPTION_KEYS", ["ai-only-key-0001"], raising=False)
    import app.ai_encryption as E
    importlib.reload(E)
    assert E._keys() == ["ai-only-key-0001"]
