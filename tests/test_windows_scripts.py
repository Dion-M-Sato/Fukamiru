from __future__ import annotations

import unittest
from pathlib import Path


class WindowsScriptTests(unittest.TestCase):
    def test_installer_does_not_require_python_launcher(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        installer = (repository_root / "scripts/windows/install.ps1").read_text(encoding="utf-8")

        self.assertIn('Command = "py"', installer)
        self.assertIn('Command = "python"', installer)
        self.assertIn('Command = "python3"', installer)
        self.assertIn('PrefixArguments = @("-3.11")', installer)
        self.assertIn("Get-Command", installer)
        self.assertIn("sys.version_info >= (3, 11)", installer)
        self.assertIn("https://download.pytorch.org/whl/cu126", installer)
        self.assertIn('torch.version.cuda == "12.6"', installer)
        self.assertIn("torch.cuda.is_available()", installer)
        self.assertIn("--force-reinstall", installer)
        self.assertNotIn("\n    py -3.12 -m venv", installer)

    def test_start_script_hides_background_window(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        start_script = (repository_root / "scripts/windows/start.ps1").read_text(encoding="utf-8")

        self.assertIn('WindowStyle = "Hidden"', start_script)

if __name__ == "__main__":
    unittest.main()
