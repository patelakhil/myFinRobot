from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import stat
import sys
from pathlib import Path
from typing import Any

import keyring.errors
import pytest

import finrobot.secret_store as secret_store_module
from finrobot.secret_store import (
    EncryptedFileSecretStore,
    KeychainSecretStore,
    SecretStoreError,
    create_secret_store,
    default_key_path,
)

_posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")


def _file_store(tmp_path: Path, **kwargs: Any) -> EncryptedFileSecretStore:
    """Store whose data file and key file both live under tmp_path (never ~)."""
    return EncryptedFileSecretStore(
        tmp_path / "home" / ".secrets", key_path=tmp_path / "keydir" / "secrets.key", **kwargs
    )


@pytest.mark.asyncio
async def test_file_secret_store_roundtrip(tmp_path: Path) -> None:
    store = _file_store(tmp_path)
    await store.set("FMP_API_KEY", "abc123")
    assert await store.get("FMP_API_KEY") == "abc123"
    assert await store.has("FMP_API_KEY") is True
    await store.delete("FMP_API_KEY")
    assert await store.get("FMP_API_KEY") is None


@pytest.mark.asyncio
async def test_secrets_survive_a_new_store_instance(tmp_path: Path) -> None:
    """A restart (new instance, same paths) must decrypt what the last one wrote."""
    await _file_store(tmp_path).set("provider_key:openai", "sk-live-1")
    assert await _file_store(tmp_path).get("provider_key:openai") == "sk-live-1"


@_posix_only
@pytest.mark.asyncio
async def test_new_secret_file_is_created_0600(tmp_path: Path) -> None:
    store = _file_store(tmp_path)
    await store.set("k", "v")
    path = tmp_path / "home" / ".secrets"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR


@_posix_only
@pytest.mark.asyncio
async def test_key_file_is_created_0600_in_a_0700_dir(tmp_path: Path) -> None:
    store = _file_store(tmp_path)
    await store.set("k", "v")
    key_path = tmp_path / "keydir" / "secrets.key"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_path.parent.stat().st_mode) == 0o700
    assert len(base64.b64decode(key_path.read_text().strip())) == 32  # AES-256


