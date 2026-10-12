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
DB_F = os.path.join(HERE, "app.db")  # 只读校验 trades 真实性

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


def _get(path, timeout=10):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return 200, json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:  # noqa: BLE001
        return None, {"_err": str(e)[:160]}


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
            "max_dt": max_dt, "recent_authors": recent_authors,
            "all_authors": all_authors,
        }
    except Exception:  # noqa: BLE001
        return None


def popup_alert(level, msg):
    """桌面弹窗主动提醒（不依赖第三方库）。
    用 wscript 执行临时 .vbs（UTF-16 保证中文不乱码），异步启动不阻塞巡检；
    vbSystemModal(4096)+vbInformation(64)：置顶提示框，用户点掉才消失。
    弹窗失败不影响告警日志（铃是加装，小本子照写）。"""
    try:
        text = (f"{level}\n\n{msg}\n\n"
                f"（巡检自动弹出；处理后回到查看器对话叫我排查即可）")
        vbs = os.path.join(tempfile.gettempdir(), "wb_viewer_alert.vbs")
        lit = '"' + text.replace('"', '""') + '"'  # VBS 字符串转义
        with open(vbs, "w", encoding="utf-16") as f:
            f.write(f'MsgBox {lit}, 4096 + 64, "股票论坛查看器告警"\n')
        subprocess.Popen(["wscript.exe", vbs], close_fds=True)
    except Exception:  # noqa: BLE001
        pass


def save_state(st):
    try:
        json.dump(st, open(STATE_F, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def main():
    now = datetime.datetime.now()
    sc_code, sc = _get("/api/scheduler/status")
    rf_code, rf = _get("/api/refresh_status")
    st = load_state()
    fired = {}

    def maybe_alert(key, level, msg):
        last = st.get(key)
        last_dt = _parse_dt(last) if isinstance(last, str) else None
        if last_dt and (now - last_dt).total_seconds() < DEDUPE_HOURS * 3600:
            return  # 去重：同条件在静默期内
        fired[key] = now.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{now.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}\n"
        try:
            with open(ALERT_LOG, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:  # noqa: BLE001
            pass
        popup_alert(level, msg)  # 桌面弹窗主动提醒（与日志同去重频率，不会刷屏）
        print(line.strip())

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
    last_ok = rf.get("last_ok_at") if (rf_code == 200 and isinstance(rf, dict)) else None
    lok = _parse_dt(last_ok)
    if in_window and lok is not None:
        gap_h = (now - lok).total_seconds() / 3600.0
        if gap_h > STALE_HOURS:
            maybe_alert("stale", "WARN",
                        f"交易窗口内数据已 {gap_h:.1f}h 未成功更新(阈值{STALE_HOURS}h)；last_ok_at={last_ok}")

    # ===== 数据真实性层（不依赖交易窗口，照常跑）=====
    stats = get_trades_stats()
    ver_summary = "veracity=unavailable"
    if stats:
        top1, top5 = stats["top1"], stats["top5"]
        ver_summary = f"top1={top1:.1f}% top5={top5:.1f}% n={stats['total']}"
        # 规则3：集中度硬上限（无基线也能拦，直接命中旧数据 Top5=65.4%）
        if top1 > TOP1_CEIL or top5 > TOP5_CEIL:
            maybe_alert("conc_ceil", "CRITICAL",
                        f"标的集中度畸高（硬上限 Top1>{TOP1_CEIL}% / Top5>{TOP5_CEIL}%）："
                        f"当前 Top1={top1:.1f}% Top5={top5:.1f}%，total={stats['total']}。"
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
        print(f"{now.strftime('%Y-%m-%d %H:%M:%S')} OK  窗口内={in_window} "
              f"errors_today={errors_today} last_ok_at={last_ok} | {ver_summary}")

    save_state({**st, **fired})


if __name__ == "__main__":
    main()
