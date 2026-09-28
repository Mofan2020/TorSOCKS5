#!/usr/bin/env python3
"""文档 ↔ 代码一致性校验。

文档一旦落后于代码，就会从「帮助」变成「障碍」。这个脚本把两者绑在一起：
CI 里跑一次，对不上就红。

检查项（每项都能被负向测试触发，见 --selftest）：

1. 路由方式：``torsocks5.routes.ROUTES`` 里的名字必须全部出现在 README 与 docs/routes.md，
   反过来文档里也不许出现不存在的路由名。
2. 子命令与选项：``build_parser()`` 里的每个子命令都要在 README 的「命令行详解」一节里有条目，
   每个选项（``--help`` 除外）都要在 README 里出现过——只在 ``--help`` 里存在的选项，
   等于对用户不存在。
3. 用例数：README 里写的单元测试个数必须等于 tests/ 里真实的 ``def test_*`` 数量。
4. 协议错误码：``torsocks5.tunnel.protocol`` 里的 ERR_* 都要在 docs/tunnel-protocol.md 里出现。
5. 配置段：``config.example.toml`` 的顶层段必须与 ``config.DEFAULTS`` 一一对应。
6. 相对链接：README 与 docs/*.md 里指向仓库内文件的链接必须真实存在。
7. 文档可达性：仓库里每个 ``.md``（README 之外）都要能从 README 找到入口
   —— 写了但没人找得到的文档等于没写。

用法::

    python scripts/check_docs.py            # 检查，失败返回 1
    python scripts/check_docs.py --selftest # 顺手证明校验器真的会拦
"""

from __future__ import annotations

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from torsocks5.log import force_utf8_output  # noqa: E402

README = os.path.join(ROOT, "README.md")
ROUTES_DOC = os.path.join(ROOT, "docs", "routes.md")
PROTOCOL_DOC = os.path.join(ROOT, "docs", "tunnel-protocol.md")
NOTES_DOC = os.path.join(ROOT, "docs", "notes.md")
EXAMPLE_CONFIG = os.path.join(ROOT, "torsocks5", "config.example.toml")
TESTS_DIR = os.path.join(ROOT, "tests")

FAILURES: list[str] = []
CHECKS_RUN = 0


def fail(message: str) -> None:
    FAILURES.append(message)


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def check(label: str, condition: bool, detail: str) -> None:
    global CHECKS_RUN
    CHECKS_RUN += 1
    if not condition:
        fail("[%s] %s" % (label, detail))


# --------------------------------------------------------------------- 各项检查
def check_routes() -> None:
    from torsocks5.routes import ROUTES, USER_ROUTES

    readme = read(README)
    doc = read(ROUTES_DOC)
    for name in USER_ROUTES:
        check("路由", name in readme, "路由 %r 未出现在 README.md" % name)
        check("路由", name in doc, "路由 %r 未出现在 docs/routes.md" % name)
    # 文档里不许出现「像路由但不存在」的名字
    known = set(ROUTES)
    allowed_extras = {"ws-server", "api-server"}  # 已知的非路由名（如依赖项）
    for text, label in ((readme, "README.md"), (doc, "docs/routes.md")):
        for token in re.findall(r"`([a-z][a-z0-9-]{3,})`", text):
            if (token.count("-") >= 1 and token.endswith(("relay", "meek"))
                    and token not in known and token not in allowed_extras):
                fail("[路由] %s 提到了不存在的路由名 %r" % (label, token))


def check_subcommands() -> None:
    from torsocks5.cli import build_parser

    readme = read(README)
    section = readme.split("## 命令行详解", 1)[-1].split("\n## ", 1)[0]
    parser = build_parser()
    subs = {}
    for action in parser._subparsers._group_actions:  # type: ignore[attr-defined]
        subs.update(action.choices)
    for name in sorted(subs):
        check("子命令", "### `%s`" % name in section,
              "子命令 %r 在 README「命令行详解」里没有条目" % name)

    # 选项：只在 --help 里存在的选项，等于对用户不存在
    skip = {"-h", "--help"}
    for name, sub in sorted(subs.items()):
        for action in sub._actions:
            for option in action.option_strings:
                if option in skip:
                    continue
                check("选项", option in readme,
                      "`%s %s` 这个选项在 README 里没有出现" % (name, option))
    for action in parser._actions:
        for option in action.option_strings:
            if option in skip:
                continue
            check("选项", option in readme,
                  "全局选项 %s 在 README 里没有出现" % option)


