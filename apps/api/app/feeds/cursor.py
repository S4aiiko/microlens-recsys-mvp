from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime


class CursorError(ValueError):
    pass


MAX_CURSOR_OFFSET = 10_000_000


@dataclass(frozen=True, slots=True)
class CursorState:
    snapshot_id: uuid.UUID
    user_id: uuid.UUID
    feed_type: str
    offset: int
    scan_offset: int
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.feed_type or len(self.feed_type) > 32:
            raise ValueError("feed_type must be non-empty")
        if (
            isinstance(self.offset, bool)
            or isinstance(self.scan_offset, bool)
            or not isinstance(self.offset, int)
            or not isinstance(self.scan_offset, int)
            or not 0 <= self.offset <= MAX_CURSOR_OFFSET
            or not 0 <= self.scan_offset <= MAX_CURSOR_OFFSET
        ):
            raise ValueError("cursor offsets are outside the supported range")
        if self.expires_at.tzinfo is None:
            raise ValueError("cursor expiry must be timezone-aware")


class CursorCodec:
    """URL-safe signed cursor binding identity, progress, feed and expiry."""

    def __init__(self, secret: str | bytes) -> None:
        encoded = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
        if len(encoded) < 32:
            raise ValueError("cursor secret must be at least 32 bytes")
        self._secret = encoded

    def encode(self, state: CursorState) -> str:
        payload = json.dumps(
            {
                "v": 1,
                "snapshot_id": str(state.snapshot_id),
                "user_id": str(state.user_id),
                "feed_type": state.feed_type,
                "offset": state.offset,
                "scan_offset": state.scan_offset,
                "expires_at": state.expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return self._b64(payload) + "." + self._b64(signature)

    def decode(
        self,
        token: str,
        *,
        user_id: uuid.UUID,
        feed_type: str,
        now: datetime,
    ) -> CursorState:
        if now.tzinfo is None:
            raise ValueError("cursor validation time must be timezone-aware")
        try:
            payload_token, signature_token = token.split(".", 1)
            payload = self._unb64(payload_token)
            signature = self._unb64(signature_token)
        except (ValueError, TypeError) as exc:
            raise CursorError("cursor is malformed") from exc
        expected = hmac.new(self._secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise CursorError("cursor signature is invalid")
        try:
            value = json.loads(payload)
            if not isinstance(value, dict) or set(value) != {
                "v",
                "snapshot_id",
                "user_id",
                "feed_type",
                "offset",
                "scan_offset",
                "expires_at",
            }:
                raise ValueError("unsupported cursor shape")
            if isinstance(value["v"], bool) or not isinstance(value["v"], int) or value["v"] != 1:
                raise ValueError("unsupported cursor version")
            if any(
                not isinstance(value[field], str) or not value[field]
                for field in ("snapshot_id", "user_id", "feed_type", "expires_at")
            ):
                raise ValueError("cursor text fields are invalid")
            if any(
                isinstance(value[field], bool) or not isinstance(value[field], int)
                for field in ("offset", "scan_offset")
            ):
                raise ValueError("cursor offsets must be JSON integers")
            state = CursorState(
                snapshot_id=uuid.UUID(value["snapshot_id"]),
                user_id=uuid.UUID(value["user_id"]),
                feed_type=value["feed_type"],
                offset=value["offset"],
                scan_offset=value["scan_offset"],
                expires_at=datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00")),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CursorError("cursor payload is invalid") from exc
        if state.user_id != user_id or state.feed_type != feed_type:
            raise CursorError("cursor does not belong to this user and feed")
        if state.expires_at.astimezone(UTC) <= now.astimezone(UTC):
            raise CursorError("cursor is expired")
        return state

    @staticmethod
    def _b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _unb64(value: str) -> bytes:
        if not value:
            raise ValueError("empty base64 value")
        padding = "=" * (-len(value) % 4)
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
