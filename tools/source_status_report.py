"""来源可用状态总表（2026-09-23）。

回答「我能访问哪些信息源、各自什么状态、要你配合什么」这个问题。
把三条互不相干的管线拉齐到同一张表上：

  1) 国内股吧/论坛  —— config/sites.yaml 的 sites + scraper 的 PARSER_REGISTRY + raw_pages 真存档
  2) 海外资讯/RSS   —— config/overseas_sources.yaml 的 sources + overseas/sources.py 的 _FETCHERS
  3) 学习知识库     —— config/kb_sources.yaml 的 kb_sources

判定只看两件事：**有没有适配器**（能力）× **有没有真实产出**（事实）。
配置里写了但代码不认的、代码认了但没跑通的，分别归到不同状态，不混成一句「不可用」。

用法：
    python tools/source_status_report.py            # 全表
    python tools/source_status_report.py --short    # 只输出汇总与待办
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 真实抓取存档里出现过的 site 值 → 中文名（用于事实层核对）
SITE_CN = {
    "taoguba": "淘股吧", "tgb": "淘股吧",
    "eastmoney": "东方财富", "eastmoney_guba": "东财股吧",
    "xueqiu": "雪球", "xueqiu_hot": "雪球热帖", "ths": "同花顺",
    "sina": "新浪财经", "jrj": "金融界", "cnfol": "中金在线",
}


def _read(p) -> str:
    try:
        return io.open(p, encoding="utf-8").read()
    except OSError:
        return ""


# ---------------------------------------------------------------- 1. 国内

def domestic_rows() -> list[dict]:
    """国内股吧/论坛：以「代码里真有适配器」为能力判据，以「raw_pages 真有非示例存档」为事实判据。"""
    from viewer.db import Database

    db = Database()
    sites_cfg = _read(ROOT / "config" / "sites.yaml")
    # 配置里实际启用的站点（顶层 sites: 下的键，跳过注释）
    cfg_keys = []
    in_sites = False
    for line in sites_cfg.splitlines():
        if re.match(r"^sites:\s*$", line):
            in_sites = True
            continue
        if in_sites:
            m = re.match(r"^  ([A-Za-z_][\w-]*):\s*$", line)
            if m:
                cfg_keys.append(m.group(1))
            elif line.strip() and not line.startswith(" ") and not line.startswith("#"):
                in_sites = False

    # 代码里的解析器注册表
    reg_src = _read(ROOT / "scraper" / "pipeline.py")
    parser_keys = re.findall(r'"([a-z_0-9]+)"\s*:\s*\w+Parser', reg_src)

    # 真实存档（排除 example.local 合成数据）
    real_pages = {}
    for r in db.conn.execute(
        "SELECT site, COUNT(*) n FROM raw_pages "
        "WHERE COALESCE(url,'') NOT LIKE '%example.local%' GROUP BY site"
    ):
        real_pages[r["site"]] = r["n"]

    login = {}
    for r in db.conn.execute("SELECT * FROM login_status"):
        login[r["site"]] = dict(r)

    rows = []
    for k in sorted(set(cfg_keys) | set(parser_keys)):
        cn = SITE_CN.get(k, k)
        has_parser = k in parser_keys
        n_pages = real_pages.get(k, 0)
        lg = login.get(k) or {}
        status = lg.get("status") or ""
        detail = (lg.get("detail") or "")[:110]
        if n_pages and status == "ok":
            st, why = "✅ 正常", f"已存档 {n_pages} 页，登录态正常"
        elif n_pages:
            st, why = "⚠️ 抓到了但没解析出来", (
                f"网络层通（已存档 {n_pages} 页），但登录态={status or '未检测'}"
                + (f"：{detail}" if detail else "")
                + "。选手主页等受限页返回登录跳转页 → posts/trades/positions 解析为 0"
            )
        elif has_parser:
            st, why = "⏸ 有适配器但无产出", "适配器已注册，尚未成功抓取"
        else:
            st, why = "❌ 无适配器", "配置里存在但代码未注册解析器"
        rows.append({"key": k, "name": cn, "parser": has_parser, "pages": n_pages,
                     "status": st, "why": why,
                     "login": (lg.get("status") or "-")})
    return rows


# ---------------------------------------------------------------- 2. 海外

def overseas_rows() -> list[dict]:
    import yaml

    cfg = yaml.safe_load(_read(ROOT / "config" / "overseas_sources.yaml")) or {}
    srcs = cfg.get("sources") or []
    fe_src = _read(ROOT / "overseas" / "sources.py")
    fetchers = set(re.findall(r'"([a-z_]+)":\s*_fetch_', fe_src))

    from viewer.db import Database

    db = Database()
    real = {}
    total = {}
    for r in db.conn.execute(
        "SELECT source_key, COUNT(*) n, "
        "SUM(CASE WHEN COALESCE(url,'') NOT LIKE '%example.local%' THEN 1 ELSE 0 END) real "
        "FROM overseas_news GROUP BY source_key"
    ):
        total[r["source_key"]] = r["n"]
        real[r["source_key"]] = r["real"]

    rows = []
    for s in srcs:
        key = s.get("key") or ""
        typ = s.get("type") or "rss"
        enabled = bool(s.get("enabled"))
        has_fe = typ in fetchers
        n_real = real.get(key, 0)
        gates = []
        if s.get("requires_vpn"):
            gates.append("需境外网络")
        if s.get("requires_key"):
            gates.append("需 API Key")
        if n_real and enabled:
            st = "✅ 正常"
        elif not enabled:
            st = "⏸ 已写好但未启用"
        elif not has_fe:
            st = "❌ 类型无适配器"
        else:
            st = "❌ 启用但抓不到"
        rows.append({
            "key": key, "name": s.get("name") or key, "region": s.get("region") or "",
            "type": typ, "enabled": enabled, "has_fetcher": has_fe,
            "synthetic": total.get(key, 0) - n_real,
            "real": n_real, "status": st, "gates": gates,
            "note": (s.get("note") or "")[:130],
        })
    return rows


# ---------------------------------------------------------------- 3. 知识库

def kb_rows() -> list[dict]:
    import yaml

    cfg = yaml.safe_load(_read(ROOT / "config" / "kb_sources.yaml")) or {}
    out = []
    for c in cfg.get("kb_sources") or []:
        paths = []
        p1 = c.get("path")
        if p1:
            paths.append(Path(p1))
        import glob as _g
        pg = c.get("path_glob")
        if pg:
            paths += [Path(x) for x in _g.glob(pg, recursive=True)]
        hit = [p for p in paths if p.exists()]
        out.append({
            "key": c.get("doc_key"), "track": c.get("track"),
            "enabled": bool(c.get("enabled")), "found": bool(hit),
            "file": (str(hit[0]) if hit else (p1 or "")),
            "n_found": len(hit),
        })
    return out


def live_overseas() -> list[dict]:
    """界面读的就是这个接口，用它交叉验证上面的静态判定。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/overseas_sources",
                                    timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # 查看器没起也不该让报告失败
        return [{"_err": str(e)[:120]}]
    return d if isinstance(d, list) else (d.get("sources") or d.get("list") or [])


