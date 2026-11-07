"""开机补跑（方案 A）——服务启动后，若「最新交易日」还没有批量辩论结果，就后台自动补一次。

为什么是「最新交易日」而不是「今天」：
    盘前 / 周末 / 节假日开机时，库里的最新交易日仍是上一个交易日。
    用它当判据，就天然得到三种正确行为：
      · 昨天的已经跑过 → 跳过（不会给你一份昨天数据的重复结论）；
      · 收盘后抓取落库、最新交易日推进到今天 → 补跑今天；
      · 今天还没抓完数据（最新交易日仍停在昨天）→ 不跑，等数据就绪。

四道闸门（任一不满足就不跑，宁可少跑也不乱烧钱）：
    1. 开关        DEBATE_AUTORUN=0 整体关闭
    2. 去重        该交易日已有 status='ok' 的 batch 记录 → 永不重跑
    3. 重试上限    同一交易日最多自动跑 DEBATE_AUTORUN_MAX_TRIES 次（默认 2），
                   防「开机 → 崩 → 再开机」无限循环烧 API 额度
    4. 数据就绪    该交易日指标行数需 >= DEBATE_AUTORUN_MIN_COVERAGE（默认 3000），
                   否则视为抓取未完成，退避 DEBATE_AUTORUN_RETRY_WAIT 秒后再判

另有一条时间闸门：target 就是今天且未到 15:10 时，等到 15:10 再跑（盘中数据是半成品）。

两种「没跑」的场景都能覆盖：
    · 电脑没开 → 开机启动时补跑（这是「方案 A」的本意）；
    · 电脑一直开着 → 常驻巡检每 TICK 秒判一次，收盘抓取落库后自动跑（不必重启）。

本模块只做「判定」，不直接调模型——真正的触发由 server 传入的 start_batch 完成，
这样可以复用它的单飞锁（同一 key 在跑就不重复拉起）。
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- 旋钮（env 可覆盖）

def _flag(name: str, default: str = "1") -> bool:
    return (os.environ.get(name) or default).strip().lower() not in (
        "0", "false", "no", "off", "")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except Exception:  # noqa: BLE001
        return default


ENABLED = _flag("DEBATE_AUTORUN", "1")
DELAY = _env_int("DEBATE_AUTORUN_DELAY", 180)          # 启动后等多久再判（秒）
MAX_TRIES = _env_int("DEBATE_AUTORUN_MAX_TRIES", 2)
MIN_COVERAGE = _env_int("DEBATE_AUTORUN_MIN_COVERAGE", 3000)
RETRY_WAIT = _env_int("DEBATE_AUTORUN_RETRY_WAIT", 1200)  # 数据未就绪时退避（秒）
TICK = _env_int("DEBATE_AUTORUN_TICK", 900)               # 常驻巡检间隔（秒）
CLOSE_GATE_MIN = 15 * 60 + 10                              # 15:10

_TRIES_KEY = "debate_autorun_tries:"          # + 交易日


# ---------------------------------------------------------------- 小工具

def _meta(conn, key: str, default: str = "") -> str:
    try:
        r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(r[0]) if r and r[0] is not None else default
    except Exception:  # noqa: BLE001
        return default


def _bump_tries(conn, date: str) -> int:
    """记一次「已自动触发」。写失败不影响主流程（只是少了重试上限保护）。"""
    key = _TRIES_KEY + date
    try:
        n = int(_meta(conn, key, "0") or 0) + 1
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(n)))
        conn.commit()
        return n
    except Exception:  # noqa: BLE001
        log.warning("[debate_autorun] 写重试计数失败（已忽略）", exc_info=True)
        return 0


def _latest_trade_date(conn) -> str:
    try:
        from viewer import ai_review as ar
        return str(ar.latest_trade_date(conn) or "")
    except Exception:  # noqa: BLE001
        return time.strftime("%Y-%m-%d")


def coverage(conn, date: str) -> int:
    """该交易日有多少只票的指标已入库 —— 用来判断抓取是否跑完。"""
    try:
        r = conn.execute(
            "SELECT COUNT(*) FROM stock_indicators_daily WHERE date=?", (date,)).fetchone()
        return int(r[0]) if r and r[0] is not None else 0
    except Exception:  # noqa: BLE001
        return 0


def _has_ok_batch(conn, date: str) -> bool:
    try:
        r = conn.execute(
            "SELECT 1 FROM ai_debate WHERE mode='batch' AND status='ok' "
            "AND review_date=? LIMIT 1", (date,)).fetchone()
        return bool(r)
    except Exception:  # noqa: BLE001
        return False


def _gate_deadline(now: datetime) -> datetime:
    return now.replace(hour=15, minute=10, second=0, microsecond=0)


def has_key() -> bool:
    """是否已配置大模型 Key —— 没配就绝不自动跑（否则每次开机白烧一次调用）。"""
    try:
        from viewer import ai_review as ar
        from viewer import debate as D
        return bool(ar.provider_key(D._resolve_provider("")))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 判定

def decide(conn, now: datetime | None = None) -> dict:
    """判断这一轮该不该补跑。

    返回 dict：
        run         是否应立即触发
        reason      判定理由（写日志 / 便于排查）
        target_date 目标交易日
        wait_sec    0=不必再等；>0=过这么多秒后重新判定
        tries       该交易日已自动触发次数
        coverage    目标日指标覆盖只数
    """
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    out = {"run": False, "reason": "", "target_date": "", "wait_sec": 0,
           "tries": 0, "coverage": 0}

    if not ENABLED:
        out["reason"] = "disabled"
        return out

    if not has_key():
        out["reason"] = "no_key"
        return out

    target = _latest_trade_date(conn)
    out["target_date"] = target
    if not target:
        out["reason"] = "no_trade_date"
        return out

    # 闸门 2：已有成功结果 → 绝不重跑
    if _has_ok_batch(conn, target):
        out["reason"] = "done:%s" % target
        return out

    # 闸门 3：同一交易日自动触发次数上限
    tries = 0
    try:
        tries = int(_meta(conn, _TRIES_KEY + target, "0") or 0)
    except Exception:  # noqa: BLE001
        tries = 0
    out["tries"] = tries
    if tries >= MAX_TRIES:
        out["reason"] = "give_up:%d 次已用完" % tries
        return out

    # 闸门 4：数据是否抓完
    cov = coverage(conn, target)
    out["coverage"] = cov
    if cov < MIN_COVERAGE:
        out["reason"] = "data_not_ready:%d/%d" % (cov, MIN_COVERAGE)
        out["wait_sec"] = max(60, RETRY_WAIT)
        return out

    # 时间闸门：今天的数据在今天 15:10 前是半成品
    if target == today:
        if now.hour * 60 + now.minute < CLOSE_GATE_MIN:
            dl = _gate_deadline(now)
            out["reason"] = "pre_close"
            out["wait_sec"] = max(60, int((dl - now).total_seconds()))
            return out

    out["run"] = True
    out["reason"] = "pending"
    return out


def drive(start_batch, conn, once: bool = False) -> None:
    """后台线程主循环：等延迟 → 判定 → 触发 / 等待 / 继续巡检。阻塞，调用方放 daemon 线程。

    为什么是「常驻巡检」而不是「启动判一次」：
        只判一次只能覆盖「关机 → 开机」这一种场景。若你白天一直开着电脑，
        收盘抓取落库后没有任何事件会重新判定，当天就永远不跑了。
        所以这里常驻：每 TICK 秒醒一次（默认 15 分钟，几乎零开销），
        判定条件全部满足就触发；跑成功后自然进入 done 分支不再重复。
    """
    if not ENABLED:
        log.info("[debate_autorun] 已关闭（DEBATE_AUTORUN=0），不补跑")
        return
    log.info("[debate_autorun] 启动，%ds 后开始判定，巡检间隔 %ds", DELAY, TICK)
    try:
        time.sleep(max(0, DELAY))
    except Exception:  # noqa: BLE001
        pass

    while True:
        try:
            d = decide(conn)
        except Exception:  # noqa: BLE001
            log.exception("[debate_autorun] 判定异常（忽略，下轮再判）")
            if once:
                return
            _sleep(TICK)
            continue

        log.info("[debate_autorun] 判定：run=%s reason=%s target=%s tries=%s cov=%s",
                 d["run"], d["reason"], d["target_date"], d["tries"], d["coverage"])

        if d["run"]:
            n = _bump_tries(conn, d["target_date"])
            try:
                r = start_batch()
                log.info("[debate_autorun] 已触发补跑 %s（第 %d 次）：%s",
                         d["target_date"], n, r)
            except Exception:  # noqa: BLE001
                log.exception("[debate_autorun] 触发失败（下轮再试，受重试上限约束）")
            if once:
                return
            # 等它跑完（约 2 分钟）再进下一轮判定，避免判定撞上正在跑的批次
            _sleep(max(TICK, 600))
            continue

        if d["wait_sec"] > 0:
            log.info("[debate_autorun] 等待 %ds 后重判（%s）", min(d["wait_sec"], TICK), d["reason"])
            if once:
                return
            _sleep(min(d["wait_sec"], TICK))
            continue

        # disabled / no_key / give_up 之类：不是永久终态（用户随时可能配 Key），
        # 睡一个巡检周期后继续看，别把线程退出后再也没人管。
        if once:
            return
        _sleep(TICK)


def _sleep(sec: int) -> None:
    try:
        time.sleep(max(1, int(sec)))
    except Exception:  # noqa: BLE001
        pass


def status(conn) -> dict:
    """给前端/排查看的一行摘要。"""
    try:
        d = decide(conn)
    except Exception as e:  # noqa: BLE001
        return {"enabled": ENABLED, "error": str(e)[:120]}
    d["enabled"] = ENABLED
    d["delay"] = DELAY
    d["max_tries"] = MAX_TRIES
    return d


if __name__ == "__main__":  # 手动查一次判定结果
    from viewer.db import Database
    print(status(Database().conn))
