from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vcut_studio.providers.tts import (
    BuiltinVoiceCatalogProvider,
    InstalledVoice,
    KokoroLocalProvider,
    MeloLocalProvider,
    _decode_powershell_clixml,
    _powershell_utf8_expression,
    builtin_english_voices,
    create_tts_provider,
    kokoro_local_voices,
    tts_provider_choices,
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

    def test_kokoro_default_english_voice_uses_curated_bella(self) -> None:
        self.assertEqual(TTSSettings(provider_type="kokoro_local").default_voice_for_language("en"), "af_bella")
        self.assertEqual(TTSSettings(provider_type="melo_local").default_voice_for_language("zh"), "melo_zh_female")

    @patch("vcut_studio.providers.tts.find_sherpa_onnx_tts_executable", return_value=Path("dummy/bin/sherpa-onnx-offline-tts.exe"))
    @patch("vcut_studio.providers.tts.find_kokoro_model_dir", return_value=Path("dummy/models/kokoro-en-v0_19"))
    def test_create_kokoro_local_tts_provider(self, _mock_model_dir: object, _mock_executable: object) -> None:
        provider = create_tts_provider(TTSSettings(provider_type="kokoro_local"))
        self.assertIsInstance(provider, KokoroLocalProvider)

    @patch("vcut_studio.providers.tts.find_sherpa_onnx_tts_executable", return_value=Path("dummy/bin/sherpa-onnx-offline-tts.exe"))
    @patch("vcut_studio.providers.tts.find_melo_model_dir", return_value=Path("dummy/models/vits-melo-tts-zh_en"))
    def test_create_melo_local_tts_provider(self, _mock_model_dir: object, _mock_executable: object) -> None:
        provider = create_tts_provider(TTSSettings(provider_type="melo_local", default_voice="melo_zh_female"))
        self.assertIsInstance(provider, MeloLocalProvider)

    @patch("vcut_studio.providers.tts.find_kokoro_model_dir", return_value=Path("dummy/models/csukuangfj-kokoro-multi-lang-v1_1"))
    def test_kokoro_local_voices_use_hf_v1_1_chinese_inventory(self, _mock_model_dir: object) -> None:
        voices = kokoro_local_voices(language="zh")
        voice_ids = [voice.voice_id for voice in voices]
        self.assertEqual(voice_ids[:4], ["zf_001", "zf_002", "zf_003", "zf_004"])
        self.assertEqual(len(voices), 100)

    @patch("vcut_studio.providers.tts.find_kokoro_model_dir", return_value=Path("dummy/models/csukuangfj-kokoro-multi-lang-v1_1"))
    def test_kokoro_local_voices_use_hf_v1_1_english_inventory(self, _mock_model_dir: object) -> None:
        voices = kokoro_local_voices(language="en")
        voice_ids = {voice.voice_id for voice in voices}
        self.assertEqual(voice_ids, {"af_maple", "af_sol", "bf_vale"})
        self.assertEqual(len(voices), 3)

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

    @patch("vcut_studio.providers.tts.kokoro_local_ready", return_value=True)
    @patch("vcut_studio.providers.tts.melo_local_ready", return_value=False)
    def test_tts_provider_choices_include_kokoro_for_chinese_when_ready(
        self,
        _mock_melo_ready: object,
        _mock_kokoro_ready: object,
    ) -> None:
        chinese_choices = tts_provider_choices("zh")
        english_choices = tts_provider_choices("en")

        self.assertEqual(chinese_choices, [("Kokoro 多语言 v1.1 中文音色（推荐）", "kokoro_local")])
        self.assertEqual(len(english_choices), 1)
        self.assertEqual(english_choices[0][1], "kokoro_local")
        self.assertIn("Kokoro", english_choices[0][0])

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

    def test_kokoro_chinese_args_keep_zh_lang_for_chinese_only_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            model_dir = Path(tmp_dir)
            for filename in (
                "lexicon-zh.txt",
                "lexicon-us-en.txt",
                "lexicon-gb-en.txt",
                "date-zh.fst",
                "number-zh.fst",
                "phone-zh.fst",
            ):
                (model_dir / filename).write_text("dummy", encoding="utf-8")

            provider = object.__new__(KokoroLocalProvider)
            args = provider._optional_model_args(model_dir, "zf_001", "这是一个中文测试。")

        self.assertIn("--kokoro-lang=zh", args)
        self.assertIn(f"--kokoro-lexicon={model_dir / 'lexicon-zh.txt'}", args)

    def test_kokoro_chinese_args_allow_mixed_english_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            model_dir = Path(tmp_dir)
            for filename in (
                "lexicon-zh.txt",
                "lexicon-us-en.txt",
                "lexicon-gb-en.txt",
                "date-zh.fst",
                "number-zh.fst",
                "phone-zh.fst",
            ):
                (model_dir / filename).write_text("dummy", encoding="utf-8")

            provider = object.__new__(KokoroLocalProvider)
            args = provider._optional_model_args(
                model_dir,
                "zf_001",
                "这是 CNPC 的 ERP 培训，COGT 项目于 2026 年 3 月上线。",
            )

        self.assertNotIn("--kokoro-lang=zh", args)
        self.assertIn(
            "--kokoro-lexicon="
            + ",".join(
                str(model_dir / filename)
                for filename in ("lexicon-zh.txt", "lexicon-us-en.txt", "lexicon-gb-en.txt")
            ),
            args,
        )

    @patch("vcut_studio.providers.tts.kokoro_local_ready", return_value=True)
    @patch("vcut_studio.providers.tts.melo_local_ready", return_value=True)
    def test_tts_provider_choices_offer_kokoro_and_melo_for_chinese(
        self,
        _mock_melo_ready: object,
        _mock_kokoro_ready: object,
    ) -> None:
        chinese_choices = tts_provider_choices("zh")
        english_choices = tts_provider_choices("en")

        self.assertEqual(
            chinese_choices,
            [
                ("Kokoro 多语言 v1.1 中文音色（推荐）", "kokoro_local"),
                ("Melo 中文专用音色（女声）", "melo_local"),
            ],
        )
        self.assertEqual(len(english_choices), 1)
        self.assertEqual(english_choices[0][1], "kokoro_local")
        self.assertIn("Kokoro", english_choices[0][0])


if __name__ == "__main__":
    unittest.main()
