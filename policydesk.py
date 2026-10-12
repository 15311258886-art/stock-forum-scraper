# -*- coding: utf-8 -*-
"""政策风向（国家级政策desk）——替代旧「全市场快讯」的研判型政策面板。

数据口径（王总 2026-09-30 需求）：
  ① 主源 = 中国政府网政策文件库 JSON 接口（sousuo.www.gov.cn/search-gov/data）：
     - t=zhengcelibrary_gw  国务院文件 / 国务院办公厅文件（国家级，最权威）
     - t=zhengcelibrary_bm  部委文件（央行/发改委/工信部/财政部等联合印发，含全文摘要）
  ② 辅源 = 金十快讯 flash_newest.js + 东财 7×24 快讯（emapi.fast_news）：
     **只保留命中「中央级机构/货币政策」关键词的条目**，省市地方新闻默认过滤
     （如"天津市xxx"这类，正是王总点名的问题方向）。
  ③ 每条做板块影响映射（规则表 _SECTOR_RULES，机器初判）：关键词 → 影响板块 + 传导逻辑
     （例：降准降息/利率调整 → 银行（信贷扩容）+ 房地产（融资成本下降）+ 券商）。
  ④ 落库 policy_news 表积累历史（为后续"试点实施结果是否正向"跟踪打底）。
     隔离红线：该表零 JOIN、零外键，不与任何既有业务表混用。
诚实口径：机器关键词初判，非语义研判；代表股=板块资金流快照按总市值取的龙头，仅示意方向。
"""
from __future__ import annotations

import hashlib
import json
import re
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

REFRESH_TTL = 600          # 进程内刷新冷却（秒）
# 2026-10-05：25 → 60。只取最近 25 条时，政策库基本只覆盖几天，"压制/约束"类政策
# 一条都进不来（实测全库 111 条里标题级压制条款仅 1 条），利空面等于没有数据。
# 放宽到 60 后回溯窗口明显拉长，压制型政策才可能进入扫描范围（王总要求：利空也要看得见）。
GOV_PAGE_SIZE = 60         # gov.cn 每类拉取条数
FLASH_KEEP = 60            # 快讯筛选后最多保留条数

# ---------------------------------------------------------------- 基础设施

import netctx  # noqa: E402  —— 统一出网 TLS 口径（整改 #27）
_SSL_CTX = netctx.https_context("finance.eastmoney.com")  # 整改 #27：默认不校验，按环境变量可开启
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _http_text(url: str, referer: str = "", timeout: int = 10) -> str:
    headers = {"User-Agent": _UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    raw = urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX).read()
    return raw.decode("utf-8", "ignore")


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    return _TAG_RE.sub("", s or "").replace("&nbsp;", " ").strip()


# ---------------------------------------------------------------- 机构识别

# 中央级机构关键词（快讯筛选准入 + 发文机关展示名）。顺序即展示优先级。
_CENTRAL_ORGS = [
    (r"中央政治局|中共中央|中央经济工作会议|中央财经委员会|中央深改", "中共中央"),
    (r"国务院办公厅|国办", "国务院办公厅"),
    (r"国务院|国常会", "国务院"),
    (r"人民银行|央行", "中国人民银行"),
    (r"金融监管总局|国家金融监督管理", "金融监管总局"),
    (r"证监会", "证监会"),
    (r"发改委|发展改革委", "国家发展改革委"),
    (r"财政部", "财政部"),
    (r"工信部|工业和信息化部", "工业和信息化部"),
    (r"商务部", "商务部"),
    (r"住建部|住房和城乡建设部", "住房和城乡建设部"),
    (r"交通运输部|交通部", "交通运输部"),
    (r"农业农村部", "农业农村部"),
    (r"卫健委|卫生健康委", "国家卫健委"),
    (r"国资委", "国务院国资委"),
    (r"国家能源局|能源局", "国家能源局"),
    (r"市场监管总局", "市场监管总局"),
    (r"文化和旅游部|文旅部", "文化和旅游部"),
    (r"自然资源部", "自然资源部"),
    (r"生态环境部", "生态环境部"),
    (r"科技部", "科技部"),
    (r"人社部|人力资源社会保障", "人社部"),
    (r"民政部", "民政部"),
    (r"海关总署", "海关总署"),
    (r"税务总局", "税务总局"),
    (r"网信办", "网信办"),
    (r"国家数据局|数据局", "国家数据局"),
    (r"国家统计局", "国家统计局"),
    (r"金融监管", "金融监管部门"),
]
_CENTRAL_RE = re.compile("(" + "|".join(k for k, _ in _CENTRAL_ORGS) +
                         "|LPR|MLF|逆回购|降准|降息|公开市场操作)")


def _org_label(text: str) -> str:
    """从标题/正文里识别展示机关名（命中第一个即返回）。"""
    for pat, label in _CENTRAL_ORGS:
        if re.search(pat, text or ""):
            return label
    return ""


# 地方口径过滤（王总点名：只认国家官方口径，不认"天津市xxx"这类地方条目）
_LOCAL_RE = re.compile(
    r"[\u4e00-\u9fa5]{1,5}(?:省|市委|市政府|人民政府|发改委|工信局|住建局|文旅局|交通局|商务局)"
    r"|开发区|自治州|管委会|保税区")

# 外国机构过滤（"英国央行/美联储"这类外围动态不属于国内政策口径）
_FOREIGN_RE = re.compile(
    r"美联储|欧洲央行|英国央行|日本央行|加拿大央行|澳洲央行|澳大利亚联储|韩国央行|"
    r"印尼央行|泰国央行|菲律宾央行|新西兰联储|瑞士央行|瑞典央行|挪威央行|土耳其央行|"
    r"鲍威尔|拉加德|贝利|植田和男")


# ---------------------------------------------------------------- 板块影响映射（规则表，机器初判）

