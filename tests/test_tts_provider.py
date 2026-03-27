from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from vcut_studio.providers.tts import (
    BuiltinVoiceCatalogProvider,
    InstalledVoice,
    KokoroLocalProvider,
    _decode_powershell_clixml,
    _powershell_utf8_expression,
    builtin_english_voices,
    create_tts_provider,
)
from vcut_studio.settings import TTSSettings


class TTSProviderTests(unittest.TestCase):
    def test_builtin_english_voices_include_presets(self) -> None:
        voice_ids = {voice.voice_id for voice in builtin_english_voices()}
        self.assertIn("emma_clear", voice_ids)
        self.assertIn("oliver_story", voice_ids)
        self.assertIn("maya_bright", voice_ids)
        self.assertIn("henry_anchor", voice_ids)

    def test_create_builtin_tts_provider(self) -> None:
        provider = create_tts_provider(TTSSettings(provider_type="builtin_voice_catalog"))
        self.assertEqual(provider.provider_name, "builtin_voice_catalog")

    @patch("vcut_studio.providers.tts.find_sherpa_onnx_tts_executable", return_value=Path("dummy/bin/sherpa-onnx-offline-tts.exe"))
    @patch("vcut_studio.providers.tts.find_kokoro_model_dir", return_value=Path("dummy/models/kokoro-en-v0_19"))
    def test_create_kokoro_local_tts_provider(self, _mock_model_dir: object, _mock_executable: object) -> None:
        provider = create_tts_provider(TTSSettings(provider_type="kokoro_local"))
        self.assertIsInstance(provider, KokoroLocalProvider)

    @patch(
        "vcut_studio.providers.tts.english_windows_voices",
        return_value=(
            InstalledVoice(name="Voice A", culture="en-US"),
            InstalledVoice(name="Voice B", culture="en-GB"),
        ),
    )
    def test_preset_voices_can_map_to_different_system_voices(self, _mocked_voices: object) -> None:
        provider = BuiltinVoiceCatalogProvider(rate=1.0)
        emma_voice_name, _ = provider._resolve_voice_name("emma_clear")
        oliver_voice_name, _ = provider._resolve_voice_name("oliver_story")
        self.assertNotEqual(emma_voice_name, oliver_voice_name)

    def test_powershell_utf8_expression_avoids_embedding_raw_smart_quote_text(self) -> None:
        expression = _powershell_utf8_expression("First, let’s test dubbing.")
        self.assertIn("FromBase64String", expression)
        self.assertNotIn("let’s", expression)

    def test_decode_powershell_clixml_flattens_error_text(self) -> None:
        raw = (
            '#< CLIXML\n'
            '<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
            '<S S="Error">Line 1_x000D__x000A_</S>'
            '<S S="Error">Line 2</S>'
            "</Objs>"
        )
        decoded = _decode_powershell_clixml(raw)
        self.assertIn("Line 1", decoded)
        self.assertIn("Line 2", decoded)


if __name__ == "__main__":
    unittest.main()
