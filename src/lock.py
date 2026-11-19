"""페이지 데이터 비밀번호 잠금 (AES-256-GCM + PBKDF2) — 브라우저 WebCrypto 와 같은 방식"""
from __future__ import annotations

import base64
import gzip
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITER = 200_000


def _key(password: str, salt: bytes, it: int = ITER) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=it)
    return kdf.derive(password.encode("utf-8"))


def encrypt(obj, password: str, compress: bool = True) -> dict:
    salt, iv = os.urandom(16), os.urandom(12)
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if compress:  # 암호문은 압축이 안 되므로 암호화 전에 gzip
        raw = gzip.compress(raw, 9, mtime=0)
    ct = AESGCM(_key(password, salt)).encrypt(iv, raw, None)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    out = {"v": 1, "iter": ITER, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}
    if compress:
        out["z"] = "gzip"
    return out


def decrypt(enc: dict, password: str):
    """이전 실행 결과 재사용용. 비밀번호가 바뀌었으면 예외."""
    d = base64.b64decode
    raw = AESGCM(_key(password, d(enc["salt"]), enc.get("iter", ITER))).decrypt(d(enc["iv"]), d(enc["ct"]), None)
    if enc.get("z") == "gzip":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))
