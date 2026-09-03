from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from apps.api.app.feeds.cursor import MAX_CURSOR_OFFSET, CursorCodec, CursorError, CursorState

NOW = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
SECRET = b"phase-4-cursor-test-secret-is-long-enough"
BASE64URL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _signed(value: dict[str, object]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(SECRET, payload, hashlib.sha256).digest()
    encode = lambda row: base64.urlsafe_b64encode(row).rstrip(b"=").decode()  # noqa: E731
    return f"{encode(payload)}.{encode(signature)}"


def _payload(owner_id: uuid.UUID, **updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "v": 1,
        "snapshot_id": str(uuid.uuid4()),
        "user_id": str(owner_id),
        "feed_type": "personalized",
        "offset": 20,
        "scan_offset": 22,
        "expires_at": (NOW + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
    }
    value.update(updates)
    return value


def test_cursor_round_trip_binds_user_feed_snapshot_and_scan_progress() -> None:
    user_id = uuid.uuid4()
    state = CursorState(
        snapshot_id=uuid.uuid4(),
        user_id=user_id,
        feed_type="personalized",
        offset=4,
        scan_offset=7,
        expires_at=NOW + timedelta(minutes=5),
    )
    codec = CursorCodec(SECRET)
    assert (
        codec.decode(codec.encode(state), user_id=user_id, feed_type="personalized", now=NOW)
        == state
    )
    with pytest.raises(CursorError, match="does not belong"):
        codec.decode(codec.encode(state), user_id=uuid.uuid4(), feed_type="personalized", now=NOW)
    with pytest.raises(CursorError, match="does not belong"):
        codec.decode(codec.encode(state), user_id=user_id, feed_type="popular", now=NOW)


def test_cursor_rejects_tamper_expiry_non_integer_coercion_and_bounds() -> None:
    user_id = uuid.uuid4()
    codec = CursorCodec(SECRET)
    token = _signed(_payload(user_id))
    with pytest.raises(CursorError, match="signature"):
        codec.decode(
            token[:-1] + ("A" if token[-1] != "A" else "B"),
            user_id=user_id,
            feed_type="personalized",
            now=NOW,
        )
    with pytest.raises(CursorError, match="expired"):
        codec.decode(
            _signed(_payload(user_id, expires_at=(NOW - timedelta(seconds=1)).isoformat())),
            user_id=user_id,
            feed_type="personalized",
            now=NOW,
        )
    for invalid in (True, "3", 3.5, MAX_CURSOR_OFFSET + 1):
        with pytest.raises(CursorError, match="payload is invalid"):
            codec.decode(
                _signed(_payload(user_id, offset=invalid)),
                user_id=user_id,
                feed_type="personalized",
                now=NOW,
            )


def test_cursor_rejects_noncanonical_signature_alias_with_identical_decoded_bytes() -> None:
    user_id = uuid.uuid4()
    codec = CursorCodec(SECRET)
    token = _signed(_payload(user_id))
    payload_token, signature_token = token.split(".")
    final_index = BASE64URL_ALPHABET.index(signature_token[-1])
    assert final_index % 4 == 0
    alias = signature_token[:-1] + BASE64URL_ALPHABET[final_index + 1]
    assert base64.urlsafe_b64decode(alias + "=") == base64.urlsafe_b64decode(signature_token + "=")

    with pytest.raises(CursorError, match="signature"):
        codec.decode(
            f"{payload_token}.{alias}",
            user_id=user_id,
            feed_type="personalized",
            now=NOW,
        )


def test_cursor_rejects_non_string_identity_and_malformed_tokens() -> None:
    user_id = uuid.uuid4()
    codec = CursorCodec(SECRET)
    with pytest.raises(CursorError, match="payload is invalid"):
        codec.decode(
            _signed(_payload(user_id, user_id=[str(user_id)])),
            user_id=user_id,
            feed_type="personalized",
            now=NOW,
        )
    for token in ("", "abc", "***.***"):
        with pytest.raises(CursorError, match="malformed"):
            codec.decode(token, user_id=user_id, feed_type="personalized", now=NOW)