_SECTOR_RULES = [
    (r"降准|降息|LPR|利率|货币政策|逆回购|MLF|流动性|公开市场",
     ["银行", "房地产", "券商"],
     "货币宽松信号→资金面转松：银行信贷规模有望扩大，地产融资成本下降，券商交投活跃度提升"),
    (r"资本市场|发行上市|并购重组|分红|回购|投资者",
     ["券商", "金融IT"],
     "资本市场制度安排→市场活跃度与配置需求提升，券商/金融科技直接受益"),
    (r"住房|房地产|楼市|保障房|城中村|物业|房贷",
     ["房地产", "建材", "家居"],
     "地产政策松绑/加杠杆→销售与开工预期修复，带动建材家居等后周期"),
    (r"基建|重大工程|专项债|水利|城市更新|基础设施",
     ["基建", "工程机械", "建材"],
     "财政/专项债加码→实物工作量提升，基建链订单改善"),
    (r"消费|扩大内需|以旧换新|消费券|零售|促消费|体育赛事",
     ["零售", "家电", "食品饮料", "体育产业"],
     "促消费政策→终端需求改善，家电/汽车以旧换新与赛事经济直接受益"),
    (r"汽车|新能源车|智能网联|自动驾驶|房车",
     ["汽车", "汽车零部件", "锂电池"],
     "汽车产业政策→销量与出海预期提升，零部件与电池链跟随"),
    (r"芯片|半导体|集成电路|晶圆|光刻|大基金",
     ["半导体", "半导体设备", "电子化学品"],
     "科技自主/大基金投向→国产替代加速，设备材料环节弹性大"),
    (r"人工智能|AI|大模型|算力|智能算力",
     ["AI算力", "服务器", "光模块"],
     "AI 政策加码→算力基建投资扩张，算力链（服务器/光模块）受益"),
    (r"机器人|具身智能|智能制造|工业母机",
     ["机器人", "工业母机"],
     "智能制造升级→自动化设备与核心零部件需求增长"),
    (r"低空经济|无人机|通用航空|eVTOL|航空器",
     ["低空经济", "军工", "通航"],
     "低空经济部署→整机与空管基建先行受益"),
    (r"卫星|商业航天|北斗|航天",
     ["卫星互联网", "军工"],
     "航天工程与星座组网→卫星制造与地面终端链受益"),
    (r"新能源|光伏|风电|储能|氢能|核电",
     ["光伏", "风电", "储能", "电网设备"],
     "能源转型部署→装机与电网投资保持高位，设备链受益"),
    (r"电力|电网|电价|煤炭|油气|天然气|能源保供",
     ["电力", "煤炭", "油气"],
     "能源保供/价格机制→煤电盈利与上游资源景气度变化"),
    (r"医药|医疗|创新药|疫苗|集采|康复|护理",
     ["医药", "医疗器械", "医疗服务"],
     "医疗政策→需求扩容（如康复护理工程）或集采压制，细分方向分化"),
    (r"生育|养老|银发|托育|社保|人口",
     ["养老产业", "医疗服务"],
     "人口政策投入→养老服务与康复产业链扩容"),
    (r"文旅|旅游|文化|影视|游戏|非遗|演唱会",
     ["旅游酒店", "传媒", "影视"],
     "文旅政策→出行链与内容消费景气度提升"),
    (r"外贸|出口|关税|跨境电商|自贸|RCEP|稳外贸",
     ["跨境电商", "港口航运"],
     "稳外贸措施→出口链订单与物流吞吐改善"),
    (r"外资|准入|扩大开放|自由贸易",
     ["跨境电商", "券商"],
     "扩大开放→外资流入与服务贸易机会增加"),
    (r"数字经济|数据要素|数据流通|区块链",
     ["数字经济", "数据要素", "IDC"],
     "数据要素政策→数据基建与运营环节价值重估"),
    (r"农业|粮食|种业|生猪|乡村振兴|农产品",
     ["农业", "种业", "养殖"],
     "农业支持政策→种植链与养殖盈利预期改善"),
    (r"环保|碳|绿色|节能|污染|生态",
     ["环保", "绿电"],
     "绿色转型约束/激励→环保设备与绿电运营受益"),
    (r"稀土|有色|铜|铝|锂|矿产|收储",
     ["稀土永磁", "有色金属"],
     "资源管理/收储→供给约束下资源品价格弹性"),
    (r"钢铁|煤炭|去产能|化解过剩|产能治理",
     ["钢铁", "煤炭"],
     "供给侧约束→行业格局优化，龙头盈利修复"),
    (r"保险|险资|内贸险",
     ["保险", "多元金融"],
     "保险政策→险资运用范围扩大/险种创新，保险板块受益"),
    (r"教育|职业教育|培训",
     ["教育", "传媒"],
     "教育政策→职教与教育信息化投入增加"),
    (r"信贷|融资|普惠金融|金融支持|贷款",
     ["银行", "多元金融"],
     "金融支持文件→信贷投放定向扩容，受益行业视文件指向而定"),
    (r"中小企业|民营|个体工商户|稳企",
     ["银行", "多元金融"],
     "助企政策→融资成本下降，小微业务占比高的银行受益"),
    (r"减税|税费|财政政策|国债|预算",
     ["银行", "基建"],
     "财政工具→相关行业资金来源改善，利率债供给变化影响银行配置"),
    (r"就业|稳岗|工资",
     ["零售", "食品饮料"],
     "稳就业→居民收入预期改善，大众消费受益"),
    (r"服务业|扩能提质|家政|托育服务",
     ["消费", "旅游酒店"],
     "服务业支持→线下服务消费供给改善，相关连锁业态受益"),
]
_SECTOR_RE_CACHE = [(re.compile(p), s, n) for p, s, n in _SECTOR_RULES]


def map_sectors(text: str, max_n: int = 4) -> tuple:
    """规则映射：标题+摘要 → (板块名列表, 传导逻辑)。机器初判，命中多条取前 max_n。"""
    text = text or ""
    sectors, notes = [], []
    for rex, secs, note in _SECTOR_RE_CACHE:
        if rex.search(text):
            for s in secs:
                if s not in sectors:
                    sectors.append(s)
            if note and note not in notes:
                notes.append(note)
            if len(sectors) >= max_n:
                break
    return sectors[:max_n], "；".join(notes[:2])


# ---------------------------------------------------------------- 采集

_GOV_API = ("https://sousuo.www.gov.cn/search-gov/data?t=%s"
            "&q=&timetype=timeqb&mintime=&maxtime=&sort=pubtime&sortType=1"
            "&searchfield=title&p=1&n=%d")
_GOV_TYPES = [("国务院文件", "zhengcelibrary_gw"), ("部委文件", "zhengcelibrary_bm")]
_JIN10_URL = "https://www.jin10.com/flash_newest.js"
_JIN10_RE = re.compile(r"newest\s*=\s*(\[.*\]);?\s*$", re.S)


