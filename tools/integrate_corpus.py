# -*- coding: utf-8 -*-
"""
豆包博主语料 · 整合层（2026-10-20 进哥需求：分域整合、产出高维价值）
---------------------------------------------------------------------------
本脚本对 douyin_corpus_* 做"升华"处理，产出三件事（均为幂等、可重跑）：

  1) 技术域(douyin_corpus_video.domain='tech') → 归并到 9 条"学习方法论轨道" topic
     原 category 太碎(16 类 + 943 条"其他")，归并后让 2701 条变成可系统学的结构。
  2) 情绪域(domain='sentiment') → 给每条观点打"观点类型" stance
     (看多 / 看空 / 中性研判 / 风险提示 / 板块机会)，情绪域内也分类清晰、不与技术混。
  3) 从 title+summary 抽 A股板块/题材关键词 → douyin_corpus_plate(video_id, plate, domain)
     供"板块多维联动"视图使用（点一个板块看 情绪观点+技术战法+荐股）。

博主双域画像（技术 tags + 情绪 tags 合并）不物理落表，由后端 API 实时聚合。

用法：
  python tools/integrate_corpus.py              # 全量重算
  python tools/integrate_corpus.py --domain tech
  python tools/integrate_corpus.py --dry-run    # 只打印统计不写库
"""
import sqlite3, os, re, argparse, json

DB = os.path.join(os.path.dirname(__file__), "..", "viewer", "app.db")

# ---------------------------------------------------------------------------
# 1) 技术域 → 高维主题轨道
# ---------------------------------------------------------------------------
# category 优先映射（除"其他/None"）；"其他/None" 与兜底用关键词。
CAT_TO_TRACK = {
    "交易系统": "趋势与龙头",
    "交易理念": "交易认知",
    "复盘/大盘": "交易认知",
    "技术指标/口诀": "均线指标",
    "量化策略": "量化与程序化",
    "K线形态": "K线与形态",
    "K线形态/口诀": "K线与形态",
    "仓位": "仓位与风控",
    "量价": "量价与盘口",
    "盘口": "量价与盘口",
    "估值": "估值与基本面",
    "均线": "均线指标",
    "MACD": "均线指标",
    "缠论": "缠论",
}
# 关键词 → 轨道（用于"其他/None"重分，也作为 category 映射的二级确认）
KW_TRACK = [
    ("缠论", ["缠论", "中枢", "背驰", "笔", "线段", "分型", "走势终完美"]),
    ("量化与程序化", ["量化", "QMT", "PTrade", "程序化", "算法交易", "因子", "回测", "网格", "套利"]),
    ("仓位与风控", ["仓位", "风控", "止损", "资金管理", "回撤", "纪律", "仓位管理", "分仓", "止盈"]),
    ("估值与基本面", ["估值", "基本面", "财报", "市盈率", "PE", "业绩", "价值投资", "护城河", "现金流", "ROE", "分红"]),
    ("均线指标", ["均线", "MACD", "EMA", "BOLL", "布林", "指标", "金叉", "死叉", "多空线"]),
    ("K线与形态", ["K线", "涨停", "十字星", "出水芙蓉", "形态", "吞没", "上影线", "下影线", "缺口", "平台突破"]),
    ("量价与盘口", ["量价", "盘口", "分时", "做T", "支撑位", "压力位", "换手", "量能", "集合竞价", "内外盘", "挂单"]),
    ("趋势与龙头", ["趋势", "龙头", "主升", "波段", "赛道", "机构抱团", "趋势龙", "连板", "一进二", "打板", "低吸", "首板"]),
    ("交易认知", ["认知", "心法", "心态", "人性", "散户", "机构思维", "逻辑买卖", "交易系统", "体系", "思维", "格局"]),
]

def track_of(category, text):
    cat = (category or "").strip()
    if cat in CAT_TO_TRACK:
        return CAT_TO_TRACK[cat]
    t = (text or "")
    for track, kws in KW_TRACK:
        for kw in kws:
            if kw in t:
                return track
    return "综合与其他"

# ---------------------------------------------------------------------------
# 2) 情绪域 → 观点类型 stance
# ---------------------------------------------------------------------------
RISK_KW = ["风险", "警惕", "避雷", "割肉", "套牢", "爆雷", "承压", "见顶", "崩", "踩雷", "退潮", "杀跌", "利空出尽"]
BULL_KW = ["反弹", "看多", "机会", "支撑", "向好", "上升", "上涨", "突破", "牛市", "利好", "复苏", "走强", "上攻", "回暖"]
BEAR_KW = ["下跌", "看空", "调整", "回落", "下行", "谨慎", "熊市", "利空", "破位", "走弱", "缩量", "探底"]
PLATE_KW = ["板块", "主线", "方向", "赛道", "题材", "关注", "看好", "轮动"]

def stance_of(category, text):
    cat = (category or "").strip()
    t = (text or "")
    # 风险优先
    if any(k in t for k in RISK_KW):
        return "风险提示"
    if cat == "板块轮动" or any(k in t for k in PLATE_KW):
        return "板块机会"
    b = sum(1 for k in BULL_KW if k in t)
    r = sum(1 for k in BEAR_KW if k in t)
    if b > r and b > 0:
        return "看多"
    if r > b and r > 0:
        return "看空"
    return "中性研判"

