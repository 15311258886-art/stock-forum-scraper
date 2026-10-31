/* ══════════════════════════════════════════════════════════════════════════
   公告实证（2026-10-31 进哥需求：「图表或更清晰的元素少了一点」）

   这个面板回答的问题只有一句：
     **公告里那些"利好 / 利空"标签，到底哪些是真的、哪些是我们自己脑补的？**

   为什么要有它（而不是再加一个"荐股面板"）：
     进哥的原话是「博主/龙虎榜/抖音输出的观点挺有说服力，但胜率连 50% 都上不了，
     很多还是负面指标」。要形成自己的方法论，第一步不是找更好的荐股源，而是先量出
     **哪些事件在 A 股是真的有方向性、哪些只是看起来有**。这份东西就是那把尺子。

   数据来源：`research/out/announcement_event_study.md`（115.8 万条公告 → 60 日冷却去重 →
     2016-01 ~ 2026-10 · 5575 只 · 全市场等权基准 · 按交易日聚合 · 判据 |t|≥3.0）。
     前端读的是 `static/ann_event_data.js`（由 research/gen_ann_event_js.py 从 md 生成）。
     **纯静态、零请求、零后端改动** —— 改完刷新页面就生效，不需要重启服务。

   产品口径（改之前先读）：
     · 这是**统计描述**，不是买卖建议，也不给目标价、不给"推荐关注"；
     · 配色走 A 股惯例：正超额=红(--bull)、负超额=绿(--bear)；
     · 不显著的条目**刻意画淡**（45% 不透明度）。不许为了"图好看"把不显著的画得和显著一样，
       那正好是本面板要拆穿的那种毛病。
   ══════════════════════════════════════════════════════════════════════════ */
