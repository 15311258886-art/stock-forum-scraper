"""抽取验证：/api/person 的 hold_days 口径（person_first_build）是否补齐并与其它视图互不串味。

隔离策略（硬约束：绝不触碰真实 viewer/app.db）：
  * 用 sqlite3 backup 把 viewer/app.db 复制到项目下 `.iso_person/app.db`（WAL 安全，
    直接 copy 文件会丢掉还在 -wal 里的写入）；
  * VIEWER_DB 指向副本、VIEWER_PORT=8891 启动 `python -m viewer.server`；
  * 断言全部走 HTTP，跑完杀进程并删除 `.iso_person`，同时核对真实库的 size/mtime 未变。

用法：  python tools/verify_person_hold_days.py [名字 ...]     缺省验证 尾盘狙击
退出码：0=全部通过，1=有断言失败。
"""
import hashlib
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
PROD_DB = Path(os.environ.get("VIEWER_DB") or (ROOT / "viewer" / "app.db"))
ISO_DIR = ROOT / ".iso_person"
ISO_DB = ISO_DIR / "app.db"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
PORT = int(os.environ.get("VERIFY_PORT", "8891"))
BASE = f"http://127.0.0.1:{PORT}"
DEFAULT_NAMES = ["尾盘狙击"]
SYMBOL = "600519"

FAILS: list[str] = []


def ok(msg):
    print(f"  [OK] {msg}")


def bad(msg):
    FAILS.append(msg)
    print(f"  [FAIL] {msg}")


def check(cond, msg, detail=""):
    if cond:
        ok(msg)
    else:
        bad(f"{msg}{(' —— ' + detail) if detail else ''}")
    return cond


def db_fingerprint(path: Path):
    st = path.stat()
    h = hashlib.md5(path.read_bytes()).hexdigest()[:12]
    return (st.st_size, int(st.st_mtime), h)


