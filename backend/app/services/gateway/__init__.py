"""gateway: selección y descifrado de credencial (BYOK/master) + streaming
litellm (streaming llega en S1-10).

El cifrado/descifrado de keys BYOK vive aquí porque el descifrado SOLO
puede ocurrir en este servicio (regla 3, CLAUDE.md) — `onboarding` importa
`encrypt_provider_key` para guardar la key validada, pero nunca
`decrypt_provider_key`; esa función solo la usa GatewayService al hacer la
llamada real al proveedor (S1-10).
"""

import base64

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _fernet() -> Fernet:
    # APP_MASTER_KEY se genera con `openssl rand -base64 32` (base64 estándar,
    # ver .env.example) — Fernet exige su propia variante url-safe, así que se
    # decodifica a los 32 bytes crudos y se re-codifica en el formato que pide.
    raw_key = base64.b64decode(settings.app_master_key)
    return Fernet(base64.urlsafe_b64encode(raw_key))


def encrypt_provider_key(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_provider_key(ciphertext: bytes) -> str:
    """SOLO se llama desde gateway (regla 3, CLAUDE.md)."""
    try:
        return _fernet().decrypt(bytes(ciphertext)).decode("utf-8")
    except InvalidToken as e:
        raise ValueError(
            "no se pudo descifrar la key (master key incorrecta o dato corrupto)"
        ) from e
