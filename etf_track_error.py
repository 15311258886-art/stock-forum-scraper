# -*- coding: utf-8 -*-
"""ETF 跟踪误差抓取（B 栏第 7 维「跟踪误差」的数据源，B 方案已落地）。

数据来源：东方财富 F10 基金档案接口
  https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=trackerr&code=<6位基金代码>
  返回形如 `var apidata=[[日期, 跟踪误差%], ...]`（最新一条即年化跟踪误差）。

重要事实（2026-10-17 实测，进哥红线：打不通就明说、不绕过）：
  - akshare 无 ETF 跟踪误差专题函数（其 ETF 函数全是行情/规模/净值）→ 已排除。
  - 集思录 etf_list 接口自认无此字段 → 已排除。
  - 东财 F10 该接口在**本沙箱出口 IP 被整体软封**（带进哥 cookie 也所有 type 均返回空 apidata），
    故只能在进哥部署机（正常 IP + 东财登录 cookie）运行本脚本才能取到真实值。
  - 即便在部署机，东财 F10 跟踪误差页通常只覆盖**场外指数基金 / ETF 联接**，
    场内 ETF 自身大概率返回空 → **已 implemented B 方案**：脚本内置 `etf_link_map.py`
    （场内 ETF → 同指数联接/指数基金映射），直取为空时自动用联接基金代码重抓，
    结果仍以原 ETF 代码落库并标 `approx=True` / `via=联接代码` / `name=联接基金名`，
    界面显式标「近似值·联接基金」。映射表代码为人工精选，跑出后以 via+name 核对。

cookie 获取（进哥操作）：浏览器登录 fund.eastmoney.com → F12 → Network →
  复制任意 F10 请求的 Cookie 为 Netscape 格式存为 eastmoney_cookie.txt（项目根），
  或设置环境变量 EASTMONEY_COOKIE="k1=v1; k2=v2"。脚本不写死 cookie 到代码。

产出：etf_track_error_cache.json（与 jisilu_etf_cache.json 同级），etf.py 的 etf_advisory()
  离线读它，点亮第 7 维「跟踪误差」。请求路径不写死 cookie 到代码。
"""
from __future__ import annotations
import os, sys, json, re, time
from datetime import datetime
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(BASE, 'eastmoney_cookie.txt')
CACHE = os.path.join(BASE, 'etf_track_error_cache.json')
URL_TMPL = 'https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=trackerr&code={code}&rt={rt}'

# B 方案：场内 ETF → 同指数联接/指数基金映射（东财 F10 不返场内 ETF 跟踪误差时作近似代理）
try:
    from etf_link_map import ETF_LINK_MAP
except Exception:  # noqa: BLE001
    ETF_LINK_MAP = {}


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


def load_cookie() -> str | None:
    env = os.environ.get('EASTMONEY_COOKIE')
    if env:
        return env
    if os.path.exists(COOKIE_FILE):
        txt = open(COOKIE_FILE, encoding='utf-8').read().strip()
        # Netscape 格式（tab 分隔）转 k=v
        if '\t' in txt:
            out = []
            for line in txt.splitlines():
                line = line.rstrip('\n')
                if not line.strip() or line.startswith('#'):
                    continue
                p = line.split('\t')
                if len(p) >= 7:
                    out.append('%s=%s' % (p[5], p[6]))
            return '; '.join(out) if out else None
        return txt or None
    return None


def _universe() -> list:
    """抓取目标：优先 ETF 核心宇宙，再并入集思录已接入的 ETF 扩面。"""
    codes: list = []
    try:
        if BASE not in sys.path:
            sys.path.insert(0, BASE)
        from etf_data import ETF_UNIVERSE as _U
        codes = list(_U)
    except Exception:
        codes = []
    jp = os.path.join(BASE, 'jisilu_etf_cache.json')
    if os.path.exists(jp):
        try:
            for c in json.load(open(jp, encoding='utf-8')).get('etfs', {}):
                if c not in codes:
                    codes.append(c)
        except Exception:
            pass
    return codes


def fetch(codes=None, progress=None) -> dict | None:
    ck = load_cookie()
    if not ck:
        print('[etf_track_error] 无东财 cookie（环境变量 EASTMONEY_COOKIE 或 eastmoney_cookie.txt 均缺失）：跳过。')
        return None
    if codes is None:
        # 默认只抓 60 只主流 ETF（B 映射表覆盖的宇宙）；其余 1437 只集思录 ETF 不在映射表、
        # 场内直取又必空，全抓只会在进哥机器上白打上千个请求、还猛刷东财。按需可显式传 codes 扩面。
        try:
            from etf_data import ETF_UNIVERSE as _U
            codes = list(_U)
        except Exception:
            codes = []
    H = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://fundf10.eastmoney.com/',
        'Cookie': ck,
    }

    def _try_one(code):
        code = str(code).strip()
        if len(code) != 6 or not code.isdigit():
            return None
        url = URL_TMPL.format(code=code, rt=time.time())
        try:
            req = urllib.request.Request(url, headers=H)
            data = urllib.request.urlopen(req, timeout=15).read().decode('utf-8', 'ignore')
            m = re.search(r'var apidata\s*=\s*(\[.*?\]);', data, re.S)
            body = m.group(1).strip() if m else ''
            if not body or body == '[]':
                return None
            arr = json.loads(body)
            for row in reversed(arr):
                if isinstance(row, (list, tuple)) and len(row) >= 2 and row[1] not in (None, '-', ''):
                    return {'name': None, 'track_err': _f(row[1]), 'as_of': row[0]}
        except Exception as e:  # noqa: BLE001
            print('[etf_track_error] %s 失败: %s' % (code, e))
        return None

    result: dict = {}
    for code in codes:
        code = str(code).strip()
        if len(code) != 6 or not code.isdigit():
            continue
        v = _try_one(code)
        approx = False
        via = None
        name = None
        # B 方案：场内 ETF 直取为空 → 自动用同指数联接/指数基金代码重抓（近似值）
        if v is None and code in ETF_LINK_MAP:
            link = ETF_LINK_MAP[code]
            lv = _try_one(link['code'])
            if lv is not None:
                v = lv
                approx = True
                via = link['code']
                name = link.get('name')
        if v is not None:
            v['approx'] = approx
            v['via'] = via
            if name:
                v['name'] = name
            result[code] = v

    approx_n = sum(1 for v in result.values() if (v or {}).get('approx'))
    cache = {
        'source': 'fundf10.eastmoney.com FundArchivesDatas type=trackerr（场内 ETF 直取为空者以同指数联接基金近似，approx=True）',
        'as_of': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'n': len(result),
        'approx_n': approx_n,
        'track_error': result,
    }
    with open(CACHE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    if progress:
        progress(cache)
    return cache


if __name__ == '__main__':
    def _p(o):
        print('已写入 %s | 跟踪误差有值 %d 只（其中近似值·联接基金 %d 只）' % (CACHE, o['n'], o.get('approx_n', 0)))
        if o['n'] == 0:
            print('  ⚠️ 全部为空：东财 F10 未返回任何跟踪误差（场内 ETF 直取与联接基金近似均空）。')
        for c, v in list(o['track_error'].items())[:8]:
            tag = ' [近似值·联接基金 %s]' % v['via'] if v.get('approx') else ''
            print('  %s | 年化跟踪误差=%s%% (as_of %s)%s' % (c, v['track_err'], v['as_of'], tag))
    fetch(progress=_p)
