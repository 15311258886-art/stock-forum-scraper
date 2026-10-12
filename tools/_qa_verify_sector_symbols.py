"""临时脚本：核实 SECTOR_SYMBOLS 中每个代码的股票简称，并检查领域名覆盖。输出写文件。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from overseas.lexicon import SECTOR_SYMBOLS, TRANSMISSION_TABLE, IMPACT_STRUCTURE  # noqa: E402
from overseas.hotspots import TOPIC_DEFAULT  # noqa: E402
from viewer.db import Database  # noqa: E402
from viewer.stock_names import Resolver  # noqa: E402

res = Resolver(db=Database(), online=True)


def codes_of(sym_map):
    seen = []
    for sector, lst in sym_map.items():
        for code, _name in lst:
            if code not in seen:
                seen.append(code)
    return seen


codes = codes_of(SECTOR_SYMBOLS)
info = {}
for c in codes:
    r = res.resolve(c, allow_online=True)
    info[c] = r

lines = []
lines.append(f"领域数={len(SECTOR_SYMBOLS)}  唯一代码数={len(codes)}  "
             f"公司条目总数={sum(len(v) for v in SECTOR_SYMBOLS.values())}")

# 重复键检查（Python dict 天然去重，这里只提示领域名重复列出）
# 名称一致性
bad = []
empty = []
for sector, lst in SECTOR_SYMBOLS.items():
    n = len(lst)
    if n < 3 or n > 5:
        bad.append(f"[条目数异常] {sector}: {n}")
    for code, name in lst:
        r = info.get(code) or {}
        got = r.get("name") or ""
        if not got:
            empty.append(f"[查不到] {sector} {code} {name}")
        elif got != name:
            bad.append(f"[名称不符] {sector} {code} 写的={name} 查询={got} 来源={r.get('source')}")

lines.append("")
lines.append("== 名称/条目问题 ==")
lines.extend(bad or ["（无）"])
lines.append("")
lines.append("== 查不到名字的代码 ==")
lines.extend(empty or ["（无）"])

# 覆盖率：代码里实际用到的领域名
used = []
for row in TRANSMISSION_TABLE:
    used += list(row.get("sectors_up") or []) + list(row.get("sectors_down") or [])
for v in IMPACT_STRUCTURE.values():
    used += list(v.get("sectors_up") or []) + list(v.get("sectors_down") or [])
for v in TOPIC_DEFAULT.values():
    used += list(v.get("sectors_up") or []) + list(v.get("sectors_down") or [])
uniq_used = sorted(set(used))
missing = [s for s in uniq_used if s not in SECTOR_SYMBOLS]
lines.append("")
lines.append(f"== 代码中出现过的领域名 {len(uniq_used)} 个，未建条目 {len(missing)} 个 ==")
lines.extend(missing or ["（无）"])

lines.append("")
lines.append("== SECTOR_SYMBOLS 全部键（排序） ==")
lines.extend(sorted(SECTOR_SYMBOLS))

extra = [k for k in SECTOR_SYMBOLS if k not in uniq_used]
lines.append("")
lines.append(f"== 本表有、但代码 sectors_up/down 里未出现的键 {len(extra)} 个 ==")
lines.extend(extra or ["（无）"])

Path(ROOT / "tools" / "_qa_verify_out.txt").write_text("\n".join(lines), encoding="utf-8")
print("done", len(bad), len(empty), len(missing))
