"""Хеширование паролей приложения, JWT и шифрование внешних секретов."""

from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

_hasher = PasswordHasher()


def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        _hasher.verify(hashed, raw)
        return True
    except (VerifyMismatchError, VerificationError):
        return False


def create_access_token(user_id: int) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=s.jwt_ttl_minutes),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_access_token(token: str) -> int | None:
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


def _fernet() -> Fernet:
    return Fernet(get_settings().secrets_key.encode())


def encrypt_secret(raw: str) -> str:
    """Секреты внешних площадок хранятся только шифротекстом.

    У 4tochki нет токена — login/password уходят параметрами в каждый SOAP-вызов,
    поэтому расшифровка происходит в памяти на время запроса и никуда не логируется.
    """
    return _fernet().encrypt(raw.encode()).decode()


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError(
            "Не удалось расшифровать секрет: SECRETS_KEY не совпадает с тем, "
            "которым он шифровался."
        ) from exc


def mask_secret(raw: str) -> str:
    """Для UI: показываем только хвост."""
    if not raw:
        return ""
    if len(raw) <= 4:
        return "•" * len(raw)
    return "•" * 4 + raw[-4:]
