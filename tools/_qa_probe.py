"""临时探针：确认 stock_names 在线解析是否可用。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from viewer.db import Database  # noqa: E402
from viewer.stock_names import Resolver  # noqa: E402

db = Database()
r = Resolver(db=db, online=True)
lines = []
for code in ("300308", "688256", "600938", "000034", "002837", "688027"):
    info = r.resolve(code, allow_online=True)
    lines.append(f"{code} -> 简称={info['name'] or '(空)'} 板块={info['market']} 来源={info['source']}")
Path(ROOT / "tools" / "_qa_probe_out.txt").write_text("\n".join(lines), encoding="utf-8")
print("done")