@_posix_only
@pytest.mark.asyncio
async def test_perms_drift_self_heals_instead_of_crashing(tmp_path: Path) -> None:
    """BUG-083: mode != 0600 must self-heal (chmod back to 0600), not raise.

    Permission drift to a looser mode is a recoverable, real-world event
    (backup/restore, editor rewrite under umask, sync tools). Reading must
    tighten the file back to 0600 and continue rather than crash startup.
    """
    path = tmp_path / "home" / ".secrets"
    store = _file_store(tmp_path)
    await store.set("FMP_API_KEY", "abc123")

    # Simulate external drift: editor rewrite / restored backup leaves 0644.
    path.chmod(0o644)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644

    # Must NOT raise — self-heals and still returns the value.
    value = await store.get("FMP_API_KEY")
    assert value == "abc123"

    # File was tightened back to 0600.
    assert stat.S_IMODE(path.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR


@_posix_only
@pytest.mark.asyncio
async def test_perms_drift_group_world_readable_self_heals(tmp_path: Path) -> None:
    path = tmp_path / "home" / ".secrets"
    key_path = tmp_path / "keydir" / "secrets.key"
    store = _file_store(tmp_path)
    await store.set("k", "v")

    path.chmod(0o666)  # world-writable drift
    key_path.chmod(0o644)
    # A subsequent write path must not crash, and must heal both files.
    await _file_store(tmp_path).set("k2", "v2")

    assert await store.get("k") == "v"
    assert await store.get("k2") == "v2"
    assert stat.S_IMODE(path.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR
    assert stat.S_IMODE(key_path.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR


@_posix_only
@pytest.mark.asyncio
async def test_parent_permission_lock_failure_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets" / ".secrets"
    store = EncryptedFileSecretStore(path, key_path=tmp_path / "keydir" / "secrets.key")
    real_chmod = os.chmod

    def deny_chmod(target: Path | str, mode: int) -> None:
        if Path(target) == path.parent:
            raise OSError("chmod denied")
        real_chmod(target, mode)

    monkeypatch.setattr("finrobot.secret_store.os.chmod", deny_chmod)

    with pytest.raises(SecretStoreError, match="could not be permission-locked"):
        await store.set("k", "v")


# ---------------------------------------------------------------------------
# EncryptedFileSecretStore — encryption at rest.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_secret_name_or_value_is_on_disk_in_the_clear(tmp_path: Path) -> None:
    """The point of the change: nothing greppable (names or `sk-…` values)."""
    store = _file_store(tmp_path)
    await store.set("provider_key:openai", "sk-proj-SUPERSECRET")
    await store.set("fmp_api_key", "fmp-SUPERSECRET")

    raw = (tmp_path / "home" / ".secrets").read_text()
    assert "SUPERSECRET" not in raw
    assert "provider_key" not in raw
    assert "fmp_api_key" not in raw
    envelope = json.loads(raw)
    assert envelope["format"] == "finrobot-secrets"
    assert envelope["version"] == 1
    assert envelope["kdf"] == "keyfile"
    assert set(envelope) == {"format", "version", "kdf", "nonce", "ciphertext"}


@pytest.mark.asyncio
async def test_every_write_uses_a_fresh_nonce(tmp_path: Path) -> None:
    """AES-GCM nonce reuse under one key is catastrophic — never repeat one."""
    store = _file_store(tmp_path)
    path = tmp_path / "home" / ".secrets"
    seen = set()
    for _ in range(5):
        await store.set("k", "same-value")
        envelope = json.loads(path.read_text())
        seen.add((envelope["nonce"], envelope["ciphertext"]))
    assert len(seen) == 5


@pytest.mark.asyncio
async def test_key_file_is_not_stored_next_to_the_data(tmp_path: Path) -> None:
    """Copying the data directory alone must not be enough to decrypt it."""
    store = _file_store(tmp_path)
    await store.set("k", "v")
    assert [p.name for p in (tmp_path / "home").iterdir()] == [".secrets"]


@pytest.mark.asyncio
async def test_concurrent_writes_are_not_lost(tmp_path: Path) -> None:
    store = _file_store(tmp_path)
    await asyncio.gather(*(store.set(f"k{i}", f"v{i}") for i in range(20)))
    fresh = _file_store(tmp_path)
    for i in range(20):
        assert await fresh.get(f"k{i}") == f"v{i}"


@pytest.mark.asyncio
async def test_delete_of_absent_key_does_not_mint_a_key_file(tmp_path: Path) -> None:
    store = _file_store(tmp_path)
    await store.delete("never-set")
    assert await store.get("never-set") is None
    assert not (tmp_path / "keydir" / "secrets.key").exists()


# -- legacy plaintext migration ------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_plaintext_file_is_encrypted_on_first_read(
    tmp_path: Path, caplog: Any
) -> None:
    path = tmp_path / "home" / ".secrets"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"fmp_api_key": "fmp-LEGACY", "provider_key:deepseek": "sk-LEGACY"}))

    store = _file_store(tmp_path)
    with caplog.at_level(logging.WARNING, logger="finrobot.secret_store"):
        # A READ (boot hydration) migrates — no need to wait for a save.
        assert await store.get("fmp_api_key") == "fmp-LEGACY"

    raw = path.read_text()
    assert "LEGACY" not in raw
    assert json.loads(raw)["format"] == "finrobot-secrets"
    assert await _file_store(tmp_path).get("provider_key:deepseek") == "sk-LEGACY"
    assert any("legacy PLAINTEXT" in r.getMessage() for r in caplog.records)
    # The log line reports how many entries migrated — never their values.
    assert all("LEGACY" not in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_legacy_empty_object_file_is_migrated(tmp_path: Path) -> None:
    """The old store seeded new files with `{}` — that must migrate cleanly."""
    path = tmp_path / "home" / ".secrets"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    store = _file_store(tmp_path)
    assert await store.get("anything") is None
    await store.set("k", "v")
    assert json.loads(path.read_text())["format"] == "finrobot-secrets"


# -- unreadable files: degrade on read, move aside on write, never delete -------


async def _tamper(tmp_path: Path, mutate: Any) -> Path:
    store = _file_store(tmp_path)
    await store.set("fmp_api_key", "fmp-ORIGINAL")
    path = tmp_path / "home" / ".secrets"
    envelope = json.loads(path.read_text())
    mutate(envelope)
    path.write_text(json.dumps(envelope))
    return path


def _flip_ciphertext(envelope: dict[str, Any]) -> None:
    ct = bytearray(base64.b64decode(envelope["ciphertext"]))
    ct[0] ^= 0x01
    envelope["ciphertext"] = base64.b64encode(bytes(ct)).decode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_flip_ciphertext, id="ciphertext-bitflip"),
        pytest.param(lambda e: e.pop("nonce"), id="nonce-missing"),
        pytest.param(lambda e: e.pop("ciphertext"), id="ciphertext-missing"),
        pytest.param(lambda e: e.update(version=99), id="unknown-version"),
        pytest.param(lambda e: e.update(kdf="rot13"), id="unknown-kdf"),
        pytest.param(
            lambda e: e.update(kdf="scrypt", salt="AAAA", n=2**30, r=8, p=1),
            id="scrypt-cost-bomb",
        ),
    ],
)
async def test_tampered_file_degrades_instead_of_crashing(tmp_path: Path, mutate: Any) -> None:
    await _tamper(tmp_path, mutate)
    # Fresh instances, as at boot — in both key modes (a passphrase store is
    # what actually reaches the scrypt-parameter validation branch).
    for store in (_file_store(tmp_path), _file_store(tmp_path, passphrase="pw")):
        assert await store.get("fmp_api_key") is None
        assert await store.has("fmp_api_key") is False


