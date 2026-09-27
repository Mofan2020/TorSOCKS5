"""跨平台相关的回归测试：Windows 控制台编码、路径空格、配置路径。"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class WindowsConsoleEncodingTest(unittest.TestCase):
    """Windows 控制台默认是本地代码页，中文/符号输出会崩 UnicodeEncodeError。"""

    def _run(self, args, encoding):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = encoding
        env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "torsocks5_cli.py")] + args,
            cwd=ROOT, env=env, capture_output=True, timeout=120,
        )
        return proc

    def test_help_under_gbk(self):
        proc = self._run(["--help"], "cp936")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertIn(b"usage", proc.stdout)

    def test_help_under_cp1252(self):
        proc = self._run(["--help"], "cp1252")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))

    def test_version_under_cp1252(self):
        proc = self._run(["--version"], "cp1252")
        self.assertEqual(proc.returncode, 0)
        self.assertIn(b"TorSOCKS5", proc.stdout)

    def test_subcommand_help_under_gbk(self):
        for command in ("run", "doctor", "bridges", "config", "fetch-tor", "selftest"):
            proc = self._run([command, "--help"], "cp936")
            self.assertEqual(proc.returncode, 0, "%s --help 失败: %s" % (command, proc.stderr[:200]))

    def test_selftest_under_gbk(self):
        proc = self._run(["selftest"], "cp936")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))


class ShimPathTest(unittest.TestCase):
    """tor 不支持带空格的插件路径，必须生成无空格 shim。"""

    def test_command_without_spaces_is_used_directly(self):
        from torsocks5 import config as config_mod
        from torsocks5.tor.manager import TorProcess

        tor = TorProcess(config_mod.Config({}), [])
        command = tor.raw_plugin_command()
        self.assertTrue(all(" " not in token for token in command), command)

    def test_shim_is_created_for_spaced_command(self):
        from torsocks5 import config as config_mod
        from torsocks5.tor.manager import TorProcess, ShimError

        tor = TorProcess(config_mod.Config({}), [])
        spaced = ['/some path/python3', '/other path/meek_pt.py']
        try:
            launcher = tor._make_shim(spaced)
        except ShimError as exc:
            self.skipTest("本机没有可写的无空格目录: %s" % exc)
        for token in launcher:
            self.assertNotIn(" ", token, "shim 命令里不能有空格: %r" % token)
        self.assertTrue(os.path.exists(launcher[-1] if os.name != "nt" else launcher[2]))
        if os.name != "nt":
            with open(launcher[-1], encoding="utf-8") as handle:
                content = handle.read()
            self.assertTrue(content.startswith("#!/bin/sh"))
            for token in spaced:
                self.assertIn('"%s"' % token, content)
            self.assertTrue(os.access(launcher[-1], os.X_OK))


class ConfigPathTest(unittest.TestCase):
    def test_default_paths_are_absolute(self):
        from torsocks5 import config as config_mod

        for path in (
            config_mod.default_config_path(),
            config_mod.default_bridges_path(),
            config_mod.default_data_dir(),
        ):
            self.assertTrue(os.path.isabs(path), path)

    def test_env_override(self):
        from torsocks5 import config as config_mod

        old = os.environ.get("TORSOCKS5_CONFIG")
        try:
            os.environ["TORSOCKS5_CONFIG"] = "/tmp/custom-config.toml"
            config = config_mod.Config.load()
            self.assertEqual(config.path, "/tmp/custom-config.toml")
        finally:
            if old is None:
                os.environ.pop("TORSOCKS5_CONFIG", None)
            else:
                os.environ["TORSOCKS5_CONFIG"] = old


class PythonCompatTest(unittest.TestCase):
    def test_declared_minimum(self):
        self.assertGreaterEqual(sys.version_info[:2], (3, 8))

    def test_no_3_9plus_only_syntax_in_library(self):
        """3.8 上不能用 ``dict[str, int]`` 这类内建泛型注解。"""
        import ast
        import pathlib

        offenders = []
        for path in pathlib.Path(ROOT, "torsocks5").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            has_future = any(
                isinstance(node, ast.ImportFrom)
                and node.module == "__future__"
                and any(alias.name == "annotations" for alias in node.names)
                for node in tree.body
            )
            if has_future:
                continue  # 注解被延迟求值，3.8 安全
            for node in ast.walk(tree):
                # 变量注解里的下标表达式（如 x: list[int] = []）在 3.8 会立刻求值
                if isinstance(node, ast.AnnAssign) and isinstance(node.annotation, ast.Subscript):
                    value = node.annotation.value
                    if isinstance(value, ast.Name) and value.id in {"list", "dict", "set", "tuple", "type"}:
                        offenders.append("%s:%d" % (path.name, node.lineno))
        self.assertEqual(offenders, [], "这些注解需要 3.9+：" + ", ".join(offenders))


if __name__ == "__main__":
    unittest.main(verbosity=2)
