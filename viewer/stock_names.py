"""股票代码 → 股票名称 / 公司全称 解析器。

为什么需要它：
    论坛正文里通常只出现 6 位股票代码（如 600519），解析器只提取到代码，
    若不补全名称，界面就只剩一串数字，用户无法辨认。本模块负责给代码配上
    「股票简称」（贵州茅台）与「公司全称」（贵州茅台酒股份有限公司）。

解析优先级（前者命中即返回）：
    1) config/stock_names.yaml    —— 用户可手工维护/纠正（最高优先级）
    2) 数据库 stock_names 缓存表  —— 联网补全结果落库，离线可复用
    3) 代码内置 BUILTIN 常用表    —— 离线兜底，覆盖高频标的
    4) 数据库 trades/positions 反查 —— 抓取时帖子里已带名称的情况
    5) 联网查询（东方财富）        —— 可选，需网络；结果写入缓存表

纯标准库 + requests（已在 venv 内），不引入新依赖。
命令行用法：
    python -m viewer.stock_names --sync 600519 002594     # 联网补全指定代码并落库
    python -m viewer.stock_names --sync-all               # 补全库中所有代码
    python -m viewer.stock_names --show 600519            # 查看解析结果
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONFIG_PATH = ROOT / "config" / "stock_names.yaml"

CODE_RE = re.compile(r"^(60|68|00|30|43|83|87|92)\d{4}$")

# 市场板块（中国内地交易所）
BOARDS = [
    ("60", "沪市主板"), ("68", "科创板"), ("00", "深市主板"),
    ("30", "创业板"), ("43", "北交所"), ("83", "北交所"), ("87", "北交所"), ("92", "北交所"),
]

# 内置常用 A 股映射：代码 -> (股票简称, 公司全称)。缺失的由联网/缓存补全。
BUILTIN: dict[str, tuple[str, str]] = {
    "600519": ("贵州茅台", "贵州茅台酒股份有限公司"),
    "000858": ("五粮液", "宜宾五粮液股份有限公司"),
    "000568": ("泸州老窖", "泸州老窖股份有限公司"),
    "600809": ("山西汾酒", "山西杏花村汾酒厂股份有限公司"),
    "002304": ("洋河股份", "江苏洋河酒厂股份有限公司"),
    "002594": ("比亚迪", "比亚迪股份有限公司"),
    "300750": ("宁德时代", "宁德时代新能源科技股份有限公司"),
    "002460": ("赣锋锂业", "江西赣锋锂业集团股份有限公司"),
    "601127": ("赛力斯", "赛力斯集团股份有限公司"),
    "601633": ("长城汽车", "长城汽车股份有限公司"),
    "600104": ("上汽集团", "上海汽车集团股份有限公司"),
    "000625": ("长安汽车", "重庆长安汽车股份有限公司"),
    "601318": ("中国平安", "中国平安保险(集团)股份有限公司"),
    "601601": ("中国太保", "中国太平洋保险(集团)股份有限公司"),
    "601628": ("中国人寿", "中国人寿保险股份有限公司"),
    "600036": ("招商银行", "招商银行股份有限公司"),
    "000001": ("平安银行", "平安银行股份有限公司"),
    "601398": ("工商银行", "中国工商银行股份有限公司"),
    "601939": ("建设银行", "中国建设银行股份有限公司"),
    "601288": ("农业银行", "中国农业银行股份有限公司"),
    "601988": ("中国银行", "中国银行股份有限公司"),
    "600000": ("浦发银行", "上海浦东发展银行股份有限公司"),
    "601166": ("兴业银行", "兴业银行股份有限公司"),
    "600016": ("民生银行", "中国民生银行股份有限公司"),
    "600030": ("中信证券", "中信证券股份有限公司"),
    "300059": ("东方财富", "东方财富信息股份有限公司"),
    "000333": ("美的集团", "美的集团股份有限公司"),
    "000651": ("格力电器", "珠海格力电器股份有限公司"),
    "600690": ("海尔智家", "海尔智家股份有限公司"),
    "000100": ("TCL科技", "TCL科技集团股份有限公司"),
    "000725": ("京东方A", "京东方科技集团股份有限公司"),
    "002415": ("海康威视", "杭州海康威视数字技术股份有限公司"),
    "002475": ("立讯精密", "立讯精密工业股份有限公司"),
    "002230": ("科大讯飞", "科大讯飞股份有限公司"),
    "000063": ("中兴通讯", "中兴通讯股份有限公司"),
    "603501": ("韦尔股份", "上海韦尔半导体股份有限公司"),
    "688981": ("中芯国际", "中芯国际集成电路制造有限公司"),
    "688041": ("海光信息", "海光信息技术股份有限公司"),
    "688111": ("金山办公", "北京金山办公软件股份有限公司"),
    "603986": ("兆易创新", "北京兆易创新科技股份有限公司"),
    "300308": ("中际旭创", "中际旭创股份有限公司"),
    "300124": ("汇川技术", "深圳市汇川技术股份有限公司"),
    "002050": ("三花智控", "浙江三花智能控制股份有限公司"),
    "603259": ("药明康德", "无锡药明康德新药开发股份有限公司"),
    "600276": ("恒瑞医药", "江苏恒瑞医药股份有限公司"),
    "300760": ("迈瑞医疗", "深圳迈瑞生物医疗电子股份有限公司"),
    "300015": ("爱尔眼科", "爱尔眼科医院集团股份有限公司"),
    "300347": ("泰格医药", "杭州泰格医药科技股份有限公司"),
    "000538": ("云南白药", "云南白药集团股份有限公司"),
    "600436": ("片仔癀", "漳州片仔癀药业股份有限公司"),
    "601888": ("中国中免", "中国旅游集团中免股份有限公司"),
    "600887": ("伊利股份", "内蒙古伊利实业集团股份有限公司"),
    "603288": ("海天味业", "佛山市海天调味食品股份有限公司"),
    "601012": ("隆基绿能", "隆基绿能科技股份有限公司"),
    "600438": ("通威股份", "通威股份有限公司"),
    "600585": ("海螺水泥", "安徽海螺水泥股份有限公司"),
    "600309": ("万华化学", "万华化学集团股份有限公司"),
    "601899": ("紫金矿业", "紫金矿业集团股份有限公司"),
    "601088": ("中国神华", "中国神华能源股份有限公司"),
    "600900": ("长江电力", "中国长江电力股份有限公司"),
    "601857": ("中国石油", "中国石油天然气股份有限公司"),
    "600028": ("中国石化", "中国石油化工股份有限公司"),
    "600031": ("三一重工", "三一重工股份有限公司"),
    "601668": ("中国建筑", "中国建筑股份有限公司"),
    "601390": ("中国中铁", "中国中铁股份有限公司"),
    "601186": ("中国铁建", "中国铁建股份有限公司"),
    "601669": ("中国电建", "中国电力建设股份有限公司"),
    "601600": ("中国铝业", "中国铝业股份有限公司"),
    "600050": ("中国联通", "中国联合网络通信股份有限公司"),
    "601728": ("中国电信", "中国电信股份有限公司"),
    "600941": ("中国移动", "中国移动有限公司"),
    "002714": ("牧原股份", "牧原食品股份有限公司"),
    "002352": ("顺丰控股", "顺丰控股股份有限公司"),
    "601111": ("中国国航", "中国国际航空股份有限公司"),
    "600029": ("南方航空", "中国南方航空股份有限公司"),
    "600009": ("上海机场", "上海国际机场股份有限公司"),
    "002027": ("分众传媒", "分众传媒信息技术股份有限公司"),
    "000002": ("万科A", "万科企业股份有限公司"),
    "601138": ("工业富联", "富士康工业互联网股份有限公司"),
    "600760": ("中航沈飞", "中航沈飞股份有限公司"),
    "002179": ("中航光电", "中航光电科技股份有限公司"),
    "600893": ("航发动力", "中国航发动力股份有限公司"),
    "600150": ("中国船舶", "中国船舶工业股份有限公司"),
}


def board_of(code: str) -> str:
    """按代码前缀判断上市板块。"""
    code = (code or "").strip()
    for prefix, name in BOARDS:
        if code.startswith(prefix):
            return name
    return "未知板块"


def normalize(code: str) -> str:
    """从任意文本中提取 6 位 A 股代码；失败返回原值。

    2026-10-12 修复：原正则 `\\b(\\d{6})\\b` 在 Python 里把 CJK 也视作 \\w，
    于是「宁德时代300750」中 代|3 之间没有词边界，代码提不出来（淘股吧正文大量这种写法）。
    改用数字环视（前后不得再紧跟数字）即可正确取出，同时仍能拒绝 7 位以上的长数字串。
    """
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", code or "")
    return m.group(1) if m else (code or "").strip()


# ---------------- 名字 → 代码 反查（2026-09-29） ----------------
# 背景：淘股吧等源帖只写股票名不写代码（"昨日买入：吉鑫科技"），解析器 symbol 恒为空，
# 导致详情/K线/行情全链路查不到。而代码其实就在本地库里（stock_names + 龙虎榜证券简称），
# 只是此前从未做过"名字→代码"方向的反查。


def _known_name_index(db=None) -> dict[str, str]:
    """名字 → 代码 索引：内置表 + stock_names 缓存表 + 龙虎榜证券简称（交易所口径，最权威）。"""
    idx: dict[str, str] = {}
    for code, (nm, _co) in BUILTIN.items():
        if nm:
            idx.setdefault(nm.strip(), code)
    if db is not None:
        queries = (
            "SELECT code, name FROM stock_names WHERE name IS NOT NULL AND name<>''",
            "SELECT DISTINCT security_code AS code, security_name AS name FROM lhb_stocks "
            "WHERE security_name IS NOT NULL AND security_name<>''",
            "SELECT DISTINCT security_code AS code, security_name AS name FROM lhb_inst "
            "WHERE security_name IS NOT NULL AND security_name<>''",
        )
        for q in queries:
            try:
                for r in db.conn.execute(q).fetchall():
                    nm = (r["name"] or "").strip()
                    if nm:
                        idx.setdefault(nm, r["code"])
            except Exception:  # noqa: BLE001 老库缺表时静默跳过
                continue
    return idx


def name_to_code(name: str, db=None, allow_online: bool = False,
                 timeout: float = 6.0) -> dict | None:
    """股票名 → 代码 反查。命中返回 {symbol, name, match}，未命中返回 None（不编造）。

    口径（依次尝试）：
      1) 本地精确匹配：内置表 + stock_names 缓存表 + 龙虎榜证券简称；
      2) 黏连后缀：正文抓出的词常带 1~2 字垃圾尾巴（"三羊马走"→三羊马、"金健米业打"→金健米业），
         去尾后精确命中即认；
      3) 唯一前缀：抓短了（"平潭"→平潭发展），仅当有且只有一个已知名以它开头才认（防歧义）；
      4) allow_online=True 时东财搜索接口兜底，命中后写回 stock_names 缓存表，下次离线可用。
    """
    nm = (name or "").strip()
    if not nm or len(nm) < 2:
        return None
    idx = _known_name_index(db)
    if nm in idx:
        return {"symbol": idx[nm], "name": nm, "match": "exact"}
    for ln in (len(nm) - 1, len(nm) - 2):
        if ln >= 2 and nm[:ln] in idx:
            return {"symbol": idx[nm[:ln]], "name": nm[:ln], "match": "suffix"}
    cands = {c for n, c in idx.items() if n.startswith(nm) and len(n) > len(nm)}
    if len(cands) == 1:
        return {"symbol": next(iter(cands)), "name": nm, "match": "prefix"}
    if not allow_online:
        return None
    try:
        import requests
        r = requests.get(
            "https://searchapi.eastmoney.com/api/suggest/get",
            params={"input": nm, "type": "14", "count": "8",
                    "token": "D43BF722C8E33BDC906FB84D85E326E8"},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout,
        )
        plist = (((r.json() or {}).get("Data") or {}).get("PromptList")) or []
        hits = [p for p in plist
                if (p.get("Name") or "").strip() == nm
                and str(p.get("QuotationCode") or "").split(".")[-1].isdigit()]
        if hits:
            code = hits[0]["QuotationCode"].split(".")[-1]
            try:  # 命中即落缓存表，离线可复用
                if db is not None:
                    db.conn.execute(
                        "INSERT OR REPLACE INTO stock_names (code, name, company, market, source, updated_at) "
                        "VALUES (?,?,?,?,?,datetime('now','localtime'))",
                        (code, nm, "", board_of(code), "suggest"))
                    db.conn.commit()
            except Exception:  # noqa: BLE001 缓存写失败不影响结果
                pass
            return {"symbol": code, "name": nm, "match": "online"}
    except Exception:  # noqa: BLE001 网络失败静默，保持有名无码
        pass
    return None


def sync_all_stock_names(db, timeout: float = 15.0) -> int:
    """全量同步 A 股 代码↔名称 到 stock_names 缓存表（新浪 hs_a 列表，分页拉全）。

    背景：suggest 搜索接口已失效（2026-09-29 实测返回空）；push2 clist 又在批量拉取中途
    触发东财限流拒连（只到手零头）。改用新浪 Market_Center.getHQNodeData（项目板块资金流
    同源，实测稳定、代理/直连均通）：全 A（含北交所）逐页拉取。
    INSERT OR IGNORE：不覆盖既有行（保护 config/eastmoney 来源里带公司全称的记录）。
    返回本次新增行数；彻底失败返回 0（调用方决定是否容忍）。
    """
    try:
        import requests
        import time
    except Exception:  # noqa: BLE001
        return 0
    added = 0
    page, empty_streak = 1, 0
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0",
                         "Referer": "https://finance.sina.com.cn"})
    while page <= 90:  # ~5400 只 / 每页 80 ≈ 68 页；90 页硬上限防死循环
        rows = None
        for attempt in range(3):  # 偶发断连：代理失败换直连重试
            try:
                sess.trust_env = attempt < 2
                r = sess.get(
                    "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                    "Market_Center.getHQNodeData",
                    params={"page": page, "num": 80, "sort": "symbol", "asc": 1,
                            "node": "hs_a", "symbol": "", "_s_r_a": "page"},
                    timeout=timeout,
                )
                rows = r.json()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5 * (attempt + 1))
                if attempt == 2:
                    rows = None
        if not rows:
            empty_streak += 1
            if empty_streak >= 2:  # 连续两页空视为拉完/被限流，收手
                break
            page += 1
            continue
        empty_streak = 0
        for it in rows:
            code = str(it.get("code") or "").strip()
            nm = str(it.get("name") or "").strip()
            if not code or not nm or not code.isdigit() or len(code) != 6:
                continue
            cur = db.conn.execute(
                "SELECT 1 FROM stock_names WHERE code=?", (code,)).fetchone()
            if cur:
                continue
            db.conn.execute(
                "INSERT OR IGNORE INTO stock_names (code, name, company, market, source, updated_at) "
                "VALUES (?,?,?,?,?,datetime('now','localtime'))",
                (code, nm, "", board_of(code), "sina"))
            added += 1
        db.conn.commit()
        page += 1
        time.sleep(0.12)  # 温和限速，别把新浪也惹毛了
    return added


class Resolver:
    """代码 → 名称解析器（带内存 + 数据库双层缓存）。"""

    def __init__(self, db=None, online: bool = True, timeout: float = 6.0):
        self.db = db
        self.online = online
        self.timeout = timeout
        self._mem: dict[str, dict] = {}
        self._yaml: dict[str, tuple[str, str]] | None = None

    # ---------- 数据源 ----------
    def _load_yaml(self) -> dict[str, tuple[str, str]]:
        if self._yaml is not None:
            return self._yaml
        out: dict[str, tuple[str, str]] = {}
        if CONFIG_PATH.exists():
            try:
                import yaml  # 已在依赖内；缺失则跳过
                raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
                for k, v in (raw.get("names") or {}).items():
                    code = normalize(str(k))
                    if isinstance(v, (list, tuple)) and v:
                        out[code] = (str(v[0] or ""), str(v[1] if len(v) > 1 else ""))
                    elif isinstance(v, str):
                        out[code] = (v, "")
            except Exception:  # noqa: BLE001 配置文件损坏不应影响主流程
                pass
        self._yaml = out
        return out

    def _from_cache_table(self, code: str) -> dict | None:
        if not self.db:
            return None
        try:
            row = self.db.conn.execute(
                "SELECT code, name, company, market, source FROM stock_names WHERE code=?", (code,)
            ).fetchone()
        except Exception:  # noqa: BLE001 老库无此表时静默跳过
            return None
        if not row or not row["name"]:
            return None
        return {"symbol": row["code"], "name": row["name"], "company": row["company"] or "",
                "market": row["market"] or board_of(row["code"]), "source": row["source"] or "cache"}

    def _from_records(self, code: str) -> dict | None:
        """从**本地已入库的记录**反查名称（这些名字早就躺在库里，不必联网）。

        2026-09-26 修正：原先只查 positions / trades（帖子类记录），而这两张表里带名称的
        代码极少（实测仅十来个），于是详情面板对约 1/3 的代码显示"名称待补全"——
        可是同一批代码在 lhb_stocks / lhb_inst 里**每一条都带 security_name**
        （交易所口径的证券简称，比帖子里的写法更权威），却从没被用上。
        这里按可信度排序：交易所披露 → 帖子记录。
        """
        if not self.db:
            return None
        sources = (
            ("lhb_stocks", "security_code", "security_name"),
            ("lhb_inst", "security_code", "security_name"),
            ("positions", "symbol", "symbol_name"),
            ("trades", "symbol", "symbol_name"),
        )
        for table, code_col, name_col in sources:
            try:
                row = self.db.conn.execute(
                    f"SELECT {name_col} AS nm FROM {table} WHERE {code_col}=? "
                    f"AND {name_col} IS NOT NULL AND {name_col}<>'' LIMIT 1",
                    (code,),
                ).fetchone()
            except Exception:  # noqa: BLE001 老库缺表时静默跳过
                continue
            if row and row["nm"]:
                return {"symbol": code, "name": row["nm"], "company": "",
                        "market": board_of(code),
                        "source": "lhb" if table.startswith("lhb") else "db"}
        return None

    # ---------- 联网 ----------
    def fetch_online(self, code: str) -> dict | None:
        """东方财富：先取股票简称，再取公司全称。失败返回 None（不抛异常）。"""
        if not self.online or not CODE_RE.match(code):
            return None
        try:
            import requests
        except Exception:  # noqa: BLE001
            return None
        secid = f"{'1' if code.startswith(('6', '9')) else '0'}.{code}"
        name = company = ""
        try:
            r = requests.get(
                "https://push2.eastmoney.com/api/qt/stock/get",
                params={"secid": secid, "fields": "f57,f58"},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=self.timeout,
            )
            d = (r.json() or {}).get("data") or {}
            name = (d.get("f58") or "").strip()
        except Exception:  # noqa: BLE001
            pass
        try:
            r = requests.get(
                "https://datacenter-web.eastmoney.com/api/data/v1/get",
                params={
                    "reportName": "RPT_F10_BASIC_ORGINFO",
                    "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,ORG_NAME",
                    "filter": f'(SECURITY_CODE="{code}")',
                    "pageSize": 1, "source": "WEB", "client": "WEB",
                },
                headers={"User-Agent": "Mozilla/5.0"}, timeout=self.timeout,
            )
            rows = ((r.json() or {}).get("result") or {}).get("data") or []
            if rows:
                name = name or (rows[0].get("SECURITY_NAME_ABBR") or "").strip()
                company = (rows[0].get("ORG_NAME") or "").strip()
        except Exception:  # noqa: BLE001
            pass
        if not name:
            return None
        return {"symbol": code, "name": name, "company": company,
                "market": board_of(code), "source": "eastmoney"}

    # ---------- 对外 ----------
    def resolve(self, code: str, allow_online: bool = True) -> dict:
        raw = (code or "").strip()
        c = normalize(raw)
        if c in self._mem:
            return self._mem[c]
        info = {"symbol": c, "name": "", "company": "", "market": board_of(c), "source": "none"}
        y = self._load_yaml().get(c)
        if y and y[0]:
            info.update(name=y[0], company=y[1], source="config")
        else:
            cache = self._from_cache_table(c) or self._from_records(c)
            if cache:
                info.update(name=cache["name"], company=cache.get("company", ""), source=cache["source"])
            elif c in BUILTIN:
                n, comp = BUILTIN[c]
                info.update(name=n, company=comp, source="builtin")
            elif allow_online:
                on = self.fetch_online(c)
                if on:
                    info.update(name=on["name"], company=on["company"], source="eastmoney")
                else:
                    info["source"] = "missing"
        self._mem[c] = info
        return info

    def resolve_many(self, codes: list[str], allow_online: bool = False) -> dict[str, dict]:
        """批量解析。默认先走离线（内置表/缓存/库内反查），缺名才联网，避免批量请求拖慢接口。"""
        out: dict[str, dict] = {}
        missing: list[str] = []
        for c in codes:
            key = normalize(c)
            if not key:
                continue
            info = self.resolve(key, allow_online=False)
            out[key] = info
            if not info["name"]:
                missing.append(key)
        if allow_online and missing:
            for key in missing:
                on = self.fetch_online(key)
                if on:
                    out[key] = on
                    self._mem[key] = on
        return out

    def persist(self, infos: list[dict]) -> int:
        """把解析结果写入 stock_names 缓存表，并回填 stock_mentions/trades/positions 的名称列。"""
        if not self.db:
            return 0
        n = 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for i in infos:
            if not (i.get("symbol") and i.get("name")):
                continue
            try:
                self.db.conn.execute(
                    "INSERT OR REPLACE INTO stock_names (code, name, company, market, source, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (i["symbol"], i["name"], i.get("company", ""), i.get("market", ""), i.get("source", ""), now),
                )
                for table in ("stock_mentions", "trades", "positions"):
                    self.db.conn.execute(
                        f"UPDATE {table} SET symbol_name=? WHERE symbol=? AND (symbol_name IS NULL OR symbol_name='')",
                        (i["name"], i["symbol"]),
                    )
                n += 1
            except Exception:  # noqa: BLE001
                continue
        try:
            self.db.conn.commit()
        except Exception:  # noqa: BLE001
            pass
        return n

    def backfill_missing(self, codes: list[str] | None = None, online: bool = True) -> dict:
        """只补「还查不到名字」的代码，并把结果落库 + 回填历史记录。
        抓取入库后调用一次，即可让新出现的代码自动带上名称（已识别的代码不重复联网）。"""
        codes = codes if codes is not None else (self.db.all_symbols() if self.db else [])
        infos: list[dict] = []
        fetched = 0
        for c in codes:
            key = normalize(c)
            if not key:
                continue
            info = self.resolve(key, allow_online=False)
            if not info["name"] and online:
                on = self.fetch_online(key)
                if on:
                    info = on
                    self._mem[key] = on
                    fetched += 1
            if info.get("name"):
                infos.append(info)
        return {"requested": len(codes), "resolved": len(infos), "fetched": fetched,
                "persisted": self.persist(infos), "items": infos}

    def sync(self, codes: list[str], online: bool = True) -> dict:
        """强制刷新指定代码（即使离线表已有名字也重新联网核对）。

        2026-09-26 修正：原实现是「联网拿不到就退回 yaml/builtin」，**不查库内反查**——
        在没网/VPN 不通时，会把龙虎榜里现成的简称一起丢掉（写回缓存时被覆盖为空）。
        改为：离线（config → 缓存表 → 库内反查 → 内置表）先兜一层，联网结果再覆盖。
        """
        infos = []
        for c in codes:
            key = normalize(c)
            if not key:
                continue
            y = self._load_yaml().get(key)
            if y and y[0]:
                info = {"symbol": key, "name": y[0], "company": y[1],
                        "market": board_of(key), "source": "config"}
            else:
                info = self._from_cache_table(key) or self._from_records(key)
                if not info and key in BUILTIN:
                    n, comp = BUILTIN[key]
                    info = {"symbol": key, "name": n, "company": comp,
                            "market": board_of(key), "source": "builtin"}
            if online:
                on = self.fetch_online(key)
                if on:
                    # 联网结果优先（通常带公司全称），但别把离线已有的全称丢掉
                    if info and info.get("company") and not on.get("company"):
                        on["company"] = info["company"]
                    info = on
            if info:
                self._mem[key] = info
                infos.append(info)
        return {"resolved": len(infos), "persisted": self.persist(infos), "items": infos}


# ---------- 全局便捷入口（接口层调用） ----------
_RESOLVER: Resolver | None = None


def get_resolver(db=None, online: bool = True) -> Resolver:
    global _RESOLVER
    if _RESOLVER is None:
        _RESOLVER = Resolver(db=db, online=online)
    elif db is not None and _RESOLVER.db is None:
        _RESOLVER.db = db
    return _RESOLVER


def enrich(symbols: list[str], db=None, online: bool = False) -> dict[str, dict]:
    return get_resolver(db=db, online=online).resolve_many(symbols, allow_online=online)


def main():
    ap = argparse.ArgumentParser(description="股票代码 → 名称 解析/补全工具")
    ap.add_argument("--sync", nargs="*", metavar="CODE", help="联网补全指定代码并落库")
    ap.add_argument("--sync-all", action="store_true", help="补全数据库中出现过的全部代码")
    ap.add_argument("--show", metavar="CODE", help="查看单个代码的解析结果")
    ap.add_argument("--offline", action="store_true", help="只做离线解析，不联网")
    args = ap.parse_args()

    from viewer.db import Database
    db = Database()
    res = Resolver(db=db, online=not args.offline)

    if args.show:
        info = res.resolve(args.show, allow_online=not args.offline)
        print(f"{info['symbol']}  简称={info['name'] or '（未知）'}  板块={info['market']}  "
              f"来源={info['source']}\n  公司全称：{info['company'] or '（未知）'}")
        return

    codes: list[str] = list(args.sync or [])
    if args.sync_all or (not args.sync and not args.show):
        # 2026-09-26 修正：代码来源原先只取 stock_mentions / trades / positions，
        # 而界面上真正大量「名称待补全」的代码来自龙虎榜与股东披露（lhb_stocks /
        # lhb_inst / stock_holders / stock_holder_num）。少这几张表，
        # 就等于"补全"跑完仍有大批代码没名字。这里一并纳入。
        rows = db.conn.execute(
            "SELECT DISTINCT symbol AS s FROM stock_mentions WHERE symbol IS NOT NULL "
            "UNION SELECT DISTINCT symbol FROM trades WHERE symbol IS NOT NULL "
            "UNION SELECT DISTINCT symbol FROM positions WHERE symbol IS NOT NULL "
            "UNION SELECT DISTINCT security_code FROM lhb_stocks WHERE security_code IS NOT NULL "
            "UNION SELECT DISTINCT security_code FROM lhb_inst WHERE security_code IS NOT NULL "
            "UNION SELECT DISTINCT symbol FROM stock_holders WHERE symbol IS NOT NULL "
            "UNION SELECT DISTINCT symbol FROM stock_holder_num WHERE symbol IS NOT NULL"
        ).fetchall()
        codes = sorted({r["s"] for r in rows if r["s"]})
    if not codes:
        print("库中没有可补全的股票代码。")
        return

    r = res.sync(codes, online=not args.offline)
    print(f"共 {len(codes)} 个代码：解析成功 {r['resolved']}，写入缓存 {r['persisted']}。")
    for i in r["items"]:
        print(f"  {i['symbol']}  {i['name']}  |  {i['company'] or '—'}  |  {i['market']}  ({i['source']})")


if __name__ == "__main__":
    main()
