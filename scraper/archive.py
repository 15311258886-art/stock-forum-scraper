"""累积存档：将每日抓取到的实盘交易 / 一手资讯沉淀进一个持续维护的 Excel 文件。

设计要点：
- 单一文件 archive/股票论坛抓取历史存档.xlsx，含三个工作表：
  · 实盘交易：每条被解析出的买卖记录（按主键去重后追加）
  · 一手资讯：每条一手市场消息（按主键去重后追加）
  · 每日汇总：每次运行的元信息（日期 / 站点 / 模式 / 新增数 / 状态 / 备注）
- 去重：交易主键 (source_site, post_id)；资讯主键 (source_site, url)。
  重复运行不会产生重复行，因此可放心「每日全量重抓 -> 增量入库」。
- 合规：入库字段全部来自已通过 compliance.filter_public_only 清洗后的公开字段，
  此处不引入任何用户行为 / 画像字段。
- 读取/写入接口同时接受 dataclass 或 dict；时间字段统一转字符串，规避时区歧义。
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Iterable, Optional, Sequence

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# 列定义：(中文表头, 内部字段名)
TRADE_COLUMNS = [
    ("抓取日期", "scrape_date"),
    ("来源站点", "source_site"),
    ("选手署名", "author"),
    ("帖子ID", "post_id"),
    ("帖子链接", "post_url"),
    ("发帖时间", "post_time"),
    ("动作", "action"),
    ("标的代码", "symbol"),
    ("标的名称", "symbol_name"),
    ("价格", "price"),
    ("数量", "quantity"),
    ("数据状态", "record_status"),
    ("原文摘要", "raw_text"),
]
NEWS_COLUMNS = [
    ("抓取日期", "scrape_date"),
    ("来源站点", "source_site"),
    ("标题", "title"),
    ("链接", "url"),
    ("发布时间", "publish_time"),
    ("摘要", "summary"),
    ("来源署名", "author"),
    ("数据状态", "record_status"),
]
SUMMARY_COLUMNS = [
    ("运行日期", "run_date"),
    ("站点", "source_site"),
    ("模式", "mode"),
    ("新增交易", "new_trades"),
    ("新增资讯", "new_news"),
    ("状态", "status"),
    ("备注", "note"),
]

TRADE_SHEET = "实盘交易"
NEWS_SHEET = "一手资讯"
SUMMARY_SHEET = "每日汇总"

SHEET_COLUMNS = {
    TRADE_SHEET: TRADE_COLUMNS,
    NEWS_SHEET: NEWS_COLUMNS,
    SUMMARY_SHEET: SUMMARY_COLUMNS,
}

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT_WRAP = Alignment(horizontal="left", vertical="top", wrap_text=True)


def _to_row_value(v) -> str:
    if v is None:
        return ""
    if isinstance(v, _dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, _dt.date):
        return v.strftime("%Y-%m-%d")
    return str(v)


class ExcelArchive:
    """维护一个累积式 Excel 存档文件。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._wb = self._load_or_create()

    # ---------- 底层 ---------- #
    def _load_or_create(self) -> Workbook:
        if self.path.exists():
            return load_workbook(self.path)
        wb = Workbook()
        # 删除默认 Sheet，建立三个标准表
        if wb.sheetnames:
            wb.remove(wb[wb.sheetnames[0]])
        for title, cols in SHEET_COLUMNS.items():
            self._make_sheet(wb, title, cols)
        return wb

    @staticmethod
    def _make_sheet(wb: Workbook, title: str, columns: Sequence[tuple]) -> None:
        ws = wb.create_sheet(title=title)
        ws.append([c[0] for c in columns])
        for cell in ws[1]:
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = CENTER
        ws.freeze_panes = "A2"
        for i, (label, _key) in enumerate(columns, start=1):
            width = max(10, min(46, len(label) * 2 + 4))
            ws.column_dimensions[get_column_letter(i)].width = width
        # 长文本列自动换行
        for i, (_label, key) in enumerate(columns, start=1):
            if key in ("raw_text", "summary", "note", "post_url", "url"):
                for r in range(2, ws.max_row + 1):
                    ws.cell(row=r, column=i).alignment = LEFT_WRAP

    def _sheet(self, title: str) -> "object":
        if title not in self._wb.sheetnames:
            self._make_sheet(self._wb, title, SHEET_COLUMNS[title])
        return self._wb[title]

    def _save(self) -> None:
        self._wb.save(self.path)

    def ensure_file(self) -> None:
        """强制落盘（即便尚无数据），用于初始化空存档。"""
        self._save()

    # ---------- 去重辅助 ---------- #
    def _existing_keys(self, ws, key_labels: Sequence[str]) -> set:
        if ws.max_row < 2:
            return set()
        header = [c.value for c in ws[1]]
        idx = []
        for lbl in key_labels:
            try:
                idx.append(header.index(lbl))
            except ValueError:
                continue
        if not idx:
            return set()
        out = set()
        for row in ws.iter_rows(min_row=2, values_only=True):
            out.add(tuple("" if row[i] is None else str(row[i]) for i in idx))
        return out

    # ---------- 写入 ---------- #
    def append_trades(self, records: Iterable[dict], scrape_date: Optional[str] = None) -> int:
        scrape_date = scrape_date or _dt.date.today().isoformat()
        ws = self._sheet(TRADE_SHEET)
        existing = self._existing_keys(ws, ["来源站点", "帖子ID"])
        added = 0
        for r in records:
            key = (str(r.get("source_site", "")), str(r.get("post_id", "")))
            if key in existing:
                continue
            existing.add(key)
            ws.append(self._build_row(TRADE_COLUMNS, r, scrape_date))
            added += 1
        if added:
            self._save()
        return added

    def append_news(self, records: Iterable[dict], scrape_date: Optional[str] = None) -> int:
        scrape_date = scrape_date or _dt.date.today().isoformat()
        ws = self._sheet(NEWS_SHEET)
        existing = self._existing_keys(ws, ["来源站点", "链接"])
        added = 0
        for r in records:
            key = (str(r.get("source_site", "")), str(r.get("url", "")))
            if key in existing:
                continue
            existing.add(key)
            ws.append(self._build_row(NEWS_COLUMNS, r, scrape_date))
            added += 1
        if added:
            self._save()
        return added

    def _build_row(self, columns, record: dict, scrape_date: str) -> list:
        row = []
        for _label, key in columns:
            if key == "scrape_date":
                row.append(scrape_date)
            elif key == "record_status":
                row.append("新增")
            else:
                row.append(_to_row_value(record.get(key)))
        return row

    def log_run(self, source_site: str, mode: str, new_trades: int,
                new_news: int, status: str = "ok", note: str = "") -> None:
        ws = self._sheet(SUMMARY_SHEET)
        ws.append([_dt.date.today().isoformat(), source_site, mode,
                   new_trades, new_news, status, note])
        self._save()

    # ---------- 查询 ---------- #
    def stats(self) -> dict:
        def count(sheet: str) -> int:
            if sheet in self._wb.sheetnames:
                return max(0, self._wb[sheet].max_row - 1)
            return 0
        return {
            "trades": count(TRADE_SHEET),
            "news": count(NEWS_SHEET),
            "runs": count(SUMMARY_SHEET),
            "path": str(self.path),
        }

    def recent_runs(self, n: int = 10) -> list:
        if SUMMARY_SHEET not in self._wb.sheetnames:
            return []
        ws = self._wb[SUMMARY_SHEET]
        if ws.max_row < 2:
            return []
        rows = list(ws.iter_rows(min_row=2, values_only=True))
        labels = [c[0] for c in SUMMARY_COLUMNS]
        return [dict(zip(labels, r)) for r in rows[-n:]]
