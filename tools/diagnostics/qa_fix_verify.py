# -*- coding: utf-8 -*-
"""QA FAIL-1a/1b/1c 修复验证（跑**真解析器**，2026-10-10）。

用法：.venv/Scripts/python.exe tools/qa_fix_verify.py
证据口径：
  · 1a：QA 用例 '买入九阳股份/传艺科技/华北制药' 应 1 条；并统计语料里
        "单动作词认领 ≥2 只股" 的帖数（旧基线 = QA 报的 74/1067，新应 = 0）。
  · 1b：'卖出600519' → 1 条 SELL 贵州茅台；'600519 涨停了' → 0 条。
  · 1c：code_name 四码抽查。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import name_match as nm  # noqa: E402
from scraper.parsers.taoguba import TaogubaParser  # noqa: E402

ACTION_RE = TaogubaParser.ACTION_RE

CORPUS = Path(r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据\players_posts.jsonl")

# ---- 给 ③/④ 装计量探针：按"动作词位置"记它认领了哪些标的 ----
_CLAIMS: dict[int, set] = {}
_ORIG_MFA = nm.match_after
_ORIG_MEND = nm.match_ending
_ORIG_ATA = nm.associate_to_action


def _verb_pos_at_end(text: str, i: int) -> int | None:
    """match_after(i=动词 end) → 回推动词起点（取 ACTION_RE 中 end==i 的匹配）。"""
    hits = [m.start() for m in ACTION_RE.finditer(text) if m.end() == i]
    return hits[0] if hits else None


def _mfa(text, i, max_len=6):
    got = _ORIG_MFA(text, i, max_len=max_len)
    pos = _verb_pos_at_end(text, i)
    if got and pos is not None:
        _CLAIMS.setdefault(pos, set()).add(got[0])
    return got


def _mend(text, end, max_len=None):
    got = _ORIG_MEND(text, end, max_len=max_len)
    if got:
        _CLAIMS.setdefault(end, set()).add(got[0])
    return got


def _ata(text, apos, kw, names, window=nm.ASSOC_WINDOW, block=nm.ASSOC_BLOCK):
    got = _ORIG_ATA(text, apos, kw, names, window=window, block=block)
    if got:
        _CLAIMS.setdefault(apos, set()).add(got[0])
    return got


def extract_with_meter(p, text):
    global _CLAIMS
    _CLAIMS = {}
    trades = p._extract_trades(text)
    multi = sum(1 for _pos, s in _CLAIMS.items() if len(s) >= 2)
    return trades, multi


def show(tag, text, p):
    trades, multi = extract_with_meter(p, text)
    rows = ", ".join("%s/%s" % (t["action"], t.get("symbol_name") or t.get("symbol"))
                     for t in trades)
    print("  [%s] %r" % (tag, text))
    print("       → %d 条：%s%s" % (len(trades), rows or "（空）",
                                   "   ⚠单动作词认领≥2" if multi else ""))


def main():
    p = TaogubaParser({})

    print("=" * 74)
    print("FAIL-1a  规则③与④重复认领同一动作词")
    print("=" * 74)
    show("期望1条", "买入九阳股份/传艺科技/华北制药", p)
    show("QA副作用探针(期望1条,漏第2只为预期取舍)", "清仓宁德时代、贵州茅台", p)
    show("回归(仍应2条:两个动词)", "清仓贵州茅台，买入宁德时代", p)
    show("回归(名前动后)", "德石股份割了", p)

    print()
    print("=" * 74)
    print("FAIL-1b  裸 6 位代码")
    print("=" * 74)
    show("期望1条 SELL", "卖出600519", p)
    show("期望0条(无动词)", "600519 涨停了", p)
    show("期望1条 BUY", "买入300750", p)
    show("期望0条(非已知码)", "卖出123456", p)
    show("期望0条(日期/长数字不算)", "2026-10-09 收盘", p)
    show("括号式仍工作", "莲花控股(600186)出剩二手", p)

    print()
    print("=" * 74)
    print("FAIL-1c  code_name 规范名")
    print("=" * 74)
    for code, want in (("600519", "贵州茅台"), ("300750", "宁德时代"),
                       ("601318", "中国平安"), ("000001", "平安银行")):
        got = nm.code_name(code)
        print("  %s → %s  %s" % (code, got, "OK" if got == want else "✗期望 %s" % want))

    print()
    print("=" * 74)
    print("语料级：'单动作词认领 ≥2 只股' 的帖数（旧基线=QA 报 74/1067）")
    print("=" * 74)
    n_posts = n_multi = n_before_rows = n_after_rows = 0
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("site") != "taoguba" or r.get("text_kind") != "full":
            continue
        text = r.get("text") or ""
        if not text:
            continue
        n_posts += 1
        trades, multi = extract_with_meter(p, text)
        n_after_rows += len(trades)
        if multi:
            n_multi += 1
    print("  语料 taoguba/full 帖数 = %d" % n_posts)
    print("  '单动作词认领 ≥2 只股' 帖数 = %d  (%.1f%%)   ← 新规则应≈0"
          % (n_multi, 100.0 * n_multi / max(1, n_posts)))
    print("  新规则交易总条数 = %d" % n_after_rows)


if __name__ == "__main__":
    main()
