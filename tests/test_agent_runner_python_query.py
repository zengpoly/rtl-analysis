from __future__ import annotations

import hashlib
import io
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLI = runpy.run_path(str(ROOT / "tools/agent-runner/bin/rtlq"))
SERVER = runpy.run_path(str(ROOT / "tools/agent-runner/runtime_server.py"))


class PythonQueryClientTests(unittest.TestCase):
    def test_file_and_stdin_produce_native_python_request(self) -> None:
        code = "saved = root.topInstances[0]\nresult = saved.name\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "query.py"
            path.write_text(code)
            args = CLI["parser"]().parse_args(["python", str(path)])
            file_request = CLI["request"](args)
            with patch("sys.stdin", io.StringIO(code)):
                stdin_request = CLI["request"](CLI["parser"]().parse_args(["python", "-"]))
        for payload in [file_request, stdin_request]:
            self.assertEqual(payload["op"], "python")
            self.assertEqual(payload["code"], code)
            event = SERVER["bounded_request"](payload)
            self.assertEqual(event["code_sha256"], hashlib.sha256(code.encode()).hexdigest())
            self.assertEqual(event["code_bytes"], len(code.encode()))
            self.assertNotIn("code", event)

    def test_invalid_code_is_not_sent(self) -> None:
        args = CLI["parser"]().parse_args(["python", "-"])
        for code in [" \n", "中" * 30_000]:
            with self.subTest(length=len(code)), patch("sys.stdin", io.StringIO(code)):
                with self.assertRaises(ValueError):
                    CLI["request"](args)


if __name__ == "__main__":
    unittest.main()
