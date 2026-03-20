"""
AES-256-GCM encryption layer for provider API keys.

Storage format (providers.enc):
  {
    "version": 1,
    "kdf": "pbkdf2-sha256",
    "iterations": 600000,
    "salt": "<base64-16-bytes>",
    "nonce": "<base64-12-bytes>",
    "ciphertext": "<base64-AES-256-GCM-output>"
  }

Master key resolution order:
  1. Env var LEAFHUB_MASTER_KEY (base64)
  2. System Keychain (via keyring)
  3. ~/.leafhub/.masterkey file (chmod 600)
  4. First run: generate and persist to Keychain or file

Ref: ModelHub/core/crypto.py (adapted — path prefix and env var renamed)
"""

import base64
import json
import logging
import os
import secrets
import stat
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from leafhub.core import default_hub_dir  # canonical definition

log = logging.getLogger(__name__)

_KEYRING_SERVICE  = "leafhub"
_KEYRING_USERNAME = "master_key"
_KDF_ITERATIONS   = 600_000
_SALT_BYTES       = 16
_NONCE_BYTES      = 12
_KEY_BYTES        = 32  # AES-256


def _resolve_dir(hub_dir: Path | None) -> Path:
    return hub_dir if hub_dir is not None else default_hub_dir()


# ── Master key resolution ─────────────────────────────────────────────────────

def _load_master_key_env() -> bytes | None:
    raw = os.environ.get("LEAFHUB_MASTER_KEY")
    if raw:
        key = base64.b64decode(raw)
        if len(key) != _KEY_BYTES:
            raise ValueError(
                f"LEAFHUB_MASTER_KEY must decode to exactly {_KEY_BYTES} bytes "
                f"(got {len(key)}). Generate a valid key with: "
                "python -c \"import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())\""
            )
        return key
    return None


def _load_master_key_keyring() -> bytes | None:
    try:
        import keyring
        val = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
        if val:
            return base64.b64decode(val)
    except Exception:
        pass
    return None


def _save_master_key_keyring(key: bytes) -> bool:
    try:
        import keyring
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME,
                             base64.b64encode(key).decode())
        # Verify the key was actually persisted — null/stub backends accept
        # set_password without raising but return nothing on get_password,
        # which would cause a different key to be generated on the next call.
        return keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME) is not None
    except Exception:
        return False


def _load_master_key_file(hub_dir: Path | None) -> bytes | None:
    p = _resolve_dir(hub_dir) / ".masterkey"
    if not p.exists():
        return None
    mode = os.stat(p).st_mode
    if mode & 0o077:
        log.warning(
            "Master key file %s has unsafe permissions (%o). "
            "Run: chmod 600 %s", p, mode & 0o777, p,
        )
    return base64.b64decode(p.read_bytes().strip())


def _save_master_key_file(key: bytes, hub_dir: Path | None) -> None:
    p = _resolve_dir(hub_dir) / ".masterkey"
    p.write_bytes(base64.b64encode(key))
    # Ref: Trileaf/scripts/rewrite_config.py — chmod 600 for credential files
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)


def load_master_key(hub_dir: Path | None = None) -> bytes:
    """
    Resolve master key from available sources. On first run,
    generate a random key and persist it.
    """
    key = (
        _load_master_key_env()
        or _load_master_key_keyring()
        or _load_master_key_file(hub_dir)
    )
    if key:
        return key

    log.info("No master key found, generating new one")
    key = secrets.token_bytes(_KEY_BYTES)
    if not _save_master_key_keyring(key):
        log.warning("Keychain unavailable, falling back to ~/.leafhub/.masterkey")
        _save_master_key_file(key, hub_dir)
    return key


# ── Key derivation ────────────────────────────────────────────────────────────

def _derive_aes_key(master_key: bytes, salt: bytes) -> bytes:
    """PBKDF2-SHA256 → 32-byte AES key."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=salt,
        iterations=_KDF_ITERATIONS,
    )
    return kdf.derive(master_key)


# ── Encrypt / Decrypt ─────────────────────────────────────────────────────────

def _enc_file(hub_dir: Path | None) -> Path:
    return _resolve_dir(hub_dir) / "providers.enc"


def encrypt_providers(data: dict, master_key: bytes,
                      hub_dir: Path | None = None) -> None:
    """Encrypt provider dict (containing api_keys) and write to providers.enc."""
    salt      = secrets.token_bytes(_SALT_BYTES)
    nonce     = secrets.token_bytes(_NONCE_BYTES)
    aes_key   = _derive_aes_key(master_key, salt)
    plaintext = json.dumps(data, ensure_ascii=False).encode()
    ciphertext = AESGCM(aes_key).encrypt(nonce, plaintext, associated_data=None)

    payload = {
        "version":    1,
        "kdf":        "pbkdf2-sha256",
        "iterations": _KDF_ITERATIONS,
        "salt":       base64.b64encode(salt).decode(),
        "nonce":      base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }
    p = _enc_file(hub_dir)
    p.write_text(json.dumps(payload, indent=2))
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)  # chmod 600


def decrypt_providers(master_key: bytes,
                      hub_dir: Path | None = None) -> dict:
    """
    Read providers.enc and return decrypted dict.
    Returns {} if the file does not exist yet.
    Raises RuntimeError if the file is corrupt or the key is wrong.
    """
    p = _enc_file(hub_dir)
    if not p.exists():
        return {}

    try:
        payload    = json.loads(p.read_text())
        version    = payload.get("version")
        if version != 1:
            raise RuntimeError(
                f"providers.enc: unsupported version {version!r}. "
                "This file was written by a newer version of Leafhub. "
                "Upgrade leafhub or delete providers.enc and re-add your providers."
            )
        salt       = base64.b64decode(payload["salt"])
        nonce      = base64.b64decode(payload["nonce"])
        ciphertext = base64.b64decode(payload["ciphertext"])
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"providers.enc is corrupt or unreadable ({p}): {exc}. "
            "If you changed the master key, restore the original key or "
            "delete providers.enc and re-add your providers."
        ) from exc

    try:
        aes_key   = _derive_aes_key(master_key, salt)
        plaintext = AESGCM(aes_key).decrypt(nonce, ciphertext, associated_data=None)
        return json.loads(plaintext)
    except Exception as exc:
        raise RuntimeError(
            "Failed to decrypt providers.enc — master key may be wrong. "
            "Check LEAFHUB_MASTER_KEY or ~/.leafhub/.masterkey."
        ) from exc
