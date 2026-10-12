#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
关键接口冒烟测试（中优先级）

针对查看器核心只读接口做断言，弥补产品"零自动化测试"的体检发现。
不依赖 pytest 等第三方框架，标准库即可运行：

    python viewer/smoke_test.py

退出码：0=全部 PASS；1=存在 FAIL。
"""
import json
import sys
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8765"


def get(path, timeout=10):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:160]


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}  {detail}")
    return cond


def main():
    print("=== 关键接口冒烟测试 ===")
    ok = True

    # 1. 版本接口
    st, body = get("/api/version")
    ok &= check("version 接口 200", st == 200, f"code={st}")
    if st == 200:
        try:
            j = json.loads(body)
            ok &= check("version 返回非空版本串", isinstance(j, dict) and bool(str(j)))
        except Exception as e:  # noqa: BLE001
            ok &= check("version 可解析JSON", False, str(e)[:80])

    # 2. 汇总接口（含 trades 换库后行数）
    st, body = get("/api/summary")
    ok &= check("summary 接口 200", st == 200, f"code={st}")
    if st == 200:
        try:
            j = json.loads(body)
            c = j.get("counts", {})
            n = c.get("trades")
            ok &= check("summary.counts.trades 存在且 >0",
                        isinstance(n, int) and n > 0, f"trades={n}")
            print(f"        (trades={n}，换库后期望 941；>0 即证明接口与库连通)")
        except Exception as e:  # noqa: BLE001
            ok &= check("summary 可解析JSON", False, str(e)[:80])

    # 3. 两个核心数据面板接口
    for ep in ("/api/operating", "/api/players"):
        st, body = get(ep)
        ok &= check(f"{ep} 接口 200", st == 200, f"code={st}")
        ok &= check(f"{ep} 返回数据非空", len(body) > 100, f"len={len(body)}")

    # 4. 前端首页可加载
    st, body = get("/")
    ok &= check("前端首页 200 且含 <title>", st == 200 and "<title>" in body, f"code={st}")

    print("\n=== 结果:", "全部 PASS" if ok else "存在 FAIL", "===")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
