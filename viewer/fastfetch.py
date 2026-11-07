# -*- coding: utf-8 -*-
"""一次请求拿全市场的两条外部通路（2026-11-03 D72 新增）。

## 这个模块存在的理由

日更链里有两个任务在**逐只抓新浪**：

    kline_daily    3362.8s   build_kline_db.main() → 逐只 api_kline()，5550 次请求
    kbase_daily    1886.8s   kbase.incremental_update() → 逐只新浪，5550 次请求

两个加起来 87 分钟，而且新浪限流敏感、必须把 workers 压到 2。真正的浪费不在网络，
在「一只票一个请求」这个形状上：全市场 5500 只，用批量接口 3~4 秒就能拿到当日快照。

本模块只做**取数与解析**，不碰任何库、不写任何文件 —— 落库口径全部在
`tools/fast_grab.py` 里，那里才有复权/幂等/对账的纪律。这样拆的理由是：
`viewer/server.py` 的新端点也要用同一个解析器，但它绝不该顺手拿到写库的能力。

## 两条通路（均已实测，数字见 DECISIONS.md D72）

1. **通达信官网盘后包** —— 一次 HTTP 拿全市场「今日一根」。
   `https://www.tdx.com.cn/products/data/data/g4day/{YYYYMMDD}.zip`
   实测 20261102：2,759,837 字节 / 9.3 秒 / 解出 52,314 行（沪 27,787 + 深 24,542 +
   北 392，含指数/债券/基金，过滤后是 5,498 只股票）。
   ⚠️ **硬约束：当天收盘后不是马上有包。** 20261103（当天）实测 HTTP 404。
   所以调用方必须准备降级路径，不能把「包没发布」当成异常。

2. **腾讯批量报价** —— `https://qt.gtimg.cn/q=sh600519,sz000001,...`
   实测 100 只一批 0.35 秒；全市场 121 批、6 并发 9.15 秒（其中一半是抓了 12,027 个
   含已退市/新三板的老代码，真实股票池 ~5,500 只约 4 秒）。
   对照 `a_shares_snapshot` 任务要 71.8 秒 —— 时间全花在 `PAGE_SLEEP=1.5` 的批间
   串行 sleep 上，不是网络慢。

## 盘后包格式（从 zip 实际字节反推，不是抄来的）

zip 内是「按市场分包」：`sh261102.cod` + `sh261102.md1`（sz/bj 同构）。实测：

    .cod  定长 **150 字节/只**，前 6 字节是代码 ASCII，偏移 40 起是 GBK 名称
    .md1  定长 **512 字节/只**，与 .cod 的条目**一一对应、同序**
          （已用 600519/601398/688981/300735 四只逐字段验证，见下）

`.md1` 里我们用到的偏移（**全部是 little-endian，价格/成交额是 double**）：

    4   prev_close（昨收，double）
    12  open      （double）
    20  high      （double）
    28  low       （double）
    36  close     （double）
    56  volume    （int32，单位：**股**）
    72  amount    （double，单位：**元**）

⚠️ 这里踩过两个坑，都写在这里免得下次重犯：

* **不是**通达信老日线那种「32 字节/天、日期在前」的格式。我曾按 32 字节去切，
  得到 474,592 条全是垃圾数（日期字段解出来是 4744）。真实记录是 512 字节的**盘口
  快照**（偏移 112 起还有五档买卖价），一只票一条。
* **不能用「文件字节数 ÷ 32 能整除」当格式正确的证据**。14226944 也能被 32 整除，
  但它真正的结构是 512 × 27787。
* 因此本模块**不按文件名/后缀猜市场**，而是把 zip 里所有 `.md1` 与同名 `.cod`
  配对后再解析；少一个市场就抛错（见 `tdx_daily_package`）。

## 成交量单位：只有靠数据自证，不能靠板块表

腾讯 `f[36]` 的成交量对**绝大多数**标的单位是「手」，但**科创板 688/689 是「股」**。
2026-11-03 实测（对照本地 kline_daily.db 同日数据）：

    600519  原始 74121     → ×100 = 7,412,100   库内 7,412,056    （手）
    000001  原始 2139674   → ×100 = 213,967,400 库内 213,967,422  （手）
    920001  原始 12589     → ×100 = 1,258,900   库内 1,258,857    （手，北交所也是手）
    688111  原始 5888662   → ×1   = 5,888,662   库内 5,888,662    （股！）
    689009  原始 10728039  → ×1   = 10,728,039  库内 10,728,039   （股！）

所以本模块**不写「688 用手/其余用股」这种前缀表**，而是复用 `viewer.units.infer_volume_unit`
（amount ÷ close ÷ volume ≈ 1 → 股，≈100 → 手），判不出就明确返回 None 并让上游降级。
理由见 viewer/units.py 顶部那段：×k 型污染「比值不变、差值全错」，不会报错。

## 本模块**不**做的事

* 不连数据库、不读文件、不写文件；
* 不 import pandas —— 它会被 `viewer/server.py` 的端点 import，服务端热路径禁止引 pandas；
* 不做复权。前复权/后复权是落库口径，属于 `tools/fast_grab.py` 的职责。
"""
from __future__ import annotations