def _fetch_gov() -> list:
    """gov.cn 政策文件库（国务院 + 部委）。天然国家级，直接入库。"""
    out = []
    for type_name, t in _GOV_TYPES:
        try:
            d = json.loads(_http_text(_GOV_API % (t, GOV_PAGE_SIZE),
                                      referer="https://www.gov.cn/zhengce/", timeout=12))
            docs = (d.get("searchVO") or {}).get("listVO") or []
        except Exception:  # noqa: BLE001
            continue
        for it in docs:
            if not isinstance(it, dict):
                continue
            title = (it.get("title") or "").strip()
            if not title:
                continue
            pubtime = (it.get("pubtimeStr") or "").replace(".", "-")
            org = (it.get("puborg") or "").strip() or type_name
            summary = _strip_html(str(it.get("summary") or ""))
            sectors, transmission = map_sectors(title + " " + summary)
            imp = 3 if (t == "zhengcelibrary_gw" and "办公厅" not in org) else 2
            out.append({
                "key": (it.get("url") or "gov:" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:16]),
                "pub_time": pubtime[:10],
                "issuer": org,
                "level": "中央",
                "importance": imp,
                "source": "gov.cn",
                "source_label": "中国政府网",
                "title": title,
                "url": it.get("url") or "",
                "summary": summary[:160],
                "sectors": sectors,
                "transmission": transmission,
                "doc_type": type_name,
            })
    return out


def _fetch_flash() -> list:
    """金十快讯 + 东财快讯：只保留中央级口径（_CENTRAL_RE 命中），地方默认过滤。"""
    cands = []
    # ① 金十（有 important 标记）
    try:
        txt = _http_text(_JIN10_URL, referer="https://www.jin10.com/", timeout=10)
        m = _JIN10_RE.search(txt.strip())
        arr = json.loads(m.group(1)) if m else []
        for it in arr:
            data = it.get("data") or {}
            body = _strip_html(str(data.get("content") or ""))
            title = _strip_html(str(data.get("title") or "")) or body
            if not title:
                continue
            cands.append({"time": str(it.get("time") or ""),
                          "title": title[:120],
                          "important": 1 if it.get("important") else 0,
                          "url": str(data.get("url") or "")})
    except Exception:  # noqa: BLE001
        pass
    # ② 东财 7×24
    try:
        import emapi
        r = emapi.fast_news(num=120)
        if r.get("ok"):
            for it in r.get("rows", []):
                t = (it.get("title") or it.get("summary") or "").strip()
                if not t:
                    continue
                code = it.get("code") or ""
                cands.append({"time": it.get("showTime") or "",
                              "title": t[:120],
                              "important": 0,
                              "url": ("https://finance.eastmoney.com/a/%s.html" % code) if code else ""})
    except Exception:  # noqa: BLE001
        pass
    # 筛选：中央级准入 + 去地方
    out, seen = [], set()
    for c in cands:
        title = c["title"]
        if title in seen:
            continue
        if _LOCAL_RE.search(title):
            continue
        if _FOREIGN_RE.search(title):
            continue
        if not _CENTRAL_RE.search(title):
            continue
        seen.add(title)
        org = _org_label(title)
        sectors, transmission = map_sectors(title)
        key_money = bool(re.search(r"降准|降息|LPR|MLF|逆回购|政治局|国常会|国务院|中央政治局", title))
        out.append({
            "key": ("flash:" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:20]),
            "pub_time": c["time"][:16],
            "issuer": org or "中央动态",
            "level": "中央",
            "importance": 3 if (key_money or c.get("important")) else 1,
            "source": "flash",
            "source_label": "快讯（金十/东财筛选）",
            "title": title,
            "url": c.get("url") or "",
            "summary": "",
            "sectors": sectors,
            "transmission": transmission,
            "doc_type": "快讯",
        })
        if len(out) >= FLASH_KEEP:
            break
    return out


# ---------------------------------------------------------------- 落库

_SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_news (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT UNIQUE NOT NULL,
  pub_time TEXT,
  issuer TEXT,
  level TEXT,
  importance INTEGER DEFAULT 1,
  source TEXT,
  source_label TEXT,
  title TEXT NOT NULL,
  url TEXT,
  summary TEXT,
  sectors TEXT,
  transmission TEXT,
  doc_type TEXT,
  fetched_at TEXT
);
"""
_SCHEMA_DONE = False


def ensure_schema(conn) -> None:
    global _SCHEMA_DONE
    if _SCHEMA_DONE:
        return
    conn.executescript(_SCHEMA)
    conn.commit()
    _SCHEMA_DONE = True


_LAST_REFRESH = 0.0     # 进程内成功刷新时间戳
_LOCK = None            # 延迟初始化（server 进程 import 时创建）
try:
    import threading
    _LOCK = threading.Lock()
except Exception:  # noqa: BLE001
    _LOCK = None


def maybe_refresh(conn, force: bool = False) -> dict:
    """刷新采集并落库（带 TTL 与线程锁；失败不影响读旧数据）。"""
    global _LAST_REFRESH
    ensure_schema(conn)
    if _LOCK is not None:
        got = _LOCK.acquire(blocking=False) if not force else _LOCK.acquire(timeout=30)
        if not got:
            return {"ok": True, "skipped": "busy"}
        try:
            if not force and time.time() - _LAST_REFRESH < REFRESH_TTL:
                return {"ok": True, "skipped": "ttl"}
            res = _refresh_inner(conn)
            if res.get("ok"):
                _LAST_REFRESH = time.time()
            return res
        finally:
            _LOCK.release()
    if not force and time.time() - _LAST_REFRESH < REFRESH_TTL:
        return {"ok": True, "skipped": "ttl"}
    res = _refresh_inner(conn)
    if res.get("ok"):
        _LAST_REFRESH = time.time()
    return res


def _refresh_inner(conn) -> dict:
    items = _fetch_gov() + _fetch_flash()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inserted = 0
    for it in items:
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO policy_news"
                " (key,pub_time,issuer,level,importance,source,source_label,title,url,"
                "  summary,sectors,transmission,doc_type,fetched_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (it["key"], it["pub_time"], it["issuer"], it["level"], it["importance"],
                 it["source"], it.get("source_label", ""), it["title"], it.get("url", ""),
                 it.get("summary", ""), json.dumps(it.get("sectors", []), ensure_ascii=False),
                 it.get("transmission", ""), it.get("doc_type", ""), now))
            inserted += cur.rowcount if cur.rowcount > 0 else 0
        except Exception:  # noqa: BLE001
            continue
    conn.commit()
    return {"ok": True, "fetched": len(items), "inserted": inserted}


# ---------------------------------------------------------------- 代表股（板块 → 龙头，联动 board_flow 新浪源）

# 行业板块名单缓存（新浪行业板块约 48~96 个，长时间不变）
_SNAP_CACHE = {"ts": 0.0, "rows": []}
_SNAP_TTL = 3600

# 板块 → 代表股：按板块独立缓存；失败只短缓存(180s)尽快重试，且不污染其它板块
_SEC_MEM = {}
_SEC_POS_TTL = 1800
_SEC_NEG_TTL = 180
_TOP_PER = 8          # 每个板块给几只代表股（王总要求多列几只，按知名度排序）
_SEC_MEM_TTL = 60     # 代表股（含实时价）缓存秒数


# 规则标签 → 新浪行业板块名（粒度粗：银行/券商都在「金融行业」，需别名落地）
_SECTOR_BOARD_ALIASES = {
    "银行": ["金融行业"], "券商": ["金融行业"], "保险": ["金融行业"], "多元金融": ["金融行业"],
    "金融IT": ["金融行业", "电子信息"],
    "半导体": ["电子器件", "电子信息"], "半导体设备": ["电子器件"], "电子化学品": ["化工行业"],
    "AI算力": ["电子信息", "电子器件"], "服务器": ["电子信息"], "光模块": ["电子信息"],
    "数字经济": ["电子信息"], "数据要素": ["电子信息"], "IDC": ["电子信息"],
    "医药": ["生物制药"], "医疗服务": ["生物制药", "医疗器械"], "医疗器械": ["医疗器械"],
    "养老产业": ["生物制药"],
    "家电": ["家电行业"], "家居": ["家具行业"],
    "建材": ["建筑建材", "水泥行业", "玻璃行业"], "基建": ["建筑建材"],
    "工程机械": ["机械行业"], "工业母机": ["机械行业"], "机器人": ["机械行业", "电子器件"],
    "汽车": ["汽车制造"], "汽车零部件": ["汽车制造"], "锂电池": ["有色金属", "电子器件"],
    "电力": ["电力行业"], "电网设备": ["发电设备", "电力行业"], "绿电": ["电力行业", "发电设备"],
    "储能": ["发电设备", "有色金属"], "光伏": ["发电设备"], "风电": ["发电设备"],
    "煤炭": ["煤炭行业"], "油气": ["石油行业", "煤炭行业"],
    "钢铁": ["钢铁行业"], "稀土永磁": ["有色金属"], "有色金属": ["有色金属"],
    "零售": ["商业百货"], "食品饮料": ["食品行业", "酿酒行业"], "消费": ["商业百货", "食品行业"],
    "旅游酒店": ["酒店旅游"], "传媒": ["传媒娱乐"], "影视": ["传媒娱乐"], "教育": ["传媒娱乐"],
    "体育产业": ["传媒娱乐", "商业百货"],
    "农业": ["农林牧渔"], "种业": ["农林牧渔", "农药化肥"], "养殖": ["农林牧渔"],
    "环保": ["环保行业"], "军工": ["飞机制造", "船舶制造"], "通航": ["飞机制造"],
    "卫星互联网": ["飞机制造"], "低空经济": ["飞机制造", "机械行业"],
    "跨境电商": ["物资外贸"], "港口航运": ["交通运输", "公路桥梁"],
    "房地产": ["房地产"], "物业": ["房地产"], "纺织": ["纺织行业"], "服装": ["服装鞋类"],
    "电器": ["电器行业"], "电子": ["电子器件", "电子信息"],
}


def _sina_industry_rows():
    """行业板块名单（new_ 节点），缓存 1h。失败返回已缓存内容或空。"""
    now = time.time()
    if now - _SNAP_CACHE["ts"] < _SNAP_TTL and _SNAP_CACHE["rows"]:
        return _SNAP_CACHE["rows"]
    rows = []
    try:
        import board_flow
        # ⚠️ 只用**行业**板块（new_ 节点）：实测新浪 getHQNodeData 对 gn_ 概念节点
        # 会返回全市场兜底名单（格力/复星这类），纯属错误数据——概念板块宁缺毋滥。
        for asc in (0, 1):
            try:
                rows.extend(board_flow._sina_snapshot("industry", 100, asc=asc) or [])
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    if rows:
        _SNAP_CACHE.update(ts=now, rows=rows)
    return rows


def _sina_members_one(sec: str, top_per: int, out: dict, now: float) -> None:
    """无静态名单的板块 → 回退新浪板块节点（老口径，尽力而为）。"""
    rows = _sina_industry_rows()
    wanted, used_bc = [], set()

    def _pick(_sec, cond):
        for r in rows:
            bn = r.get("board_name") or ""
            bc = r.get("board_code") or ""
            if bn and bc and bc not in used_bc and cond(bn):
                return (bn, bc)
        return None

    hit = _pick(sec, lambda bn, s=sec: bn == s)
    if not hit:
        for alias in _SECTOR_BOARD_ALIASES.get(sec, []):
            hit = _pick(sec, lambda bn, a=alias: bn == a)
            if hit:
                break
    if not hit:
        hit = _pick(sec, lambda bn, s=sec: (s in bn or bn in s) and len(bn) >= 2)
    if hit:
        used_bc.add(hit[1])
        wanted.append((sec, hit[1]))
    for _s, bc in wanted:
        c = _SEC_MEM.get(_s)
        if c is not None and now - c["ts"] < (_SEC_POS_TTL if not c["neg"] else _SEC_NEG_TTL):
            if not c["neg"]:
                out[_s] = c["members"]
            continue
        try:
            import board_flow
            got = board_flow._sina_members(bc, top_per) or []
        except Exception:  # noqa: BLE001
            got = []
        if got:
            _SEC_MEM[_s] = {"ts": now, "members": got, "neg": False}
            out[_s] = got
        else:
            _SEC_MEM[_s] = {"ts": now, "members": [], "neg": True}


def _sector_members(sectors: list, top_per: int = _TOP_PER) -> dict:
    """板块名 → 代表股，**按知名度 / 总市值降序（越靠前越靠前）**。取不到留空。

    2026-10-01（王总：每个子板块多列几只代表股、按知名度排序）：
    主口径改为 sector_leaders.py 里人工维护的龙头名单。原因是实测新浪 new_xxx 板块
    节点口径严重串类——券商 / 保险 / 多元金融 / 金融IT 都命中「金融行业」→ 返回工行
    建行这类非券商股；影视 / 教育 / 体育产业 都命中「传媒娱乐」；卫星互联网 / 通航 /
    低空经济 都命中「飞机制造」；机器人 / 工程机械 同一批；光伏 / 风电 / 储能 / 电网
    设备 同一批；锂电池 / 稀土永磁 / 有色金属 同一批。且 `_pick` 的 used_bc 去重会让
    后出现的 sector 拿到同一批错股（「旅游酒店」混进巨人网络、「零售」混进藏格矿业），
    东财 push2 本机又 RemoteDisconnected。
    名单只管「谁 + 顺序」，实时价 / 涨跌幅走新浪批量行情（sector_chain.batch_quotes，
    自带磁盘缓存）；名单里的名称兜底，行情挂了也不会退化成一串光秃秃的代码。
    名单里没有的 sector 才回退新浪节点（尽力而为）。
    """
    out = {}
    if not sectors:
        return out
    now = time.time()
    pending = []          # [(sec, [(code, name), ...])]
    for sec in sectors[:8]:
        c = _SEC_MEM.get(sec)
        if c is not None and now - c["ts"] < _SEC_MEM_TTL:
            out[sec] = c["members"]
            continue
        try:
            import sector_leaders
            lst = sector_leaders.leaders(sec, top=top_per)
        except Exception:  # noqa: BLE001
            lst = []
        if lst:
            pending.append((sec, lst))
        else:
            _sina_members_one(sec, top_per, out, now)
    if not pending:
        return out
    try:
        import sector_chain
        q = sector_chain.batch_quotes([c for _, l in pending for c, _ in l]) or {}
    except Exception:  # noqa: BLE001
        q = {}
    got = {}
    for sec, lst in pending:
        ms = []
        for code, name in lst:
            d = q.get(code) or {}
            ms.append({"code": code, "name": d.get("name") or name,
                       "pct": d.get("pct"), "price": d.get("price")})
        if ms:
            got[sec] = ms
    if got:
        _SEC_MEM.update({k: {"ts": now, "members": v, "neg": False} for k, v in got.items()})
        out.update(got)
    return out



# ---------------------------------------------------------------- 输出

_BASIS = [
    "政策文件来自中国政府网政策文件库（国务院文件 + 部委文件）；快讯经中央级机构关键词筛选，"
    "省市级条目默认过滤（王总口径：以国家官方口径为准，不以地方政策定方向）。",
    "影响板块与传导逻辑为关键词规则映射（机器初判），不是语义研判，方向仅供参考。",
    "代表股=板块资金流快照按总市值取的龙头（新浪源），仅示意板块方向，不构成个股推荐。",
    "政策落地成效（试点实施结果是否正向）需持续跟踪：系统已将每条政策落库积累（policy_news 表），"
    "后续逐步补充试点与执行效果的关联分析。",
    "政策承压面（利空）只看标题里的压制措辞，正文顺带提及不算政策取向；"
    "境外快讯不计入国内政策口径。「产能约束」类（去产能/能耗双控）整体是分化——"
    "龙头受益、高成本产能出清，不等同于单边利空。压制类型按 impact 分型标注。",
]


# 压制/约束类信号（2026-10-05 王总要求：政策不只是利好，也要看得见利空面）
# 旧写法有三处硬伤，逐条修：
#  ① 只在标题上匹配。正文顺带提一句「集采」就把《医疗康复护理扩容提升工程实施方案》
#     这种整体利好的方案整条判成压制 —— 一条政策里支持与压制并存时，整体基调才是准的。
#  ② 「限制」「约束」是裸词，会误伤「智利铜产量降至 15 年最低」这类供应收缩描述
#     （对有色其实是利好），也会把境外快讯（以色列航班限制）拉进国内政策口径。
#     裸词剔除，只留明确组合；境外机构（_FOREIGN_RE）一律不进国内政策口径。
#  ③ 没有区分压制类型。集采压价、限产去产能、反垄断处罚，对板块的意义完全不同：
#     前两者是毛利/收入的直接承压，去产能反而是「龙头受益、中小出清」的分化。
#     一律标成「利空」是错误的，必须按 impact 分型标注。
_BEAR_RE = re.compile(
    r"集采|带量采购|集中采购|医保谈判|医保控费|药品价格|医药价格|耗材|"
    r"去产能|化解过剩|产能治理|淘汰落后|能耗双控|限产|错峰生产|双碳.*管控|"
    r"加征|房产税|资源税|环保税|税收优惠.*取消|补贴退坡|退坡|"
    r"反垄断|反不正当竞争|罚款|处罚|整顿|整治|整改|约谈|关停|取缔|清退|"
    r"打击|严查|核查|禁运|出口管制|限制出口|限购|限贷|限售|限价|"
    r"规范.*秩序|监管.*趋严|监管.*从严|数据安全|个人信息保护")

# 标题里的明确支持型基调：若标题同时是支持型（扩容/补贴/促进…）且压制词不在标题里，
# 视为「有压制条款但整体偏扶持」，记为 mixed 而不算净压制。
_BULLISH_TITLE_RE = re.compile(
    r"扩容|提升|支持|促进|鼓励|补贴|扶持|开放|放宽|减免|保障|试点|工程|规划|条例|意见|方案")

# 政策承压规则表（与上方 _SECTOR_RULES「利好映射」对称）。
# 每一条 = (命中正则, 承压板块, impact 分型, 传导说明)
_BEAR_RULES = [
    (r"集采|带量采购|集中采购|医保谈判|医保控费|药品价格|医药价格|耗材|仿制药|高值医用耗材",
     ["医药", "医疗器械", "医疗服务"], "压价",
     "集采/医保谈判压价→产品与耗材价格中枢下移，相关企业毛利与以量补价节奏承压"),
    (r"DRG|DIP|按病种付费|医保支付改革|控费",
     ["医疗服务", "医药"], "压价",
     "医保支付改革（DRG/DIP）→按病种打包付费压缩单次服务收入，控费要求向产业链传导"),
    (r"去产能|化解过剩|产能治理|淘汰落后|能耗双控|限产|错峰生产|两高|碳排放双控|环保督察",
     ["钢铁", "水泥", "电解铝", "化工", "玻璃", "造纸"], "产能约束",
     "限产/能耗约束→产量收缩、格局优化：龙头受益、高成本产能出清（整体是分化，不是单边利空）"),
    (r"限购|限贷|限售|房贷|楼市调控|需求端管控|限签",
     ["房地产", "建材", "家居"], "需求收缩",
     "需求端调控加码→销售与回款预期下修，地产链后周期同步承压"),
    (r"出口管制|限制出口|禁运|出口许可|配额|管制.*出口",
     ["稀土永磁", "有色金属", "半导体", "军工"], "出口受限",
     "出口管制/配额收紧→海外订单与溢价空间受限，相关资源与制造链承压"),
    (r"反垄断|反不正当竞争|平台经济|罚款|抽佣|价格战",
     ["互联网", "传媒", "零售", "食品饮料"], "合规处罚",
     "反垄断/合规处罚→抽佣与定价空间受限，原有增长模式需要调整"),
    (r"双减|校外培训|学科类培训|教育培训.*规范",
     ["教育"], "需求收缩",
     "双减/学科培训规范→校外学科培训需求收缩，仅素质类与职教保留空间"),
    (r"清朗|短视频|直播|网络.*乱象|虚假宣传|饭圈|网络暴力|网信办|网络平台",
     ["传媒", "影视", "游戏"], "合规处罚",
     "网络内容治理/清朗行动→内容合规成本上升，营销与买量打法受限，短剧直播等业态承压"),
    (r"整顿|整治|整改|约谈|关停|取缔|清退|清理|打击|严查|核查|违法违规",
     ["传媒", "影视", "旅游酒店", "餐饮"], "合规处罚",
     "行业整顿/执法趋严→合规成本上升，不合规业态与产能收缩"),
    (r"补贴退坡|补贴取消|退坡|加征|房产税|资源税|环保税|税费.*调整|收费",
     ["新能源", "光伏", "储能", "汽车", "锂电池", "风电"], "成本抬升",
     "补贴退坡或税费加征→终端成本上升、需求端补贴红利消失，盈利预期下修"),
    (r"限价|保供稳价|价格干预|价格疏导|调控价格",
     ["煤炭", "电力", "油气", "粮食"], "压价",
     "价格干预/保供稳价→上游价格上行空间受限，资源品盈利预期下修"),
    (r"数据安全|个人信息保护|数据出境|网络安全|关键基础设施",
     ["数字经济", "数据要素", "IDC", "服务器"], "合规处罚",
     "数据安全与出境监管→跨境业务与数据变现路径受限，合规成本上升"),
]
_BEAR_RULE_CACHE = [(re.compile(p), s, k, n) for p, s, k, n in _BEAR_RULES]

# impact 分型的中文标签（前端徽标用）
_BEAR_IMPACT_LABEL = {
    "压价": "压价", "需求收缩": "需求收缩", "产能约束": "产能约束",
    "出口受限": "出口受限", "合规处罚": "合规处罚", "成本抬升": "成本抬升",
}


def _age_days(ts: str) -> int:
    """政策距今天数（取不到就返回 0，前端按 '' 处理）。"""
    try:
        s = str(ts or "")[:10].replace("/", "-")
        if not s:
            return 0
        d0 = datetime.strptime(s, "%Y-%m-%d").date()
        return (datetime.now().date() - d0).days
    except Exception:  # noqa: BLE001
        return 0


_IMPL_DATE_RE = re.compile(
    r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*起\s*(?:施行|实施|执行)")
_IMPL_VAGUE_RE = re.compile(r"自\s*(?:印发|公布|发布)\s*之?日起\s*(?:施行|实施)")


def _impl_entries(it: dict) -> list:
    """从单条政策提取实施时间（严格口径，防把快讯里的事件日期当生效日期）。

    - 明确生效：标题/摘要出现「自 YYYY 年 M 月 D 日起施行/实施/执行」→ 具体日期；
    - 印发生效：「自印发/公布/发布之日起施行」→ 文字标记；
    - 都没有（意见/规划/方案类官方未标注）→ 返回空，前端如实写「未标注」，
      绝不拿发布日期凑数冒充实施日期（2026-10-06 王总要求，不能编）。
    """
    text = (it.get("title") or "") + " " + (it.get("summary") or "")
    m = _IMPL_DATE_RE.search(text)
    if m:
        try:
            d = "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:  # noqa: BLE001
            return []
        return [{"date": d, "title": (it.get("title") or "")[:40]}]
    m2 = _IMPL_VAGUE_RE.search(text)
    if m2:
        return [{"date": "印发即施行", "title": (it.get("title") or "")[:40]}]
    return []


def _is_bear_item(it: dict) -> bool:
    """该条政策是否属压制型（统一口径，利好面/利空面共用一处判定）。

    只认标题：正文顺带提一句「集采」不代表政策取向；境外快讯、支持型基调不判压制。
    """
    title = it.get("title") or ""
    if _FOREIGN_RE.search((it.get("issuer") or "") + title):
        return False
    if _BULLISH_TITLE_RE.search(title) and not _BEAR_RE.search(title):
        return False
    return bool(_BEAR_RE.search(title))


def _bear_signals(items: list) -> list:
    """扫政策库里的承压（利空面）信号。

    返回 [(板块, {"sector","n","bear","impact","evidence"}), ...]，按压制强度排序。

    判定口径（诚实边界，机器初判不是语义研判）：
    - 先跑 _BEAR_RULES 做主题映射（标题/摘要命中压制主题 → 该批板块进入承压面）；
    - 再看标题是否硬命中 _BEAR_RE（字面压制措辞），硬命中才计 1 条 bear；
      仅主题映射、标题无压制措辞的记 mixed（有压制主题但整体仍偏扶持）；
    - 境外机构（_FOREIGN_RE）与明确支持型标题一律不进承压池；
    - 一个板块可被多条规则覆盖，取 impact 去重后展示，evidence 按重要度保留前 3 条。
    """
    agg = {}
    for it in items:
        title = it.get("title") or ""
        issuer = it.get("issuer") or ""
        if _FOREIGN_RE.search(issuer + title):
            continue                       # 境外快讯不属于国内政策口径
        hard = _is_bear_item(it)               # L1：标题级压制措辞
        bull = bool(_BULLISH_TITLE_RE.search(title))
        text = (title or "") + " " + (it.get("summary") or "")
        for rex, secs, impact, note in _BEAR_RULE_CACHE:
            if not rex.search(text):
                continue
            # 一条政策只按最具体命中的那条规则归类：标题里「清朗·整治短视频」会同时命中
            # 「清朗/短视频」和「整顿/整治」两条规则，两条叠加会把旅游酒店、餐饮这类
            # 跟短视频整治无关的板块一起拖进承压面（2026-10-05 实测复现过）。
            for s in secs:
                # 2026-10-05 修 bug：这里原本有一句「if s not in it.sectors: continue」，
                # 但规则表的 secs 本身就是从 _SECTOR_RULES 来的，再和 map_sectors 的结果
                # 交叉过滤会把「规则表命中、map_sectors 因 max_n 截断没写进 sectors」的条目
                # 整条卡死（实测：网信办「清朗·整治短视频虚假人设」这条全库唯一真利空政策
                # 就是这么被漏掉的）。命中规则表即视为相关，直接计。
                a = agg.setdefault(s, {"sector": s, "n": 0, "bear": 0, "mixed": 0,
                                       "impacts": [], "notes": [], "evidence": [],
                                       "weight": 0})
                a["n"] += 1
                w = {3: 3, 2: 2}.get(int(it.get("importance") or 1), 1)
                a["weight"] += w
                if hard:
                    a["bear"] += 1            # L1 硬命中：标题有压制措辞
                elif bull:
                    a["mixed"] += 1           # L2 条款提及：支持型基调 + 正文带压制措辞
                else:
                    a["mixed"] += 1
                if impact not in a["impacts"]:
                    a["impacts"].append(impact)
                    if note not in a["notes"]:
                        a["notes"].append(note)
                if len(a["evidence"]) < 3:
                    sig = _BEAR_RE.search(title)
                    if hard or (not bull):
                        a["evidence"].append({
                            "title": title, "issuer": issuer, "url": it.get("url") or "",
                            "impact": impact, "hard": hard,
                            # 时效：压制条款是哪天的政策给的，跟政策一样要看新鲜度，
                            # 半年前的药品价格机制对新盘面的约束力跟昨天发布的完全不同。
                            "age_days": _age_days(it.get("pub_time") or it.get("time") or ""),
                            "signal": sig.group(0) if sig else "",
                            "importance": int(it.get("importance") or 1),
                            "date": str(it.get("pub_time") or it.get("time") or "")[:10],
                        })
            break       # 一条政策只按最具体命中的那条规则归类（8a 命中就不再吃 8b 的板块）
    out = []
    for a in agg.values():
        a["bear_pct"] = round(100.0 * a["bear"] / a["n"], 1) if a["n"] else 0.0
        a["impacts"] = a["impacts"][:3]
        a["notes"] = a["notes"][:2]
        a["evidence"] = sorted(a["evidence"], key=lambda x: -x["importance"])[:3]
        a["level"] = "承压" if a["bear_pct"] >= 60 else "关注"
        out.append(a)
    out.sort(key=lambda x: (-x["bear"], -x["weight"]))
    return out[:8]


def _sector_judgements(items: list, members: dict, top_n: int = 8) -> list:
    """按板块聚合研判：立场分级 + 传导理由 + 依据条数。

    立场口径（诚实边界：政策文本绝大多数是"支持/促进"型，规则库天然偏多）：
    - 强利好：政策密集（≥6 条）且重要度高（加权分 ≥18），短期情绪与资金面正面；
    - 利好：有明确支持性政策（加权分 ≥8）；
    - 关注：有政策提及但密度低，需等落地措施；
    - 分化/承压：命中条目中压制类信号（集采/去产能/整顿等）占比 ≥1/3，
      该板块不是单向利好，需区别细分方向。
    """
    agg = {}
    for it in items:
        # 2026-10-05 统一口径：压制判定只看标题（见 _is_bear_item），
        # 旧写法把 transmission 也扫进去，正文带一句「限制」就把强利好板块判成分化。
        bear = _is_bear_item(it)
        w = {3: 3, 2: 2}.get(int(it.get("importance") or 1), 1)
        for s in (it.get("sectors") or []):
            a = agg.setdefault(s, {"n": 0, "weight": 0, "bear": 0, "reasons": [],
                                   "pubs": [], "impls": []})
            a["n"] += 1
            a["weight"] += w
            if bear:
                a["bear"] += 1
            # 政策时间（2026-10-06 王总要求：研判卡必须标发布时间与实施时间）
            pt = str(it.get("pub_time") or it.get("time") or "")[:10]
            if pt and pt not in a["pubs"]:
                a["pubs"].append(pt)
            if len(a["impls"]) < 3:
                for x in _impl_entries(it):
                    if x not in a["impls"]:
                        a["impls"].append(x)
            # transmission 是多条拼接串（"；"连接），拆成原子逻辑再按板块去重，
            # 否则同一句逻辑出现在不同拼接组合里会重复展示（2026-09-30 渲染实测）。
            for r in re.split("[；;]", (it.get("transmission") or "")):
                r = r.strip()
                if r and r not in a["reasons"]:
                    a["reasons"].append(r)
    out = []
    for s, a in agg.items():
        # 「内有分化」是**附加面**不是主立场（2026-10-01 王总指出口径不一）：
        # 旧写法把「压制类占比≥1/3」当最高优先级，一个「政策密集且强利好、
        # 同时带 2 条集采压制」的板块会被整块判成「分化」，读者只看得到一个「分化」，
        # 看不到它其实整体偏多。改法：主档按强度判，分化降级成附加标注。
        mixed = bool(a["n"] > 0 and a["bear"] * 3 >= a["n"])
        if a["n"] >= 6 and a["weight"] >= 15:
            stance = "强利好"
        elif a["weight"] >= 8:
            stance = "利好"
        elif mixed:
            stance = "分化"
        else:
            stance = "关注"
        pubs = [p for p in a.get("pubs", []) if p]
        out.append({"sector": s, "n": a["n"], "weight": a["weight"],
                    "stance": stance, "mixed": mixed, "bear": a["bear"],
                    "reasons": a["reasons"][:3],
                    "pub_latest": max(pubs) if pubs else "",
                    "impl": a.get("impls", [])[:3],
                    "members": members.get(s, [])})
    out.sort(key=lambda x: (-x["weight"], -x["n"]))
    return out[:top_n]


def _rows_to_items(rows: list) -> list:
    """数据库行 → 面板条目（sectors 反序列化 + 字段归一）。"""
    items = []
    for r in rows:
        try:
            secs = json.loads(r.get("sectors") or "[]")
        except Exception:  # noqa: BLE001
            secs = []
        items.append({
            "key": r["key"], "time": r.get("pub_time") or "", "issuer": r.get("issuer") or "",
            "level": r.get("level") or "中央", "importance": int(r.get("importance") or 1),
            "source": r.get("source") or "", "source_label": r.get("source_label") or "",
            "title": r.get("title") or "", "url": r.get("url") or "", "pub_time": r.get("pub_time") or "",
            "summary": r.get("summary") or "", "sectors": secs,
            "transmission": r.get("transmission") or "", "doc_type": r.get("doc_type") or "",
        })
    return items


def _span_label(items: list) -> str:
    """政策覆盖时间跨度（如实告知扫描窗口，避免拿半年跨度说成「当前风向」）。"""
    days = [str(it.get("pub_time") or it.get("time") or "")[:10] for it in items
            if str(it.get("pub_time") or it.get("time") or "")[:10]]
    if not days:
        return ""
    lo, hi = min(days), max(days)
    try:
        d0 = datetime.strptime(lo, "%Y-%m-%d").date()
        d1 = datetime.strptime(hi, "%Y-%m-%d").date()
        return "%s ~ %s（约 %d 天）" % (lo, hi, (d1 - d0).days + 1)
    except Exception:  # noqa: BLE001
        return "%s ~ %s" % (lo, hi)


def payload(conn, num: int = 80) -> dict:
    """面板数据：历史条目（落库倒序）+ 今日风向聚合 + 口径。"""
    ensure_schema(conn)
    num = max(10, min(int(num or 80), 200))
    try:
        cur = conn.execute(
            "SELECT key,pub_time,issuer,level,importance,source,source_label,title,url,"
            "summary,sectors,transmission,doc_type FROM policy_news"
            " ORDER BY pub_time DESC, importance DESC, id DESC")
        all_rows = [dict(r) for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        all_rows = []
    rows = all_rows[:num]
    items = _rows_to_items(rows)          # 展示 + 利好面：仍按 num 条（王总已习惯的口径不变）
    all_items = _rows_to_items(all_rows)  # 利空面：扫全库，否则压制型政策被展示窗口截断就永远看不到
    sector_count = {}
    for r in rows:
        try:
            secs = json.loads(r.get("sectors") or "[]")
        except Exception:  # noqa: BLE001
            secs = []
        for s in secs:
            sector_count[s] = sector_count.get(s, 0) + 1
    top = sorted(sector_count.items(), key=lambda x: -x[1])[:8]
    top_sectors = [s for s, _ in top]
    # 2026-10-05 王总要求：政策不止利好面，也要看得见利空面。
    # 承压板块往往按权重排不进利好 Top8（医药/有色就是这样被挤掉的），
    # 所以代表股要按「利好 Top + 承压面」合并取，否则承压卡片会没有代表股。
    bear_rows = _bear_signals(all_items) if all_items else []
    bear_sectors = [b["sector"] for b in bear_rows]
    need_sectors = list(dict.fromkeys(top_sectors + bear_sectors))
    members = _sector_members(need_sectors) if need_sectors else {}
    judgements = _sector_judgements(items, members) if items else []
    for b in bear_rows:
        b["members"] = members.get(b["sector"], [])
    counts = {
        "gov": sum(1 for i in items if i["source"] == "gov.cn"),
        "flash": sum(1 for i in items if i["source"] != "gov.cn"),
        "total": len(items),
    }
    if judgements:
        strong = [j["sector"] for j in judgements if j["stance"] == "强利好"]
        good = [j["sector"] for j in judgements if j["stance"] == "利好"]
        # 用附加面 mixed 而非 stance=="分化" 归类：强利好但内有压制的板块也该被点名
        mixed = [j["sector"] for j in judgements if j.get("mixed")]
        parts = []
        if strong:
            parts.append("政策密集支持、短期情绪偏正面：%s" % "、".join(strong[:4]))
        if good:
            parts.append("有明确政策利好、待落地措施验证：%s" % "、".join(good[:4]))
        if mixed:
            parts.append("政策存在约束/压制面、方向分化：%s" % "、".join(mixed[:3]))
        bears = [b["sector"] for b in bear_rows]
        if bears:
            parts.append("另有 %d 个板块出现压制/约束条款、负向传导需留意：%s"
                         % (len(bears), "、".join(bears[:3])))
        line = ("综合 %d 条中央级政策研判 → " % len(items)) + "；".join(parts) + \
               "（立场由政策密度与重要度加权得出，属规则初判非涨跌预测）"
    elif top:
        line = ("近 %d 条中央级政策里，影响板块出现频次最高：%s——这是系统按规则表聚合的"
                "「政策风向」，不是涨跌预测。" % (len(items),
                                             "、".join("%s(%d)" % (s, n) for s, n in top[:4])))
    else:
        line = "暂无政策条目（首次加载会现场抓取，若网络受限可稍后刷新）。"
    return {
        "ok": True,
        "warming": len(items) == 0,   # 库内尚无数据（首次启动/网络受限），前端据此 3s 后补取
        "items": items,
        "verdict": {"line": line,
                    "sectors": judgements,
                    "bear_sectors": bear_rows,
                    "scan": {"total": len(all_items), "window": len(rows),
                             "span": _span_label(all_items)},
                    "top": [{"sector": s, "n": n,
                             "members": members.get(s, [])} for s, n in top],
                    "counts": counts},
        "basis": _BASIS,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
