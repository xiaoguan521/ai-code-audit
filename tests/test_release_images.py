"""验证版本发布的离线镜像导出逻辑，无需 Docker daemon。"""
import gzip
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ReleaseImagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "p = pathlib.Path(os.environ['FAKE_DOCKER_TAGS'])\n"
            "tags = json.loads(p.read_text()) if p.exists() else {}\n"
            "if sys.argv[1] == 'tag':\n"
            "    tags[sys.argv[3]] = sys.argv[2]\n"
            "    p.write_text(json.dumps(tags))\n"
            "elif sys.argv[1] == 'save':\n"
            "    sources = [tags[name] for name in sys.argv[2:]]\n"
            "    if len(set(sources)) != 1: sys.exit(4)\n"
            "    if sources[0] == os.environ.get('FAKE_DOCKER_FAIL'): sys.exit(5)\n"
            "    print(json.dumps({'source': sources[0], 'tags': sys.argv[2:]}))\n"
            "else: sys.exit(6)\n",
            encoding="utf-8",
        )
        docker.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
                        FAKE_DOCKER_TAGS=str(self.root / "tags.json"), GITHUB_REF_NAME="v9.1.2",
                        SCANNER_IMAGE="ghcr.io/example/audit", RUNNER_IMAGE="ghcr.io/example/audit-runner")

    def run_export(self):
        return subprocess.run(["bash", str(ROOT / "docker/export_release_images.sh")], cwd=self.root,
                              env=self.env, text=True, capture_output=True)

    def test_exports_both_images_for_both_architectures_with_checksums(self):
        result = self.run_export()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        offline = self.root / "offline"
        self.assertEqual(len(list(offline.iterdir())), 8)
        for arch in ("amd64", "arm64"):
            for component, remote in (("ai-code-audit", "ghcr.io/example/audit"),
                                      ("ai-code-audit-runner", "ghcr.io/example/audit-runner")):
                archive = offline / f"{component}_v9.1.2_{arch}.tar.gz"
                with gzip.open(archive, "rt", encoding="utf-8") as f:
                    saved = json.load(f)
                self.assertEqual(saved["source"], f"{component}:ci-{arch}")
                self.assertEqual(saved["tags"], [f"{component}:9.1.2", f"{remote}:9.1.2"])
                expected = hashlib.sha256(archive.read_bytes()).hexdigest()
                self.assertEqual((offline / f"{archive.name}.sha256").read_text().split(),
                                 [expected, archive.name])

    def test_export_stops_when_docker_save_fails(self):
        self.env["FAKE_DOCKER_FAIL"] = "ai-code-audit-runner:ci-arm64"
        self.assertNotEqual(self.run_export().returncode, 0)
        self.assertFalse((self.root / "offline/ai-code-audit-runner_v9.1.2_arm64.tar.gz.sha256").exists())


if __name__ == "__main__":
    unittest.main()
