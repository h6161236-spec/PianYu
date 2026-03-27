from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vcut_studio.asr import (
    FasterWhisperTranscriber,
    collect_asr_runtime_diagnostics,
    normalize_simplified_chinese,
    segment_from_whisper,
)
from vcut_studio.media import (
    build_waveform_points,
    parse_frame_rate,
    parse_media_info,
    parse_media_info_from_ffmpeg,
    preview_proxy_required,
    resolve_ffmpeg_path,
    resolve_ffprobe_path,
)
from vcut_studio.models import MediaInfo
from vcut_studio.settings import ASRSettings


class DummyWord:
    def __init__(self, word: str, start: float, end: float, probability: float) -> None:
        self.word = word
        self.start = start
        self.end = end
        self.probability = probability


class DummySegment:
    def __init__(self) -> None:
        self.start = 0.0
        self.end = 1.6
        self.text = "你好 世界"
        self.words = [
            DummyWord("你好", 0.0, 0.7, 0.95),
            DummyWord("世界", 0.8, 1.6, 0.91),
        ]


class MediaParsingTests(unittest.TestCase):
    def test_build_waveform_points_downsamples_peaks(self) -> None:
        points = build_waveform_points([0, 1000, -2000, 500, 3000, -4000], point_count=3)
        self.assertEqual(len(points), 3)
        self.assertGreater(points[2], points[1])
        self.assertGreater(points[1], points[0])

    def test_parse_frame_rate_fraction(self) -> None:
        self.assertAlmostEqual(parse_frame_rate("30000/1001"), 29.97002997, places=4)
        self.assertEqual(parse_frame_rate("0/0"), 0.0)
        self.assertEqual(parse_frame_rate("N/A"), 0.0)

    def test_parse_media_info(self) -> None:
        payload = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "25/1",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "48000",
                },
            ],
            "format": {"duration": "12.34"},
        }
        info = parse_media_info(payload)
        self.assertEqual(info.duration_ms, 12340)
        self.assertTrue(info.has_video)
        self.assertTrue(info.has_audio)
        self.assertEqual(info.width, 1920)
        self.assertEqual(info.height, 1080)
        self.assertEqual(info.video_codec, "h264")
        self.assertEqual(info.audio_codec, "aac")
        self.assertEqual(info.sample_rate, 48000)
        self.assertAlmostEqual(info.fps, 25.0)

    def test_parse_media_info_from_ffmpeg_output(self) -> None:
        stderr_text = """
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'demo.mp4':
  Duration: 00:00:12.34, start: 0.000000, bitrate: 122 kb/s
  Stream #0:0: Video: h264, yuv420p, 1920x1080, 25 fps, 25 tbr, 12800 tbn
  Stream #0:1: Audio: aac, 48000 Hz, stereo, fltp, 128 kb/s
"""
        info = parse_media_info_from_ffmpeg(stderr_text)
        self.assertEqual(info.duration_ms, 12340)
        self.assertTrue(info.has_video)
        self.assertTrue(info.has_audio)
        self.assertEqual(info.video_codec, "h264")
        self.assertEqual(info.audio_codec, "aac")
        self.assertEqual(info.width, 1920)
        self.assertEqual(info.sample_rate, 48000)

    def test_resolve_ffmpeg_and_ffprobe_from_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            ffmpeg_path = temp_root / "ffmpeg.exe"
            ffprobe_path = temp_root / "ffprobe.exe"
            ffmpeg_path.write_bytes(b"fake-ffmpeg")
            ffprobe_path.write_bytes(b"fake-ffprobe")

            self.assertEqual(resolve_ffmpeg_path(str(ffmpeg_path)), str(ffmpeg_path))
            self.assertEqual(resolve_ffprobe_path(str(ffmpeg_path)), str(ffprobe_path))

    def test_preview_proxy_required_for_non_ascii_path(self) -> None:
        info = MediaInfo(
            duration_ms=1000,
            has_video=True,
            has_audio=True,
            video_codec="h264",
            audio_codec="aac",
        )
        self.assertTrue(preview_proxy_required("C:/视频/培训.mp4", info))

    def test_preview_proxy_not_required_for_ascii_h264_aac(self) -> None:
        info = MediaInfo(
            duration_ms=1000,
            has_video=True,
            has_audio=True,
            video_codec="h264",
            audio_codec="aac",
        )
        self.assertFalse(preview_proxy_required("C:/videos/training.mp4", info))


