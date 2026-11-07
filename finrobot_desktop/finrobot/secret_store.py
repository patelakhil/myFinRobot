from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import stat
import sys
import tempfile
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

logger = logging.getLogger(__name__)

SecretStorageMode = Literal["keychain", "encrypted_file"]


class SecretStoreError(RuntimeError):
    """A secret could not be written to / deleted from the underlying store.

    Raised instead of the backend's own exception type so route handlers can
    surface ONE stable error to the user (a failed key save must never look
    like success, nor like an opaque 500). The message carries only the key
    name and the backend failure — never the secret value.
    """


class SecretStore(ABC):
    """Abstract secret storage.

    Business code calls this interface and never touches the underlying
    storage mechanism directly.
    """

    @abstractmethod
    async def get(self, key: str) -> str | None:
        """Retrieve a secret by key. Returns None if not set."""

    @abstractmethod
    async def set(self, key: str, value: str) -> None:
        """Store a secret."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove a secret."""

    @abstractmethod
    async def has(self, key: str) -> bool:
        """Check if a secret exists without retrieving it."""


class KeychainSecretStore(SecretStore):
    """OS keychain-backed secret storage via the optional keyring package.

    The constructor probe only proves the backend worked ONCE at boot — the
    keychain can still refuse at runtime (the user clicks "Deny" on the macOS
    access prompt, the keychain locks, the backend breaks mid-session). Every
    call therefore handles ``keyring.errors.KeyringError`` (keyring's documented
    base — the macOS backend wraps every Security.framework failure into it,
    denial included, as ``KeyringLocked``) plus ``OSError`` for backend file/IPC
    faults on other platforms. Reads degrade (None/False + one warning per key,
    same self-heal-over-crash stance as EncryptedFileSecretStore's BUG-083
    permission healing); writes raise :class:`SecretStoreError` so a failed key
    save is never reported as success. ``KeyboardInterrupt`` / ``CancelledError`` are
    BaseException and pass through untouched.
    """

    def __init__(self, service_name: str = "FinRobot") -> None:
        try:
            import keyring
        except ImportError as e:
            raise RuntimeError("keyring package is not available") from e
        # Probe: verify the keyring backend is functional. In headless
        # environments (CI / Docker / WSL) keyring imports fine but raises
        # NoKeyringError on the first real call.
        try:
            keyring.get_password(service_name, "__finrobot_probe__")
        except (keyring.errors.KeyringError, OSError) as e:
            raise RuntimeError(f"keyring backend not functional: {e}") from e
        self._keyring = keyring
        self._service_name = service_name
        # Keys whose reads already logged a degradation warning — one line per
        # key per process, not one per poll (GET /api/settings probes every key).
        self._degraded_keys: set[str] = set()

    def _warn_degraded_once(self, key: str, exc: Exception) -> None:
        if key in self._degraded_keys:
            return
        self._degraded_keys.add(key)
        logger.warning(
            "Keychain read for %r failed (%s: %s) — treating the secret as unset. "
            "Grant FinRobot keychain access (or unlock the keychain), then re-open "
            "Settings; re-enter the key if it stays missing.",
            key,
            type(exc).__name__,
            exc,
        )

    async def get(self, key: str) -> str | None:
        try:
            return await asyncio.to_thread(self._keyring.get_password, self._service_name, key)
        except (self._keyring.errors.KeyringError, OSError) as exc:
            # Denied/locked/broken keychain → behave as "not stored" instead of
            # crashing the caller: boot hydration then proceeds without keys
            # (validate_runtime_config surfaces THAT via the startup_error
            # banner) and GET /api/settings keeps answering instead of 500-ing.
            self._warn_degraded_once(key, exc)
            return None

    async def set(self, key: str, value: str) -> None:
        try:
            await asyncio.to_thread(self._keyring.set_password, self._service_name, key, value)
        except (self._keyring.errors.KeyringError, OSError) as exc:
            # Unlike reads, a failed WRITE must be loud — pretending the key was
            # saved strands the user with a config that dies on next boot. The
            # message carries the key name and backend error, never the value.
            raise SecretStoreError(
                f"Failed to store secret '{key}' in the OS keychain ({type(exc).__name__}: {exc})"
            ) from exc

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(self._keyring.delete_password, self._service_name, key)
        except self._keyring.errors.PasswordDeleteError:
            # Key absent — delete is idempotent. (On macOS a denied prompt also
            # lands here: the backend folds every delete failure into
            # PasswordDeleteError. The follow-up GET /api/settings re-reads the
            # keychain, so the UI reflects the real stored state either way.)
            return
        except (self._keyring.errors.KeyringError, OSError) as exc:
            # Backends that DO distinguish (locked keychain, IPC fault): the
            # secret is still stored, so the clear must not pretend success.
            raise SecretStoreError(
                f"Failed to delete secret '{key}' from the OS keychain "
                f"({type(exc).__name__}: {exc})"
            ) from exc

    async def has(self, key: str) -> bool:
        return await self.get(key) is not None


