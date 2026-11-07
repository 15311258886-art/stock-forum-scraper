# -*- coding: utf-8 -*-
"""四家专家团每日评估：建表 / 配置 / 取数 / 回写。

## 这个模块是什么（以及**不是**什么）

用户（进哥）要的是：点 AI 复盘里的 WorkBuddy 之后，下面挂四家专家团，
每家都有一份**当天**的独立报告 —— 大盘技术面 / 主线板块 / 未来几天值得关注 /
从 5000+ 只票里推荐买哪些。

这些报告**不是**本进程调模型生成的。每天 04:00，WorkBuddy 定时任务把
「专家团 agent」叫起来，agent 用 `expert_desk.py` 取信息包（brief）、分析、
再用 `expert_desk.py --save` 把报告写回库。本进程只负责三件不花钱的事：

  1. 建表（`ai_expert_review`）；
  2. 给 agent 用的取数/回写（`candidate_pool` / `write_review`）与只读查询（`build_items`）；
  3. 把结果如实端给前端（`/api/ai_expert`）。

**本模块绝不 import 任何模型调用代码，也绝不写 ai_review 的生成逻辑。**
DeepSeek 那条链路（点了就先算、10 秒出结果）与这里完全无关，一行都不该受影响。

## 为什么单独建模块而不是塞进 ai_review.py

`ai_review.py` 的每一行都在回答「怎么让模型算出一份复盘」；而这里回答的是
「四份已经算好的复盘怎么存、怎么取」。两者唯一的重合点是**复用它的信息包渲染**
（`build_context` / `_render_user`），那是 import 关系，不是同源关系。
混在一个文件里，下次改专家团一定会误伤 AI 复盘的生成逻辑 —— 这正是要避免的。

## 时间口径（重要，别"修"成今天）

04:00 跑的时候，最新交易日是**昨天/上一个交易日**（当天还没开盘、没有数据）。
所以 `latest_review_date()` 取的是「有数据的最大交易日」，而不是 `date.today()`。
把它改成今天，会让每天早上生成一份针对「尚无数据的日子」的空报告。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

# 项目根：本文件在 <root>/viewer/ai_expert.py，故 parent.parent 即根目录。
PROJ_ROOT = Path(__file__).resolve().parent.parent
# 库路径与 viewer/db.py 同口径（VIEWER_DB 可覆盖，测试与隔离环境靠它）。
DB_PATH = Path(os.environ.get("VIEWER_DB") or (PROJ_ROOT / "viewer" / "app.db"))
CONFIG_PATH = PROJ_ROOT / "config" / "expert_teams.json"

# 专家包根目录。默认取本机 WorkBuddy 的 experts 市场目录；用 Path.home() 而不是
# 硬编码 "C:/Users/野比&大熊"，换机器/换用户名时不会静默失效。
DEFAULT_EXPERT_ROOT = Path.home() / ".workbuddy" / "plugins" / "marketplaces" / "experts" / "plugins"
EXPERT_ROOT = Path(os.environ.get("EXPERT_PACK_ROOT") or DEFAULT_EXPERT_ROOT)

# 配置缺失时的兜底：顺序即前端展示顺序。
# 为什么要在代码里再写一份 —— config 文件被误删/写坏时，前端仍然要能"四家恒在"
# 地如实显示「未安装 / 今日未跑」，而不是整块消失（那就成了功能坏掉）。
DEFAULT_TEAMS = [
    {
        "key": "stock-partner",
        "name": "腾讯自选股股票投研专家团",
        "tags": ["估值定价", "资金风向", "短线节奏"],
        "pack": "stock-partner-team",
        "lead": "agents/stock-partner-lead.md",
        "members": ["agents/industry-strategist.md", "agents/signal-chief.md",
                    "agents/valuation-analyst.md", "agents/contrarian-investor.md",
                    "agents/fundamental-researcher.md", "agents/shortterm-surfer.md"],
    },
    {
        # 2026-11-04 进哥换了第 2 家：利樹说Pro（本机从未安装）→ 股票行情应用架构专家团。
        # pack 留空 + match_name：包名由「专家中文显示名」反查得到，装完即生效，
        # 不必再让人去问「装好之后目录叫什么」（见 _discover_pack 的注释）。
        "key": "stock-app-arch",
        "name": "股票行情应用架构专家团",
        "tags": ["股票行情应用", "全栈架构拆解"],
        "pack": "", "lead": "", "members": [], "match_name": "股票行情应用架构专家团",
    },
    {
        "key": "finance-suite",
        "name": "金融业务全流程专家团",
        "tags": ["金融建模与估值", "投资银行与证券研究"],
        "pack": "finance-workflow-suite",
        "lead": "agents/finance-workflow-suite-team-lead.md",
        "members": [],
    },
    {
        "key": "five-wave",
        "name": "股票五浪法专家团",
        "tags": ["波浪数浪", "时段研究", "三个月研究"],
        "pack": "stock-five-wave",
        "lead": "agents/stock-five-wave-team-lead.md",
        "members": ["agents/elliott-wave-analyst.md", "agents/market-window-analyst.md"],
    },
]

# ---------------------------------------------------------------- schema

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_expert_review (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    review_date TEXT NOT NULL,            -- 报告针对的交易日（YYYY-MM-DD）
    expert_key  TEXT NOT NULL,            -- 与 config/expert_teams.json 的 key 一致
    expert_name TEXT NOT NULL,            -- 冗余存中文名：配置改名不影响历史报告的可读性
    status      TEXT NOT NULL DEFAULT 'ok',
    stance      TEXT,                     -- 偏多 / 偏空 / 中性
    position    TEXT,                     -- 建议仓位，如「半仓以内」
    verdict     TEXT,                     -- 一句话结论
    market_read TEXT,
    mainlines_json TEXT,                  -- [{sector,stage,note}]
    picks_json  TEXT,                     -- [{code,name,action,reason,entry,stop,horizon}]
    watch_json  TEXT,                     -- ["..."]
    risks_json  TEXT,                     -- ["..."]
    score_note  TEXT,                     -- 这一家本轮的评分口径一句话（四家刻度不同，必须留证据）
    report_md   TEXT,                     -- 完整 markdown 报告（前端展开就是它）
    model       TEXT,
    elapsed_ms  INTEGER,
    err         TEXT,
    created_at  TEXT,
    UNIQUE(review_date, expert_key)       -- 一家一天一份：重复回写走 upsert，绝不堆垃圾行
);
CREATE INDEX IF NOT EXISTS idx_ai_expert_date ON ai_expert_review(review_date);
"""


