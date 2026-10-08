# -*- coding: utf-8 -*-
"""真实行情夹具采集机（王总本机 · 有网 · 一次性运行）。

仅在王总本机（有网、可 pip install akshare/tushare）运行，从数据源抓取真实日线，
并用【独立于被测系统】的受信方法计算 Oracle 真值，冻结进 fixtures/*.json
（内嵌 is_real_data=true 与 sha256，供离线沙箱做正确性对照）。

用法示例：
  .venv/Scripts/python.exe fixtures/collect_real_quotes.py \
      --symbol 600000.SH --start 2023-01-01 --end 2023-12-31 --source akshare
  # 可重复 --symbol；或用 --symbols 600000.SH,000001.SZ,某退市.SH 一次采集多只

红线：本脚本输出的 JSON 必须是真实数据；sha256 在写出前计算并回填，load_fixtures 加载时复核。
依赖：akshare / tushare 仅在采集机安装，沙箱 / CI 运行时不需要（load_fixtures 不依赖它们）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_payload_text(data: dict) -> str:
    meta = dict(data.get("metadata", {}))
    meta.pop("sha256", None)
    stripped = dict(data)
    stripped["metadata"] = meta
    return json.dumps(stripped, ensure_ascii=False, indent=2, sort_keys=True)


# ───────────────────────── 数据源抽象（默认 akshare，免 token） ─────────────────────────
def fetch_bars(source: str, symbol: str, start: str, end: str):
    """从数据源抓取真实日线。仅在有网且装好对应库的本机可用；失败抛 RuntimeError。

    返回 (bars, adj)：bars = [{date,open,high,low,close,vol,amount}]，adj = 复权方式。
    """
    if source == "akshare":
        try:
            import akshare as ak  # type: ignore
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("未安装 akshare（请 pip install akshare）：%s" % e)
        code = symbol.split(".")[0]
        df = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=start.replace("-", ""), end_date=end.replace("-", ""),
            adjust="hfq")
        if df is None or len(df) == 0:
            raise RuntimeError("akshare 未返回 %s 的真实数据" % symbol)
        bars = []
        for _, row in df.iterrows():
            bars.append({
                "date": str(row.get("日期", "")),
                "open": float(row.get("开盘")),
                "high": float(row.get("最高")),
                "low": float(row.get("最低")),
                "close": float(row.get("收盘")),
                "vol": float(row.get("成交量")),
                "amount": float(row.get("成交额")),
            })
        return bars, "hfq"
    elif source == "tushare":
        try:
            import tushare as ts  # type: ignore
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("未安装 tushare（请 pip install tushare）：%s" % e)
        token_path = ROOT.parent / "viewer" / ".tushare_token"
        token = token_path.read_text(encoding="utf-8").strip() if token_path.exists() else ""
        if not token:
            raise RuntimeError("tushare 需要 token：请写入 viewer/.tushare_token")
        ts.set_token(token)
        # 真实调用位置（保留，王总本机配置好即可用）
        df = ts.pro_bar(ts_code=symbol, start_date=start.replace("-", ""),
                        end_date=end.replace("-", ""), adj="hfq", freq="D")
        if df is None or len(df) == 0:
            raise RuntimeError("tushare 未返回 %s 的真实数据" % symbol)
        bars = []
        for _, row in df.iterrows():
            bars.append({
                "date": str(row.get("trade_date", "")),
                "open": float(row.get("open")),
                "high": float(row.get("high")),
                "low": float(row.get("low")),
                "close": float(row.get("close")),
                "vol": float(row.get("vol")),
                "amount": float(row.get("amount")),
            })
        return bars, "hfq"
    else:
        raise RuntimeError("不支持的 source=%s（支持 akshare/tushare）" % source)


# ───────────────────────── 独立 Oracle 真值（受信方法，不依赖被测系统） ─────────────────────────
def compute_expected(bars: list) -> dict:
    """用纯标准库独立核算 Oracle 真值（与 screener_extra._metrics_from_nav 解耦）。"""
    if len(bars) < 2:
        return {"total_return": None, "ann_return": None, "sharpe": None,
                "max_drawdown": None, "daily_returns": []}
    closes = [float(b["close"]) for b in bars]
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    n_days = len(rets)
    total = closes[-1] / closes[0] - 1
    ann = (closes[-1] / closes[0]) ** (252 / n_days) - 1
    peak = closes[0]
    mdd = 0.0
    for v in closes:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > mdd:
                mdd = dd
    mean_r = sum(rets) / n_days
    var = sum((x - mean_r) ** 2 for x in rets) / n_days
    sd = var ** 0.5
    sharpe = (mean_r / sd) * (252 ** 0.5) if sd > 0 else None
    return {
        "total_return": round(total, 4),
        "ann_return": round(ann, 4) if ann is not None else None,
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "max_drawdown": round(-mdd, 4),   # 以负数表示亏损幅度，与闸门口径一致
        "daily_returns": [round(r, 6) for r in rets],
    }


def collect_one(source: str, symbol: str, start: str, end: str, instrument_type: str) -> Path:
    bars, adj = fetch_bars(source, symbol, start, end)
    expected = compute_expected(bars)
    payload = {
        "metadata": {
            "source": source,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "instrument_type": instrument_type,
            "adj": adj,
            "fields": ["date", "open", "high", "low", "close", "vol", "amount"],
            "trade_days": len(bars),
            "is_real_data": True,
            "sha256": "",   # 写出前回填
        },
        "bars": bars,
        "expected": expected,
    }
    payload["metadata"]["sha256"] = _sha256_text(_canonical_payload_text(payload))
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    out = ROOT / ("oracle_%s.json" % symbol)
    out.write_text(text, encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="真实行情夹具采集机")
    ap.add_argument("--symbol", action="append", default=[], help="标的，如 600000.SH；可多次")
    ap.add_argument("--symbols", default="", help="逗号分隔的标的列表")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2023-12-31")
    ap.add_argument("--source", default="akshare", choices=["akshare", "tushare"])
    ap.add_argument("--type", default="stock", choices=["stock", "delisted", "index"])
    args = ap.parse_args()
    symbols = list(args.symbol)
    if args.symbols:
        symbols += [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("请通过 --symbol 或 --symbols 指定至少一个标的")
        return 1
    for s in symbols:
        try:
            out = collect_one(args.source, s, args.start, args.end, args.type)
            print("已生成真实夹具:", out.name)
        except Exception as e:  # noqa: BLE001
            print("采集失败 %s: %s" % (s, e))
            return 1
    print("全部采集完成。请将 fixtures/oracle_*.json 提交入库（涉密则按 Q7 仅留 .gitkeep）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
