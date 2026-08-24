"""Phase 4 hardening — mailbox credentials are encrypted at rest, fail-closed, migratable, rotatable.

Verifies: reversible authenticated encryption; the raw DB column never holds the plaintext; fail-closed on a
public deployment still using the default SECRET_KEY; the migration detects/encrypts planted plaintext and is
idempotent; key rotation re-keys tokens and the old key stops working. Secrets are never asserted into logs.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import outreach as OUT
from app.models import MailAccount, User

SECRET = "hunter2-not-in-db-please"


def test_round_trip_and_not_plaintext():
    tok = OUT.mail_encrypt(SECRET)
    assert tok and tok != SECRET and SECRET not in tok          # ciphertext hides the secret
    assert OUT.mail_decrypt(tok) == SECRET                       # reversible
    assert OUT.mail_encrypt("") == "" and OUT.mail_decrypt("") == ""


def test_db_column_holds_no_plaintext(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'c.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(User(email="a@t.local", name="A", role="admin", active=True, password_hash="x")); s.commit()
        uid = s.exec(select(User.id)).first()
        s.add(MailAccount(user_id=uid, email="m@go4it.vip", admin_owned=True,
                          smtp_password_enc=OUT.mail_encrypt(SECRET),
                          imap_password_enc=OUT.mail_encrypt(SECRET))); s.commit()
    # read the raw bytes straight from sqlite — the plaintext must appear nowhere
    with engine.connect() as conn:
        raw = conn.execute(text("SELECT smtp_password_enc, imap_password_enc FROM mailaccount")).fetchone()
    assert SECRET not in (raw[0] or "") and SECRET not in (raw[1] or "")
    assert OUT.mail_decrypt(raw[0]) == SECRET and OUT.mail_decrypt(raw[1]) == SECRET


def test_fail_closed_on_public_deploy_with_default_key(monkeypatch):
    monkeypatch.setattr("app.config.IS_LOCAL", False)               # simulate a public deployment
    monkeypatch.setattr(OUT, "SECRET_KEY", "dev-insecure-change-me")  # shipped default
    monkeypatch.setattr("app.config.SECRET_KEY", "dev-insecure-change-me")
    ok, why = OUT._encryption_key_ok()
    assert ok is False and "default" in why.lower()
    with pytest.raises(RuntimeError):
        OUT.mail_encrypt("x")                                       # refuses rather than use a forgeable key


def test_migration_detects_and_encrypts_plaintext(tmp_path, monkeypatch):
    import scripts.encrypt_credentials as MIG
    engine = create_engine(f"sqlite:///{tmp_path/'m.db'}", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(MIG, "engine", engine)
    monkeypatch.setattr(MIG, "init_db", lambda: None)
    with Session(engine) as s:
        s.add(User(email="a@t.local", name="A", role="admin", active=True, password_hash="x")); s.commit()
        uid = s.exec(select(User.id)).first()
        # plant one PLAINTEXT credential (legacy) + one already-encrypted
        s.add(MailAccount(user_id=uid, email="legacy@go4it.vip", admin_owned=True,
                          smtp_password_enc="PLAINTEXT-legacy-pw"))
        s.add(MailAccount(user_id=uid, email="ok@go4it.vip", admin_owned=True,
                          smtp_password_enc=OUT.mail_encrypt(SECRET))); s.commit()
    dry = MIG.scan(apply=False)
    assert dry["plaintext"] == 1 and dry["migrated"] == 0          # detected, nothing changed
    with engine.connect() as conn:
        assert conn.execute(text("SELECT smtp_password_enc FROM mailaccount WHERE email='legacy@go4it.vip'")
                            ).fetchone()[0] == "PLAINTEXT-legacy-pw"
    applied = MIG.scan(apply=True)
    assert applied["migrated"] == 1
    again = MIG.scan(apply=True)
    assert again["plaintext"] == 0 and again["migrated"] == 0       # idempotent
    with Session(engine) as s:
        m = s.exec(select(MailAccount).where(MailAccount.email == "legacy@go4it.vip")).one()
        assert m.smtp_password_enc != "PLAINTEXT-legacy-pw"
        assert OUT.mail_decrypt(m.smtp_password_enc) == "PLAINTEXT-legacy-pw"   # value preserved, now encrypted


def test_key_rotation_rekeys_and_old_key_stops_working(tmp_path, monkeypatch):
    import scripts.encrypt_credentials as MIG
    old_key, new_key = "OLD-app-key", "NEW-app-key"
    engine = create_engine(f"sqlite:///{tmp_path/'r.db'}", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    # store a token under the OLD key
    monkeypatch.setattr(OUT, "SECRET_KEY", old_key)
    tok_old = OUT.mail_encrypt(SECRET)
    with Session(engine) as s:
        s.add(User(email="a@t.local", name="A", role="admin", active=True, password_hash="x")); s.commit()
        uid = s.exec(select(User.id)).first()
        s.add(MailAccount(user_id=uid, email="rot@go4it.vip", admin_owned=True,
                          smtp_password_enc=tok_old)); s.commit()
    # now the app runs under the NEW key; rotate old->new
    monkeypatch.setattr(OUT, "SECRET_KEY", new_key)
    monkeypatch.setattr(MIG, "SECRET_KEY", new_key)
    monkeypatch.setattr(MIG, "engine", engine)
    monkeypatch.setattr(MIG, "init_db", lambda: None)
    monkeypatch.setenv("GO4IT_OLD_SECRET_KEY", old_key)
    res = MIG.rotate(apply=True)
    assert res["rotated"] == 1
    with Session(engine) as s:
        m = s.exec(select(MailAccount).where(MailAccount.email == "rot@go4it.vip")).one()
        assert OUT.mail_decrypt(m.smtp_password_enc) == SECRET       # decrypts under the NEW key
        assert m.smtp_password_enc != tok_old                        # token actually changed
        assert OUT._fernet_from_secret(old_key).decrypt.__self__ is not None  # old cipher exists...
        with pytest.raises(Exception):
            OUT._fernet_from_secret(old_key).decrypt(m.smtp_password_enc.encode())  # ...but no longer opens it
