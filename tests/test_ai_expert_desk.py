# -*- coding: utf-8 -*-
"""四家专家团每日评估（`ai_expert_review`）的契约测试。

## 这个功能「坏掉」时会怎么坏（也就是这组测试在钉什么）

1. **报告回来了但库里是两份** —— 同一天 agent 重跑一次，页面上出现两条，用户不知道信哪个。
   钉法：`UNIQUE(review_date, expert_key)` + upsert 后行数恒为 1。
2. **报告写进去了，但字段错位** —— 结构化列（picks/mainlines）与 text 列一旦错位，
   前端会把「主线板块」渲染成「一句话结论」，看起来像专家团说的话，其实是我们自己的 bug。
   钉法：JSON 落库后逐字段回读比对。
3. **只交了 Markdown 的报告整篇丢失** —— agent 常直接写一篇 md 回来。
   钉法：md 落库后 `report_md` 全文相等、`verdict` 取到首行正文而不是 `#` 符号。
4. **某一家没报告时整块消失** —— 前端「四家恒在」是硬需求：缺的那家本身就是信息。
   钉法：空库调接口，断言仍是 4 条、status=missing、顺序与配置一致。
5. **没装专家包却显示「今日未跑」** —— 这会让用户一直等一份永远不会来的报告。
   钉法：pack 目录不存在时 `installed=False`。

## 为什么全程用临时库

`VIEWER_DB` / 显式连接参数都指向 `tmp_path`。生产库 `viewer/app.db` 有 1.5GB 且正在被
8000+ 端口的生产服务读写，测试碰它一次就可能留下假报告（`tests/test_prod_write_guard.py`
记录过同类事故）。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _detach_prod_log_handlers() -> list:
    """导入 `viewer.server` 会往根 logger 挂生产日志 FileHandler —— 先摘掉，
    否则本文件的每行日志都会写进 viewer/logs/ 里，污染事后排查（见
    tests/test_offrepo… 同类教训：test_ai_review_fallback.py 的同一段注释）。"""
    root = logging.getLogger()
    removed = [h for h in list(root.handlers) if isinstance(h, logging.FileHandler)]
    for h in removed:
        root.removeHandler(h)
    return removed


from viewer import ai_expert as AX  # noqa: E402
from viewer import server as S  # noqa: E402

_DETACHED = _detach_prod_log_handlers()

DATE = "2026-11-03"


class _FakeDB:
    """`DB` 的最小替身：`api_ai_expert()` 只用到 `.conn`。"""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "ai_expert.db"), check_same_thread=False, timeout=5)
    c.row_factory = sqlite3.Row
    AX.ensure_schema(c)
    yield c
    c.close()


@pytest.fixture()
def api_env(monkeypatch, conn):
    """把 server 的 DB 换成临时库，并把「本机安装了哪些专家包」固定下来。

    固定 EXPERT_ROOT 到一个不存在的目录：测试必须与「这台机器恰好装了什么」无关，
    否则换台机器跑同一份测试，installed 的期望值就变了。
    """
    monkeypatch.setattr(S, "DB", _FakeDB(conn))
    monkeypatch.setattr(AX, "EXPERT_ROOT", Path("/nonexistent-expert-root-for-test"))
    return conn


# ---------------------------------------------------------------- 1. 建表 + upsert


def test_schema_is_idempotent_and_upsert_keeps_one_row(tmp_path):
    """建表幂等；同一天同一家重复回写只留一行（内容以最后一次为准）。"""
    p = str(tmp_path / "schema.db")
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    AX.ensure_schema(c)
    AX.ensure_schema(c)          # 第二次不许抛（CREATE ... IF NOT EXISTS）
    AX.ensure_schema(c)

    tables = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ai_expert_review" in tables
    idx = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_ai_expert_date" in idx, "日期索引缺失（API 每次按日期整批取 4 条）"

    AX.write_review(c, DATE, "stock-partner", {"verdict": "第一版", "stance": "偏多"})
    AX.write_review(c, DATE, "stock-partner", {"verdict": "第二版", "stance": "偏空"})
    rows = c.execute("SELECT * FROM ai_expert_review WHERE review_date=?", (DATE,)).fetchall()
    assert len(rows) == 1, "同一天同一家出现了 %d 行 —— UNIQUE/upsert 没生效" % len(rows)
    assert rows[0]["verdict"] == "第二版" and rows[0]["stance"] == "偏空"
    # 另一家同一天可以各有一行（UNIQUE 是两列的组合，不是单列）
    AX.write_review(c, DATE, "five-wave", {"verdict": "另一家"})
    assert c.execute("SELECT COUNT(*) FROM ai_expert_review").fetchone()[0] == 2
    c.close()


# ---------------------------------------------------------------- 2. JSON 落库


def test_save_from_json_maps_every_column(conn, tmp_path):
    """`--save` 读 JSON：结构化字段逐列落对位置，缺的列存 NULL（不是空串/0）。"""
    src = tmp_path / "stock-partner.json"
    src.write_text(json.dumps({
        "review_date": DATE,
        "stance": "偏多",
        "position": "半仓以内",
        "verdict": "指数缩量回踩 20 日线，主线仍在算力",
        "market_read": "两市成交 1.6 万亿，结构分化",
        "mainlines": [{"sector": "算力", "stage": "加速", "note": "资金连续 3 日净流入"}],
        "picks": [{"code": "600519", "name": "贵州茅台", "action": "买",
                   "reason": "缩量回踩", "entry": "1450-1480", "stop": "1400", "horizon": "5日"}],
        "watch": ["下周美联储议息"],
        "risks": ["量能不足"],
        "report_md": "# 完整报告\n正文",
        "model": "expert-team",
        "elapsed_ms": 12345,
    }, ensure_ascii=False), encoding="utf-8")

    res = AX.save_path(conn, src, "latest", "stock-partner")
    assert res["format"] == "json" and res["review_date"] == DATE

    row = conn.execute("SELECT * FROM ai_expert_review WHERE expert_key='stock-partner'").fetchone()
    assert row["expert_name"] == "腾讯自选股股票投研专家团", "expert_name 必须从配置补齐"
    assert row["stance"] == "偏多" and row["position"] == "半仓以内"
    assert row["verdict"].startswith("指数缩量回踩")
    assert row["market_read"].startswith("两市成交")
    assert json.loads(row["mainlines_json"])[0]["sector"] == "算力"
    assert json.loads(row["picks_json"])[0]["code"] == "600519"
    assert json.loads(row["watch_json"]) == ["下周美联储议息"]
    assert json.loads(row["risks_json"]) == ["量能不足"]
    assert row["report_md"] == "# 完整报告\n正文"
    assert row["model"] == "expert-team" and row["elapsed_ms"] == 12345
    assert row["status"] == "ok" and row["created_at"], "created_at 必须自动补"

    # 缺字段的另一家：text 列必须是 NULL，不能是空串（前端据此区分「没写」与「写了空」）
    src2 = tmp_path / "five-wave.json"
    src2.write_text(json.dumps({"verdict": "只有一句话"}), encoding="utf-8")
    AX.save_path(conn, src2, DATE, "five-wave")
    r2 = conn.execute("SELECT * FROM ai_expert_review WHERE expert_key='five-wave'").fetchone()
    for col in ("stance", "position", "market_read", "mainlines_json", "picks_json",
                "watch_json", "risks_json", "report_md", "model", "elapsed_ms"):
        assert r2[col] is None, "%s 应为 NULL，实际 %r" % (col, r2[col])


# ---------------------------------------------------------------- 3. Markdown 落库


def test_save_from_markdown_extracts_verdict_and_keeps_full_text(conn, tmp_path):
    """`--save` 读 Markdown：整篇进 report_md，verdict 取第一个非空行并去掉 `#`。"""
    md = "\n\n## 2026-11-03 五浪结构：主升 3 浪未走完\n\n- 上证 3080 一带是 2 浪底\n- 关注量能\n"
    src = tmp_path / "five-wave.md"
    src.write_text(md, encoding="utf-8")

    res = AX.save_path(conn, src, DATE, "five-wave")
    assert res["format"] == "md"
    row = conn.execute("SELECT * FROM ai_expert_review WHERE expert_key='five-wave'").fetchone()
    assert row["report_md"] == md, "全文必须与文件逐字节一致（前端展开显示的就是它）"
    assert row["verdict"] == "2026-11-03 五浪结构：主升 3 浪未走完"
    assert not row["verdict"].startswith("#")
    # 结构化列不许「猜」：md 里没有结构化信息，就留 NULL
    assert row["picks_json"] is None and row["stance"] is None


def test_save_rejects_unknown_key_and_missing_arg(conn, tmp_path):
    """未知 key / 空 key 必须报错而不是静默写进一个前端永远看不到的行。"""
    src = tmp_path / "x.md"
    src.write_text("# hi", encoding="utf-8")
    with pytest.raises(ValueError):
        AX.save_path(conn, src, DATE, "no-such-team")
    with pytest.raises(ValueError):
        AX.save_path(conn, src, DATE, "   ")
    assert conn.execute("SELECT COUNT(*) FROM ai_expert_review").fetchone()[0] == 0


# ---------------------------------------------------------------- 4. 接口：四家恒在


def test_api_always_returns_four_in_config_order(api_env):
    """空库也返回 4 条，顺序与 config/expert_teams.json 完全一致，全部 missing。"""
    d = S.api_ai_expert()
    assert d["ok"] is True
    keys = [x["key"] for x in d["items"]]
    assert keys == [t["key"] for t in AX.load_teams()], "顺序必须就是前端展示顺序"
    assert len(keys) == 4
    assert all(x["status"] == "missing" for x in d["items"])
    # 没装专家包（EXPERT_ROOT 指到不存在的目录）
    assert all(x["installed"] is False for x in d["items"])
    assert all(x["picks"] == [] and x["mainlines"] == [] for x in d["items"])


def test_api_missing_vs_ok_and_report_md_only_on_request(api_env):
    """有报告的 status=ok 并带出立场/仓位/票；全文只在该 key 被请求时返回。"""
    conn = api_env
    AX.write_review(conn, DATE, "stock-partner", {
        "stance": "偏多", "position": "半仓以内", "verdict": "主线仍是算力",
        "picks": [{"code": "600519", "name": "贵州茅台", "action": "买"}],
        "mainlines": [{"sector": "算力", "stage": "加速"}],
        "report_md": "# 全文\n很长很长",
        "elapsed_ms": 8000,
    })

    d = S.api_ai_expert(date=DATE)
    by = {x["key"]: x for x in d["items"]}
    assert d["date"] == DATE
    assert by["stock-partner"]["status"] == "ok"
    assert by["stock-partner"]["stance"] == "偏多"
    assert by["stock-partner"]["verdict"] == "主线仍是算力"
    assert by["stock-partner"]["picks"][0]["code"] == "600519"
    assert "report_md" not in by["stock-partner"], "列表接口不该带全文（四份几万字）"
    assert by["five-wave"]["status"] == "missing"
    assert by["five-wave"]["stance"] is None

    # 指定 key → 只有那一条带全文
    d2 = S.api_ai_expert(date=DATE, key="stock-partner")
    by2 = {x["key"]: x for x in d2["items"]}
    assert by2["stock-partner"]["report_md"] == "# 全文\n很长很长"
    assert "report_md" not in by2["five-wave"]

    # 不传 date：用库里 MAX(review_date)
    d3 = S.api_ai_expert()
    assert d3["date"] == DATE, "不传 date 应取库里已有的最大 review_date"
    assert d3["latest_date"], "latest_date 必须回给前端（日期下拉要用）"


# ---------------------------------------------------------------- 5. 安装判定


def test_installed_false_when_pack_missing(monkeypatch, tmp_path):
    """专家包目录/lead 文件不存在 → installed=False（前端显示「专家包未安装」）。"""
    monkeypatch.setattr(AX, "EXPERT_ROOT", tmp_path / "packs")
    team = {"key": "stock-partner", "pack": "stock-partner-team",
            "lead": "agents/stock-partner-lead.md", "members": []}
    assert AX.is_installed(team) is False

    # 只建目录、不建 lead：半装状态也必须算「没装好」（否则自动化会去跑一家没有提示词的团）
    (tmp_path / "packs" / "stock-partner-team" / "agents").mkdir(parents=True)
    assert AX.is_installed(team) is False

    (tmp_path / "packs" / "stock-partner-team" / "agents" / "stock-partner-lead.md").write_text(
        "# lead", encoding="utf-8")
    assert AX.is_installed(team) is True

    # pack 为空（需求里 lishu-pro 就是这种情况）
    assert AX.is_installed({"key": "lishu-pro", "pack": "", "lead": ""}) is False


# ---------------------------------------------------------------- 6. 候选池容错


def test_candidate_pool_never_raises_on_empty_db(conn):
    """空库取候选池：不抛异常，返回空池 + 一句人话原因（brief 才能「跳过本节并注明」）。"""
    pool = AX.candidate_pool(conn, DATE, limit=40)
    assert pool["rows"] == []
    assert pool["note"], "必须给出一句可写进 brief 头部的说明，而不是空白"
    assert "已跳过" in pool["note"]


# ---------------------------------------------------------------- 7. 信息包 CSV（QA 回归）


def _pool(n: int = 3) -> dict:
    return {"rows": [{
        "code": "60051%d" % i, "name": "测试股%d" % i, "price": 10.5 + i,
        "pct": 1.25, "amount": 2652907402.12, "score": 0.912345,
        "reason": "全市场扫描",
    } for i in range(n)], "source": "stock_universe_scan_daily", "note": "测试"}


def test_universe_csv_has_no_double_carriage_return(tmp_path):
    """CSV 不许出现 `\\r\\r\\n`。

    来历（QA 独立验收到的不只是"多几个空行"）：`csv.writer` 默认行结束符就是 `\\r\\n`，
    再用文本模式 `write_text` 写，Python 会把 `\\n` 再翻一次 → 每行 `\\r\\r\\n`。
    实测 40 行数据被读成 81 行、pandas 拿到 40 行真数据 + 40 行 NaN。
    """
    import csv as _csv
    import expert_desk as ED

    p = tmp_path / "u.csv"
    n = ED._write_universe_csv(p, _pool(40))
    assert n == 40

    raw = open(p, "rb").read()
    assert raw.count(b"\r\r\n") == 0, "出现 \\r\\r\\n（双重换行翻译）"
    assert b"\xef\xbb\xbf" in raw[:5], "缺 UTF-8 BOM（Excel 打开会乱码）"

    with open(p, encoding="utf-8-sig", newline="") as f:
        rows = list(_csv.reader(f))
    assert len(rows) == 41, "表头 + 40 数据行应读成 41 行，实际 %d 行（空行没被消掉）" % len(rows)
    assert rows[0][0] == "代码"
    # 单位必须写在表头里：brief 那张 markdown 表是「成交额(亿)」，CSV 存的是元
    assert rows[0][4] == "成交额(元)", "成交额列没写单位，agent 会按亿读，差 1e8"
    assert all(len(r) == 7 for r in rows), "有空行被 csv.reader 读成 [] 了"


# ---------------------------------------------------------------- 8. 信息包质量门槛


def test_brief_quality_flags_missing_sections_and_short_body():
    """门槛本身：缺小节 / 字数不足都必须被点名（这就是 04:00 自动化能看见的那个信号）。"""
    import expert_desk as ED

    ok_body = ("## 一、市场环境\n" + "x" * 9000 +
               "\n## 二、主线板块\n龙虎榜\n## 八、本系统的实证边界\n【上次荐股的结果】")
    assert ED._brief_quality(ok_body) == []

    bad = ED._brief_quality("## 一、市场环境\n太短了")
    joined = "；".join(bad)
    assert "缺少小节：主线板块" in joined
    assert "上次荐股" in joined            # review_score 缺失时整段消失 → 必须点名
    assert "实证边界" in joined            # evidence 缺失时整节消失 → 必须点名
    assert "门槛" in joined                # 字数门槛也要报


def test_brief_exits_3_and_keeps_old_file_when_degraded(monkeypatch, tmp_path, conn):
    """残包必须 exit 3、**不覆盖**旧文件、不留 .tmp。

    反向场景同样重要：旧实现是 exit 0 + 短包覆盖同名文件 —— 04:00 的定时任务永远发现不了降级。
    """
    import expert_desk as ED
    from viewer import ai_review as AR

    monkeypatch.setattr(ED, "_conn", lambda: conn)
    monkeypatch.setattr(ED, "BRIEF_DIR", tmp_path / "_expert")
    monkeypatch.setattr(AR, "build_context", lambda *a, **k: {"review_date": DATE})
    monkeypatch.setattr(AR, "_render_user", lambda ctx: "## 一、市场环境\n太短了，缺了两大节")

    d = tmp_path / "_expert"
    d.mkdir()
    final = d / ("brief_%s.md" % DATE)
    final.write_text("旧的完整信息包（不许被残包覆盖）", encoding="utf-8")

    assert ED.cmd_brief(DATE) == 3
    assert final.read_text(encoding="utf-8") == "旧的完整信息包（不许被残包覆盖）"
    assert not (d / ("brief_%s.md.tmp" % DATE)).exists(), "残包的 .tmp 必须删掉"
    assert not (d / ("universe_%s.csv" % DATE)).exists(), "brief 不合格时不许留下新 CSV（防旧 brief + 新 csv 混搭）"


def test_brief_replaces_atomically_when_complete(monkeypatch, tmp_path, conn):
    """合格包：正常替换旧文件，且 .tmp 不残留。"""
    import expert_desk as ED
    from viewer import ai_review as AR

    body = ("## 一、市场环境\n" + "x" * 9000 +
            "\n## 二、主线板块\n龙虎榜\n## 八、本系统的实证边界\n【上次荐股的结果】")
    monkeypatch.setattr(ED, "_conn", lambda: conn)
    monkeypatch.setattr(ED, "BRIEF_DIR", tmp_path / "_expert")
    monkeypatch.setattr(AR, "build_context", lambda *a, **k: {"review_date": DATE})
    monkeypatch.setattr(AR, "_render_user", lambda ctx: body)

    d = tmp_path / "_expert"
    d.mkdir()
    final = d / ("brief_%s.md" % DATE)
    final.write_text("旧内容", encoding="utf-8")

    assert ED.cmd_brief(DATE) == 0
    txt = final.read_text(encoding="utf-8")
    assert "旧内容" not in txt and "市场环境" in txt
    assert not (d / ("brief_%s.md.tmp" % DATE)).exists(), "brief 的 .tmp 必须已被 replace 走"
    assert not (d / ("universe_%s.csv.tmp" % DATE)).exists(), "csv 的 .tmp 必须已被 replace 走"
    # 空库 → 候选池为空，CSV 仍写出（只有表头），行数 0
    with open(d / ("universe_%s.csv" % DATE), encoding="utf-8-sig") as f:
        assert len(f.read().strip().splitlines()) == 1


def test_brief_keeps_both_files_when_csv_write_fails(monkeypatch, tmp_path, conn):
    """CSV 写失败 → exit 3，md 与 csv **都保持旧内容**，两个 .tmp 都不留。

    为什么要单独钉这条：brief 与 csv 是一对（brief 里写着「候选池前 N 条」），
    「新 brief + 旧 csv」这种组合**不会报错**，只会让 agent 按新 brief 的条数去读一份旧名单。
    所以 CSV 也必须走「先 .tmp 再 replace」，且失败时不能有任何一半被换掉。
    这条分支是 2026-11-04 那次重构新引入的，之前只有人工注入（把 csv.tmp 建成目录）验过。
    """
    import expert_desk as ED
    from viewer import ai_review as AR

    body = ("## 一、市场环境\n" + "x" * 9000 +
            "\n## 二、主线板块\n龙虎榜\n## 八、本系统的实证边界\n【上次荐股的结果】")
    monkeypatch.setattr(ED, "_conn", lambda: conn)
    monkeypatch.setattr(ED, "BRIEF_DIR", tmp_path / "_expert")
    monkeypatch.setattr(AR, "build_context", lambda *a, **k: {"review_date": DATE})
    monkeypatch.setattr(AR, "_render_user", lambda ctx: body)

    def _boom(*a, **k):
        raise OSError("boom: 磁盘写满 / 权限不足（测试注入）")
    monkeypatch.setattr(ED, "_write_universe_csv", _boom)

    d = tmp_path / "_expert"
    d.mkdir()
    md = d / ("brief_%s.md" % DATE)
    csv_p = d / ("universe_%s.csv" % DATE)
    md.write_text("旧的完整信息包", encoding="utf-8")
    csv_p.write_text("旧,CSV,表头\n1,2,3\n", encoding="utf-8")

    assert ED.cmd_brief(DATE) == 3
    # 两半都不许被换掉 —— 只换一半比都不换更危险（错配且无迹可循）
    assert md.read_text(encoding="utf-8") == "旧的完整信息包"
    assert csv_p.read_text(encoding="utf-8") == "旧,CSV,表头\n1,2,3\n"
    assert not (d / ("brief_%s.md.tmp" % DATE)).exists(), "失败路径也必须清掉 md 的 .tmp"
    assert not (d / ("universe_%s.csv.tmp" % DATE)).exists(), "失败路径也必须清掉 csv 的 .tmp"


# ---------------------------------------------------------------- 9. md verdict 前缀


def test_markdown_verdict_strips_nested_heading_prefix(conn, tmp_path):
    """`#  ## x` / `#\\tx` 这类前缀必须全去掉（QA 实测残留 `##`）。"""
    src = tmp_path / "x.md"
    src.write_text("#  ## 结论：三浪未走完\n\n正文", encoding="utf-8")
    AX.save_path(conn, src, DATE, "five-wave")
    row = conn.execute("SELECT verdict FROM ai_expert_review WHERE expert_key='five-wave'").fetchone()
    assert row["verdict"] == "结论：三浪未走完", "前缀没清干净：%r" % (row["verdict"],)


# ---------------------------------------------------------------- 10. 空库首次 GET 能自愈


def test_api_creates_table_on_first_get_without_prior_schema(tmp_path):
    """全新的空库（表还不存在）第一次 GET 也不许 500 —— 但要**先探测再建**，不每请求 executescript。"""
    fresh = sqlite3.connect(str(tmp_path / "brand_new.db"), check_same_thread=False)
    fresh.row_factory = sqlite3.Row
    assert AX.table_ready(fresh) is False, "全新库里不该有这张表"

    old = S.DB
    try:
        S.DB = _FakeDB(fresh)
        d = S.api_ai_expert()
    finally:
        S.DB = old
    assert d["ok"] is True and len(d["items"]) == 4
    assert AX.table_ready(fresh) is True, "空库首次 GET 应把表建出来，否则页面永远 4 条 missing"
    fresh.close()


# ---------------------------------------------------------------- 11. 评分与合并榜（2026-11-04）


def test_norm_score_accepts_every_score_syntax_agents_actually_use():
    """各家写的分数形式不统一：85 / "85分" / "约 88" / 85.6 都要落成同一个整数。

    为什么这条重要：分数是前端排序和「≥85」筛选的唯一依据。一个 "85分" 漏成字符串，
    这只票在榜上就会掉到 0 分或被按字典序排到最后 —— 页面看起来有票，其实是错的。
    """
    assert AX._norm_score(85) == 85
    assert AX._norm_score(85.0) == 85
    assert AX._norm_score("85") == 85
    assert AX._norm_score("85分") == 85
    assert AX._norm_score("约 88 分") == 88
    assert AX._norm_score("88.6") == 89, "四舍五入到整数，不留小数"
    # 越界钳到 0~100：agent 偶尔写 120 分或 -5 分，前端进度/配色会崩
    assert AX._norm_score(120) == 100
    assert AX._norm_score(-5) == 0
    # 拿不到分数必须是 None，不能是 0 —— 0 是「极其不合格」这个结论，None 才是「没打分」
    for bad in (None, "", "暂无", "N/A", True, False, [], {}):
        assert AX._norm_score(bad) is None, "输入 %r 应归一化为 None" % (bad,)


def test_board_sorts_filters_and_marks_strong(conn):
    """合并榜：只收可执行、有 6 位代码、打了分的票；按分数降序；≥85 标 strong。"""
    AX.write_review(conn, DATE, "stock-partner", {"picks": [
        {"code": "600900", "name": "长江电力", "action": "持有", "reason": "逆势红盘", "score": "88分"},
        {"code": "600642", "name": "申能股份", "action": "回踩买入", "reason": "电力双属性", "score": 92},
        {"code": "000591", "name": "太阳能", "action": "规避", "reason": "涨停追高", "score": 95},
        {"code": "600", "name": "残缺代码", "action": "买入", "reason": "x", "score": 99},
        {"code": "600406", "name": "国电南瑞", "action": "回踩买入", "reason": "电算协同"},       # 没打分
    ]})
    AX.write_review(conn, DATE, "finance-suite", {"picks": [
        {"code": "600900", "name": "长江电力", "action": "买入", "reason": "股息率", "score": 86},
        {"code": "601988", "name": "中国银行", "action": "持有", "reason": "低估", "score": 70},
    ]})

    items = AX.build_items(conn, DATE)
    b = AX.build_board(items, min_score=85, min_rows=5, cap=15)
    got = [(r["code"], r["score"], r["strong"]) for r in b["items"]]

    assert ("000591", 95, True) not in got, "规避类不是推荐个股，不该进榜"
    assert all(len(r["code"]) == 6 for r in b["items"]), "非 6 位代码点不开个股，不许进榜"
    assert all(r["score"] is not None for r in b["items"]), "没打分的票不许进榜"
    # 92 > 88 > 86 严格降序
    assert got[:3] == [("600642", 92, True), ("600900", 88, True), ("600900", 86, True)]
    assert b["n_strong"] == 3
    # 达标 3 只不足 5 只 → 用次高分补足；但库里可执行且打了分的只有 4 只，所以补到 4 为止
    # （补不满也不许造票），且补进来的必须标成非 strong —— 不能把 70 分伪装成达标
    assert len(b["items"]) == 4
    assert b["items"][3]["code"] == "601988" and b["items"][3]["strong"] is False
    # 两家共推 600900：co_teams=2（共振是有效信息，页面上会显示「2 家共推」）
    assert all(r["co_teams"] == 2 for r in b["items"] if r["code"] == "600900")
    assert b["n_scored"] == 4 and b["n_teams"] == 2


def test_board_caps_rows_and_reports_true_strong_count(conn):
    """强推超过上限时截断显示，但 n_strong 仍是**真实达标数**（不许因截断而少报）。"""
    picks = [{"code": "%06d" % (600000 + i), "name": "票%d" % i, "action": "回踩买入",
              "reason": "r", "score": 90 + (i % 10)} for i in range(20)]
    AX.write_review(conn, DATE, "stock-partner", {"picks": picks})
    b = AX.build_board(AX.build_items(conn, DATE), cap=15)
    assert len(b["items"]) == 15
    assert b["n_strong"] == 20, "n_strong 必须是真实达标数，否则页面会少报机会"
    assert all(r["strong"] for r in b["items"]), "截断应保留高分（已按降序排）"
    assert b["items"][0]["score"] >= b["items"][-1]["score"]


def test_board_picks_are_normalized_on_write_and_read(conn):
    """写库就把分数收成 int：库里不许出现 "88分" 这类字符串（前端排序依据只有这个字段）。"""
    AX.write_review(conn, DATE, "stock-partner", {"picks": [
        {"code": "600900", "name": "长江电力", "action": "持有", "score": "88分"},
    ]})
    raw = json.loads(conn.execute(
        "SELECT picks_json FROM ai_expert_review WHERE review_date=?", (DATE,)).fetchone()[0])
    assert raw[0]["score"] == 88 and isinstance(raw[0]["score"], int)


def test_api_returns_board_and_old_reports_stay_honest(api_env):
    """接口必须回 board；旧报告（无评分）时 board 为空但 items 一条不少 —— 不许编分数。"""
    conn = api_env
    d0 = S.api_ai_expert(date=DATE)
    assert "board" in d0 and d0["board"]["items"] == []
    assert d0["board"]["min_score"] == AX.MIN_SCORE

    AX.write_review(conn, DATE, "five-wave", {"picks": [
        {"code": "600519", "name": "贵州茅台", "action": "观察", "reason": "浪型", "score": 87},
    ], "report_md": "# 全文"})
    d1 = S.api_ai_expert(date=DATE)
    assert d1["board"]["items"][0]["code"] == "600519"
    assert d1["board"]["items"][0]["key"] == "five-wave"
    # 全文仍然只在该 key 被请求时返回（榜单不许把几万字一起带出来）
    assert "report_md" not in {k: v for k, v in d1["board"]["items"][0].items()}




# ---------------------------------------------------------------- 12. 评分口径（2026-11-04）

def test_build_items_carries_score_note(conn):
    """每家一句「评分口径」必须穿过 build_items 的字段白名单，一路到接口。

    为什么单独测这条：build_items 里是**白名单**（不是把 row 全量透传），
    新增字段漏加进白名单时，库里有、_row_to_item 也读得到，但接口静默回 None ——
    2026-11-04 加 score_note 时就踩了这个坑，前端那句口径一直不显示，查了半天。
    """
    AX.write_review(conn, DATE, "stock-partner", {
        "picks": [{"code": "600900", "name": "长江电力", "action": "持有", "score": 88}],
        "score_note": "以量价结构为主，85 分 = 当日可下单",
    })
    it = [x for x in AX.build_items(conn, DATE) if x["key"] == "stock-partner"][0]
    assert it["score_note"] == "以量价结构为主，85 分 = 当日可下单"
    assert it["picks"][0]["score"] == 88, "口径带上了，picks 也不能丢"


def test_ensure_schema_gives_old_table_the_score_note_column(tmp_path):
    """老库（表里没有 score_note 列）必须自动补列。

    为什么必须有迁移：CREATE TABLE IF NOT EXISTS 对已存在的表**什么都不做**，
    而这张表存着历史报告、不能删表重建。不补列的话，下一次 --save 会直接报
    「no column named score_note」整体失败 —— 那不是少一个字段，是全天四家都落不了库。
    """
    import sqlite3
    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE ai_expert_review (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        review_date TEXT NOT NULL, expert_key TEXT NOT NULL, expert_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ok', stance TEXT, position TEXT, verdict TEXT,
        market_read TEXT, mainlines_json TEXT, picks_json TEXT, watch_json TEXT,
        risks_json TEXT, report_md TEXT, model TEXT, elapsed_ms INTEGER, err TEXT,
        created_at TEXT, UNIQUE(review_date, expert_key));
    """)
    c.commit()
    assert "score_note" not in {r[1] for r in c.execute("PRAGMA table_info(ai_expert_review)")}

    AX.ensure_schema(c)
    assert "score_note" in {r[1] for r in c.execute("PRAGMA table_info(ai_expert_review)")}

    # 补列的目的就是让写库能继续用
    r = AX.write_review(c, DATE, "stock-partner", {"picks": [], "score_note": "口径一句话"})
    assert r.get("id")
    row = c.execute("SELECT score_note FROM ai_expert_review WHERE review_date=?", (DATE,)).fetchone()
    assert row[0] == "口径一句话"