import io
import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from viewer import units as U  # noqa: E402 —— 单位自证唯一来源，别处不许再写前缀表

# ---------------------------------------------------------------- 腾讯批量报价

TENCENT_URL = "https://qt.gtimg.cn/q="
TENCENT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://gu.qq.com/",
}
TENCENT_BATCH = 100      # 腾讯批量上限约 300，100 稳妥（与 fetch_a_shares_tencent 同口径）
TENCENT_WORKERS = 6
TENCENT_RETRY = 3
TENCENT_TIMEOUT = 10

# 字段序号与 tools/fetch_a_shares_tencent.py 的 _fetch_batch 完全相同。
# 这里单列成常量不是为了「更优雅」，而是因为腾讯这套 ~ 分隔的 88 字段没有任何文档，
# 序号一旦写错不会报错、只会把 PE 当 PB 用 —— 唯一可信来源就是那边已经逐字段
# 交叉验证过（茅台实测吻合）的实现，所以这份序号必须与它保持一致，改一处要改两处。
_F_NAME, _F_CODE, _F_CLOSE, _F_PREV, _F_OPEN = 1, 2, 3, 4, 5
_F_SESSION, _F_CHANGE, _F_PCT = 30, 31, 32
_F_HIGH, _F_LOW, _F_VOL, _F_AMT, _F_TURN = 33, 34, 36, 37, 38
_F_PE, _F_FLOAT_MV, _F_TOTAL_MV, _F_PB = 39, 44, 45, 46


def _coerce(v):
    """'--' / '' / '-' 这类腾讯用来表示「没有这个值」的占位，一律返回 None。

    照抄 fetch_a_shares_tencent._coerce 的语义：**不要**把缺失当 0。
    缺 PE（亏损股）和 PE=0 是两件事，混淆后估值分档会把亏损股排到最便宜。
    """
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "-", "--", "None", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _market_prefix(code: str) -> str:
    """腾讯接口必须带市场前缀（sh/sz/bj），否则整只票静默返回空。

    2026-11-01 的教训（fetch_a_shares_tencent 里记着）：按 `code[0] in ("8","4")`
    判北交所会把 920 段拼成 sh，结果 348 只永远拿不到行情且**不报错**。
    所以这里同样委托 security_master，不在本模块另写一套前缀表。
    """
    from viewer import security_master as SM
    ex = SM.exchange_of(code)
    return {SM.EXCHANGE_SSE: "sh", SM.EXCHANGE_SZSE: "sz", SM.EXCHANGE_BSE: "bj"}.get(ex, "sh")


