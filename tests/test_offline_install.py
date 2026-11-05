"""Exercise offline installation with real local payloads and blocked downloads."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = "rtl-analysis-agent-V1-linux-x86_64.tar.gz"


class OfflineInstallTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="rtl-offline-test-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name) / "bundle"
        self.pack = self.root / "src/rtl-pack"
        (self.pack / "config").mkdir(parents=True)
        (self.pack / "scripts").mkdir()
        (self.pack / "config/domain-pack.json").write_text("{}")
        shutil.copy2(ROOT / "src/rtl-pack/scripts/install-tools", self.pack / "scripts/install-tools")
        self.downloads = self.root / ".local/downloads"
        self.downloads.mkdir(parents=True)
        archive = self.downloads / "fixture.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            payload = b"#!/bin/sh\necho fixture\n"
            member = tarfile.TarInfo("fixture/bin/tool")
            member.size = len(payload)
            bundle.addfile(member, io.BytesIO(payload))
        wheel = self.downloads / "rtl_offline_fixture-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as bundle:
            bundle.writestr("rtl_offline_fixture.py", "__version__ = '1.0'\n")
            bundle.writestr("rtl_offline_fixture-1.0.dist-info/METADATA",
                            "Metadata-Version: 2.1\nName: rtl-offline-fixture\nVersion: 1.0\n")
            bundle.writestr("rtl_offline_fixture-1.0.dist-info/WHEEL",
                            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            bundle.writestr("rtl_offline_fixture-1.0.dist-info/RECORD", "")

        def record(path):
            return {"archive": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "url": "https://download.invalid/" + path.name}

        self.manifest = {"schema": "rtl-analysis-agent/toolchain-v1", "platform": "linux-x86_64",
                         "tools": [
                             {"name": "fixture", "version": "1", **record(archive),
                              "files": [{"archive_path": "fixture/bin/tool", "install_path": "bin/tool"}]},
                             {"name": "wheel-fixture", "version": "1", "kind": "python-wheel-set",
                              "wheels": [record(wheel)], "entry_points": [
                                  {"install_path": "bin/fixture-version", "module": "rtl_offline_fixture",
                                   "attribute": "__version__"}]}]}
        (self.pack / "config/toolchain.json").write_text(json.dumps(self.manifest))
        self.guard = Path(self.scratch.name) / "network-guard"
        self.guard.mkdir()
        self.attempts = self.guard / "attempts"
        for name in ("wget", "curl", "git"):
            command = self.guard / name
            command.write_text('#!/bin/sh\necho "$0 $*" >> "$NETWORK_ATTEMPTS"\nexit 97\n')
            command.chmod(0o755)
        # Also block Python networking, including a mistaken pip index lookup.
        (self.guard / "sitecustomize.py").write_text(
            "import os, socket\n"
            "def blocked(*args, **kwargs):\n"
            "    with open(os.environ['NETWORK_ATTEMPTS'], 'a') as stream: stream.write('socket\\n')\n"
            "    raise RuntimeError('network blocked by offline installation test')\n"
            "socket.socket.connect = blocked\nsocket.create_connection = blocked\n")
        self.env = {**os.environ, "PATH": f"{self.guard}:{os.environ['PATH']}",
                    "PYTHONPATH": str(self.guard), "NETWORK_ATTEMPTS": str(self.attempts),
                    "PIP_CONFIG_FILE": os.devnull, "PIP_INDEX_URL": "https://download.invalid/simple",
                    "PYTHONDONTWRITEBYTECODE": "1"}

    def invoke(self, *args):
        result = subprocess.run([sys.executable, str(self.pack / "scripts/install-tools"), *args],
                                cwd=self.root, env=self.env, capture_output=True, text=True, timeout=15)
        self.assertFalse(self.attempts.exists(),
                         "installer attempted network access: " +
                         (self.attempts.read_text() if self.attempts.exists() else ""))
        return result

    def mark_bundle(self):
        (self.root / "runtime-lock.json").write_text('{"version":"V1"}')

    def test_cached_archives_and_wheels_install_offline_and_repeat(self):
        result = self.invoke("--offline")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)
        self.assertTrue((self.root / ".local/tools/fixture/1/bin/tool").is_file())
        self.assertTrue((self.root / ".local/tools/wheel-fixture/1/site-packages/rtl_offline_fixture.py").is_file())
        self.assertEqual(self.invoke("--offline").returncode, 0)

    def test_explicit_offline_missing_archive_stops_with_expected_filename(self):
        (self.downloads / "fixture.tar.gz").unlink()
        result = self.invoke("--offline")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixture.tar.gz", result.stderr)
        self.assertIn(ARCHIVE, result.stderr)
        self.assertIn("offline", result.stderr.lower())

    def test_source_archive_defaults_to_offline_when_cache_is_missing(self):
        (self.downloads / "fixture.tar.gz").unlink()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixture.tar.gz", result.stderr)
        self.assertIn(ARCHIVE, result.stderr)

    def test_source_online_download_requires_explicit_option(self):
        (self.downloads / "fixture.tar.gz").unlink()
        result = subprocess.run([sys.executable, str(self.pack / "scripts/install-tools"), "--online"],
                                cwd=self.root, env=self.env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.attempts.exists(), result.stderr)
        self.assertIn("wget", self.attempts.read_text())
        self.assertIn("download.invalid/fixture.tar.gz", self.attempts.read_text())

    def test_bundle_rejects_online_override(self):
        self.mark_bundle()
        result = self.invoke("--online")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--online", result.stderr)
        self.assertIn("offline bundle rejects", result.stderr)
        self.assertFalse((self.root / ".local/tools/fixture/1").exists())

    def test_incomplete_bundle_rejects_online_even_without_domain_manifest(self):
        self.mark_bundle()
        (self.pack / "config/domain-pack.json").unlink()
        result = self.invoke("--online")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("offline bundle rejects", result.stderr)

    def test_bundle_direct_install_is_offline_even_when_runtime_is_missing(self):
        self.mark_bundle()
        (self.downloads / "fixture.tar.gz").unlink()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(ARCHIVE, result.stderr)

    def test_bundle_missing_wheel_stops_before_installing_any_tool(self):
        self.mark_bundle()
        wheel = self.downloads / self.manifest["tools"][1]["wheels"][0]["archive"]
        wheel.unlink()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(wheel.name, result.stderr)
        self.assertIn(ARCHIVE, result.stderr)
        self.assertFalse((self.root / ".local/tools/fixture/1").exists())

    def test_bundle_corrupt_cache_stops_with_full_package_hint(self):
        self.mark_bundle()
        (self.downloads / "fixture.tar.gz").write_bytes(b"corrupt")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hash mismatch", result.stderr)
        self.assertIn(ARCHIVE, result.stderr)

    def test_bin_install_requires_full_bundle_and_never_downloads(self):
        (self.root / "bin").mkdir()
        shutil.copy2(ROOT / "packaging/install", self.root / "bin/install")
        result = subprocess.run([str(self.root / "bin/install")], cwd=self.root, env=self.env,
                                capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("vendor/python/bin/python3", result.stderr)
        self.assertIn(ARCHIVE, result.stderr)
        self.assertFalse(self.attempts.exists())


class OfflineGuidanceTest(unittest.TestCase):
    def test_packaged_agent_entry_routes_to_offline_first(self):
        # build-offline copies these exact files into the release. Their first
        # route must handle the bundle before giving the online source recipe.
        agents = (ROOT / "AGENTS.md").read_text()
        quickstart = (ROOT / "quickstart.md").read_text()
        self.assertIn("./bin/install", agents.split("## 使用约定")[0])
        self.assertIn("runtime-lock.json", agents.split("## 使用约定")[0])
        self.assertIn("完全离线", quickstart.split("## 1.")[0])
        self.assertIn("./bin/install", quickstart.split("## 1.")[0])
        self.assertIn(ARCHIVE, quickstart)

    def test_release_guidance_distinguishes_source_archive(self):
        for relative in ("README.md", "packaging/README.md"):
            text = (ROOT / relative).read_text()
            self.assertIn("V1 离线安装包（Linux x86_64）", text)
            self.assertIn("Source code", text)
            self.assertIn(ARCHIVE, text)
            self.assertIn("github.com/zengpoly/rtl-analysis/releases", text)


if __name__ == "__main__":
    unittest.main()