(function (global) {
  "use strict";

  var HE = window.VIZ.esc;
  var P = window.VIZ.pct;
  var N = window.VIZ.num;

  var TONE_LABEL = { bad: "利空标签", good: "利好标签", neutral: "中性" };

  function rowsOf(h) {
    var d = global.ANN_EV;
    if (!d || !d.rows) return [];
    return d.rows.filter(function (r) { return r.h === h; });
  }

  /** 取某一 (类别, 持有期) 的一行 */
  function pick(cat, h) {
    var rs = rowsOf(h);
    for (var i = 0; i < rs.length; i++) { if (rs[i].cat === cat) return rs[i]; }
    return null;
  }

  /* ── 渲染主入口 ───────────────────────────────────────────────────── */
  function render() {
    var box = document.getElementById("p-ann_event");
    if (!box) return;
    var d = global.ANN_EV;
    if (!d || !d.rows || !d.rows.length) {
      box.innerHTML = '<div class="aenv"><div class="aenv-head"><div class="aenv-title">公告实证</div></div>'
        + '<div class="empty">数据文件 static/ann_event_data.js 未生成。'
        + '请运行 <code>.venv/Scripts/python.exe research/gen_ann_event_js.py</code>。</div></div>';
      return;
    }
    var m = d.meta;

    // ── 派生统计（全部从原始行算，不手抄数字） ──
    var t5 = rowsOf(5).slice().sort(function (a, b) { return a.ex - b.ex; });
    var t20 = rowsOf(20);
    var sigT5 = t5.filter(function (r) { return r.sig; });
    var badSig = sigT5.filter(function (r) { return r.tone === "bad" && r.ex < 0; });
    var goodSig = sigT5.filter(function (r) { return r.tone === "good" && r.ex > 0; });
    // 反例：贴利空标签、实测却为正且显著
    var trap = t5.filter(function (r) { return r.tone === "bad" && r.ex > 0 && r.sig; });
    // 反例：贴利好标签、实测为负（不论显不显著）
    var goodNeg = t5.filter(function (r) { return r.tone === "good" && r.ex < 0; });
    // 更狠的反例：贴利好标签，实测**显著**为负
    var goodNegSig = goodNeg.filter(function (r) { return r.sig; });
    var goodAll = t5.filter(function (r) { return r.tone === "good"; });

    var worst = t5[0];
    var best = t5[t5.length - 1];
    // 8 个显著为负的类别做三期对照
    var decayCats = t5.filter(function (r) { return r.sig && r.ex < 0; }).map(function (r) { return r.cat; });

    var h = [];
    h.push('<div class="aenv">');

    /* ═══ ① 头 ═══ */
    h.push('<div class="aenv-head">'
      + '<div class="aenv-title">公告实证<span class="aenv-sub">标签 vs 实际 —— 用 115.8 万条公告验一遍</span></div>'
      + '<div class="aenv-chips">'
      + chip("股票池 " + N(m.stocks, 0) + " 只")
      + chip("交易日 " + N(m.trade_days, 0))
      + chip("检验 " + m.tests + " 次")
      + chip("显著 " + m.sig_n + " 次", "hot")
      + chip("判据 |t|≥" + N(m.criterion, 1), "rule")
      + '</div></div>');

    /* ═══ ② 一句话结论 ═══ */
    h.push('<div class="aenv-verdict">'
      + '<div class="aenv-v-tag">一句话结论</div>'
      + '<div class="aenv-v-text">被贴上<b>利空</b>标签的事件，实测大多是<b>真的跌</b>（'
      + badSig.length + ' 类在 T+5 显著为负）；被贴上<b>利好</b>标签的事件，实测'
      + (goodSig.length ? '只有 ' + goodSig.length + ' 类显著为正' : '<b>显著为正的一类都没有</b>')
      + ' —— 也就是说：<b>坏消息能当信号用，好消息基本是噪音。</b></div>'
      + '<div class="aenv-v-note">最狠的一条是「' + HE(worst.cat) + '」：T+5 平均超额 '
      + P(worst.ex) + '（t=' + N(worst.t) + '，n=' + N(worst.n, 0) + '）。'
      + '反过来，最大牌的利好「回购增持」39,216 个样本，T+5 只有 ' + P(pick("回购增持", 5).ex)
      + '（t=' + N(pick("回购增持", 5).t) + '）—— 等于零。'
      + (goodNegSig.length
        ? '更反直觉的是：被标为「利好」的 ' + goodAll.length + ' 类里，「' + HE(goodNegSig[0].cat)
          + '」实测 T+5 反而<b>显著为负</b>（' + P(goodNegSig[0].ex) + '，t=' + N(goodNegSig[0].t)
          + '）—— 说明<b>「利好公告」这个标签本身不提供方向</b>，得看是哪一类。'
        : "")
      + '</div>'
      + '</div>');

    /* ═══ ③ 主力图：T+5 全类别发散条 ═══ */
    h.push(sect("图 1 · 谁真的在动：20 类公告的 T+5 平均超额",
      "按数值从小到大排。淡色带 = 95% 置信区间；左侧短竖线 = 通过 |t|≥3.0 判据；"
      + "整体发灰的条目 = 不显著（统计上和不发生没区别），不许当成结论用。",
      VIZ.legend([
        { color: "var(--bear,#1FB873)", text: "负超额（跌）" },
        { color: "var(--bull,#F2544C)", text: "正超额（涨）" },
        { color: "var(--muted,#A6ABB5)", op: 0.26, text: "95% CI" },
      ])
      + VIZ.diverging({
        rows: t5.map(function (r) {
          return { label: r.cat, value: r.ex, lo: r.lo, hi: r.hi, t: r.t, sig: r.sig };
        }),
        axisHint: "T+5 = 公告后次一交易日买入、持 5 个交易日；超额 = 个股收益 − 同日全市场等权均值；统计量按交易日聚合（不按事件，避免重叠样本虚增 t）。",
        aria: "20 类公告 T+5 平均超额发散条形图",
      })));

    /* ═══ ④ 散点：均值 vs 胜率 ═══ */
    var trapRow = pick("风险提示", 20);
    h.push(sect("图 2 · 均值好看不代表能赚：超额 × 日胜率",
      "横轴是「按交易日算，超额为正的天数占比」，纵轴是平均超额。"
      + "只有落在<b>右上角</b>（又正又过半）才是真的有效。"
      + "落在<b>左上角</b>的 = 平均值靠少数几天的大涨拉起来，绝大多数日子在跌 —— 这种钱赚不到。",
      VIZ.scatter({
        points: t5.map(function (r) {
          return { x: r.win, y: r.ex, label: r.cat + " " + P(r.ex), sig: r.sig, tone: r.tone };
        }),
        xLabel: "日胜率（%）",
        yLabel: "T+5 平均超额（%）",
        xRef: 50,
        yRef: 0,
        aria: "公告类别 T+5 超额与日胜率散点图",
      })
      + (trapRow ? '<div class="aenv-callout warn">'
        + '<b>这张图里最该记住的一个点：「风险提示」</b>'
        + '<div>T+20 平均超额 <b>' + P(trapRow.ex) + '</b>（t=' + N(trapRow.t) + '）看着很猛，'
        + '但它的日胜率只有 <b>' + N(trapRow.win, 1) + '%</b> —— 不到一半。'
        + '意思是：多数时候它在跌，全靠少数几只（大概率是 ST / 小市值在几波反弹里的极端值）把均值拽上去。'
        + '<b>这种「平均值」是拿不到的</b>，用它做信号等于赌那几只票。</div>'
        + '<div class="aenv-callout-fix">→ 处理方式：这一条暂时<b>不进任何规则</b>，'
        + '等按市值 / ST 状态分层之后再判断。宁可少一条规则，不要多一条假规则。</div></div>' : "")));

    /* ═══ ⑤ 分期对照：显著利空是否越拿越亏 ═══ */
    if (decayCats.length) {
      var groups = decayCats.map(function (cat) {
        return {
          label: cat,
          values: [1, 5, 20].map(function (hh) {
            var r = pick(cat, hh);
            return r ? { k: "T+" + hh, v: r.ex, sig: r.sig } : null;
          }).filter(Boolean),
        };
      });
      h.push(sect("图 3 · 利空是「一天的事」还是「越拿越亏」",
        "同一批类别分三个持有期并排。条越长 = 亏得越多。"
        + "如果 T+1 就长、后面差不多 → 一次性冲击，躲过那天就行；"
        + "如果 T+1 短、T+20 越来越长 → 慢变量，持有期越长越危险。",
        VIZ.legend([
          { color: "var(--bear,#1FB873)", op: 0.95, text: "T+1" },
          { color: "var(--bear,#1FB873)", op: 0.735, text: "T+5" },
          { color: "var(--bear,#1FB873)", op: 0.52, text: "T+20" },
          { color: "var(--bear,#1FB873)", dashed: true, text: "该期不显著" },
        ])
        + VIZ.groupBars({
          groups: groups,
          keys: ["T+1", "T+5", "T+20"],
          axisHint: "深→浅依次为 T+1 / T+5 / T+20。空心虚线的条表示该持有期未通过 |t|≥3.0，不能当结论用。",
          aria: "显著为负类别的分期超额对照",
        })));
    }

    /* ═══ ⑥ 三条可落地的用法 ═══ */
    var jc = pick("股东减持", 1), jc5 = pick("股东减持", 5), jc20 = pick("股东减持", 20);
    var dd = pick("订单产能", 1), dd5 = pick("订单产能", 5), dd20 = pick("订单产能", 20);
    h.push('<div class="aenv-card">'
      + '<div class="aenv-card-hd">这三条是能直接用的（其余都还只是「看着有方向」）</div>'
      + '<div class="aenv-rules">'
      + rule("①", "股东减持 —— 次日就开始跌，而且后面还在阴跌",
        jc ? ('T+1 超额 <b>' + P(jc.ex) + '</b>（t=' + N(jc.t) + '）—— 这个 t 值是全部持有期里'
          + '绝对值最大的，说明<b>次日的反应最确定、最不可能是噪音</b>；'
          + '之后 T+5 ' + P(jc5.ex) + '、T+20 ' + P(jc20.ex) + ' —— <b>幅度还在扩大</b>。'
          + '也就是说：<b>不是「跌一天就完」，而是从次日开始持续跑输</b> —— '
          + '晚反应一天，就少躲一截。') : "")
      + rule("②", "订单产能 —— 慢刀，别急但别拿",
        dd ? ('T+1 只有 ' + P(dd.ex) + '（t=' + N(dd.t) + '，不显著），但 T+5 <b>' + P(dd5.ex)
          + '</b>、T+20 <b>' + P(dd20.ex) + '</b>，越拿越亏。'
          + '这类是「当期不影响、后面才反映到报表和预期里」，所以<b>不能因为当天没跌就放心持有</b>。') : "")
      + rule("③", "回购增持 / 股权激励 / 股东回报 —— 不要拿它们当选股理由",
        '三类加起来 9.3 万个样本，T+5 分别是 ' + P(pick("回购增持", 5).ex) + ' / '
        + P(pick("股权激励", 5).ex) + ' / ' + P(pick("股东回报", 5).ex) + '，t 值全部在 ±1.5 以内。'
        + '这三类公告在市场里被讲得最多、最像「利好」，但<b>实测等于零</b>。'
        + '把它们当买入理由，正是「看着有说服力、胜率上不了 50%」的典型来源。')
      + '</div></div>');

    /* ═══ ⑦ 明细表 ═══ */
    var maxAbs = 0;
    d.rows.forEach(function (r) { maxAbs = Math.max(maxAbs, Math.abs(r.ex)); });

    function tblCell(x) {
      if (!x) return '<td class="r">—</td>';
      var col = x.ex < 0 ? "var(--bear,#1FB873)" : "var(--bull,#F2544C)";
      return '<td class="r" style="color:' + col + ';opacity:' + (x.sig ? 1 : 0.45) + '">'
        + '<span class="mono">' + P(x.ex) + '</span>'
        + '<span class="aenv-mb">' + VIZ.microBar(x.ex, maxAbs, col) + '</span></td>';
    }
    function tblRow(r) {
      var a = pick(r.cat, 1), c = pick(r.cat, 20);
      var jcls = r.sig ? (r.ex < 0 ? "bear" : "bull") : "ns";
      return '<tr class="tone-' + r.tone + '">'
        + '<td><b>' + HE(r.cat) + '</b></td>'
        + '<td><span class="aenv-tag ' + r.tone + '">' + HE(TONE_LABEL[r.tone] || "中性") + '</span></td>'
        + '<td class="r mono">' + N(r.n, 0) + '</td>'
        + tblCell(a) + tblCell(r) + tblCell(c)
        + '<td class="r mono" style="opacity:' + (r.sig ? 1 : 0.45) + '">' + N(r.win, 1) + '%</td>'
        + '<td class="r mono" style="opacity:' + (r.sig ? 1 : 0.45) + '">' + (r.t > 0 ? "+" : "") + N(r.t) + '</td>'
        + '<td><span class="aenv-judge ' + jcls + '">' + (r.sig ? "显著" : "不显著") + '</span></td>'
        + '</tr>';
    }
    var tblHtml = '<div class="aenv-tblwrap"><table class="aenv-tbl"><thead><tr>'
      + '<th>类别</th><th>标签</th><th class="r">事件数</th>'
      + '<th class="r">T+1</th><th class="r">T+5</th><th class="r">T+20</th>'
      + '<th class="r">T+5 日胜率</th><th class="r">T+5 t</th><th>判定</th>'
      + '</tr></thead><tbody>'
      + t5.map(tblRow).join("")
      + '</tbody></table></div>';
    tblHtml += '<div class="aenv-tblnote">淡色数字 = 未通过 |t|≥3.0，统计上与「这件事没发生」没有可区分的差别。'
      + '「标签」列是本面板自己的语义分组，不是模型给的 tone 字段（那批数据里 tone 全是占位符）——'
      + '分组只影响排序展示，不影响任何统计量。</div>';
    h.push(sect("明细（20 类 × 3 持有期 = 60 次检验）",
      "按 T+5 排序。判定列沿用 md 里的原始结论，不在这里二次加工。",
      tblHtml));


    /* ═══ ⑧ 局限 ═══ */
    h.push('<div class="aenv-card aenv-limit">'
      + '<div class="aenv-card-hd">这份统计不能替你做什么（同样是明说的）</div>'
      + '<ul class="aenv-ul">'
      + (m.caveats || []).map(function (c) { return '<li>' + HE(c) + '</li>'; }).join("")
      + '</ul>'
      + (m.borderline && m.borderline.length
        ? '<div class="aenv-bl"><b>卡在 3.0 ~ ' + N(m.strict_t) + ' 之间的 ' + m.borderline.length + ' 条（判据略宽松区）：</b>'
        + HE(m.borderline.join("、")) + ' —— 这几条请以 95% CI 是否跨零为准，别当硬结论。</div>'
        : "")
      + '<div class="aenv-src">数据：<code>' + HE(m.source) + '</code> · 基准：' + HE(m.bench)
      + ' · 生成：' + HE(m.generated) + ' · 重跑：'
      + '<code>.venv/Scripts/python.exe research/announcement_event_study.py</code></div>'
      + '</div>');

    h.push('</div>');
    box.innerHTML = h.join("");
  }

  /* ── 小组件 ───────────────────────────────────────────────────────── */
  function chip(text, cls) {
    return '<span class="aenv-chip' + (cls ? " " + cls : "") + '">' + HE(text) + '</span>';
  }
  function sect(title, desc, body) {
    return '<div class="aenv-card">'
      + '<div class="aenv-card-hd">' + HE(title) + '</div>'
      + (desc ? '<div class="aenv-card-desc">' + desc + '</div>' : "")
      + '<div class="aenv-card-bd">' + body + '</div></div>';
  }
  function rule(no, title, text) {
    return '<div class="aenv-rule"><span class="aenv-rno">' + no + '</span>'
      + '<div class="aenv-rbody"><div class="aenv-rt">' + HE(title) + '</div>'
      + '<div class="aenv-rx">' + text + '</div></div></div>';
  }

  /** 惰性：只有真正进入面板才渲染（静态数据，渲染一次就够，重复进入走缓存 DOM） */
  var _done = false;
  function load(force) {
    if (_done && !force) return;
    render();
    _done = true;
  }

  global.ANN = { load: load, render: render };
})(window);
