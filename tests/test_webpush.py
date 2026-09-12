"""PR-AA: Web Push crypto and sender.

The encryption is pinned to RFC 8291 Appendix A — same keys, salt and
plaintext, byte-identical output — so a regression here is a failing
test rather than notifications that silently never arrive. The random
path is checked with an independent decrypt written against RFC 8188.
"""

from __future__ import annotations

import json
import struct

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from kodji.config import reset_settings_cache
from kodji.models import PushSubscription
from kodji.services import webpush
from kodji.services.webpush import (
    MAX_PAYLOAD_BYTES,
    WebPushSender,
    audience_for,
    b64url_decode,
    b64url_encode,
    encrypt,
    generate_vapid_keys,
    public_key_for,
    vapid_authorization,
    vapid_jwt,
)

# RFC 8291 Appendix A.
UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
SALT = "DGv6ra1nlYgDCS1FRnbzlw"
PLAINTEXT = b"When I grow up, I want to be a watermelon"
EXPECTED = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
)


def _decrypt(body: bytes, ua_private_b64: str, auth_b64: str) -> bytes:
    """The receiver side, per RFC 8188 §2 and RFC 8291 §3 — written
    independently of `encrypt` so the two can disagree."""
    salt, rs, idlen = body[:16], struct.unpack(">I", body[16:20])[0], body[20]
    assert rs == 4096
    as_public_raw = body[21 : 21 + idlen]
    ciphertext = body[21 + idlen :]
    ua_priv = ec.derive_private_key(int.from_bytes(b64url_decode(ua_private_b64), "big"), ec.SECP256R1())
    ua_public_raw = ua_priv.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    as_public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public_raw)
    shared = ua_priv.exchange(ec.ECDH(), as_public)

    def hkdf(s, ikm, info, n):
        return HKDF(algorithm=hashes.SHA256(), length=n, salt=s, info=info).derive(ikm)

    ikm = hkdf(b64url_decode(auth_b64), shared, b"WebPush: info\x00" + ua_public_raw + as_public_raw, 32)
    cek = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    record = AESGCM(cek).decrypt(nonce, ciphertext, None)
    # Strip padding: last record ends with 0x02 then zero bytes.
    stripped = record.rstrip(b"\x00")
    assert stripped.endswith(b"\x02")
    return stripped[:-1]


class TestEncrypt:
    def test_matches_rfc_8291_appendix_a(self):
        body = encrypt(
            PLAINTEXT, UA_PUBLIC, AUTH, salt=b64url_decode(SALT), as_private_b64url=AS_PRIVATE,
        )
        assert b64url_encode(body) == EXPECTED

    def test_random_salt_and_key_round_trip(self):
        one = encrypt(b'{"title":"SNTS +5%"}', UA_PUBLIC, AUTH)
        two = encrypt(b'{"title":"SNTS +5%"}', UA_PUBLIC, AUTH)
        assert one != two  # fresh salt + ephemeral key per message
        assert _decrypt(one, UA_PRIVATE, AUTH) == b'{"title":"SNTS +5%"}'
        assert _decrypt(two, UA_PRIVATE, AUTH) == b'{"title":"SNTS +5%"}'

    def test_header_layout(self):
        body = encrypt(b"x", UA_PUBLIC, AUTH)
        assert struct.unpack(">I", body[16:20])[0] == 4096
        assert body[20] == 65
        assert body[21] == 0x04  # uncompressed point marker
        assert len(body) == 16 + 4 + 1 + 65 + len(b"x\x02") + 16

    def test_refuses_oversize_payload(self):
        with pytest.raises(ValueError, match="too large"):
            encrypt(b"x" * (MAX_PAYLOAD_BYTES + 1), UA_PUBLIC, AUTH)

    @pytest.mark.parametrize(
        "p256dh,auth",
        [
            ("AAAA", AUTH),                         # not a point
            (UA_PUBLIC, "c2hvcnQ"),                 # auth too short
            (b64url_encode(b"\x02" + b"\x00" * 64), AUTH),  # compressed marker
        ],
    )
    def test_refuses_malformed_client_keys(self, p256dh, auth):
        with pytest.raises(ValueError):
            encrypt(b"x", p256dh, auth)


class TestVapid:
    def test_keygen_pair_is_consistent(self):
        private, public = generate_vapid_keys()
        assert len(b64url_decode(private)) == 32
        assert len(b64url_decode(public)) == 65
        assert public_key_for(private) == public

    def test_jwt_is_es256_with_the_right_claims_and_verifies(self):
        private, public = generate_vapid_keys()
        token = vapid_jwt("https://fcm.googleapis.com", "mailto:ops@kodji.app", private, now=1_800_000_000)
        h, c, sig = token.split(".")
        assert json.loads(b64url_decode(h)) == {"typ": "JWT", "alg": "ES256"}
        claims = json.loads(b64url_decode(c))
        assert claims == {
            "aud": "https://fcm.googleapis.com",
            "exp": 1_800_000_000 + 12 * 3600,
            "sub": "mailto:ops@kodji.app",
        }
        raw = b64url_decode(sig)
        assert len(raw) == 64  # r‖s, not DER
        r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64url_decode(public))
        pub.verify(encode_dss_signature(r, s), f"{h}.{c}".encode(), ec.ECDSA(hashes.SHA256()))

    def test_authorization_header_shape(self):
        private, public = generate_vapid_keys()
        value = vapid_authorization("https://updates.push.services.mozilla.com", "mailto:x@y", private, public)
        assert value.startswith("vapid t=")
        assert value.endswith(f", k={public}")

    @pytest.mark.parametrize(
        "endpoint,aud",
        [
            ("https://fcm.googleapis.com/fcm/send/abc:def", "https://fcm.googleapis.com"),
            ("https://web.push.apple.com/QW9ye", "https://web.push.apple.com"),
            ("https://push.example:8443/x", "https://push.example:8443"),
        ],
    )
    def test_audience_is_the_origin(self, endpoint, aud):
        assert audience_for(endpoint) == aud

    def test_audience_rejects_relative(self):
        with pytest.raises(ValueError):
            audience_for("/not/a/url")