def _fetch_tencent_batch(codes, timeout: float = TENCENT_TIMEOUT) -> dict:
    """请求一批（≤100 只），返回 {code: 原始字段 dict}。失败抛异常由调用方重试。"""
    q = ",".join(_market_prefix(c) + c for c in codes)
    # 逗号分隔符**不能**被 URL 编码（safe=","），否则腾讯不认整串、只回第一只。
    url = TENCENT_URL + urllib.parse.quote(q, safe=",")
    req = urllib.request.Request(url, headers=TENCENT_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("gbk", "ignore")
    out = {}
    for line in raw.strip().split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        rhs = line.split("=")[1].strip().strip('"')
        f = rhs.split("~")
        if len(f) < 47:
            # 少于 47 段 = 腾讯返回的是「空票占位」（如 v_sh600519="";），不是行情
            continue
        code = (f[_F_CODE] or "").strip()
        if not (len(code) == 6 and code.isdigit()):
            continue
        close = _coerce(f[_F_CLOSE])
        vol_raw = _coerce(f[_F_VOL])
        amt = (_coerce(f[_F_AMT]) or 0.0) * 10000.0   # 腾讯给「万元」，统一成「元」
        # 单位自证：判不出（amount/close 缺、或当日零成交）就留 None，
        # **绝不**回退成 ×100 —— 那会让科创板凭空多 100 倍成交量。
        unit = U.infer_volume_unit(vol_raw, amt, close)
        session = (f[_F_SESSION] or "").strip()
        total_mv_yi = _coerce(f[_F_TOTAL_MV])
        out[code] = {
            "code": code,
            "name": (f[_F_NAME] or "").strip(),
            "price": close,
            "open": _coerce(f[_F_OPEN]),
            "high": _coerce(f[_F_HIGH]),
            "low": _coerce(f[_F_LOW]),
            "prev_close": _coerce(f[_F_PREV]),
            "pct": _coerce(f[_F_PCT]),
            "change": _coerce(f[_F_CHANGE]),
            "volume": U.volume_to_shares(vol_raw, amt, close, unit=unit),   # 股；判不出为 None
            "volume_raw": vol_raw,                                          # 源原值（手 or 股）
            "volume_unit": unit,                                            # share / hand / None
            "amount": amt,                                                  # 元
            "turnover": _coerce(f[_F_TURN]),
            "pe_ttm": _coerce(f[_F_PE]),
            "pb": _coerce(f[_F_PB]),
            "mcap_yi": total_mv_yi,                                         # 总市值（亿元）
            "total_mv": (total_mv_yi or 0.0) * 1e8,
            "float_mv": (_coerce(f[_F_FLOAT_MV]) or 0.0) * 1e8,
            # f[30] = yyyymmddHHMMSS，源自己声明的「这是哪一场」。
            # 必须往上传：拿最新一场去填历史某天 = 伪造历史，不报错也无法事后分辨。
            #
            # `_session` 与 `session` **是同一个值，故意写两份**：
            # 落库前的守卫用的是既有生产代码
            # `tools/fetch_a_shares_tencent._session_mismatch()`，它读的键名是 `_session`
            # （前缀下划线是那边的私有约定）。2026-11-04 的 P1 就是因为这里只写了
            # `session`、那边读 `_session`，守卫恒返回空 dict、被当成「全部一致」——
            # 死代码一样静默通过，把 11-03 的价格写成了 40 行 11-04 的快照。
            # 两个键名统一成本低（一个字段），但**改错方向的代价不对称**：改
            # `_session_mismatch` 会动到 a_shares_snapshot 那条跑了很久的生产路径，
            # 所以选择在源头补齐、并在 fast_grab.write_snapshot 里再加一层 fail-closed。
            # 已有测试 lock 住这一行（tests/test_fast_grab.py）。
            "session": session,
            "_session": session,
            "trade_date": session[:8],
        }
    return out


def tencent_quote_bulk(codes, workers: int = TENCENT_WORKERS,
                       batch: int = TENCENT_BATCH,
                       timeout: float = TENCENT_TIMEOUT) -> dict:
    """批量取全市场报价快照，返回 {6位代码: 字段 dict}。

    参数
    ----
    codes : 可迭代[str]
        6 位代码（不带市场前缀）。会去重、去空。
    workers : int
        并发批数。实测 6 并发对腾讯没有触发限流；再多收益递减（121 批 6 并发 9.15s）。
    batch : int
        每批只数。腾讯单请求上限约 300，取 100 与既有实现一致，出问题好对账。
    timeout : float
        单请求超时秒。

    返回字段（**volume 已归一到「股」**，与 kline_daily.db / kbase.db 口径一致）：
        price / prev_close / open / high / low / volume / amount / pe_ttm / pb /
        mcap_yi / name / trade_date
      附加：pct / change / turnover / volume_raw / volume_unit / total_mv /
            float_mv / session / _session / code
      `_session` 是 `session` 的别名，专供 `fetch_a_shares_tencent._session_mismatch()`
      读取（那边约定的键名带下划线），两个值永远相同。

    ⚠️ 当日零成交的票**照样会出现在返回里**，但 `volume` 为 0、`price == prev_close`。
    这是源的真实回答（退市股会一直返回最后一天的价格），是否落库由调用方判定 ——
    2026-11-01 已经踩过「照落 → 给退市股伪造了一根今日 K 线」的坑。
    """
    clean = []
    seen = set()
    for c in codes or []:
        s = str(c or "").strip()
        if len(s) == 6 and s.isdigit() and s not in seen:
            seen.add(s)
            clean.append(s)
    if not clean:
        return {}
    batch = max(1, int(batch))
    workers = max(1, int(workers))
    chunks = [clean[i:i + batch] for i in range(0, len(clean), batch)]
    out = {}
    failed = []

    def _one(chunk):
        """一批 + 最多 3 次重试。**批内不 sleep** —— 时间全花在 sleep 上是旧实现的病根。"""
        last = None
        for attempt in range(1, TENCENT_RETRY + 1):
            try:
                return chunk, _fetch_tencent_batch(chunk, timeout=timeout), None
            except Exception as e:  # noqa: BLE001 —— 单批失败不该拖垮整轮
                last = e
                if attempt < TENCENT_RETRY:
                    time.sleep(0.5 * attempt)
        return chunk, {}, last

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for chunk, got, err in ex.map(_one, chunks):
            if err is not None:
                failed.append({"batch_head": chunk[0], "n": len(chunk),
                               "err": "%s: %s" % (type(err).__name__, str(err)[:120])})
            out.update(got)
    if failed:
        # 不 raise：整轮 5500 只里挂 1 批不该让结果全丢。但要留痕，调用方据此判断
        # 「这批票是真没行情，还是我们没抓到」—— 两者处理方式完全不同。
        out["__failed_batches__"] = failed  # type: ignore[assignment]
    return out


def strip_internal(quotes: dict) -> list:
    """去掉 `__failed_batches__` 这类内部键，返回纯 {code: 字段}。"""
    return {k: v for k, v in (quotes or {}).items() if not k.startswith("__")}


# ---------------------------------------------------------------- 通达信盘后包

TDX_URL = "https://www.tdx.com.cn/products/data/data/g4day/%s.zip"
TDX_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TDX_MIN_BYTES = 1_000_000     # 实测 20261102 是 2,759,837；小于 1MB 基本不是完整包
TDX_TIMEOUT = 60
# 一个市场「代码表条目数」低于下限 → 认定这个市场的包不完整，整轮判失败。
# 这是**结构性**完整性判据（.cod 里有多少只），实测 20261102：
#     sh 27,787 条 / sz 24,542 条 / bj 392 条
# 阈值取实测值的约 1/3，容得下正常增减，又拦得住「只解出几百条」的半截包。
# ⚠️ 宁可整轮失败也不返回部分市场：只写沪深不写北交所，用户看到的是
# 「北交所集体退市」而不是「北交所没抓到」，这种数据比没数据危险得多。
TDX_MARKET_MIN_ROWS = {"sh": 10000, "sz": 3000, "bj": 50}
# 有价记录（close>0 且 volume>0）的下限。和上面那个是两码事：
# .cod 里的条目大多是债券/基金/权证，包内行情区里它们常常是空的 ——
# 实测 20261102 有价记录只有 sh 5,095 / sz 5,022 / bj 356（条目数 27,787/24,542/392）。
# 一开始我拿条目数的阈值去卡有价记录数，结果 20261102 这个**完全正常**的包被判成
# 「有价记录过少」（sh=5095<10000）—— 阈值套错了对象，比没有阈值更糟。
TDX_MARKET_MIN_PRICED = {"sh": 3000, "sz": 3000, "bj": 50}

# .md1 记录布局（从 zip 实际字节反推，详见模块顶部说明）
_MD1_REC_SIZE = 512
_COD_REC_SIZE = 150
_MD1_OFF_PREV, _MD1_OFF_OHLC, _MD1_OFF_VOL, _MD1_OFF_AMT = 4, 12, 56, 72


def norm_date(date) -> str:
    """把 2026-11-02 / 20261102 / 2026/11/02 统一成 'YYYYMMDD'。"""
    s = str(date or "").strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) != 8:
        raise ValueError("日期格式不对，需要 YYYY-MM-DD 或 YYYYMMDD：%r" % (date,))
    return digits


