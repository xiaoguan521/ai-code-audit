"""验证扫描入口传给 Qwen 的提示词参数，不需要模型或 Docker。"""
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class QwenEntrypointTest(unittest.TestCase):
    def test_leading_dash_prompts_remain_single_safe_arguments(self):
        with tempfile.TemporaryDirectory(prefix="audit entrypoint ") as tmp:
            base = Path(tmp)
            context = base / "context"
            context.mkdir()
            prompt = '---\nAUDIT_ENTRYPOINT_TEST "quoted" $(not-a-command)\n'
            (context / "prompt.md").write_text(prompt, encoding="utf-8")
            skill_file = ROOT / "skill/eight-quality-audit/SKILL.md"
            schema_file = ROOT / "config/result.schema.json"
            skill = skill_file.read_text(encoding="utf-8").rstrip("\n")
            schema = schema_file.read_text(encoding="utf-8").rstrip("\n")
            self.assertTrue(skill.startswith("---\n"))

            # 镜像路径替换为临时文件；仍执行真实入口脚本和 shell 传参逻辑。
            script = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
            replacements = {
                "PROMPT_FILE=/workspace/context/prompt.md": "PROMPT_FILE=" + shlex.quote(str(context / "prompt.md")),
                "cat /opt/audit/skills/eight-quality-code-audit/SKILL.md": "cat " + shlex.quote(str(skill_file)),
                "cat /opt/audit/result.schema.json": "cat " + shlex.quote(str(schema_file)),
                "--include-directories /workspace/context": "--include-directories " + shlex.quote(str(context)),
            }
            for old, new in replacements.items():
                self.assertEqual(script.count(old), 1)
                script = script.replace(old, new)
            entrypoint = base / "entrypoint.sh"
            entrypoint.write_text(script, encoding="utf-8")

            bin_dir = base / "bin"
            bin_dir.mkdir()
            qwen = bin_dir / "qwen"
            qwen.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                            "open(os.environ['AUDIT_TEST_ARGS'], 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n",
                            encoding="utf-8")
            qwen.chmod(0o755)
            args_file = base / "args.json"
            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
                       OPENAI_BASE_URL="http://127.0.0.1:1/v1", OPENAI_MODEL="mock-model",
                       OPENAI_API_KEY="dummy", AUDIT_TEST_ARGS=str(args_file))
            subprocess.run(["sh", str(entrypoint)], env=env, cwd=base, check=True,
                           text=True, capture_output=True, timeout=10)
            args = json.loads(args_file.read_text(encoding="utf-8"))
            prompt_arg = args[args.index("--prompt") + 1]
            system_arg = args[args.index("--append-system-prompt") + 1]
            self.assertEqual(prompt_arg, " " + prompt.rstrip("\n"))
            self.assertTrue(system_arg.startswith(" " + skill + "\n\n"))
            self.assertTrue(system_arg.endswith(schema))


if __name__ == "__main__":
    unittest.main()
