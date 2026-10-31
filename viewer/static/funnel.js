/* ===== 方法共振 / 多层筛选漏斗（2026-10-26 重构：P1 接入真实方法判定）
   架构：板块选池（舆情池=豆包文本挖名 / 官方池=东财概念行业全量成员）
        → 逐层方法算子（可开关/排序/设保留方向）→ 共识结果表 + 逐层 AND 收缩。
   四方法方向统一 dir ∈ {看涨,看跌,中性,未覆盖}；颜色：看涨=红、看跌=绿、中性=灰、未覆盖=虚线灰。 */
window.DOUYIN = window.DOUYIN || {};
(function () {
  var FN_PLATES = ["AI", "半导体", "机器人", "黄金", "券商", "银行", "光伏", "消费", "地产",
    "算力", "锂电", "白酒", "军工", "软件", "汽车", "芯片", "游戏"];
  var FN_METHODS = [
    { id: "kronos", name: "K线预测", engine: "kronos.py", src: "/api/kronos",
      desc: "基础模型对 K 线做概率预测，输出看涨/看跌 + 预期收益。" },
    { id: "doubao_tech", name: "豆包技术方法", engine: "douyin_corpus", src: "/api/douyin/stances",
      desc: "豆包对个股技术面立场标注：看多 / 看空 / 板块机会 / 风险提示。" },
    { id: "resonance", name: "豆包×主力共振", engine: "resonance.py + 六源信号", src: "/api/resonance",
      desc: "个股共振 + 六源统一信号两路加权判方向（原「多博主共振」已下线：实证反向且源停更）。" },
    { id: "bili_value", name: "技术价值教学", engine: "bili_methods.py", src: "/api/bili/methods",
      desc: "B站技术·价值教学方法卡片聚合，提炼可复用的买卖框架。" }
  ];
  // 默认官方池：从东财官方板块全量成员（干净宇宙）里挑，而非被博主文本挖名绑架的舆情池。
  var FN = { plate: "AI", mode: "official", pool: [], score: null, loading: false, showAll: false, methods: null };

  function initMethods() {
    if (FN.methods) return;
    FN.methods = FN_METHODS.map(function (m, i) {
      return { id: m.id, on: true, keep: "bull", order: i };
    });
  }
  function sortedMethods() {
    return FN.methods.slice().sort(function (a, b) { return a.order - b.order; });
  }
  function methodMeta(id) {
    for (var i = 0; i < FN_METHODS.length; i++) if (FN_METHODS[i].id === id) return FN_METHODS[i];
    return null;
  }
  function errText(e) {
    return (window.apiErrText ? window.apiErrText(e) : (e && e.message ? e.message : String(e)));
  }
  function dirBadge(dir) {
    var cls = dir === "看涨" ? "bull" : dir === "看跌" ? "bear" : dir === "中性" ? "neu" : "ncov";
    return '<span class="ic-badge ' + cls + '">' + esc(dir) + '</span>';
  }
  // 某方法对单票是否"通过"其保留方向（未覆盖仅在保留=全部时通过，保持严格诚实）
  function methodPass(dir, keep) {
    if (dir === "未覆盖") return keep === "all";
    if (keep === "all") return true;
    if (keep === "bull") return dir === "看涨";
    if (keep === "bear") return dir === "看跌";
    if (keep === "neutral") return dir === "中性";
    return false;
  }
  function keepLabel(k) {
    return k === "bull" ? "看涨" : k === "bear" ? "看跌" : k === "neutral" ? "中性" : "全部";
  }

  function renderFunnel() {
    var box = ensurePanel("funnel");
    if (!box) return;
    initMethods();
    box.innerHTML = ''
      + '<div class="fn-wrap">'
      +   '<div class="fn-hd"><h2>方法共振 · 多层筛选漏斗</h2>'
      +     '<span class="fn-sub">板块选池 → 逐层方法算子 → 共识结果（P1：真实方法判定已接入）</span></div>'
      +   '<div class="fn-pick">'
      +     '<div class="fn-chips" id="fnChips"></div>'
      +     '<input class="fn-input" id="fnInput" placeholder="或输入板块名，如 半导体" />'
      +     '<button class="fn-run" id="fnGo">出结论</button>'
      +   '</div>'
      +   '<div class="fn-modes" id="fnModes">'
      +     '<span class="fn-mode' + (FN.mode === "sentiment" ? ' on' : '') + '" data-mode="sentiment">舆情池</span>'
      +     '<span class="fn-mode' + (FN.mode === "official" ? ' on' : '') + '" data-mode="official">官方池</span>'
      +     '<button class="fn-run fn-refresh" id="fnRefresh">↻ 刷新</button>'
      +   '</div>'
      +   '<div id="fnPool"></div>'
      +   '<div class="fn-methods" id="fnMethods"></div>'
      +   '<div id="fnResult"></div>'
      + '</div>';
    renderChips();
    bindPick();
    bindModes();
    loadPlate(FN.plate);
  }

  function renderChips() {
    var c = document.getElementById("fnChips");
    if (!c) return;
    c.innerHTML = FN_PLATES.map(function (p) {
      return '<span class="dy-chip' + (p === FN.plate ? ' dy-chip--on' : '') + '" data-plate="' + escAttr(p) + '">' + esc(p) + '</span>';
    }).join("");
    Array.prototype.forEach.call(c.querySelectorAll('.dy-chip'), function (el) {
      el.onclick = function () { FN.plate = el.dataset.plate; renderChips(); loadPlate(FN.plate); };
    });
  }

  function bindPick() {
    var inp = document.getElementById("fnInput");
    var go = document.getElementById("fnGo");
    if (go) go.onclick = function () {
      var v = (inp && inp.value || "").trim();
      if (v) { FN.plate = v; renderChips(); }
      loadPlate(FN.plate);
    };
    if (inp) inp.onkeydown = function (e) { if (e.key === "Enter" && go) go.onclick(); };
  }

  function bindModes() {
    var box = document.getElementById("fnModes");
    if (!box) return;
    Array.prototype.forEach.call(box.querySelectorAll('.fn-mode'), function (el) {
      el.onclick = function () {
        FN.mode = el.dataset.mode;
        Array.prototype.forEach.call(box.querySelectorAll('.fn-mode'), function (x) {
          x.classList.toggle('on', x.dataset.mode === FN.mode);
        });
        loadPlate(FN.plate);
      };
    });
    var rf = document.getElementById("fnRefresh");
    if (rf) rf.onclick = function () { loadPlate(FN.plate); };
  }

  function loadPlate(plate) {
    var poolBox = document.getElementById("fnPool");
    if (!poolBox) return;
    poolBox.innerHTML = '<div class="fn-pool"><div class="fn-pool-t">正在计算「' + esc(plate)
      + '」板块「' + (FN.mode === "official" ? "官方池" : "舆情池") + '」的四方法共振…</div></div>';
    FN.loading = true;
    api("/api/funnel/score?plate=" + encodeURIComponent(plate) + "&mode=" + encodeURIComponent(FN.mode))
      .then(function (d) {
        FN.loading = false;
        if (!d || d.ok !== true || !d.pool) {
          poolBox.innerHTML = '<div class="fn-pool"><div class="fn-pool-t">板块数据读取失败：'
            + esc((d && d.note) || "接口异常") + '，请稍后重试。</div></div>';
          return;
        }
        FN.score = d;
        FN.pool = d.pool;
        renderPool(d);
        renderMethods();
        renderResult();
      })
      .catch(function (e) {
        FN.loading = false;
        poolBox.innerHTML = '<div class="fn-pool"><div class="fn-pool-t">板块数据读取失败：' + esc(errText(e)) + '</div></div>';
      });
  }

  // 干净宇宙透明卡：展示从全A剔除了多少垃圾、剩多少可入选（进哥：不在垃圾堆里挑垃圾）
  function renderClean(d) {
    if (!d) return '';
    var cu = d.clean_universe || {};
    var ex = d.excluded || {};
    var by = ex.by_reason || {};
    var parts = [];
    if (by.st) parts.push('ST/退市 <b>' + by.st + '</b>');
    if (by.blogger_noise) parts.push('博主噪声 <b>' + by.blogger_noise + '</b>');
    if (by.no_kline) parts.push('无K线 <b>' + by.no_kline + '</b>');
    var removedTxt = parts.length
      ? ('已剔除 <b>' + (ex.total || 0) + '</b> 只（' + parts.join(' · ') + '）')
      : '剔除池为空';
    var cuTxt = (cu.clean != null)
      ? ('全A ' + (cu.total_a || 0) + ' → 干净宇宙 <b>' + cu.clean + '</b> 只（'
        + (100 * cu.clean / (cu.total_a || 1)).toFixed(1) + '%）')
      : '';
    return '<div class="fn-clean">'
      + '<div class="fn-clean-h">🧹 干净宇宙 · 选股负面清单透明卡</div>'
      + '<div class="fn-clean-b">' + removedTxt + '　|　' + cuTxt + '</div>'
      + '<div class="fn-clean-note">博主推荐实证 T+5 平均 −2%~−3%、60%~66% 下跌（反向/噪声信号），'
      + '已从「正选宇宙」剔除，仅作噪声监控保留原始数据，不删除。</div>'
      + '</div>';
  }

  function renderPool(d) {
    var box = document.getElementById("fnPool");
    if (!box || !d) return;
    var n = d.n || (d.pool ? d.pool.length : 0);
    var modeName = d.mode === "official" ? "官方池" : "舆情池";
    var extra = "";
    if (d.mode === "official" && d.official_fallback) {
      extra = '<div class="fn-pool-warn">⚠ 官方池尚未抓取（plate_members 为空），已自动回退「舆情池」。'
        + '运行 <code>tools/fetch_plate_members.py</code> 抓取东财全量成员后即可切换为真实官方池。</div>';
    } else if (d.mode === "official") {
      extra = '<div class="fn-pool-ok">✓ 使用东财官方板块全量成员。</div>';
    } else {
      extra = '<div class="fn-pool-warn" style="opacity:.85">舆情池＝豆包视频文本挖名的注意力池（非交易所官方全量），'
        + '切到「官方池」可看交易所全量成分股。</div>';
    }
    box.innerHTML = ''
      + '<div class="fn-pool">'
      +   '<div class="fn-pool-h">' + esc(d.plate) + ' · ' + modeName + '候选池</div>'
      +   '<div class="fn-pool-t">经四方法归一化评分，共 <b>' + n + '</b> 只候选股进入共识结果表。</div>'
      +   extra
      +   renderClean(d)
      + '</div>';
  }

  function renderMethods() {
    var box = document.getElementById("fnMethods");
    if (!box) return;
    var ms = sortedMethods();
    var pool = (FN.score && FN.score.pool) || [];
    // 本层真实命中统计
    function counts(id) {
      var c = { "看涨": 0, "看跌": 0, "中性": 0, "未覆盖": 0 };
      for (var i = 0; i < pool.length; i++) {
        var d = pool[i].methods && pool[i].methods[id];
        if (d && d.dir) c[d.dir] = (c[d.dir] || 0) + 1;
      }
      return c;
    }
    box.innerHTML = ms.map(function (m) {
      var meta = methodMeta(m.id);
      var c = counts(m.id);
      var kpi = '本层判定：看涨 <b>' + c["看涨"] + '</b> · 看跌 <b>' + c["看跌"]
        + '</b> · 中性 <b>' + c["中性"] + '</b> · 未覆盖 <b>' + c["未覆盖"] + '</b>';
      return ''
        + '<div class="fn-mcard' + (m.on ? '' : ' fn-mcard--off') + '" data-mid="' + escAttr(m.id) + '">'
        +   '<div class="fn-mhd"><span class="fn-mname">' + esc(meta.name) + '</span>'
        +     '<span class="fn-engine">' + esc(meta.engine) + '</span></div>'
        +   '<div class="fn-desc">' + esc(meta.desc) + '</div>'
        +   '<div class="fn-ctl">'
        +     '<span class="fn-toggle' + (m.on ? ' on' : '') + '" data-act="toggle">' + (m.on ? '已启用' : '已关闭') + '</span>'
        +     '<span class="fn-order" data-act="up" title="上移">↑</span>'
        +     '<span class="fn-order" data-act="down" title="下移">↓</span>'
        +     '<select class="fn-keep" data-act="keep">'
        +       '<option value="bull"' + (m.keep === "bull" ? ' selected' : '') + '>保留：看涨</option>'
        +       '<option value="bear"' + (m.keep === "bear" ? ' selected' : '') + '>保留：看跌</option>'
        +       '<option value="neutral"' + (m.keep === "neutral" ? ' selected' : '') + '>保留：中性</option>'
        +       '<option value="all"' + (m.keep === "all" ? ' selected' : '') + '>保留：全部</option>'
        +     '</select>'
        +   '</div>'
        +   '<div class="fn-kpi">' + kpi + '</div>'
        + '</div>';
    }).join("");
    Array.prototype.forEach.call(box.querySelectorAll('.fn-mcard'), function (card) {
      var id = card.dataset.mid;
      var m = null;
      for (var i = 0; i < FN.methods.length; i++) if (FN.methods[i].id === id) m = FN.methods[i];
      if (!m) return;
      card.querySelector('[data-act="toggle"]').onclick = function () { m.on = !m.on; renderMethods(); renderResult(); };
      card.querySelector('[data-act="up"]').onclick = function () { moveOrder(m, -1); renderMethods(); renderResult(); };
      card.querySelector('[data-act="down"]').onclick = function () { moveOrder(m, 1); renderMethods(); renderResult(); };
      card.querySelector('[data-act="keep"]').onchange = function (e) { m.keep = e.target.value; renderMethods(); renderResult(); };
    });
  }

  function moveOrder(m, dir) {
    var arr = sortedMethods();
    var idx = arr.indexOf(m);
    var swap = idx + dir;
    if (swap < 0 || swap >= arr.length) return;
    var other = arr[swap];
    var t = m.order; m.order = other.order; other.order = t;
  }

  // 逐层 AND 收缩：按启用层顺序逐层过滤
  function contraction() {
    var enabled = sortedMethods().filter(function (m) { return m.on; });
    var pool = FN.pool || [];
    var byCode = {};
    pool.forEach(function (s) { byCode[s.code] = s; });
    var survivors = pool.map(function (s) { return s.code; });
    var trace = [];
    enabled.forEach(function (m) {
      var before = survivors.length;
      survivors = survivors.filter(function (code) {
        var s = byCode[code];
        var d = s && s.methods && s.methods[m.id];
        return d && methodPass(d.dir, m.keep);
      });
      trace.push({ name: methodMeta(m.id).name, keep: m.keep, before: before, after: survivors.length });
    });
    return { enabled: enabled, survivors: survivors, trace: trace, byCode: byCode };
  }

  function renderResult() {
    var box = document.getElementById("fnResult");
    if (!box) return;
    var pool = FN.pool || [];
    if (!pool.length) { box.innerHTML = ''; return; }
    var enabled = sortedMethods().filter(function (m) { return m.on; });
    var ct = contraction();
    var total = enabled.length;

    // 共识度：被几个启用层共同命中（按各自保留方向通过）
    function consensusOf(s) {
      var hit = 0;
      enabled.forEach(function (m) {
        var d = s.methods && s.methods[m.id];
        if (d && methodPass(d.dir, m.keep)) hit++;
      });
      return hit;
    }
    // 全票通过集合（逐层 AND 收缩幸存）
    var survivorSet = {};
    ct.survivors.forEach(function (c) { survivorSet[c] = true; });

    // 决定展示集合：默认仅全票通过；若为空则展示全部并提示
    var showAll = FN.showAll || ct.survivors.length === 0;
    var rowsSrc = (showAll ? pool : ct.survivors.map(function (c) { return ct.byCode[c]; }))
      .slice().sort(function (a, b) {
        var ca = consensusOf(b), cb = consensusOf(a);
        if (ca !== cb) return ca - cb;
        return (a.code || "").localeCompare(b.code || "");
      });

    var head = '<tr><th>代码</th><th>名称</th>'
      + enabled.map(function (m) { return '<th>' + esc(methodMeta(m.id).name) + '</th>'; }).join("")
      + '<th title="避坑标记：6 条实证指标的当日命中（放量下跌/当日暴跌/缩量下跌/MACD死叉/利空公告/两日累跌）。不含任何博主数据。">避坑</th>'
      + '<th title="解禁提示：未来 60 个交易日内的限售解禁批次。实证：首发原股东解禁且占流通≥3% 的票，解禁前 20 个交易日跑输大盘 3.36%（t=-5.09）；前 40 个交易日仍为超额上涨，故 40 日外只作信息提示不亮灯。">解禁</th>'
      + '<th>共识度</th><th>状态</th></tr>';

    var rows = rowsSrc.map(function (s) {
      var cells = enabled.map(function (m) {
        var d = s.methods && s.methods[m.id];
        return '<td>' + (d ? dirBadge(d.dir) : dirBadge("未覆盖")) + '</td>';
      }).join("");
      var hit = consensusOf(s);
      var pct = total ? Math.round(hit / total * 100) : 0;
      var isFull = !!survivorSet[s.code];
      // 避坑徽标（2026-10-30 替代博主反向警示）：只在中/高档亮灯，低档不显示。
      var av = s.avoid;
      var avCell = '<td>' + ((typeof avoidBadge === "function" && av && av.level)
        ? avoidBadge(av) : '<span class="muted">—</span>') + '</td>';
      // 解禁列（2026-10-30）：high/mid 亮灯，watch 给灰字日期 —— 候选池里也要能看到
      // 「这票 40 天后有大解禁」，否则买进去才发现，等于没提示。
      var lf = s.lift;
      var lfCell = '<td>' + ((typeof liftBadge === "function" && lf && lf.level)
        ? liftBadge(lf, { watch: true }) : '<span class="muted">—</span>') + '</td>';
      var cons = '<div class="fn-cons-bar"><div class="fn-cons-fill" style="width:' + pct + '%"></div>'
        + '<span class="fn-cons-num">' + hit + '/' + total + '</span></div>';
      var status = isFull ? '<span class="ic-badge bull">✓全过</span>' : (hit > 0 ? '<span class="ic-badge neu">部分</span>' : '<span class="ic-badge ncov">未过</span>');
      return '<tr' + (isFull ? ' class="fn-row-full"' : '') + '>'
        + '<td class="fn-code">' + esc(s.code || "") + '</td><td>' + esc(s.name || "") + '</td>'
        + cells + avCell + lfCell + '<td>' + cons + '</td><td>' + status + '</td></tr>';
    }).join("");

    // 逐层收缩轨迹
    var traceHtml = '<div class="fn-trace"><div class="fn-trace-h">逐层 AND 收缩（上一层幸存才进下一层）</div>'
      + '<div class="fn-trace-body">';
    if (total === 0) {
      traceHtml += '<div class="fn-trace-row">未启用任何方法层。</div>';
    } else {
      traceHtml += '<div class="fn-trace-row"><span class="fn-trace-step">入池</span>'
        + '<span class="fn-trace-num">' + pool.length + '</span> 只候选</div>';
      ct.trace.forEach(function (t, i) {
        traceHtml += '<div class="fn-trace-row"><span class="fn-trace-step">第' + (i + 1) + '层 '
          + esc(t.name) + '（保留:' + keepLabel(t.keep) + '）</span>'
          + ' 进 <b>' + t.before + '</b> → 留 <b>' + t.after + '</b></div>';
      });
      traceHtml += '<div class="fn-trace-row fn-trace-final"><span class="fn-trace-step">最终全票通过</span>'
        + ' <b>' + ct.survivors.length + '</b> 只</div>';
    }
    traceHtml += '</div></div>';

    var toggleHtml = '<div class="fn-toggleview">'
      + '<span class="fn-tv' + (showAll ? '' : ' on') + '" data-tv="survive">' + (ct.survivors.length) + ' 只全票通过</span>'
      + '<span class="fn-tv' + (showAll ? ' on' : '') + '" data-tv="all">全部 ' + pool.length + ' 只</span>'
      + '</div>';

    var note = '<div class="fn-note">说明：共识度＝被几个「启用层」按其保留方向共同命中的数量（X/N）；'
      + '「逐层 AND 收缩」为硬过滤——必须逐层通过所有启用层才计入「全票通过」。'
      + '未覆盖＝该方法对个股暂无信号（不计入方向共识）。改某层「保留方向」或开关/排序方法层会实时重算。</div>';

    box.innerHTML = traceHtml + toggleHtml
      + '<table class="fn-table"><thead>' + head + '</thead><tbody>' + rows + '</tbody></table>'
      + note;

    Array.prototype.forEach.call(box.querySelectorAll('.fn-tv'), function (el) {
      el.onclick = function () {
        var tv = el.dataset.tv;
        FN.showAll = (tv === "all");
        Array.prototype.forEach.call(box.querySelectorAll('.fn-tv'), function (x) {
          x.classList.toggle('on', x.dataset.tv === tv);
        });
        renderResult();
      };
    });
  }

  window.DOUYIN.renderFunnel = renderFunnel;
})();