def tdx_package_url(date) -> str:
    """盘后包地址。date 接受 'YYYY-MM-DD' 或 'YYYYMMDD'。"""
    return TDX_URL % norm_date(date)


def package_available(date, timeout: float = 15) -> tuple:
    """只用 HEAD 判「今天这个包发布了没有」，**不下载整个包**。

    返回 (ok: bool, detail: dict)。detail 里带 status / 字节数 / 原因，方便落进摘要。

    为什么用 HEAD：实测 HEAD 与 404 都是 0.17 秒，而完整包 9.3 秒 / 2.7MB。
    日更链里每轮都判一次，用 GET 就是每轮白烧 2.7MB 流量。

    为什么不能只判「200」：站点对未发布日返回 404（实测 20261103 → 404），
    但网络抖动/CDN 半截响应也可能给 200。所以额外要求 Content-Length ≥ 1MB。
    """
    url = tdx_package_url(date)
    req = urllib.request.Request(url, method="HEAD", headers=TDX_HEADERS)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            size = r.headers.get("Content-Length")
            try:
                n = int(size or 0)
            except (TypeError, ValueError):
                n = 0
            ok = (r.status == 200 and n >= TDX_MIN_BYTES)
            return ok, {"url": url, "status": r.status, "bytes": n,
                        "elapsed_sec": round(time.time() - t0, 2),
                        "reason": "" if ok else "响应正常但字节数 %d < %d" % (n, TDX_MIN_BYTES)}
    except urllib.error.HTTPError as e:
        return False, {"url": url, "status": e.code, "bytes": 0,
                       "elapsed_sec": round(time.time() - t0, 2),
                       "reason": "HTTP %d（当日包未发布）" % e.code}
    except Exception as e:  # noqa: BLE001
        return False, {"url": url, "status": None, "bytes": 0,
                       "elapsed_sec": round(time.time() - t0, 2),
                       "reason": "%s: %s" % (type(e).__name__, str(e)[:120])}


