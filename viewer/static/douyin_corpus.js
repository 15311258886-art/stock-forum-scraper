/* ============================================================================
   豆包整理的抖音博主语料前端模块（2026-10-20 进哥需求）
   ----------------------------------------------------------------------------
   为什么独立成文件而不写进 app.js：
     app.js 已 1.5 万行 / 34 万字符，历史上多次「大段 Edit 静默丢写」；
     本模块是**新增域**（豆包舆情 / 豆包技术方法），与既有渲染零耦合，
     独立文件 + 独立挂载点，回滚只需删一个 <script>。

   两个挂载点（进哥 2026-10-20 拍板）：
     ① #p-sentiment 顶部「豆包舆情」框  —— 情绪板块里独立成框、标来源
     ② #p-tech 新面板「豆包技术方法」  —— 选项 A：新建技术面板（2701 条需后端分页+搜索）

   硬规则（沿用项目口径）：
     · null 一律显示「—」/「日期未知」，绝不写 0；
     · 列表只拿 summary_brief（180 字），点开才请求全文，避免 2701 条 × 数千字把浏览器拖死；
     · 后端分页（limit/offset），不做前端全量。
   ========================================================================== */
(function () {
  "use strict";

  var API = "/api/douyin";
  var LBL = { sentiment: "豆包舆情", tech: "豆包技术方法" };
  var NOTE = {
    sentiment: "豆包整理的博主情绪/舆情观点（取自抖音视频，由豆包逐条转写翻译）。与「舆论情绪」的多空计数是两个独立来源，故单独成框、不混算。",
    tech: "豆包整理的技术方法与交易体系。摘要由豆包对视频做语音转写+翻译后提炼，非逐字原文，用于找方法、不当作原文引用。"
  };

  var State = {};

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function S(domain, opts) {
    if (!State[domain]) {
      State[domain] = {
        f: null,                 // facets
        q: "", blogger: "", category: "", sort: "date", topic: "", stance: "",
        offset: 0, limit: (opts && opts.limit) || 20,
        rows: [], total: 0, loading: false, err: "",
        open: {}                 // video_id -> 全文（避免重复请求）
      };
    }
    if (opts && opts.limit) State[domain].limit = opts.limit;
    return State[domain];
  }

  function fmtSrc(upd) { return upd ? "数据截至 " + upd : "数据截至 —"; }
  function fmtDate(d) { return d ? d : "日期未知"; }

  /* ---------- 骨架 ---------- */
  function shell(domain, opts) {
    var s = S(domain, opts);
    var compact = !!(opts && opts.compact);
    return ''
      + '<div class="dy-box' + (compact ? " dy-box--compact" : "") + '" data-dy="' + domain + '">'
      + '  <div class="dy-hd">'
      + '    <span class="dy-src" title="数据来源标识：本框内容由豆包整理自抖音博主视频，与页面其它来源区分">豆包整理</span>'
      + '    <span class="dy-title">' + esc(LBL[domain]) + '</span>'
      + '    <span class="dy-meta" data-dy-meta>读取中…</span>'
      + '    <span class="dy-sp"></span>'
      + '    <button class="dy-btn dy-btn-ghost" data-dy-collapse="1" title="收起/展开本框内容">收起</button>'
      + '  </div>'
      + '  <div class="dy-note">' + esc(NOTE[domain]) + '</div>'
      + '  <div class="dy-body">'
      + '    <div class="dy-filters">'
      + '      <select class="dy-sel" data-dy-f="blogger"><option value="">全部博主</option></select>'
      + '      <select class="dy-sel" data-dy-f="category"><option value="">全部分类</option></select>'
      + '      <input class="dy-search" type="search" placeholder="搜标题 / 摘要 / 博主关键词…" data-dy-q />'
      + '      <select class="dy-sel" data-dy-f="sort">'
      + '        <option value="date">按日期（新→旧）</option>'
      + '        <option value="old">按日期（旧→新）</option>'
      + '        <option value="author">按博主</option>'
      + '      </select>'
      + '    </div>'
      + '    <div class="dy-list" data-dy-list><div class="dy-empty">读取中…</div></div>'
      + '    <div class="dy-foot">'
      + '      <span class="dy-count" data-dy-count></span>'
      + '      <button class="dy-btn" data-dy-more="1">加载更多</button>'
      + '    </div>'
      + '  </div>'
      + '</div>';
  }

  /* ---------- 事件绑定（委托，避免 2701 条逐个绑 / 内联转义出错） ---------- */
  function bind(domain, box, opts) {
    if (box && box.__dyBound) return;   // 幂等：双挂载不重复绑定事件
    if (box) box.__dyBound = true;
    var s = S(domain, opts);
    var qTimer = null;

    box.addEventListener("change", function (ev) {
      var t = ev.target;
      if (t.hasAttribute && t.hasAttribute("data-dy-f")) {
        var k = t.getAttribute("data-dy-f");
        if (k === "blogger" || k === "category" || k === "sort") {
          s[k] = t.value || (k === "sort" ? "date" : "");
          reload(domain, box, opts);
        }
      }
    });

    box.addEventListener("input", function (ev) {
      var t = ev.target;
      if (t.hasAttribute && t.hasAttribute("data-dy-q")) {
        var v = t.value || "";
        clearTimeout(qTimer);
        qTimer = setTimeout(function () {
          s.q = v.trim();
          reload(domain, box, opts);
        }, 320);
      }
    });

    box.addEventListener("click", function (ev) {
      var t = ev.target.closest ? ev.target.closest("[data-dy-more],[data-dy-collapse],[data-dy-vid],[data-dy-all]") : null;
      if (!t) return;
      if (t.hasAttribute("data-dy-more")) {
        s.offset = s.rows.length;
        loadList(domain, box, opts, false);
        return;
      }
      if (t.hasAttribute("data-dy-collapse")) {
        var body = box.querySelector(".dy-body");
        var on = body.style.display === "none";
        body.style.display = on ? "" : "none";
        t.textContent = on ? "收起" : "展开";
        return;
      }
      if (t.hasAttribute("data-dy-vid")) {
        toggleDetail(domain, box, opts, t.getAttribute("data-dy-vid"));
        return;
      }
      if (t.hasAttribute("data-dy-all")) {
        s.blogger = "";
        var sel = box.querySelector('[data-dy-f="blogger"]');
        if (sel) sel.value = "";
        reload(domain, box, opts);
      }
    });
  }

  function reload(domain, box, opts) {
    var s = S(domain, opts);
    s.offset = 0;
    s.rows = [];
    s.open = {};
    var list = box.querySelector("[data-dy-list]");
    if (list) list.innerHTML = '<div class="dy-empty">筛选中…</div>';
    loadList(domain, box, opts, true);
  }

  /* ---------- facets ---------- */
  function loadFacets(domain, box, opts) {
    fetch(API + "/facets?domain=" + encodeURIComponent(domain))
      .then(function (r) { return r.json(); })
      .then(function (f) {
        var s = S(domain, opts);
        s.f = f;
        if (!f || f.ok === false) {
          var m = box.querySelector("[data-dy-meta]");
          if (m) m.textContent = (f && f.note) || "数据未就绪";
          return;
        }
        var meta = box.querySelector("[data-dy-meta]");
        if (meta) {
          var dr = f.date_range || [];
          var span = (dr[0] && dr[1]) ? (" · " + dr[0] + " ~ " + dr[1]) : "";
          var nd = f.no_date ? (" · " + f.no_date + " 条日期未知") : "";
          meta.textContent = f.n_blogger + " 位博主 · " + f.n_video + " 条视频" + span + nd + " · " + fmtSrc(f.src_updated);
        }
        var bs = box.querySelector('[data-dy-f="blogger"]');
        if (bs) {
          bs.innerHTML = '<option value="">全部博主（' + (f.n_blogger || 0) + '）</option>'
            + (f.bloggers || []).filter(function (b) { return b.n > 0; })
              .map(function (b) {
                return '<option value="' + esc(b.name) + '">' + esc(b.name) + "（" + b.n + "）</option>";
              }).join("");
        }
        var cs = box.querySelector('[data-dy-f="category"]');
        if (cs) {
          var cats = (f.categories || []).filter(function (c) { return c.name !== "未分类"; });
          cs.innerHTML = '<option value="">全部分类（' + (cats.length || 0) + '）</option>'
            + cats.map(function (c) {
                return '<option value="' + esc(c.name) + '">' + esc(c.name) + "（" + c.n + "）</option>";
              }).join("")
            + '<option value="__none__">未分类</option>';
        }
      })
      .catch(function (e) {
        var m = box.querySelector("[data-dy-meta]");
        if (m) m.textContent = "筛选项读取失败：" + (e && e.message ? e.message : e);
      });
  }

  /* ---------- 列表 ---------- */
  function loadList(domain, box, opts, reset) {
    var s = S(domain, opts);
    if (s.loading) return;
    s.loading = true;
    var more = box.querySelector("[data-dy-more]");
    if (more) { more.disabled = true; more.textContent = "加载中…"; }

    var qs = API + "/list?domain=" + encodeURIComponent(domain)
      + "&limit=" + s.limit + "&offset=" + (reset ? 0 : s.offset) + "&sort=" + encodeURIComponent(s.sort);
    if (s.q) qs += "&q=" + encodeURIComponent(s.q);
    if (s.blogger) qs += "&blogger=" + encodeURIComponent(s.blogger);
    if (s.category) qs += "&category=" + encodeURIComponent(s.category);
    if (s.topic) qs += "&topic=" + encodeURIComponent(s.topic);
    if (s.stance) qs += "&stance=" + encodeURIComponent(s.stance);

    fetch(qs)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        s.loading = false;
        if (more) { more.disabled = false; more.textContent = "加载更多"; }
        if (!d || d.ok === false) {
          var list = box.querySelector("[data-dy-list]");
          if (list) list.innerHTML = '<div class="dy-empty">' + esc((d && d.note) || "数据未就绪") + "</div>";
          return;
        }
        s.total = d.total || 0;
        s.rows = reset ? (d.rows || []) : s.rows.concat(d.rows || []);
        s.hasMore = !!d.has_more;
        paintList(domain, box, opts);
      })
      .catch(function (e) {
        s.loading = false;
        if (more) { more.disabled = false; more.textContent = "加载更多"; }
        var list = box.querySelector("[data-dy-list]");
        if (list) list.innerHTML = '<div class="dy-empty">加载失败：' + esc(e && e.message ? e.message : e) + "</div>";
      });
  }

  function paintList(domain, box, opts) {
    var s = S(domain, opts);
    var list = box.querySelector("[data-dy-list]");
    if (!list) return;
    if (!s.rows.length) {
      list.innerHTML = '<div class="dy-empty">' + (s.q || s.blogger || s.category
        ? "没有匹配的视频（换关键词或清空筛选试试）"
        : "暂无豆包语料") + "</div>";
    } else {
      list.innerHTML = s.rows.map(function (r) {
        var open = s.open[r.video_id];
        var title = r.title || (r.summary_brief ? r.summary_brief.slice(0, 40) + "…" : "（无标题）");
        return ''
          + '<article class="dy-item">'
          + '  <div class="dy-item-hd">'
          + '    <span class="dy-date">' + esc(fmtDate(r.publish_date)) + '</span>'
          + '    <span class="dy-author">@' + esc(r.author) + '</span>'
          + (r.category ? '<span class="dy-cat">' + esc(r.category) + '</span>' : '')
          + (r.source_of_summary === "asr" ? '<span class="dy-asr" title="摘要来自视频语音转写后提炼">ASR</span>' : '')
          + '    <span class="dy-sp"></span>'
          + (r.video_url ? '<a class="dy-link" href="' + esc(r.video_url) + '" target="_blank" rel="noopener">原视频 ↗</a>' : '')
          + '  </div>'
          + '  <div class="dy-item-title">'
          + (r.has_summary
              ? '<button class="dy-tbtn" data-dy-vid="' + esc(r.video_id) + '">' + esc(title) + '</button>'
              : '<span>' + esc(title) + '</span>')
          + '  </div>'
          + (r.summary_brief
              ? '<div class="dy-brief' + (open ? " dy-brief--dim" : "") + '">' + esc(r.summary_brief) + '</div>'
              : '<div class="dy-brief dy-dim">—（本条无摘要）</div>')
          + (open ? '<div class="dy-full">' + esc(open) + '</div>' : '')
          + '</article>';
      }).join("");
    }
    var cnt = box.querySelector("[data-dy-count]");
    if (cnt) {
      cnt.textContent = "共 " + s.total + " 条" + (s.rows.length ? "，已显示 " + s.rows.length + " 条" : "")
        + (s.q ? " · 关键词「" + s.q + "」" : "");
    }
    var more = box.querySelector("[data-dy-more]");
    if (more) more.style.display = s.hasMore ? "" : "none";
  }

  /* ---------- 单条全文 ---------- */
  function toggleDetail(domain, box, opts, vid) {
    var s = S(domain, opts);
    if (s.open[vid]) { delete s.open[vid]; paintList(domain, box, opts); return; }
    fetch(API + "/detail?video_id=" + encodeURIComponent(vid))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        s.open[vid] = (d && d.ok && d.row && d.row.summary) ? d.row.summary : "（该条无完整摘要）";
        paintList(domain, box, opts);
      })
      .catch(function (e) {
        s.open[vid] = "摘要读取失败：" + (e && e.message ? e.message : e);
        paintList(domain, box, opts);
      });
  }


  /* ---------- 技术域：主题轨道筛选（整合层①） ---------- */
  function renderTechTracks(sel) {
    var root = document.querySelector(sel);
    if (!root) return;
    var box = root.querySelector("[data-dy]");
    if (!box) return;
    if (box.querySelector(".dy-tracks")) return;   // 已建，避免双挂载重复插入轨道条
    var rail = document.createElement("div");
    rail.className = "dy-tracks";
    rail.innerHTML = '<span class="dy-tracks-lbl">主题轨道：</span>'
      + '<button class="dy-chip dy-chip--on" data-track="">全部</button>'
      + '<span class="dy-tracks-loading">读取中…</span>';
    box.insertBefore(rail, box.querySelector(".dy-note"));
    fetch(API + "/topics").then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.ok) { rail.querySelector(".dy-tracks-loading").textContent = "轨道读取失败"; return; }
      var chips = d.tracks.map(function (t) {
        return '<button class="dy-chip" data-track="' + esc(t.track) + '" title="' + esc(t.desc) + '">'
          + esc(t.track) + "（" + t.n + "）</button>";
      }).join("");
      rail.innerHTML = '<span class="dy-tracks-lbl">主题轨道：</span>' + chips;
      rail.addEventListener("click", function (ev) {
        var b = ev.target.closest ? ev.target.closest("[data-track]") : null;
        if (!b) return;
        State.tech.topic = b.getAttribute("data-track") || "";
        rail.querySelectorAll(".dy-chip").forEach(function (c) { c.classList.remove("dy-chip--on"); });
        b.classList.add("dy-chip--on");
        reload("tech", box, {});
      });
    }).catch(function () { rail.querySelector(".dy-tracks-loading").textContent = "轨道读取失败"; });
  }

  /* ---------- 情绪域：观点类型筛选（整合层②） ---------- */
  function renderStanceChips(sel) {
    var root = document.querySelector(sel);
    if (!root) return;
    var box = root.querySelector("[data-dy]");
    if (!box) return;
    if (box.querySelector(".dy-stance")) return;   // 已建，避免双挂载重复插入观点类型条
    var bar = document.createElement("div");
    bar.className = "dy-stance";
    bar.innerHTML = '<span class="dy-stance-lbl">观点类型：</span>'
      + '<button class="dy-chip dy-chip--on" data-stance="">全部</button>'
      + '<span class="dy-stance-loading">读取中…</span>';
    box.insertBefore(bar, box.querySelector(".dy-body"));
    fetch(API + "/stances").then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.ok) { bar.querySelector(".dy-stance-loading").textContent = "读取失败"; return; }
      var chips = d.stances.map(function (s) {
        return '<button class="dy-chip" data-stance="' + esc(s.stance) + '">' + esc(s.stance) + "（" + s.n + "）</button>";
      }).join("");
      bar.innerHTML = '<span class="dy-stance-lbl">观点类型：</span>' + chips;
      bar.addEventListener("click", function (ev) {
        var b = ev.target.closest ? ev.target.closest("[data-stance]") : null;
        if (!b) return;
        State.sentiment.stance = b.getAttribute("data-stance") || "";
        bar.querySelectorAll(".dy-chip").forEach(function (c) { c.classList.remove("dy-chip--on"); });
        b.classList.add("dy-chip--on");
        reload("sentiment", box, {});
      });
    }).catch(function () { bar.querySelector(".dy-stance-loading").textContent = "读取失败"; });
  }

  function topDist(dist) {
    var ks = Object.keys(dist || {});
    if (!ks.length) return '<span class="ib-dim">—</span>';
    return ks.slice(0, 3).map(function (k) { return '<span class="ib-d">' + esc(k) + " " + dist[k] + "</span>"; }).join("");
  }

  /* ---------- 整合面板：板块多维联动 + 博主分域群像（化学反应核心） ---------- */
  function renderIntegration(sel) {
  var root = document.querySelector(sel || "#p-integrate");
  if (!root) return;
  var HOT = ["AI", "半导体", "机器人", "黄金", "券商", "银行", "光伏", "消费", "地产", "算力", "锂电", "白酒", "军工", "软件", "汽车", "芯片", "游戏"];
  root.innerHTML = ''
    + '<div class="integ-wrap">'
    + '  <div class="integ-hd"><span class="dy-src">整合图谱</span>'
    + '    <span class="dy-title">板块结论 · 落到个股</span><span class="dy-sp"></span>'
    + '    <span class="dy-meta">豆包语料 → 六源信号 / 合成卡 → 看涨·看跌（博主共振已下线）</span></div>'
    + '  <div class="integ-pick">'
    + '    <input class="dy-search" id="integ-plate-input" placeholder="输入板块，如 半导体 / AI / 机器人…" />'
    + '    <button class="dy-btn dy-btn-primary" id="integ-plate-go">出结论</button></div>'
    + '  <div class="integ-hot" id="integ-hot"></div>'
    + '  <div class="integ-out" id="integ-out"><div class="dy-empty">选一个板块，直接看「这个板块该关注谁、谁在看涨、谁有风险」。</div></div>'
    + '</div>';
  var hotEl = root.querySelector("#integ-hot");
  hotEl.innerHTML = HOT.map(function (p) {
    return '<button class="dy-chip" data-plate="' + esc(p) + '">' + esc(p) + "</button>";
  }).join("");

  function dirCls(dd) { return dd === "看涨" ? "bull" : (dd === "看跌" ? "bear" : "neu"); }

  function rowHtml(x) {
    var c = dirCls(x.dir);
    var rs = (x.reasons || []).slice(0, 3).map(function (r) {
      return '<span class="ic-rs">' + esc(r) + "</span>";
    }).join("");
    var q = (x.samples && x.samples[0])
      ? '<div class="ic-q">「' + esc(String(x.samples[0].text || "").slice(0, 56)) + '…」'
        + (x.samples[0].author ? "— " + esc(x.samples[0].author) : "") + "</div>"
      : "";
    return '<div class="ic-row">'
      + '<div class="ic-row-h"><b>' + esc(x.name) + '</b>'
      + '<span class="ic-code">' + esc(x.code) + '</span>'
      + '<span class="ic-badge ' + c + '">' + esc(x.dir) + "·" + esc(x.strength) + '</span>'
      + '<span class="ic-score">' + x.score + ' 分</span></div>'
      + '<div class="ic-rs-wrap">' + rs + "</div>" + q + "</div>";
  }

  function colHtml(head, cls, list) {
    var body = (list && list.length) ? list.map(rowHtml).join("") : '<div class="dy-empty">无</div>';
    return '<div class="ic-col ' + cls + '"><div class="ic-col-h">' + head + "</div>" + body + "</div>";
  }

  function goPlate(p) {
    if (!p) return;
    var out = root.querySelector("#integ-out");
    out.innerHTML = '<div class="dy-empty">聚合推理中…</div>';
    fetch(API + "/plate?plate=" + encodeURIComponent(p)).then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.ok) { out.innerHTML = '<div class="dy-empty">' + esc((d && (d.note || d.error)) || "无数据") + "</div>"; return; }
      if (!d.stocks || !d.stocks.length) {
        out.innerHTML = '<div class="dy-empty">' + esc(d.note || ("「" + d.plate + "」暂未抽出点名个股")) + "</div>";
        return;
      }
      var h = '<div class="ic-sum"><div class="ic-sum-h">' + esc(d.plate) + ' · 结论</div>'
        + '<div class="ic-sum-t">' + esc(d.summary || "") + "</div>"
        + '<div class="ic-kpi">'
        + '<span class="kpi bull">看涨 <b>' + d.n_bull + "</b></span>"
        + '<span class="kpi bear">看跌 <b>' + d.n_bear + "</b></span>"
        + '<span class="kpi neu">中性 <b>' + d.n_neutral + "</b></span>"
        + '<span class="kpi all">共 <b>' + d.n_stocks + "</b> 只</span>"
        + "</div></div>";
      h += '<div class="ic-split">'
        + colHtml("看涨 · 值得关注（" + d.n_bull + "）", "bull", d.bulls || [])
        + colHtml("看跌 · 风险回避（" + d.n_bear + "）", "bear", d.bears || [])
        + "</div>";
      var tb = (d.tech && d.tech.tracks) || [];
      var sb = (d.sentiment && d.sentiment.stances) || [];
      var bh = '<details class="ic-basis"><summary>凭什么这么判断（中性个股 / 方法轨道 / 舆情观点 / 涉及博主）</summary>';
      if (d.neuts && d.neuts.length) {
        bh += '<div class="ic-basis-neu"><div class="ic-basis-h">中性 · 观察（' + d.n_neutral + '）</div>'
          + '<div class="ic-neu-list">' + d.neuts.map(function (x) {
            return '<span class="ic-neu">' + esc(x.name) + "(" + esc(x.code) + ") " + esc(x.dir) + "·" + esc(x.strength) + "</span>";
          }).join("") + "</div></div>";
      }
      if (tb.length || sb.length) {
        bh += '<div class="ic-basis-cols">';
        if (tb.length) {
          bh += '<div><div class="ic-basis-h">方法轨道</div>' + tb.map(function (x) {
            return '<div class="ic-kv"><span>' + esc(x[0]) + '</span><b>' + x[1] + "</b></div>";
          }).join("") + "</div>";
        }
        if (sb.length) {
          bh += '<div><div class="ic-basis-h">舆情观点</div>' + sb.map(function (x) {
            return '<div class="ic-kv"><span>' + esc(x[0]) + '</span><b>' + x[1] + "</b></div>";
          }).join("") + "</div>";
        }
        bh += "</div>";
      }
      if (d.bloggers && d.bloggers.length) {
        bh += '<div class="ic-basis-blog">涉及博主：' + d.bloggers.slice(0, 24).map(function (b) { return esc(b); }).join("、") + "</div>";
      }
      bh += "</details>";
      h += bh;
      out.innerHTML = h;
    }).catch(function (e) {
      out.innerHTML = '<div class="dy-empty">聚合失败：' + esc((e && e.message) || e) + "</div>";
    });
  }

  root.querySelector("#integ-plate-go").addEventListener("click", function () {
    goPlate(root.querySelector("#integ-plate-input").value.trim());
  });
  hotEl.addEventListener("click", function (ev) {
    var t = ev.target.closest ? ev.target.closest("[data-plate]") : null;
    if (t) goPlate(t.getAttribute("data-plate"));
  });
}


