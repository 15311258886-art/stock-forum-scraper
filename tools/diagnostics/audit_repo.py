# -*- coding: utf-8 -*-
"""仓库卫生审计（2026-10-31 新增，测评 P0-E「仓库治理」项的机检部分）。

## 它拦什么

只拦**会复发的那几类**，不管风格：

  1. **0 字节库文件**。这是本项目最阴的一类：某个模块用错相对路径打开
     `data/app.db`，SQLite 会**静默新建一个空库**，程序不报错、日志不告警，
     数据全空 —— 直到有人发现"今天怎么一只票都没有"。
     2026-10-31 实测清出 3 个：`data/app.db`、`viewer/data/app.db`、`viewer/data/kbase.db`。
     清掉只是治标，所以这里加一条：**再出现 0 字节 .db 即判红**。

  2. **可重建的大体积运行期缓存被版本库跟踪**。`viewer/data/` 下 5,661 个
     `kline_*.json` 合计 2.2 GB，而原 .gitignore 只挡了 `*_cache.json`，
     一条都没挡住。这类文件每天都变，进库等于每次提交都是全量 diff。

  3. **缺陷注册表缺关闭条件**。状态写 `mitigated` / `open` 而没有
     `close_condition`，等于"我们知道它没关，但没人知道什么情况下才算关"——
     于是它会永久停在 mitigated，看起来像已经处理过了。

## 为什么写在脚本里

和 audit_params 同一个理由：「约定」已经失效过一次。0 字节库这个问题
不是第一次出现，前两轮清理过，又长出来了 —— 说明靠记忆拦不住。
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {".venv", ".git", "__pycache__", "backups", "extlib", "node_modules",
             ".browser-profile", "archive", "dist", "build"}

# 允许为 0 字节的例外（没有任何一条；写了空集合是为了让"加例外"必须改代码，
# 而不是改一个看起来无害的配置）
ZERO_BYTE_ALLOW = set()

# 可重建的运行期缓存：不允许被 git 跟踪
CACHE_GLOBS = [
    ("viewer/data", "kline_"),
]


def _walk_db():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith("_pre_repair")]
        for fn in filenames:
            if fn.endswith((".db", ".sqlite", ".sqlite3")):
                yield os.path.join(dirpath, fn)


def check_zero_byte_db():
    bad = []
    for p in _walk_db():
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        if rel in ZERO_BYTE_ALLOW:
            continue
        try:
            if os.path.getsize(p) == 0:
                bad.append(rel)
        except OSError:
            continue
    if bad:
        print("[FAIL] 发现 %d 个 0 字节库文件（有代码在用错路径静默新建空库）：" % len(bad))
        for b in bad:
            print("   " + b)
        print("   修法：找到打开它的模块，把路径改成绝对路径；删掉空文件只是治标。")
        return 1
    print("[PASS] 无 0 字节库文件")
    return 0


def _tracked_files():
    """当前被 git 跟踪的文件列表（非 git 仓库则返回 None）。"""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True, timeout=120)
        if out.returncode != 0:
            return None
        return set(x.strip().replace("\\", "/") for x in out.stdout.splitlines() if x.strip())
    except (OSError, subprocess.SubprocessError):
        return None


def check_cache_tracked():
    tracked = _tracked_files()
    if tracked is None:
        print("[SKIP] 不是 git 仓库或 git 不可用，跳过缓存跟踪检查")
        return 0
    bad = []
    for d, prefix in CACHE_GLOBS:
        for f in tracked:
            if f.startswith(d + "/") and os.path.basename(f).startswith(prefix):
                bad.append(f)
    if bad:
        print("[FAIL] %d 个可重建的运行期缓存被 git 跟踪（例：%s）"
              % (len(bad), bad[0]))
        print("   修法：git rm --cached <path>（只解除跟踪，不删本地文件），确认 .gitignore 已覆盖。")
        return 1
    print("[PASS] 运行期缓存未被 git 跟踪")
    return 0


def check_defects_have_close_condition():
    p = os.path.join(ROOT, "KNOWN_DEFECTS.json")
    if not os.path.exists(p):
        print("[FAIL] KNOWN_DEFECTS.json 缺失")
        return 1
    d = json.load(open(p, encoding="utf-8"))
    bad = []
    for x in d.get("defects", []):
        st = (x.get("status") or "").lower()
        if st in ("fixed", "closed"):
            continue
        if not (x.get("close_condition") or "").strip():
            bad.append("%s(%s)" % (x.get("id"), st))
    if bad:
        print("[FAIL] %d 条未关闭缺陷没有 close_condition：" % len(bad))
        for b in bad:
            print("   " + b)
        print("   含义：状态写了 mitigated/open，却没人知道什么条件下才算关闭 ——")
        print("         于是它会永久停在 mitigated，看起来像已处理。")
        return 1
    print("[PASS] 所有未关闭缺陷都写明了关闭条件")
    return 0


def main() -> int:
    print("=" * 72)
    print("仓库卫生审计")
    print("  根目录：%s" % ROOT)
    print("=" * 72)
    rc = 0
    rc |= check_zero_byte_db()
    rc |= check_cache_tracked()
    rc |= check_defects_have_close_condition()
    print("-" * 72)
    print("[PASS] 仓库卫生全部通过" if rc == 0 else "[FAIL] 存在未通过项，见上")
    return rc


if __name__ == "__main__":
    sys.exit(main())
