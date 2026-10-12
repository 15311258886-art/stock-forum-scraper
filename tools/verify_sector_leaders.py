# -*- coding: utf-8 -*-
"""校验 sector_leaders 名单：代码 ↔ 名称 是否对得上（新浪实时行情为准）。

用法：.venv\\Scripts\\python.exe tools\\verify_sector_leaders.py [--write]
  --write 把校正后的名称回写进 sector_leaders.py（新浪返回的规范简称）。

校验视角：
  · 代码存在但行情拿不到 → 「代码过期/停牌退市」；
  · 行情能取到但名称对不上 → 名单里写错字 / 代码换了主体，必须修；
  · 名称一致但为空 → 行情当日无数据（多半是停牌），代码本身没错。
"""

import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sector_chain            # noqa: E402
import sector_leaders          # noqa: E402

OUT = []


def _full2half(s: str) -> str:
    """全角 Ａ→A（新浪简称里 A 股常带全角字母，如 万 科Ａ）。"""
    return str(s).translate(str.maketrans("ＡＢＣＤＥＦＧＨＩＪａｌ", "ABCDEFGHIJaA"))


def norm(s: str) -> str:
    """归一化：去空白、ST/*ST 前缀、除权标识（XD/XR/DR）、全角转半角。

    目的：ST / 除息只是当天状态，不该判定为「名单写错」；「万 科Ａ」与「万科A」
    是同一只。真正要报警的，是代码挂到了不同主体（如 300798=锦鸡股份而非尚品宅配）。
    """
    t = _full2half(s)
    t = re.sub(r"^\*?ST", "", t, flags=re.I)
    t = re.sub(r"^(XD|XR|DR)", "", t, flags=re.I)   # 除权/除息前缀，别让它判成"换了个股"
    return re.sub(r"\s+", "", t).upper()


def same(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    if a == b:
        return True
    # 「XD海螺水」→「海螺水泥」这类简称截断：一方是另一方的前缀即视为同一只
    return a.startswith(b) or b.startswith(a)


def main(write_back: bool = False) -> int:
    pairs = {}
    order = []
    for sec, lst in sector_leaders.SECTOR_LEADERS.items():
        for code, name in lst:
            if code not in pairs:
                pairs[code] = name
                order.append(code)
    # 先查板块内重复（同一板块里同一只重复只是白占一行，前端还会点两次同一个）
    dup = []
    for sec, lst in sector_leaders.SECTOR_LEADERS.items():
        seen = {}
        for code, name in lst:
            if code in seen:
                dup.append((sec, code, seen[code], name))
            seen[code] = name
    if dup:
        OUT.append("=== 板块内重复 ===")
        for sec, code, a, b in dup:
            OUT.append("  %s  %s  重复：%s / %s" % (sec, code, a, b))
        OUT.append("")

    q = sector_chain.batch_quotes(order)
    bad, empty, ok = [], [], 0
    for code in order:
        mine = pairs[code]
        got = (q.get(code) or {}).get("name") or ""
        if not got:
            empty.append(code)
            continue
        if same(got, mine):
            ok += 1
        else:
            bad.append((code, mine, got))
    OUT.append("校验 %d 条 | 一致 %d | 名称不符 %d | 行情无数据 %d"
               % (len(order), ok, len(bad), len(empty)))
    if bad:
        OUT.append("")
        OUT.append("=== 名称不符（必须修）===")
        for c, mine, got in bad:
            OUT.append("  %s  名单=%s  行情=%s" % (c, mine, got))
    if empty:
        OUT.append("")
        OUT.append("=== 行情无数据（代码可能过期）===")
        for c in empty:
            OUT.append("  %s %s" % (c, pairs[c]))
    if write_back and bad:
        OUT.append("")
        OUT.append("--write 暂不自动改文件，请人工按上面清单修正。")

    text = "\n".join(OUT)
    print(text)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main("--write" in sys.argv))