def count_tests() -> int:
    """tests/ 里真实的 ``def test_*`` 数量。"""
    actual = 0
    for name in sorted(os.listdir(TESTS_DIR)):
        if not name.startswith("test_") or not name.endswith(".py"):
            continue
        body = read(os.path.join(TESTS_DIR, name))
        actual += len(re.findall(r"^\s+def test_", body, re.M))
    return actual


def check_test_count() -> None:
    actual = count_tests()
    # README 与 docs/*.md 里凡是「单元测试（N 个…」都要等于真实数量
    # （Worker / Deno 的用例数写法不同，不会被这里误伤）
    targets = [README] + [os.path.join(ROOT, "docs", name)
                          for name in sorted(os.listdir(os.path.join(ROOT, "docs")))
                          if name.endswith(".md")]
    claimed = []
    for path in targets:
        for number in re.findall(r"单元测试（(\d+) 个", read(path)):
            claimed.append((path, int(number)))
    check("用例数", bool(claimed), "找不到任何「单元测试（N 个…」的声明")
    for path, number in claimed:
        check("用例数", number == actual,
              "%s 写 %d 个用例，实际 tests/ 里有 %d 个"
              % (os.path.relpath(path, ROOT), number, actual))


def check_error_codes() -> None:
    from torsocks5.tunnel import protocol

    doc = read(PROTOCOL_DOC)
    readme = read(README)
    codes = {name: value for name, value in vars(protocol).items()
             if name.startswith("ERR_") and isinstance(value, int)}
    check("错误码", bool(codes), "protocol.py 里没有解析出 ERR_* 常量")
    for name, value in codes.items():
        short = name[len("ERR_"):]
        check("错误码", short in doc, "错误码 %s 未出现在 docs/tunnel-protocol.md" % short)
        check("错误码", "0x%02x" % value in doc,
              "错误码 %s (0x%02x) 的值未出现在 docs/tunnel-protocol.md" % (name, value))
    for opcode in ("OPEN", "OPEN_OK", "OPEN_ERR", "DATA", "CLOSE", "RESET", "PING", "PONG"):
        check("帧类型", opcode in doc, "帧类型 %s 未出现在 docs/tunnel-protocol.md" % opcode)
        check("帧类型", opcode in readme, "帧类型 %s 未出现在 README.md" % opcode)


def check_config_sections() -> None:
    from torsocks5.config import DEFAULTS

    example = read(EXAMPLE_CONFIG)
    sections = set(re.findall(r"^\[([a-z_]+)\]", example, re.M))
    default_sections = {key.split(".")[0] for key, _value in DEFAULTS}
    for section in sorted(sections):
        check("配置段", section in default_sections,
              "config.example.toml 里的 [%s] 在 DEFAULTS 中不存在" % section)
    for section in sorted(default_sections):
        check("配置段", section in sections,
              "DEFAULTS 里的 [%s] 未写进 config.example.toml" % section)
    readme = read(README)
    for section in ("[split]", "[cf_relay]", "[self_relay]", "[relay]"):
        check("配置段", section in readme, "README 的配置说明里缺少 %s" % section)


def check_docs_reachable() -> None:
    """仓库里的每个 .md 都要能从 README 找到入口（不留孤儿文档）。"""
    readme = read(README)
    for path in _markdown_files():
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        if rel == "README.md":
            continue
        name = os.path.basename(rel)
        reachable = rel in readme or name in readme
        check("文档可达性", reachable,
              "%s 在 README 里没有任何入口（既没有链接也没有文件名）" % rel)


def _markdown_files() -> list:
    """需要「能从 README 找到」的文档：随代码走的 .md。

    ``.github/`` 之类的点开头目录整个跳过——issue / PR 模板由 GitHub 自己展示，
    不需要 README 给入口；构建产物与依赖目录同理。
    """
    skip_dirs = {"node_modules", "build", "dist", "__pycache__"}
    found = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in skip_dirs)
        for name in sorted(files):
            if name.endswith(".md"):
                found.append(os.path.join(root, name))
    return found