def make_iso_copy():
    """WAL 安全的库复制：通过 sqlite3 backup API，而不是 copy 文件。"""
    if ISO_DIR.exists():
        shutil.rmtree(ISO_DIR)
    ISO_DIR.mkdir(parents=True)
    src = sqlite3.connect(f"file:{PROD_DB.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(ISO_DB))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    print(f"隔离副本已就位：{ISO_DB}（{ISO_DB.stat().st_size} 字节）")


def wait_port(port, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.5):
                return True
        except OSError:
            time.sleep(0.4)
    return False


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:  # noqa: S310 —— 仅本机 127.0.0.1
        import json
        return json.loads(r.read().decode("utf-8"))


def rows_of(d, *keys):
    out = []
    for k in keys:
        out.extend(x for x in (d.get(k) or []) if isinstance(x, dict))
    return out


def dump_rows(title, rows):
    print(f"  {title}：共 {len(rows)} 条")
    print("    {:<8} {:<7} {:<8} {:<12} {:>7}  {}".format("symbol", "action", "mode", "hold_since", "days", "备注"))
    for it in rows:
        print("    {:<8} {:<7} {:<8} {:<12} {:>7}  {}".format(
            str(it.get("symbol") or "-"), str(it.get("action") or "-"),
            str(it.get("hold_days_mode") or "MISSING"),
            str(it.get("hold_since") or "-")[:12],
            str(it.get("hold_days") if it.get("hold_days") is not None else "MISSING"),
            "SELL" if str(it.get("action") or "").upper() == "SELL" else "",
        ))


def verify_person(name):
    print(f"\n[1] /api/person?name={name} —— 本视图应为 person_first_build")
    d = api(f"/api/person?name={quote(name)}")
    check(d.get("hold_days_basis") == "person_first_build",
          "顶层 hold_days_basis == person_first_build",
          f"实际={d.get('hold_days_basis')!r}")
    top_days = d.get("hold_days")
    check(isinstance(top_days, int), "顶层 hold_days 是整数（可直接给前端取用）",
          f"实际={top_days!r}")
    print(f"    顶层 hold_days = {top_days}")

    rows = rows_of(d, "trades", "positions")
    check(len(rows) > 0, "trades/positions 记录数 > 0", f"实际={len(rows)}")
    dump_rows("逐条明细", rows)

    modes = {(it.get("hold_days_mode")) for it in rows}
    check(bool(modes) and modes.issubset({"open", "closed"}),
          "逐条 hold_days_mode 非空且 ⊆ {open, closed}", f"实际取值集合={modes!r}")

    mode_wrong, days_missing, unanchored = [], [], []
    for it in rows:
        is_sell = str(it.get("action") or "").upper() == "SELL"
        want = "closed" if is_sell else "open"
        sym, got, since = it.get("symbol"), it.get("hold_days_mode"), (it.get("hold_since") or "").strip()
        if got != want:
            mode_wrong.append((sym, it.get("action"), want, got))
        elif it.get("hold_days") is None and since:
            days_missing.append((sym, it.get("action"), since))          # 有起点却算不出天数 = 真 bug
        elif it.get("hold_days") is None and not since:
            unanchored.append((sym, it.get("action")))                   # 数据缺口：此人无该股建仓记录
    check(not mode_wrong, "每条 mode 与判别式 (action==SELL?closed:open) 一致",
          f"{len(mode_wrong)} 条不一致：{mode_wrong[:5]}")
    check(not days_missing, "有 hold_since 的记录都算出了 hold_days",
          f"{len(days_missing)} 条缺值：{days_missing[:5]}")
    if unanchored:
        print(f"    [说明] {len(unanchored)} 条此人在该标的上无任何建仓记录，起点未知故 hold_days=None："
              f"{sorted(set(unanchored))}")
    print(f"    mode 分布：open={sum(1 for r in rows if r.get('hold_days_mode') == 'open')}，"
          f"closed={sum(1 for r in rows if r.get('hold_days_mode') == 'closed')}")
    return d


def verify_player(name, person_d):
    print(f"\n[2] /api/player?name={name} —— 本视图应为 player_first_build")
    d = api(f"/api/player?name={quote(name)}")
    if d.get("error"):
        bad(f"/api/player 返回错误：{d.get('error')}")
        return d
    check(d.get("hold_days_basis") == "player_first_build",
          "顶层 hold_days_basis == player_first_build", f"实际={d.get('hold_days_basis')!r}")

    # 交叉核对：同一人的 person / player 两个视图，同 symbol 同买卖状态下的 hold_days 必须一致
    # （含 None —— 起点未知的「数据缺口」也必须在两处表现一致，不能一处给数字一处给空）
    def bucket(rows):
        res = {}
        for it in rows:
            sym = it.get("symbol")
            if not sym:
                continue
            mode = it.get("hold_days_mode")
            res.setdefault((sym, mode), set()).add(it.get("hold_days"))
        return res

    p_rows = rows_of(d, "holdings", "sells", "trades", "positions")
    a, b = bucket(rows_of(person_d, "trades", "positions")), bucket(p_rows)
    common = set(a) & set(b)
    diff = {k: (sorted(a[k], key=str), sorted(b[k], key=str)) for k in common if a[k] != b[k]}
    check(bool(common), "两视图存在可比对的重合记录", f"person键={len(a)} player键={len(b)}")
    check(not diff, "person / player 两视图同一 (symbol, mode) 的 hold_days 完全一致（同为『本人首次建仓』口径）",
          f"{len(diff)} 处不一致：{list(diff.items())[:4]}")
    print(f"    比对键 {len(common)} 个；示例：{[(k, sorted(a[k]), sorted(b[k])) for k in list(common)[:3]]}")
    return d


def verify_stock(player_d):
    print(f"\n[3] /api/stock?symbol={SYMBOL} —— 本视图应为 symbol_first_build（全市场最早建仓）")
    d = api(f"/api/stock?symbol={SYMBOL}")
    check(d.get("hold_days_basis") == "symbol_first_build",
          "顶层 hold_days_basis == symbol_first_build", f"实际={d.get('hold_days_basis')!r}")
    stock_days = d.get("hold_days")
    check(isinstance(stock_days, int) and all(r.get("hold_days") == stock_days for r in rows_of(d, "trades", "positions")),
          "个股视图所有记录共用同一个 hold_days（全市场起点）",
          f"stock_days={stock_days}")

    # 反向证明「本人」起点没有被全市场起点污染：同一只票、不同人，起点必须不同。
    holders = sorted({p.get("player") for p in (d.get("positions") or [])
                      if p.get("player") and str(p.get("action") or "").upper() != "SELL"})[:4]
    seen = {}
    for who in holders:
        try:
            pd = api(f"/api/person?name={quote(who)}")
        except Exception as e:  # noqa: BLE001
            print(f"    跳过 {who}：{type(e).__name__}: {e}")
            continue
        if pd.get("hold_days_basis") != "person_first_build":
            bad(f"{who} 的 /api/person 基准不是 person_first_build：{pd.get('hold_days_basis')!r}")
        row = next((r for r in rows_of(pd, "trades", "positions") if r.get("symbol") == SYMBOL), None)
        if row:
            seen[who] = (row.get("hold_since"), row.get("hold_days"), row.get("hold_days_mode"))
    print(f"    {SYMBOL} 各人「本人起点」样本：")
    for who, v in seen.items():
        print(f"      {who:<10} since={str(v[0])[:16]:<16} days={v[1]} mode={v[2]}")
    check(len(seen) >= 2, f"{SYMBOL} 至少取到 2 位持有者用于比对", f"实际={len(seen)}")
    check(len({str(v[0]) for v in seen.values()}) > 1,
          f"{SYMBOL} 上不同人的建仓起点各不相同 ⇒ person 视图起点确为『本人』，未被全市场起点污染",
          f"全部相同：{seen}")
    return d


def main():
    names = sys.argv[1:] or DEFAULT_NAMES
    if not PROD_DB.exists() or PROD_DB.stat().st_size == 0:
        print(f"找不到真实库：{PROD_DB}")
        return 1
    before = db_fingerprint(PROD_DB)
    print(f"真实库基线：size={before[0]} mtime={before[1]} md5={before[2]}    ({PROD_DB})")

    make_iso_copy()
    env = dict(os.environ, VIEWER_DB=str(ISO_DB), VIEWER_PORT=str(PORT), PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([str(PYTHON), "-m", "viewer.server"], cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    try:
        if not wait_port(PORT):
            out = ""
            try:
                proc.kill()
                out = proc.stdout.read()[-2000:]
            except Exception:
                pass
            print(f"服务未能在 60s 内监听 {PORT}\n--- 服务输出 ---\n{out}")
            return 1
        print(f"隔离实例已启动：VIEWER_DB={ISO_DB} VIEWER_PORT={PORT}")
        try:
            for name in names:
                person_d = verify_person(name)
                player_d = verify_player(name, person_d)
                verify_stock(player_d)
        except Exception as e:  # noqa: BLE001
            bad(f"请求/解析异常：{type(e).__name__}: {e}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        print(f"\n隔离实例已停止（pid={proc.pid}）")

    after = db_fingerprint(PROD_DB)
    print("\n[4] 真实库是否全程未被动")
    check(after == before, "viewer/app.db 的 size/mtime/md5 与运行前完全一致",
          f"前={before} 后={after}")

    shutil.rmtree(ISO_DIR, ignore_errors=True)
    print(f"临时隔离目录已删除：{ISO_DIR} exists={ISO_DIR.exists()}")

    print("\n" + "=" * 60)
    if FAILS:
        print(f"结果：FAIL（{len(FAILS)} 项）")
        for m in FAILS:
            print("  - " + m)
        return 1
    print("结果：PASS —— 全部断言通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