def _migrate_columns(conn: sqlite3.Connection) -> None:
    """给老库补列 —— CREATE TABLE IF NOT EXISTS **不会**给已存在的表加列。

    为什么必须单独做这一步：`score_note` 是 2026-11-04 才引入的口径字段，
    此前落库的表没有这一列。不补的话 `write_review` 的 INSERT 会直接报
    「table ai_expert_review has no column named score_note」而整体失败 ——
    但这张表里存着历史报告，不能靠删表重建（会丢 11-03 那批）。
    幂等：列已在就什么都不做。
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_expert_review)")}
    except Exception as e:  # noqa: BLE001
        log.debug("[ai_expert] PRAGMA table_info 失败：%s", e)
        return
    if not cols:
        return
    if "score_note" not in cols:
        try:
            conn.execute("ALTER TABLE ai_expert_review ADD COLUMN score_note TEXT")
            log.info("[ai_expert] 已为老库补列 score_note")
        except Exception as e:  # noqa: BLE001
            # 并发下另一个连接可能刚补完，重复 ALTER 会报 duplicate column —— 不算错
            log.debug("[ai_expert] 补列 score_note 跳过：%s", e)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """幂等建表。刻意**不做**进程内一次性标记：测试用独立临时库，
    全局标记会让第二个库拿不到表（ai_review 那边就有这个坑）。
    两条 CREATE ... IF NOT EXISTS 是 schema 级判断，代价约等于一次空查询。

    ⚠ 只在「需要写/确认表存在」的场合调用（CLI 的 --brief/--save、首次 GET）。
    `executescript()` 会**隐式 COMMIT** 当前连接上的事务 —— 在服务共享的 `DB.conn` 上
    每请求都执行一次是不必要的副作用，所以读接口先用 `table_ready()` 轻量探测。
    """
    conn.executescript(_SCHEMA)
    _migrate_columns(conn)
    try:
        conn.commit()
    except Exception:  # noqa: BLE001 —— 只读连接（?mode=ro）提交会失败，忽略即可
        pass


def table_ready(conn: sqlite3.Connection) -> bool:
    """表是否已存在（只查 sqlite_master，不建表、不提交事务）。

    读接口用这个做闸：全新库（表还不存在）仍会走 ensure_schema 把表建出来，
    但**正常情况下每次 GET 只是一次极轻的 schema 查询**，不碰 `executescript`。
    """
    try:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_expert_review'"
        ).fetchone())
    except Exception as e:  # noqa: BLE001
        log.debug("[ai_expert] sqlite_master 探测失败：%s", e)
        return False


def connect(path=None) -> sqlite3.Connection:
    """打开库（默认生产库 / VIEWER_DB 覆盖）。CLI 与测试共用同一入口。"""
    p = str(path or DB_PATH)
    conn = sqlite3.connect(p, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=15000")
    except Exception:  # noqa: BLE001
        pass
    return conn


# ---------------------------------------------------------------- 配置


def load_teams() -> list:
    """读 `config/expert_teams.json`。

    读不到/格式坏 → 退回内置 DEFAULT_TEAMS 并记 warning。
    为什么不用异常：前端「四家恒在」是硬需求，配置坏了是**显示问题**，
    不该升级成「整个 AI 复盘面板报错」。
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, list) or not raw:
            raise ValueError("配置不是非空数组")
        out = []
        for i, t in enumerate(raw):
            if not isinstance(t, dict):
                raise ValueError("第 %d 项不是对象" % (i + 1))
            key = str(t.get("key") or "").strip()
            if not key:
                raise ValueError("第 %d 项缺 key" % (i + 1))
            out.append({
                "key": key,
                "name": str(t.get("name") or key),
                "tags": [str(x) for x in (t.get("tags") or [])],
                "pack": str(t.get("pack") or ""),
                "lead": str(t.get("lead") or ""),
                "members": [str(x) for x in (t.get("members") or [])],
                # 专家包的**中文显示名**：pack 为空时按它反查本机目录（见 _discover_pack）
                "match_name": str(t.get("match_name") or ""),
                "optional": bool(t.get("optional") or False),
            })
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("[ai_expert] 读 %s 失败（%s），退回内置四家", CONFIG_PATH, e)
        return [dict(t) for t in DEFAULT_TEAMS]


