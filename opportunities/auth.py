"""Signed headers for the authenticated websocket handshakes. Both venues sign timestamp + method + path."""

import base64
import time
from functools import cache

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

import config


def kalshi_configured() -> bool:
    return bool(config.KALSHI_API_KEY_ID) and config.KALSHI_PRIVATE_KEY_PATH.is_file()


def polymarket_configured() -> bool:
    return bool(config.POLYMARKET_API_KEY_ID and config.POLYMARKET_API_SECRET)


def kalshi_headers(method: str, path: str) -> dict[str, str]:
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method}{path}".encode()
    key = _kalshi_key()
    if isinstance(key, rsa.RSAPrivateKey):
        signature = key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    elif isinstance(key, ed25519.Ed25519PrivateKey):
        signature = key.sign(message)
    else:
        raise ValueError(f"unsupported Kalshi key type: {type(key).__name__}")
    return {
        "KALSHI-ACCESS-KEY": config.KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
    }


def polymarket_headers(method: str, path: str) -> dict[str, str]:
    timestamp = str(int(time.time() * 1000))
    signature = _polymarket_key().sign(f"{timestamp}{method}{path}".encode())
    return {
        "X-PM-Access-Key": config.POLYMARKET_API_KEY_ID,
        "X-PM-Timestamp": timestamp,
        "X-PM-Signature": base64.b64encode(signature).decode(),
    }


@cache
def _kalshi_key():
    return serialization.load_pem_private_key(config.KALSHI_PRIVATE_KEY_PATH.read_bytes(), password=None)


@cache
def _polymarket_key() -> ed25519.Ed25519PrivateKey:
    try:
        secret = base64.b64decode(config.POLYMARKET_API_SECRET, validate=True)
    except ValueError as e:
        raise ValueError(f"POLYMARKET_API_SECRET in .env isn't valid base64 ({e}); paste the secret key exactly") from None
    return ed25519.Ed25519PrivateKey.from_private_bytes(secret[:32])
