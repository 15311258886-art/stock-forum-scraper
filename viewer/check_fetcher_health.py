#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
论坛实时抓取管道健康巡检 + 数据真实性校验（高优先级告警钩子）

背景：产品体检发现两类盲区：
  (A) /api/scheduler/status 的 errors_today 只累计不告警，实时管道被外部源风控掐断无人发现；
  (B) 更隐蔽的「数据真实性」问题——某张表陈旧/冻住、标的集中度畸高却仍「结构自洽」，
      结构测试全绿却给出错误结论（如旧 trades 冻在 4-5 只票、Top5 集中度 65%）。
本脚本作为独立巡检，零侵入：
  - 只读本地 API（scheduler/status、refresh_status）判断抓取管道健康；
  - 只读本地 app.db（mode=ro，不改库、不改 server.py）判断数据真实性。

  巡检项：
    [抓取] 1. 交易窗口内高错误率（errors_today）
           2. 交易窗口内数据陈旧（last_ok_at 距现在）
    [真实性] 3. 集中度硬上限（Top1/Top5 占比，无基线也能拦）
           4. 集中度漂移（相对滚动基线突增）
           5. 数据冻结（表 MAX(post_time) 冻住但抓取器自称近期成功）

  命中写入 viewer/alerts.log（带去重，同条件 DEDUPE_HOURS 内不重复刷屏），
  同时弹 Windows 桌面置顶提示框主动提醒（复用同一去重频率）。
  去重状态存 viewer/.alert_state.json。非交易窗口期抓取类规则跳过，真实性规则照常跑。
