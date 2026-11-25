import os
from pathlib import Path
import shlex
import subprocess
import unittest
from unittest.mock import patch

from backend.config import default_data_dir
from backend.dialogs import choose_directory, choose_files
from backend.library import LibraryError


class DesktopTests(unittest.TestCase):
    def setUp(self):
        home = patch.object(Path, "home", return_value=Path(__file__).resolve().parent)
        home.start()
        self.addCleanup(home.stop)

    def test_bundled_data_directory_preserves_windows_and_uses_posix_user_storage(self):
        home = Path.home()
        with patch("backend.config.sys.platform", "win32"):
            self.assertEqual(default_data_dir(home, True), home / "data")
        with patch("backend.config.sys.platform", "darwin"):
            self.assertEqual(default_data_dir(home, False), home / "data")
            self.assertEqual(default_data_dir(home, True), home / "Library/Application Support/SkyTiles")
        with patch("backend.config.sys.platform", "linux"):
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(home / "xdg")}):
                self.assertEqual(default_data_dir(home, True), home / "xdg/skytiles")
            with patch.dict(os.environ, {"XDG_DATA_HOME": "relative"}):
                self.assertEqual(default_data_dir(home, True), home / ".local/share/skytiles")

    def test_macos_picker_passes_the_directory_as_an_argument_and_preserves_names(self):
        directory = str(Path.home())
        files = ["/Users/name/航测 数据/A's.jpg", "/Users/name/航测\nB.jpg"]
        completed = subprocess.CompletedProcess([], 0, stdout=shlex.join(files) + "\n")
        with patch("backend.dialogs.sys.platform", "darwin"), patch("backend.dialogs.subprocess.run", return_value=completed) as run:
            self.assertEqual(choose_files(directory), files)
            args, options = run.call_args
            self.assertEqual(args[0], ("/usr/bin/osascript", "-", directory))
            self.assertIn("with multiple selections allowed", options["input"])
            completed.stdout = "\n"
            self.assertIsNone(choose_directory(directory))

    def test_linux_picker_result_and_missing_display(self):
        completed = subprocess.CompletedProcess([], 0, stdout='"/home/user/航测资料"')
        with patch("backend.dialogs.sys.platform", "linux"), patch("backend.dialogs.subprocess.run", return_value=completed) as run:
            self.assertEqual(choose_directory(), "/home/user/航测资料")
            self.assertEqual(run.call_args.args[0][-4:-2], ["--picker", "folder"])
            run.side_effect = subprocess.CalledProcessError(1, ["SkyTiles"], stderr="no display")
            with self.assertLogs("backend.dialogs", level="ERROR"), self.assertRaises(LibraryError) as failure:
                choose_directory()
            self.assertEqual(failure.exception.status, 422)
