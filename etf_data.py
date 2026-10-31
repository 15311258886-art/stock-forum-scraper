# -*- coding: utf-8 -*-
"""ETF 数据接入（etf-data-v1.0，2026-10-17 进哥要求：今天打通数据链路，不让活儿干一半）。

职责：
  - ETF_UNIVERSE：主流 ETF 清单（宽基 / 行业主题 / 跨境 / 商品债券），6 位代码。
  - fetch_etf_klines(force, progress)：对每只 ETF 调 strategy_sim.fetch_kline 拉日 K，
    合并进 predict_kcache.json（与 A 股日 K 共存，不动既有数据），使 viewer/etf.py 的
    etf_universe() 能扫到 ETF 代码 → A 栏「量化多因子 ETF 候选」自动点亮打分。
  - 数据来源：新浪日线（strategy_sim.fetch_kline 同款），纯标准库，无需 akshare / 无需联网装包。

用法：
  python etf_data.py            # 增量拉取（今天已抓过且非盘中则跳过）
  python etf_data.py --force    # 强制全量重拉
"""
from __future__ import annotations

import os
import sys
import json
import time
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "viewer"))

# kcache 路径（与 predictor / strategy_sim 一致：ROOT/predict_kcache.json）
KCACHE = os.path.join(ROOT, "predict_kcache.json")
BARS = 480  # 约两年交易日，满足 etf_universe 的 MIN_BARS=130，够 MA120 / 年线

# 优先复用 strategy_sim.fetch_kline（不重抄因子/接口逻辑）；失败（如 import 副作用）则复刻新浪接口
try:
    from strategy_sim import fetch_kline  # noqa: E402
except Exception:  # noqa: BLE001
    import urllib.request  # noqa: E402
    _UA = "Mozilla/5.0"

    def fetch_kline(code, datalen, adj="hfq"):
        pre = "sh" if code[0] in "659" else ("bj" if code[0] in "48" else "sz")
        url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"CN_MarketData.getKLineData?symbol={pre}{code}&scale=240&ma=no&datalen={datalen}"
               + (f"&adj={adj}" if adj else ""))
        req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                                    "Referer": "https://finance.sina.com.cn/"})
        raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
        data = json.loads(raw)
        rows = []
        for x in data or []:
            try:
                rows.append({"d": str(x["day"])[:10], "o": float(x["open"]), "h": float(x["high"]),
                             "l": float(x["low"]), "c": float(x["close"]), "v": float(x.get("volume") or 0)})
            except (TypeError, ValueError, KeyError):
                continue
        return rows


# 主流 ETF 清单（6 位代码）：宽基 / 行业主题 / 跨境 / 商品债券
_ETF_RAW = [
    # 宽基
    "510300", "510330", "510310", "159919", "510050", "510100", "510500", "510510", "159922",
    "588000", "588080", "159915", "159901", "512100", "560010", "159845", "510880", "159905",
    "515800", "563000", "159949", "510760",
    # 行业 / 主题
    "512660", "512010", "515790", "516970", "512690", "515030", "512760", "159995", "515000",
    "512800", "512880", "516110", "159928", "512980", "515250", "516150", "512670", "515700",
    "512580", "159813", "515700", "512980",
    # 跨境
    "513050", "513100", "513500", "159920", "513180", "159941", "513090", "161725",
    # 商品 / 债券
    "518880", "159980", "511260", "511010", "511380", "159981", "518850",
]
ETF_UNIVERSE = list(dict.fromkeys(_ETF_RAW))  # 去重保序


def _load_kc() -> dict:
    if os.path.exists(KCACHE):
        try:
            with open(KCACHE, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and "bars" in d:
                return d
        except Exception:  # noqa: BLE001
            pass
    return {"updated": None, "bars": {}}


def _save_kc(d: dict) -> None:
    tmp = KCACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, KCACHE)


def fetch_etf_klines(force: bool = False, progress=None) -> dict:
    """拉取 ETF_UNIVERSE 的日 K，合并进 predict_kcache.json。返回统计 dict。"""
    kc = _load_kc()
    bars = kc.setdefault("bars", {})
    today = datetime.now().strftime("%Y-%m-%d")
    need = []
    for c in ETF_UNIVERSE:
        v = bars.get(c)
        fresh = v and str(v[-1].get("d", ""))[:10] >= today
        if force or not fresh:
            need.append(c)
    if not need:
        return {"ok": True, "skipped_all": True, "n": 0, "total": len(ETF_UNIVERSE)}

    done = [0]
    ok = [0]
    lock = threading.Lock()

    def one(code):
        try:
            v = fetch_kline(code, BARS)
        except Exception:  # noqa: BLE001
            v = []
        with lock:
            done[0] += 1
            if v:
                ok[0] += 1
                bars[code] = v
            if progress and done[0] % 10 == 0:
                progress(done[0], len(need))

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(one, need))

    # 只有成功率过半才更新缓存时间戳（对齐 predictor.fetch_bars 的保守策略）
    if ok[0] >= max(1, len(need) // 2):
        kc["updated"] = today
    _save_kc(kc)
    return {"ok": True, "n": ok[0], "need": len(need), "total": len(ETF_UNIVERSE), "updated": kc["updated"]}


if __name__ == "__main__":
    force = "--force" in sys.argv
    t0 = time.time()
    r = fetch_etf_klines(force=force)
    print("ETF 日 K 拉取完成:", r, "耗时 %.1fs" % (time.time() - t0), flush=True)
