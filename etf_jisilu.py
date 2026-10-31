# -*- coding: utf-8 -*-
"""集思录 ETF 数据抓取（B 栏溢价率/规模/费率/真实份额净申赎的来源）。

数据来源：https://www.jisilu.cn/data/etf/etf_list/ （需进哥提供的登录态 cookie）
  —— 该接口覆盖 1400+ 只 ETF，含：discount_rt(IOPV折溢价率)、iopv、fund_nav(净值)、
     fee/m_fee/t_fee(管理费+托管费)、index_nm(跟踪指数)、amount_incr/amount_increase_rt(份额净申赎)、
     unit_total(总份额，×净值可估算规模)。**不含跟踪误差字段**（该维仍待接入，需东财 F10）。

cookie 获取（进哥操作）：浏览器登录 jisilu.cn → F12 → Network → 复制 ETF 页的 XHR 请求为
  "Copy as cURL" 或 直接导出 Netscape cookie 存为 jisilu_cookie.txt（本项目根目录）。
  脚本优先读环境变量 JISILU_COOKIE，其次读 jisilu_cookie.txt。

产出：jisilu_etf_cache.json（与 etf_advisory_cache.json 同级），etf.py 的 etf_advisory() 离线读它，
  点亮 B 栏 溢价率/规模/费率 3 维 + 升级资金流为真实份额净申赎。请求路径不写死 cookie 到代码。
"""
from __future__ import annotations
import os, sys, json
from datetime import datetime

for k in ['http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY','all_proxy','ALL_PROXY']:
    os.environ.pop(k, None)
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(BASE, 'jisilu_cookie.txt')
CACHE = os.path.join(BASE, 'jisilu_etf_cache.json')
URL = 'https://www.jisilu.cn/data/etf/etf_list/'

# 2026-11-01 P1-5：凭据读取改走 viewer.secrets_dpapi —— 优先 `<名>.dpapi`（Windows DPAPI 密文），
#   缺失/解不开时回退项目根同名明文（回退时 `secrets_dpapi.load` 会打一条限频 WARNING）。
# 为什么用 try/except 包住：本脚本有两个入口方式 —— 被 `viewer/etf.py` 当模块导入（工程根在
#   sys.path 上），以及被直接 `python etf_jisilu.py` 跑（此时 sys.path[0] 是工程根，也在）。
#   但只要有第三种调用方式没把工程根放进 sys.path，裸 `from viewer import ...` 就会 ImportError，
#   而 Cookie 是抓取的硬依赖 —— 绝不能让"加密改造的 import 失败"把整条采集带崩。
#   导入失败 → `_secrets = None` → 退回改造前的明文直读，行为完全不变，只是少了加密保护。
try:
    if BASE not in sys.path:
        sys.path.insert(0, BASE)
    from viewer import secrets_dpapi as _secrets
except Exception:  # noqa: BLE001
    _secrets = None


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


def _read_cookie_text() -> str | None:
    """读取 jisilu cookie 文本：优先 DPAPI 密文，回退项目根明文；都没有返回 None。

    回退是**刻意**的：加密迁移没做完之前（读取点改造 ≠ 文件已加密），
    不能让"多了一层加密"把线上采集改停。回退由 `secrets_dpapi` 打一条限频 WARNING
    作为迁移提醒（同一进程同一天只一条，避免每抓一轮刷一行把日志淹掉）。
    """
    if _secrets is not None:
        txt = _secrets.load('jisilu_cookie.txt')
        if txt is not None:
            return txt
    if os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE, encoding='utf-8') as fh:
                return fh.read()
        except OSError:
            return None
    return None


def load_cookie() -> str | None:
    env = os.environ.get('JISILU_COOKIE')
    if env:
        return env
    text = _read_cookie_text()
    if text is None:
        return None
    out = []
    for line in text.splitlines():
        line = line.rstrip('\n')
        if not line.strip() or line.startswith('#'):
            continue
        p = line.split('\t')
        if len(p) >= 7:
            out.append('%s=%s' % (p[5], p[6]))
    return '; '.join(out)


def fetch(progress=None) -> dict | None:
    ck = load_cookie()
    if not ck:
        print('[etf_jisilu] 无 cookie（环境变量 JISILU_COOKIE 或 jisilu_cookie.txt 均缺失）：跳过。')
        return None
    H = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.jisilu.cn/data/etf/',
        'X-Requested-With': 'XMLHttpRequest',
        'Cookie': ck,
    }
    r = requests.get(URL, headers=H, timeout=30)
    data = r.json()
    rows = data.get('rows') or []
    etfs: dict = {}
    for it in rows:
        c = it.get('cell') or {}
        code = str(c.get('fund_id', '')).strip()
        if len(code) != 6 or not code.isdigit():
            continue
        fnav = _f(c.get('fund_nav'))
        unit = _f(c.get('unit_total'))
        scale = (unit * fnav) if (unit is not None and fnav is not None) else None  # 估算规模（份额×净值，单位亿元）
        # 真溢价率字段是 nav_discount_rt（净值折溢价率）；discount_rt / iopv_discount_rt 在本接口已废弃为空
        prem = c.get('nav_discount_rt')
        prem = _f(prem) if prem not in (None, '-') else None
        etfs[code] = {
            'name': c.get('fund_nm'),
            'index_nm': c.get('index_nm'),
            'price': _f(c.get('price')),
            'increase_rt': _f(c.get('increase_rt')),
            'premium_rt': prem,
            'iopv': _f(c.get('iopv')),
            'fund_nav': fnav,
            'amount': _f(c.get('amount')),
            'amount_incr': _f(c.get('amount_incr')),
            'amount_increase_rt': _f(c.get('amount_increase_rt')),
            'm_fee': _f(c.get('m_fee')),
            't_fee': _f(c.get('t_fee')),
            'fee': _f(c.get('fee')),
            'unit_total': unit,
            'scale_est': scale,
            'issuer_nm': c.get('issuer_nm'),
            'last_dt': c.get('last_dt'),
        }
    cache = {
        'source': 'jisilu.cn/data/etf/etf_list',
        'as_of': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'n_total': len(etfs),
        'etfs': etfs,
    }
    with open(CACHE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    if progress:
        progress(cache)
    return cache


if __name__ == '__main__':
    def _p(o):
        print('已写入 %s | ETF %d 只' % (CACHE, o['n_total']))
        for c, v in list(o['etfs'].items())[:4]:
            print('  %s %s | 溢价率=%s%% 规模估算=%s亿 费率=%s%% 份额净申赎=%s%% 跟踪指数=%s'
                  % (c, v['name'], v['premium_rt'], v['scale_est'], v['fee'],
                     v['amount_increase_rt'], v['index_nm']))
    fetch(progress=_p)
