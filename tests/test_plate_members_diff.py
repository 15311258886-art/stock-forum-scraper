# -*- coding: utf-8 -*-
"""板块成分股抓取的「双形态解析」契约测试（审计 2026-11-06 P1-01）。

## 这条链路坏掉时是什么样

`plate_members` 连续失败 14 次，`/api/health` 一直 `degraded`，错误只有一行：

    KeyError: 0

根因不是网络、不是限流，是东财 `clist` 接口的 `diff` 字段**有两种形态**：

    数组：  "diff": [["000001", 0, "平安银行"], ["000002", 0, "万科A"], ...]
    对象：  "diff": {"0": {"f12": "000001", "f14": "平安银行"}, "1": {...}}

旧代码只按数组写：`for row in data.get("diff") or []` 然后 `row[0]`。
返回对象时，`for row in dict` 迭代的是**键名**，`row` 变成字符串 `"0"`，
再对它取 `[0]` 得到 `"0"`，接着拿 `"0"` 当字典下标 → `KeyError: 0`。

所以这组测试钉的不是"函数能跑"，而是三件更具体的事：

1. **两种形态都必须解析出同样的 (code, name)** —— 形态切换不该影响数据。
2. **脏行只跳过自己，不炸整批** —— 一个板块 500 只成分股，
   因为一行缺字段就整批失败，等于整个板块消失且没人知道。
3. **分页终止判据用原始条数** —— 若用"解析后条数"判断末页，
   一页里恰好有几条脏数据就会被当成最后一页，后面的成分股**静默丢失**。
   这条最难在线上发现：数据不是空的，只是少了一截。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.fetch_plate_members import _diff_rows, _raw_count  # noqa: E402


ARRAY_FORM = {
    "total": 2,
    "diff": [
        ["000001", 0, "平安银行"],
        ["600519", 1, "贵州茅台"],
    ],
}

DICT_FORM = {
    "total": 2,
    "diff": {
        "0": {"f12": "000001", "f13": 0, "f14": "平安银行"},
        "1": {"f12": "600519", "f13": 1, "f14": "贵州茅台"},
    },
}


def test_array_form_parses():
    assert _diff_rows(ARRAY_FORM) == [("000001", "平安银行"), ("600519", "贵州茅台")]


def test_dict_form_parses():
    """这是修复前直接抛 KeyError: 0 的那条路径。"""
    assert _diff_rows(DICT_FORM) == [("000001", "平安银行"), ("600519", "贵州茅台")]


def test_both_forms_agree():
    """两种形态必须给出**完全相同**的结果。

    分开断言两次不够：如果哪天两种解析各自漂移（比如对象形态漏了 name），
    两条测试都会绿，但线上换形态时数据会悄悄变一半。
    """
    assert _diff_rows(ARRAY_FORM) == _diff_rows(DICT_FORM)


def test_object_values_may_be_lists():
    """对象形态里 value 也见过 list —— 不能假定它一定是 dict。"""
    data = {"diff": {"0": ["000001", 0, "平安银行"]}}
    assert _diff_rows(data) == [("000001", "平安银行")]


def test_dirty_row_is_skipped_not_fatal():
    """缺 f12 的行跳过，其余照常返回（fail-soft，不是 fail-fast）。"""
    data = {
        "diff": {
            "0": {"f12": "000001", "f14": "平安银行"},
            "1": {"f14": "没有代码的脏行"},
            "2": {"f12": "", "f14": "空代码"},
            "3": {"f12": "600519", "f14": "贵州茅台"},
        }
    }
    assert _diff_rows(data) == [("000001", "平安银行"), ("600519", "贵州茅台")]


def test_name_may_be_missing():
    """有代码没名字要保留代码 —— 名字可以后补，代码丢了这只票就没了。"""
    assert _diff_rows({"diff": [["000001"]]}) == [("000001", "")]


def test_empty_and_none_forms():
    """空响应不能抛异常：节假日/盘前东财会返回 `diff: null`。"""
    assert _diff_rows({}) == []
    assert _diff_rows({"diff": None}) == []
    assert _diff_rows({"diff": []}) == []
    assert _diff_rows({"diff": {}}) == []


def test_non_iterable_diff_is_safe():
    """异常形态（字符串/数字）不该炸，返回空即可。"""
    assert _diff_rows({"diff": "unexpected"}) == []
    assert _diff_rows({"diff": 123}) == []


def test_code_is_not_zero_padded_here():
    """本函数**不做**补零：板块代码是 "BK0475" 这种字母码，补零会破坏它。

    补零只在个股成分股那一层做（`fetch_members` 里 zfill(6)），
    这是有意的分层 —— 两种代码混在一处处理，迟早会把板块码补成 "0BK0475"。
    """
    assert _diff_rows({"diff": [["BK0475", 0, "芯片"]]}) == [("BK0475", "芯片")]


# ---------------------------------------------------------------------------
# 分页判据（2026-11-06 实测补的第二处修复）
#
# 东财 clist **单页硬上限 100 条**，`pz` 传多大都只回 100。
# 原来的成分股抓取写的是 `pz=10000` + `if len(diff) < pz: break`：
# 100 < 10000 恒成立，于是第一页就退出 —— 每个板块最多 100 只，
# 一个 439 只成分股的板块只入库 100 只，**不报错、只是少了一截**。
# 所以分页终止判据必须看「原始条数 vs 真实页长」，而不是「解析后条数 vs 请求的 pz」。
# ---------------------------------------------------------------------------

def test_raw_count_handles_both_forms():
    assert _raw_count(ARRAY_FORM) == 2
    assert _raw_count(DICT_FORM) == 2


def test_raw_count_counts_dirty_rows():
    """原始条数要**包含脏行** —— 这正是它不能被"解析后条数"替代的原因。

    一页 100 条里如果有 3 条缺字段，解析后是 97。若拿 97 < 100 当"这是最后一页"，
    后面 300 只成分股就永远拿不到了。
    """
    data = {"diff": [["000001", 0, "平安银行"], ["000002"], ["000003", 0, "国农科技"]]}
    assert _raw_count(data) == 3
    assert len(_diff_rows(data)) == 3   # 三条都能解析（缺 name 也保留 code）
    dirty = {"diff": [{"f12": "000001"}, {"f14": "没代码"}, {"f12": ""}]}
    assert _raw_count(dirty) == 3
    assert len(_diff_rows(dirty)) == 1  # 解析后只剩 1 条 —— 绝不能用它判断分页


def test_raw_count_empty():
    assert _raw_count({}) == 0
    assert _raw_count({"diff": None}) == 0
    assert _raw_count({"diff": {}}) == 0
    assert _raw_count({"diff": "unexpected"}) == 0
