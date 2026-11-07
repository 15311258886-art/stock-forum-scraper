# -*- coding: utf-8 -*-
"""`expert_desk.py` —— 四家专家团的取数 / 回写工具（**不调模型**）。

用法（在项目根目录）：
    ./.venv/Scripts/python.exe expert_desk.py --teams
    ./.venv/Scripts/python.exe expert_desk.py --date latest --brief
    ./.venv/Scripts/python.exe expert_desk.py --date latest --status
    ./.venv/Scripts/python.exe expert_desk.py --save --key stock-partner --file out.json

## 为什么单独一个 CLI，而不是做成 HTTP 接口给 agent 调

04:00 的定时任务里，agent 需要的是「一次进程内拿到完整信息包」——
约 1.5 万字的市场数据 + 候选池，写成文件让 agent 直接读，比走 HTTP 再落盘少两层出错机会；
回写同理：agent 产出 JSON/Markdown 后一条命令落库，失败有明确退出码与非零 stderr。
**全程没有任何模型调用**：这里只搬数据，判断由 agent 自己做。

## 四条子命令各自的用途

- `--brief`  ：生成 `_expert/brief_<date>.md` + `_expert/universe_<date>.csv`（信息包）
- `--status` ：四家「装没装 / 今天有没有报告」—— 自动化据此决定跑哪家、跳过哪家
- `--save`   ：把 agent 的 JSON/Markdown 落库（upsert，一家一天一份）
- `--teams`  ：配置 + 本机安装情况

库路径默认 `viewer/app.db`，可用环境变量 `VIEWER_DB` 覆盖（与 viewer/db.py 同口径，
测试就是靠它把写入隔离到临时库，绝不动生产库）。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parent
# 两个路径都要进 sys.path，缺一个就会「看起来能跑、其实少了一大截」：
#   · PROJ_ROOT         —— `from viewer import ai_review`；
#   · PROJ_ROOT/viewer  —— ai_review 内部是**裸 import**（`import review_score` / `import evidence`），
#                          服务进程里 viewer/ 本来就在 sys.path 上（server.py 里同样是裸 import）。
#   CLI 不补这一条，实测会**同时**丢三块且 exit code 仍是 0（QA 独立验收实测 15232 → 8203 字）：
#                          ① 成绩单  ② 上次荐股对账单（都来自 review_score）
#                          ③ 八、本系统的实证边界（来自 evidence）
#  2026-11-04 起这条不再靠注释兜底：`_check_brief_deps()` 会显式 import 并在失败时 exit 3。
for _p in (str(PROJ_ROOT), str(PROJ_ROOT / "viewer")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from viewer import ai_expert as AX  # noqa: E402  —— 必须在 sys.path 注入之后

BRIEF_DIR = PROJ_ROOT / "_expert"


def _conn():
    """读写连接（--save 要写库，所以不能开只读）。路径由 ai_expert 统一决定。"""
    return AX.connect()


def _log(msg: str) -> None:
    print(msg)


# ---------------------------------------------------------------- --teams


def cmd_teams() -> int:
    """打印配置 + 每家在本机是否安装。给 agent 用：它据此决定去哪读提示词。"""
    teams = AX.load_teams()
    _log("专家团根目录：%s" % AX.EXPERT_ROOT)
    _log("配置文件    ：%s" % AX.CONFIG_PATH)
    _log("")
    _log("%-16s %-26s %-8s %s" % ("key", "name", "exists", "pack"))
    _log("-" * 88)
    for t in teams:
        inst = AX.is_installed(t)
        d = AX.pack_dir(t)
        # pack 为空但按中文名反查到了目录时，必须显示**真实目录名** ——
        # 否则自动化会以为"没配置去哪读提示词"，而包里其实是有提示词的。
        pack = t.get("pack") or (d.name if d is not None else "(未配置)")
        _log("%-16s %-26s %-8s %s" % (t["key"], t["name"], "true" if inst else "false", pack))
        if inst:
            lead = t.get("lead")
            _log("%-16s %-26s %-8s lead: %s" % (
                "", "", "",
                (d / lead) if lead else "（未配置 lead，自行 ls %s/agents 找主理人提示词）" % d))
        for m in t.get("members") or []:
            ok = ""
            d = AX.pack_dir(t)
            if d is not None:
                ok = " √" if (d / m).is_file() else " ×（缺）"
            _log("%-16s %-26s %-8s member: %s%s" % ("", "", "", m, ok))
    not_installed = [t["key"] for t in teams if not AX.is_installed(t)]
    _log("")
    _log("已安装 %d/%d 家；未安装：%s"
         % (len(teams) - len(not_installed), len(teams), "、".join(not_installed) or "无"))
    return 0


# ---------------------------------------------------------------- --status


def cmd_status(date_arg: str) -> int:
    """四家当天状态。自动化用它判断「哪家要跑、哪家已有报告不必重跑」。"""
    conn = _conn()
    try:
        date = AX.resolve_date(conn, date_arg)
        rows = AX.rows_for_date(conn, date)
        teams = AX.load_teams()
        _log("目标交易日：%s（latest=库里有数据的最大交易日；04:00 时它就是上一个交易日）" % date)
        _log("")
        _log("%-16s %-26s %-9s %-9s %-19s %s"
             % ("key", "name", "installed", "has", "created_at", "verdict"))
        _log("-" * 110)
        have = 0
        for t in teams:
            inst = AX.is_installed(t)
            r = rows.get(t["key"])
            if r:
                have += 1
            _log("%-16s %-26s %-9s %-9s %-19s %s" % (
                t["key"], t["name"],
                "true" if inst else "false",
                (r.get("status") or "ok") if r else "missing",
                (r.get("created_at") or "-") if r else "-",
                ((r.get("verdict") or "")[:40]) if r else "-"))
        _log("")
        todo = [t["key"] for t in teams
                if AX.is_installed(t) and t["key"] not in rows]
        skip = [t["key"] for t in teams if t["key"] in rows]
        _log("已有报告 %d 家；待跑 %d 家：%s；可跳过：%s"
             % (have, len(todo), "、".join(todo) or "无", "、".join(skip) or "无"))

        # 机器可读的一行，方便定时任务直接 grep/解析，不必去解析上面的对齐表格。
        _log("JSON: " + json.dumps({
            "date": date,
            "items": [{"key": t["key"], "installed": AX.is_installed(t),
                       "status": (rows.get(t["key"]) or {}).get("status") or "missing",
                       "created_at": (rows.get(t["key"]) or {}).get("created_at")}
                      for t in teams],
            "todo": todo,
        }, ensure_ascii=False))
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------- --brief

# 信息包质量门槛（2026-11-04 补，起因是 QA 独立验收抓到的"静默降级"）：
# 只要 `review_score` 或 `evidence` 有一个 import 不到，信息包就会**安静地**少掉
# 「上次荐股对账单 + 成绩单」和「本系统的实证边界」两大节（实测 15232 → 8203 字），
# exit code 还是 0 —— 明早 04:00 的定时任务只会看到"成功"，然后把一份残包交给四家 agent。
# 所以：先显式自检依赖，再对生成结果做硬门槛，任何一条不满足就 exit 3 且**不覆盖**旧文件。
BRIEF_MIN_CHARS = 8000
# 用**实际写出来的标题词**做子串匹配（不是需求里的口语名）：
#   「大盘」在正文里一次都没出现，真实标题是「## 一、市场环境」；
#   「龙虎榜」「上次荐股」则分别落在资金段与 review_score 的对账单段里
#   —— 后两个正是最容易因 import 失败而整段消失的部分，所以必须钉。
BRIEF_REQUIRED = ("市场环境", "主线板块", "龙虎榜", "实证边界", "上次荐股")
# ⚠ 耦合提醒：这 5 个串是 `viewer/ai_review.py` 里 `_render_user()` / `review_score.picks_context()`
#   实际写出来的标题文案。**谁改那边的标题措辞，必须同步这里**，否则 04:00 会每天准点 exit 3
#   且不出报告（fail-closed 总好过静默残包，但这个耦合必须显式写下来）。
#   已知对应关系：
#     「市场环境」← `## 一、市场环境`（口语里的"大盘技术面"就是这一节，正文没有"大盘"二字）
#     「主线板块」← `## 二、主线板块（…）`
#     「龙虎榜」  ← 资金段与独立性约定段
#     「实证边界」← `## 八、本系统的实证边界（…）`（来自 evidence，import 失败时整节消失）
#     「上次荐股」← `【上次荐股的结果 …】`（来自 review_score，import 失败时整段消失）
#   两个"canary"选的是最容易因 import 失败而消失的两节 —— 不要为了"好维护"换成固定骨架标题。


def _brief_quality(body: str) -> list:
    """检查信息包是否完整。返回问题列表（空列表 = 合格）。

    单独成函数而不是写在 cmd_brief 里：这样测试可以直接钉「什么样的包算残包」，
    不必真的去造一个坏库。
    """
    problems = [("缺少小节：%s" % k) for k in BRIEF_REQUIRED if k not in body]
    if len(body) < BRIEF_MIN_CHARS:
        problems.append("正文 %d 字 < 门槛 %d 字（疑似依赖缺失导致的残包）"
                        % (len(body), BRIEF_MIN_CHARS))
    return problems


def _check_brief_deps() -> str:
    """显式导入信息包的两块外部依赖。返回空串 = 正常，否则返回人话错误。

    为什么不用 `try: import` 包住整个 build_context：
    `ai_review` 内部对这两处是 `except Exception: log.exception(...)`（账本/对账单缺失
    不该阻断复盘）—— 那个设计对"服务里的 AI 复盘"是对的，对"04:00 的自动化信息包"是错的：
    它把致命问题降级成一行日志。

    为什么先把 `viewer/` 塞进 sys.path：`ai_review` 里是**裸 import**
    （`import review_score` / `import evidence`），只有在 viewer/ 位于 sys.path 时才解析得到。
    先坐实这一点，再用**和它完全相同的写法**去 import —— 那么这里通过，
    就代表 `build_context` 里那两处也一定通过（这才是"闸"的意义）。
    """
    viewer_dir = str(PROJ_ROOT / "viewer")
    if viewer_dir not in sys.path:
        sys.path.insert(0, viewer_dir)
    try:
        import review_score  # noqa: F401  —— 成绩单 + 上次荐股对账单
    except Exception as e:  # noqa: BLE001
        return ("review_score 导入失败（%s: %s）。缺它信息包会少掉「上次荐股对账单 + 成绩单」，"
                "四家 agent 看不到自己上期的成绩。" % (type(e).__name__, e))
    try:
        import evidence  # noqa: F401  —— 实证边界（prompt 里自称硬约束）
    except Exception as e:  # noqa: BLE001
        return ("evidence 导入失败（%s: %s）。缺它信息包会少掉「八、本系统的实证边界」整节，"
                "agent 会重新引用我们已证伪的常识。" % (type(e).__name__, e))
    return ""


def _fmt_num(v, nd: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return ("%." + str(nd) + "f") % float(v)
    except (TypeError, ValueError):
        return "—"


def _pool_md(pool: dict) -> str:
    """候选池 → markdown 表格（供 agent 直接读，也方便人肉核对）。"""
    rows = pool.get("rows") or []
    if not rows:
        return ""
    L = ["## 候选池（按现有打分排序，供选股用）", "",
         "> 来源：%s" % pool.get("note", ""), "",
         "| # | 代码 | 名称 | 现价 | 涨跌幅% | 成交额(亿) | 分数 | 理由 |",
         "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        amt = r.get("amount")
        amt_s = _fmt_num(amt / 1e8 if isinstance(amt, (int, float)) else None, 2)
        L.append("| %d | %s | %s | %s | %s | %s | %s | %s |" % (
            i, r.get("code") or "", r.get("name") or "",
            _fmt_num(r.get("price"), 2), _fmt_num(r.get("pct"), 2), amt_s,
            _fmt_num(r.get("score"), 3) if r.get("score") is not None else "—",
            str(r.get("reason") or "").replace("|", "／")))
    return "\n".join(L)


def _write_universe_csv(path: Path, pool: dict) -> int:
    """候选池 → CSV。返回数据行数（不含表头）。

    三个必须讲清的点：
    · **utf-8-sig**（带 BOM）：Excel 双击打开不乱码；agent 用 utf-8/gbk 读也都能处理 BOM。
    · **`newline=""`**：`csv.writer` 默认行结束符就是 `\\r\\n`，而文本模式写文件时 Python 会把
      每个 `\\n` 再翻成 `\\r\\n` —— 两下一叠加每行变成 `\\r\\r\\n`，`csv.reader` / pandas 会读到
      一堆空行（QA 实测 40 行数据被数成 81 行、pandas 得到 40 行真数据 + 40 行 NaN）。
      关掉换行翻译（`newline=""`）是官方文档给的标准解法，不要再去改 lineterminator。
    · 表头**必须带单位**：brief 里那张 markdown 表写的是「成交额(亿)」，CSV 存的是**元**，
      只写「成交额」会让 agent 按亿读，差 1e8 倍 —— 这类错不会报错，只会让结论悄悄错。
    """
    rows = pool.get("rows") or []
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["代码", "名称", "现价", "涨跌幅(%)", "成交额(元)", "分数", "理由"])
        for r in rows:
            w.writerow([
                r.get("code") or "", r.get("name") or "",
                "" if r.get("price") is None else round(float(r["price"]), 2),
                "" if r.get("pct") is None else round(float(r["pct"]), 2),
                "" if r.get("amount") is None else int(float(r["amount"])),
                "" if r.get("score") is None else round(float(r["score"]), 6),
                (r.get("reason") or "").replace("\n", " "),
            ])
    return len(rows)


def cmd_brief(date_arg: str) -> int:
    """生成信息包。返回 0 成功 / 3 信息包不完整（旧文件保留、不覆盖）。

    主体直接复用 `ai_review._render_user(ai_review.build_context(...))` ——
    **绝不自己拼**：专家团看的必须是同一份事实底稿，否则四家报告会和
    WorkBuddy/DeepSeek 的复盘互相矛盾，用户无从判断谁对。
    provider 固定传 workbuddy：它只影响「上次同大脑结论」这一段注入。
    """
    from viewer import ai_review as AR

    # 第一道闸：依赖。必须在 build_context 之前 —— 进去以后就没机会响了（异常被内部吞掉）。
    dep_err = _check_brief_deps()
    if dep_err:
        _log("错误：%s" % dep_err)
        _log("已中止，未生成也未覆盖任何文件。修好后重跑："
             "./.venv/Scripts/python.exe expert_desk.py --date latest --brief")
        return 3

    conn = _conn()
    try:
        date = AX.resolve_date(conn, date_arg)
        # 建表：保证 status/save 能立刻工作（第一次部署时库是空的）
        AX.ensure_schema(conn)

        t0 = time.time()
        ctx = AR.build_context(conn, date, provider="workbuddy")
        body = AR._render_user(ctx)
        elapsed = time.time() - t0

        pool = AX.candidate_pool(conn, date, limit=40)
        pool_note = pool.get("note") or ""

        BRIEF_DIR.mkdir(parents=True, exist_ok=True)
        brief_path = BRIEF_DIR / ("brief_%s.md" % date)
        uni_path = BRIEF_DIR / ("universe_%s.csv" % date)

        head = [
            "# 四家专家团 · 当日信息包 %s" % date,
            "",
            "- 生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
            "- 目标交易日：%s（`--date latest` 取的是库里有数据的最大交易日；04:00 时即上一个交易日）" % date,
            "- 包里有什么：① 完整信息包（与 AI 复盘同一份事实底稿，来自 viewer/ai_review.py 的 "
            "build_context + _render_user，本次取数 %.1fs / %d 字）"
            % (elapsed, len(body)),
        ]
        if pool.get("rows"):
            head.append("- 候选池：前 %d 条（来源 %s）→ 另存 `_expert/universe_%s.csv`"
                        % (len(pool["rows"]), pool.get("source") or "-", date))
        else:
            # note 自己已经说了「本节已跳过」，这里只交代原因，不再重复那句话
            head.append("- 候选池：无 —— %s" % pool_note)
        head += [
            "- 说明：本文件由 `expert_desk.py --brief` 生成，**不含任何模型调用**；"
            "分析由专家团 agent 完成，结论用 `--save` 回写 `ai_expert_review`。",
            "",
            "---",
            "",
        ]
        text = "\n".join(head) + body + "\n"
        pool_md = _pool_md(pool)
        if pool_md:
            text += "\n\n" + pool_md + "\n"

        # 第二道闸：先写 .tmp，验完再**原子替换**。
        # 为什么不直接写正式文件：写成残包再删掉，会留下一个「文件存在但很短」的时间窗，
        # 而 04:00 的 agent 可能正好在这个窗口里读它。os.replace 是原子的 —— 要么旧的、要么新的。
        # 并发说明：.tmp 名固定，同日两个 brief 同时跑会抢同一个临时文件。当前只有 04:00 单任务，
        # 属于可接受的假设；若将来要并行，把 pid 拼进 tmp 名即可。
        tmp_path = brief_path.with_suffix(".md.tmp")
        tmp_path.write_text(text, encoding="utf-8")

        problems = _brief_quality(body)
        if problems:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            _log("信息包不完整：%s" % "；".join(problems))
            _log("已保留旧文件，未覆盖：%s" % brief_path)
            _log("（旧文件若不存在，则今天没有可用的信息包 —— 请先修好数据/依赖再重跑）")
            return 3

        # CSV 也是「先 tmp 再 replace」：brief 里引用了 csv 的条数，两者是一对。
        # 只保证 md 原子、csv 直接覆盖的话，csv 写失败会留下「新 brief + 旧 csv」——
        # 那种组合不报错，只会让 agent 按 brief 里写的「40 条」去读一份旧名单。
        csv_tmp = uni_path.with_suffix(".csv.tmp")
        try:
            n_rows = _write_universe_csv(csv_tmp, pool)
        except OSError as e:
            for _p in (tmp_path, csv_tmp):
                try:
                    _p.unlink()
                except OSError:
                    pass
            _log("错误：候选池 CSV 写入失败（%s），本次不替换任何文件，旧文件保留" % e)
            return 3

        os.replace(str(tmp_path), str(brief_path))
        os.replace(str(csv_tmp), str(uni_path))

        _log("brief   : %s" % brief_path)
        # 两个数必须分别打印：门槛判的是**正文**（body），落盘的是**全文**（含头部与候选池）。
        # 只打全文会让人以为门槛判的是 15232，下次动门槛时容易调错量级。
        _log("字数    : 正文 %d 字（门槛按它判） / 全文 %d 字" % (len(body), len(text)))
        _log("节数    : %d 个必备小节全部在位" % len(BRIEF_REQUIRED))
        _log("universe: %s（%d 行数据 + 1 行表头）" % (uni_path, n_rows))
        _log("最新交易日: %s" % date)
        _log("候选池来源: %s" % (pool.get("source") or "（无，本节已跳过）"))
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------- --save


def cmd_save(date_arg: str, key: str, file_path: str) -> int:
    """把 agent 交付的 JSON / Markdown 落库（upsert）。"""
    if not str(key or "").strip():
        _log("错误：--save 必须带 --key（例如 --key stock-partner）")
        return 2
    if not str(file_path or "").strip():
        _log("错误：--save 必须带 --file（agent 产出的 .json 或 .md）")
        return 2
    conn = _conn()
    try:
        res = AX.save_path(conn, file_path, date_arg, key)
        _log("已落库：id=%s review_date=%s key=%s status=%s format=%s"
             % (res.get("id"), res.get("review_date"), res.get("key"),
                res.get("status"), res.get("format")))
        _log("verdict：%s" % (res.get("verdict") or "—"))
        return 0
    except FileNotFoundError as e:
        _log("错误：%s" % e)
        return 3
    except (ValueError, json.JSONDecodeError) as e:
        _log("错误：%s" % e)
        return 2
    finally:
        conn.close()


# ---------------------------------------------------------------- --board


def cmd_board(date_arg: str, min_score: int = 0) -> int:
    """打印「今日荐股 · 合并榜」：四家 picks 合并、按各家自评分排序。

    为什么给 agent 也留这个口子：定时任务的最后一步要汇报"今天几家推了哪些票"。
    让它自己从四份 JSON 里拼，各家字段写法稍有出入就会拼错；
    这里读的是**落库后的同一份数据**，页面上看到什么，汇报里就是什么。
    """
    conn = _conn()
    try:
        d = AX.resolve_date(conn, date_arg)
        items = AX.build_items(conn, d)
        ms = min_score or AX.MIN_SCORE
        b = AX.build_board(items, min_score=ms)
        _log("交易日 %s · 合并榜（各家用自己体系打分，不跨家比较）" % d)
        _log("≥%d 分：%d 只；参与打分的可执行标的共 %d 只；覆盖 %d 家"
             % (b["min_score"], b["n_strong"], b["n_scored"], b["n_teams"]))
        _log("")
        if not b["items"]:
            _log("（当天没有带评分的 picks —— 要么报告是评分功能上线前写的，要么今天各家都没给票）")
            return 0
        _log("%-4s %-7s %-12s %-24s %-10s %s"
             % ("分", "代码", "名称", "专家团", "动作", "理由"))
        _log("-" * 110)
        for r in b["items"]:
            _log("%-4d %-7s %-12s %-24s %-10s %s%s"
                 % (r["score"], r["code"], (r["name"] or "")[:6], (r["team"] or "")[:12],
                    (r["action"] or "")[:5], "" if r["strong"] else "[观察] ",
                    (r["reason"] or "").replace("\n", " ")[:60]))
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------- main


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="expert_desk.py",
        description="四家专家团每日评估：取数（brief）/ 查状态（status）/ 回写（save）/ 看配置（teams）。"
                    "本工具不调任何模型。",
        epilog="返回码：0=成功；2=参数/文件内容有误（--save）；3=信息包不完整或依赖缺失（--brief，"
               "此时不覆盖已有的 brief_<date>.md）。"
               "示例：./.venv/Scripts/python.exe expert_desk.py --date latest --status")
    p.add_argument("--date", default="latest",
                   help="交易日：latest（默认，取库里有数据的最大交易日）或 YYYY-MM-DD")
    p.add_argument("--brief", action="store_true",
                   help="生成信息包到 _expert/brief_<date>.md 与 _expert/universe_<date>.csv。"
                        "返回码 0=成功，3=信息包不完整（缺依赖/缺小节/字数不足，旧文件保留不覆盖）")
    p.add_argument("--status", action="store_true",
                   help="打印四家的安装情况与当天是否已有报告（自动化据此决定跑哪家）")
    p.add_argument("--save", action="store_true",
                   help="把 JSON / Markdown 报告落库（需配合 --key 与 --file）")
    p.add_argument("--key", default="", help="专家团 key，取值见 config/expert_teams.json")
    p.add_argument("--file", default="", help="要落库的文件路径（.json 或 .md）")
    p.add_argument("--teams", action="store_true", help="打印四家配置及本机是否已安装专家包")
    p.add_argument("--board", action="store_true",
                   help="打印「今日荐股 · 合并榜」：四家 picks 按各家自评分排序，"
                        "≥85 分全列（不足 5 只用次高分补足并标 [观察]）")
    p.add_argument("--min-score", type=int, default=0,
                   help="配合 --board：达标线，默认 85（0 = 用内置默认值）")
    args = p.parse_args(argv)

    # 优先级：--teams / --status / --brief / --save / --board（互斥但都给了就按安全顺序先做只读）
    if args.teams:
        return cmd_teams()
    if args.status:
        return cmd_status(args.date)
    if args.board:
        return cmd_board(args.date, args.min_score)
    if args.save:
        return cmd_save(args.date, args.key, args.file)
    if args.brief:
        return cmd_brief(args.date)
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
