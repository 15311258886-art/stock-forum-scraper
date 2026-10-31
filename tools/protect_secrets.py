# -*- coding: utf-8 -*-
"""tools/protect_secrets.py — 把项目根的明文会话凭据迁成 DPAPI 密文（一次性工具）。

背景与加密原理见 `viewer/secrets_dpapi.py` 的模块 docstring。这里只做 CLI：

```
python tools/protect_secrets.py --check          # 只看状态（默认动作，不写任何文件）
python tools/protect_secrets.py --protect        # 生成 <name>.dpapi，明文保留
python tools/protect_secrets.py --verify         # 逐字节比对解密结果与明文
python tools/protect_secrets.py --shred --yes    # 校验通过后删除明文（默认拒绝执行）
```

## 三条设计纪律

1. **默认什么都不做**（`--check`）：一个"顺手帮你加密一下"的工具，最容易在错误的
   时刻（抓取正在跑、文件正在被写）把凭据改坏。
2. **永远先验证再删除**：`--shred` 会先跑 `verify()`，解密结果与明文不一致就拒绝删除。
   宁可多留一份明文，也不能把唯一一份凭据搞丢。
3. **不打印凭据内容**：只打印字节数与是否可解。日志/终端回滚可能被截图。

## 迁移完还要做的一步（不在本工具能力范围内）

`load()` 生效的前提是**读取方改用它**。当前三个读取点仍是直接 `open()`：

| 读取点 | 现在 | 应改为 |
|---|---|---|
| `scraper/browser_fetch.py:192-204` | 遍历 `cookies.txt` / `cookies.json` | `secrets_dpapi.load("cookies.txt")` |
| `etf_jisilu.py:25`（`COOKIE_FILE`） | `open(COOKIE_FILE)` | `secrets_dpapi.load("jisilu_cookie.txt")` |
| `etf_track_error.py:32`（`COOKIE_FILE`） | `open(COOKIE_FILE)` | `secrets_dpapi.load("eastmoney_cookie.txt")` |

**没改这三处之前跑 `--protect` 只是多了一份密文，明文照样在、读取方照样读明文 ——
那是"假防护"，比不做更容易让人误判已安全。** 所以 `--protect` 结束时会把这句话再打一遍。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from viewer import secrets_dpapi as S  # noqa: E402


def _fmt(ts) -> str:
    if not ts:
        return "-"
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def cmd_check() -> int:
    print("受管凭据盘点（只读，未修改任何文件）")
    print("-" * 78)
    print("%-22s %-8s %-10s %-20s %-9s %-8s" %
          ("文件", "明文", "明文字节", "明文 mtime", "密文", "可解"))
    any_plain = False
    for st in S.status():
        any_plain = any_plain or st["plain_exists"]
        print("%-22s %-8s %-10d %-20s %-9s %-8s" % (
            st["name"],
            "有" if st["plain_exists"] else "-",
            st["plain_bytes"],
            _fmt(st["plain_mtime"]),
            "有" if st["cipher_exists"] else "-",
            ("是" if st["cipher_ok"] else "**否**") if st["cipher_exists"] else "-",
        ))
    print("-" * 78)
    if not S.available():
        print("⚠️  本机 DPAPI 不可用（非 Windows 或 crypt32 绑定失败）：--protect 会直接失败，不会伪造成功。")
    if any_plain:
        print("结论：仍有明文凭据在盘上。迁移步骤见文件头，或 `--protect` 生成密文后再改读取点。")
    else:
        print("结论：三份受管凭据均无明文（或本机没有这些文件）。")
    return 0


def cmd_protect() -> int:
    if not S.available():
        print("✗ DPAPI 不可用，已停止（不伪造成功）。")
        return 2
    rc = 0
    for name in S.MANAGED:
        if not S.plain_path(name).is_file():
            print("· %-22s 跳过（明文不存在）" % name)
            continue
        try:
            cp = S.protect(name)
        except Exception as e:  # noqa: BLE001
            print("✗ %-22s 失败：%s" % (name, e))
            rc = 1
            continue
        size = cp.stat().st_size
        ok = S.verify(name)
        print("%s %-22s → %s（%d 字节，解密校验：%s）" % (
            "✓" if ok else "!", name, cp.name, size, "通过" if ok else "**不通过**"))
        if not ok:
            rc = 1
    print()
    print("⚠️ 提醒：读取点尚未改成 secrets_dpapi.load()，此刻**明文仍在、也仍被读取**。")
    print("   在这三处改完之前，本次加密不产生实际防护效果（见文件头表格）。")
    return rc


def cmd_verify() -> int:
    rc = 0
    for name in S.MANAGED:
        if not S.cipher_path(name).is_file():
            print("· %-22s 无密文" % name)
            continue
        ok = S.verify(name)
        print("%s %-22s 解密=%s" % ("✓" if ok else "✗", name,
                                    "与明文一致" if ok else "**不一致/解不开**"))
        if not ok:
            rc = 1
    return rc


def cmd_shred(yes: bool) -> int:
    if not yes:
        print("拒绝执行：删除明文是不可逆动作，必须同时给 `--shred --yes`。")
        print("建议先跑 `--verify` 确认每一份密文都能解回原文。")
        return 2
    rc = 0
    for name in S.MANAGED:
        if not S.plain_path(name).is_file():
            continue
        if not S.verify(name):
            print("✗ %-22s 密文校验不通过，**拒绝删除明文**（保留唯一一份凭据）" % name)
            rc = 1
            continue
        print("%s %-22s 明文已删除（密文 %s 保留）" % (
            "✓" if S.shred(name) else "!", name, S.cipher_path(name).name))
    if not rc:
        print()
        print("别忘了：读取点必须改成 secrets_dpapi.load()，否则删了明文后抓取会拿不到 Cookie。")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="明文会话凭据 → Windows DPAPI 密文（一次性迁移）")
    ap.add_argument("--check", action="store_true", help="只盘点状态（默认）")
    ap.add_argument("--protect", action="store_true", help="生成 .dpapi（明文保留）")
    ap.add_argument("--verify", action="store_true", help="校验密文可解回明文")
    ap.add_argument("--shred", action="store_true", help="删除明文（需 --yes）")
    ap.add_argument("--yes", action="store_true", help="配合 --shred 的二次确认")
    a = ap.parse_args()
    if a.shred:
        return cmd_shred(a.yes)
    if a.protect:
        return cmd_protect()
    if a.verify:
        return cmd_verify()
    return cmd_check()


if __name__ == "__main__":
    sys.exit(main())
