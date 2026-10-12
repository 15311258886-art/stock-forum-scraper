# -*- coding: utf-8 -*-
"""建议9 / P3 工程卫生：把散落在工程根目录的「过渡态垃圾」归档进 backups/archive-20261005/。

原则：**只移不删**——原文件一律 move 到归档目录，绝不 delete；
      若某个文件仍被 .md / .html 报告引用（搬走会让图文关系断掉），跳过它并记进 SKIPPED。

跑法：.venv\\Scripts\\python.exe _hygiene_archive.py
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DST = ROOT / "backups" / "archive-20261005"
SUB = {"bak": DST / "bak", "shots": DST / "shots", "logs": DST / "logs", "db": DST / "db"}

# 已经被正式目录收编的，不碰
KEEP_DIRS = {".venv", ".git", "__pycache__", "node_modules", "backups", "archive",
             "viewer", "scraper", "config", "tools", "tests", "data", "artifacts"}

# (子串/后缀, 目标子目录, whole=True 表示必须整名结尾才匹配)
MOVE_PATTERNS = [(".bak", "bak", False), (".png", "shots", True), (".log", "logs", True)]
# 隔离测试库 / Edge profile 残留（只移不删）
RESIDUE_DIRS = ["artifacts", "tests", "data"]


def referenced_names() -> set[str]:
    """被 .md/.html 文档引用的图片/日志名（搬走会让报告缺图）→ 跳过。"""
    import re
    names: set[str] = set()
    pat = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)", re.I)
    for f in ROOT.glob("*.md"):
        try:
            t = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in pat.findall(t):
            if "/" not in m.replace("\\", "/"):
                names.add(Path(m.replace("\\", "/")).name)
    return names


def main() -> int:
    for d in SUB.values():
        d.mkdir(parents=True, exist_ok=True)
    ref = referenced_names()
    moved: list[tuple[str, str]] = []
    skipped: list[str] = []

    # --- 1) 根目录散落的过渡态文件 ---
    for p in sorted(ROOT.iterdir()):
        if not p.is_file():
            continue
        name = p.name
        if name.startswith(".") or name == Path(__file__).name:
            continue
        kind = None
        for suffix, sub, whole in MOVE_PATTERNS:   # whole=True → 走 endswith（文件结尾），否则是子串
            if name.endswith(suffix) if whole else (suffix in name):
                kind = sub
                break
        if kind is None:
            continue
        if name in ref:
            skipped.append(name)
            continue
        dst = SUB[kind] / name
        i = 1
        while dst.exists():
            dst = SUB[kind] / f"{name}.dup{i}"
            i += 1
        shutil.move(str(p), str(dst))
        moved.append((name, str(dst.relative_to(ROOT))))

    # --- 2) 隔离测试库 / Edge profile 残留 ---
    residue: list[tuple[str, str]] = []
    for sub in RESIDUE_DIRS:
        src = ROOT / sub
        if not src.exists():
            continue
        # tests/ 与 data/ 是活目录，只搬明确属于「隔离测试库」的那一份
        if sub == "tests":
            for f in (src / "qa_iso").glob("*"):
                if f.is_file():
                    dst = SUB["db"] / f.name
                    shutil.move(str(f), str(dst))
                    residue.append((str(f.relative_to(ROOT)), str(dst.relative_to(ROOT))))
            if not any(src.rglob("*")):
                shutil.move(str(src), str(SUB["db"] / "tests"))
                residue.append(("tests/", "backups/archive-20261005/db/tests"))
        else:
            pass  # artifacts/ data/ 交给下面统一处理

    # --- 3) 写出清单（只移不删的凭据） ---
    lines = ["# archive-20261005 工程卫生归档清单（只移不删）", "",
             f"共移动 {len(moved)} 个文件 + {len(residue)} 个残留项。", ""]
    for a, b in moved:
        lines.append(f"- {a}  ->  {b}")
    for a, b in residue:
        lines.append(f"- [残留] {a}  ->  {b}")
    if skipped:
        lines += ["", "## 以下文件被 .md 报告引用，未搬走："] + [f"- {n}" for n in sorted(skipped)]
    (DST / "MANIFEST.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"moved={len(moved)}  residue={len(residue)}  skipped={len(skipped)}")
    for a, b in moved[:5]:
        print(f"  {a}  ->  {b}")
    if skipped:
        print("  skipped:", skipped)
    print(f"manifest -> {DST / 'MANIFEST.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
