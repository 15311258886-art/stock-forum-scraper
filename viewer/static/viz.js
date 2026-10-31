/* ══════════════════════════════════════════════════════════════════════════
   VIZ —— 零依赖 SVG 图表基元（2026-10-31）

   为什么要有这一层：
     app.js 里已经散着 7~8 段手搓的 SVG 拼字符串（ov-chart / ov2-pie / sc-hm-svg /
     lhbw-chart …），每段都自己算坐标、自己定色、自己处理边界。结果就是——
     **每加一张图的成本 = 重写一遍**，于是图表越用越少。本文件把那套几何计算
     抽成四个可复用基元，以后加图只传数据，不再碰坐标。

   设计约定（改之前先读）：
     · 纯函数，只返回 SVG 字符串，不碰 DOM、不发请求、不引任何图表库；
     · 所有颜色走 CSS 变量（--bull / --bear / --warn / --muted / --border / --text），
       暗色主题与浅色主题自动跟随，**禁止在这里写死 #hex 色值**；
     · 涨跌配色遵循 A 股惯例：正=红（--bull），负=绿（--bear）。
       注意这与欧美相反，也和「红=危险」的 harvest 语义不同 —— 本库只画**收益类**量，
       故用涨跌色；非收益类的（如资金活跃度）请调用方自己传色，别硬套；
     · 数字格式化统一走 _num()，避免各处 toFixed 口径不一。
   ══════════════════════════════════════════════════════════════════════════ */
