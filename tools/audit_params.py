# -*- coding: utf-8 -*-
"""参数纪律审计（2026-10-31 新增，测评 P0-D 的可机检部分）。

## 它拦什么

只拦一件事：**同一个阈值在第二个地方被重新写成一个字面量。**

本轮的真问题不是"40 和 500 谁对"，而是"没有任何一处代码或检查发现它们是矛盾的"。
所以这里的断言不判"阈值取值是否合理"（那要人拍），只判"阈值有没有第二个来源"。

判据（任一条命中即 FAIL）：
  1. 除 viewer/kernel/params.py 外，出现 `MIN_N = <数字>`；
  2. 除 params.py 外，出现 `exp(-… / <数字>` 形态（eff 曲线的斜率）；
  3. 除 params.py 外，出现 `n < <三位以上数字>` 形态（样本量门槛）；
  4. params.py 自身缺少 EFF_SCALE / JUDGE_MIN_N 之外的必需符号。

## 为什么写成脚本而不是靠约定

"约定"已经失效过一次：40 与 500 共存了一个多月，期间两次代码评审、一次全链路体检都没发现，
因为每个数字单看都合理。约定要求人记得住，脚本不要求。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARAMS = os.path.join(ROOT, "viewer", "kernel", "params.py")

SKIP_DIRS = {".venv", ".git", "__pycache__", "backups", "extlib", "_pre_repair_snapshot", "archive"}
# 只审"生产代码"：viewer/ 与 tools/ 下的 .py；tests/ 里的断言允许出现数字（那是判据本身）。
TARGET_DIRS = ["viewer", "tools"]

PATTERNS = [
    ("MIN_N 字面量", re.compile(r"\bMIN_N\s*=\s*\d+")),
    ("eff 斜率字面量", re.compile(r"math\.exp\(\s*-\s*[^)]*/\s*\d+(?:\.\d+)?\s*\)")),
    ("样本量门槛字面量", re.compile(r"\bn\s*<\s*\d{3,}\b")),
]

# 诊断脚本豁免（2026-10-31 二审）：tools/probe_*.py 是一次性探测脚本，
# 里面的 `if n < 130` / `n < 560` 是**对它们自己那批数据**的过滤条件，
# 不是生产判定，也不参与任何对外结论。把它们改成引 params 反而是错的
# ——那会让"某次探测恰好用了 130"看起来像是系统的正式门槛。
# 所以按**文件名前缀**豁免，且只在 tools/ 一层，不外扩：
#   · 新增 probe_*.py 自动落入豁免（这是有意为之，探测脚本本该随手写）；
#   · 一旦有人想在 viewer/ 下写这种字面量，前缀豁免不生效，照样判红。
EXEMPT_PREFIXES = [("tools", "probe_")]


def _is_exempt(rel: str) -> bool:
    head, _, tail = rel.replace("\\", "/").partition("/")
    return any(head == d and tail.startswith(p) for d, p in EXEMPT_PREFIXES)


def _iter_files():
    for d in TARGET_DIRS:
        base = os.path.join(ROOT, d)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames
                           if x not in SKIP_DIRS and not x.startswith("_pre_repair")]
            for fn in filenames:
                if fn.endswith(".py"):
                    yield os.path.join(dirpath, fn)


def main() -> int:
    hits = []
    exempted = []
    for path in _iter_files():
        if os.path.abspath(path) == os.path.abspath(PARAMS):
            continue
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        try:
            src = open(path, encoding="utf-8", errors="ignore").read()
        except Exception:  # noqa: BLE001
            continue
        for i, line in enumerate(src.splitlines(), 1):
            stripped = line.strip()
            # 注释与文档字符串里提到阈值不算违规（那正是我们希望留的说明）
            if stripped.startswith("#"):
                continue
            for name, pat in PATTERNS:
                if pat.search(line):
                    rec = (rel, i, name, stripped[:110])
                    (exempted if _is_exempt(rel) else hits).append(rec)

    # params.py 必备符号自检
    if not os.path.exists(PARAMS):
        print("[FAIL] 参数注册表缺失：%s" % os.path.relpath(PARAMS, ROOT))
        return 1
    psrc = open(PARAMS, encoding="utf-8").read()
    missing = [s for s in ("EFF_SCALE", "EFF_FLOOR", "JUDGE_MIN_N", "LIST_N",
                           "MIN_BARS_SHORT_SIGNAL", "MIN_BARS_LONG_ALIGN", "eff_of",
                           # 2026-10-31 决策①：三层制的符号也纳入必备集。
                           # 不纳入的话，后来者把 OBSERVE_MIN_N 删掉、在
                           # evidence.py 里重写一个 40，审计器会一声不响地放行。
                           "OBSERVE_MIN_N", "TIER_OBSERVE", "TIER_PROVISIONAL",
                           "TIER_DECIDABLE", "tier_of", "weight_gate", "eff_weight")
               if s not in psrc]
    if missing:
        print("[FAIL] 参数注册表缺少符号：%s" % missing)
        return 1

    print("=" * 72)
    print("参数纪律审计（阈值唯一来源）")
    print("  审计范围：%s 下的 .py（跳过 %s）" % (" + ".join(TARGET_DIRS), "/".join(sorted(SKIP_DIRS))))
    print("  参数注册表：viewer/kernel/params.py")
    if EXEMPT_PREFIXES:
        print("  诊断脚本豁免：%s（仅这些文件，不外扩）"
              % ", ".join("%s/%s*" % (d, p) for d, p in EXEMPT_PREFIXES))
    print("=" * 72)
    if exempted:
        print("[豁免] %d 处位于诊断脚本内（对自身数据的过滤，不参与生产判定）：" % len(exempted))
        for rel, ln, name, txt in exempted:
            print("   %s:%d  [%s]  %s" % (rel, ln, name, txt))
        print("")
    if not hits:
        print("[PASS] 生产代码未发现阈值字面量外泄 —— 阈值只有一个来源。")
        return 0
    print("[FAIL] 生产代码发现 %d 处阈值字面量外泄（阈值出现了第二个来源）：" % len(hits))
    for rel, ln, name, txt in hits:
        print("   %s:%d  [%s]  %s" % (rel, ln, name, txt))
    print("\n修法：把数字改成从 viewer.kernel.params 引用；不要在本文件里保留第二份。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
