import base64
import json
import logging
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Lazy import — only needed when encryption is enabled
_Fernet = None


def _get_fernet():
    global _Fernet
    if _Fernet is None:
        from cryptography.fernet import Fernet
        _Fernet = Fernet
    return _Fernet


# Marker prefix to distinguish encrypted files from plaintext
_ENCRYPTED_PREFIX = "ENC::"


@dataclass
class UserToken:
    """OAuth token for a single WhatsApp user's linked 42 account."""

    access_token: str
    refresh_token: str | None
    expires_at: float  # wall-clock via time.time()
    user_id: int

    @property
    def is_expired(self) -> bool:
        """Check if the token has expired (with 30s safety margin).

        Uses ``time.time()`` (not ``time.monotonic()``) because this
        value is persisted to disk and must survive process restarts.
        Contrast with :class:`FtClient` which uses ``monotonic()`` for
        its in-memory-only app token.
        """
        return time.time() >= (self.expires_at - 30)


class UserTokenStore:
    """Persistent JID → UserToken mapping.

    Follows the SeenEvents pattern: saves to disk after every mutation.

    If *encryption_key* is provided (a Fernet key string), tokens are
    encrypted at rest.  On load, if the file is plaintext but a key is
    now configured, the store silently migrates to encrypted format on
    the next save.
    """

    def __init__(self, path: str, *, encryption_key: str = "") -> None:
        self._path = Path(path)
        self._tokens: dict[str, UserToken] = {}
        self._fernet = None
        if encryption_key:
            Fernet = _get_fernet()
            self._fernet = Fernet(encryption_key.encode())
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._tokens = {}
            return
        text = self._path.read_text().strip()
        if not text:
            self._tokens = {}
            return

        # Decrypt if the file is encrypted
        if text.startswith(_ENCRYPTED_PREFIX):
            if self._fernet is None:
                raise RuntimeError(
                    f"Token file {self._path} is encrypted but no "
                    f"BOT_TOKEN_ENCRYPTION_KEY is configured"
                )
            encrypted_b64 = text[len(_ENCRYPTED_PREFIX):]
            try:
                text = self._fernet.decrypt(encrypted_b64.encode()).decode()
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to decrypt {self._path} — wrong key? {exc}"
                ) from exc

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Corrupted user-tokens file {self._path}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeError(
                f"Corrupted user-tokens file {self._path}: expected JSON object, got {type(data).__name__}"
            )
        for number, tok in data.items():
            self._tokens[number] = UserToken(
                access_token=tok["access_token"],
                refresh_token=tok.get("refresh_token"),
                expires_at=tok["expires_at"],
                user_id=tok["user_id"],
            )
        logger.info("Loaded %d user token(s) from %s", len(self._tokens), self._path)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        for number, tok in self._tokens.items():
            data[number] = {
                "access_token": tok.access_token,
                "refresh_token": tok.refresh_token,
                "expires_at": tok.expires_at,
                "user_id": tok.user_id,
            }
        content = json.dumps(data, indent=2)

        # Encrypt at rest if a key is configured
        if self._fernet is not None:
            encrypted = self._fernet.encrypt(content.encode()).decode()
            content = _ENCRYPTED_PREFIX + encrypted

        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(content)
        # Restrict permissions — tokens can register users for events
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        os.replace(tmp, self._path)

    def get(self, whatsapp_number: str) -> UserToken | None:
        return self._tokens.get(whatsapp_number)

    def set(self, whatsapp_number: str, token: UserToken) -> None:
        from whatsapp_bot.security import redact_phone
        self._tokens[whatsapp_number] = token
        self._save()
        logger.info("Stored token for %s (user_id=%d)", redact_phone(whatsapp_number), token.user_id)

    def items(self) -> list[tuple[str, UserToken]]:
        """Return all (whatsapp_number, token) pairs."""
        return list(self._tokens.items())

    def delete(self, whatsapp_number: str) -> None:
        from whatsapp_bot.security import redact_phone
        if whatsapp_number in self._tokens:
            del self._tokens[whatsapp_number]
            self._save()
            logger.info("Deleted token for %s", redact_phone(whatsapp_number))
