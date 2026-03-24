import base64
import pytest
from subtitles.tools.translate.services.encryption import encrypt_api_key, validate_encryption_key


class TestValidateEncryptionKey:
    def test_valid_64_hex(self):
        assert validate_encryption_key("e2c65e79252379c1da0874f9d0928c6194fbc5e5460e3ab4047860560a2aa597") is True

    def test_empty_string(self):
        assert validate_encryption_key("") is False

    def test_too_short(self):
        assert validate_encryption_key("abcd1234") is False

    def test_too_long(self):
        assert validate_encryption_key("a" * 65) is False

    def test_non_hex_chars(self):
        assert validate_encryption_key("g" * 64) is False

    def test_uppercase_hex(self):
        assert validate_encryption_key("A" * 64) is True

    def test_mixed_case_hex(self):
        assert validate_encryption_key("aAbBcCdD" * 8) is True


class TestEncryptApiKey:
    KEY_HEX = "e2c65e79252379c1da0874f9d0928c6194fbc5e5460e3ab4047860560a2aa597"
    API_KEY = "sk-or-v1-test1234567890"

    def test_returns_enc_prefix(self):
        result = encrypt_api_key(self.API_KEY, self.KEY_HEX)
        assert result.startswith("enc:")

    def test_base64_payload(self):
        result = encrypt_api_key(self.API_KEY, self.KEY_HEX)
        payload = result[4:]  # strip "enc:"
        decoded = base64.b64decode(payload)
        # 12-byte nonce + ciphertext + 16-byte tag
        # ciphertext length = len(api_key bytes)
        assert len(decoded) == 12 + len(self.API_KEY.encode()) + 16

    def test_unique_nonces(self):
        r1 = encrypt_api_key(self.API_KEY, self.KEY_HEX)
        r2 = encrypt_api_key(self.API_KEY, self.KEY_HEX)
        assert r1 != r2  # different nonce each time

    def test_roundtrip_decrypt(self):
        """Verify we can decrypt what we encrypted."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        encrypted = encrypt_api_key(self.API_KEY, self.KEY_HEX)
        payload = base64.b64decode(encrypted[4:])
        nonce = payload[:12]
        ciphertext = payload[12:]
        aesgcm = AESGCM(bytes.fromhex(self.KEY_HEX))
        decrypted = aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
        assert decrypted == self.API_KEY

    def test_invalid_key_raises(self):
        with pytest.raises(ValueError):
            encrypt_api_key(self.API_KEY, "not-a-hex-key")

    def test_short_key_raises(self):
        with pytest.raises(ValueError):
            encrypt_api_key(self.API_KEY, "abcd1234")