def _sub(endpoint: str = "https://push.example/sub/SECRET-PATH") -> PushSubscription:
    return PushSubscription(id=1, user_id=1, endpoint=endpoint, p256dh=UA_PUBLIC, auth=AUTH)


def _sender(handler) -> WebPushSender:
    private, public = generate_vapid_keys()
    return WebPushSender(
        private_key=private,
        public_key=public,
        subject="mailto:ops@kodji.app",
        ttl_s=3600,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


class TestSender:
    def test_posts_an_encrypted_vapid_signed_request(self):
        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(201)

        result = _sender(handler).send(_sub(), {"title": "SNTS +5.0%", "body": "39 275 XOF"})
        assert result.ok
        req = seen[0]
        assert req.method == "POST"
        assert str(req.url) == "https://push.example/sub/SECRET-PATH"
        assert req.headers["content-encoding"] == "aes128gcm"
        assert req.headers["content-type"] == "application/octet-stream"
        assert req.headers["ttl"] == "3600"
        assert req.headers["authorization"].startswith("vapid t=")
        body = req.content
        assert struct.unpack(">I", body[16:20])[0] == 4096
        assert json.loads(_decrypt(body, UA_PRIVATE, AUTH)) == {"title": "SNTS +5.0%", "body": "39 275 XOF"}

    @pytest.mark.parametrize(
        "status,ok,permanent,gone",
        [
            (200, True, False, False),
            (201, True, False, False),
            (404, False, True, True),
            (410, False, True, True),
            (400, False, True, False),
            (401, False, True, False),
            (413, False, True, False),
            (429, False, False, False),
            (500, False, False, False),
            (503, False, False, False),
        ],
    )
    def test_status_classification(self, status, ok, permanent, gone):
        result = _sender(lambda req: httpx.Response(status)).send(_sub(), {"title": "x"})
        assert (result.ok, result.permanent, result.gone) == (ok, permanent, gone)
        if not ok:
            assert result.note == f"http_{status}"

    def test_transport_error_note_never_contains_the_endpoint(self):
        """The endpoint is a capability URL; httpx puts it in exception
        text, and the note is logged."""

        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"connect failed for {req.url}")

        result = _sender(handler).send(_sub(), {"title": "x"})
        assert result.ok is False
        assert "SECRET-PATH" not in result.note
        assert result.note.startswith("transport_error")

    def test_corrupt_subscription_is_reported_gone(self):
        bad = PushSubscription(id=9, user_id=1, endpoint="https://push.example/x", p256dh="nope", auth="nope")
        result = _sender(lambda req: httpx.Response(201)).send(bad, {"title": "x"})
        assert result.gone and result.permanent and not result.ok

    def test_vapid_token_is_reused_per_origin(self):
        auths: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            auths.append(req.headers["authorization"])
            return httpx.Response(201)

        sender = _sender(handler)
        sender.send(_sub("https://push.example/a"), {"title": "1"})
        sender.send(_sub("https://push.example/b"), {"title": "2"})
        sender.send(_sub("https://other.example/c"), {"title": "3"})
        assert auths[0] == auths[1]
        assert auths[2] != auths[0]


class TestSettings:
    def test_no_sender_without_keys(self, monkeypatch):
        monkeypatch.setenv("VAPID_PUBLIC_KEY", "")
        monkeypatch.setenv("VAPID_PRIVATE_KEY", "")
        reset_settings_cache()
        assert webpush.sender_from_settings() is None

    @pytest.mark.parametrize(
        "subject,reply_to,email_from,expected",
        [
            ("https://kodji.app", "", "", "https://kodji.app"),
            ("", "support@kodji.app", "", "mailto:support@kodji.app"),
            # EMAIL_FROM carries a display name; `mailto:Kodji <x@y>` is
            # rejected by Apple's push service, so it must be unwrapped.
            ("", "", "Kodji <connexion@mail.kodji.app>", "mailto:connexion@mail.kodji.app"),
            ("", "", "plain@kodji.app", "mailto:plain@kodji.app"),
        ],
    )
    def test_vapid_subject_resolution(self, monkeypatch, subject, reply_to, email_from, expected):
        from kodji.config import settings

        monkeypatch.setenv("VAPID_SUBJECT", subject)
        monkeypatch.setenv("EMAIL_REPLY_TO", reply_to)
        monkeypatch.setenv("EMAIL_FROM", email_from)
        reset_settings_cache()
        assert settings.vapid_subject_effective == expected
        reset_settings_cache()

    def test_sender_from_settings(self, monkeypatch):
        private, public = generate_vapid_keys()
        monkeypatch.setenv("VAPID_PUBLIC_KEY", public)
        monkeypatch.setenv("VAPID_PRIVATE_KEY", private)
        monkeypatch.setenv("VAPID_SUBJECT", "")
        monkeypatch.setenv("EMAIL_REPLY_TO", "support@kodji.app")
        reset_settings_cache()
        sender = webpush.sender_from_settings()
        assert sender is not None
        assert sender.subject == "mailto:support@kodji.app"
        sender.close()
        reset_settings_cache()