def download_package(date, timeout: float = TDX_TIMEOUT) -> bytes:
    """下载盘后包原始字节。404 抛 ValueError（这是「未发布」，不是故障）。"""
    url = tdx_package_url(date)
    req = urllib.request.Request(url, headers=TDX_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # 语义分级很重要：404 是「今天的还没出」（走降级），其它是「通道坏了」（要报警）。
            # 混成一个异常，调度侧就只能二选一：要么每天误报故障，要么真故障被当常态吞掉。
            raise ValueError("当日包未发布") from e
        raise RuntimeError("盘后包下载失败：HTTP %d" % e.code) from e


def _yymmdd(date8: str) -> str:
    """20261102 → 261102（zip 内文件名用的是两位年）。"""
    return date8[2:]


def _parse_cod(blob: bytes) -> list:
    """.cod 定长 150 字节/只 → 代码列表（保持原序，与 .md1 一一对应）。"""
    n = len(blob) // _COD_REC_SIZE
    codes = []
    for i in range(n):
        raw = blob[i * _COD_REC_SIZE:i * _COD_REC_SIZE + 6]
        codes.append(raw.decode("ascii", "ignore").strip())
    return codes


def _parse_md1(blob: bytes, codes: list) -> dict:
    """.md1 定长 512 字节/只 → {code: 当日一根}。

    只认「有价」的记录：close>0 且 volume>0。停牌/退市的记录在包里通常是全 0，
    收进来就等于给它们伪造一根「当日 0 元 K 线」。
    """
    n = min(len(blob) // _MD1_REC_SIZE, len(codes))
    out = {}
    for i in range(n):
        code = codes[i]
        if not (len(code) == 6 and code.isdigit()):
            continue
        base = i * _MD1_REC_SIZE
        prev_close, o, h, l, c = struct.unpack(
            "<5d", blob[base + _MD1_OFF_PREV:base + _MD1_OFF_PREV + 40])
        vol = struct.unpack("<i", blob[base + _MD1_OFF_VOL:base + _MD1_OFF_VOL + 4])[0]
        amt = struct.unpack("<d", blob[base + _MD1_OFF_AMT:base + _MD1_OFF_AMT + 8])[0]
        if not (c > 0) or vol <= 0:
            continue          # 停牌 / 退市整理结束 / 指数以外的空记录
        out[code] = {
            "code": code,
            "open": o,
            "close": c,
            "high": h,
            "low": l,
            "prev_close": prev_close,
            # 包里这两列单位是**实测**出来的，不是猜的：
            #   volume 是「股」（688981 解出 34,214,339，与库内新浪同日值完全相同）
            #   amount 是「元」（600519 解出 11,849,258,015，与 vol×均价 吻合到 0.4%）
            # 所以这里不做任何 ×100 换算 —— 老日线格式才需要 ÷100 得价格、÷100 得手数。
            "volume": float(vol),
            "amount": float(amt),
        }
    return out


def tdx_daily_package(date, blob: bytes = None) -> list:
    """解析某一交易日的通达信盘后包，返回**全市场当日一根**。

    返回 list[dict]，每项：
        code / open / close / high / low / prev_close / volume(股) / amount(元) / trade_date

    失败语义（调用方必须能区分，所以刻意用不同异常）：
        ValueError  —— 当日包尚未发布（404）。这是**正常状态**，走降级，不是故障。
        RuntimeError —— 包存在但不可用：不是 zip / 解压失败 / 缺某个市场 /
                        某市场有价记录低于下限。**绝不返回部分市场当全市场。**

    ⚠️ 只做「原始价」。前复权/后复权由 tools/fast_grab.py 负责，理由见那边的说明。
    """
    date8 = norm_date(date)
    if blob is None:
        blob = download_package(date8)
    if not blob:
        raise RuntimeError("盘后包为空（0 字节）")
    if not zipfile.is_zipfile(io.BytesIO(blob)):
        # 典型场景：站点返回了一个 HTML 错误页但状态码是 200。
        raise RuntimeError("盘后包不是 zip（前 16 字节：%r）" % blob[:16])
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
        names = zf.namelist()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("盘后包解压失败：%s: %s" % (type(e).__name__, e)) from e

    ymd = _yymmdd(date8)
    # 按「市场 + 日期」找配对，而不是假定文件名前缀长什么样。
    markets = {}
    for nm in names:
        low = nm.lower()
        if not low.endswith((".md1", ".cod")):
            continue
        stem = low.rsplit(".", 1)[0]
        if not stem.endswith(ymd):
            continue
        mkt = stem[:-len(ymd)]
        markets.setdefault(mkt, {})[low.rsplit(".", 1)[1]] = nm
    if not markets:
        raise RuntimeError("盘后包里没有 %s 的任何市场数据（包内文件：%s）"
                           % (ymd, ",".join(names)[:200]))
    need = set(TDX_MARKET_MIN_ROWS)
    missing = [m for m in need if m not in markets]
    if missing:
        # 缺市场的包不能用：补出来的「全市场」会让北交所 350 只凭空消失。
        raise RuntimeError("盘后包缺市场 %s（包内：%s）"
                           % (",".join(sorted(missing)), ",".join(names)[:200]))

    rows = []
    stats = {}
    for mkt, files in sorted(markets.items()):
        if "md1" not in files or "cod" not in files:
            raise RuntimeError("市场 %s 的 .md1/.cod 不成对（%s）" % (mkt, files))
        try:
            cod_blob = zf.read(files["cod"])
            md_blob = zf.read(files["md1"])
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("市场 %s 读取失败：%s: %s" % (mkt, type(e).__name__, e)) from e
        codes = _parse_cod(cod_blob)
        if len(md_blob) // _MD1_REC_SIZE != len(codes):
            # 条数不齐 = 代码表与行情表对不上，此时按位取值会**整体错位**
            # （把 A 的收盘价写成 B 的），而且不会报错。必须硬失败。
            raise RuntimeError(
                "市场 %s 代码表与行情表条数不一致：cod=%d 条 / md1=%d 条（按位取会整体错位）"
                % (mkt, len(codes), len(md_blob) // _MD1_REC_SIZE))
        got = _parse_md1(md_blob, codes)
        for r in got.values():
            # 市场必须跟着行走：同一个 6 位代码在不同市场代表**完全不同的东西**。
            # 沪深都有 000001 —— 沪市那个是上证指数（999999 之外的另一个指数代码），
            # 深市那个才是平安银行。不带上市场，后面对账就会拿 3638.03 去比 11.54。
            r["market"] = mkt
        stats[mkt] = {"cod": len(codes), "priced": len(got)}
        rows.extend(got.values())

    thin = [m for m, need_n in TDX_MARKET_MIN_ROWS.items()
            if stats.get(m, {}).get("cod", 0) < need_n]
    if thin:
        raise RuntimeError(
            "盘后包代码表条目过少，拒绝当全市场使用：%s（阈值 %s）"
            % ("; ".join("%s=%d<%d" % (m, stats.get(m, {}).get("cod", 0),
                                       TDX_MARKET_MIN_ROWS[m]) for m in thin),
               TDX_MARKET_MIN_ROWS))
    low_priced = [m for m, need_n in TDX_MARKET_MIN_PRICED.items()
                  if stats.get(m, {}).get("priced", 0) < need_n]
    if low_priced:
        raise RuntimeError(
            "盘后包有价记录过少，拒绝当全市场使用：%s（阈值 %s；实测正常日 %s）"
            % ("; ".join("%s=%d<%d" % (m, stats.get(m, {}).get("priced", 0),
                                       TDX_MARKET_MIN_PRICED[m]) for m in low_priced),
               TDX_MARKET_MIN_PRICED, {"sh": 5095, "sz": 5022, "bj": 356}))

    rows = _dedupe_by_market(rows)
    for r in rows:
        r["trade_date"] = date8
    if not rows:
        raise RuntimeError("盘后包解析出 0 条有价记录（%s）" % stats)
    return rows


def _dedupe_by_market(rows) -> list:
    """跨市场撞号去重：只保留「交易所归属与所在市场一致」的那一条。

    为什么必须做：实测 20261102 三个市场合计 10,473 条有价记录，但其中 000001 这类
    代码在沪（指数）和深（平安银行）各出现一次。不去重的话：
      · 对上证指数会拿 3638.03 去比平安银行的 11.54，看起来像「大面积单位错」；
      · 还会凭空多出 1,242 条「库里没有的票」，把「写了几行」这个数字弄脏。
    判据用 `security_master.exchange_of()`（全项目唯一允许写代码段规则的地方），
    不写「sh 的 00 开头是指数」这种特例表。
    """
    from viewer import security_master as SM
    want = {SM.EXCHANGE_SSE: "sh", SM.EXCHANGE_SZSE: "sz", SM.EXCHANGE_BSE: "bj"}
    keep = {}
    for r in rows or []:
        code = r.get("code") or ""
        mkt = want.get(SM.exchange_of(code))
        # mkt 为 None = 这个代码不属于任何 A 股交易所（指数 999999 / 深证 399xxx /
        # 沪 B 股 900xxx …），直接丢；mkt != r["market"] = 撞号，丢错market的那一条。
        if mkt and mkt == r.get("market"):
            keep[code] = r
    return list(keep.values())


def package_stats(rows) -> dict:
    """按市场给一份计数（摘要/对账用）。市场归属同样委托 security_master。"""
    from viewer import security_master as SM
    out = {"total": 0, "sh": 0, "sz": 0, "bj": 0, "other": 0, "stock": 0, "non_stock": 0}
    for r in rows or []:
        out["total"] += 1
        code = r.get("code") or ""
        ex = SM.exchange_of(code)
        key = {SM.EXCHANGE_SSE: "sh", SM.EXCHANGE_SZSE: "sz", SM.EXCHANGE_BSE: "bj"}.get(ex)
        out[key or "other"] = out.get(key or "other", 0) + 1
        if U.stock_kind(code) == U.KIND_STOCK:
            out["stock"] += 1
        else:
            out["non_stock"] += 1
    return out


def only_stocks(rows) -> list:
    """只留 A 股。包里有指数（999999 上证指数）、债券、基金、B 股，混进来会污染全市场统计。

    判据只认 viewer.units.stock_kind —— 2026-11-01 那次「可转债/ETF 被抓成股票落库」
    就是因为各处自己写前缀判断。判不出（unknown）的**丢掉**，宁可少算不误算。
    """
    return [r for r in rows or [] if U.stock_kind(r.get("code") or "") == U.KIND_STOCK]
