# -*- coding: utf-8 -*-
"""把 research/out/announcement_event_study.md 的表格转成前端可直接读的静态数据。

为什么走静态文件而不是新开 API 路由：
  · 这份结果是**离线研究产物**，只在重跑 announcement_event_study.py 时变化，
    没有实时性要求，不需要进 _TTL_ROUTES / 不占请求线程；
  · 前端静态文件按请求从磁盘读，改完刷新页面即生效，**不需要重启查看器服务**
    （lhb_weather 那次就因为后端路由没注册、要先重启，前端只能给一句"接口尚未生效"的降级提示）。
  · 数据只有 24 类 × 3 持有期 = 72 行，体积几 KB，比一次 HTTP 往返更便宜。

用法：
    .venv/Scripts/python.exe research/gen_ann_event_js.py
输出：
    viewer/static/ann_event_data.js   （window.ANN_EV = {...}）
"""
import io
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MD = os.path.join(BASE, "research", "out", "announcement_event_study.md")
OUT = os.path.join(BASE, "viewer", "static", "ann_event_data.js")

# 类别 → 我自己的语义分组（**不是**模型的 tone 字段：那批 md 里 tone 全是占位 "-"）。
# 分组只用于前端左右对照展示，统计口径与分组无关。
TONE = {
    "股东减持": "bad", "监管处罚": "bad", "资产/股权冻结": "bad",
    "诉讼仲裁": "bad", "立案调查": "bad", "人事变动": "bad",
    "质押担保": "bad", "减值计提": "bad", "退市风险": "bad",
    "业绩承压": "bad", "风险提示": "bad",
    "回购增持": "good", "股权激励": "good", "股东回报": "good",
    "并购重组": "good", "业绩向好": "good", "订单产能": "good",
    "产品/资质突破": "good", "题材催化": "good",
    "其他事务": "neutral",
}

ROW_RE = re.compile(
    r"^\|\s*(?P<cat>[^|]+?)\s*\|\s*(?P<tone>[^|]*?)\s*\|\s*(?P<n>\d+)\s*\|\s*(?P<days>\d+)\s*\|"
    r"\s*T\+(?P<h>\d+)\s*\|\s*(?P<ex>[+-]?\d+\.\d+)%\s*\|\s*(?P<t>[+-]?\d+\.\d+)\s*\|"
    r"\s*\[\s*(?P<lo>[+-]?\d+\.\d+)%,\s*(?P<hi>[+-]?\d+\.\d+)%\s*\]\s*\|\s*(?P<win>\d+\.\d+)%\s*\|\s*(?P<judge>[^|]+?)\s*\|"
)


def main() -> int:
    if not os.path.exists(MD):
        print("找不到 %s，先跑 research/announcement_event_study.py" % MD)
        return 2
    raw = io.open(MD, encoding="utf-8").read()

    rows = []
    for line in raw.splitlines():
        m = ROW_RE.match(line.strip())
        if not m:
            continue
        d = m.groupdict()
        cat = d["cat"].strip()
        rows.append({
            "cat": cat,
            "tone": TONE.get(cat, "neutral"),
            "h": int(d["h"]),
            "n": int(d["n"]),
            "days": int(d["days"]),
            "ex": float(d["ex"]),
            "t": float(d["t"]),
            "lo": float(d["lo"]),
            "hi": float(d["hi"]),
            "win": float(d["win"]),
            # 显著性：|t| ≥ 3.0 是预注册判据（45 次检验的 Bonferroni 宽松下限）
            "sig": abs(float(d["t"])) >= 3.0,
            "judge": d["judge"].strip(),
        })
    if not rows:
        print("没解析到任何数据行，检查 md 表格格式")
        return 3

    cats = []
    for r in rows:
        if r["cat"] not in cats:
            cats.append(r["cat"])
    missing = [c for c in cats if c not in TONE]
    if missing:
        print("⚠ 以下类别没配语义分组，按 neutral 处理：%s" % "、".join(missing))

    n_sig = len({(r["cat"], r["h"]) for r in rows if r["sig"]})
    # 严格 Bonferroni 阈值：双侧 z，按实跑检验数算（不要写死 8 条 —— 那是上一版 45 次检验的遗留）。
    # 用 3.34 作近似（α=0.05/60）；落在 [3.00, 3.34) 的条目单列出来，提醒"别当硬结论"。
    STRICT_T = 3.34
    borderline = sorted(
        "%s T+%d (t=%+.2f)" % (r["cat"], r["h"], r["t"])
        for r in rows if 3.0 <= abs(r["t"]) < STRICT_T
    )
    payload = {
        "meta": {
            "generated": "2026-10-30",
            "source": "research/out/announcement_event_study.md",
            "bench": "全市场等权均值（非中位数）",
            "stocks": 5575,
            "span": "1990-12-19 ~ 2026-10-30",
            "trade_days": 8755,
            "cooldown": 60,
            "criterion": 3.0,
            "tests": len(rows),
            "sig_n": n_sig,
            "boot": 400,
            # 诚实标注：判据 3.0 是按「45 次检验」定的，实跑是 60 次。
            # 严格 Bonferroni（α=0.05/60）对应双侧 z≈3.34，故 3.0 略宽松。
            "strict_t": STRICT_T,
            "borderline": borderline,
            "caveats": [
                "判据 |t|≥3.0 原按 45 次检验设定；本次实跑 %d 次检验，严格 Bonferroni "
                "（α=0.05/%d）约需 |t|≥%.2f —— 落在 3.0~%.2f 之间的 %d 条请以 95%% CI 是否跨零为准，"
                "不要当成硬结论" % (len(rows), len(rows), STRICT_T, STRICT_T, len(borderline)),
                "股票池只含当前仍在册的 5575 只 → 有幸存者偏差，退市股的历史样本缺失，"
                "对「退市风险」这一类的估计尤其不可信（n 已偏小）",
                "未控制市值 / 行业 / 前期涨幅 —— 这是单变量统计，分层检验留给后续",
                "同一 (代码, 类别) 60 自然日内只取第一次，避免同一件事被反复计数",
                "均值超额与日胜率会背离（右尾驱动），两个指标必须一起看，只看均值会踩陷阱",
            ],
        },
        "cats": cats,
        "rows": rows,
    }

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    js = (
        "/* 自动生成，请勿手改 —— 真源：research/out/announcement_event_study.md\n"
        "   重新生成：.venv/Scripts/python.exe research/gen_ann_event_js.py\n"
        "   口径：全市场等权均值 · 按交易日聚合 · 判据 |t|≥3.0 · block bootstrap 400 次 */\n"
        "window.ANN_EV = " + body + ";\n"
    )
    with io.open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(js)

    print("写入 %s" % OUT)
    print("  类别 %d 个 · 数据行 %d 条 · 显著 %d/%d (%.1f%%) · %.1f KB"
          % (len(cats), len(rows), n_sig, len(rows), 100.0 * n_sig / len(rows), len(js) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
