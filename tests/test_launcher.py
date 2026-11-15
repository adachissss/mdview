"""验证启停锁的生命周期，以及 PID 文件不会导致误杀其他进程。"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        repo = Path(__file__).resolve().parents[1]
        for name in ("mdview.sh", "serve.py", "rendering.py"):
            shutil.copy2(repo / name, self.root / name)
        (self.root / "static").symlink_to(repo / "static", target_is_directory=True)
        if sys.prefix != sys.base_prefix:
            (self.root / ".venv").symlink_to(sys.prefix, target_is_directory=True)
        else:
            (self.root / ".venv/bin").mkdir(parents=True)
            (self.root / ".venv/bin/python").symlink_to(sys.executable)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.env = {**os.environ, "MDVIEW_PORT": str(port)}
        self.addCleanup(self.command, "stop")

    def command(self, *args):
        result = subprocess.run([str(self.root / "mdview.sh"), *args], env=self.env,
                                capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_start_stop_and_restart_release_lifecycle_lock(self):
        self.command("start", "--bind", "127.0.0.1", "--no-auth")
        pid = (self.root / ".mdview.pid").read_text()
        self.assertIn("已经在运行", self.command("start").stdout)
        self.assertEqual((self.root / ".mdview.pid").read_text(), pid)
        self.command("restart", "--bind", "127.0.0.1", "--no-auth")
        self.assertNotEqual((self.root / ".mdview.pid").read_text(), pid)
        self.command("stop")
        self.assertFalse((self.root / ".mdview.pid").exists())

    def test_stale_pid_cannot_stop_unrelated_process(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        try:
            (self.root / ".mdview.pid").write_text(str(process.pid))
            self.command("stop")
            self.assertIsNone(process.poll())
        finally:
            process.terminate()
            process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
