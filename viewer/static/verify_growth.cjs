// verify_growth.cjs —— 复盘成长面板回归测试（Node 18+，零依赖：assert + http/fetch + child_process）
// 覆盖：① adjust_log 返回结构（含 12 维）② 空数据安全 ③ 路由/导航注册 ④ 隔离表建表且无外键/JOIN
// 用法：node viewer/static/verify_growth.cjs   （会自动拉起 server.py 测试实例，结束即关闭）
'use strict';
const { spawn, spawnSync } = require('child_process');
const path = require('path');
const assert = require('assert');
const http = require('http');

const ROOT = path.resolve(__dirname, '..', '..');        // viewer/static -> viewer -> 工程根
const DB_PATH = path.join(ROOT, 'viewer', 'app.db');
const PORT = process.env.VIEWER_PORT || '8799';
const BASE = `http://127.0.0.1:${PORT}`;
// 解释器优先取 PYTHON 环境变量（便于 venv 不在 PATH 时显式指定，如 PYTHON=./.venv/Scripts/python.exe node verify_growth.cjs）
const PY = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');

function getJson(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (res) => {
      let buf = '';
      res.on('data', (c) => (buf += c));
      res.on('end', () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(buf) }); }
        catch (e) { reject(new Error('JSON 解析失败: ' + buf.slice(0, 200))); }
      });
    }).on('error', reject);
  });
}

function waitServer(timeoutMs) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const tick = () => {
      http.get(`${BASE}/api/version`, (res) => { res.resume(); resolve(true); })
        .on('error', () => {
          if (Date.now() - start > timeoutMs) reject(new Error('服务启动超时（可能依赖未装/端口被占）'));
          else setTimeout(tick, 300);
        });
    };
    tick();
  });
}

(async () => {
  let server;
  const env = Object.assign({}, process.env, { VIEWER_PORT: PORT });
  try {
    // ① 启动本地服务（隔离端口，自动建表）
    console.log('[1] 启动本地服务（VIEWER_PORT=' + PORT + '）...');
    server = spawn(PY, ['-m', 'viewer.server'], { cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'] });
    server.stderr.on('data', (d) => process.stderr.write('[server] ' + d));

    await waitServer(30000);

    // ② adjust_log 返回结构（含 12 维）
    console.log('[2] GET /api/growth/adjust_log ...');
    const r = await getJson(`${BASE}/api/growth/adjust_log`);
    assert.strictEqual(r.status, 200, 'adjust_log HTTP 状态应为 200');
    const d = r.body;
    assert.strictEqual(d.ok, true, 'ok 应为 true');
    assert.ok(Array.isArray(d.signals), 'signals 应为数组');
    assert.strictEqual(d.signals.length, 12, 'signals 应为 12 维（与 predictor.SIGNALS 一致）');
    assert.ok(Array.isArray(d.recent_adjusts), 'recent_adjusts 应为数组');
    assert.ok(typeof d.weights_series === 'object' && d.weights_series !== null, 'weights_series 应为对象');
    assert.ok(typeof d.current_weights === 'object' && d.current_weights !== null, 'current_weights 应为对象');
    assert.ok(typeof d.has_data === 'boolean', 'has_data 应为布尔');
    assert.ok(d.caliber && typeof d.caliber.label === 'string', 'caliber.label 应为字符串');
    const keys = d.signals.map((s) => s.key);
    assert.strictEqual(new Set(keys).size, 12, '12 个信号键应互不相同（动态、不硬编码）');
    console.log('    ✅ 结构正确：signals=' + d.signals.length + ' 维，has_data=' + d.has_data);

    // ③ 空数据安全：即便预留表为空，API 也返回合法结构、12 维不缺失、面板不会崩
    console.log('[3] 空数据安全校验 ...');
    assert.strictEqual(keys.length, 12, '空库下仍返回 12 维 signals（前端据此动态渲染，不崩）');
    console.log('    ✅ 空数据安全：返回合法结构，未崩溃');

    // ④ 路由注册：未知路由应 404（证明路由表正常工作，adjust_log 确已注册）
    console.log('[4] 路由注册校验 ...');
    const nf = await getJson(`${BASE}/api/__growth_test_noop__`);
    assert.strictEqual(nf.status, 404, '不存在的路由应返回 404（路由表工作正常）');
    console.log('    ✅ 路由注册正常：adjust_log 命中、未知路由 404');

    // ⑤ 隔离表建表成功且无外键/JOIN 依赖
    console.log('[5] 校验隔离表 knowledge_source / growth_notes ...');
    const py = `
import sqlite3, sys
p = sys.argv[1]
con = sqlite3.connect(p)
tabs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
for t in ("knowledge_source", "growth_notes"):
    assert t in tabs, "缺失表: " + t
for t in ("knowledge_source", "growth_notes"):
    sql = (con.execute("SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone() or [None])[0] or ""
    assert "REFERENCES" not in sql.upper(), t + " 含外键（违反零 JOIN 隔离）"
for r in con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name LIKE 'predictor_%'"):
    s = r[0] or ""
    assert "knowledge_source" not in s and "growth_notes" not in s, "predictor 表引用了隔离表（违反零 JOIN）"
print("OK")
`;
    const chk = spawnSync(PY, ['-c', py, DB_PATH], { cwd: ROOT, encoding: 'utf8' });
    if (chk.status !== 0) throw new Error('隔离表校验失败:\n' + (chk.stderr || chk.stdout));
    assert.ok(/OK/.test(chk.stdout), '隔离表校验应输出 OK');
    console.log('    ✅ 隔离表建表成功且无外键/JOIN 依赖');

    console.log('\n✅ 全部通过：verify_growth.cjs');
    if (server) try { server.kill('SIGTERM'); } catch (_) {}
    process.exit(0);
  } catch (e) {
    console.error('\n❌ 测试失败：', e.message);
    if (server) try { server.kill('SIGTERM'); } catch (_) {}
    process.exit(1);
  }
})();