@pytest.mark.asyncio
async def test_write_over_unreadable_file_moves_it_aside(tmp_path: Path) -> None:
    path = await _tamper(tmp_path, _flip_ciphertext)
    tampered_bytes = path.read_bytes()

    store = _file_store(tmp_path)
    await store.set("finnhub_api_key", "fh-NEW")

    assert await _file_store(tmp_path).get("finnhub_api_key") == "fh-NEW"
    aside = [p for p in path.parent.iterdir() if p.name.startswith(".secrets.unreadable-")]
    assert len(aside) == 1
    assert aside[0].read_bytes() == tampered_bytes  # preserved verbatim, not deleted


@pytest.mark.asyncio
async def test_corrupt_legacy_json_does_not_crash_boot(tmp_path: Path) -> None:
    """A torn pre-encryption file used to raise out of boot hydration."""
    path = tmp_path / "home" / ".secrets"
    path.parent.mkdir(parents=True)
    path.write_text('{"fmp_api_key": "trunc')
    store = _file_store(tmp_path)
    assert await store.get("fmp_api_key") is None
    await store.set("fmp_api_key", "fmp-2")
    assert await _file_store(tmp_path).get("fmp_api_key") == "fmp-2"


@pytest.mark.asyncio
async def test_lost_key_file_reads_as_unset_and_read_never_mints_a_key(
    tmp_path: Path, caplog: Any
) -> None:
    store = _file_store(tmp_path)
    await store.set("fmp_api_key", "fmp-1")
    key_path = tmp_path / "keydir" / "secrets.key"
    key_path.unlink()

    fresh = _file_store(tmp_path)
    with caplog.at_level(logging.WARNING, logger="finrobot.secret_store"):
        assert await fresh.get("fmp_api_key") is None
        assert await fresh.get("fmp_api_key") is None
    # A read must never create a new key (that would orphan the old data).
    assert not key_path.exists()
    # One warning, not one per read (GET /api/settings probes every key).
    assert len([r for r in caplog.records if "cannot be read" in r.getMessage()]) == 1

    await fresh.set("fmp_api_key", "fmp-2")  # recovers: new key, old file aside
    assert key_path.exists()
    assert await _file_store(tmp_path).get("fmp_api_key") == "fmp-2"


@pytest.mark.asyncio
async def test_corrupt_key_file_fails_the_save_and_is_never_overwritten(tmp_path: Path) -> None:
    key_path = tmp_path / "keydir" / "secrets.key"
    key_path.parent.mkdir(parents=True)
    key_path.write_text("not-base64-key!!")
    store = _file_store(tmp_path)
    with pytest.raises(SecretStoreError, match="unusable") as excinfo:
        await store.set("fmp_api_key", "fmp-SECRETVALUE")
    assert "SECRETVALUE" not in str(excinfo.value)
    assert key_path.read_text() == "not-base64-key!!"
    assert not (tmp_path / "home" / ".secrets").exists()


# -- passphrase mode -------------------------------------------------------------


@pytest.mark.asyncio
async def test_passphrase_mode_never_writes_a_key_file(tmp_path: Path) -> None:
    store = _file_store(tmp_path, passphrase="correct horse battery staple")
    await store.set("fmp_api_key", "fmp-PP")
    envelope = json.loads((tmp_path / "home" / ".secrets").read_text())
    assert envelope["kdf"] == "scrypt"
    assert "salt" in envelope
    assert not (tmp_path / "keydir" / "secrets.key").exists()
    fresh = _file_store(tmp_path, passphrase="correct horse battery staple")
    assert await fresh.get("fmp_api_key") == "fmp-PP"


@pytest.mark.asyncio
@pytest.mark.parametrize("passphrase", ["wrong passphrase", None])
async def test_passphrase_file_is_unreadable_without_the_right_passphrase(
    tmp_path: Path, passphrase: str | None
) -> None:
    await _file_store(tmp_path, passphrase="right").set("fmp_api_key", "fmp-PP")
    assert await _file_store(tmp_path, passphrase=passphrase).get("fmp_api_key") is None