def team_by_key(key: str) -> dict:
    k = (key or "").strip()
    for t in load_teams():
        if t["key"] == k:
            return t
    return {}


def _norm_name(s: str) -> str:
    """名字归一化：去空白/常见标点，便于「专家中文名 → 包目录名」的宽松匹配。"""
    return re.sub(r"[\s\-_·、,，.。/\\|()（）\[\]【】]+", "", str(s or "")).lower()


# 「中文名 → 包目录名」反查缓存：{归一化中文名: (ts, 目录名)}
# 为什么要缓存：服务是常驻进程，读接口每次 GET 都会问一次 is_installed；
# 每次都 walk 20+ 个 plugin.json 是纯浪费。但也不能永久缓存 —— 用户可能是
# 服务跑着的时候才去专家中心点安装，60 秒后必须能自己认到（否则又变成
# 「装了还是显示未安装，得重启服务」，这正是要避免的那类体验）。
_DISCOVER_CACHE: dict = {}
_DISCOVER_TTL_SEC = 60


def _discover_pack(match_name: str) -> str:
    """按专家中文显示名，反查本机已装专家包的目录名。

    背景（2026-11-04）：第 2 家从「利樹说Pro」换成「股票行情应用架构专家团」。
    配置里只能写 `pack`（目录名），而专家中心装出来的目录名跟中文名毫无关系。
    如果要求「装完告诉我目录叫什么，我再改配置」，那就每次换团都要改代码 ——
    所以改成：pack 留空 + match_name，运行时读各包 plugin.json 的 displayName.zh 反查。

    匹配规则（从严到宽）：完全相等 → 互为子串。取第一个命中的，顺序稳定（按目录名排序）。
    找不到返回 ""（调用方按「未安装」处理，不抛异常）。
    """
    want = _norm_name(match_name)
    if not want:
        return ""
    hit = _DISCOVER_CACHE.get(want)
    now = time.time()
    if hit and now - hit[0] < _DISCOVER_TTL_SEC:
        return hit[1]
    found = ""
    try:
        for d in sorted(p for p in EXPERT_ROOT.iterdir() if p.is_dir()):
            manifest = d / ".codebuddy-plugin" / "plugin.json"
            if not manifest.is_file():
                continue
            try:
                with open(manifest, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:  # noqa: BLE001 —— 单个包清单坏了不该影响别的团
                continue
            dn = meta.get("displayName")
            names = []
            if isinstance(dn, dict):
                names = [dn.get("zh"), dn.get("en")]
            else:
                names = [dn]
            names.append(meta.get("name"))
            cand = [_norm_name(x) for x in names if x]
            if want in cand or any(want in c or c in want for c in cand):
                found = d.name
                break
    except Exception as e:  # noqa: BLE001
        log.warning("[ai_expert] 按名反查专家包失败（%s）：%s", match_name, e)
        return ""
    _DISCOVER_CACHE[want] = (now, found)
    return found


def pack_dir(team: dict) -> Path | None:
    """该专家团在本机的包目录；未配置且反查不到返回 None。"""
    pack = str((team or {}).get("pack") or "").strip()
    if not pack:
        pack = _discover_pack(str((team or {}).get("match_name") or ""))
    if not pack:
        return None
    return EXPERT_ROOT / pack


def is_installed(team: dict) -> bool:
    """本机是否真装了这个专家包 —— 判据是「目录在 + lead 提示词在」。

    只看目录会在半装（下载中断、只留空壳）时误报「已安装」，
    于是自动化会去跑一家根本没有提示词可读的团，产出空报告。
    """
    d = pack_dir(team)
    if d is None or not d.is_dir():
        return False
    lead = str((team or {}).get("lead") or "").strip()
    if not lead:
        return True  # 只配了包名没配 lead：目录在即认为可用
    return (d / lead).is_file()


# ---------------------------------------------------------------- 日期


def _one(conn, sql: str, args=()):
    """安全查询：表缺失/字段缺失一律返回 None，绝不因某张表不存在而整包失败。"""
    try:
        row = conn.execute(sql, args).fetchone()
    except Exception as e:  # noqa: BLE001
        log.debug("[ai_expert] 查询失败 %s: %s", sql[:60], e)
        return None
    if row is None:
        return None
    return row[0]


def latest_review_date(conn) -> str:
    """最近一个**有数据**的交易日。

    顺序：stock_daily_snapshot.trade_date → market_kline.date（若存在）
          → ai_review.review_date → 今天。
    04:00 时它返回的必然是昨天/上一个交易日 —— 这是正确行为（见模块 docstring）。
    """
    d = _one(conn, "SELECT MAX(trade_date) FROM stock_daily_snapshot")
    if d:
        return str(d)
    d = _one(conn, "SELECT MAX(date) FROM market_kline")
    if d:
        return str(d)
    d = _one(conn, "SELECT MAX(review_date) FROM ai_review")
    if d:
        return str(d)
    return time.strftime("%Y-%m-%d")


def resolve_date(conn, date: str = "") -> str:
    """`latest` / 空 / 非法 → 最近交易日；合法 YYYY-MM-DD 原样返回。"""
    d = str(date or "").strip()
    if not d or d.lower() == "latest":
        return latest_review_date(conn)
    if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return d
    log.warning("[ai_expert] 日期 %r 不是 YYYY-MM-DD，按 latest 处理", d)
    return latest_review_date(conn)


# ---------------------------------------------------------------- 读


def _jload(s, default):
    """JSON 列解析：坏数据给默认值，绝不抛（前端宁可少一块也不要整页崩）。"""
    if s is None or s == "":
        return default
    if isinstance(s, (list, dict)):
        return s
    try:
        v = json.loads(s)
    except Exception:  # noqa: BLE001
        return default
    return v if isinstance(v, type(default)) else default


# ---------------------------------------------------------------- 荐股评分与合并榜
#
# 2026-11-04 进哥需求：四家各推几只票、按**各自体系**打 0~100 分，把 85 分以上的依次列出来，
# 旁边写清推荐理由，点代码能直接看这只票。
#
# 两条刻意的设计：
#   ① 分数由各家自己定，我们不替它们统一标准 —— 四家的方法论本来就不同（趋势/估值/资金/浪型），
#      强行拉到一个刻度上，等于把「专家」变成「同一套指标换个名字」，那就没意义了。
#      我们只做**归一化**（"85分"/"85.0"/"约 88" → 85/85/88），因为 agent 的写法不统一，
#      字符串进前端会把排序和「≥85」筛选一起搞错。
#   ② 只收「买/持」方向。规避、不参与这类结论也很有价值，但它们不是"推荐个股"，
#      混进榜里会让人误以为今天有 20 个机会。

MIN_SCORE = 85        # 「强推」线：各家体系里属于"可下单"的那一档
BOARD_MIN_ROWS = 5    # 进哥要的节奏：每天 5~10 只。达标不足 5 只时按分数补到 5 只（标注"观察"）
BOARD_CAP = 15        # 上限：「十几只也行」，再往上就成信息噪音

# 这些动作不是"推荐买入"，不进榜。用子串匹配而不是精确相等：
# 各家写法五花八门（"规避"、"不参与"、"观望不追"、"减仓"）。
_AVOID_ACTIONS = ("规避", "回避", "不参与", "不做", "减仓", "清仓", "卖出", "观望")


def _norm_score(v):
    """把各家写的分数收成 0~100 的整数；拿不到分数返回 None（不是 0）。

    为什么区分 None 与 0：0 分是「这只票极其不合格」的**结论**，
    None 是「这一家没打分」的**事实**。混用一个值，页面就没法诚实显示「旧报告没有评分」。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        s = int(round(float(v)))
    else:
        m = re.search(r"-?\d+(?:\.\d+)?", str(v))
        if not m:
            return None
        try:
            s = int(round(float(m.group())))
        except (TypeError, ValueError):
            return None
    return max(0, min(100, s))


def _norm_pick(p) -> dict | None:
    """单只票归一化：只动 score 的类型，其余字段原样保留（包括我们不认识的字段）。"""
    if not isinstance(p, dict):
        return None
    q = dict(p)
    q["score"] = _norm_score(p.get("score"))
    return q


def _norm_picks(v) -> list:
    if not isinstance(v, list):
        return []
    return [x for x in (_norm_pick(p) for p in v) if x is not None]


def build_board(items: list, min_score: int = MIN_SCORE,
                min_rows: int = BOARD_MIN_ROWS, cap: int = BOARD_CAP) -> dict:
    """四家 picks 合并成一张按分数排的榜（"今日荐股 · 合并榜"）。

    - 只收 6 位真代码（点得开个股）、非规避类动作、且打了分的票；
    - score >= min_score 的按分数降序全列（最多 cap 条）；
    - 达标不足 min_rows 条时，用次高分补足，`strong=False` 标成"观察"，绝不把
      80 分的票伪装成 85 分 —— 榜单的价值就在这条线是硬的。
    """
    order = {str(it.get("key") or ""): i for i, it in enumerate(items or [])}
    team_name = {str(it.get("key") or ""): str(it.get("name") or "") for it in (items or [])}

    rows, by_code = [], {}
    for it in (items or []):
        key = str(it.get("key") or "")
        for p in (it.get("picks") or []):
            code = str(p.get("code") or "").strip()
            if not re.match(r"^\d{6}$", code):
                continue
            act = str(p.get("action") or "")
            if any(a in act for a in _AVOID_ACTIONS):
                continue
            sc = p.get("score")
            if sc is None:
                continue
            rows.append({
                "key": key,
                "team": team_name.get(key, key),
                "code": code,
                "name": str(p.get("name") or ""),
                "score": sc,
                "action": act,
                "reason": str(p.get("reason") or ""),
                "entry": str(p.get("entry") or ""),
                "stop": str(p.get("stop") or ""),
                "horizon": str(p.get("horizon") or ""),
                "stance": it.get("stance"),
            })
            by_code.setdefault(code, [])
            if key not in by_code[code]:
                by_code[code].append(key)

    rows.sort(key=lambda r: (-r["score"], order.get(r["key"], 99), r["code"]))
    strong = [r for r in rows if r["score"] >= min_score]
    out = strong[:cap]
    if len(out) < min_rows:
        rest = [r for r in rows if r["score"] < min_score]
        out = out + rest[:max(0, min_rows - len(out))]
    for r in out:
        r["strong"] = r["score"] >= min_score
        r["co_teams"] = len(by_code.get(r["code"], []))   # 几家同时推同一只（共振是有效信息）
    return {
        "min_score": min_score,
        "items": out,
        "n_strong": len(strong),
        "n_scored": len(rows),
        "n_teams": len({r["key"] for r in rows}),
    }


def _row_to_item(row) -> dict:
    return {
        "status": str(row["status"] or "ok"),
        "stance": row["stance"],
        "position": row["position"],
        "verdict": row["verdict"],
        "market_read": row["market_read"],
        # 老库可能还没补这一列（只读连接走不到 ALTER）——取不到就是 None，不抛
        "score_note": row["score_note"] if "score_note" in row.keys() else None,
        "mainlines": _jload(row["mainlines_json"], []),
        # n-20261104：读的时候再归一化一次。库里的老行（评分功能上线前写入的）
        # 不会有 score，写库时的归一化也管不到它们；读侧兜一层，前端就只有一个数据形状。
        "picks": _norm_picks(_jload(row["picks_json"], [])),
        "watch": _jload(row["watch_json"], []),
        "risks": _jload(row["risks_json"], []),
        "report_md": row["report_md"],
        "model": row["model"],
        "elapsed_ms": row["elapsed_ms"],
        "err": row["err"],
        "created_at": row["created_at"],
    }


def rows_for_date(conn, date: str) -> dict:
    """{expert_key: item}：当天已落库的报告。表不存在 → 空字典（不是异常）。"""
    try:
        rows = conn.execute(
            "SELECT * FROM ai_expert_review WHERE review_date=?", (date,)).fetchall()
    except Exception as e:  # noqa: BLE001
        log.debug("[ai_expert] 读 %s 失败（可能表未建）：%s", date, e)
        return {}
    return {str(r["expert_key"]): _row_to_item(r) for r in rows}


def build_items(conn, date: str, with_report_key: str = "") -> list:
    """按配置顺序产出四家（**永远四条**）。

    没有当天报告 → status="missing"、其余字段为 None；
    本机没装专家包 → installed=False（这是「不可能有报告」的诚实原因，
    与「装好了但今天还没跑」必须区分开，否则用户会一直等一份永远不会来的报告）。
    `with_report_key` 命中的那条额外带 `report_md` 全文（列表接口不带全文，省带宽）。
    """
    rows = rows_for_date(conn, date)
    out = []
    for t in load_teams():
        it = {
            "key": t["key"],
            "name": t["name"],
            "tags": list(t["tags"]),
            "installed": bool(is_installed(t)),
            "status": "missing",
            "stance": None,
            "position": None,
            "verdict": None,
            "picks": [],
            "mainlines": [],
            "created_at": None,
            "elapsed_ms": None,
            "err": None,
        }
        r = rows.get(t["key"])
        if r:
            it.update({k: r.get(k) for k in (
                "status", "stance", "position", "verdict", "picks", "mainlines",
                "created_at", "elapsed_ms", "err", "market_read", "watch", "risks",
                # score_note 必须在这张白名单里：它是**白名单**，不在名单上的字段到这里
                # 就会被静默丢掉。2026-11-04 加口径字段时先漏了这一处，接口一直回 None，
                # 而库里和 _row_to_item 都是对的 —— 排查了半天是这里吃了字段。
                "score_note", "model")})
            it["status"] = r.get("status") or "ok"
        if with_report_key and t["key"] == with_report_key:
            it["report_md"] = (r or {}).get("report_md") or ""
        out.append(it)
    return out


# ---------------------------------------------------------------- 写


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


_TXT_COLS = ("status", "stance", "position", "verdict", "market_read", "score_note", "model", "err")
_JSON_COLS = (("mainlines", "mainlines_json"), ("picks", "picks_json"),
              ("watch", "watch_json"), ("risks", "risks_json"))


def write_review(conn, review_date: str, expert_key: str, data: dict) -> dict:
    """一家一天一份的 upsert（ON CONFLICT(review_date, expert_key) DO UPDATE）。

    为什么必须 upsert 而不是先 DELETE 再 INSERT：同一天 agent 可能重跑
    （补数据、换模型、修错别字），DELETE 方式会在两步之间留下「这一家今天没报告」
    的空窗，页面正好刷新就会看到假的「今日未跑」。
    """
    key = str(expert_key or "").strip()
    if not key:
        raise ValueError("expert_key 不能为空")
    date = resolve_date(conn, review_date)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise ValueError("日期必须是 YYYY-MM-DD，收到 %r" % (review_date,))
    team = team_by_key(key)
    name = str(data.get("expert_name") or team.get("name") or key)

    val = {}
    # picks 的 score 先归一化再序列化：库里只该有一种形状的分数（int 或 None）。
    if isinstance(data.get("picks"), list):
        data = dict(data)
        data["picks"] = _norm_picks(data["picks"])
    for c in _TXT_COLS:
        v = data.get(c)
        val[c] = None if v is None else str(v)
    val["status"] = val["status"] or "ok"
    for src, col in _JSON_COLS:
        v = data.get(src)
        if v is None or v == "":
            val[col] = None
        elif isinstance(v, str):
            # 已经是 JSON 字符串：只校验能否解析，不重新序列化（保持 agent 原样留痕）
            try:
                json.loads(v)
                val[col] = v
            except Exception:  # noqa: BLE001
                val[col] = json.dumps([v], ensure_ascii=False)
        else:
            val[col] = json.dumps(v, ensure_ascii=False)
    val["report_md"] = data.get("report_md")
    em = data.get("elapsed_ms")
    try:
        val["elapsed_ms"] = int(em) if em not in (None, "") else None
    except (TypeError, ValueError):
        val["elapsed_ms"] = None
    val["created_at"] = str(data.get("created_at") or _now())

    cols = ["review_date", "expert_key", "expert_name"] + list(_TXT_COLS) + \
           [c for _, c in _JSON_COLS] + ["report_md", "elapsed_ms", "created_at"]
    params = [date, key, name] + [val[c] for c in _TXT_COLS] + \
             [val[c] for _, c in _JSON_COLS] + \
             [val["report_md"], val["elapsed_ms"], val["created_at"]]
    sql = (
        "INSERT INTO ai_expert_review (%s) VALUES (%s) "
        "ON CONFLICT(review_date, expert_key) DO UPDATE SET %s"
        % (", ".join(cols), ", ".join(["?"] * len(cols)),
           ", ".join("%s=excluded.%s" % (c, c) for c in cols[2:]))
    )
    cur = conn.execute(sql, params)
    conn.commit()
    rid = cur.lastrowid
    if not rid:
        # DO UPDATE 时 lastrowid 不可靠 —— 回查真实 id，保证 CLI 打印的是库里那一行。
        try:
            rid = conn.execute(
                "SELECT id FROM ai_expert_review WHERE review_date=? AND expert_key=?",
                (date, key)).fetchone()[0]
        except Exception:  # noqa: BLE001
            rid = 0
    return {"id": rid, "review_date": date, "key": key, "expert_name": name,
            "verdict": val["verdict"], "status": val["status"]}


def read_report_file(path) -> tuple:
    """读 agent 交付的文件，返回 (payload dict, 原始格式 'json'|'md')。

    .json 走字段映射；.md 整篇进 report_md，verdict 取第一个非空行（去掉 # 标记）。
    为什么 md 只抽 verdict：结构化列硬凑只会产生假数据（把「## 主线」当成 stance），
    宁可留 NULL，也不让前端显示一个编出来的结论。
    """
    p = Path(str(path))
    if not p.is_file():
        raise FileNotFoundError("文件不存在：%s" % p)
    raw = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix.lower() == ".json":
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("JSON 顶层必须是对象（收到 %s）" % type(data).__name__)
        return data, "json"
    lines = [ln.strip() for ln in raw.replace("\r\n", "\n").split("\n")]
    verdict = ""
    for ln in lines:
        # 去掉标题/列表前缀。为什么用循环 lstrip 而不是 lstrip("#")：
        # agent 常写 `# ## 标题` 或 `#\t标题`，单次 lstrip("#") 会留下 "## 标题" / 前导空白，
        # 结果前端那一行结论显示成 "## 结论" —— 这是 QA 实测到的真实现象。
        t = ln.lstrip("# \t").strip()
        if t:
            verdict = t[:200]
            break
    return {"report_md": raw, "verdict": verdict or None, "status": "ok"}, "md"


def save_path(conn, path, date: str, key: str) -> dict:
    """`expert_desk.py --save` 的落库实现（CLI 与测试共用）。"""
    k = str(key or "").strip()
    if not k:
        raise ValueError("--key 不能为空（必须是 config/expert_teams.json 里的 key）")
    if not team_by_key(k):
        raise ValueError("未知 expert_key：%s（可用：%s）"
                         % (k, "、".join(t["key"] for t in load_teams())))
    payload, kind = read_report_file(path)
    rd = str(payload.get("review_date") or "").strip() or date
    if not rd or str(rd).lower() == "latest":
        rd = resolve_date(conn, date)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rd)):
        raise ValueError("review_date 必须是 YYYY-MM-DD，收到 %r" % (rd,))
    ensure_schema(conn)
    res = write_review(conn, rd, k, payload)
    res["format"] = kind
    return res


# ---------------------------------------------------------------- 候选池


def _f(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _pool_from_universe_scan(conn, date: str, limit: int) -> list:
    """首选：`stock_universe_scan_daily`（全市场扫描，带 0~1 分数 + 分层 + 当日行情）。

    它同时满足三件事：覆盖全市场、分数可比、有复盘日的行情可一起取。
    用 LEFT JOIN 而不是 INNER JOIN：行情缺一天时仍能给出代码与分数（信息不完整好过整节消失）。
    """
    rows = conn.execute(
        "SELECT s.code, s.name, s.score, s.rank_pct, s.tier, "
        "       d.close, d.pct, d.amount "
        "FROM stock_universe_scan_daily s "
        "LEFT JOIN stock_daily_snapshot d "
        "       ON d.trade_date = s.trade_date AND d.code = s.code "
        "WHERE s.trade_date = ? "
        "ORDER BY s.score DESC, s.rank_pct DESC LIMIT ?",
        (date, int(limit) * 4)).fetchall()
    out, seen = [], set()
    for r in rows:
        code = str(r["code"] or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append({
            "code": code, "name": r["name"] or "",
            "price": _f(r["close"]), "pct": _f(r["pct"]), "amount": _f(r["amount"]),
            "score": _f(r["score"]),
            "reason": "全市场扫描 %s 分档%s" % (
                ("%.3f" % r["score"]) if r["score"] is not None else "-",
                ("·" + str(r["tier"])) if r["tier"] else ""),
        })
        if len(out) >= limit:
            break
    return out


def _pool_from_strategy_picks(conn, date: str, limit: int) -> list:
    """次选：`stock_strategy_picks`（策略模板的票，带 reasons）。无日期列，取最新一批。"""
    rows = conn.execute(
        "SELECT template, code, name, score, reasons FROM stock_strategy_picks "
        "ORDER BY score DESC LIMIT ?", (int(limit) * 6,)).fetchall()
    out, seen = [], set()
    for r in rows:
        code = str(r["code"] or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        try:
            rs = json.loads(r["reasons"] or "[]")
            reason = "、".join(str(x) for x in rs)[:80] if isinstance(rs, list) else str(rs)[:80]
        except Exception:  # noqa: BLE001
            reason = str(r["reasons"] or "")[:80]
        out.append({
            "code": code, "name": r["name"] or "",
            "price": None, "pct": None, "amount": None,
            "score": _f(r["score"]),
            "reason": ("%s：%s" % (r["template"] or "", reason)).strip("："),
        })
        if len(out) >= limit:
            break
    return out


def _pool_from_signals(conn, date: str, limit: int) -> list:
    """第三选：`stock_signals`（买卖信号，带 buy_reasons）。"""
    rows = conn.execute(
        "SELECT code, name, buy_score, buy_reasons FROM stock_signals "
        "WHERE date = ? ORDER BY buy_score DESC LIMIT ?", (date, int(limit) * 4)).fetchall()
    out, seen = [], set()
    for r in rows:
        code = str(r["code"] or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        try:
            rs = json.loads(r["buy_reasons"] or "[]")
            reason = "、".join(str(x) for x in rs)[:80] if isinstance(rs, list) else str(rs)[:80]
        except Exception:  # noqa: BLE001
            reason = str(r["buy_reasons"] or "")[:80]
        out.append({
            "code": code, "name": r["name"] or "",
            "price": None, "pct": None, "amount": None,
            "score": _f(r["buy_score"]), "reason": reason,
        })
        if len(out) >= limit:
            break
    return out


def _pool_from_sel_pool(conn, date: str, limit: int) -> list:
    """第四选：`sel_pool`（选股池，asof 是 YYYYMMDD 字符串，无分数但带振幅/量比）。"""
    asof = date.replace("-", "")
    rows = conn.execute(
        "SELECT code, name, close, amt, tier FROM sel_pool WHERE asof = ? LIMIT ?",
        (asof, int(limit) * 4)).fetchall()
    out, seen = [], set()
    for r in rows:
        code = str(r["code"] or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append({
            "code": code, "name": r["name"] or "",
            "price": _f(r["close"]), "pct": None, "amount": _f(r["amt"]),
            "score": None, "reason": "选股池 %s" % (r["tier"] or "-"),
        })
        if len(out) >= limit:
            break
    return out


def _pool_from_quotes(conn, date: str, limit: int) -> list:
    """兜底（需求指定）：`a_shares_universe` + `market_quotes` 按涨幅/成交额取前 N。

    注意市场快照 `market_quotes` 只覆盖部分股票（实测几十条），所以这条兜底
    给出的池子可能不足 40 条 —— 这是**如实**的：宁可少给，也不去拿已退市
    （`stock_daily_snapshot` 里带「摘牌」字样的票涨 400%）来凑数。
    """
    rows = conn.execute(
        "SELECT u.code, u.name, q.price, q.pct FROM a_shares_universe u "
        "JOIN market_quotes q ON q.symbol = u.code "
        "WHERE q.pct IS NOT NULL "
        "ORDER BY q.pct DESC LIMIT ?", (int(limit),)).fetchall()
    out = []
    for r in rows:
        out.append({
            "code": str(r["code"] or "").strip(), "name": r["name"] or "",
            "price": _f(r["price"]), "pct": _f(r["pct"]), "amount": None,
            "score": None, "reason": "市场快照涨幅榜",
        })
    if out:
        return out
    rows = conn.execute(
        "SELECT code, name, close, pct, amount FROM stock_daily_snapshot "
        "WHERE trade_date = ? AND pct IS NOT NULL "
        "ORDER BY amount DESC LIMIT ?", (date, int(limit))).fetchall()
    return [{
        "code": str(r["code"] or "").strip(), "name": r["name"] or "",
        "price": _f(r["close"]), "pct": _f(r["pct"]), "amount": _f(r["amount"]),
        "score": None, "reason": "当日成交额榜（无扫描分，仅供人工筛）",
    } for r in rows]


# 依次尝试的取池源。每条都独立 try：任何一张表缺失/字段改名都只跳过这一步。
_POOL_SOURCES = (
    ("stock_universe_scan_daily", _pool_from_universe_scan),
    ("stock_strategy_picks", _pool_from_strategy_picks),
    ("stock_signals", _pool_from_signals),
    ("sel_pool", _pool_from_sel_pool),
    ("a_shares_universe+market_quotes", _pool_from_quotes),
)


def candidate_pool(conn, date: str, limit: int = 40) -> dict:
    """候选池（供专家团选股用）。返回 {rows, source, note}。

    **永不抛异常**：任何源失败都返回空池 + 一句人话原因，
    让 brief 可以「跳过这一节并在头部注明」，而不是整份 informational 包生成失败。
    """
    for name, fn in _POOL_SOURCES:
        try:
            rows = fn(conn, date, limit)
        except Exception as e:  # noqa: BLE001
            log.warning("[ai_expert] 候选池源 %s 取数失败：%s", name, e)
            continue
        if rows:
            return {"rows": rows[:limit], "source": name,
                    "note": "来自 %s，按现有打分/成交额排序取前 %d 条" % (name, limit)}
    return {"rows": [], "source": "", "note": "库里没有可用的选股/扫描结果，本节已跳过"}
