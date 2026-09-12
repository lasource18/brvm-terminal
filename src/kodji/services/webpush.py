"""Web Push: message encryption (RFC 8291), VAPID (RFC 8292), one sender.

Written against the RFCs with `cryptography` rather than pulling in
`pywebpush`, which brings `requests` and two helper packages for what is
~120 lines: an ECDH agreement, two HKDF derivations, one AES-128-GCM
seal, and an ES256 JWT. `tests/test_webpush.py` pins the RFC 8291
Appendix A vector, so a wrong byte anywhere fails the suite rather than
producing notifications that silently never arrive.

Vocabulary, as in the RFCs:

* **UA** — the user agent (the subscriber's browser). Its ECDH public key
  is `p256dh` and its 16-byte secret is `auth`; both come from
  `PushSubscription.toJSON()` on the client.
* **AS** — the application server (us). A fresh ephemeral key per
  message; the public half rides in the body header.
* **VAPID** — our long-lived P-256 key pair. The push service learns the
  public key from the subscription, and every request carries a JWT
  signed with the private key, so a leaked endpoint URL alone cannot be
  used to spam the device.

Key formats on the wire and in `.env` are the ones every browser and
push library uses: base64url without padding; the public key is the
65-byte uncompressed point, the private key the raw 32-byte scalar.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from kodji.config import settings
from kodji.logging import get
from kodji.models import PushSubscription

log = get(__name__)

_CURVE = ec.SECP256R1()
# RFC 8188 record size. One record carries the whole message; a payload
# larger than this would need chunking, which push services refuse anyway
# (they cap the body at 4 KB).
_RECORD_SIZE = 4096
MAX_PAYLOAD_BYTES = _RECORD_SIZE - 16 - 1 - 86  # tag, delimiter, header
# A VAPID token is good for at most 24 h (RFC 8292 §2); 12 h leaves room
# for clock skew at the push service.
_JWT_LIFETIME_S = 12 * 3600


# ---------------------------------------------------------------------------
# base64url helpers
# ---------------------------------------------------------------------------


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(s: str) -> bytes:
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def _private_from_raw(raw: bytes) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(raw, "big"), _CURVE)


def _public_raw(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def _public_from_raw(raw: bytes) -> ec.EllipticCurvePublicKey:
    return ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, raw)


def generate_vapid_keys() -> tuple[str, str]:
    """A fresh VAPID pair as `(private_b64url, public_b64url)` — what
    `just vapid-keygen` prints for `.env`."""
    priv = ec.generate_private_key(_CURVE)
    raw_priv = priv.private_numbers().private_value.to_bytes(32, "big")
    return b64url_encode(raw_priv), b64url_encode(_public_raw(priv.public_key()))


def public_key_for(private_b64url: str) -> str:
    """The public half implied by a private key, for checking `.env`
    carries a matching pair."""
    priv = _private_from_raw(b64url_decode(private_b64url))
    return b64url_encode(_public_raw(priv.public_key()))


# ---------------------------------------------------------------------------
# RFC 8291 — message encryption, aes128gcm content coding (RFC 8188)
# ---------------------------------------------------------------------------


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(
    plaintext: bytes,
    ua_public_b64url: str,
    auth_b64url: str,
    *,
    salt: bytes | None = None,
    as_private_b64url: str | None = None,
) -> bytes:
    """Seal `plaintext` for one subscription. Returns the full request
    body: salt · rs · key id · ciphertext (RFC 8188 §2.1).

    `salt` and `as_private_b64url` are injectable so the RFC test vector
    can pin the output; production leaves them None for fresh randomness
    per message.
    """
    if len(plaintext) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload too large for one push record: {len(plaintext)} bytes")
    ua_public_raw = b64url_decode(ua_public_b64url)
    auth_secret = b64url_decode(auth_b64url)
    if len(ua_public_raw) != 65 or ua_public_raw[0] != 0x04:
        raise ValueError("p256dh is not a 65-byte uncompressed P-256 point")
    if len(auth_secret) != 16:
        raise ValueError("auth secret is not 16 bytes")

    ua_public = _public_from_raw(ua_public_raw)
    as_private = (
        _private_from_raw(b64url_decode(as_private_b64url))
        if as_private_b64url
        else ec.generate_private_key(_CURVE)
    )
    as_public_raw = _public_raw(as_private.public_key())
    salt = salt if salt is not None else os.urandom(16)
    if len(salt) != 16:
        raise ValueError("salt is not 16 bytes")

    ecdh_secret = as_private.exchange(ec.ECDH(), ua_public)
    # RFC 8291 §3.3 to §3.4: auth secret salts the ECDH secret into the IKM,
    # then the aes128gcm salt derives the content key and nonce from it.
    key_info = b"WebPush: info\x00" + ua_public_raw + as_public_raw
    ikm = _hkdf(auth_secret, ecdh_secret, key_info, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)

    # Single (last) record: plaintext, the 0x02 delimiter, no padding.
    record = plaintext + b"\x02"
    ciphertext = AESGCM(cek).encrypt(nonce, record, None)

    header = salt + struct.pack(">I", _RECORD_SIZE) + bytes([len(as_public_raw)]) + as_public_raw
    return header + ciphertext


# ---------------------------------------------------------------------------
# RFC 8292 — VAPID
# ---------------------------------------------------------------------------


def audience_for(endpoint: str) -> str:
    """`scheme://host[:port]` of the push service — the JWT `aud` claim."""
    parts = urlsplit(endpoint)
    if not parts.scheme or not parts.netloc:
        raise ValueError("endpoint is not an absolute URL")
    return f"{parts.scheme}://{parts.netloc}"


def vapid_jwt(
    audience: str,
    subject: str,
    private_b64url: str,
    *,
    now: float | None = None,
    lifetime_s: int = _JWT_LIFETIME_S,
) -> str:
    """A signed ES256 token for one push-service origin."""
    priv = _private_from_raw(b64url_decode(private_b64url))
    exp = int(now if now is not None else time.time()) + lifetime_s
    header = b64url_encode(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64url_encode(
        json.dumps({"aud": audience, "exp": exp, "sub": subject}, separators=(",", ":")).encode()
    )
    signing_input = f"{header}.{claims}".encode("ascii")
    der = priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    # JWS wants the raw r‖s pair, not the DER structure cryptography emits.
    r, s = decode_dss_signature(der)
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{header}.{claims}.{b64url_encode(sig)}"


def vapid_authorization(
    audience: str,
    subject: str,
    private_b64url: str,
    public_b64url: str,
    *,
    now: float | None = None,
) -> str:
    """The `Authorization` header value (RFC 8292 §3): `vapid t=<jwt>, k=<key>`."""
    return f"vapid t={vapid_jwt(audience, subject, private_b64url, now=now)}, k={public_b64url}"


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PushResult:
    """Outcome of one send to one device.

    `permanent` follows the alert-delivery convention (a 4xx that will
    never succeed). `gone` is the subset where the push service says the
    subscription itself is dead (404/410) — the caller should delete it.
    """

    ok: bool
    note: str = ""
    permanent: bool = False
    gone: bool = False


@dataclass
class WebPushSender:
    """POSTs encrypted notifications with a VAPID-signed request.

    One instance per delivery pass: the VAPID token is cached per push
    service origin for its lifetime, so a batch to fifty Chrome devices
    signs once.
    """

    private_key: str
    public_key: str
    subject: str
    ttl_s: int = 12 * 3600
    client: httpx.Client | None = None
    _owned: bool = field(default=False, init=False, repr=False)
    _tokens: dict[str, tuple[float, str]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = httpx.Client(timeout=settings.http_timeout_s)
            self._owned = True

    def close(self) -> None:
        if self._owned and self.client is not None:
            self.client.close()

    def _authorization(self, audience: str) -> str:
        now = time.time()
        cached = self._tokens.get(audience)
        # Re-sign with an hour to spare; a token that expires mid-pass
        # would turn the tail of the batch into 401s.
        if cached and cached[0] - now > 3600:
            return cached[1]
        header = vapid_authorization(
            audience, self.subject, self.private_key, self.public_key, now=now
        )
        self._tokens[audience] = (now + _JWT_LIFETIME_S, header)
        return header

    def send(self, sub: PushSubscription, payload: dict[str, object]) -> PushResult:
        """Encrypt `payload` as JSON and POST it to the subscription.

        Never puts the endpoint in the returned note: httpx embeds the
        URL in exception messages and the caller logs the note. The
        endpoint is a capability URL — anyone holding it can push to the
        device (VAPID aside) — so it stays out of the journal.
        """
        assert self.client is not None
        try:
            audience = audience_for(sub.endpoint)
            body = encrypt(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                sub.p256dh,
                sub.auth,
            )
        except (ValueError, TypeError) as e:
            # A corrupt row will never encrypt; drop it from the fan-out.
            return PushResult(ok=False, note=f"bad_subscription: {type(e).__name__}", permanent=True, gone=True)
        headers = {
            "Authorization": self._authorization(audience),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(self.ttl_s),
            "Urgency": "normal",
        }
        try:
            resp = self.client.post(sub.endpoint, content=body, headers=headers)
        except httpx.HTTPError as e:
            return PushResult(ok=False, note=f"transport_error: {type(e).__name__}")
        code = resp.status_code
        if code in (200, 201, 202):
            return PushResult(ok=True, note="ok")
        if code in (404, 410):
            # The push service has forgotten this subscription: the user
            # revoked permission, cleared site data, or the browser rotated
            # it (then `pushsubscriptionchange` re-registers the new one).
            return PushResult(ok=False, note=f"http_{code}", permanent=True, gone=True)
        if code == 429 or code >= 500:
            return PushResult(ok=False, note=f"http_{code}")
        # 400 bad request, 401/403 VAPID rejected, 413 too large: none of
        # these clear up on retry.
        return PushResult(ok=False, note=f"http_{code}", permanent=True)


def sender_from_settings(client: httpx.Client | None = None) -> WebPushSender | None:
    """The production sender, or None when VAPID keys are not configured."""
    if not settings.has_push:
        return None
    return WebPushSender(
        private_key=settings.vapid_private_key,
        public_key=settings.vapid_public_key,
        subject=settings.vapid_subject_effective,
        ttl_s=settings.push_ttl_s,
        client=client,
    )
