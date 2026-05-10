from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vcut_studio.runtime_assets import find_kokoro_model_dir, find_melo_model_dir


def _create_dummy_kokoro_model(root: Path, model_name: str) -> Path:
    model_dir = root / "models" / "tts" / model_name
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "model.onnx").write_bytes(b"dummy")
    (model_dir / "voices.bin").write_bytes(b"dummy")
    (model_dir / "tokens.txt").write_text("dummy", encoding="utf-8")
    (model_dir / "espeak-ng-data").mkdir(exist_ok=True)
    return model_dir


def _create_dummy_melo_model(root: Path, model_name: str = "vits-melo-tts-zh_en") -> Path:
    model_dir = root / "models" / "tts" / model_name
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "model.onnx").write_bytes(b"dummy")
    (model_dir / "lexicon.txt").write_text("dummy", encoding="utf-8")
    (model_dir / "tokens.txt").write_text("dummy", encoding="utf-8")
    (model_dir / "date.fst").write_bytes(b"dummy")
    (model_dir / "number.fst").write_bytes(b"dummy")
    (model_dir / "phone.fst").write_bytes(b"dummy")
    return model_dir


class RuntimeAssetsTests(unittest.TestCase):
    def test_find_kokoro_model_dir_prefers_v1_for_english(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            english_model = _create_dummy_kokoro_model(root, "kokoro-multi-lang-v1_0")
            _create_dummy_kokoro_model(root, "csukuangfj-kokoro-multi-lang-v1_1")
            with patch("vcut_studio.runtime_assets.runtime_base_dirs", return_value=[root]):
                self.assertEqual(find_kokoro_model_dir(language="en"), english_model)
                self.assertEqual(find_kokoro_model_dir(preferred_voice_id="am_eric"), english_model)

    def test_find_kokoro_model_dir_prefers_v1_1_for_chinese(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            chinese_model = _create_dummy_kokoro_model(root, "csukuangfj-kokoro-multi-lang-v1_1")
            _create_dummy_kokoro_model(root, "kokoro-multi-lang-v1_0")
            with patch("vcut_studio.runtime_assets.runtime_base_dirs", return_value=[root]):
                self.assertEqual(find_kokoro_model_dir(language="zh"), chinese_model)
                self.assertEqual(find_kokoro_model_dir(preferred_voice_id="zf_001"), chinese_model)

    def test_find_kokoro_model_dir_keeps_v1_1_for_v1_1_english_voice_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            v1_1_model = _create_dummy_kokoro_model(root, "csukuangfj-kokoro-multi-lang-v1_1")
            _create_dummy_kokoro_model(root, "kokoro-multi-lang-v1_0")
            with patch("vcut_studio.runtime_assets.runtime_base_dirs", return_value=[root]):
                self.assertEqual(find_kokoro_model_dir(preferred_voice_id="af_maple"), v1_1_model)

    def test_find_melo_model_dir_prefers_official_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            melo_model = _create_dummy_melo_model(root)
            with patch("vcut_studio.runtime_assets.runtime_base_dirs", return_value=[root]):
                self.assertEqual(find_melo_model_dir(), melo_model)


if __name__ == "__main__":
    unittest.main()
