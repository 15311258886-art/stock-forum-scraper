const { execFileSync } = require('child_process');
const http = require('http');

function probe(port, timeout = 700) {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port, path: '/json/version', timeout }, (res) => {
      let d = '';
      res.on('data', (c) => (d += c));
      res.on('end', () => {
        let j = null;
        try { j = JSON.parse(d); } catch { /* not CDP */ }
        resolve(j && j.Browser ? { port, browser: j.Browser, ws: j.webSocketDebuggerUrl || '' } : null);
      });
    });
    req.on('error', () => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
  });
}

(async () => {
  let out = '';
  try {
    out = execFileSync('C:/Windows/System32/netstat.exe', ['-ano'], { encoding: 'utf8' });
  } catch (e) { console.log('netstat failed', e.message); return; }

  const ports = new Set();
  for (const line of out.split(/\r?\n/)) {
    const m = line.match(/TCP\s+(\d+\.\d+\.\d+\.\d+|\[::\]):(\d+)\s+\S+\s+LISTENING/);
    if (m) ports.add(Number(m[2]));
  }
  const list = [...ports].sort((a, b) => a - b);
  console.log('listening ports:', list.length);

  const found = [];
  for (const p of list) {
    const r = await probe(p);
    if (r) found.push(r);
  }
  if (found.length) {
    console.log('=== CDP FOUND ===');
    for (const f of found) console.log(f.port, '|', f.browser, '|', f.ws);
  } else {
    console.log('=== NO CDP ENDPOINT ===');
  }
})();
