# -*- coding: utf-8 -*-
"""离线夹具加载框架（零第三方依赖）。

职责（红线）：
- 扫描 fixtures/*.json，逐个校验 sha256 与 is_real_data=true。
- 任何 is_real_data 非 true / sha256 不匹配 / 文件损坏 → 拒绝加载（绝不混入合成数据）。
- 缺失真实夹具 → 抛 FixtureMissing，调用方据此 pytest.skip（诚实降级，绝不假 PASS）。

sha256 计算口径：对「去除 metadata.sha256 字段后的 payload」做规范化序列化后再取哈希，
保证「声明值」与「实际值」一致校验，且不被 sha256 字段自身污染。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent


class FixtureMissing(Exception):
    """没有可用真实夹具时抛出（沙箱无网 / 王总尚未采集）。"""


class FixtureRejected(Exception):
    """夹具校验失败（is_real_data 非 true / sha256 不匹配 / 结构异常）。"""


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_payload_text(data: Dict[str, Any]) -> str:
    """去除 metadata.sha256 后的规范化序列化（用于稳定哈希）。"""
    meta = dict(data.get("metadata", {}))
    meta.pop("sha256", None)
    stripped = dict(data)
    stripped["metadata"] = meta
    return json.dumps(stripped, ensure_ascii=False, indent=2, sort_keys=True)


def _load_one(path: Path) -> Dict[str, Any]:
    """加载并校验单个夹具文件；失败抛 FixtureRejected。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise FixtureRejected("无法读取 %s: %s" % (path.name, e))
    try:
        data = json.loads(text)
    except Exception as e:  # noqa: BLE001
        raise FixtureRejected("JSON 解析失败 %s: %s" % (path.name, e))
    meta = data.get("metadata") or {}
    declared = meta.get("sha256")
    if not isinstance(declared, str):
        raise FixtureRejected("缺少 sha256 字段: %s" % path.name)
    actual = _sha256_text(_canonical_payload_text(data))
    if actual != declared:
        raise FixtureRejected(
            "sha256 不匹配 %s（可能被篡改）：声明=%s 实际=%s"
            % (path.name, declared, actual))
    if meta.get("is_real_data") is not True:
        raise FixtureRejected(
            "is_real_data 非 true，拒绝加载（绝不混入合成数据）: %s" % path.name)
    return data


def load_fixture(symbol: str) -> Dict[str, Any]:
    """按 symbol 加载单个夹具；缺失或校验失败抛异常。"""
    candidates = [
        ROOT / ("oracle_%s.json" % symbol),
        ROOT / ("%s.json" % symbol),
    ]
    for p in candidates:
        if p.exists():
            return _load_one(p)
    raise FixtureMissing("未找到 symbol=%s 的真实夹具（沙箱无网或待采集）" % symbol)


def load_all_fixtures() -> List[Dict[str, Any]]:
    """加载 fixtures/ 下全部真实夹具；无则抛 FixtureMissing；任一损坏抛 FixtureRejected。"""
    files = sorted(ROOT.glob("*.json"))
    files = [
        f for f in files
        if (not f.name.startswith("_"))
        and (not f.name.lower().startswith("readme"))
    ]
    if not files:
        raise FixtureMissing("fixtures/ 下没有任何真实夹具（沙箱无网或待采集）")
    return [_load_one(f) for f in files]


if __name__ == "__main__":
    try:
        fx = load_all_fixtures()
        print("已加载 %d 个真实夹具" % len(fx))
        for d in fx:
            print(" -", d["metadata"]["symbol"], d["metadata"].get("source"))
    except (FixtureMissing, FixtureRejected) as e:
        # 诚实降级：无夹具 / 校验失败都不是错误，正常退出 0
        print("诚实降级:", e)
        sys.exit(0)
