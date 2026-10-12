"""知识库重吸取（命令行，不依赖查看器进程）。

上游（豆包/其他工具）每天把来源 md 覆盖写出，本脚本负责把它重新解析入库。
幂等：`content_hash` **且** `parser_version` 都未变则**直接跳过**，不写库、不动
ingested_at——所以每天定时跑一次也不会产生无意义重建，可以放心交给自动化。

两个触发重建的条件（任一成立即重建）：
  1. 上游文件内容变了（sha256 不等）；
  2. 解析器变了（kb.py 的 PARSER_VERSION 与库中存值不等）——修好解析缺陷后，
     上游文件一个字节没动，但库里的旧解析结果必须被重建，否则修复成果进不来。

用法：
    python tools/kb_ingest.py              # 全部启用的来源
    python tools/kb_ingest.py --force      # 忽略哈希，强制重建
    python tools/kb_ingest.py --key zdx_stock
    python tools/kb_ingest.py --json       # 只输出 JSON（供自动化读取）

退出码：0=全部成功（含跳过）；1=有来源失败。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="重新吸取知识库（幂等）")
    ap.add_argument("--key", default="", help="只处理指定 doc_key（默认全部）")
    ap.add_argument("--force", action="store_true", help="忽略内容哈希，强制重建")
    ap.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    args = ap.parse_args()

    from viewer import kb
    from viewer.db import Database

    db = Database()
    cfgs = kb.load_config()
    if args.key:
        cfgs = [c for c in cfgs if (c.get("doc_key") or "kb") == args.key]
    if not cfgs:
        msg = "未配置可用的知识库来源（检查 config/kb_sources.yaml）"
        print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False) if args.json
              else "!! " + msg)
        return 1

    results, failed = [], 0
    for cfg in cfgs:
        key = cfg.get("doc_key") or "kb"
        label = f"{cfg.get('track') or ''}知识库".strip() or key
        try:
            payload = kb.build_payload(cfg)
            if payload.get("status") != "ok":
                err = payload.get("error") or "吸取失败"
                db.record_refresh(key, "error", 0, label, error=err)
                results.append({"doc_key": key, "status": "error", "error": err})
                failed += 1
                continue
            r = db.kb_save(payload, force=args.force)
            db.record_refresh(key, "ok", int(payload.get("node_count") or 0), label)
            results.append({**r, "status": "ok", "track": payload.get("track"),
                            "version": payload.get("version"),
                            "node_count": payload.get("node_count"),
                            "source_mtime": payload.get("source_mtime")})
        except Exception as e:  # noqa: BLE001 —— 单库失败不影响其它库
            err = str(e)[:200]
            db.record_refresh(key, "error", 0, label, error=err)
            results.append({"doc_key": key, "status": "error", "error": err})
            failed += 1

    if args.json:
        print(json.dumps({"ok": failed == 0, "results": results}, ensure_ascii=False))
    else:
        for r in results:
            if r["status"] != "ok":
                print(f"  [FAIL] {r['doc_key']}: {r.get('error')}")
            elif r.get("skipped"):
                print(f"  [SKIP] {r['doc_key']} 内容未变，未产生写入（上次吸取 {r.get('ingested_at')}）")
            else:
                times = r.get("reload_count") or 1
                if r.get("parser_bumped"):
                    how = f"解析器升级 v{r.get('parser_version')} 触发重建"
                else:
                    how = "首次入库" if times <= 1 else f"第 {times} 次重建"
                print(f"  [OK]   {r['doc_key']} → {r.get('version')} · {r.get('node_count')} 个知识点"
                      f" · {how} · 上游修改 {r.get('source_mtime')}")
        print("完成。" + ("" if failed == 0 else f" 有 {failed} 个来源失败。"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