# ---------------------------------------------------------------------------
# Encrypted local-file store
# ---------------------------------------------------------------------------

# Envelope written to ~/.finrobot/.secrets. Only the header fields are ever in
# the clear; every secret name and value lives inside the AES-GCM ciphertext.
_ENVELOPE_FORMAT = "finrobot-secrets"
_ENVELOPE_VERSION = 1
_KDF_KEYFILE = "keyfile"
_KDF_SCRYPT = "scrypt"
_KEY_BYTES = 32  # AES-256
_NONCE_BYTES = 12  # 96-bit nonce, the GCM standard size
_SALT_BYTES = 16
# scrypt cost: 2**15 / 8 / 1 ≈ 32 MiB and ~0.1 s per derivation — OWASP's
# interactive-login baseline. Stored in the header so it can be raised later
# without breaking existing files; bounded on read so a tampered header can't
# make us allocate gigabytes.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAX_N = 2**20
_SCRYPT_MAX_R = 16
_SCRYPT_MAX_P = 4

PASSPHRASE_ENV = "FINROBOT_SECRETS_PASSPHRASE"
KEY_FILE_ENV = "FINROBOT_SECRETS_KEY_FILE"


class _UnreadableSecretsError(Exception):
    """The secrets file exists but cannot be decrypted / parsed (internal)."""


def default_secrets_path() -> Path:
    """Encrypted secrets file: ``~/.finrobot/.secrets`` (same path as before)."""
    return Path.home() / ".finrobot" / ".secrets"


def default_key_path() -> Path:
    """Where the file store's master key lives when no passphrase is set.

    Deliberately NOT under ``~/.finrobot/``: that directory is documented as
    the thing users back up, sync, zip into bug reports, or ``rm -rf``. Keeping
    the key in the OS per-user app-data dir means a copy of ``~/.finrobot``
    alone no longer leaks usable API keys. Override with
    ``FINROBOT_SECRETS_KEY_FILE`` (e.g. a Docker secret mount).
    """
    override = os.environ.get(KEY_FILE_ENV)
    if override:
        return Path(override).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support" / "FinRobot"
    elif sys.platform == "win32":
        # LOCALAPPDATA (not APPDATA): the non-roaming profile, so the key does
        # not follow the user to other machines via roaming-profile sync.
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "FinRobot"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "finrobot"
    return base / "secrets.key"


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: object, field: str) -> bytes:
    if not isinstance(text, str):
        raise _UnreadableSecretsError(f"envelope field {field!r} is missing or not a string")
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _UnreadableSecretsError(f"envelope field {field!r} is not valid base64") from exc


def _is_envelope(parsed: dict[str, Any]) -> bool:
    # Keyed on the format marker alone: a damaged envelope (say, ciphertext
    # missing) must be reported as unreadable, never mistaken for a legacy
    # plaintext file and "migrated" with its header fields as secrets.
    return parsed.get("format") == _ENVELOPE_FORMAT


def _header_aad(header: dict[str, Any]) -> bytes:
    """Canonical bytes of the clear-text header, bound as AES-GCM associated
    data so flipping any header field (kdf, salt, cost params, version) makes
    decryption fail instead of being silently honoured."""
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")


# POSIX mode bits are meaningless on Windows (os.chmod only toggles the
# read-only flag; protection comes from the per-user profile ACLs on
# %USERPROFILE% / %LOCALAPPDATA%), so the permission checks are skipped there.
_POSIX_PERMS = sys.platform != "win32"


