# -*- coding: utf-8 -*-
"""全球大盘 / 海外市场（2026-10-17 新增 · 「消息与外围」板块）

目标：为「消息与外围」补齐**外部市场环境**维度——美股 / 日韩 / 欧洲等主要经济体的大盘涨跌
与板块涨跌，作为 A 股研判的外部输入（提升反应灵敏度与维度完整度）。

数据来源（全部国内可直连、免登录、免 cookie）：
  ① 新浪财经 hq.sinajs.cn   gb_ 前缀 → 美股指数 / SPDR 行业 ETF / 国家 ETF 代理（实时）
  ② 腾讯行情 qt.gtimg.cn    hk 前缀 → 港股指数（实时）
  （需带 Referer=https://finance.sina.com.cn，否则新浪 403/空）

【口径与诚实边界 — 不夸大、不冒充】
  · 美股大盘 = **真实指数**（道指/纳指/标普/费半），字段 kind="index"。
  · 日、韩、德、英、法、中国台湾、印度、巴西等**无稳定的国内免费实时指数源**，
    改用**美股上市的国家 ETF（iShares MSCI 系列）作代理**：字段 proxy=True，
    界面须标注「ETF 代理（含汇率与美股情绪因素；方向可参考，点位不等同本地指数）」。
  · 新浪 int_ 系列国际指数接口**实测为陈旧数据**（道指返回 46247.52 vs 实时 52001.52，
    方向甚至相反），本项目一律不用——这是 2026-10-17 实测结论，勿回退。

  · 已确认**不可用**的源（勿再试）：东方财富 push2 整域（沙箱/机房 IP 被风控，
    RemoteDisconnected）、网易 126 数据源（连接超时）、雪球（需 xq_a_token）、
    同花顺 d.10jqka（502）。东财 fundf10 / datacenter-web 可连但不覆盖这些品种。

产出：
  · global_market_cache.json —— 离线可读的行情快照（供 /api/global_market）
  · global_market_state.json —— 刷新状态（冷却计时）
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "global_market_cache.json")
STATE = os.path.join(ROOT, "global_market_state.json")

CN_TZ = timezone(timedelta(hours=8))
COOLDOWN_MIN = 30          # 后台刷新冷却（分钟）
STALE_MIN = 45             # 超过此时长视为陈旧，接口访问时尝试同步补抓

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_REF = "https://finance.sina.com.cn"

# ── 美股大盘指数（真实指数）──────────────────────────────
US_INDICES = [
    ("gb_$dji",  "道琼斯",     "US"),
    ("gb_$ixic", "纳斯达克",   "US"),
    ("gb_$inx",  "标普500",    "US"),
    ("gb_$sox",  "费城半导体", "US"),
]

# ── 美股行业板块（SPDR 11 只行业 ETF，作为「板块涨跌」代理）────
US_SECTORS = [
    ("gb_xlk",  "科技"),
    ("gb_xlf",  "金融"),
    ("gb_xlv",  "医疗"),
    ("gb_xle",  "能源"),
    ("gb_xli",  "工业"),
    ("gb_xlp",  "必需消费"),
    ("gb_xly",  "可选消费"),
    ("gb_xlb",  "材料"),
    ("gb_xlu",  "公用事业"),
    ("gb_xlre", "房地产"),
    ("gb_xlc",  "通信服务"),
]

# ── 主题关注（跨市场）──────────────────────────────────
THEMES = [
    ("gb_smh",  "半导体(SMH)"),
    ("gb_kweb", "中概互联(KWEB)"),
]

# ── 跨资产风向标 ───────────────────────────────────────
CROSS_ASSET = [
    ("gb_gld", "黄金(GLD)"),
    ("gb_uso", "原油(USO)"),
    ("gb_tlt", "美债20年+(TLT)"),
    ("gb_uup", "美元(UUP)"),
]

# ── 主要经济体（美股上市国家 ETF 代理；proxy=True）──────────
COUNTRY_PROXY = [
    ("gb_ewj",  "日本",     "JP"),
    ("gb_ewy",  "韩国",     "KR"),
    ("gb_ewg",  "德国",     "DE"),
    ("gb_ewu",  "英国",     "UK"),
    ("gb_ewq",  "法国",     "FR"),
    ("gb_ewt",  "中国台湾", "TW"),
    ("gb_inda", "印度",     "IN"),
    ("gb_ewz",  "巴西",     "BR"),
    ("gb_ewc",  "加拿大",   "CA"),
]

# ── 港股指数（腾讯，真实指数）────────────────────────────
HK_INDICES = [
    ("hkHSI",    "恒生指数"),
    ("hkHSCEI",  "恒生国企"),
    ("hkHSTECH", "恒生科技"),
]


# ══════════════════ 网络 ══════════════════

def _http(url: str, referer: str | None = None, timeout: int = 15) -> str:
    """GET → str。新浪/腾讯行情均为 GBK，故 utf-8 优先、失败回退 gbk。"""
    h = {"User-Agent": _UA}
    if referer:
        h["Referer"] = referer
    raw = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout).read()
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except Exception:  # noqa: BLE001
            continue
    return raw.decode("utf-8", "ignore")


def _f(x) -> float | None:
    try:
        return float(x)
    except Exception:  # noqa: BLE001
        return None


# ══════════════════ 行情时间（双时区标注，2026-10-20 新增）══════════════════

def _mode_time(rows) -> str | None:
    """取一组行情行里出现次数最多的 time 字符串（代表本轮行情时点）。"""
    from collections import Counter
    ts = [r.get("time") for r in (rows or []) if r.get("time")]
    return Counter(ts).most_common(1)[0][0] if ts else None


def _us_dst_offset_hours(y: int, m: int, d: int) -> int:
    """美国东部时区相对 UTC 的偏移小时数（含夏令时规则，无需 tzdata）。
    EDT=-4（3月第2个周日 ～ 11月第1个周日），EST=-5。"""
    from datetime import date, timedelta as _td

    def nth_sun(n: int) -> date:
        first = date(y, m, 1)
        return first + _td(days=(6 - first.weekday()) % 7 + (n - 1) * 7)

    if m in (4, 5, 6, 7, 8, 9, 10):
        return 4
    if m in (1, 2, 12):
        return 5
    if m == 3:
        return 4 if date(y, m, d) >= nth_sun(2) else 5
    return 4 if date(y, m, d) < nth_sun(1) else 5  # 11 月


def _enrich_times(obj: dict) -> dict:
    """推导并补全行情时间字段（幂等）：
    · us_time  —— 美股行情时间。新浪 gb_ 源返回的是**北京时间**（2026-10-19 实测：
      time 与抓取时刻同钟，美股盘中实时报价）。
    · us_time_et —— 换算后的美东时间（按美国夏令时规则回推）。
    · us_session  —— 盘中实时 / 已收盘（按美东时刻 + 周一~五 09:30-16:00 判定）。
    · hk_time —— 港股行情时间（腾讯源，北京时间，格式 2026/10/16 16:08:38）。"""
    us = obj.get("us_time") or _mode_time(obj.get("indices"))
    hk = obj.get("hk_time") or _mode_time(obj.get("hk"))
    obj["us_time"], obj["hk_time"] = us, hk
    obj["us_time_et"] = None
    obj["us_session"] = None
    if us:
        try:
            dt = datetime.strptime(str(us).replace("/", "-"), "%Y-%m-%d %H:%M:%S")
            # 北京(UTC+8) → 美东(UTC-4/-5)：总差 = 8 + off 小时（EDT 差12、EST 差13）
            et = dt - timedelta(hours=8 + _us_dst_offset_hours(dt.year, dt.month, dt.day))
            obj["us_time_et"] = et.strftime("%Y-%m-%d %H:%M:%S")
            hm = et.strftime("%H:%M")
            obj["us_session"] = ("盘中实时" if (et.weekday() < 5 and "09:30" <= hm <= "16:00")
                                 else "已收盘（上一交易时段）")
        except Exception as e:  # noqa: BLE001
            log.warning("全球大盘：行情时间解析失败 %s", e)
    return obj


# ══════════════════ 解析 ══════════════════

def _parse_sina(text: str) -> dict:
    """hq.sinajs.cn 行格式：var hq_str_gb_xxx="名称,现价,涨跌幅%,时间,涨跌额,开盘,最高,最低,52w高,52w低,...";"""
    out: dict[str, dict] = {}
    for line in text.splitlines():
        m = re.match(r'var hq_str_([A-Za-z0-9_$]+)\s*=\s*"(.*)";', line.strip())
        if not m:
            continue
        code, body = m.group(1), m.group(2)
        if not body:
            continue
        f = body.split(",")
        if len(f) < 4:
            continue
        price, pct = _f(f[1]), _f(f[2])
        if price is None or pct is None:
            continue
        out[code] = {"name": (f[0] or "").strip(), "price": price, "pct": pct,
                     "chg": _f(f[4]) if len(f) > 4 else None,
                     "time": (f[3] or "").strip()}
    return out


def _parse_tencent(text: str) -> dict:
    """qt.gtimg.cn 行格式：v_hkHSI="1~名称~代码~现价~昨收~开盘~…~时间~涨跌额~涨跌幅%~…";"""
    out: dict[str, dict] = {}
    for line in text.splitlines():
        m = re.match(r'v_([A-Za-z0-9_]+)\s*=\s*"(.*)";', line.strip())
        if not m:
            continue
        code, body = m.group(1), m.group(2)
        f = body.split("~")
        if len(f) < 33:
            continue
        price, pct = _f(f[3]), _f(f[32])
        if price is None or pct is None:
            continue
        out[code] = {"name": (f[1] or "").strip(), "price": price, "pct": pct,
                     "chg": _f(f[31]), "time": (f[30] or "").strip()}
    return out


# ══════════════════ 抓取 ══════════════════

def fetch(progress=None) -> dict:
    """抓一轮全球行情 → 写缓存。网络异常抛给调用方（由 maybe_refresh 兜住）。"""
    def say(msg):
        if callable(progress):
            try:
                progress(msg)
            except Exception:  # noqa: BLE001
                pass

    codes = [c for c, *_ in US_INDICES + US_SECTORS + THEMES + CROSS_ASSET + COUNTRY_PROXY]
    say("拉取新浪全球行情（%d 个标的）…" % len(codes))
    sina = _parse_sina(_http("https://hq.sinajs.cn/list=" + ",".join(codes), referer=_REF))
    if not sina:  # $ 号被 URL 层吞掉的兜底
        alt = ",".join(c.replace("$", "%24") for c in codes)
        sina = _parse_sina(_http("https://hq.sinajs.cn/list=" + alt, referer=_REF))

    say("拉取港股指数…")
    try:
        tencent = _parse_tencent(_http("https://qt.gtimg.cn/q=" + ",".join(c for c, *_ in HK_INDICES)))
    except Exception as e:  # noqa: BLE001
        log.warning("全球大盘：腾讯港股抓取失败 %s", e)
        tencent = {}

    def pack(lst, extra=None):
        rows = []
        for item in lst:
            code = item[0]
            d = sina.get(code)
            if not d:
                continue
            row = {"code": code, "name": item[1], "price": d["price"],
                   "pct": d["pct"], "chg": d["chg"], "time": d["time"], "kind": "index"}
            if extra:
                row[extra] = item[2]
            rows.append(row)
        return rows

    indices = pack(US_INDICES, "region")
    sectors = pack(US_SECTORS)
    for r in sectors:
        r["kind"] = "sector"
    themes = pack(THEMES)
    for r in themes:
        r["kind"] = "theme"
    cross = pack(CROSS_ASSET)
    for r in cross:
        r["kind"] = "cross"
    countries = pack(COUNTRY_PROXY, "region")
    for r in countries:
        r["kind"] = "country"
        r["proxy"] = True          # ⚠️ ETF 代理，非本地指数
    hk = []
    for code, name in HK_INDICES:
        d = tencent.get(code)
        if d:
            hk.append({"code": code, "name": name, "price": d["price"], "pct": d["pct"],
                       "chg": d["chg"], "time": d["time"], "kind": "index", "region": "HK"})

    now = datetime.now(CN_TZ)
    obj = {
        "ok": bool(indices or sectors or countries),
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
        "indices": indices,
        "sectors": sectors,
        "themes": themes,
        "cross_asset": cross,
        "countries": countries,
        "hk": hk,
        "summary": _summarize(indices, sectors, countries, hk, cross),
        "note": "美股为实时真实指数；日韩欧等为美股上市国家 ETF 代理（含汇率因素），方向可参考、点位不等同本地指数。",
    }
    obj = _enrich_times(obj)
    try:
        tmp = CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(obj, fp, ensure_ascii=False, indent=1)
        os.replace(tmp, CACHE)
    except Exception as e:  # noqa: BLE001
        log.warning("全球大盘：写缓存失败 %s", e)
    _write_state(now)
    return obj


def _summarize(indices, sectors, countries, hk, cross) -> dict:
    def avg(rows):
        v = [r["pct"] for r in rows if r.get("pct") is not None]
        return round(sum(v) / len(v), 2) if v else None

    us_avg = avg(indices)
    tone = "neutral"
    if us_avg is not None:
        if us_avg <= -0.8:
            tone = "risk_off"
        elif us_avg >= 0.8:
            tone = "risk_on"

    s = [x for x in sectors if x.get("pct") is not None]
    s_sorted = sorted(s, key=lambda x: -x["pct"])
    c = [x for x in cross if x.get("pct") is not None]
    cross_sorted = sorted(c, key=lambda x: -x["pct"])
    return {
        "us_avg": us_avg,
        "risk_tone": tone,                       # risk_on / neutral / risk_off
        "sector_top": [{"name": x["name"], "pct": x["pct"]} for x in s_sorted[:3]],
        "sector_bottom": [{"name": x["name"], "pct": x["pct"]} for x in s_sorted[-3:]][::-1],
        "country_avg": avg(countries),
        "hk_avg": avg(hk),
        "cross_top": [{"name": x["name"], "pct": x["pct"]} for x in cross_sorted[:2]],
    }


# ══════════════════ 状态 / 冷却 ══════════════════

def _read_state() -> dict:
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _write_state(now: datetime) -> None:
    try:
        with open(STATE, "w", encoding="utf-8") as fp:
            json.dump({"last_ts": now.timestamp(),
                       "last_at": now.strftime("%Y-%m-%d %H:%M:%S")}, fp, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        log.warning("全球大盘：写状态失败 %s", e)


def _age_min() -> float | None:
    st = _read_state()
    ts = st.get("last_ts")
    if not ts:
        return None
    return (time.time() - float(ts)) / 60.0


# ══════════════════ 对外接口 ══════════════════

def payload(auto: bool = True) -> dict:
    """读缓存（离线优先）。auto=True 且缓存缺失/陈旧时，尝试同步补抓一次（失败静默返回旧值）。"""
    obj = None
    try:
        obj = json.load(open(CACHE, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        obj = None

    age = _age_min()
    stale = (age is None) or (age > STALE_MIN) or not (obj or {}).get("ok")
    if auto and stale:
        try:
            obj = fetch()
        except Exception as e:  # noqa: BLE001
            log.warning("全球大盘：同步补抓失败（沿用缓存）%s", e)

    if not obj:
        return {"ok": False, "as_of": None, "indices": [], "sectors": [], "themes": [],
                "cross_asset": [], "countries": [], "hk": [], "summary": {},
                "note": "暂无全球行情缓存，请稍后刷新。"}
    if not obj.get("us_time"):      # 旧缓存兼容：就地推导行情时间，无需等下次刷新
        obj = _enrich_times(obj)
    obj["age_min"] = round(age, 1) if age is not None else None
    return obj


def maybe_refresh(force: bool = False) -> dict:
    """调度钩子：带冷却的刷新（默认 30 分钟一次）。始终不抛异常。"""
    try:
        age = _age_min()
        if not force and age is not None and age < COOLDOWN_MIN:
            return {"skipped": True, "age_min": round(age, 1)}
        return fetch()
    except Exception as e:  # noqa: BLE001
        log.warning("全球大盘：调度刷新失败 %s", e)
        return {"ok": False, "error": str(e)[:120]}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    r = fetch(progress=print)
    print("ok=%s indices=%d sectors=%d countries=%d hk=%d"
          % (r["ok"], len(r["indices"]), len(r["sectors"]), len(r["countries"]), len(r["hk"])))
    print("summary=", json.dumps(r["summary"], ensure_ascii=False))