"""
import json
import os
import sys
import time
import datetime
import urllib.request
import urllib.error
import sqlite3
import subprocess
import tempfile

BASE = "http://127.0.0.1:8765"
HERE = os.path.dirname(os.path.abspath(__file__))
ALERT_LOG = os.path.join(HERE, "alerts.log")
STATE_F = os.path.join(HERE, ".alert_state.json")
DB_F = os.environ.get("VIEWER_DB") or os.path.join(HERE, "app.db")  # 只读校验 trades 真实性

# ---- 抓取管道阈值 ----
ERR_THRESHOLD = 10      # 交易窗口内当日累计失败 >= 此值 -> 高错误率告警
STALE_HOURS = 3         # 交易窗口内距上次成功抓取 > 此小时 -> 数据陈旧告警
DEDUPE_HOURS = 6        # 同条件最短告警间隔，避免刷屏

# ---- 数据真实性阈值 ----
TOP1_CEIL = 30.0        # Top1 标的占比硬上限(%)：健康论坛不应单只票占三成
TOP5_CEIL = 60.0        # Top5 标的占比硬上限(%)：旧数据 65.4% 会触发
DRIFT_MULT = 1.5        # 漂移：当前 Top5 > 基线*1.5 且 +DRIFT_PAD 才告警
DRIFT_PAD = 10.0        # 漂移附加百分点
FREEZE_DAYS = 3         # 表 MAX(post_time) 冻住超过此天数
FREEZE_OK_DAYS = 2      # 且抓取器最近此天内自称成功 -> 判定「表没跟上」

# ---- 集中度指标的适用前提（2026-11-02 补） ----
# 教训：trades 表只有 24 条、4 个标的时，Top5 占比**必然**是 100%，
# Top1 也轻易越过 30%。这不是"数据异常"，是小学数学 —— 指标在小样本下无定义。
# 旧版没有这道闸，导致每 6 小时雷打不动弹两条 CRITICAL/WARN（且因 VBS 编码 bug
# 变成两个脚本错误框），属于纯误报刷屏。样本不够时宁可不判断，也不许乱报。
CONC_MIN_TOTAL = 100    # 总记录数下限
CONC_MIN_SYMBOLS = 6    # 标的数下限（≤5 时 Top5 占比恒为 100%）


def _get(path, timeout=10, tries=1, gap=3.0):
    """GET 本地接口。tries>1 时对"连不上"做重试 ——
    后端重启（改代码后 reload、守护进程拉起）通常 10~30s，旧版一次连不上就
    直接判"服务不可达"并弹 CRITICAL，属于把"运维动作"误报成"线上故障"。"""
    last = (None, {"_err": "no attempt"})
    for i in range(max(1, tries)):
        try:
            with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
                return 200, json.loads(r.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            return e.code, None            # 服务活着，只是这接口报错，重试无意义
        except Exception as e:  # noqa: BLE001
            last = (None, {"_err": str(e)[:160]})
            if i + 1 < tries:
                time.sleep(gap)
    return last


def _parse_dt(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except Exception:  # noqa: BLE001
            pass
    return None


def load_state():
    try:
        return json.load(open(STATE_F, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def get_trades_stats():
    """只读 app.db 计算 trades 真实性指标。失败返回 None（不影响抓取类规则）。"""
    if not os.path.exists(DB_F):
        return None
    try:
        uri = "file:" + DB_F + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        cur = conn.cursor()
        # 标的集中度
        rows = cur.execute(
            "SELECT symbol, COUNT(*) FROM trades GROUP BY symbol"
        ).fetchall()
        total = sum(c for _, c in rows) or 0
        counts = sorted((c for _, c in rows), reverse=True)
        top1 = (counts[0] / total * 100) if total else 0.0
        top5 = (sum(counts[:5]) / total * 100) if total else 0.0
        # 时间冻结
        mx = cur.execute("SELECT MAX(post_time) FROM trades").fetchone()[0]
        max_dt = _parse_dt(mx) if mx else None
        # 近 30 天独立作者数（活跃塌缩用）
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
        recent_authors = cur.execute(
            "SELECT COUNT(DISTINCT author) FROM trades WHERE post_time >= ?", (cutoff,)
        ).fetchone()[0]
        all_authors = cur.execute("SELECT COUNT(DISTINCT author) FROM trades").fetchone()[0]
        conn.close()
        return {
            "total": total, "top1": top1, "top5": top5,
            "n_symbols": len(rows),
            "max_dt": max_dt, "recent_authors": recent_authors,
            "all_authors": all_authors,
        }
    except Exception:  # noqa: BLE001
        return None


def conc_usable(stats):
    """集中度指标是否具备适用前提。返回 (可用?, 不可用原因)。

    两个下限任一不满足，Top1/Top5 的百分比就没有解释力：
      · 记录数太少 → 一条记录就能让占比跳 10 个点；
      · 标的数 ≤5   → Top5 占比按定义恒为 100%，永远"超标"。
    """
    if stats["total"] < CONC_MIN_TOTAL:
        return False, f"样本量 {stats['total']} < {CONC_MIN_TOTAL}"
    if stats["n_symbols"] <= 5:
        return False, f"标的数 {stats['n_symbols']} ≤ 5（Top5 恒为 100%，无定义）"
    return True, ""


def popup_alert(level, msg):
    """桌面弹窗主动提醒（不依赖第三方库）。

    实现要点（2026-11-02 重写，修「Windows Script Host 未结束的字符串常量」）：
      旧写法把整段消息直接拼进 VBScript 的 MsgBox 字面量，而消息里含换行
      （"WARN\\n\\n正文…"）。VBScript 的字符串字面量**不允许跨行**，解析到
      第 1 个 CR 就报 800A0409（行1 字符13，正好是 "WARN" 后面那个回车），
      于是每次巡检都弹一个「脚本错误」框，而不是告警内容。

      现在的做法：消息正文写进独立的 .msg 文本文件（UTF-16，中文不乱码），
      VBS 只做固定动作 —— 打开同目录的 .msg、ReadAll、MsgBox。
      **消息内容不再进入任何代码字面量**，换行/引号/反斜杠/中文全都无所谓。
      再加 `//B`（batch 模式）：万一今后脚本本身仍有问题，直接静默失败，
      绝不再把「Windows Script Host」错误框糊到用户屏幕上。

    文件名带 pid+时间戳：并发两次巡检不会互相踩写（旧版固定文件名，
    存在「后一个进程正在写、前一个 wscript 正在读」的截断风险）。

    vbSystemModal(4096)+vbInformation(64)：置顶提示框，用户点掉才消失。
    弹窗失败不影响告警日志（铃是加装，小本子照写）。
    """
    try:
        text = (f"{level}\n\n{msg}\n\n"
                f"（巡检自动弹出；处理后回到查看器对话叫我排查即可）")
        tmp = tempfile.gettempdir()
        tag = f"{os.getpid()}_{int(time.time() * 1000)}"
        msg_name = f"wb_viewer_alert_{tag}.msg"
        vbs = os.path.join(tmp, f"wb_viewer_alert_{tag}.vbs")
        # newline="" 关掉 Python 的换行转换，保证写进去的就是我们写的
        with open(os.path.join(tmp, msg_name), "w",
                  encoding="utf-16", newline="") as f:
            f.write(text.replace("\r\n", "\n").replace("\n", "\r\n"))
        with open(vbs, "w", encoding="utf-16", newline="") as f:
            f.write(
                "Dim fso, f, s, d\r\n"
                "Set fso = CreateObject(\"Scripting.FileSystemObject\")\r\n"
                "d = fso.GetParentFolderName(WScript.ScriptFullName)\r\n"
                f"Set f = fso.OpenTextFile(fso.BuildPath(d, \"{msg_name}\"), 1, False, -1)\r\n"
                "s = f.ReadAll\r\n"
                "f.Close\r\n"
                "MsgBox s, 4096 + 64, \"股票论坛查看器告警\"\r\n"
            )
        # //B = 批处理模式，抑制一切提示与脚本错误对话框
        subprocess.Popen(["wscript.exe", "//B", vbs], close_fds=True)
        _cleanup_old_alert_files(tmp)
    except Exception:  # noqa: BLE001
        pass


def _cleanup_old_alert_files(tmp, keep_seconds=3600):
    """顺手清掉一小时前遗留的 vbs/msg（wscript 读完就没人要了）。"""
    try:
        now = time.time()
        for name in os.listdir(tmp):
            if not name.startswith("wb_viewer_alert_"):
                continue
            p = os.path.join(tmp, name)
            try:
                if now - os.path.getmtime(p) > keep_seconds:
                    os.remove(p)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass


def save_state(st):
    try:
        json.dump(st, open(STATE_F, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def main():
    now = datetime.datetime.now()
    # 给后端重启留 ~20s 余量，避免把"正在拉起"误判成"服务挂了"
    sc_code, sc = _get("/api/scheduler/status", tries=5, gap=4.0)
    rf_code, rf = _get("/api/refresh_status")
    st = load_state()
    fired = {}

    def maybe_alert(key, level, msg, dedupe_h=None):
        last = st.get(key)
        last_dt = _parse_dt(last) if isinstance(last, str) else None
        window_h = DEDUPE_HOURS if dedupe_h is None else dedupe_h
        if last_dt and (now - last_dt).total_seconds() < window_h * 3600:
            return False  # 去重：同条件在静默期内
        fired[key] = now.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{now.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}\n"
        try:
            with open(ALERT_LOG, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:  # noqa: BLE001
            pass
        popup_alert(level, msg)  # 桌面弹窗主动提醒（与日志同去重频率，不会刷屏）
        print(line.strip())
        return True

    if sc is None:
        maybe_alert("svc_down", "ERROR",
                    f"巡检失败：/api/scheduler/status 不可达 (code={sc_code})，查看器后端可能未运行")
        save_state({**st, **fired})
        return

    errors_today = int(sc.get("errors_today", 0))
    in_window = bool(sc.get("in_window"))
    sources = sc.get("sources")

    # 规则1：交易窗口内高错误率
    if in_window and errors_today >= ERR_THRESHOLD:
        maybe_alert("high_err", "ERROR",
                    f"交易窗口内实时抓取累计失败 {errors_today} 次(阈值{ERR_THRESHOLD})，"
                    f"论坛/行情管道疑似被外部源风控掐断；sources={sources}")

    # 规则2：交易窗口内数据陈旧
    # 2026-11-02 修正（两处结论性错误）：
    # ① 旧版只看 refresh_status 顶层的 last_ok_at，而那个字段需要**全部源成功**才推进。
    #    实测 6 个源里 5 个正常，只因 taoguba Cookie 过期，顶层时间就冻在 10-29，
    #    报出"数据已 97.2h 未成功更新" —— 结论是错的。改为按**最鲜的那个源**判断。
    # ② 更要紧：这套分源刷新是**手动触发**的（用户点「抓取并更新」），而自动调度器
    #    /api/scheduler/status 明确 sources_empty、skip_reason=sources_empty，即
    #    "没有任何自动任务在跑"。对一个没有自动任务的数据源，用"多久没更新"去报警，
    #    等于拿用户没点按钮当故障 —— 会每 2 小时弹一次，永远弹下去。
    #    因此陈旧判定只在**自动调度器真的被配置了**时才成立；否则只在摘要行如实说明。
    rf_ok = (rf_code == 200 and isinstance(rf, dict))
    last_ok = rf.get("last_ok_at") if rf_ok else None
    lok = _parse_dt(last_ok)
    srcs = (rf.get("sources") or []) if rf_ok else []
    sched_srcs = sources if isinstance(sources, list) else []
    auto_on = bool(sc.get("enabled")) and bool(sched_srcs)
    fresh_dt, fresh_name = None, ""
    for s in srcs:
        dt = _parse_dt(s.get("last_ok_at")) if isinstance(s, dict) else None
        if dt and (fresh_dt is None or dt > fresh_dt):
            fresh_dt, fresh_name = dt, str(s.get("source_key") or s.get("display_name") or "")
    eff_dt = fresh_dt or lok or _parse_dt(sc.get("last_run_at"))
    gap_h = ((now - eff_dt).total_seconds() / 3600.0) if eff_dt is not None else 0.0
    fully_stale = bool(in_window and auto_on and eff_dt is not None and gap_h > STALE_HOURS)
    if fully_stale:
        maybe_alert("stale", "CRITICAL",
                    f"交易窗口内自动管道已 {gap_h:.1f}h 没有成功更新"
                    f"(阈值{STALE_HOURS}h)；最鲜的一次来自 {fresh_name or '调度器'}"
                    f" @ {eff_dt.strftime('%Y-%m-%d %H:%M')}。疑似停摆。")
    # 已经在喊"全停了"，就别再补一句"某个源坏了" —— 两条同时弹等于双倍打扰。
    # 注意这里判的是**当前状态**而不是"本轮报没报"：否则 stale 被 6h 去重压住的那一轮，
    # source_error 会钻空子补弹一次，等于换个顺序继续骚扰。
    # 同时跟 stale 一样只在 auto_on 时才算：手动刷新失败在界面上本来就有红字，
    # 不需要再弹一个系统框（那不是"没人发现的故障"，是"你刚点的按钮报错了"）。
    if in_window and auto_on and srcs and not fully_stale:
        bad = [s for s in srcs if isinstance(s, dict) and s.get("status") == "error"]
        if bad:
            parts = []
            for s in bad[:3]:
                nm = s.get("source_key") or s.get("display_name") or "?"
                err = str(s.get("error") or "")[:60]
                parts.append(f"{nm}（{err}）" if err else nm)
            more = f" 等 {len(bad)} 个" if len(bad) > 3 else ""
            maybe_alert("source_error", "WARN",
                        "部分信源抓取失败，其余源仍在更新（数据不是全停，只是不全）："
                        + "；".join(parts) + more + "。多数是登录/Cookie 过期，"
                        "到查看器「抓取并更新」点「点此登录」重登即可。",
                        dedupe_h=24)

    # ===== 数据真实性层（不依赖交易窗口，照常跑）=====
    stats = get_trades_stats()
    ver_summary = "veracity=unavailable"
    if stats:
        top1, top5 = stats["top1"], stats["top5"]
        usable, why = conc_usable(stats)
        ver_summary = (f"top1={top1:.1f}% top5={top5:.1f}% n={stats['total']} "
                       f"k={stats['n_symbols']}")
        if not usable:
            # 样本不够就明说"这次不判"，而不是拿百分比硬套阈值造告警
            ver_summary += f"（集中度本轮不判：{why}）"
        else:
            # 规则3：集中度硬上限（无基线也能拦，直接命中旧数据 Top5=65.4%）
            if top1 > TOP1_CEIL or top5 > TOP5_CEIL:
                maybe_alert("conc_ceil", "CRITICAL",
                            f"标的集中度畸高（硬上限 Top1>{TOP1_CEIL}% / Top5>{TOP5_CEIL}%）："
                            f"当前 Top1={top1:.1f}% Top5={top5:.1f}%，total={stats['total']}，"
                            f"标的数={stats['n_symbols']}。"
                            f"健康论坛不应如此集中，疑似表陈旧/数据未刷新。")
            # 规则4：集中度漂移（相对滚动基线）
            base_top5 = st.get("ver_base_top5")
            if base_top5 is not None and top5 > base_top5 * DRIFT_MULT + DRIFT_PAD:
                maybe_alert("conc_drift", "WARN",
                            f"Top5 集中度相对基线突增：当前 {top5:.1f}% vs 基线 {base_top5:.1f}%"
                            f"（阈值 >{base_top5*DRIFT_MULT+DRIFT_PAD:.1f}%）。")
            elif top5 <= TOP5_CEIL:
                # 仅在健康时更新基线（EWMA），避免把异常值记进基线
                new_base = top5 if base_top5 is None else base_top5 * 0.7 + top5 * 0.3
                st["ver_base_top5"] = round(new_base, 2)
                st["ver_base_top1"] = round(
                    top1 if st.get("ver_base_top1") is None else st["ver_base_top1"] * 0.7 + top1 * 0.3, 2)
        # 规则5：数据冻结（表 MAX(post_time) 冻住，但抓取器自称近期成功 -> 表没跟上）
        mx = stats["max_dt"]
        if mx is not None:
            age_days = (now - mx).days
            scraper_recent = (lok is not None) and ((now - lok).days <= FREEZE_OK_DAYS)
            if age_days > FREEZE_DAYS and scraper_recent:
                maybe_alert("data_frozen", "CRITICAL",
                            f"数据冻结：trades 最新 post_time={mx.strftime('%Y-%m-%d %H:%M')} "
                            f"已冻 {age_days} 天，但抓取器最近 {FREEZE_OK_DAYS} 天内自称成功"
                            f"(last_ok_at={last_ok}) -> 显示的表未跟上抓取，疑似陈旧表/未刷新。")
        # 规则6：作者活跃塌缩
        if stats["all_authors"] >= 20 and stats["recent_authors"] < 5:
            maybe_alert("author_collapse", "WARN",
                        f"近30天独立作者仅 {stats['recent_authors']} 人（全量 {stats['all_authors']}），"
                        f"活跃度塌缩，疑似数据停留在旧时段。")

    if not fired:
        note = ""
        if not auto_on:
            note = (f" | 自动调度器未启用或无信源(skip_reason={sc.get('skip_reason')})"
                    f"→它不会自动抓，本轮不做陈旧判定")
        print(f"{now.strftime('%Y-%m-%d %H:%M:%S')} OK  窗口内={in_window} "
              f"errors_today={errors_today} last_ok_at={last_ok} | {ver_summary}{note}")

    save_state({**st, **fired})


if __name__ == "__main__":
    if "--test-popup" in sys.argv:
        # 手动验证弹窗本身：python viewer/check_fetcher_health.py --test-popup
        popup_alert("测试", "如果你看到这个框，说明告警弹窗已经正常了。\n"
                            "（真实告警只在交易时段发现问题时才弹，不会定时打扰）")
        print("已尝试弹出测试提示框；若 3 秒内没出现，说明本机 wscript 被禁用。")
    else:
        main()