(function (global) {
  "use strict";

  var _ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return _ESC[c]; });
  }

  /** 固定小数位；null/NaN → "—"。绝不返回 undefined 让页面出现 "undefined"。 */
  function num(v, d) {
    if (v == null || v === "" || isNaN(v)) return "—";
    return Number(v).toFixed(d == null ? 2 : d);
  }

  /** 带正负号的百分比，用于超额收益 */
  function pct(v, d) {
    if (v == null || isNaN(v)) return "—";
    var s = num(v, d == null ? 2 : d);
    return (v > 0 ? "+" : "") + s + "%";
  }

  /** 估算文字像素宽度：CJK 按 1 个字宽、ASCII 按 0.55 字宽。用于标签防重叠，不需要精确。 */
  function tw(str, fs) {
    var w = 0, s = String(str == null ? "" : str);
    for (var i = 0; i < s.length; i++) {
      w += s.charCodeAt(i) > 0x2e80 ? 1.0 : 0.55;
    }
    return w * (fs || 11);
  }

  /** 矩形重叠判定（标签避让用） */
  function _hit(a, b) {
    return !(a.x2 <= b.x1 || b.x2 <= a.x1 || a.y2 <= b.y1 || b.y2 <= a.y1);
  }

  /** 把跨度切成「好看」的刻度步长（1 / 2 / 2.5 / 5 × 10^k）。
     必须做这一步：直接四等分会得到 1.8 / 0.7 / -0.3 这种刻度，
      零线看着像 "-0.3"，读图的人会误判方向。 */
  function niceStep(span, target) {
    var raw = Math.abs(span) / (target || 4);
    if (!(raw > 0)) return 1;
    var mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
    var norm = raw / mag;
    var f = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
    return f * mag;
  }

  /** 生成落在 [lo,hi] 内的刻度值数组 */
  function niceTicks(lo, hi, target) {
    var step = niceStep(hi - lo, target);
    var out = [];
    var k = Math.ceil(lo / step);
    // 上限 40 个，防极端数据把图撑爆
    for (var i = 0; i < 40; k++) {
      var v = k * step;
      if (v > hi + step * 1e-9) break;
      // 消掉浮点尾差（0.30000000000000004 之类）
      out.push(Math.abs(v) < step * 1e-9 ? 0 : Number(v.toFixed(10)));
      i++;
    }
    return { ticks: out, step: step };
  }

  /** 按步长决定小数位 */
  function stepDec(step) {
    if (!(step > 0)) return 0;
    var d = Math.ceil(-Math.log(step) / Math.LN10);
    return Math.max(0, Math.min(3, d));
  }

  /* ──────────────────────────────────────────────────────────────────────
     基元 1：发散条形图（diverging bars）—— 正负对照的主力图
       典型用途：一组类别各有「相对基准的超额」，一半正一半负，要一眼看出谁在零的哪边。
       rows: [{ label, value, lo, hi, t, sig, tone }]
         · lo/hi 是 95% 置信区间，画成条背后的淡色"不确定带"（不是误差棒 —— 误差棒在 24 行
           的密度下会糊成一片，淡带既表达不确定性又不抢视线）；
         · sig=false 的条目整体降到 45% 不透明度：**视觉上就该"看不清"**，
           这是刻意的 —— 不显著的结论不该和显著的结论长得一样醒目。
     参数：{ rows, unit, rowH, labelW, axisHint, note }
     ────────────────────────────────────────────────────────────────────── */
  function diverging(o) {
    o = o || {};
    var rows = o.rows || [];
    if (!rows.length) return '<div class="vz-empty">无数据</div>';

    var W = o.W || 800;
    var labelW = o.labelW || 116;
    var rowH = o.rowH || 27;
    var headH = 30;
    var footH = o.axisHint ? 34 : 18;
    var valW = 62, tW = 46, gapR = 8;
    var plotL = labelW + 6;
    var plotR = W - (valW + tW + gapR * 2 + 6);
    var pw = plotR - plotL;
    var H = headH + rows.length * rowH + footH;

    // 对称域：零线恒在正中，正负两侧等宽。这样「谁偏得远」不需要换算，肉眼直接可比。
    var m = 0;
    rows.forEach(function (r) {
      [r.value, r.lo, r.hi].forEach(function (v) {
        if (v != null && !isNaN(v)) m = Math.max(m, Math.abs(v));
      });
    });
    if (m <= 0) m = 1;
    m = m * 1.08;
    var zeroX = plotL + pw / 2;
    function sx(v) { return zeroX + (Math.max(-m, Math.min(m, v)) / m) * (pw / 2); }

    var s = [];
    s.push('<svg class="vz" viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:auto;display:block" '
      + 'role="img" aria-label="' + esc(o.aria || "发散条形图") + '">');

    // 竖向参考线：±m/2 虚线 + 零线实线
    [-0.5, 0.5].forEach(function (f) {
      var x = zeroX + f * (pw / 2);
      s.push('<line x1="' + x.toFixed(1) + '" y1="' + (headH - 8) + '" x2="' + x.toFixed(1)
        + '" y2="' + (headH + rows.length * rowH + 2) + '" stroke="var(--border,#2C313B)" '
        + 'stroke-dasharray="2 4" stroke-width="1" opacity=".7"/>');
    });
    s.push('<line x1="' + zeroX.toFixed(1) + '" y1="' + (headH - 8) + '" x2="' + zeroX.toFixed(1)
      + '" y2="' + (headH + rows.length * rowH + 2) + '" stroke="var(--muted,#A6ABB5)" stroke-width="1" opacity=".55"/>');

    // 顶部刻度
    [-1, -0.5, 0, 0.5, 1].forEach(function (f) {
      var x = zeroX + f * (pw / 2);
      s.push('<text x="' + x.toFixed(1) + '" y="' + (headH - 15) + '" text-anchor="middle" '
        + 'font-size="10" fill="var(--muted,#A6ABB5)">' + (f === 0 ? "0" : num(f * m, 1) + (o.unit || "%")) + '</text>');
    });

    rows.forEach(function (r, i) {
      var y = headH + i * rowH;
      var cy = y + rowH / 2;
      var neg = (r.value || 0) < 0;
      var col = neg ? "var(--bear,#1FB873)" : "var(--bull,#F2544C)";
      var op = r.sig === false ? 0.45 : 1;

      // 左侧显著性刻线：一眼扫出哪些是硬结论
      if (r.sig !== false) {
        s.push('<rect x="0" y="' + (y + 5) + '" width="3" height="' + (rowH - 10) + '" rx="1.5" fill="' + col + '"/>');
      }

      // 不确定带（lo..hi）
      if (r.lo != null && r.hi != null) {
        var x1 = sx(Math.min(r.lo, r.hi)), x2 = sx(Math.max(r.lo, r.hi));
        s.push('<rect x="' + x1.toFixed(1) + '" y="' + (y + 6) + '" width="' + Math.max(1, x2 - x1).toFixed(1)
          + '" height="' + (rowH - 12) + '" rx="1" fill="var(--muted,#A6ABB5)" opacity="' + (0.26 * op).toFixed(2) + '"/>');
      }

      // 点估计条（0 → value）
      var bx = sx(Math.min(0, r.value)), bw = Math.abs(sx(r.value) - zeroX);
      s.push('<rect x="' + bx.toFixed(1) + '" y="' + (y + 8) + '" width="' + Math.max(1, bw).toFixed(1)
        + '" height="' + (rowH - 16) + '" rx="1.5" fill="' + col + '" opacity="' + op.toFixed(2) + '"/>');

      // 类别名（左侧，右对齐）
      s.push('<text x="' + (labelW - 2) + '" y="' + (cy + 4) + '" text-anchor="end" font-size="12" '
        + 'fill="' + (r.sig === false ? "var(--muted,#A6ABB5)" : "var(--text,#fff)") + '">' + esc(r.label) + '</text>');

      // 数值列（右对齐，等宽）
      s.push('<text x="' + (W - tW - gapR - 4) + '" y="' + (cy + 4) + '" text-anchor="end" font-size="11.5" '
        + 'font-family="Consolas,Monaco,monospace" fill="' + col + '" opacity="' + op.toFixed(2) + '">'
        + pct(r.value, o.dec == null ? 2 : o.dec) + '</text>');

      // t 值列：不显著的只画一个 "·"，避免"一堆数字看着都很硬"
      var tTxt = (r.t == null || isNaN(r.t)) ? "" : ((r.t > 0 ? "+" : "") + num(r.t, 2));
      s.push('<text x="' + (W - 4) + '" y="' + (cy + 4) + '" text-anchor="end" font-size="10" '
        + 'font-family="Consolas,Monaco,monospace" fill="' + (r.sig === false ? "var(--muted,#A6ABB5)" : "var(--text,#fff)") + '" '
        + 'opacity="' + (r.sig === false ? 0.5 : 0.75) + '">' + esc(tTxt) + '</text>');
    });

    // 底部轴提示
    if (o.axisHint) {
      var by = headH + rows.length * rowH + 16;
      s.push('<text x="6" y="' + by + '" font-size="10.5" fill="var(--muted,#A6ABB5)">' + esc(o.axisHint) + '</text>');
    }
    s.push('</svg>');
    return s.join("");
  }

  /* ──────────────────────────────────────────────────────────────────────
     基元 2：散点图（scatter）—— 专门用来拆穿"均值好看但胜率不行"这类陷阱
       典型用途：x=日胜率、y=平均超额。四个象限，右上才是真本事，
                 左上「均值正但胜率低」= 右尾驱动 = 不可交易。这个图能一秒看出这件事。
       points: [{ x, y, label, sig, tone }]
       参数：{ points, xLabel, yLabel, xRef, yRef, xPad, yPad, W, H }
     ────────────────────────────────────────────────────────────────────── */
  function scatter(o) {
    o = o || {};
    var pts = (o.points || []).filter(function (p) { return p && !isNaN(p.x) && !isNaN(p.y); });
    if (!pts.length) return '<div class="vz-empty">无数据</div>';

    var W = o.W || 800, H = o.H || 360;
    var L = 58, R = 22, T = 20, B = 46;
    var pw = W - L - R, ph = H - T - B;

    var xs = pts.map(function (p) { return p.x; }).concat([o.xRef == null ? null : o.xRef]).filter(function (v) { return v != null; });
    var ys = pts.map(function (p) { return p.y; }).concat([o.yRef == null ? null : o.yRef]).filter(function (v) { return v != null; });
    var xmin = Math.min.apply(null, xs), xmax = Math.max.apply(null, xs);
    var ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys);
    var xpad = o.xPad == null ? (xmax - xmin) * 0.12 + 0.5 : o.xPad;
    var ypad = o.yPad == null ? (ymax - ymin) * 0.14 + 0.1 : o.yPad;
    xmin -= xpad; xmax += xpad; ymin -= ypad; ymax += ypad;
    if (xmax - xmin < 1e-9) { xmin -= 1; xmax += 1; }
    if (ymax - ymin < 1e-9) { ymin -= 1; ymax += 1; }

    function px(v) { return L + (v - xmin) / (xmax - xmin) * pw; }
    function py(v) { return T + ph - (v - ymin) / (ymax - ymin) * ph; }

    var xr = o.xRef == null ? null : px(o.xRef);
    var yr = o.yRef == null ? null : py(o.yRef);

    var s = [];
    s.push('<svg class="vz" viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:auto;display:block" '
      + 'role="img" aria-label="' + esc(o.aria || "散点图") + '">');

    // 象限底色：右上（胜率过半且超额为正）= 真有效区，轻轻染红；左下 = 真该躲，轻染绿
    if (xr != null && yr != null) {
      s.push('<rect x="' + xr.toFixed(1) + '" y="' + T + '" width="' + (L + pw - xr).toFixed(1)
        + '" height="' + (yr - T).toFixed(1) + '" fill="var(--bull,#F2544C)" opacity=".05"/>');
      s.push('<rect x="' + L + '" y="' + yr.toFixed(1) + '" width="' + (xr - L).toFixed(1)
        + '" height="' + (T + ph - yr).toFixed(1) + '" fill="var(--bear,#1FB873)" opacity=".06"/>');
    }
    // 象限分割线
    if (yr != null) {
      s.push('<line x1="' + L + '" y1="' + yr.toFixed(1) + '" x2="' + (L + pw) + '" y2="' + yr.toFixed(1)
        + '" stroke="var(--muted,#A6ABB5)" stroke-width="1" stroke-dasharray="5 4" opacity=".75"/>');
    }
    if (xr != null) {
      s.push('<line x1="' + xr.toFixed(1) + '" y1="' + T + '" x2="' + xr.toFixed(1) + '" y2="' + (T + ph)
        + '" stroke="var(--muted,#A6ABB5)" stroke-width="1" stroke-dasharray="5 4" opacity=".75"/>');
    }
    // 外框
    s.push('<rect x="' + L + '" y="' + T + '" width="' + pw + '" height="' + ph
      + '" fill="none" stroke="var(--border,#2C313B)" stroke-width="1"/>');

    // 轴刻度 + 轴标题（用 nice ticks，保证零线一定有刻度且数值好读）
    var xt = niceTicks(xmin, xmax, 5), yt = niceTicks(ymin, ymax, 5);
    var xd = stepDec(xt.step), yd = stepDec(yt.step);
    xt.ticks.forEach(function (xv) {
      var xx = px(xv);
      s.push('<text x="' + xx.toFixed(1) + '" y="' + (T + ph + 15) + '" text-anchor="middle" font-size="10" fill="var(--muted,#A6ABB5)">'
        + num(xv, xd) + '</text>');
    });
    yt.ticks.forEach(function (yv) {
      var yy = py(yv);
      s.push('<text x="' + (L - 8) + '" y="' + (yy + 3.5) + '" text-anchor="end" font-size="10" fill="var(--muted,#A6ABB5)">'
        + num(yv, yd) + '</text>');
      s.push('<line x1="' + L + '" y1="' + yy.toFixed(1) + '" x2="' + (L + pw) + '" y2="' + yy.toFixed(1)
        + '" stroke="var(--border,#2C313B)" stroke-width="1" opacity=".45"/>');
    });
    if (o.xLabel) {
      s.push('<text x="' + (L + pw / 2).toFixed(1) + '" y="' + (H - 10) + '" text-anchor="middle" font-size="11" fill="var(--muted,#A6ABB5)">'
        + esc(o.xLabel) + '</text>');
    }
    if (o.yLabel) {
      s.push('<text x="14" y="' + (T + ph / 2).toFixed(1) + '" text-anchor="middle" font-size="11" fill="var(--muted,#A6ABB5)" '
        + 'transform="rotate(-90 14 ' + (T + ph / 2).toFixed(1) + ')">' + esc(o.yLabel) + '</text>');
    }

    // 点（先画不显著的，显著的压在上面）
    var order = pts.slice().sort(function (a, b) {
      return (a.sig === false ? 0 : 1) - (b.sig === false ? 0 : 1);
    });
    order.forEach(function (p) {
      var col = p.y < 0 ? "var(--bear,#1FB873)" : "var(--bull,#F2544C)";
      var op = p.sig === false ? 0.42 : 1;
      var r = p.sig === false ? 4 : 5.4;
      s.push('<circle cx="' + px(p.x).toFixed(1) + '" cy="' + py(p.y).toFixed(1) + '" r="' + r
        + '" fill="' + col + '" opacity="' + op.toFixed(2) + '" stroke="var(--panel,#20232B)" stroke-width="1.2"/>');
    });

    // 标签避让：按 y 排序后逐个尝试候选偏移，第一个不撞的才用
    var placed = [];
    var labelled = order.filter(function (p) { return p.label && p.sig !== false; });
    labelled.sort(function (a, b) { return py(a.y) - py(b.y); });
    labelled.forEach(function (p) {
      var cx = px(p.x), cy = py(p.y), w = tw(p.label, 10.5), h = 12;
      var cands = [
        [8, -3], [8, 10], [-8 - w, -3], [8, -14], [-8 - w, 10], [-w / 2, -13]
      ];
      for (var k = 0; k < cands.length; k++) {
        var bx1 = cx + cands[k][0], by1 = cy + cands[k][1];
        var box = { x1: bx1 - 2, y1: by1 - h + 2, x2: bx1 + w + 2, y2: by1 + 4 };
        if (box.x1 < L || box.x2 > L + pw || box.y1 < T || box.y2 > T + ph) continue;
        var bad = false;
        for (var q = 0; q < placed.length; q++) { if (_hit(box, placed[q])) { bad = true; break; } }
        if (bad) continue;
        placed.push(box);
        s.push('<text x="' + bx1.toFixed(1) + '" y="' + by1.toFixed(1) + '" font-size="10.5" fill="var(--text,#fff)" '
          + 'opacity=".92" stroke="var(--panel,#20232B)" stroke-width="2.6" paint-order="stroke">' + esc(p.label) + '</text>');
        break;
      }
    });

    s.push('</svg>');
    return s.join("");
  }

  /* ──────────────────────────────────────────────────────────────────────
     基元 3：同一类别×多个持有期的横向分组条（groupBars）
       典型用途：T+1 / T+5 / T+20 三期并排 —— 看"利空是不是越拿越亏"。
       同一色相的深浅表达期数（不是三种颜色，避免读者以为在比不同性质的量）。
       groups: [{ label, values: [{ k, v, sig }] }]，keys: ["T+1","T+5","T+20"]
     ────────────────────────────────────────────────────────────────────── */
  function groupBars(o) {
    o = o || {};
    var groups = o.groups || [];
    var keys = o.keys || [];
    if (!groups.length || !keys.length) return '<div class="vz-empty">无数据</div>';

    var W = o.W || 800;
    var labelW = o.labelW || 116;
    var barH = 11, barGap = 3;
    var groupPad = 12;
    var groupH = keys.length * (barH + barGap) + groupPad;
    var headH = 30, footH = 20;
    // 数值列必须完全落在绘图区之外：早先写成 plotR = W - valW - gapR - 6 且文本右对齐到
    // W - valW - gapR - 2，结果文本左缘压进绘图区，最后一根条直接盖住数字。
    var valW = 64, gapR = 8;
    var plotL = labelW + 6, plotR = W - valW - gapR - 6;
    var pw = plotR - plotL;
    var H = headH + groups.length * groupH + footH;

    var mn = 0, mx = 0;
    groups.forEach(function (g) {
      (g.values || []).forEach(function (v) {
        if (v.v == null || isNaN(v.v)) return;
        mn = Math.min(mn, v.v); mx = Math.max(mx, v.v);
      });
    });
    if (mn === 0 && mx === 0) { mn = -1; mx = 1; }
    mn = mn * 1.06; mx = mx * 1.06;
    var span = mx - mn || 1;
    function sx(v) { return plotL + (v - mn) / span * pw; }
    var zeroX = sx(0);

    var opByKey = {};
    // 期数用**深浅**表达：0.95 / 0.72 / 0.52。
    // 注意：不显著**不能**也用降透明度表达 —— 那会和"T+20 本来就浅"撞车，
    // 读者分不清一条淡色条是"远期"还是"没统计意义"。所以不显著改用**空心虚线**，
    // 两套视觉语言互不占用。
    keys.forEach(function (k, i) { opByKey[k] = 0.95 - i * (0.43 / Math.max(1, keys.length - 1)); });

    var s = [];
    s.push('<svg class="vz" viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:auto;display:block" '
      + 'role="img" aria-label="' + esc(o.aria || "分期对照条形图") + '">');
    s.push('<line x1="' + zeroX.toFixed(1) + '" y1="' + (headH - 8) + '" x2="' + zeroX.toFixed(1)
      + '" y2="' + (headH + groups.length * groupH) + '" stroke="var(--muted,#A6ABB5)" stroke-width="1" opacity=".6"/>');
    s.push('<text x="' + zeroX.toFixed(1) + '" y="' + (headH - 14) + '" text-anchor="middle" font-size="10" fill="var(--muted,#A6ABB5)">0</text>');

    groups.forEach(function (g, gi) {
      var gy = headH + gi * groupH + groupPad / 2;
      s.push('<text x="' + (labelW - 2) + '" y="' + (gy + (keys.length * (barH + barGap)) / 2 + 3) + '" '
        + 'text-anchor="end" font-size="12" fill="var(--text,#fff)">' + esc(g.label) + '</text>');
      keys.forEach(function (k, ki) {
        var rec = (g.values || []).filter(function (x) { return x.k === k; })[0];
        if (!rec || rec.v == null || isNaN(rec.v)) return;
        var y = gy + ki * (barH + barGap);
        var col = rec.v < 0 ? "var(--bear,#1FB873)" : "var(--bull,#F2544C)";
        var op = opByKey[k] || 0.8;
        var bx = sx(Math.min(0, rec.v)), bw = Math.abs(sx(rec.v) - zeroX);
        var barAttrs = 'x="' + bx.toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + Math.max(1, bw).toFixed(1)
          + '" height="' + barH + '" rx="1.5" fill="' + col + '" opacity="' + op.toFixed(2) + '"';
        if (rec.sig === false) {
          // 空心虚线 = 该持有期不显著（统计上不可区分于 0）
          s.push('<rect ' + barAttrs + ' fill="none" fill-opacity="0" stroke="' + col
            + '" stroke-width="1" stroke-dasharray="3 2.5" opacity=".8"/>');
        } else {
          s.push('<rect ' + barAttrs + '/>');
        }
        s.push('<text x="' + (W - 6) + '" y="' + (y + barH - 1.5).toFixed(1)
          + '" text-anchor="end" font-size="9.5" font-family="Consolas,Monaco,monospace" fill="' + col
          + '" opacity="' + Math.min(1, op + 0.15).toFixed(2) + '">' + pct(rec.v, 2) + '</text>');
      });
    });

    if (o.axisHint) {
      s.push('<text x="6" y="' + (H - 6) + '" font-size="10.5" fill="var(--muted,#A6ABB5)">' + esc(o.axisHint) + '</text>');
    }
    s.push('</svg>');
    return s.join("");
  }

  /* ──────────────────────────────────────────────────────────────────────
     基元 4：迷你条（microBar）—— 给表格单元格用，一行数字里塞一根条
       比在表里只放数字更容易扫出极值。内联，不占额外行。
     ────────────────────────────────────────────────────────────────────── */
  function microBar(v, max, color) {
    var p = max > 0 ? Math.min(1, Math.abs(v) / max) : 0;
    var w = 56, h = 7;
    var col = color || (v < 0 ? "var(--bear,#1FB873)" : "var(--bull,#F2544C)");
    var bx = v < 0 ? (w / 2 - p * (w / 2)) : (w / 2);
    // 底轨：没有它时，|值| 小的条只剩 2~3px，看起来像渲染坏了。
    // 有底轨之后读者能看出"这条相对满格有多长"，语义仍然线性、不失真。
    return '<svg class="vz-mini" viewBox="0 0 ' + w + ' ' + h + '" style="width:' + w + 'px;height:' + h
      + 'px;vertical-align:middle" aria-hidden="true">'
      + '<rect x="0" y="0" width="' + w + '" height="' + h + '" rx="' + (h / 2)
      + '" fill="var(--muted,#A6ABB5)" opacity=".13"/>'
      + '<line x1="' + (w / 2) + '" y1="1" x2="' + (w / 2) + '" y2="' + (h - 1)
      + '" stroke="var(--muted,#A6ABB5)" stroke-width="1" opacity=".5"/>'
      + '<rect x="' + bx.toFixed(1) + '" y="1" width="' + Math.max(1.5, p * (w / 2)).toFixed(1)
      + '" height="' + (h - 2) + '" rx="1" fill="' + col + '" opacity=".92"/></svg>';
  }

  /* 图例：色块 + 文字，一行。dashed=true 时画成空心描边（对应"不显著"的编码） */
  function legend(items) {
    return '<div class="vz-legend">' + (items || []).map(function (it) {
      var style = it.dashed
        ? 'background:transparent;border:1.5px dashed ' + it.color
        : 'background:' + it.color + ';opacity:' + (it.op == null ? 1 : it.op);
      return '<span class="vz-lg-i"><i style="' + style + '"></i>'
        + esc(it.text) + '</span>';
    }).join("") + '</div>';
  }

  global.VIZ = {
    esc: esc, num: num, pct: pct, tw: tw,
    diverging: diverging, scatter: scatter, groupBars: groupBars,
    microBar: microBar, legend: legend
  };
})(window);