# ---------------------------------------------------------------------------
# 3) 板块/题材关键词抽取
# ---------------------------------------------------------------------------
PLATE_DICT = [
    "半导体","芯片","集成电路","光刻","AI","人工智能","算力","CPO","光模块","液冷","机器人",
    "减速器","新能源汽车","锂电","锂电池","光伏","储能","风电","氢能","军工","航天","卫星","北斗",
    "创新药","CXO","中药","医美","白酒","食品饮料","家电","消费","零售","免税","旅游","酒店",
    "券商","银行","保险","地产","物业","建材","水泥","工程机械","重卡","钢铁","煤炭","有色","黄金",
    "铜","铝","化工","化肥","农药","石油","天然气","电力","核电","水电","电网","特高压","农业",
    "养殖","猪肉","种业","传媒","游戏","影视","教育","通信","5G","6G","软件","信创","云计算",
    "大数据","物联网","汽车","零部件","轮胎","稀土","面板","PCB","消费电子","华为","特斯拉",
    "比亚迪","宁德时代","贵州茅台","东方财富","招商银行","中信证券",
]
PLATE_RE = re.compile("|".join(re.escape(p) for p in PLATE_DICT))

def plates_of(text):
    if not text:
        return []
    found = set()
    for m in PLATE_RE.finditer(text):
        found.add(m.group(0))
    return list(found)

# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", default=None, help="tech / sentiment / 不填=全部")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    db = os.path.abspath(DB)
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    cur = con.cursor()
    domains = [a.domain] if a.domain else ["tech", "sentiment"]
    stats = {"topic": {}, "stance": {}, "plate_pairs": 0, "videos": 0}
    # 先全部读进内存，dry-run 完全不碰库
    mem = {}
    for dom in domains:
        mem[dom] = cur.execute(
            "SELECT video_id, category, title, summary FROM douyin_corpus_video WHERE domain=?",
            (dom,)).fetchall()
    con.close()

    if a.dry_run:
        for dom in domains:
            for vid, cat, title, summary in mem[dom]:
                text = f"{title or ''} {summary or ''}"
                if dom == "tech":
                    v = track_of(cat, text); stats["topic"][v] = stats["topic"].get(v, 0) + 1
                else:
                    v = stance_of(cat, text); stats["stance"][v] = stats["stance"].get(v, 0) + 1
                stats["plate_pairs"] += len(plates_of(text)); stats["videos"] += 1
        _emit(stats, [])
        print("DRY-RUN DONE (未写库)")
        return

    con = sqlite3.connect(db); con.execute("PRAGMA journal_mode=WAL"); cur = con.cursor()
    for col in ("topic", "stance"):
        try:
            cur.execute(f"ALTER TABLE douyin_corpus_video ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass
    cur.execute("""CREATE TABLE IF NOT EXISTS douyin_corpus_plate (
        video_id TEXT, plate TEXT, domain TEXT, PRIMARY KEY (video_id, plate))""")
    for dom in domains:
        cur.execute("DELETE FROM douyin_corpus_plate WHERE domain=?", (dom,))
        for vid, cat, title, summary in mem[dom]:
            text = f"{title or ''} {summary or ''}"
            if dom == "tech":
                v = track_of(cat, text)
                cur.execute("UPDATE douyin_corpus_video SET topic=? WHERE video_id=?", (v, vid))
                stats["topic"][v] = stats["topic"].get(v, 0) + 1
            else:
                v = stance_of(cat, text)
                cur.execute("UPDATE douyin_corpus_video SET stance=? WHERE video_id=?", (v, vid))
                stats["stance"][v] = stats["stance"].get(v, 0) + 1
            for pl in plates_of(text):
                cur.execute("INSERT OR IGNORE INTO douyin_corpus_plate(video_id, plate, domain) VALUES(?,?,?)",
                            (vid, pl, dom))
                stats["plate_pairs"] += 1
            stats["videos"] += 1
    plate_cnt = cur.execute(
        "SELECT plate, count(*) FROM douyin_corpus_plate GROUP BY plate ORDER BY 2 DESC LIMIT 25").fetchall()
    con.commit(); con.close()
    _emit(stats, plate_cnt)
    print("DONE")

def _emit(stats, plate_cnt):
    print(f"\n=== 处理视频数: {stats['videos']} ===")
    print("\n--- 技术域 topic 轨道分布 ---")
    for k, v in sorted(stats["topic"].items(), key=lambda x: -x[1]):
        print(f"  {k:14s} {v}")
    print("\n--- 情绪域 stance 观点类型分布 ---")
    for k, v in sorted(stats["stance"].items(), key=lambda x: -x[1]):
        print(f"  {k:14s} {v}")
    print(f"\n--- 板块关联总条数: {stats['plate_pairs']} ---")
    if plate_cnt:
        print("  Top 板块:", ", ".join(f"{p}({n})" for p, n in plate_cnt))

if __name__ == "__main__":
    main()