@pytest.mark.asyncio
async def test_setting_a_passphrase_migrates_a_keyfile_store(tmp_path: Path) -> None:
    await _file_store(tmp_path).set("fmp_api_key", "fmp-1")
    assert await _file_store(tmp_path, passphrase="pw").get("fmp_api_key") == "fmp-1"
    envelope = json.loads((tmp_path / "home" / ".secrets").read_text())
    assert envelope["kdf"] == "scrypt"
    # The key file alone no longer opens the store.
    assert await _file_store(tmp_path).get("fmp_api_key") is None


@pytest.mark.asyncio
async def test_tampered_scrypt_header_is_detected(tmp_path: Path) -> None:
    """Header fields are AES-GCM associated data: editing one (e.g. lowering
    the scrypt cost) must break decryption, not be silently honoured."""
    await _file_store(tmp_path, passphrase="pw").set("k", "v")
    path = tmp_path / "home" / ".secrets"
    envelope = json.loads(path.read_text())
    envelope["p"] = 2
    path.write_text(json.dumps(envelope))
    assert await _file_store(tmp_path, passphrase="pw").get("k") is None


# -- key location policy --------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "expected_parts"),
    [
        ("darwin", ("Library", "Application Support", "FinRobot", "secrets.key")),
        ("linux", (".local", "share", "finrobot", "secrets.key")),
    ],
)
def test_default_key_path_is_outside_the_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str, expected_parts: tuple[str, ...]
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("FINROBOT_SECRETS_KEY_FILE", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    key_path = default_key_path()
    assert key_path == tmp_path.joinpath(*expected_parts)
    assert (tmp_path / ".finrobot") not in key_path.parents


def test_default_key_path_windows_uses_local_appdata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.delenv("FINROBOT_SECRETS_KEY_FILE", raising=False)
    assert default_key_path() == tmp_path / "Local" / "FinRobot" / "secrets.key"


def test_default_key_path_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FINROBOT_SECRETS_KEY_FILE", str(tmp_path / "mounted.key"))
    assert default_key_path() == tmp_path / "mounted.key"


# ---------------------------------------------------------------------------
# KeychainSecretStore — runtime keychain refusal must degrade, not crash.
# The boot-time probe only proves the backend worked ONCE; the user can click
# "Deny" on the macOS prompt (KeyringLocked) at any later call. Reads degrade
# to None/False with one warning per key; writes raise SecretStoreError so a
# failed save is never reported as success.
# ---------------------------------------------------------------------------


class _RefusingKeyring:
    """Stand-in for the keyring module whose backend rejects every call."""

    errors = keyring.errors

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def get_password(self, service: str, key: str) -> str | None:
        raise self.exc

    def set_password(self, service: str, key: str, value: str) -> None:
        raise self.exc

    def delete_password(self, service: str, key: str) -> None:
        raise self.exc


def _refusing_store(exc: Exception | None = None) -> KeychainSecretStore:
    """Build a KeychainSecretStore around a refusing backend, skipping the
    constructor probe (which touches the real OS keychain)."""
    store = KeychainSecretStore.__new__(KeychainSecretStore)
    store._keyring = _RefusingKeyring(  # type: ignore[assignment]
        exc or keyring.errors.KeyringLocked("Can't get password: user denied access")
    )
    store._service_name = "FinRobotTest"
    store._degraded_keys = set()
    return store


@pytest.mark.asyncio
async def test_keychain_get_denied_degrades_to_none(caplog: Any) -> None:
    store = _refusing_store()
    with caplog.at_level(logging.WARNING, logger="finrobot.secret_store"):
        assert await store.get("fmp_api_key") is None
        assert await store.get("fmp_api_key") is None  # second read: no new warning
        assert await store.get("finnhub_api_key") is None  # new key: its own warning

    warnings = [r for r in caplog.records if "treating the secret as unset" in r.getMessage()]
    assert len(warnings) == 2  # one per key, not one per call
    assert "fmp_api_key" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_keychain_has_denied_degrades_to_false() -> None:
    store = _refusing_store()
    assert await store.has("fmp_api_key") is False


@pytest.mark.asyncio
async def test_keychain_set_denied_raises_secret_store_error() -> None:
    store = _refusing_store()
    with pytest.raises(SecretStoreError) as excinfo:
        await store.set("fmp_api_key", "sk-super-secret-value")
    # The error names the key but NEVER carries the secret value.
    assert "fmp_api_key" in str(excinfo.value)
    assert "sk-super-secret-value" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_keychain_delete_missing_key_is_idempotent() -> None:
    store = _refusing_store(keyring.errors.PasswordDeleteError("not found"))
    await store.delete("fmp_api_key")  # must not raise


@pytest.mark.asyncio
async def test_keychain_delete_denied_raises_secret_store_error() -> None:
    store = _refusing_store(keyring.errors.KeyringLocked("keychain locked"))
    with pytest.raises(SecretStoreError):
        await store.delete("fmp_api_key")


# ---------------------------------------------------------------------------
# create_secret_store — platform-aware, prompt-free backend selection.
# macOS has no prompt-free keychain without a stable code signature, so it
# defaults to the file; Windows/Linux use the silent OS credential store.
# ---------------------------------------------------------------------------


def _forbid_keychain(monkeypatch: Any) -> None:
    """Fail loudly if KeychainSecretStore is constructed — proves the macOS and
    dev paths never touch the OS keychain (the whole point of the fix)."""

    def _boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("KeychainSecretStore must not be constructed here")

    monkeypatch.setattr(secret_store_module, "KeychainSecretStore", _boom)


def test_create_store_dev_mode_uses_file(monkeypatch: Any) -> None:
    monkeypatch.delenv("FINROBOT_DEV_MODE", raising=False)
    _forbid_keychain(monkeypatch)
    store, mode = create_secret_store(dev_mode=True)
    assert isinstance(store, EncryptedFileSecretStore)
    assert mode == "encrypted_file"


def test_create_store_dev_mode_via_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("FINROBOT_DEV_MODE", "1")
    _forbid_keychain(monkeypatch)
    _, mode = create_secret_store()
    assert mode == "encrypted_file"


def test_create_store_macos_defaults_to_file(monkeypatch: Any) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("FINROBOT_DEV_MODE", raising=False)
    monkeypatch.delenv("FINROBOT_USE_KEYCHAIN", raising=False)
    _forbid_keychain(monkeypatch)  # macOS must skip the keychain entirely
    store, mode = create_secret_store()
    assert isinstance(store, EncryptedFileSecretStore)
    assert mode == "encrypted_file"


def test_create_store_macos_opt_in_keychain(monkeypatch: Any) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("FINROBOT_DEV_MODE", raising=False)
    monkeypatch.setenv("FINROBOT_USE_KEYCHAIN", "1")
    sentinel = object()
    monkeypatch.setattr(secret_store_module, "KeychainSecretStore", lambda *_a, **_k: sentinel)
    store, mode = create_secret_store()
    assert store is sentinel
    assert mode == "keychain"


def test_create_store_windows_uses_keychain(monkeypatch: Any) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("FINROBOT_DEV_MODE", raising=False)
    sentinel = object()
    monkeypatch.setattr(secret_store_module, "KeychainSecretStore", lambda *_a, **_k: sentinel)
    store, mode = create_secret_store()
    assert store is sentinel
    assert mode == "keychain"


def test_create_store_keychain_unavailable_falls_back_to_file(monkeypatch: Any) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("FINROBOT_DEV_MODE", raising=False)

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("no functional backend")

    monkeypatch.setattr(secret_store_module, "KeychainSecretStore", _boom)
    store, mode = create_secret_store()
    assert isinstance(store, EncryptedFileSecretStore)
    assert mode == "encrypted_file"


def test_create_store_passes_passphrase_from_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("FINROBOT_SECRETS_PASSPHRASE", "from-env")
    _forbid_keychain(monkeypatch)
    store, mode = create_secret_store(dev_mode=True)
    assert isinstance(store, EncryptedFileSecretStore)
    assert store.uses_passphrase
    assert mode == "encrypted_file"


def test_create_store_empty_passphrase_env_means_key_file(monkeypatch: Any) -> None:
    monkeypatch.setenv("FINROBOT_SECRETS_PASSPHRASE", "")
    _forbid_keychain(monkeypatch)
    store, _ = create_secret_store(dev_mode=True)
    assert isinstance(store, EncryptedFileSecretStore)
    assert not store.uses_passphrase


@_posix_only
@pytest.mark.asyncio
async def test_secret_dir_tightened_to_0700(tmp_path: Path) -> None:
    sub = tmp_path / "nested"
    sub.mkdir(mode=0o755)
    store = EncryptedFileSecretStore(sub / ".secrets", key_path=tmp_path / "keydir" / "k")
    await store.set("k", "v")
    assert stat.S_IMODE(sub.stat().st_mode) == 0o700
