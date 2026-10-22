# -*- coding: utf-8 -*-
"""
fund_masters.py  —  基金经理策略查看器 · 巴菲特深度研报并入脚本

【重要背景，请勿误读为本项目的"选股引擎"】
原 732 行选股引擎（本地量化筛选 27 位经理、评分、分档、生成研报、聚合共识）
在本机已丢失且无任何备份（git 未跟踪、__pycache__ 已被破坏脚本覆盖重编译、
全盘搜索无副本）。因此本文件不再是自动选股引擎，而是一段**幂等、只动 buffett**
的修复脚本：把「巴菲特价值投资选股」的独立深度研报管线并入 fund_masters 查看器。

查看器实际消费的是生成产物（index.json + reports/<key>/*.html），
且 iframe 通过 reportFrame.src='reports/'+latest 直接加载研报 HTML，
故无需引擎源码即可完成修复。

本脚本职责（只动 buffett 相关，不触碰其余 26 位经理）：
  1. 把 static/buffett/reports/*.html（7 期深度研报）复制到
     static/fund_masters/reports/buffett/。
  2. 改写 index.json 中 managers.buffett 的 latest/history/counts/picks，
     指向深度研报最新一期（2026-10-13）。
  3. 按原引擎共识规则（每位经理取 core+watch 的 Top5）重算 consensus，
     用深度研报的标的替换原先浅层自动生成的标的。

若日后找回原引擎源码，应恢复其完整逻辑；本文件仅用于在当前无源码情况下维持查看器一致。
"""
import json
import os
import shutil
import datetime
import sys

ROOT = "viewer/static/fund_masters"
SRC = "viewer/static/buffett"
IDX = os.path.join(ROOT, "index.json")


def top5(picks):
    """原引擎共识规则：取 core+watch 的 Top5（core 优先于 watch，剔除 out）。"""
    order = {"core": 0, "watch": 1, "out": 2}
    sel = [p for p in picks if p.get("tier") in ("core", "watch")]
    sel.sort(key=lambda p: order.get(p.get("tier"), 9))
    return [p["code"] for p in sel[:5]]


def main():
    if not os.path.exists(IDX):
        print("ERROR: 找不到", IDX, file=sys.stderr)
        return 1

    # 0. 备份
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = IDX + ".bak-inject-" + ts
    shutil.copy2(IDX, bak)
    print("backup ->", bak)

    # 1. 读取深度研报来源
    bd = json.load(open(os.path.join(SRC, "index.json"), encoding="utf-8"))
    deep_reports = bd["reports"]
    latest = deep_reports[0]
    deep_files = [os.path.basename(r["file"]) for r in deep_reports]
    print("deep latest:", latest["period"], "files:", deep_files)

    # 2. 复制深度研报 HTML
    dst_dir = os.path.join(ROOT, "reports", "buffett")
    os.makedirs(dst_dir, exist_ok=True)
    for r in deep_reports:
        shutil.copy2(os.path.join(SRC, r["file"]),
                     os.path.join(dst_dir, os.path.basename(r["file"])))
    print("copied", len(deep_reports), "deep report html ->", dst_dir)

    # 3. 改写 buffett 入口
    idx = json.load(open(IDX, encoding="utf-8"))
    mg = idx["managers"]
    assert "buffett" in mg, "index.json 缺少 buffett 入口"
    b = mg["buffett"]
    b["latest"] = "buffett/" + os.path.basename(latest["file"])
    b["history"] = deep_files
    b["counts"] = latest["counts"]
    b["picks"] = latest["picks"]
    print("buffett.latest =", b["latest"], "| counts =", b["counts"])

    # 4. 重算 consensus
    new_codes = set(top5(latest["picks"]))
    bc = idx["consensus"]["by_code"]
    old_codes = {c for c, info in bc.items() if "buffett" in info.get("managers", [])}
    for code in old_codes - new_codes:
        info = bc[code]
        info["managers"] = [m for m in info["managers"] if m != "buffett"]
        info["n"] = len(info["managers"])
        if info["n"] == 0:
            del bc[code]
    name_of = {p["code"]: p["name"] for p in latest["picks"]}
    for code in new_codes - old_codes:
        if code in bc:
            if "buffett" not in bc[code]["managers"]:
                bc[code]["managers"].append("buffett")
            bc[code]["n"] = len(bc[code]["managers"])
        else:
            bc[code] = {"code": code, "name": name_of.get(code, ""), "n": 1, "managers": ["buffett"]}
    idx["consensus"]["ranked"] = sorted(bc.values(), key=lambda x: (-x["n"], x["code"]))

    json.dump(idx, open(IDX, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("written ->", IDX, "| consensus entries:", len(bc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