def check_relative_links() -> None:
    targets = [README] + [os.path.join(ROOT, "docs", name)
                          for name in sorted(os.listdir(os.path.join(ROOT, "docs")))
                          if name.endswith(".md")]
    pattern = re.compile(r"\]\(([^)#\s]+)\)")
    for path in targets:
        base = os.path.dirname(path)
        for link in pattern.findall(read(path)):
            if link.startswith(("http://", "https://", "mailto:", "#")):
                continue
            candidate = os.path.normpath(os.path.join(base, link.split("#")[0]))
            check("链接", os.path.exists(candidate),
                  "%s 里的链接 %r 指向不存在的文件" % (os.path.relpath(path, ROOT), link))


CHECKS = {
    "路由方式": check_routes,
    "子命令": check_subcommands,
    "用例数": check_test_count,
    "协议错误码/帧类型": check_error_codes,
    "配置段": check_config_sections,
    "相对链接": check_relative_links,
    "文档可达性": check_docs_reachable,
}


def run() -> int:
    for name, func in CHECKS.items():
        before = len(FAILURES)
        func()
        status = "OK" if len(FAILURES) == before else "FAIL"
        print("  [%-16s] %s" % (name, status))
    print()
    print("共 %d 项检查，%d 项失败" % (CHECKS_RUN, len(FAILURES)))
    for message in FAILURES:
        print("  ✗ %s" % message)
    return 1 if FAILURES else 0


def selftest() -> int:
    """故意制造不一致，证明校验器真的会拦（负向测试）。"""
    import shutil

    # 用例数的锚点按真实数量推导，避免校验器自己变成「需要手工维护的数字」
    count_anchor = "单元测试（%d 个" % count_tests()
    cases = [
        ("README 少写一个路由名", README, "self-relay", "selfrelay"),
        ("README 链接指向不存在的文件", README, "docs/routes.md", "docs/routes-missing.md"),
        ("文档出现不存在的路由名", README, "`self-relay`", "`my-relay`"),
        ("README 用例数写错", README, count_anchor, "单元测试（999 个"),
        ("docs 里的用例数写错", NOTES_DOC, count_anchor, "单元测试（999 个"),
        ("CLI 选项没写进文档", README, "--keep-going", "--keepgoing"),
        ("文档变成孤儿", README, "notes.md", "notes-missing.md"),
        ("config.example.toml 缺少一个段", EXAMPLE_CONFIG, "[split]", "# [split] 已被注释掉"),
        ("协议文档漏掉一个错误码", PROTOCOL_DOC, "NOT_ALLOWED", "NOT-ALLOWED-X"),
    ]
    ok = True
    for label, path, needle, replacement in cases:
        original = read(path)
        if needle not in original:
            print("  ! 跳过 %s（找不到锚点 %r）" % (label, needle))
            ok = False
            continue
        backup = path + ".bak"
        shutil.copy2(path, backup)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                # 全部替换：只替一处的话，别的正确出现会让错误看起来「没被拦下」
                handle.write(original.replace(needle, replacement))
            global FAILURES
            saved, FAILURES = FAILURES, []
            failed = False
            print("  - 模拟：%s" % label)
            for func in CHECKS.values():
                func()
            failed = bool(FAILURES)
            FAILURES = saved
            print("      → %s" % ("被拦下 ✓" if failed else "没拦住 ✗"))
            ok = ok and failed
        finally:
            shutil.move(backup, path)
    print()
    print("负向测试：%s" % ("全部被拦下" if ok else "有未拦下的情况"))
    return 0 if ok else 1


def main() -> int:
    force_utf8_output()  # Windows 控制台打不出中文与 ✓/✗（CI 上会 UnicodeEncodeError）
    parser = argparse.ArgumentParser(description="文档与代码一致性校验")
    parser.add_argument("--selftest", action="store_true", help="用负向测试证明校验器会拦")
    args = parser.parse_args()
    if args.selftest:
        print("负向测试：故意制造不一致，看校验器是否报错")
        return selftest()
    print("文档 ↔ 代码一致性校验（%s）" % os.path.relpath(ROOT, os.path.expanduser("~")))
    return run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