def main() -> int:
    short = "--short" in sys.argv

    dom = domestic_rows()
    ovs = overseas_rows()
    kbs = kb_rows()

    print("=" * 78)
    print("一、国内股吧 / 论坛（config/sites.yaml + 解析器注册表 + 真存档）")
    print("=" * 78)
    for r in dom:
        print(f"  {r['status']:<22} {r['name']:<8} key={r['key']:<10} "
              f"适配器={'有' if r['parser'] else '无'}  真存档={r['pages']}页")
        print(f"        {r['why']}")

    print()
    print("=" * 78)
    print(f"二、海外资讯 / RSS（共 {len(ovs)} 个源）")
    print("=" * 78)
    en = [r for r in ovs if r["enabled"]]
    dis = [r for r in ovs if not r["enabled"]]
    print(f"  已启用 {len(en)} 个 / 未启用 {len(dis)} 个")
    for label, group in (("已启用", en), ("未启用", dis)):
        print(f"\n  --- {label} ---")
        for r in group:
            gate = ("；".join(r["gates"]) or "无") if label == "未启用" else ""
            tail = f"  门槛={gate}" if label == "未启用" else f"  真实条数={r['real']}"
            print(f"    {r['status']:<18} {r['name'][:26]:<28} type={r['type']:<14}{tail}")
            if r["note"]:
                print(f"        备注：{r['note']}")

    ok_ov = [r for r in en if r["real"] > 0]
    print(f"\n  小结：{len(ok_ov)}/{len(en)} 个已启用源真实产出 > 0；"
          f"未启用 {len(dis)} 个（其中需 VPN {len([r for r in dis if '需境外网络' in r['gates']])} 个、"
          f"需 Key {len([r for r in dis if '需 API Key' in r['gates']])} 个）")

    print()
    print("=" * 78)
    print("三、学习知识库（config/kb_sources.yaml）")
    print("=" * 78)
    for r in kbs:
        print(f"  {'✅ 文件在' if r['found'] else '❌ 文件找不到'}  {r['track']}  "
              f"[{r['key']}]  enabled={r['enabled']}  命中 {r['n_found']} 个文件")
        print(f"        {r['file']}")

    print()
    print("=" * 78)
    print("四、界面实际读到的海外源状态（/api/overseas_sources 交叉核对）")
    print("=" * 78)
    lo = live_overseas()
    if lo and lo[0].get("_err"):
        print("  接口不可用：", lo[0]["_err"])
    else:
        for r in lo:
            print(f"    {str(r.get('status') or '-'):<14} {str(r.get('name') or '')[:26]:<28} "
                  f"enabled={r.get('enabled')}  items={r.get('item_count')}")

    print()
    print("=" * 78)
    print("五、需要你配合的事项（按优先级）")
    print("=" * 78)
    acts = []
    for r in dom:
        if r["pages"] and r["login"] != "ok":
            acts.append(("P0", f"在本机浏览器登录 {r['name']}（tgb.cn），"
                               f"然后执行 credentials add-cookie —— 打通选手/持仓/实盘四条数据链"))
    if not any(True for r in kbs if not r["found"]):
        pass
    for r in kbs:
        if not r["found"]:
            acts.append(("P0", f"确认知识库文件仍在：{r['file']}"))
    need_vpn = [r["name"] for r in dis if "需境外网络" in r["gates"]]
    need_key = [r["name"] for r in dis if "需 API Key" in r["gates"]]
    if need_key:
        acts.append(("P1", "提供 API Key：" + "、".join(need_key)))
    if need_vpn:
        acts.append(("P2", f"保持境外网络可用（{len(need_vpn)} 个源）："
                           + "、".join(need_vpn[:8]) + ("…" if len(need_vpn) > 8 else "")))
    if not acts:
        acts.append(("-", "当前无阻塞项"))
    for pri, txt in acts:
        print(f"  [{pri}] {txt}")

    if "--short" in sys.argv:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
