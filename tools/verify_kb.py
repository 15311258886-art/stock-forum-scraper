"""隔离验证「跟我学习 → 中短线炒股」知识库链路。

不触碰生产库：app.db 复制到临时目录，VIEWER_DB 指向副本后再导入 viewer.*，
最后用临时端口 8795 起一个真实 HTTP 实例验证路由。

覆盖点：
  1. 解析正确性：8 流派 / 50 博主 / 33 策略 / 50 来源，且各流派「声明人数 == 实得人数」；
  2. 无空字段（只有标题没条目的排版行不得进入结果）；
  3. 落库幂等：同内容第二次保存必须 skipped（不写库、不动 ingested_at）；
  4. force=True 才强制重建，且 prev_hash / prev_ingested_at 记录上一版；
  4b. parser_version 变更也必须触发重建（上游文件未变时，解析缺陷修复的成果要能进库）；
  5. kb_get / kb_list 结构与字段完整；
  6. HTTP：/api/kb/list、/api/kb/doc?light=1、/api/kb/doc、POST /api/kb/reload 全 200；
  7. 知识库来源并入 refresh_status（tracked_sources 含 doc_key，且记了一笔刷新）；
  8. 容错：来源文件缺失时接口如实报错、不抛 500。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC = ROOT / "viewer" / "app.db"
PORT = 8795
KB_SRC = Path(
    r"C:\Users\野比&大熊\DoubaoWork\chats\2026-09-22\new-chat"
    r"\抖音中短线炒股博主知识库_v5.0_最终完整版.md"
)
fails: list[str] = []


def check(cond: bool, msg: str):
    if cond:
        print(f"  [OK] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        fails.append(msg)


def isolated_copy() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="qa_kb_"))
    dst = tmp / "app.db"
    shutil.copy2(SRC, dst)
    for suf in ("-wal", "-shm"):   # WAL 下必须连 -wal/-shm 一起复制
        p = Path(str(SRC) + suf)
        if p.exists():
            shutil.copy2(p, Path(str(dst) + suf))
    return dst


def http_json(path: str, method: str = "GET", body: dict | None = None):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main() -> int:
    if not SRC.exists():
        print("!! 找不到源库", SRC)
        return 2
    if not KB_SRC.exists():
        print("!! 找不到知识库源文件", KB_SRC)
        return 2
    dst = isolated_copy()
    os.environ["VIEWER_DB"] = str(dst)
    print(f"隔离库：{dst}")

    from viewer.db import Database
    from viewer import kb, server

    db = Database()

    # ---------- 1. 解析正确性 ----------
    print("\n[1] 解析正确性（真实 md 文件）")
    cfg = next((c for c in kb.load_config()
                if (c.get("doc_key") or "kb") == "zdx_stock"), None)
    check(cfg is not None, "config/kb_sources.yaml 已声明 zdx_stock 来源")
    if not cfg:
        return 1
    resolved = kb.resolve_source(cfg)
    check(resolved is not None, f"来源文件可解析：{resolved}")
    payload = kb.build_payload(cfg)
    check(payload["status"] == "ok", f"吸取状态 ok（error={payload.get('error') or '无'}）")
    doc = payload["doc"]
    m = doc["meta"]
    check(m["school_total"] == 8, f"流派数 = {m['school_total']}（期望 8）")
    check(m["blogger_total"] == 50, f"博主数 = {m['blogger_total']}（期望 50）")
    check(m["strategy_total"] == 33, f"策略回测行 = {m['strategy_total']}（期望 33）")
    check(m["source_total"] == 50, f"信息来源行 = {m['source_total']}（期望 50）")
    check(payload["version"] == "v5.0", f"版本号 = {payload['version']}")
    check(len(payload["content_hash"]) == 64, "content_hash 为 sha256")

    bad = [(s["name"], s["declared_count"], s["actual_count"])
           for s in doc["schools"] if s["declared_count"] != s["actual_count"]]
    check(not bad, f"各流派声明人数 == 实得人数（不符：{bad}）")

    empties = [(s["name"], b["name"], r.get("heading"))
               for s in doc["schools"] for b in s["bloggers"]
               for r in b["phase_notes"] + b["phase_video"] if not r.get("items")]
    check(not empties, f"无空字段条目（空字段：{empties[:3]}）")

    sample = next((b for s in doc["schools"] for b in s["bloggers"]
                   if b["has_video"] and b["sources"]), None)
    check(sample is not None and bool(sample["style"]),
          f"博主档案含 风格定位/视频笔记/来源三类（样本：{sample and sample['name']}）")

    # ---------- 2. 落库幂等 ----------
    print("\n[2] 落库幂等（同内容第二次必须跳过）")
    db.conn.execute("DELETE FROM kb_doc")
    db.conn.commit()
    r1 = db.kb_save(payload)
    check(r1["changed"] and not r1["skipped"], "首次保存 → changed=True")
    row1 = db.kb_get("zdx_stock")
    t1 = row1["ingested_at"]
    n1 = row1["node_count"]
    check(n1 == payload["node_count"], f"node_count 落库 = {n1}")
    check(row1["reload_count"] == 1, f"reload_count = {row1['reload_count']}（首次应为 1）")
    time.sleep(1.1)
    r2 = db.kb_save(payload)
    check(r2["skipped"] and not r2["changed"], "同内容再保存 → skipped=True（未重写）")
    row2 = db.kb_get("zdx_stock")
    check(row2["ingested_at"] == t1, "跳过时 ingested_at 未被改动")
    check(row2["reload_count"] == 1, "跳过时 reload_count 未自增")

    # ---------- 3. force 重建 ----------
    print("\n[3] force=True 强制重建并保留上一版指纹")
    r3 = db.kb_save(payload, force=True)
    check(r3["changed"] and not r3["skipped"], "force=True → changed=True")
    row3 = db.kb_get("zdx_stock")
    check(row3["reload_count"] == 2, f"reload_count 自增到 {row3['reload_count']}")
    check(row3["prev_hash"] == payload["content_hash"], "prev_hash 记录了上一版哈希")
    check(row3["prev_ingested_at"] == t1, "prev_ingested_at 记录了上一版吸取时刻")
    check(row3["ingested_at"] != t1, "ingested_at 已前移到本次")

    # ---------- 3b. 解析器版本触发重建 ----------
    # 场景：kb.py 修好一个解析缺陷（如某排版分支被静默丢弃），上游 md 一个字节没动。
    # 只比 content_hash 会把修复成果挡在门外，故必须把 parser_version 并入变化判定。
    print("\n[3b] parser_version 变更触发重建（上游文件未变也必须重建）")
    pv_now = kb.PARSER_VERSION
    older = dict(payload, parser_version=pv_now - 1)
    r = db.kb_save(older)
    check(r["changed"] and not r["skipped"] and r.get("parser_bumped"),
          f"库中 pv={pv_now}、来件 pv={pv_now-1}（同哈希）→ 判定为变化并重建")
    row = db.kb_get("zdx_stock")
    check(int(row["parser_version"] or 0) == pv_now - 1,
          f"落库 parser_version 更新为 {row['parser_version']}")
    stale_doc = server.api_kb_doc("zdx_stock")
    check(stale_doc["parser_stale"] is True,
          "接口如实标出 parser_stale=True（正文由旧解析逻辑产出）")
    r = db.kb_save(older)
    check(r["skipped"], "同 pv 同内容再保存 → 恢复跳过（不会循环重建）")
    r = db.kb_save(payload)
    check(r["changed"] and not r["skipped"] and r.get("parser_bumped"),
          f"回到 pv={pv_now} → 再次重建（把旧解析产物换成新逻辑产物）")
    row = db.kb_get("zdx_stock")
    check(int(row["parser_version"] or 0) == pv_now, "落库 parser_version 回到当前值")
    now_doc = server.api_kb_doc("zdx_stock")
    check(now_doc["parser_stale"] is False and now_doc["parser_current"] == pv_now,
          f"接口 parser_stale=False 且 parser_current={pv_now}")
    check(now_doc["node_count"] == payload["node_count"],
          f"重建后节点数 = {now_doc['node_count']}（解析缺陷修复的成果确实进了库）")

    # ---------- 4. kb_get / kb_list ----------
    print("\n[4] kb_get / kb_list 结构")
    d = db.kb_get("zdx_stock")
    check("doc" in d and d["doc"].get("meta", {}).get("school_total") == 8,
          "kb_get 反序列化出完整 doc")
    check("payload" not in d, "kb_get 未把原始 payload 文本外泄（已剥离为 doc/envelope）")
    check(d.get("envelope", {}).get("version") == "v5.0", "envelope 保留版本等信封字段")
    lst = db.kb_list()
    check(len(lst) == 1 and lst[0]["doc_key"] == "zdx_stock", "kb_list 返回 1 份文档")
    check(all(k in lst[0] for k in ("track", "title", "version", "ingested_at",
                                    "node_count", "content_hash", "parser_version")),
          "kb_list 字段完整（含 parser_version）")
    check(db.kb_get("not_exist") is None, "取不存在的 doc_key 返回 None")

    # ---------- 5. api_* 层 ----------
    print("\n[5] api_kb_list / api_kb_doc（进程内）")
    kl = server.api_kb_list()
    check(kl["default_key"] == "zdx_stock", "default_key = zdx_stock")
    check(kl["docs"] and kl["docs"][0]["source_exists"],
          "索引里来源文件存在标记为 True")
    check(kl["docs"][0]["track"] == "中短线炒股", "track = 中短线炒股（横向子板块名）")
    light = server.api_kb_doc("zdx_stock", light=True)
    check(light.get("outline") and not light.get("schools"),
          "light=True 只回 meta+outline（不含 schools 明细）")
    check(light.get("meta", {}).get("blogger_total") == 50, "light 也带 meta 计数")
    full = server.api_kb_doc("zdx_stock")
    check(len(full.get("schools") or []) == 8, "完整接口含 8 个流派明细（扁平结构）")
    check(len(full.get("backtests") or []) == 3, "完整接口含 3 张回测表")
    check(bool(full.get("envelope") is None), "接口不外泄落库信封（结构已扁平化）")
    empty = server.api_kb_doc("no_such_key")
    check(empty["status"] == "empty" and empty.get("message"),
          "取不存在的库 → status=empty 且带可读提示（不抛异常）")

    # ---------- 6. 并入 refresh_status ----------
    print("\n[6] 知识库来源并入刷新状态机制")
    rs = server.api_refresh_status()
    check("zdx_stock" in rs["tracked_sources"],
          "tracked_sources 含知识库 doc_key（参与时效判定）")
    before = {r["source_key"] for r in db.get_refresh_status()}
    res = server.kb_reload("zdx_stock")
    after = {r["source_key"]: r for r in db.get_refresh_status()}
    check("zdx_stock" in after, f"吸取后 refresh_status 记录了 zdx_stock（此前：{sorted(before)}）")
    check(after["zdx_stock"]["status"] == "ok", "知识库来源状态 = ok")
    check(bool(after["zdx_stock"]["last_ok_at"]), "知识库来源 last_ok_at 有值")
    check(after["zdx_stock"]["display_name"] == "中短线炒股知识库",
          f"显示名 = {after['zdx_stock']['display_name']}")
    check(res["results"][0].get("skipped") is True,
          "内容未变 → 重吸取如实回报 skipped（不谎报「已更新」）")

    # ---------- 7. 容错：来源缺失 ----------
    print("\n[7] 容错：来源文件缺失")
    broken = dict(cfg, path=str(ROOT / "no_such_kb_file.md"),
                  path_glob=str(ROOT / "no_such_dir" / "*.md"))
    bp = kb.build_payload(broken)
    check(bp["status"] == "error" and bp.get("error"), "缺文件 → status=error 且带原因")
    db.conn.execute("DELETE FROM kb_doc WHERE doc_key='tmp_missing'")
    db.conn.commit()

    # ---------- 8. HTTP 层 ----------
    print("\n[8] HTTP：/api/kb/*")
    env = dict(os.environ, VIEWER_DB=str(dst), VIEWER_PORT=str(PORT))
    proc = subprocess.Popen([sys.executable, "-m", "viewer.server"], cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        up = False
        for _ in range(100):
            try:
                st, _d = http_json("/api/kb/list")
                up = st == 200
                break
            except Exception:
                if proc.poll() is not None:
                    break
                time.sleep(0.25)
        if not up:
            out = b""
            try:
                out = proc.stdout.read() if proc.stdout else b""
            except Exception:
                pass
            check(False, "服务可达 —— " + out.decode("utf-8", "ignore")[:400])
        else:
            st, data = http_json("/api/kb/list")
            check(st == 200 and data["docs"], "GET /api/kb/list → 200")
            st, data = http_json("/api/kb/doc?key=zdx_stock&light=1")
            check(st == 200 and data.get("outline"), "GET /api/kb/doc?light=1 → 200")
            check(not data.get("schools"), "light=1 不返回流派明细（省流量）")
            st, data = http_json("/api/kb/doc?key=zdx_stock")
            check(st == 200 and len(data.get("schools") or []) == 8,
                  "GET /api/kb/doc（完整）→ 200 且含 8 流派")
            payload_bytes = len(json.dumps(data, ensure_ascii=False))
            check(payload_bytes > 100_000, f"完整 payload 体积 {payload_bytes} 字节（重接口）")
            st, data = http_json("/api/kb/reload", "POST", {"key": "zdx_stock"})
            check(st == 200 and data["results"][0]["status"] == "ok",
                  "POST /api/kb/reload → 200")
            st, data = http_json("/api/refresh_status")
            check(st == 200 and any(s["source_key"] == "zdx_stock" for s in data["sources"]),
                  "GET /api/refresh_status 含知识库来源（学习板块时效复用行情机制）")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()

    print("\n" + "=" * 56)
    if fails:
        print(f"FAILED: {len(fails)} 项未通过")
        for f in fails:
            print("  -", f)
        return 1
    print("ALL PASSED —— 知识库解析/吸取/接口/时效接入 全部符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
