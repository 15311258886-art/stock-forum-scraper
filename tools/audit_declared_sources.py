"""来源审计（v2）：把「此前明确列出的平台清单」逐条对到「配置 / 适配器 / 真实数据」三层。

背景：用户问「此前已明确列出多个知识博主、实盘人员的网站与贴吧等来源，为何未纳入抓取」。
本脚本不做推断，只做三件事：
  1. 从 `股票投资垂直社区平台清单_整合版.html` 抽出**声明过的平台清单**（用户侧既定来源）；
  2. 在 config/*.yaml 的文本里找同名/别名出现（声明层）；
  3. 在 overseas/sources.py 的 FETCHERS 与 ingest.PARSER_REGISTRY 里找适配器（能力层）；
  4. 在 SQLite 实际表里找数据（事实层）。
最后按平台逐行输出 4 列判定，并给出「已具备能力但未启用」与「完全缺失」两组。

用法：python tools/audit_declared_sources.py
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

LIST_HTML = os.path.join(os.path.dirname(ROOT), "股票投资垂直社区平台清单_整合版.html")

# 声明清单里的条目 → 用于在配置/代码里检索的关键词（含别名/域名）。
# 只列「可作为抓取目标」的前五节 37 条；后七节是画像/分级/对比等分析表，不是来源。
# 判据只用「真实域名 / 适配器键名」这类硬证据，**不能用中文泛称**
# —— 否则「贴吧」会因为模块 docstring 里写着「股票论坛/贴吧」而误判为「已有适配器」。
# （本项目实测踩过这个坑：中文泛称在文档字符串里出现 ≠ 有抓取能力。）
ALIASES = {
    "东方财富股吧": ["guba.eastmoney", "eastmoney_guba"],
    "同花顺股吧 / 论股堂 / 问财": ["10jqka", "ths"],
    "新浪财经股吧": ["sina.com.cn", "finance.sina"],
    "大智慧股吧": ["dazhihui", "gw.com.cn"],
    "金融界股吧 / 论坛": ["jrj.com.cn", "jrj"],
    "中金在线股吧": ["cnfol", "中金在线"],
    "腾讯自选股社区": ["gu.qq.com", "tencent"],
    "雪球（综合侧）": ["xueqiu"],
    "理想论坛（55188）": ["55188", "lixiang"],
    "MACD 俱乐部": ["macd"],
    "鼎砥投资论坛": ["dingdi"],
    "老钱庄股票论坛": ["laoqianzhuang"],
    "创幻财经论坛": ["chuanghuan"],
    "股海明灯（量学）": ["股海明灯"],
    "上善若水股票论坛": ["上善若水"],
    "股票天下论坛": ["股票天下"],
    "股神之路股票论坛": ["股神之路"],
    "股城股票论坛": ["gucheng"],
    "前沿观察股票论坛": ["前沿观察"],
    "缠论谈": ["chanlun"],
    "虎扑股票区": ["hupu"],
    # 百度系贴吧：代码里没有 tieba 域名字样，故一律归「完全缺失」
    "股票吧": ["tieba"],
    "炒股吧": ["tieba"],
    "股市吧": ["tieba"],
    "股民吧": ["tieba"],
    "股票分析吧 / 股票走势吧": ["tieba"],
    "缠中说禅吧": ["tieba"],
    "期货吧 / 基金吧": ["tieba"],
    "深交所互动易": ["cninfo", "irm.cninfo"],
    "上证 e 互动": ["sseinfo", "sse.com.cn"],
    "微信公众号（财经大V/机构）": ["mp.weixin", "weixin.qq"],
    "微博财经": ["weibo.com", "weibo"],
    "知乎（宽客/投资话题）": ["zhihu"],
    "抖音财经": ["douyin"],
    "小红书（金融理财）": ["xiaohongshu"],
    "B站": ["bilibili"],
    "微信视频号": ["channels.weixin", "视频号"],
}


def read(p):
    with io.open(p, encoding="utf-8", errors="ignore") as f:
        return f.read()


def normalize(name):
    """去掉序号残留与空白，便于比对。"""
    return re.sub(r"<[^>]+>", "", name).strip()


def extract_declared():
    if not os.path.exists(LIST_HTML):
        return None
    s = read(LIST_HTML)
    names = [normalize(n) for n in re.findall(r'<td class="pname">(.*?)</td>', s, re.S)]
    # 只取前五节（真实来源），后七节是分析表
    heads = re.findall(r"<h2[^>]*>(.*?)</h2>", s, re.S)
    cut = None
    for h in heads:
        if h.strip().startswith("六、"):
            cut = normalize(h)
            break
    if cut:
        idx = names.index(cut) if cut in names else None
        if idx:
            names = names[:idx]
    return [n for n in names if n]


def scan_sources():
    """返回 (config_text, code_text, db_sites) 三元组。"""
    cfg = []
    for fn in sorted(os.listdir(os.path.join(ROOT, "config"))):
        if fn.endswith((".yaml", ".yml")):
            cfg.append(read(os.path.join(ROOT, "config", fn)))
    cfg_text = "\n".join(cfg).lower()

    code_files = []
    for sub in ("overseas", "scraper", "viewer"):
        d = os.path.join(ROOT, sub)
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            if "demo_data.py" in files:
                files.remove("demo_data.py")   # 合成数据生成器不算能力
            for f in files:
                if f.endswith(".py"):
                    code_files.append(os.path.join(root, f))
    code_text = "\n".join(read(p) for p in code_files).lower()
    return cfg_text, code_text, code_files


# 判断一行是不是「合成数据」：示例数据生成器把 URL 统一写成 example.local 占位，
# 海外示例源写成 example.local/seed/*。真实抓取的 URL 一定落在真实域名上。
DEMO_SQL = ("(COALESCE({col},'') LIKE '%example.local%' "
            "OR COALESCE({col},'') = '' )")


def db_facts():
    """按表 × 来源统计 two 口径：tot（总行数）与 real（排除了合成占位的行数）。

    必须分开：否则「示例库里带了 4 个平台名字」会被误判成「真的抓过 4 个平台」。
    """
    from viewer.db import Database
    db = Database()
    out = {}
    for tbl, col, urlcol in (("raw_pages", "site", "url"),
                             ("posts", "site", "post_url"),
                             ("trades", "site", "post_url"),
                             ("positions", "site", "post_url"),   # 持仓表的来源列也叫 post_url
                             ("players", "platform", "source_url"),
                             ("news", "site", "url"),
                             ("overseas_news", "source_key", "url")):
        try:
            if urlcol:
                sql = (f"SELECT {col} k, COUNT(*) n, "
                       f"SUM(CASE WHEN {DEMO_SQL.format(col=urlcol)} THEN 0 ELSE 1 END) r "
                       f"FROM {tbl} GROUP BY {col} ORDER BY n DESC")
                rows = db.conn.execute(sql)
                out[tbl] = {x["k"]: (x["n"], x["r"] or 0) for x in rows}
            else:
                rows = db.conn.execute(
                    f"SELECT {col} k, COUNT(*) n FROM {tbl} GROUP BY {col} ORDER BY n DESC")
                out[tbl] = {x["k"]: (x["n"], x["n"]) for x in rows}   # 无 URL 列，无法区分
        except Exception:
            out[tbl] = {}
    out["_demo"] = db.get_meta("demo")
    return out


def main():
    print("=" * 78)
    print("来源审计 v2：声明清单 × 配置 × 适配器 × 真实数据")
    print("=" * 78)

    declared = extract_declared()
    if not declared:
        print("[!] 未找到清单文件：", LIST_HTML)
        return 1
    cfg_text, code_text, code_files = scan_sources()
    facts = db_facts()

    print(f"\n[声明清单] {os.path.basename(LIST_HTML)} → 可抓取来源 {len(declared)} 条")
    print(f"[扫描范围] config/*.yaml（{len(cfg_text)} 字符） + {len(code_files)} 个 py 文件（已排除 demo_data.py）")
    print(f"[示例数据] meta[demo] = {facts['_demo']!r}")

    print("\n" + "=" * 78)
    print(f"{'#':>3} {'平台':<24} {'配置':<5} {'适配器':<7} {'真数据':<7} {'示例':<5} 判定")
    print("=" * 78)

    have_cap, declared_only, missing, demo_only = [], [], [], []
    for i, name in enumerate(declared, 1):
        keys = ALIASES.get(name)
        if not keys:
            continue
        in_cfg = any(k in cfg_text for k in keys)
        in_code = any(k in code_text for k in keys)
        real, demo = [], []
        for tbl, d in facts.items():
            if tbl.startswith("_"):
                continue
            for k, (n, r) in d.items():
                if not any(key in (k or "").lower() for key in keys):
                    continue
                if r:
                    real.append(f"{tbl}:{k}={r}")
                if n - r:
                    demo.append(f"{tbl}:{k}={n - r}")

        if real:
            verdict, bucket = "真实抓取 ✓", None
        elif demo:
            verdict, bucket = "仅示例数据 ✗", demo_only
        elif in_code:
            verdict, bucket = "有能力·未产出", have_cap
        elif in_cfg:
            verdict, bucket = "仅声明·无适配器", declared_only
        else:
            verdict, bucket = "完全缺失", missing
        if bucket is not None:
            bucket.append(name)

        print(f"{i:>3} {name:<24} {'有' if in_cfg else '—':<5} "
              f"{'有' if in_code else '—':<7} {'有' if real else '—':<7} "
              f"{'有' if demo else '—':<5} {verdict}"
              + (f"  ({', '.join(real[:2])})" if real else "")
              + (f"  [示例 {', '.join(demo[:2])}]" if demo and not real else ""))

    print("\n" + "=" * 78)
    print("分组结论（真数据与示例数据严格分开）")
    print("=" * 78)
    print(f"\n[0] 仅示例数据、从未真实抓取（{len(demo_only)} 条）"
          f"——「界面上看得到名字，但那是合成的」")
    for x in demo_only:
        print("   -", x)
    print(f"\n[A] 已具备抓取能力、但无任何产出（{len(have_cap)} 条）——「代码有、没跑出来」")
    for x in have_cap:
        print("   -", x)
    print(f"\n[B] 只在配置里声明、无对应适配器（{len(declared_only)} 条）——「写了但抓不了」")
    for x in declared_only:
        print("   -", x)
    print(f"\n[C] 完全缺失（{len(missing)} 条）——「清单里有，程序里没有」")
    for x in missing:
        print("   -", x)

    print("\n" + "=" * 78)
    print("真实数据落点（事实层；括号内为排除 example.local 占位后的真实条数）")
    print("=" * 78)
    for tbl in ("raw_pages", "posts", "trades", "positions", "players", "news", "overseas_news"):
        d = facts.get(tbl) or {}
        tot = sum(n for n, _ in d.values())
        rr = sum(r for _, r in d.values())
        head = ", ".join(f"{k}={n}(真{r})" for k, (n, r) in list(d.items())[:5])
        print(f"  {tbl:<15} 共 {tot:>4} 行，真实 {rr:>4} 行  |  {head}")

    rp = facts.get("raw_pages") or {}
    real_rp = [k for k, (_n, r) in rp.items() if r]
    print(f"\n[结论] 真正被成功抓取并存档的来源（raw_pages 有真实记录）：{real_rp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
