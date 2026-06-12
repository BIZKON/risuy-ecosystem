"""Vault секретов тенанта (ТЗ §4.5, §5; таблица tenant_secrets) — AES-256-GCM.

Envelope-модель v1: один мастер-ключ VAULT_MASTER_KEY (hex, 32 байта) из env
приложения (вписывается ТОЛЬКО twc-set-env.sh; в репо/логах/чате не живёт).
key_version в строке таблицы — задел под ротацию: при смене мастер-ключа новые
записи пишутся новой версией, старые перешифровываются фоновой задачей.

ГАРАНТИЯ «НИКОГДА В ЛОГИ» (критерий приёмки §8.5): функции этого модуля не
логируют ни plaintext, ни ключ; исключения поднимаются без значений секрета.
Вызывающий код обязан соблюдать то же (audit detail — только key_name).
"""
from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12          # стандарт GCM: 96-битный nonce
_KEY_LEN = 32            # AES-256
CURRENT_KEY_VERSION = 1


class VaultError(Exception):
    """Сбой vault: нет/битый мастер-ключ, повреждённый шифртекст, чужая версия."""


def _master_key() -> bytes:
    raw = (os.environ.get("VAULT_MASTER_KEY") or "").strip()
    if not raw:
        raise VaultError("VAULT_MASTER_KEY не задан в окружении")
    try:
        key = bytes.fromhex(raw)
    except ValueError as e:
        raise VaultError("VAULT_MASTER_KEY должен быть hex-строкой") from e
    if len(key) != _KEY_LEN:
        raise VaultError(f"VAULT_MASTER_KEY должен быть {_KEY_LEN} байт (64 hex-символа)")
    return key


def enabled() -> bool:
    """Vault доступен (мастер-ключ задан и валиден)? Для гейта UI раздела «Ключи»."""
    try:
        _master_key()
        return True
    except VaultError:
        return False


def encrypt(plaintext: str, *, aad: str = "") -> tuple[bytes, bytes, int]:
    """str → (ciphertext, nonce, key_version). aad — несекретная привязка
    (tenant_id:key_name): подмена строки между тенантами ломает расшифровку."""
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(_master_key()).encrypt(nonce, plaintext.encode("utf-8"),
                                       aad.encode("utf-8") or None)
    return ct, nonce, CURRENT_KEY_VERSION


def decrypt(ciphertext: bytes, nonce: bytes, key_version: int, *, aad: str = "") -> str:
    if key_version != CURRENT_KEY_VERSION:
        raise VaultError(f"Неизвестная версия ключа {key_version} (текущая {CURRENT_KEY_VERSION})")
    try:
        pt = AESGCM(_master_key()).decrypt(nonce, ciphertext,
                                           aad.encode("utf-8") or None)
    except InvalidTag as e:
        raise VaultError("Расшифровка не прошла аутентификацию (чужой ключ/повреждение/подмена)") from e
    return pt.decode("utf-8")
