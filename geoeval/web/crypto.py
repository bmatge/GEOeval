"""
Chiffrement des secrets BYOK (ADR-078 §1).

Clé maître dans `GEOEVAL_KEY_SECRET` (base64 urlsafe, 32 octets) — posée dans
`/opt/apps/geoeval/.env` sur le VPS (survit aux pulls), copiée depuis
`.env.example` en dev local. La perte de la clé maître rend les BYOK
irrécupérables (accepté, cf. ADR-078).

Format : Fernet (AES-128-CBC + HMAC-SHA256, sérialisation base64 urlsafe).
"""
from __future__ import annotations

import base64
import os
from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


class CryptoConfigError(RuntimeError):
    """Levée quand la clé maître est absente ou invalide."""


def _key_from_env() -> bytes:
    raw = (os.environ.get("GEOEVAL_KEY_SECRET") or "").strip()
    if not raw:
        raise CryptoConfigError(
            "GEOEVAL_KEY_SECRET absent : impossible de chiffrer / déchiffrer "
            "les clés BYOK. Pose une clé Fernet (base64 urlsafe, 32 octets) "
            "dans le .env avant de démarrer."
        )
    # Fernet attend 32 octets encodés urlsafe base64 (44 chars).
    try:
        decoded = base64.urlsafe_b64decode(raw.encode("ascii") + b"===")
    except Exception as exc:  # noqa: BLE001
        raise CryptoConfigError(f"GEOEVAL_KEY_SECRET n'est pas du base64 urlsafe : {exc}")
    if len(decoded) != 32:
        raise CryptoConfigError(
            f"GEOEVAL_KEY_SECRET doit décoder à 32 octets (Fernet), reçu {len(decoded)}."
        )
    return raw.encode("ascii")


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(_key_from_env())


def generate_key() -> str:
    """Génère une nouvelle clé Fernet (à mettre dans GEOEVAL_KEY_SECRET)."""
    return Fernet.generate_key().decode("ascii")


def encrypt_secret(plain: str) -> str:
    """Chiffre un secret. Renvoie le blob base64 urlsafe (str)."""
    if plain is None:
        raise ValueError("plain=None : rien à chiffrer")
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_secret(cipher: Optional[str]) -> Optional[str]:
    """Déchiffre un blob Fernet. `None` → `None` (pas de secret enregistré)."""
    if cipher is None or cipher == "":
        return None
    try:
        return _fernet().decrypt(cipher.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise CryptoConfigError(
            "Impossible de déchiffrer un secret : GEOEVAL_KEY_SECRET actuel ne "
            "correspond pas à celui utilisé au chiffrement (rotation manquée ?)."
        ) from exc