class ASRConversionTests(unittest.TestCase):
    @patch("vcut_studio.asr._SIMPLIFIED_CHINESE_CONVERTER")
    def test_normalize_simplified_chinese_uses_converter_when_available(self, converter: object) -> None:
        converter.convert.return_value = "测试文字"
        self.assertEqual(normalize_simplified_chinese("測試文字"), "测试文字")

    def test_segment_from_whisper(self) -> None:
        segment = segment_from_whisper(DummySegment(), default_voice="emma_clear")
        self.assertEqual(segment.start_ms, 0)
        self.assertEqual(segment.end_ms, 1600)
        self.assertEqual(segment.zh_text, "你好 世界")
        self.assertEqual(len(segment.words), 2)
        self.assertEqual(segment.voice_id, "emma_clear")
        self.assertGreater(segment.words[0].confidence, 0.9)

    def test_loading_message_mentions_download_for_builtin_model(self) -> None:
        transcriber = FasterWhisperTranscriber(ASRSettings(model_name="small", device="cpu"))
        message = transcriber._loading_message("cpu", 3)
        self.assertIn("首次可能在下载", message)
        self.assertIn("small", message)

    def test_loading_message_warns_for_medium_on_cpu(self) -> None:
        transcriber = FasterWhisperTranscriber(ASRSettings(model_name="medium", device="cpu"))
        message = transcriber._loading_message("cpu", 0)
        self.assertIn("建议改用 small", message)

    @patch("vcut_studio.asr.ctypes.WinDLL", side_effect=OSError("missing"))
    def test_auto_device_falls_back_to_cpu_when_cuda_runtime_missing(self, _mock_windll: object) -> None:
        transcriber = FasterWhisperTranscriber(ASRSettings(model_name="small", device="auto"))
        self.assertEqual(transcriber._resolved_device(), "cpu")

    def test_model_reference_prefers_local_model_dir(self) -> None:
        transcriber = FasterWhisperTranscriber(
            ASRSettings(
                model_name="small",
                device="cpu",
                local_model_dir="D:/models/faster-whisper-medium",
            )
        )
        self.assertEqual(Path(transcriber._model_reference()), Path("D:/models/faster-whisper-medium"))

    def test_model_reference_resolves_relative_local_model_dir_from_runtime_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime_root = Path(tmp_dir)
            exe_path = runtime_root / "VCutStudio.exe"
            model_dir = runtime_root / "models" / "asr" / "medium"
            exe_path.write_bytes(b"")
            model_dir.mkdir(parents=True)

            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(exe_path)),
            ):
                transcriber = FasterWhisperTranscriber(
                    ASRSettings(
                        model_name="small",
                        device="cpu",
                        local_model_dir="models/asr/medium",
                    )
                )
                self.assertEqual(Path(transcriber._model_reference()), model_dir.resolve())

    @patch("vcut_studio.asr.package_version", side_effect=["1.1.0", "4.7.1"])
    @patch("vcut_studio.asr.list_nvidia_gpu_names", return_value=(True, ["NVIDIA GeForce RTX 4060"]))
    @patch("vcut_studio.asr.detect_cuda_device_count", return_value=1)
    @patch("vcut_studio.asr.missing_cuda_runtime_libraries", return_value=["cublas64_12.dll"])
    def test_collect_asr_runtime_diagnostics_reports_missing_cuda_runtime(
        self,
        _missing_cuda_runtime_libraries: object,
        _detect_cuda_device_count: object,
        _list_nvidia_gpu_names: object,
        _package_version: object,
    ) -> None:
        diagnostics = collect_asr_runtime_diagnostics("auto")
        self.assertEqual(diagnostics.cuda_device_count, 1)
        self.assertEqual(diagnostics.missing_cuda_libraries, ["cublas64_12.dll"])
        self.assertFalse(diagnostics.gpu_ready)
        self.assertTrue(diagnostics.nvidia_smi_available)

    @patch("vcut_studio.asr.package_version", side_effect=["1.1.0", "4.7.1"])
    @patch("vcut_studio.asr.list_nvidia_gpu_names", return_value=(False, []))
    @patch("vcut_studio.asr.detect_cuda_device_count", return_value=0)
    def test_collect_asr_runtime_diagnostics_skips_cuda_runtime_checks_without_gpu(
        self,
        _detect_cuda_device_count: object,
        _list_nvidia_gpu_names: object,
        _package_version: object,
    ) -> None:
        diagnostics = collect_asr_runtime_diagnostics("cpu")
        self.assertEqual(diagnostics.cuda_device_count, 0)
        self.assertEqual(diagnostics.missing_cuda_libraries, [])
        self.assertFalse(diagnostics.gpu_ready)


if __name__ == "__main__":
    unittest.main()
