import base64
from unittest.mock import patch, MagicMock  # noqa: F401
import pytest
from subtitles.tools.translate.services.encryption import encrypt_api_key, validate_encryption_key


class TestValidateEncryptionKey:
    def test_valid_64_hex(self):
        assert validate_encryption_key("79866a5b6ef41b78681a7f774f6628fe66c49b0f0c96808cc3f48acbbfe1ac41") is True

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
    KEY_HEX = "79866a5b6ef41b78681a7f774f6628fe66c49b0f0c96808cc3f48acbbfe1ac41"
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


class TestGetApiKeyValue:
    """Test the _get_api_key_value helper on OpenRouterTranslatorService."""

    KEY_HEX = "79866a5b6ef41b78681a7f774f6628fe66c49b0f0c96808cc3f48acbbfe1ac41"
    API_KEY = "sk-or-v1-test1234567890"

    def _make_service(self):
        from subtitles.tools.translate.services.openrouter_translator import OpenRouterTranslatorService
        return OpenRouterTranslatorService(
            source_srt_file="", dest_srt_file="", lang_obj=None,
            to_lang="hun", from_lang="en", media_type="series",
            video_path="", orig_to_lang="hu", forced=False, hi=False,
            sonarr_series_id=None, sonarr_episode_id=None, radarr_id=None,
        )

    @patch("subtitles.tools.translate.services.openrouter_translator.settings")
    def test_no_encryption_key_returns_plaintext(self, mock_settings):
        mock_settings.translator.openrouter_api_key = self.API_KEY
        mock_settings.translator.openrouter_encryption_key = ""
        svc = self._make_service()
        result = svc._get_api_key_value()
        assert result == self.API_KEY

    @patch("subtitles.tools.translate.services.openrouter_translator.settings")
    def test_with_encryption_key_returns_encrypted(self, mock_settings):
        mock_settings.translator.openrouter_api_key = self.API_KEY
        mock_settings.translator.openrouter_encryption_key = self.KEY_HEX
        svc = self._make_service()
        result = svc._get_api_key_value()
        assert result.startswith("enc:")

    @patch("subtitles.tools.translate.services.openrouter_translator.settings")
    def test_with_encryption_key_roundtrips(self, mock_settings):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        mock_settings.translator.openrouter_api_key = self.API_KEY
        mock_settings.translator.openrouter_encryption_key = self.KEY_HEX
        svc = self._make_service()
        result = svc._get_api_key_value()
        payload = base64.b64decode(result[4:])
        aesgcm = AESGCM(bytes.fromhex(self.KEY_HEX))
        decrypted = aesgcm.decrypt(payload[:12], payload[12:], None).decode()
        assert decrypted == self.API_KEY

    @patch("subtitles.tools.translate.services.openrouter_translator.settings")
    def test_invalid_encryption_key_raises(self, mock_settings):
        mock_settings.translator.openrouter_api_key = self.API_KEY
        mock_settings.translator.openrouter_encryption_key = "not-valid-hex"
        svc = self._make_service()
        with pytest.raises(ValueError, match="Invalid encryption key format"):
            svc._get_api_key_value()