/* ---------- 共振面板：豆包语料 × 主力合成层（孤岛打通的化学反应） ---------- */
  function renderResonance(sel) {
    var root = document.querySelector(sel || '#p-dy_reso');
    if (!root) return;
    root.innerHTML = ''
      + '<div class="integ-wrap">'
      + '  <div class="integ-hd"><span class="dy-src">豆包×主力共振</span>'
      + '    <span class="dy-title">语料孤岛 → 合成层打通</span><span class="dy-sp"></span>'
      + '    <span class="dy-meta" id="reso-meta">读取中…</span></div>'
      + '  <div class="integ-sec"><h4>① 共振标的（按可信度排序）</h4>'
      + '    <div class="reso-list" id="reso-list"><div class="dy-empty">读取中…</div></div></div>'
      + '  <div class="integ-sec"><h4>② 豆包盲区（豆包在讲、主力合成层尚未覆盖 · 自进化待学习）</h4>'
      + '    <div class="reso-gap" id="reso-gap"><div class="dy-empty">读取中…</div></div></div>'
      + '</div>';
    fetch(API + '/resonance?limit=40').then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.ok) { root.querySelector('#reso-list').innerHTML = '<div class="dy-empty">读取失败</div>'; return; }
      root.querySelector('#reso-meta').textContent = '豆包点名 ' + d.distinct_stocks + ' 只 → 与主力合成层共振 ' + d.resonance_n + ' 只';
      var rows = d.rows || [];
      root.querySelector('#reso-list').innerHTML = rows.map(function (x) {
        var vcls = x.verdict === '强共振' ? 'v-strong' : (x.verdict === '共振' ? 'v-mid' : 'v-alone');
        var smp = (x.samples && x.samples[0]) ? ('「' + (x.samples[0].text || '').slice(0, 44) + '…」— ' + (x.samples[0].author || '')) : '';
        var sd = x.sig_dir ? Object.keys(x.sig_dir).map(function (k) { return k + '×' + x.sig_dir[k]; }).join(' ') : '';
        return '<div class="reso-row" style="display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;padding:8px 10px;border-bottom:1px solid #2a2f3a;">'
          + '<b style="color:#e6edf3;min-width:64px;">' + esc(x.code) + '</b>'
          + '<span style="min-width:78px;color:#9fb3c8;">' + esc(x.name || '') + '</span>'
          + '<span class="' + vcls + '" style="padding:1px 8px;border-radius:10px;font-size:12px;">' + esc(x.verdict) + '</span>'
          + '<span style="color:#f0b400;font-size:12px;">分' + x.score + '</span>'
          + '<span style="color:#9fb3c8;font-size:12px;">合成' + esc(x.card_tier) + '/' + esc(String(x.card_score)) + '</span>'
          + '<span style="color:#9fb3c8;font-size:12px;">豆包' + x.douyin_n + '·共振博主' + (x.overlap_n || 0) + '·信号' + (x.sig_n || 0) + (sd ? '(' + sd + ')' : '') + '</span>'
          + '<span style="color:#6b7785;font-size:12px;">' + esc((x.domains || []).join('/')) + '</span>'
          + '<span style="flex-basis:100%;color:#c9d6e3;font-size:12px;">' + esc(smp) + '</span>'
          + '</div>';
      }).join('') || '<div class="dy-empty">暂无共振</div>';
      var gaps = (d.gaps || []).slice(0, 10).map(function (g) {
        return '<span style="display:inline-block;margin:3px;padding:3px 8px;border:1px solid #3a4150;border-radius:10px;color:#c9d6e3;font-size:12px;">'
          + esc(g.name || g.code) + '·' + esc(g.code) + '（豆包' + g.douyin_n + '）</span>';
      }).join('');
      root.querySelector('#reso-gap').innerHTML = gaps || '<div class="dy-empty">无盲区</div>';
    }).catch(function (e) {
      root.querySelector('#reso-list').innerHTML = '<div class="dy-empty">读取失败</div>';
    });
  }

  /* ---------- 对外挂载 ---------- */
  function mount(domain, sel, opts) {
    var box = document.querySelector(sel);
    if (!box) return;
    var existing = box.querySelector("[data-dy]");
    if (!existing) box.innerHTML = shell(domain, opts);   // 已挂载则不重建（避免丢弃在途 fetch 的节点）
    var root = box.querySelector("[data-dy]");
    if (!root) return;
    bind(domain, root, opts);
    loadFacets(domain, root, opts);
    // 幂等：已挂载且正在拉取/已有数据时不重复发起首屏请求（双挂载竞态防护）
    if (!existing) {
      loadList(domain, root, opts, true);
    } else {
      var st = State[domain];
      if (!st || (!st.rows.length && !st.loading)) loadList(domain, root, opts, true);
    }
  }

  window.DOUYIN = {
    /* ① 情绪板块顶部「豆包舆情」框（compact：默认只露出 20 条，够看当下舆情） */
    renderSentimentBox: function (sel) {
      mount("sentiment", sel || "#dy-sent-mount", { compact: true, limit: 20 });
      renderStanceChips(sel || "#dy-sent-mount");
    },
    /* ② 新建「技术方法」面板（full：2701 条，后端分页 + 搜索） */
    renderTech: function () {
      mount("tech", "#p-tech", { compact: false, limit: 30 });
      renderTechTracks("#p-tech");
    },
    /* 供调试/其它面板复用 */
    mount: mount,
    renderIntegration: renderIntegration,
    renderResonance: renderResonance,
    state: State
  };
})();
