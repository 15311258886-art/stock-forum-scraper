"""临时端到端自测：构造假 news_row，跑 build_hotspots（用假 db），检查三个新字段。

不触碰正式 viewer/app.db：这里用一个只实现 clear/save 两个方法的假对象。
运行：python tools/_qa_sector_detail.py   （结果写入 tools/_qa_detail_out.txt）
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from overseas import hotspots  # noqa: E402
from overseas.lexicon import SECTOR_SYMBOLS  # noqa: E402


class FakeDB:
    def __init__(self):
        self.saved = []

    def clear_overseas_hotspots(self):
        self.saved = []

    def save_overseas_hotspot(self, **kw):
        self.saved.append(kw)


def row(title, summary, topic, region="US", sentiment="看多", heat=5.0, source="Reuters", topics=""):
    return {
        "title": title, "summary": summary, "topic": topic, "topics": topics,
        "region": region, "sentiment": sentiment, "heat": heat, "source_name": source,
        "entities": json.dumps([{"market": "US", "symbol": "NVDA", "name": "英伟达"}], ensure_ascii=False),
        "publish_time": "2026-09-21 09:00:00", "url": "https://example.com/a",
    }


rows = [
    # 地缘冲突（分化方向，up/down 都非空）
    row("Middle East conflict escalates, missile strike hits oil facilities",
        "War risk lifts Brent crude; shipping lanes blocked", "地缘冲突与战争", heat=9.5),
    row("Strait of Hormuz blockade fears grow amid geopolitical tension",
        "Oil tanker freight rates jump", "地缘冲突与战争", heat=8.0),
    # AI 资本开支（利多，但情绪转弱 → 触发「利多但国外舆情转弱」分支）
    row("Nvidia AI capex guidance beats, data center demand surge",
        "Hyperscaler orders for 800G optical modules rise", "科技与AI",
        sentiment="看空", heat=9.0, source="Bloomberg"),
    row("Nvidia 算力 chip demand strong, AI data center expansion",
        "Server and liquid cooling orders up", "科技与AI", sentiment="看空", heat=7.5),
    # 粮食歉收（分化）
    row("Wheat shortage deepens as drought hits harvest, export ban on rice",
        "Corn shortage pushes grain prices higher, famine risk", "粮食歉收与减产", heat=6.5),
    row("Crop failure: poor harvest triggers food security alarm",
        "Global grain supply tight", "粮食歉收与减产", heat=5.5),
    # 半导体出口管制（分化）
    row("US export control widens, chip ban targets semiconductor restriction",
        "Entity list adds more Chinese firms", "国际形势", heat=8.5),
    row("China chip ban update: entity list expanded",
        "Semiconductor restriction tightens", "国际形势", heat=7.0),
    # 只有 up/down 都为空才回落到「方向待观察」，这里再造一条主题兜底卡
    row("Port congestion and strike disrupt overseas supply chain",
        "Shipping schedule delays", "供应链与制造", heat=6.0),
    row("Overseas supply chain turmoil: port congestion worsens",
        "Delivery cycle lengthens", "供应链与制造", heat=5.2),
]

db = FakeDB()
hs = hotspots.build_hotspots(db, rows)

lines = []
lines.append(f"SECTOR_SYMBOLS 领域数={len(SECTOR_SYMBOLS)}，公司条目总数="
             f"{sum(len(v) for v in SECTOR_SYMBOLS.values())}")
lines.append(f"生成热点数={len(hs)}，落库条数={len(db.saved)}"
             f"（两者应一致：{len(hs) == len(db.saved)}）")
lines.append("")

problems = []
for h in hs:
    up = json.loads(h["sectors_up_detail"])
    down = json.loads(h["sectors_down_detail"])
    dec = h["decision"]
    lines.append(f"### {h['key']}  dir={h['direction']}  conf={h['confidence']} amp={h['amplification_factor']}")
    lines.append(f"  上涨明细 {len(up)} 领域：" + "; ".join(
        f"{d['sector']}({len(d['symbols'])}家:" + "/".join(s['name'] for s in d['symbols'][:2]) + ")"
        for d in up))
    lines.append(f"  下跌明细 {len(down)} 领域：" + "; ".join(
        f"{d['sector']}({len(d['symbols'])}家:" + "/".join(s['name'] for s in d['symbols'][:2]) + ")"
        for d in down))
    lines.append(f"  decision: {dec}")
    lines.append("")

    # ---- 断言 ----
    if not dec.strip():
        problems.append(f"{h['key']}: decision 为空")
    for d in up + down:
        if set(d.keys()) != {"sector", "symbols"}:
            problems.append(f"{h['key']}: 明细键不对 {list(d.keys())}")
        for s in d["symbols"]:
            if set(s.keys()) != {"code", "name"}:
                problems.append(f"{h['key']}: 公司键不对 {list(s.keys())}")
            if len(s["code"]) != 6 or not s["code"].isdigit():
                problems.append(f"{h['key']}: 代码格式 {s}")
    names = [d["sector"] for d in up + down]
    if names and not any(n in dec for n in names):
        problems.append(f"{h['key']}: decision 未点名任何领域 -> {dec}")
    if "确定性" not in dec and "方向待观察" not in dec:
        problems.append(f"{h['key']}: decision 缺 confidence -> {dec}")
    if ("放大" not in dec) and ("方向待观察" not in dec):
        problems.append(f"{h['key']}: decision 缺放大系数 -> {dec}")
    if "证据" in dec:
        problems.append(f"{h['key']}: decision 出现『证据』字样")

# 待观察回落分支：直接单测
fallback = hotspots._decision("中性（无映射）", None, None, 0.5, 1.0, "传导偏弱")
lines.append(f"[单测] 空领域回落 -> {fallback}")
if not fallback.startswith("方向待观察"):
    problems.append("空领域未回落到「方向待观察」")

lines.append("")
# ---- 真实 SQL 校验：用临时库跑一次真实落库 + 读回三个新列 ----
import tempfile  # noqa: E402
from viewer.db import Database  # noqa: E402

tmp = Path(tempfile.gettempdir()) / "_qa_sector_detail_tmp.db"
if tmp.exists():
    tmp.unlink()
real_db = Database(path=tmp)
real_db.clear_overseas_hotspots()
if hs:
    real_db.save_overseas_hotspot(**hs[0])
    got = real_db.conn.execute(
        "SELECT sectors_up_detail, sectors_down_detail, decision FROM overseas_hotspots LIMIT 1"
    ).fetchone()
    cols = [r["name"] for r in real_db.conn.execute("PRAGMA table_info(overseas_hotspots)").fetchall()]
    need = {"sectors_up_detail", "sectors_down_detail", "decision"}
    lines.append("")
    lines.append("== 临时库真实落库校验 ==")
    lines.append(f"新列存在于表结构：{need.issubset(set(cols))}  列={sorted(need)}")
    ok_json = bool(json.loads(got["sectors_up_detail"])) and bool(json.loads(got["sectors_down_detail"]))
    lines.append(f"写回并 json.loads 成功：{ok_json}")
    lines.append(f"写回 decision：{got['decision'][:60]}...")
    if not need.issubset(set(cols)) or not ok_json or not got["decision"]:
        problems.append("临时库落库校验失败")

lines.append("")
lines.append("== 结论 ==")
lines.append("全部通过 ✅" if not problems else "\n".join(problems))

Path(ROOT / "tools" / "_qa_detail_out.txt").write_text("\n".join(lines), encoding="utf-8")
print("done", len(hs), "problems:", len(problems))
