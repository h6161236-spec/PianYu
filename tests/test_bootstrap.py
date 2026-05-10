from __future__ import annotations

import runpy
import sys
import unittest
from unittest.mock import patch


class BootstrapDispatchTests(unittest.TestCase):
    def test_bootstrap_dispatches_to_custom_ppt_export_flag(self) -> None:
        runner_called: list[bool] = []
        app_called: list[bool] = []

        def _fake_runner_main() -> int:
            runner_called.append(True)
            return 29

        def _fake_app_main() -> int:
            app_called.append(True)
            return 7

        original_argv = list(sys.argv)
        try:
            sys.argv = [
                "片语.exe",
                "--ppt-export-runner",
                "--project",
                "demo.vcutproj",
            ]
            with patch("vcut_studio.ppt_export_runner.main", _fake_runner_main), patch(
                "vcut_studio.main.main",
                _fake_app_main,
            ):
                with self.assertRaises(SystemExit) as raised:
                    runpy.run_path(
                        "E:/xiangmu/PianYu/src/vcut_studio_bootstrap.py",
                        run_name="__main__",
                    )
        finally:
            sys.argv = original_argv

        self.assertEqual(raised.exception.code, 29)
        self.assertEqual(runner_called, [True])
        self.assertEqual(app_called, [])

    def test_bootstrap_dispatches_to_ppt_export_runner(self) -> None:
        runner_called: list[bool] = []
        app_called: list[bool] = []

        def _fake_runner_main() -> int:
            runner_called.append(True)
            return 23

        def _fake_app_main() -> int:
            app_called.append(True)
            return 7

        original_argv = list(sys.argv)
        try:
            sys.argv = [
                "片语.exe",
                "-m",
                "vcut_studio.ppt_export_runner",
                "--project",
                "demo.vcutproj",
            ]
            with patch("vcut_studio.ppt_export_runner.main", _fake_runner_main), patch(
                "vcut_studio.main.main",
                _fake_app_main,
            ):
                with self.assertRaises(SystemExit) as raised:
                    runpy.run_path(
                        "E:/xiangmu/PianYu/src/vcut_studio_bootstrap.py",
                        run_name="__main__",
                    )
        finally:
            sys.argv = original_argv

        self.assertEqual(raised.exception.code, 23)
        self.assertEqual(runner_called, [True])
        self.assertEqual(app_called, [])


if __name__ == "__main__":
    unittest.main()
