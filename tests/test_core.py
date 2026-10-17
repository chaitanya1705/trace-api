import uuid

import pytest

from app.auth.authz import Cap, can_invite_role, caps_for, effective_caps
from app.config import ConfigError, Settings, load_settings
from app.storage.secrets import Sealed, Vault, VaultError, generate_key, mask


def test_production_requires_named_settings(monkeypatch):
    for k in ("DATABASE_URL", "TRACE_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TRACE_ENV", "production")
    with pytest.raises(ConfigError) as e:
        load_settings()
    assert "DATABASE_URL" in str(e.value) and "TRACE_SECRET_KEY" in str(e.value)


def test_bad_secret_key_error_names_field_not_value(monkeypatch):
    monkeypatch.setenv("TRACE_SECRET_KEY", "super-secret-not-base64-value!!")
    with pytest.raises(ConfigError) as e:
        load_settings()
    assert "trace_secret_key" in str(e.value) and "super-secret" not in str(e.value)


def test_vault_roundtrip_and_binding():
    s = Settings(trace_env="test", trace_secret_key=generate_key())
    v = Vault.from_settings(s)
    sealed = v.seal("ghp_abc123456789xyz", connection_id="c1", version=1, kind="pat")
    assert b"ghp_" not in sealed.ciphertext
    assert v.open(sealed, connection_id="c1", version=1, kind="pat") == "ghp_abc123456789xyz"
    with pytest.raises(VaultError):  # ciphertext moved to another connection fails authentication
        v.open(sealed, connection_id="c2", version=1, kind="pat")
    with pytest.raises(VaultError):
        v.open(Sealed(9, sealed.nonce, sealed.ciphertext), connection_id="c1", version=1, kind="pat")


def test_vault_rotation_keeps_old_ciphertext_readable():
    old, new = generate_key(), generate_key()
    v1 = Vault.from_settings(Settings(trace_env="test", trace_secret_key=old, trace_secret_key_version=1))
    sealed = v1.seal("x" * 20, connection_id="c", version=1, kind="t")
    v2 = Vault.from_settings(Settings(trace_env="test", trace_secret_key=new, trace_secret_key_version=2,
                                      trace_secret_keys_previous=f"1:{old}"))
    assert v2.open(sealed, connection_id="c", version=1, kind="t") == "x" * 20
    assert v2.seal("y" * 20, connection_id="c", version=2, kind="t").key_version == 2


def test_mask_never_reveals_short_secrets():
    assert mask("short") == "…" and mask("abcdefghijklmnop").endswith("mnop")


def test_roles_are_not_a_ladder():
    assert Cap.APPROVE not in caps_for(["admin"])           # admin != eligible approver
    assert Cap.MANAGE_CONNECTIONS not in caps_for(["approver"])
    assert Cap.INVESTIGATE not in caps_for(["viewer"])
    assert {Cap.APPROVE, Cap.INVESTIGATE} <= caps_for(["approver", "investigator"])


def test_environment_scoped_grants():
    e1, e2 = str(uuid.uuid4()), str(uuid.uuid4())
    grants = [("investigator", [e1])]
    assert Cap.INVESTIGATE in effective_caps(grants, e1)
    assert Cap.INVESTIGATE not in effective_caps(grants, e2)
    assert not effective_caps(grants, None)                  # env-limited grant never covers project-wide actions
    assert Cap.READ in effective_caps([("viewer", None)], None)


def test_invite_rules():
    assert can_invite_role("owner", "owner") and not can_invite_role("admin", "owner")
    assert can_invite_role("admin", "viewer") and not can_invite_role("investigator", "viewer")
