"""Phase 4 production-gate — mailbox credentials use a DEDICATED encryption key (not SECRET_KEY).

CREDENTIAL_ENCRYPTION_KEYS: the first key encrypts; the rest are decrypt-only rotation fallbacks; the legacy
SECRET_KEY is a final decrypt-only fallback so pre-migration ciphertext still reads until re-wrapped. Verifies:
no plaintext in the DB, new writes use the dedicated current key, an old dedicated key still decrypts during
rotation, SECRET_KEY-era ciphertext migrates, missing key fails closed in production, a wrong key never
destroys data, repeated migration is a no-op, and no secret/key is ever logged.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import outreach as OUT
from app.models import MailAccount, User

SECRET = "hunter2-not-in-db-please"


def _fernet(key):
    return OUT._fernet_from_secret(key)


def test_round_trip_and_not_plaintext(monkeypatch):
    monkeypatch.setattr(OUT, "CREDENTIAL_ENCRYPTION_KEYS", ["dedicated-key-A"])
    tok = OUT.mail_encrypt(SECRET)
    assert tok and tok != SECRET and SECRET not in tok
    assert OUT.mail_decrypt(tok) == SECRET
    assert OUT.mail_encrypt("") == "" and OUT.mail_decrypt("") == ""


def test_new_writes_use_dedicated_current_key(monkeypatch):
    monkeypatch.setattr(OUT, "CREDENTIAL_ENCRYPTION_KEYS", ["dedicated-key-A"])
    tok = OUT.mail_encrypt(SECRET)
    assert _fernet("dedicated-key-A").decrypt(tok.encode()).decode() == SECRET      # current key opens it
    with pytest.raises(Exception):
        _fernet(OUT.SECRET_KEY).decrypt(tok.encode())                              # SECRET_KEY does NOT


def test_db_column_holds_no_plaintext(tmp_path, monkeypatch):
    monkeypatch.setattr(OUT, "CREDENTIAL_ENCRYPTION_KEYS", ["dedicated-key-A"])
    engine = create_engine(f"sqlite:///{tmp_path/'c.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(User(email="a@t.local", name="A", role="admin", active=True, password_hash="x")); s.commit()
        uid = s.exec(select(User.id)).first()
        s.add(MailAccount(user_id=uid, email="m@go4it.vip", admin_owned=True,
                          smtp_password_enc=OUT.mail_encrypt(SECRET),
                          imap_password_enc=OUT.mail_encrypt(SECRET))); s.commit()
    with engine.connect() as conn:
        raw = conn.execute(text("SELECT smtp_password_enc, imap_password_enc FROM mailaccount")).fetchone()
    assert SECRET not in (raw[0] or "") and SECRET not in (raw[1] or "")
    assert OUT.mail_decrypt(raw[0]) == SECRET and OUT.mail_decrypt(raw[1]) == SECRET


def test_old_dedicated_key_decrypts_during_rotation(monkeypatch):
    # value written under the OLD dedicated key...
    monkeypatch.setattr(OUT, "CREDENTIAL_ENCRYPTION_KEYS", ["old-key"])
    tok = OUT.mail_encrypt(SECRET)
    # ...after rotation the config lists NEW first, OLD as a decrypt-only fallback
    monkeypatch.setattr(OUT, "CREDENTIAL_ENCRYPTION_KEYS", ["new-key", "old-key"])
    assert OUT.mail_decrypt(tok) == SECRET                                          # still readable
    assert _fernet("new-key").decrypt(OUT.mail_encrypt(SECRET).encode()).decode() == SECRET  # new writes use new


def test_missing_key_fails_closed_in_production(monkeypatch):
    monkeypatch.setattr(OUT, "CREDENTIAL_ENCRYPTION_KEYS", [])
    monkeypatch.setattr("app.config.IS_LOCAL", False)                               # public deployment
    ok, why = OUT._encryption_key_ok()
    assert ok is False and "credential" in why.lower()
    with pytest.raises(RuntimeError):
        OUT.mail_encrypt("x")
    with pytest.raises(RuntimeError):
        OUT.mail_decrypt("anything")


def test_wrong_key_does_not_destroy_data(tmp_path, monkeypatch):
    import scripts.encrypt_credentials as MIG
    engine = create_engine(f"sqlite:///{tmp_path/'w.db'}", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    # store a token under a key that is NOT in the config the migration will use
    monkeypatch.setattr(OUT, "CREDENTIAL_ENCRYPTION_KEYS", ["stranger-key"])
    monkeypatch.setattr(OUT, "SECRET_KEY", "unrelated-secret")
    foreign = OUT.mail_encrypt(SECRET)
    with Session(engine) as s:
        s.add(User(email="a@t.local", name="A", role="admin", active=True, password_hash="x")); s.commit()
        uid = s.exec(select(User.id)).first()
        s.add(MailAccount(user_id=uid, email="m@go4it.vip", admin_owned=True,
                          smtp_password_enc=foreign)); s.commit()
    # migration runs with a DIFFERENT key set → cannot open the token → must leave it UNTOUCHED
    monkeypatch.setattr(OUT, "CREDENTIAL_ENCRYPTION_KEYS", ["current-key"])
    monkeypatch.setattr(OUT, "SECRET_KEY", "current-secret")
    monkeypatch.setattr(MIG, "engine", engine); monkeypatch.setattr(MIG, "init_db", lambda: None)
    res = MIG.rewrap(apply=True)
    assert res["unreadable"] == 1 and res["rewrapped"] == 0
    with Session(engine) as s:
        assert s.exec(select(MailAccount)).one().smtp_password_enc == foreign       # not nulled/destroyed


def test_secret_key_ciphertext_migrates_and_is_idempotent(tmp_path, monkeypatch):
    import scripts.encrypt_credentials as MIG
    engine = create_engine(f"sqlite:///{tmp_path/'m.db'}", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    # legacy: encrypted under SECRET_KEY (no dedicated key configured yet, localhost)
    monkeypatch.setattr(OUT, "CREDENTIAL_ENCRYPTION_KEYS", [])
    monkeypatch.setattr(OUT, "SECRET_KEY", "legacy-secret-key")
    monkeypatch.setattr("app.config.IS_LOCAL", True)
    legacy_tok = OUT.mail_encrypt(SECRET)
    with Session(engine) as s:
        s.add(User(email="a@t.local", name="A", role="admin", active=True, password_hash="x")); s.commit()
        uid = s.exec(select(User.id)).first()
        s.add(MailAccount(user_id=uid, email="m@go4it.vip", admin_owned=True,
                          smtp_password_enc=legacy_tok)); s.commit()
    # now a DEDICATED key is configured; SECRET_KEY stays as the decrypt-only fallback
    monkeypatch.setattr(OUT, "CREDENTIAL_ENCRYPTION_KEYS", ["dedicated-current"])
    monkeypatch.setattr(MIG, "engine", engine); monkeypatch.setattr(MIG, "init_db", lambda: None)
    assert OUT.mail_decrypt(legacy_tok) == SECRET                                   # legacy still readable
    res = MIG.rewrap(apply=True)
    assert res["rewrapped"] == 1
    with Session(engine) as s:
        m = s.exec(select(MailAccount)).one()
        assert m.smtp_password_enc != legacy_tok
        assert _fernet("dedicated-current").decrypt(m.smtp_password_enc.encode()).decode() == SECRET
    again = MIG.rewrap(apply=True)
    assert again["rewrapped"] == 0 and again["already_current"] == 1                # idempotent


def test_no_secrets_in_logs_or_errors(capsys, monkeypatch):
    monkeypatch.setattr(OUT, "CREDENTIAL_ENCRYPTION_KEYS", ["dedicated-key-A"])
    tok = OUT.mail_encrypt(SECRET)
    # a decrypt of a foreign token returns "" and raises nothing that carries the secret
    assert OUT.mail_decrypt("<not a valid token>") == ""
    out = capsys.readouterr().out + capsys.readouterr().err
    assert SECRET not in out and "dedicated-key-A" not in out
