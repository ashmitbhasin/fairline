import base64

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from tape.kalshi_auth import auth_headers, sign, signing_message


def test_message_strips_query_and_uppercases_method():
    assert signing_message("1703123456789", "get", "/trade-api/v2/portfolio/orders?limit=5") == (
        b"1703123456789GET/trade-api/v2/portfolio/orders"
    )


def test_rsa_pss_signature_verifies():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sig = base64.b64decode(sign(key, "1703123456789", "GET", "/trade-api/ws/v2"))
    key.public_key().verify(
        sig,
        b"1703123456789GET/trade-api/ws/v2",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_ed25519_signature_verifies():
    key = ed25519.Ed25519PrivateKey.generate()
    sig = base64.b64decode(sign(key, "1", "GET", "/trade-api/v2/portfolio/balance"))
    key.public_key().verify(sig, b"1GET/trade-api/v2/portfolio/balance")


def test_headers_shape():
    key = ed25519.Ed25519PrivateKey.generate()
    h = auth_headers("key-id", key, "GET", "/trade-api/ws/v2", now_ms=1703123456789)
    assert h["KALSHI-ACCESS-KEY"] == "key-id"
    assert h["KALSHI-ACCESS-TIMESTAMP"] == "1703123456789"
    assert base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
