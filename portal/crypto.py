"""Authenticated, per-account bound encryption. Key material is outside the database."""
import base64
import json
import secrets
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

class CredentialError(ValueError):
    pass

def encrypt(payload, account_id):
    key_id = settings.ACTIVE_CREDENTIAL_KEY
    key = base64.b64decode(settings.CREDENTIAL_KEYS[key_id], validate=True)
    nonce = secrets.token_bytes(12)
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
    ciphertext = AESGCM(key).encrypt(nonce, raw, f'auto-buddy:v1:{account_id}'.encode())
    return f'v1:{key_id}:' + base64.b64encode(nonce + ciphertext).decode()

def decrypt(value, account_id):
    try:
        version, key_id, encoded = value.split(':', 2)
        if version != 'v1':
            raise ValueError()
        key = base64.b64decode(settings.CREDENTIAL_KEYS[key_id], validate=True)
        raw = base64.b64decode(encoded, validate=True)
        plaintext = AESGCM(key).decrypt(raw[:12], raw[12:], f'auto-buddy:v1:{account_id}'.encode())
        return json.loads(plaintext)
    except Exception:
        raise CredentialError('凭证无法解密，请检查密钥配置或重新导入') from None