def _lock_dir_0700(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if not _POSIX_PERMS:
        return
    # mkdir honours the process umask (often 0755 → world-listable). Tighten to
    # 0700 so other local users can neither list it nor plant a symlink in it.
    try:
        if stat.S_IMODE(directory.stat().st_mode) != 0o700:
            os.chmod(directory, 0o700)
    except OSError as exc:
        raise SecretStoreError(
            f"Secret directory {directory} could not be permission-locked to 0700"
        ) from exc


def _heal_file_mode_0600(path: Path, what: str) -> None:
    """Tighten a drifted file back to 0600 (backup/restore, editor rewrites and
    sync tools routinely loosen it). Self-heal, never crash (BUG-083)."""
    if not _POSIX_PERMS:
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return
    expected = stat.S_IRUSR | stat.S_IWUSR
    if mode == expected:
        return
    logger.warning("%s %s had mode %#o, expected 0600; tightening permissions.", what, path, mode)
    try:
        os.chmod(path, expected)
    except OSError as exc:
        # e.g. a read-only Docker secret mount — keep going, but say so.
        logger.warning("Could not tighten %s to 0600 (%s); continuing.", path, exc)


def _fsync_dir(directory: Path) -> None:
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        # Some filesystems (and Windows) don't support directory fsync.
        pass
    finally:
        os.close(dir_fd)


class EncryptedFileSecretStore(SecretStore):
    """Local-file secret store, encrypted at rest with AES-256-GCM.

    Used whenever no prompt-free OS credential store is available: macOS without
    a stable code signature (an unsigned/ad-hoc app re-prompts on every keychain
    item, so we avoid the keychain there — see :func:`create_secret_store`),
    Linux with no Secret Service backend, and explicit dev mode.

    Layout:

    - ``~/.finrobot/.secrets`` — a small JSON envelope (format, version, kdf,
      nonce, ciphertext). Secret names and values exist only inside the
      authenticated ciphertext; nothing greppable (``sk-…``) is on disk.
    - The 256-bit master key comes from one of:

      * ``passphrase`` (``FINROBOT_SECRETS_PASSPHRASE``) — stretched with scrypt
        (random per-file salt); the key is never written anywhere. Strongest
        option for headless / CLI / container use.
      * otherwise a random key file (see :func:`default_key_path`), 0600 in a
        0700 directory, kept OUTSIDE ``~/.finrobot`` so the data directory on
        its own is useless to whoever obtains a copy of it.

    Threat model, stated honestly: this defeats offline leaks of the data
    directory (backups, cloud/dotfile sync, support bundles, credential-
    harvesting scanners) and other local users. Key-file mode does NOT stop
    malware already running as the same OS user — only the OS keychain or a
    passphrase the process doesn't store can do that.

    Migration: a legacy plaintext ``.secrets`` (the pre-encryption format) is
    encrypted in place on first access. A file that can't be decrypted (key
    file lost, wrong passphrase, tampering) never crashes the app and is never
    deleted: reads treat it as empty, and the next write moves it aside to
    ``.secrets.unreadable-<timestamp>`` before starting a fresh file.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        key_path: str | Path | None = None,
        passphrase: str | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else default_secrets_path()
        self._key_path = Path(key_path) if key_path is not None else default_key_path()
        self._passphrase = passphrase or None
        self._lock = asyncio.Lock()
        self._keyfile_key: bytes | None = None
        # salt → derived key, so scrypt runs once per salt, not once per read.
        self._scrypt_keys: dict[bytes, bytes] = {}
        self._scrypt_salt: bytes | None = None
        self._unreadable_warned = False

    @property
    def uses_passphrase(self) -> bool:
        return self._passphrase is not None

    # -- public API ---------------------------------------------------------

    async def get(self, key: str) -> str | None:
        async with self._lock:
            data = await asyncio.to_thread(self._load, False)
        return data.get(key)

    async def set(self, key: str, value: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._mutate, key, value)

    async def delete(self, key: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._mutate, key, None)

    async def has(self, key: str) -> bool:
        return await self.get(key) is not None

    # -- load / save (sync; always called under self._lock via to_thread) ----

    def _mutate(self, key: str, value: str | None) -> None:
        data = self._load(True)
        if value is None:
            if key not in data:
                return
            data.pop(key)
        else:
            data[key] = value
        self._save(data)

    def _load(self, for_write: bool) -> dict[str, str]:
        _lock_dir_0700(self._path.parent)
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return {}
        _heal_file_mode_0600(self._path, "Secret file")
        if not raw.strip():
            return {}
        try:
            data, needs_rewrite = self._decode(raw)
        except _UnreadableSecretsError as exc:
            return self._handle_unreadable(exc, for_write)
        self._unreadable_warned = False
        if needs_rewrite:
            # Migration (legacy plaintext → encrypted, or key-file ⇄ passphrase
            # switch) happens on the FIRST access — typically boot hydration —
            # so a plaintext copy never lingers until the user happens to save.
            try:
                self._save(data)
            except (OSError, SecretStoreError) as exc:
                if for_write:
                    raise
                # A read must still succeed; the next access retries.
                logger.warning("Could not re-encrypt secret file %s (%s).", self._path, exc)
        return data

    def _decode(self, raw: bytes) -> tuple[dict[str, str], bool]:
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise _UnreadableSecretsError("file is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise _UnreadableSecretsError("file is not a JSON object")
        if not _is_envelope(parsed):
            # Pre-encryption format: {"name": "value", ...} in the clear.
            logger.warning(
                "Secret file %s is in the legacy PLAINTEXT format; encrypting it now (%d entr%s).",
                self._path,
                len(parsed),
                "y" if len(parsed) == 1 else "ies",
            )
            return {str(k): str(v) for k, v in parsed.items()}, True
        data = self._decrypt(parsed)
        return data, parsed.get("kdf") != self._preferred_kdf()

    def _handle_unreadable(self, exc: _UnreadableSecretsError, for_write: bool) -> dict[str, str]:
        if not for_write:
            if not self._unreadable_warned:
                self._unreadable_warned = True
                logger.warning(
                    "Secret file %s cannot be read (%s) — treating every stored API key "
                    "as unset. If you changed %s or lost the key file at %s, restore it; "
                    "otherwise re-enter your keys in Settings — the next save moves the "
                    "unreadable file aside (it is never deleted).",
                    self._path,
                    exc,
                    PASSPHRASE_ENV,
                    self._key_path,
                )
            return {}
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        aside = self._path.with_name(f"{self._path.name}.unreadable-{stamp}")
        try:
            os.replace(self._path, aside)
        except OSError as move_exc:
            raise SecretStoreError(
                f"Secret file {self._path} cannot be read ({exc}) and could not be moved "
                f"aside ({move_exc})"
            ) from move_exc
        logger.warning(
            "Secret file %s could not be read (%s); moved it to %s and started a new "
            "encrypted file. Nothing was deleted.",
            self._path,
            exc,
            aside,
        )
        self._unreadable_warned = False
        return {}

    def _save(self, data: dict[str, str]) -> None:
        header: dict[str, Any] = {
            "format": _ENVELOPE_FORMAT,
            "version": _ENVELOPE_VERSION,
            "kdf": self._preferred_kdf(),
        }
        if self._passphrase is not None:
            salt = self._scrypt_salt or os.urandom(_SALT_BYTES)
            header.update(salt=_b64e(salt), n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
            key = self._scrypt_key(salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
            self._scrypt_salt = salt
        else:
            key = self._keyfile_key_or_create()
        plaintext = json.dumps(data, sort_keys=True).encode("utf-8")
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, _header_aad(header))
        envelope = {**header, "nonce": _b64e(nonce), "ciphertext": _b64e(ciphertext)}
        self._atomic_write(json.dumps(envelope, indent=2, sort_keys=True).encode("utf-8"))

    # -- crypto ---------------------------------------------------------------

    def _preferred_kdf(self) -> str:
        return _KDF_SCRYPT if self._passphrase is not None else _KDF_KEYFILE

    def _decrypt(self, envelope: dict[str, Any]) -> dict[str, str]:
        version = envelope.get("version")
        if version != _ENVELOPE_VERSION:
            raise _UnreadableSecretsError(f"unsupported envelope version {version!r}")
        kdf = envelope.get("kdf")
        header: dict[str, Any] = {"format": _ENVELOPE_FORMAT, "version": version, "kdf": kdf}
        if kdf == _KDF_KEYFILE:
            key = self._keyfile_key_existing()
        elif kdf == _KDF_SCRYPT:
            if self._passphrase is None:
                raise _UnreadableSecretsError(
                    f"file is passphrase-protected but {PASSPHRASE_ENV} is not set"
                )
            salt = _b64d(envelope.get("salt"), "salt")
            n, r, p = envelope.get("n"), envelope.get("r"), envelope.get("p")
            if not (
                isinstance(n, int)
                and isinstance(r, int)
                and isinstance(p, int)
                and 2 <= n <= _SCRYPT_MAX_N
                and n & (n - 1) == 0
                and 1 <= r <= _SCRYPT_MAX_R
                and 1 <= p <= _SCRYPT_MAX_P
            ):
                raise _UnreadableSecretsError("scrypt parameters are missing or out of range")
            header.update(salt=envelope["salt"], n=n, r=r, p=p)
            key = self._scrypt_key(salt, n, r, p)
            self._scrypt_salt = salt
        else:
            raise _UnreadableSecretsError(f"unknown kdf {kdf!r}")
        nonce = _b64d(envelope.get("nonce"), "nonce")
        ciphertext = _b64d(envelope.get("ciphertext"), "ciphertext")
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, _header_aad(header))
        except (InvalidTag, ValueError) as exc:
            raise _UnreadableSecretsError(
                "decryption failed (wrong key or passphrase, or the file was modified)"
            ) from exc
        try:
            parsed = json.loads(plaintext)
        except ValueError as exc:
            raise _UnreadableSecretsError("decrypted payload is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise _UnreadableSecretsError("decrypted payload is not a JSON object")
        return {str(k): str(v) for k, v in parsed.items()}

    def _scrypt_key(self, salt: bytes, n: int, r: int, p: int) -> bytes:
        cached = self._scrypt_keys.get(salt)
        if cached is not None:
            return cached
        assert self._passphrase is not None
        key = Scrypt(salt=salt, length=_KEY_BYTES, n=n, r=r, p=p).derive(
            self._passphrase.encode("utf-8")
        )
        self._scrypt_keys = {salt: key}  # only the current file's salt matters
        return key

    # -- key file ---------------------------------------------------------------

    def _read_keyfile(self) -> bytes:
        _heal_file_mode_0600(self._key_path, "Secret key file")
        text = self._key_path.read_text(encoding="ascii").strip()
        try:
            key = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise _UnreadableSecretsError(f"key file {self._key_path} is corrupt") from exc
        if len(key) != _KEY_BYTES:
            raise _UnreadableSecretsError(f"key file {self._key_path} is corrupt")
        return key

    def _keyfile_key_existing(self) -> bytes:
        """Key for DECRYPTING: must already exist — never mint a key to read."""
        if self._keyfile_key is None:
            try:
                self._keyfile_key = self._read_keyfile()
            except FileNotFoundError as exc:
                raise _UnreadableSecretsError(
                    f"its encryption key file {self._key_path} is missing"
                ) from exc
            except (OSError, UnicodeDecodeError) as exc:
                raise _UnreadableSecretsError(
                    f"key file {self._key_path} is unreadable ({type(exc).__name__})"
                ) from exc
        return self._keyfile_key

    def _keyfile_key_or_create(self) -> bytes:
        """Key for ENCRYPTING: reuse the existing key file, or mint one."""
        if self._keyfile_key is not None:
            return self._keyfile_key
        try:
            self._keyfile_key = self._read_keyfile()
            return self._keyfile_key
        except FileNotFoundError:
            pass
        except (_UnreadableSecretsError, OSError, UnicodeDecodeError) as exc:
            # Never overwrite an existing-but-bad key file: it may be the only
            # way to recover a file encrypted under it (or a user-supplied
            # mount with a typo). Fail the save loudly instead.
            raise SecretStoreError(
                f"Secret key file {self._key_path} exists but is unusable ({exc}). "
                "Fix or remove it, then save again."
            ) from exc
        self._keyfile_key = self._create_keyfile()
        return self._keyfile_key

    def _create_keyfile(self) -> bytes:
        _lock_dir_0700(self._key_path.parent)
        key = os.urandom(_KEY_BYTES)
        # Write the full key to a private temp file, then hard-link it into
        # place: link() is atomic and fails if the target exists, so a second
        # process racing us can never observe a half-written key, and we never
        # clobber a key another process just created.
        fd, tmp = tempfile.mkstemp(dir=str(self._key_path.parent), suffix=".tmp")
        try:
            os.write(fd, (_b64e(key) + "\n").encode("ascii"))
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            try:
                os.link(tmp, self._key_path)
            except FileExistsError:
                return self._read_keyfile()  # lost the race — use the winner's key
            except OSError:
                # Filesystem without hard links: exclusive create instead.
                try:
                    out = os.open(
                        str(self._key_path),
                        os.O_CREAT | os.O_WRONLY | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                except FileExistsError:
                    return self._read_keyfile()
                try:
                    os.write(out, (_b64e(key) + "\n").encode("ascii"))
                    os.fsync(out)
                finally:
                    os.close(out)
        finally:
            if fd >= 0:
                os.close(fd)
            if os.path.exists(tmp):
                os.unlink(tmp)
        _fsync_dir(self._key_path.parent)
        logger.info("Created secret-store encryption key at %s", self._key_path)
        return key

    # -- durable write ----------------------------------------------------------

    def _atomic_write(self, payload: bytes) -> None:
        """Write to a temp file then atomically replace, fsync'ing both the file
        and the parent directory.

        Without the fsyncs, a crash right after the rename can leave a truncated
        or missing secrets file — and the Tauri shell SIGKILLs the sidecar on
        every app quit, so that crash window is hit routinely, not rarely.
        """
        _lock_dir_0700(self._path.parent)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            os.write(fd, payload)
            os.fsync(fd)  # flush contents to disk before the rename
            os.close(fd)
            fd = -1  # mark as closed
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(tmp, self._path)
            _fsync_dir(self._path.parent)  # make the rename itself durable
        except BaseException:
            if fd >= 0:
                os.close(fd)
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


def _file_store() -> EncryptedFileSecretStore:
    return EncryptedFileSecretStore(passphrase=os.environ.get(PASSPHRASE_ENV) or None)


def create_secret_store(
    dev_mode: bool | None = None,
) -> tuple[SecretStore, SecretStorageMode]:
    """Create the best prompt-free secret store for this runtime.

    Returns a ``(store, mode)`` pair so callers can surface the storage mode to
    the user without probing the store again.  ``mode`` is:
    - ``"keychain"``       — secrets held in the OS credential store (Windows
                             Credential Manager / Linux Secret Service / macOS
                             Keychain when opted in); no password prompt.
    - ``"encrypted_file"`` — secrets in an AES-256-GCM encrypted file at
                             ``~/.finrobot/.secrets``; the key is a separate
                             0600 key file outside ``~/.finrobot`` or, when
                             ``FINROBOT_SECRETS_PASSPHRASE`` is set, derived
                             from that passphrase. See
                             :class:`EncryptedFileSecretStore`.

    Secrets are never written to disk in plaintext on any path.

    Platform policy:
    - **macOS**: default to the encrypted file. The keychain binds each item's
      access ACL to the calling app's code-signing Designated Requirement; an
      unsigned or ad-hoc-signed app (every dev rebuild, every unsigned release)
      fails to match, so macOS re-prompts *per keychain item, every launch* —
      even after "Always Allow". Until the app ships a stable Developer ID
      signature there is no prompt-free keychain, so we don't use it. A signed
      build opts back in with ``FINROBOT_USE_KEYCHAIN=1``.
    - **Windows / Linux**: use the OS credential store (Credential Manager via
      DPAPI / Secret Service). These are silent and need no code-signing
      certificate; fall back to the encrypted file if the backend is unavailable.
    - **dev mode** (``FINROBOT_DEV_MODE=1``): always the encrypted file.
    """
    use_dev = dev_mode
    if use_dev is None:
        use_dev = os.environ.get("FINROBOT_DEV_MODE") == "1"
    if use_dev:
        logger.info("Using EncryptedFileSecretStore (dev mode)")
        return _file_store(), "encrypted_file"
    if sys.platform == "darwin" and os.environ.get("FINROBOT_USE_KEYCHAIN") != "1":
        logger.info(
            "Using EncryptedFileSecretStore (macOS without a stable code signature — "
            "avoids the per-item keychain prompt storm). Set "
            "FINROBOT_USE_KEYCHAIN=1 on a Developer ID-signed build to use the "
            "keychain instead."
        )
        return _file_store(), "encrypted_file"
    try:
        store = KeychainSecretStore()
        logger.info("Using KeychainSecretStore (OS credential store)")
        return store, "keychain"
    except RuntimeError as exc:
        logger.warning(
            "OS credential store unavailable (%s), falling back to "
            "EncryptedFileSecretStore (AES-256-GCM encrypted local file).",
            exc,
        )
        return _file_store(), "encrypted_file"
