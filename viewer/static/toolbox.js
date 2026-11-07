/* ============================================================================
   工具箱 · 筛台（2026-11-05 整页重写）
   ----------------------------------------------------------------------------
   进哥原话：
     「你这策略有这么多，我点一下，然后就应该是比如筛取一半，再点又筛一半，
       再点又筛一半……你这操作界面我感觉一点儿都不智能，特别的繁琐，我看的不是特别明白」
     「设计的更便捷，然后好用一些。」
   已确认：整页替换成「筛台」，原来的 why / what / evidence / caveat 收进右侧抽屉，点开才看。

   这一页的四段（自上而下），与规格第二节完全对应：
     ① 头部：工具箱 · 筛台 + 当前池子大数字
     ② 筛道：[5264 干净宇宙] › [693 箱体谷底 −4571 ×] › [出池 675 只]
     ③ 建议下一刀：自动挑「对当前池子最狠的那一刀」，一键加上
     ④ 工具墙：每张卡就是一刀，点一下加上、再点一下撤掉；卡片上直接写「现在点：675 → 38」
     ⑤ 结果表：逐级 ✓✕ + 死在第几级 / 为什么死

   数据来源只有两个接口：
     GET /api/toolbox        货架清单（全部工具的自述 + 淘汰原因 + 最近运行）
     GET /api/toolbox/stack  筛道求交（只读落盘缓存，毫秒级；没算过的返回 pending）

   ★ 为什么以前慢、现在快（改这一页之前先理解这件事）：
     旧实现「跑这条链」= 每级都从头重算全市场，六级链 40 秒起步。
     现在每个工具对全宇宙**只算一次并落盘**（后台单线程排队算），
     叠一刀只是两个集合求交 —— 所以「点一下」必须立刻有反应，慢的是数字、不是界面。
     因此这里的交互纪律是：**永远先重绘界面，再等数字**。任何点击都不许把页面卡住。

   ★ 三个必须保住的对外契约（别改名）：
     window.TB.load()        app.js 进页面时调用
     window.TB.load(true)    强制刷新清单
     所有请求走 window.api，错误文案走 window.apiErrText，提示走 window.toast
   ========================================================================== */
(function () {
  "use strict";

  var LIMIT = 200;             // 名单一次取多少条
  var POLL_MS = 1500;          // 后台还在算时的重问间隔
  var POLL_MAX = 90000;        // 连续问 90 秒还没齐就停下来提示（别无限转圈）

  /* 面板级公共说明（只在 source_kind === "github" 的工具抽屉底部出现一行）。
     为什么不写进每张卡的工具自述：这十一条共用同一套口径交代（来源、成本、样本窗口、
     数据源复权在少数除权日有机械缺口），写 11 遍既啰嗦又一定会漂移；
     卡片里只留「这一条自己的尺度与实测」，公共部分收到这里说一次。
     ★ 这里放的是**不随重跑变化**的话；任何具体数字（-0.22%、0.15%、2035 只）都不要写进来，
       因为 Step 6 用前复权重建后那些数会变。 */
  var BASIS_NOTE = "这 11 条来自开源仓库，已按 A 股口径重算。卡片数字为 261 日窗口、"
    + "未扣交易成本；3 年复核口径见各条说明。数据源复权在少数除权日有机械缺口，"
    + "已在 3 年复核中标注。";

  var TB = {
    data: null,        // /api/toolbox 的返回
    stack: null,       // /api/toolbox/stack 的返回
    router: null,      // /api/router/plan 的返回（今日资格判定）
    horizon: 20,       // 当前看多少天的目标（决定"哪个工具今天适用"）
    wallView: "pillar",// pillar=按持有期分组（原视图）| eligibility=按今日资格分组
    chain: [],         // [{tool, params}] 筛道（顺序即展示顺序）
    counted: {},       // tool_key -> 对当前池子的剩余数（null = 还在算）
    overrides: {},     // tool_key -> {参数: 值}（抽屉里调过的）
    showDropped: false,
    drawer: null,      // 当前抽屉里的 tool_key
    err: "",           // 顶部红字（请求失败时才有，筛道不清空）
    busy: false,
    _seq: 0,           // 请求序号：丢弃过期响应，防止乱序覆盖
    _rseq: 0,          // 路由请求序号（单独一套：换持有期时会连发，不能和筛道互相丢包）
    _poll: null,
    _pollStart: 0,
    _bound: false
  };
  window.TB = TB;

  /* 持有期档位。为什么是这五个：工具契约里实际出现过的 horizons 只有
     1/2/5/10/20/40/60/120/250 这些值，收敛成五档既能覆盖全部判据，
     又不会让用户在一个下拉里挑花眼。 */
  var HORIZONS = [
    { v: 1, cn: "T+1" }, { v: 5, cn: "T+5" }, { v: 20, cn: "T+20" },
    { v: 60, cn: "T+60" }, { v: 120, cn: "T+120" }
  ];

  /* ======================= 小工具 ======================= */
  function e(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function api(url, opt) {
    if (window.api) return window.api(url, opt);
    return fetch(url, opt).then(function (r) { return r.json(); });
  }
  function errText(err) {
    if (window.apiErrText) return window.apiErrText(err);
    return (err && err.message) ? String(err.message) : String(err);
  }
  function toast(msg) {
    if (window.toast) { window.toast(msg); return; }
    if (window.showToast) { window.showToast(msg); return; }
    var d = document.createElement("div");
    d.className = "tbx-toast";
    d.textContent = msg;
    document.body.appendChild(d);
    setTimeout(function () {
      if (d.parentNode) d.parentNode.removeChild(d);
    }, 6000);
  }
  function fmt(n) {
    if (n == null || n === "" || isNaN(Number(n))) return "—";
    return String(Math.round(Number(n))).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  function host() { return document.getElementById("p-toolbox"); }
  function byKey(k) {
    var list = (TB.data && TB.data.tools) || [];
    for (var i = 0; i < list.length; i++) if (list[i].tool_key === k) return list[i];
    return null;
  }
  function chainIdx(k) {
    for (var i = 0; i < TB.chain.length; i++) if (TB.chain[i].tool === k) return i;
    return -1;
  }
  function merge(a, b) {
    var o = {}, k;
    for (k in (a || {})) if (Object.prototype.hasOwnProperty.call(a, k)) o[k] = a[k];
    for (k in (b || {})) if (Object.prototype.hasOwnProperty.call(b, k)) o[k] = b[k];
    return o;
  }
  /* 某个工具此刻该用的参数：**只发抽屉里真的改过的那些键**，一个都没改就发 {}。
     2026-11-05 改（QA P0-1：首访点「箱体谷底」白等 63 秒）——
     原来这里发的是「完整默认值」，但浏览器只有一种数字类型：
     `JSON.stringify({pos_max: 1.0})` 出来是 `{"pos_max":1}`，而服务端的默认值写作 `1.0`。
     Python 里 `1 == 1.0` 成立，可两次 JSON 序列化出来的字符串不同，参数摘要就对不上，
     预热算好的那份永远命中不了。发 {} 让后端用它自己的默认值，从源头消掉这个歧义；
     服务端 `_merge_params()` 也做了同样的兜底，两道保险。 */
  function paramsFor(t) {
    if (!t) return {};
    var def = t.default_params || {};
    var ov = TB.overrides[t.tool_key] || {};
    var out = {}, k, changed = false;
    for (k in ov) {
      if (!Object.prototype.hasOwnProperty.call(ov, k)) continue;
      if (String(ov[k]) === String(def[k] == null ? "" : def[k])) continue;  // 等于默认值 → 不单独发
      out[k] = ov[k];
      changed = true;
    }
    return changed ? out : {};
  }
  /* 抽屉里的输入框要显示**有值**的参数（默认 + 改过的），不能拿上面的增量去填。 */
  function paramsShown(t) {
    if (!t) return {};
    return merge(t.default_params || {}, TB.overrides[t.tool_key] || {});
  }
  function hasOverride(t) {
    if (!t) return false;
    var ov = TB.overrides[t.tool_key];
    if (!ov) return false;
    var def = t.default_params || {};
    for (var k in ov) {
      if (!Object.prototype.hasOwnProperty.call(ov, k)) continue;
      if (String(ov[k]) !== String(def[k] == null ? "" : def[k])) return true;
    }
    return false;
  }
  function spec() {
    return TB.chain.map(function (c) { return { tool: c.tool, params: c.params || {} }; });
  }
  function currentPool() {
    var s = TB.stack;
    if (s && s.stage_sizes && s.stage_sizes.length) {
      return s.stage_sizes[s.stage_sizes.length - 1];
    }
    return (TB.data && TB.data.universe) || 0;
  }
  /* 砍掉百分之多少。别把 679 → 2 说成「砍掉 100%」：还剩 2 只就不是 100%，
     这种四舍五入会让人以为这一刀把池子清空了，比不显示更糟。 */
  function cutTxt(cur, out) {
    if (!cur) return "0";
    var p = (cur - out) * 100 / cur;
    if (out > 0 && p > 99.9) p = 99.9;
    return p.toFixed(1).replace(/\.0$/, "");
  }

  /* 手上的结果是不是「当前这条筛道」的。
     点击之后到新结果回来之间有 100 毫秒左右，这期间旧结果还在；
     不比对就会把「上一刀的名字」和「这一刀的数字」拼在一起显示。 */
  function stackMatches() {
    var s = TB.stack;
    if (!s || !s.spec) return false;
    if (s.spec.length !== TB.chain.length) return false;
    for (var i = 0; i < s.spec.length; i++) {
      if (s.spec[i].tool !== TB.chain[i].tool) return false;
    }
    return true;
  }
  function stepOf(i) {
    var s = TB.stack;
    return (s && s.steps && s.steps[i]) ? s.steps[i] : null;
  }

  var KIND = { view: "观察器", filter: "筛选器", scorer: "评分器" };
  var STATUS = { active: "在用", shadow: "影子", experiment: "试验", retired: "已淘汰" };
  var SRC = { github: "GitHub 开源仓库", bilibili: "B 站语料", doubao: "豆包语料",
    builtin: "本项目自带" };

  /* ======================= 主入口 ======================= */
  TB.load = function (force) {
    var h = host();
    if (!h) return;
    if (TB.data && !force) { TB.shell(); TB.paint(); TB.loadRouter(); TB.refreshStack(true); return; }
    if (!TB.data) {
      h.innerHTML = '<div class="tbx-head"><div class="tbx-title">工具箱 · 筛台</div></div>'
        + '<div class="tbx-empty">正在读取工具货架…</div>';
    }
    api("/api/toolbox" + (force ? "?t=" + Date.now() : ""))
      .then(function (d) {
        if (!d || d.ok === false) {
          TB.err = "工具清单读取失败：" + ((d && d.error) || "空返回");
          if (!TB.data) {
            h.innerHTML = '<div class="tbx-head"><div class="tbx-title">工具箱 · 筛台</div></div>'
              + '<div class="tbx-empty">' + e(TB.err) + '</div>';
            return;
          }
          TB.paint();
          return;
        }
        TB.data = d;
        TB.err = "";
        TB.shell();
        TB.paint();
        TB.loadRouter();
        TB.refreshStack(true);
      })
      .catch(function (err) {
        TB.err = "工具清单读取失败：" + errText(err);
        if (!TB.data) {
          h.innerHTML = '<div class="tbx-head"><div class="tbx-title">工具箱 · 筛台</div></div>'
            + '<div class="tbx-empty">' + e(TB.err) + "</div>";
          return;
        }
        TB.paint();
      });
  };

  /* ======================= 页面骨架（只建一次） =======================
     为什么要分「骨架」和「paint」两步：轮询每 1.5 秒就可能重绘一次，
     整页 innerHTML 重刷会把结果表的滚动位置和抽屉里的输入框焦点一起冲掉。 */
  TB.shell = function () {
    var h = host();
    if (!h) return;
    h.innerHTML =
      '<div class="tbx-head">'
      + '  <div class="tbx-titleWrap">'
      + '    <div class="tbx-title">工具箱 · 筛台</div>'
      + '    <div class="tbx-sub">点一张卡 = 加一道筛，再点一下 = 撤掉。卡片上的数字是'
      + '「这一刀现在砍完还剩多少」，不是历史成绩。</div>'
      + '  </div>'
      + '  <div class="tbx-pool"><div class="tbx-poolL">当前池子</div>'
      + '    <div class="tbx-poolN" id="tbxPoolN">—</div></div>'
      + '</div>'
      + '<div class="tbx-alert" id="tbxAlert"></div>'
      + '<section class="tbx-sec" id="tbxRouterSec"></section>'
      + '<section class="tbx-sec">'
      + '  <div class="tbx-secH"><span class="tbx-secT">筛道</span>'
      + '    <span class="tbx-secN" id="tbxLaneN"></span>'
      + '    <button class="tbx-mini" data-tbx-act="clear" id="tbxClear">清空</button></div>'
      + '  <div class="tbx-lane" id="tbxLane"></div>'
      + '</section>'
      + '<section class="tbx-sec" id="tbxNextSec"></section>'
      + '<section class="tbx-sec">'
      + '  <div class="tbx-secH"><span class="tbx-secT">工具墙</span>'
      + '    <span class="tbx-secN" id="tbxWallN"></span>'
      + '    <span class="tbx-viewTab">'
      + '      <button data-tbx-act="wallview" data-v="pillar">按持有期</button>'
      + '      <button data-tbx-act="wallview" data-v="eligibility">按今日资格</button>'
      + '    </span></div>'
      + '  <div class="tbx-wall" id="tbxWall"></div>'
      + '</section>'
      + '<section class="tbx-sec" id="tbxResSec">'
      + '  <div class="tbx-secH"><span class="tbx-secT" id="tbxResT">结果</span>'
      + '    <span class="tbx-secN" id="tbxResN"></span></div>'
      + '  <div id="tbxRes"></div>'
      + '</section>'
      ;
    ensureNodes();
    bindOnce();
    paintDrawer();          // 骨架是新的，抽屉内容要照老状态补回来
  };

  /* 抽屉和遮罩留在面板里（position:fixed，祖先没有 transform 之类，
     所以相对视口定位是对的），好处是切到别的面板时 `.panel{display:none}`
     会自动把它们一起藏起来 —— 挂 body 反而会留在别的面板上漂着。 */
  function ensureNodes() {
    var h = host();
    if (!h) return;
    var mk = function (id, cls, tag) {
      var el = document.getElementById(id);
      if (!el) {
        el = document.createElement(tag || "div");
        el.id = id;
        el.className = cls;
        h.appendChild(el);
      }
      return el;
    };
    mk("tbxMask", "tbx-mask");
    mk("tbxDrawer", "tbx-drawer", "aside");
  }

  function bindOnce() {
    if (TB._bound) return;
    TB._bound = true;
    document.addEventListener("click", function (ev) {
      // 点遮罩关抽屉（规格 2.6）。用委托而不是给 #tbxMask 单独绑：
      // 骨架重建时遮罩会被换掉，单独绑过的那次就没了。
      if (ev.target && ev.target.id === "tbxMask") { TB.closeDrawer(); return; }
      var el = walk(ev.target, "data-tbx-info");
      if (el) { TB.openDrawer(el.getAttribute("data-tbx-info")); ev.preventDefault(); return; }
      el = walk(ev.target, "data-tbx-del");
      if (el) { TB.cutAt(parseInt(el.getAttribute("data-tbx-del"), 10)); return; }
      el = walk(ev.target, "data-tbx-card");
      if (el) { TB.toggle(el.getAttribute("data-tbx-card")); return; }
      el = walk(ev.target, "data-tbx-act");
      if (el) { act(el.getAttribute("data-tbx-act"), el); return; }
      // 结果表点某一行 → 打开该股完整详情（行情 / K 线 / 持仓）。
      // 必须放在 data-tbx-act 之后：「显示被淘汰的 N 只」那个 checkbox 在表格外，
      // 但表格里的原因文字可能被用户拖选复制，所以先判有没有选中文字。
      el = walk(ev.target, "data-tbx-sym");
      if (el) {
        var sel = window.getSelection ? window.getSelection() : null;
        if (sel && String(sel).length) return;
        var sym = (el.getAttribute("data-tbx-sym") || "").trim();
        if (!sym) return;
        // openStock 在 app.js（本文件之后加载，但点击时必然已就绪）
        if (window.openStock) window.openStock(sym, el);
        else toast("个股详情暂时打不开（页面还没加载完），刷新一下再点");
        return;
      }
    });
    document.addEventListener("change", function (ev) {
      var el = ev.target;
      if (!el || !el.getAttribute) return;
      var tool = el.getAttribute("data-tbx-ptool");
      var pk = el.getAttribute("data-tbx-pkey");
      if (!tool || !pk) return;
      var val;
      if (el.type === "checkbox") val = el.checked ? 1 : 0;
      else if (el.type === "number") {
        if (el.value === "") return;
        val = Number(el.value);
        if (isNaN(val)) { toast("这个参数只能填数字"); return; }
      } else val = el.value;
      TB.setParam(tool, pk, val);
    });
    document.addEventListener("keydown", function (ev) {
      if (!TB.drawer) return;
      if (ev.key === "Escape" || ev.keyCode === 27) { TB.closeDrawer(); }
    });
  }

  /* 从事件目标往上找带某个属性的元素。
     属性名都带 tbx 前缀（funnel.js 用的是 data-act），所以不会和别的面板撞车，
     也就不需要靠「找到 #p-toolbox 就停」来划边界 —— 抽屉本来就在面板外面。 */
  function walk(node, attr) {
    while (node && node.getAttribute) {
      if (node.hasAttribute(attr)) return node;
      node = node.parentNode;
    }
    return null;
  }

  function act(name, el) {
    if (name === "clear") { TB.clear(); return; }
    if (name === "suggest") { TB.addSuggest(); return; }
    if (name === "close") { TB.closeDrawer(); return; }
    if (name === "retire") { TB.retire(); return; }
    if (name === "restore") { TB.restore(); return; }
    if (name === "toggle-dead") { TB.toggleDead(el.checked); return; }
    if (name === "retry") { TB.refreshStack(true); return; }
    // 换持有期 = 换一个决策问题（"这票下周能不能拿"和"能不能拿半年"是两件事），
    // 所以资格要整份重判，不能只改个高亮。
    if (name === "horizon") {
      var v = parseInt(el.getAttribute("data-v"), 10) || 20;
      if (v === TB.horizon) return;
      TB.horizon = v;
      var sec = document.getElementById("tbxRouterSec");
      if (sec) sec.innerHTML = renderRouter();     // 先给个"正在判定"的即时反馈
      TB.loadRouter();
      return;
    }
    if (name === "wallview") {
      var wv = el.getAttribute("data-v") || "pillar";
      if (wv === TB.wallView) return;
      TB.wallView = wv;
      TB.paint();
      return;
    }
  }

  /* ======================= 重绘（每次拿到新数据都走这里） ======================= */
  TB.paint = function () {
    if (!TB.data) return;
    var poolN = document.getElementById("tbxPoolN");
    if (poolN) poolN.textContent = fmt(currentPool());

    var alertBox = document.getElementById("tbxAlert");
    if (alertBox) {
      alertBox.style.display = TB.err ? "block" : "none";
      alertBox.innerHTML = TB.err
        ? '<span class="tbx-alertT">' + e(TB.err) + '</span>'
          + '<button class="tbx-mini" data-tbx-act="retry">重试</button>'
        : "";
    }

    var lane = document.getElementById("tbxLane");
    if (lane) lane.innerHTML = renderLane();
    var laneN = document.getElementById("tbxLaneN");
    if (laneN) {
      var w = (TB.stack && TB.stack.warm) || (TB.data && TB.data.warm) || null;
      laneN.textContent = w ? ("预热 " + (w.ready || 0) + "/" + (w.total || 0) + " 个工具") : "";
    }

    var nextSec = document.getElementById("tbxNextSec");
    if (nextSec) nextSec.innerHTML = renderNext();

    var wall = document.getElementById("tbxWall");
    if (wall) {
      wall.className = "tbx-wall" + (TB.busy ? " tbx-busy" : "");
      wall.innerHTML = renderWall();
    }
    var wallN = document.getElementById("tbxWallN");
    if (wallN) {
      var tools = (TB.data && TB.data.tools) || [];
      /* ★ 2026-11-06 审计 P0-02：这里以前数的是 `status !== 'retired'`，显示「可用 31 个」。
         那个数字是错的：31 里混着 9 个影子、4 个说明性、3 个回测结论为负却还是 active 的工具。
         「可用」这个词一旦被这样用，用户就没有任何依据判断该点谁 —— 而这正是审计的核心问题。
         现在按三轴口径分开数：可决策 / 仅观察 / 不该开 / 已淘汰。 */
      var nDead = 0, nNormal = 0, nWatch = 0, nOff = 0;
      for (var i = 0; i < tools.length; i++) {
        var tt = tools[i];
        if (tt.status === "retired") { nDead++; continue; }
        if (tt.decision_mode === "watch_only") nWatch++;
        else if (tt.can_decide) nNormal++;
        else nOff++;
      }
      wallN.textContent = "可决策 " + nNormal + " · 仅观察 " + nWatch
        + " · 不该开 " + nOff
        + (nDead ? " · 已淘汰 " + nDead + "（照样摆着，写明了原因）" : "");
    }
    // 视图切换按钮的高亮也是状态的一部分：骨架只建一次，不在这里刷就永远是第一次那个。
    var tabs = document.querySelectorAll(".tbx-viewTab button");
    for (var ti = 0; ti < tabs.length; ti++) {
      var tv = tabs[ti].getAttribute("data-v");
      if (tv === TB.wallView) tabs[ti].className = "on";
      else tabs[ti].className = "";
    }

    paintRes();
    // 刻意**不**在这里重绘抽屉：轮询 1.5 秒一次，重绘会把「淘汰原因」输入框
    // 里正在打的字和焦点一起冲掉。抽屉只在打开/关闭/改状态时重绘。
  };

  /* ---------- 筛道 ---------- */
  function renderLane() {
    var s = TB.stack;
    if (!TB.chain.length) {
      return '<div class="tbx-laneEmpty">还没筛 —— 下面任点一张卡就开始了</div>';
    }
    var uni = s ? s.universe : ((TB.data && TB.data.universe) || 0);
    var aligned = stackMatches();
    var out = ['<div class="tbx-laneBar">',
      '<div class="tbx-node start"><div class="tbx-nN">' + fmt(uni) + '</div>'
      + '<div class="tbx-nL">干净宇宙</div></div>'];

    TB.chain.forEach(function (c, i) {
      var t = byKey(c.tool);
      var nm = t ? t.name : c.tool;
      var st = aligned ? stepOf(i) : null;
      out.push('<div class="tbx-arrow">›</div>');
      if (st) {
        out.push('<div class="tbx-node' + (st.error ? " err" : "") + '" title="'
          + e(st.error || st.note || "") + '">'
          + '<span class="tbx-nX" data-tbx-del="' + i + '" title="只撤掉这一刀">×</span>'
          + '<div class="tbx-nN">' + fmt(st.out) + '</div>'
          + '<div class="tbx-nL">' + e(st.name || nm) + '</div>'
          + (st.drop ? '<div class="tbx-nD">−' + fmt(st.drop) + '</div>' : '')
          + '</div>');
      } else if (s && s.pending && s.pending.idx === i) {
        out.push('<div class="tbx-node pend">'
          + '<span class="tbx-nX" data-tbx-del="' + i + '">×</span>'
          + '<div class="tbx-nN">…</div>'
          + '<div class="tbx-nL">' + e(nm) + ' 正在算</div></div>');
      } else {
        out.push('<div class="tbx-node">'
          + '<span class="tbx-nX" data-tbx-del="' + i + '">×</span>'
          + '<div class="tbx-nN">—</div>'
          + '<div class="tbx-nL">' + e(nm) + '</div></div>');
      }
    });

    if (aligned && s && s.ok !== false && !s.pending && s.steps && s.steps.length === TB.chain.length) {
      out.push('<div class="tbx-arrow">›</div>');
      out.push('<div class="tbx-node out"><div class="tbx-nN">' + fmt(s.final) + '</div>'
        + '<div class="tbx-nL">出池 ' + fmt(s.final) + ' 只</div></div>');
    }
    out.push("</div>");
    return out.join("");
  }

  /* ---------- 建议下一刀（2026-11-05 改：渐进收窄，不再「一刀切到个位数」）----------
     进哥原话：「点一下筛取一半，再点又筛一半」。所以建议的目标不是「最狠的那一刀」，
     而是「离目标比例最近的那一刀」。

     旧规则（取剩余最少且 > 0）在空池子上会推荐「短线冲浪 5264 → 4」「均线多头 679 → 2」——
     一刀切到个位数，后面十个工具全废了，剩下的名单也没法挑。那不是建议，那是坑。

     新规则：目标剩余比例越往下越收，用「对数距离」挑最接近的那一刀：

         targetRatio(5264) = 0.15  → 目标 790 只，箱体谷底 699 最近 → 推荐它
         targetRatio( 679) = 0.15  → 目标 102 只
         targetRatio( 200) = 0.20  → 目标  40 只
         targetRatio(  60) = 0.5(触底) 封顶，且保底 40 只 → 目标 40 只

     为什么用 |log(剩余比例) - log(目标比例)| 而不是线性差：线性差会系统性偏向「几乎不动」的工具
     —— 0.30 与 0.15 的线性差（0.15）比 0.90 与 0.15 的差（0.75）小得多，但那两个对用户的
     意义完全不同；取对数之后是「按倍数看距离」，砍半（×0.5）和砍到 1/6（×0.167）才有可比性。

     两条硬保护（不满足就直接出局，宁可不给建议）：
       ① 候选剩余 < 5 只且当前池 > 50 只 → 排除（这就是「一刀清空」的坑）
       ② 候选剩余 == 当前池（一点没砍掉）→ 排除（那不是筛，那是原地踏步）
     当前池 ≤ 20 只时整体不出建议：这个量级再加一刀大概率归零，该看名单了。 */

  /* 目标剩余比例：池子越小，目标绝对值跟着收，避免最后几刀过狠 */
  function targetRatio(cur) {
    var r = 40 / cur;              // 池子小于 267 只时，目标保底 40 只左右
    if (r > 0.5) r = 0.5;
    if (r < 0.15) r = 0.15;        // 池子大时，目标砍到 15%
    return r;
  }

  function suggest() {
    var s = TB.stack;
    if (!s) return null;
    var cur = currentPool();
    if (cur <= 20) return null;
    var target = targetRatio(cur);
    var selected = {}, k;
    for (var i = 0; i < TB.chain.length; i++) selected[TB.chain[i].tool] = 1;
    /* ★ 2026-11-06 审计 P0-03：这里的过滤是这次改造的核心之一。
       旧算法唯一的条件是「数量够接近目标」，于是它会把
       `pick_ma_bull`（下跌行情 T+40 超额 −3.683pp）、`oss_high_vol`
       （回测结论是"高波动=风险"）当成推荐，理由仅仅是"它们砍完剩下 155 只、离目标最近"。
       那是数量规划，不是策略规划。现在两道门：
         ① 静态权限：can_decide=false 的一律不推荐（反向/淘汰/无契约）
         ② 今日资格：/api/router/plan 判为不可用的不推荐（行情不符、周期错配、数据过期） */
    var offSet = {};
    var r = TB.router;
    if (r && r.blocked_tools) {
      for (var bi = 0; bi < r.blocked_tools.length; bi++) {
        offSet[r.blocked_tools[bi].tool_key] = 1;
      }
    }
    var best = null;
    for (k in TB.counted) {
      if (!Object.prototype.hasOwnProperty.call(TB.counted, k)) continue;
      if (selected[k]) continue;                      // 已经在筛道里
      var t = byKey(k);
      if (t && t.can_decide === false) continue;      // 门①：没有决策权限
      if (offSet[k]) continue;                        // 门②：今天不该开
      var v = TB.counted[k];
      if (v == null) continue;                        // 还没算完
      if (v === cur) continue;                        // 硬保护②：一点没砍掉
      if (v < 5 && cur > 50) continue;                // 硬保护①：这一刀会把池子清空
      if (v <= 0) continue;
      var d = Math.abs(Math.log(v / cur) - Math.log(target));
      if (!best || d < best.d) best = { key: k, n: v, d: d };
    }
    return best;
  }

  function renderNext() {
    // 空筛道也给建议：刚进页面最该回答的就是「从哪下第一刀」。
    var cur = currentPool();
    if (cur <= 20) {
      return '<div class="tbx-next off"><div class="tbx-nextK">建议下一刀</div>'
        + '<div class="tbx-nextT">池子已经很小了（' + fmt(cur) + ' 只），再加刀容易一刀清空 —— 直接看名单。</div></div>';
    }
    var sg = suggest();
    if (!sg) {
      var waiting = false, kk;
      for (kk in TB.counted) {
        if (Object.prototype.hasOwnProperty.call(TB.counted, kk) && TB.counted[kk] == null) waiting = true;
      }
      return '<div class="tbx-next off"><div class="tbx-nextK">建议下一刀</div>'
        + '<div class="tbx-nextT">' + (waiting
          ? "剩下的工具还在后台算，算完这里会自动补一条建议。"
          : (TB.router && TB.router.counts && TB.router.counts.blocked >= 20
            ? "当前持有期与行情下，没有合格的正向工具可以推荐（只剩排除类能用）。"
              + "这不是坏了 —— 硬推荐一把被证伪的工具，只会得到一份没有依据的名单。"
            : "剩下的工具要么砍不动、要么一刀就清空 —— 别再加刀了，直接看名单。")) + "</div></div>";
    }
    var t = byKey(sg.key);
    var nm = t ? t.name : sg.key;
    var cut = cutTxt(cur, sg.n);
    return '<div class="tbx-next"><div class="tbx-nextK">建议下一刀</div>'
      + '<div class="tbx-nextBody">'
      + '  <span class="tbx-nextName">' + e(nm) + '</span>'
      + '  <span class="tbx-nextN">' + fmt(cur) + ' → <b>' + fmt(sg.n) + '</b>'
      + '（砍掉 ' + cut + '%）</span>'
      + '  <button class="tbx-btn primary" data-tbx-act="suggest">就筛这一刀</button>'
      + '  <span class="tbx-nextI" data-tbx-info="' + e(sg.key) + '" title="这一刀是什么来路">看它凭什么</span>'
      + '</div></div>';
  }

  /* ---------- 今日路由（2026-11-06 审计 T05/T06/T07） ----------
     这一块回答的是这一页以前**回答不了**的问题：今天该开哪把工具。

     改之前这里只有「建议下一刀」，它的算法是「挑一个能让池子最接近目标数量的工具」。
     那是数量规划，不是策略规划 —— 它不知道当前是弱势行情、不知道你打算拿几天、
     也不知道某个工具的回测结论已经是负的。实测就出现过
     「均线多头（下跌行情 −3.683pp）+ 超跌反弹 → 0 只」而接口照样 ok=true。

     判定只有一份，在后端 /api/router/plan（viewer/tool_router.py）。
     前端**不重算**，只负责把「能用 / 只能看 / 为什么不能用」摆清楚 ——
     一旦这里自己判一次，两端就会漂移，而且不报错、只能靠人眼发现。 */
  TB.loadRouter = function () {
    var my = ++TB._rseq;
    var hz = TB.horizon;
    api("/api/router/plan?horizon_days=" + hz)
      .then(function (d) {
        if (my !== TB._rseq || hz !== TB.horizon) return;   // 用户已经换了持有期
        TB.router = (d && d.ok !== false) ? d : {
          ok: false, status: "ABSTAIN",
          reason: (d && d.error) || "路由暂不可用"
        };
        var sec = document.getElementById("tbxRouterSec");
        if (sec) sec.innerHTML = renderRouter();
        // 资格视图下，分组本身依赖路由结果，必须跟着重画
        if (TB.wallView === "eligibility") {
          var wall = document.getElementById("tbxWall");
          if (wall) wall.innerHTML = renderWall();
        }
      })
      .catch(function (err) {
        if (my !== TB._rseq) return;
        TB.router = { ok: false, status: "ABSTAIN",
          reason: "路由读取失败：" + errText(err) };
        var sec = document.getElementById("tbxRouterSec");
        if (sec) sec.innerHTML = renderRouter();
      });
  };

  function statusMeta(st) {
    if (st === "ACTIONABLE") {
      return { cls: "go", cn: "今日可用",
               desc: "有合格的正向工具，可以组链" };
    }
    if (st === "WATCH") {
      return { cls: "watch", cn: "仅观察",
               desc: "有可用工具，但证据未验证 —— 只看不下结论" };
    }
    return { cls: "abstain", cn: "今日弃权",
             desc: "没有合格的正向工具 —— 不为填满页面硬凑名单" };
  }

  function renderRouter() {
    var r = TB.router;
    var hzHtml = HORIZONS.map(function (h) {
      return '<button class="tbx-hz' + (h.v === TB.horizon ? " on" : "")
        + '" data-tbx-act="horizon" data-v="' + h.v + '">' + h.cn + "</button>";
    }).join("");

    if (!r) {
      return '<div class="tbx-router loading"><div class="tbx-rTop">'
        + '<span class="tbx-rT">今日路由</span>' + '<span class="tbx-hzWrap">' + hzHtml
        + '</span></div><div class="tbx-rReason">正在按当前持况判定 33 个工具的资格…</div></div>';
    }

    var m = statusMeta(r.status);
    var ctx = r.context || {};
    var c = r.counts || {};
    var out = ['<div class="tbx-router ' + m.cls + '">'];

    out.push('<div class="tbx-rTop">'
      + '<span class="tbx-rT">今日路由</span>'
      + '<span class="tbx-rBadge ' + m.cls + '">' + e(m.cn) + "</span>"
      + '<span class="tbx-rCtx">' + e(ctx.market_regime_cn || ctx.market_regime || "")
      + (ctx.regime_ret20 != null
        ? ' · 20 日 ' + (ctx.regime_ret20 * 100).toFixed(2) + "%" : "")
      + (ctx.regime_breadth != null
        ? ' · 宽度 ' + (ctx.regime_breadth * 100).toFixed(1) + "%" : "")
      + " · 数据日 " + e(ctx.trade_date || "—") + "</span>"
      + '<span class="tbx-hzWrap">' + hzHtml + "</span>"
      + "</div>");

    out.push('<div class="tbx-rReason">' + e(r.reason || m.desc) + "</div>");

    // 选中链：只有 ACTIONABLE 才有。用「›」串起来，与筛道的视觉语言一致。
    if (r.selected_chain && r.selected_chain.length) {
      out.push('<div class="tbx-rChain"><span class="tbx-rChainK">今日链</span>'
        + r.selected_chain.map(function (s) {
          return '<span class="tbx-rStep" title="' + e(s.note || "") + '">'
            + e(s.name || s.tool_key)
            + '<i>' + e(s.role_cn || s.step) + "</i></span>";
        }).join('<span class="tbx-rArrow">›</span>')
        + "</div>");
    }

    // 为什么不用别的：只列禁用里最有信息量的几条，全列 33 条会变成噪音。
    // 但每一条都必须是**人能读懂的原因**，不是错误码。
    if (r.blocked_tools && r.blocked_tools.length) {
      var shown = r.blocked_tools.slice(0, 4);
      out.push('<div class="tbx-rBlock"><span class="tbx-rChainK">为什么不用</span>'
        + shown.map(function (b) {
          return '<span class="tbx-rWhyItem"><b>' + e(b.name || b.tool_key) + "</b>："
            + e(b.reason || "不可用") + "</span>";
        }).join("")
        + (r.blocked_tools.length > shown.length
          ? '<span class="tbx-rWhyMore">另有 ' + (r.blocked_tools.length - shown.length)
            + " 个不可用（工具墙上可逐个看）</span>" : "")
        + "</div>");
    }

    if (c.total) {
      out.push('<div class="tbx-rCount">33 个工具里：'
        + '<b class="go">' + (c.actionable || 0) + "</b> 个今天能参与判断 · "
        + '<b class="watch">' + (c.watch || 0) + "</b> 个只能观察 · "
        + '<b class="off">' + (c.blocked || 0) + "</b> 个此刻不该开"
        + '<span class="tbx-rHint">（切「按今日资格」看分组）</span></div>');
    }

    // 数据水位：审计要求"新旧数据混用不会报错，只会悄悄产生错误结论"，
    // 所以把源头日期摆在结论旁边，而不是藏在设置里。
    if (ctx.stale && ctx.stale.length) {
      out.push('<div class="tbx-rStale">⚠ ' + ctx.stale.map(e).join("；") + "</div>");
    }
    out.push("</div>");
    return out.join("");
  }

  /* ---------- 工具墙 ----------
     2026-11-05 进哥改口径：四道门拆成六道，工具墙也必须**按持有期分组**，
     否则卡片还是糊成一片，分不分类在界面上看不出来。

     分组的唯一来源是 `/api/toolbox` 返回的 `pillar_meta`（后端 viewer/toolbox.py 的
     PILLAR_ORDER）。**这里绝不硬编码「工具 → 分类」**：
     下一次改分类（已经发生过一次：短线超跌 → 超跌修复）时，手抄的那份必然错位，
     而且不报错、只能靠人眼发现 —— 那是这套东西最容易烂掉的地方。

     一个工具可以属于两个持有期（箱体谷底＝筑底布局＋长期埋伏；自学习选股＝热点＋超跌），
     那就**两组都出现**。这是刻意的，不是重复：同一个判据在两种持有时长下解释不同，
     删掉任何一边都等于替用户做了一个他不该被代替的决定。
     已淘汰的工具单独一组放最后，不混进正常分组（但要看得见，这是既有规矩）。 */
  function pillarMeta() {
    var m = (TB.data && TB.data.pillar_meta) || [];
    return (m && m.length) ? m : null;
  }

  /* 资格查询：把 /api/router/plan 的三份清单压成 key → {g, reason}。
     遍历而不是建字典：33 条，一次线性查找比每次重建 Map 更省事，也不会因为
     路由结果更新而留下旧字典（这类"两处状态不同步"的 bug 最难查）。 */
  function eligibilityOf(key) {
    var r = TB.router;
    if (!r) return null;
    var i;
    for (i = 0; i < (r.eligible_tools || []).length; i++) {
      if (r.eligible_tools[i].tool_key === key) {
        return { g: "go", reason: "" };
      }
    }
    for (i = 0; i < (r.watch_tools || []).length; i++) {
      if (r.watch_tools[i].tool_key === key) {
        return { g: "watch", reason: (r.watch_tools[i].reasons || []).join("；") };
      }
    }
    for (i = 0; i < (r.blocked_tools || []).length; i++) {
      if (r.blocked_tools[i].tool_key === key) {
        return { g: "off", reason: r.blocked_tools[i].reason || "" };
      }
    }
    return null;
  }

  var ELIG_GROUPS = [
    { g: "go", cn: "今日可用", note: "通过资格判定，可参与今天的判断（ACTIONABLE 的链只从这里挑）" },
    { g: "watch", cn: "仅观察", note: "能给材料，但不构成买入依据：箱底、形态、机构持仓都属于这一类" },
    { g: "off", cn: "此刻不该开", note: "行情不符 / 周期错配 / 证据反向 / 数据过期 / 权限不足 —— 每张卡都写了具体原因" }
  ];

  function renderWallByEligibility(tools) {
    if (!TB.router) {
      return '<div class="tbx-empty">正在判定今日资格…</div>';
    }
    var buckets = { go: [], watch: [], off: [] };
    var dead = [];
    tools.forEach(function (t) {
      if (t.status === "retired") { dead.push(t); return; }
      var el = eligibilityOf(t.tool_key);
      var g = el ? el.g : "off";
      buckets[g].push(t);
    });
    var html = [];
    ELIG_GROUPS.forEach(function (meta) {
      var list = buckets[meta.g];
      if (!list.length) return;
      html.push(groupHtml(meta.cn, TB.horizon + " 日视角 · " + list.length + " 个",
        meta.note, list.length, null, list));
    });
    if (dead.length) {
      html.push(groupHtml("已淘汰", "已下架 · 点开看为什么淘汰",
        "留档不删：淘汰原因一直写在卡片上，避免半年后重新捞起同一个坑",
        dead.length, null, dead));
    }
    return html.join("");
  }

  function renderWall() {
    var tools = (TB.data && TB.data.tools) || [];
    if (!tools.length) return '<div class="tbx-empty">货架是空的</div>';
    if (TB.wallView === "eligibility") return renderWallByEligibility(tools);
    var metas = pillarMeta();
    if (!metas) {
      /* 后端还是旧进程（分类表在 python 里，页面文件更新了但它没重启）：
         退化成原来那一整片卡片，并说清楚为什么 —— 不能摆一排空分组让人以为工具没了。 */
      return '<div class="tbx-alert" style="display:block"><span class="tbx-alertT">'
        + '工具的分类表还没生效（后端还是旧进程）—— 关掉启动窗口重新双击 '
        + 'start_viewer.bat 之后，这里会按「竞价冲浪 / 短线热点 / 超跌修复 / 中线突破 / '
        + '筑底布局 / 长期埋伏 / 跨周期闸门」分组，并标出每一类该拿多久。</span></div>'
        + groupHtml("全部工具", "尚未分组", "", tools.length, null, tools);
    }
    var html = [];
    metas.forEach(function (m) {
      var list = tools.filter(function (t) {
        return t.status !== "retired" && (t.pillar_list || []).indexOf(m.key) >= 0;
      });
      if (!list.length) return;   // 某一类暂时没有工具就别画空标题（空标题只会让人找）
      html.push(groupHtml(m.cn, m.span, m.note, list.length, m, list));
    });
    var dead = tools.filter(function (t) { return t.status === "retired"; });
    if (dead.length) {
      html.push(groupHtml("已淘汰", "已下架 · 点开看为什么淘汰",
        "留档不删：淘汰原因一直写在卡片上，避免半年后重新捞起同一个坑",
        dead.length, null, dead));
    }
    return html.join("");
  }

  /* 一组 = 标题行（中文名 + 持有期标注 + 数量）+ 一句说明 + 卡片网格。
     meta 为 null 时是不分组的兜底（旧后端 / 已淘汰组）：不显示「判据回看」那行字。 */
  function groupHtml(title, span, note, n, meta, list) {
    return '<div class="tbx-grp">'
      + '<div class="tbx-grpH">'
      + '<span class="tbx-grpT">' + e(title) + '</span>'
      + '<span class="tbx-grpS">' + e(span) + '</span>'
      + '<span class="tbx-grpN">' + n + ' 个工具</span>'
      + '</div>'
      + (note ? '<div class="tbx-grpNote">' + e(note) + '</div>' : '')
      + '<div class="tbx-grpWall">'
      + list.map(function (t) { return cardHtml(t, meta); }).join("")
      + '</div></div>';
  }

  function cardHtml(t, meta) {
    var k = t.tool_key;
    var dead = t.status === "retired";
    var sel = chainIdx(k) >= 0;
    var v = TB.counted[k];
    var cur = currentPool();
    var cls = "tbx-card" + (dead ? " dead" : (sel ? " on" : ""));
    var num, bar = 0, foot;
    var danger = false, near = false, warnLine = "";
    if (dead) {
      num = '<span class="tbx-dim">已淘汰</span>';
      foot = "已淘汰 · 点开看为什么";
    } else if (sel) {
      var st = null;
      if (stackMatches()) {
        for (var j = 0; j < TB.chain.length; j++) {
          if (TB.chain[j].tool === k) st = stepOf(j);
        }
      }
      if (st && !st.error) {
        num = "已在筛道 <b>" + fmt(st.out) + "</b>";
        bar = cur ? Math.max(0, Math.min(100, 100 * (st.drop || 0) / (st.in || cur || 1))) : 0;
      } else {
        num = '<span class="tbx-dim">这一关正在算…</span>';
      }
      foot = "已在筛道 · 点一下撤掉";
    } else if (hasOverride(t)) {
      num = '<span class="tbx-dim">改过参数 · 点我按新参数算</span>';
      foot = "参数已改，点一下按新参数下这一刀";
    } else if (v == null) {
      var err = (TB.stack && TB.stack.errors && TB.stack.errors[k]) || "";
      num = err ? '<span class="tbx-bad" title="' + e(err) + '">算不出来</span>'
        : '<span class="tbx-dim">待算</span>';
      foot = err ? "这个工具算不出来，点开看原因" : "后台排队中，算完自动填数";
    } else {
      /* 2026-11-05 加（team-lead 截图发现）：卡片上出现「679 → 0」时没有任何警示意味，
         手一抖点下去池子就空了，用户会以为程序坏了。所以：
         剩 0   → 「679 → 会清空」+ 红色，点击前弹一次确认；
         剩 ≤5  → 「679 → 3 只（几乎清空）」+ 琥珀色，点击前只给一句 toast 提示。
         注意：这只影响**展示和点击确认**，counted 的真实数值不动，
         「建议下一刀」的排除逻辑也不受影响（它本来就排除了 0 和个位数）。 */
      danger = (v === 0);
      near = (!danger && v > 0 && v <= 5 && cur > 20);
      if (danger) {
        num = "本次 " + fmt(cur) + ' → <b class="tbx-hell">会清空</b>';
        warnLine = '<div class="tbx-warnT">点下去池子就空了</div>';
      } else if (near) {
        num = "本次 " + fmt(cur) + " → <b>" + fmt(v) + "</b> 只";
        warnLine = '<div class="tbx-warnT">几乎清空</div>';
      } else {
        num = "本次 " + fmt(cur) + " → <b>" + fmt(v) + "</b>";
      }
      bar = cur ? Math.max(0, Math.min(100, 100 * (cur - v) / cur)) : 0;
      foot = (KIND[t.kind] || "工具")
        + ((t.pillar_cn && t.pillar_cn.length) ? " · " + t.pillar_cn.join("/") : "");
    }
    /* 「判据回看 X · 建议持有 Y」—— 这一行回答的是「为什么它归到这一组」：
       判据自己回看多久，这类就该拿多久。分组时取自当前组的 meta（同一个判据在
       两个组里会出现两种持有期，比如箱体谷底），所以**不能用 t 上的字段**，
       必须用这一组自己的口径。 */
    var hold = meta
      ? '<div class="tbx-cHold" title="这类判据回看 ' + e(meta.lookback)
        + '，所以建议持 ' + e(meta.hold) + '">'
        + '判据回看 ' + e(meta.lookback) + ' · 建议持有 ' + e(meta.hold) + '</div>'
      : "";
    if (danger) cls += " danger";
    var tip = dead ? (t.name + "：已淘汰，点开看为什么淘汰")
      : danger ? (t.name + "：这一刀会把 " + fmt(cur) + " 只全部筛掉，点之前会先问你一次")
      : (t.name + "：点一下加一道筛，再点一下撤掉");
    /* 资格视图下，每张卡都必须自证「今天能不能用」。
       放在左侧色条 + 底部小字，而不是再加一个大角标：右上角已经有「说明性」和 i，
       再堆一个徽章会互相压住（这页 168px 宽的卡片没有第二个角的余量）。 */
    var elig = TB.wallView === "eligibility" && !dead ? eligibilityOf(k) : null;
    if (elig) {
      cls += " elig-" + elig.g;
      var _pre = elig.g === "go" ? "今日可用"
        : (elig.g === "watch" ? "仅观察" : "不该开");
      foot = _pre + " · " + foot;
      if (elig.reason) tip += "；" + elig.reason;
    }
    // 说明性标注（2026-11-05）：11 条开源规则的定位。**不是淘汰**，卡片照常能点能用，
    // 只是不再当买卖依据。所以留在这个周期分组里、只加一个小角标 —— 把它整体挪到别的
    // 分组会让人重新找一遍工具，代价大于收益。
    if (t.advisory) {
      tip += "（说明性标注：不参与进出决策，仅供看形态）";
      cls += " adv";
    }
    return '<div class="' + cls + '" data-tbx-card="' + e(k) + '" title="' + e(tip) + '">'
      + (t.advisory ? '<span class="tbx-cardAdv" title="说明性标注：不参与进出决策">说明性</span>' : "")
      + '<span class="tbx-i" data-tbx-info="' + e(k) + '" title="看这个工具的来龙去脉">i</span>'
      + '<div class="tbx-cName">' + e(t.name) + "</div>"
      + '<div class="tbx-cNum">' + num + "</div>"
      + warnLine
      + hold
      + '<div class="tbx-bar"' + (dead ? ' style="display:none"' : "") + '>'
      + '<i style="width:' + bar.toFixed(1) + '%"></i></div>'
      + '<div class="tbx-cFoot" title="' + e(foot) + '">' + e(foot) + "</div>"
      + (dead ? '<span class="tbx-dead">已淘汰</span>' : "")
      + "</div>";
  }

  /* ---------- 结果表 ---------- */
  function paintRes() {
    var box = document.getElementById("tbxRes");
    if (!box) return;
    var wrap = document.getElementById("tbxTblWrap");
    var keep = wrap ? wrap.scrollTop : 0;
    box.innerHTML = renderRes();
    var wrap2 = document.getElementById("tbxTblWrap");
    if (wrap2 && keep) wrap2.scrollTop = keep;
  }

  function renderRes() {
    var s = TB.stack;
    var tEl = document.getElementById("tbxResT");
    var nEl = document.getElementById("tbxResN");
    if (!TB.chain.length) {
      if (tEl) tEl.textContent = "结果";
      if (nEl) nEl.textContent = "";
      return '<div class="tbx-empty">还没筛，没有名单。</div>';
    }
    if (!s) {
      if (tEl) tEl.textContent = "结果";
      if (nEl) nEl.textContent = "";
      return '<div class="tbx-empty">正在读筛道…</div>';
    }
    var steps = s.steps || [];
    var rows = s.rows || [];
    var keeps = rows.filter(function (x) { return x.failed_at < 0; });
    var deads = rows.filter(function (x) { return x.failed_at >= 0; });
    var shown = TB.showDropped ? keeps.concat(deads) : keeps;

    /* ★ 2026-11-06 审计 P1-02：链断了就不能再展示名单。
       后端现在会给 ok=false + aborted_at + reason + final=null；
       这里必须**锁住结果区**，否则页面上仍会出现「共淘汰 N 只」这种看起来正常的输出，
       而用户根本不会注意到某一步已经报错（实测 retired 工具就是这样骗过调用方的）。 */
    if (s.ok === false) {
      if (tEl) tEl.textContent = "筛道中止";
      if (nEl) nEl.textContent = "";
      var badStep = (s.steps && s.steps[s.aborted_at]) || {};
      return '<div class="tbx-resErr">'
        + '<div class="tbx-resErrH">第 ' + (Number(s.aborted_at) + 1) + " 步「"
        + e(badStep.name || s.failed_tool_name || s.failed_tool || "未知工具") + "」执行失败</div>"
        + '<div class="tbx-resErrB">' + e(s.reason || badStep.error || "未知错误") + "</div>"
        + '<div class="tbx-resErrN">断在这里之前走到了 '
        + fmt(s.interim_final != null ? s.interim_final : 0)
        + " 只 —— 但这不是结论，不能当成推荐名单用。把出错这一步撤掉再筛。" + "</div>"
        + "</div>";
    }

    if (tEl) {
      tEl.textContent = (s.pending ? "当前 " + fmt(s.final) + " 只（还有一刀在算）"
        : "最终 " + fmt(s.final) + " 只");
    }
    if (nEl) {
      nEl.textContent = "数据日 " + (s.trade_date || "—")
        + (s.n_failed ? " · 共淘汰 " + fmt(s.n_failed) + " 只（下表列前 " + deads.length + " 只）" : "");
    }

    /* 得分列三态（2026-11-05）：
         1  越大越好 —— 老行为，副标题只写「来自哪个工具」
        -1  越小越好 —— 分数与持有收益反向，名次要反过来读
         0  仅标签   —— 分数没有排序能力，名单按代码顺序给，**不能让人以为有名次**
       后端给 score_dir / score_note，前端不自己判断方向（判断两处会漂移）。
       score_dir 缺失（后端还是旧进程）时按 1 处理，页面不许因此变成空白。 */
    var sdir = (s && s.score_dir === 0) ? 0 : ((s && s.score_dir === -1) ? -1 : 1);
    var scTitle = sdir === 0
      ? "这个工具的分数没有排序能力：按分数排等于按噪声排，所以名单改用代码顺序"
      : (sdir === -1
        ? "这个工具的分数与持有期收益反向：分数小的排前面"
        : "不同工具的分数不是同一把尺子，换一条筛道就会换量纲");
    var sub = s.score_src ? ('来自「' + e(s.score_src) + "」" + (sdir === -1 ? " · 越小越好，小的排前面" : "")) : "";
    var th = "<th>名称</th><th>代码</th>"
      + '<th class="num" title="' + e(scTitle) + '">' + (sdir === 0 ? "标签" : "得分")
      + (sub ? '<div class="tbx-thSub">' + sub + "</div>" : "")
      + (sdir === 0 ? '<div class="tbx-thSub">不参与排序（按代码顺序）</div>' : "")
      + "</th>";
    steps.forEach(function (_, i) { th += '<th class="tc" title="第 ' + (i + 1) + ' 刀">' + (i + 1) + "</th>"; });
    th += "<th>通过 / 淘汰原因</th>";

    var trs = shown.map(function (x) {
      // 2026-11-05 王总反馈「这里股票点不开」：整行可点，打开该股完整个股详情
      // （行情 / K 线 / 持仓，走 app.js 的 openStock）。淘汰行同样可点 ——
      // 正好用来查「它为什么死在这一级」，不必回到筛选结论去猜。
      var tds = '<td class="tbx-sym" title="点开看行情 · K 线 · 持仓">' + e(x.name || "") + "</td>"
        + '<td class="mono tbx-sym">' + e(x.code) + "</td>"
        + '<td class="num">' + (x.score ? Number(x.score).toFixed(1) : "—") + "</td>";
      (x.trace || []).forEach(function (v) {
        tds += '<td class="tc">' + (v
          ? '<span class="tbx-dot ok">✓</span>'
          : '<span class="tbx-dot no">✕</span>') + "</td>";
      });
      tds += "<td>" + (x.failed_at >= 0
        ? '<span class="tbx-fail">第 ' + (x.failed_at + 1) + " 级淘汰：" + e(x.reason) + "</span>"
        : '<span class="tbx-pass">' + e(x.marks || "全部通过") + "</span>") + "</td>";
      return "<tr class=\"tbx-row" + (x.failed_at >= 0 ? " tbx-rowDead" : "")
        + '" data-tbx-sym="' + e(x.code) + '">' + tds + "</tr>";
    }).join("");

    var toggle = s.n_failed
      ? '<div class="tbx-toggle"><label><input type="checkbox" data-tbx-act="toggle-dead"'
        + (TB.showDropped ? " checked" : "") + "> 显示被淘汰的 " + fmt(s.n_failed) + " 只"
        + "（看它们死在第几级、为什么死）</label></div>"
      : "";

    return toggle
      + '<div class="tbx-tblWrap" id="tbxTblWrap"><table class="tbx-tbl"><thead><tr>' + th
      + "</tr></thead><tbody>"
      + (trs || '<tr><td colspan="' + (3 + steps.length + 1)
        + '" class="tbx-dim">没有结果 —— 可能某一刀把池子清空了，看上面每一刀的掉量。</td></tr>')
      + "</tbody></table></div>";
  }

  /* ---------- 抽屉 ---------- */
  function paintDrawer() {
    var el = document.getElementById("tbxDrawer");
    var mask = document.getElementById("tbxMask");
    if (!el || !mask) return;
    if (!TB.drawer) {
      el.className = "tbx-drawer";
      mask.className = "tbx-mask";
      el.innerHTML = "";
      return;
    }
    var t = byKey(TB.drawer);
    if (!t) { TB.drawer = null; el.className = "tbx-drawer"; mask.className = "tbx-mask"; return; }
    el.className = "tbx-drawer open";
    mask.className = "tbx-mask open";
    el.innerHTML = drawerHtml(t);
  }

  function nl2br(s) { return e(s || "").replace(/\n/g, "<br>"); }

  /* 抽屉里的「适用持有期」：一个工具可能跨两类（箱体谷底＝筑底布局＋长期埋伏），
     逐类列出来，每类带自己的持有期标注与判据回看尺度 —— 只写一个中文名的话，
     用户看到「箱体谷底：筑底布局」会以为它只能拿 3~6 个月，把长线用法丢了。
     兜底：pillar_meta 拿不到（旧后端）时退回只写中文名，不显示空的持有期。 */
  function pillarCells(t) {
    var keys = t.pillar_list || [];
    if (!keys.length) return "";
    var mb = {};
    (pillarMeta() || []).forEach(function (m) { mb[m.key] = m; });
    var cells = keys.map(function (k) {
      var m = mb[k];
      if (!m) return '<b>' + e((t.pillar_cn || [])[keys.indexOf(k)] || k) + "</b>";
      return '<b>' + e(m.cn) + '</b><span class="tbx-dim"> — ' + e(m.span)
        + ' · 判据回看 ' + e(m.lookback) + "</span>";
    }).join("<br>");
    return cells + (t.pillar_note ? '<div class="tbx-pnote">' + nl2br(t.pillar_note) + "</div>" : "");
  }

  function drawerHtml(t) {
    var dead = t.status === "retired";
    var row = function (lab, val) {
      if (!val) return "";
      return '<div class="tbx-row"><div class="tbx-rowK">' + lab + "</div>"
        + '<div class="tbx-rowV">' + val + "</div></div>";
    };
    var head = '<div class="tbx-dHead">'
      + '<div class="tbx-dTop"><b>' + e(t.name) + "</b>"
      + '<span class="tbx-badge">' + e(KIND[t.kind] || "工具") + "</span>"
      + '<span class="tbx-badge' + (dead ? " dead" : "") + '">' + e(STATUS[t.status] || t.status) + "</span>"
      + '<span class="tbx-dClose" data-tbx-act="close" title="关闭（Esc）">×</span></div>'
      + '<div class="tbx-dMeta">收录 <b>' + e(t.adopted_at || "") + "</b> · 来源 "
      + e(SRC[t.source_kind] || t.source_kind || "")
      + (t.source_url ? ' · <a href="' + e(t.source_url) + '" target="_blank" rel="noopener">原始链接</a>' : "")
      + "</div></div>";

    var deadBox = dead
      ? '<div class="tbx-deadBox"><b>已淘汰</b>'
        + (t.retired_at ? '<span class="tbx-deadAt">' + e(t.retired_at) + "</span>" : "")
        + "<div>" + nl2br(t.retire_reason) + "</div></div>"
      : "";

    var prm = paramsShown(t);
    var params = renderParams(t, prm);

    var acts = dead
      ? '<div class="tbx-dActs"><span class="tbx-dim">已淘汰的工具不能进筛道。'
        + '想再试试就先把它改回「试验」。</span>'
        + '<button class="tbx-btn" data-tbx-act="restore">改回试验</button></div>'
      : '<div class="tbx-dActs">'
        + '<input class="tbx-inp" id="tbxReason" placeholder="淘汰原因（必填，会一直写在卡片上）">'
        + '<button class="tbx-btn danger" data-tbx-act="retire">淘汰这个工具</button></div>';

    return head + '<div class="tbx-dBody">' + deadBox
      + (t.advisory
        ? '<div class="tbx-advBar">说明性标注 · 不参与进出决策 —— '
          + '保留它是因为形态本身值得看；在「先剔再取前 5 只」的口径下，'
          + '胜率与超额都没有可辨识的改善。</div>'
        : "")
      + row("适用持有期", pillarCells(t))
      + row("为什么收它", nl2br(t.why))
      + row("作用", nl2br(t.what))
      + row("实测表现", t.evidence ? nl2br(t.evidence) : '<span class="tbx-dim">还没跑出可信数据</span>')
      + row("短板", t.caveat ? nl2br(t.caveat) : "")
      + row("最近一次运行", renderRecent(t))
      + params + acts
      + (t.source_kind === "github"
        ? '<div class="tbx-basis">' + e(BASIS_NOTE) + "</div>" : "")
      + "</div>";
  }

  function renderRecent(t) {
    var rs = t.recent || [];
    var st = null;
    for (var i = 0; i < TB.chain.length; i++) {
      if (TB.chain[i].tool === t.tool_key) st = stepOf(i);
    }
    var out = "";
    if (st && st.ms) {
      /* 「63.6 秒」是这份缓存**当初算出来**花了多久，不是这一次点击花的钱。
         以前写成「这一次：5,264 → 699 · 63.6 秒」，点下去 40 毫秒就出结果也这么写，
         会让人以为刚才等了 63 秒（QA P1-4）。反正两个数都要给，就明确分开说。 */
      var fresh = (st.source === "fresh");
      out += '<span class="tbx-run">本次：' + fmt(st.in) + " → " + fmt(st.out) + " · "
        + (fresh ? ("现算 " + fmtMs(st.ms))
          : ("缓存命中（原始计算 " + fmtMs(st.ms) + "）")) + "</span> ";
    }
    if (!rs.length) return out || '<span class="tbx-dim">本机还没跑过</span>';
    return out + rs.slice(0, 3).map(function (r) {
      return '<span class="tbx-run">' + e(String(r.run_at || "").slice(5))
        + " · " + fmt(r.n_in) + "→" + fmt(r.n_out) + " · " + fmtMs(r.ms) + "</span>";
    }).join(" ");
  }

  function fmtMs(ms) {
    if (ms == null) return "—";
    return Number(ms) >= 1000 ? (Number(ms) / 1000).toFixed(1) + " 秒" : fmt(ms) + " 毫秒";
  }

  /* 数字输入框该显示几位小数 —— 从参数声明的 step 推。
     为什么要专门处理（QA P2-4）：「箱体位置上限」的默认值是 1.0，但浏览器只有一种数字类型，
     JSON 里的 1.0 进了 JS 就是 1，直接塞进 value 会显示成「1」，跟「上限 1.0」的心智不符。
     step 是 0.05 就显示两位有效小数，1.00 收成 1.0，0.85 还是 0.85。 */
  function stepDecimals(step) {
    if (step == null) return 0;
    var s = String(step);
    var i = s.indexOf(".");
    return i < 0 ? 0 : (s.length - i - 1);
  }
  function numText(v, p) {
    if (v == null || v === "") return "";
    var n = Number(v);
    if (isNaN(n)) return String(v);
    var d = stepDecimals(p && p.step);
    if (d <= 0) return String(n);
    var s = n.toFixed(d).replace(/0+$/, "");
    if (s.charAt(s.length - 1) === ".") s += "0";
    return s;
  }

  function renderParams(t, prm) {
    var sc = t.params_schema || [];
    if (!sc.length) return "";
    var out = ['<div class="tbx-paramBox"><div class="tbx-paramH">参数'
      + '<span class="tbx-dim">（改了会按新参数重算这一刀，其它刀不受影响）</span></div>'
      + '<div class="tbx-paramRow">'];
    sc.forEach(function (p) {
      var v = prm[p.key];
      var attrs = ' data-tbx-ptool="' + e(t.tool_key) + '" data-tbx-pkey="' + e(p.key) + '"';
      var ctl;
      if (p.options) {
        ctl = "<select" + attrs + ">" + p.options.map(function (o) {
          return '<option value="' + e(o) + '"' + (String(o) === String(v) ? " selected" : "") + ">"
            + e(o) + "</option>";
        }).join("") + "</select>";
      } else if (p.type === "bool") {
        ctl = '<input type="checkbox"' + attrs + (Number(v) ? " checked" : "") + ">";
      } else if (p.type === "text") {
        ctl = '<input type="text" value="' + e(v) + '"' + attrs + ">";
      } else {
        ctl = '<input type="number" value="' + e(numText(v, p)) + '"' + attrs
          + (p.min != null ? ' min="' + p.min + '"' : "")
          + (p.max != null ? ' max="' + p.max + '"' : "")
          + (p.step != null ? ' step="' + p.step + '"' : "") + ">";
      }
      /* A 股建议值（2026-11-04，三期开源规则带的字段）：
         卡片上显示的**默认值**一律是「原作参数」（美股 / 仓库原始口径），
         这个提示才是我们推断的 A 股刻度。没有 suggest 字段的参数不渲染任何东西，
         所以原有的 22 个工具的抽屉一个字都不会变。 */
      var sugg = (p.suggest == null) ? ""
        : '<em class="tbx-dim tbx-sugg">A股建议 ' + e(p.suggest) + "</em>";
      out.push('<label class="tbx-param"><span>' + e(p.label || p.key) + "</span>" + ctl
        + sugg + "</label>");
    });
    out.push("</div></div>");
    return out.join("");
  }

  /* ======================= 交互 ======================= */
  TB.toggle = function (k) {
    var t = byKey(k);
    if (!t) return;
    if (t.status === "retired") { TB.openDrawer(k); return; }   // 已淘汰：点它不筛选，先给原因
    var i = chainIdx(k);
    if (i >= 0) { TB.chain.splice(i, 1); TB.refreshStack(true); return; }
    /* 加刀之前先看一眼这一刀有多狠（2026-11-05，team-lead 截图发现）：
       卡片上写着「679 → 会清空」时手一抖就点下去了 —— 池子瞬间归零，看起来像程序坏了。
       会清空的问一句，几乎清空的提示一句。
       只在「参数没改过」时判断：改过参数时 counted 里那个数还是默认参数算出来的，
       拿它去问用户会问错。 */
    if (!hasOverride(t)) {
      var cur = currentPool();
      var v = TB.counted[k];
      if (typeof v === "number" && cur > 0 && v === 0) {
        if (!window.confirm("这一刀会把 " + fmt(cur) + " 只全部筛掉，确定要看空池吗？")) return;
      } else if (typeof v === "number" && v > 0 && v <= 5 && cur > 20) {
        toast("提醒：这一刀只剩 " + fmt(v) + " 只（几乎清空），可以先点开看看它筛的是什么");
      }
    }
    TB.chain.push({ tool: k, params: paramsFor(t) });
    TB.refreshStack(true);
  };

  TB.cutAt = function (i) {
    if (isNaN(i) || i < 0 || i >= TB.chain.length) return;
    TB.chain.splice(i, 1);
    TB.refreshStack(true);
  };

  TB.clear = function () {
    TB.chain = [];
    TB.refreshStack(true);
  };

  TB.addSuggest = function () {
    var sg = suggest();
    if (!sg) return;
    var t = byKey(sg.key);
    if (!t) return;
    if (chainIdx(sg.key) < 0) TB.chain.push({ tool: sg.key, params: paramsFor(t) });
    TB.refreshStack(true);
  };

  TB.openDrawer = function (k) { TB.drawer = k; paintDrawer(); };
  TB.closeDrawer = function () { TB.drawer = null; paintDrawer(); };

  TB.toggleDead = function (v) {
    TB.showDropped = !!v;
    paintRes();
  };

  TB.setParam = function (k, pk, val) {
    if (!TB.overrides[k]) TB.overrides[k] = {};
    TB.overrides[k][pk] = val;
    var t = byKey(k);
    var i = chainIdx(k);
    if (i >= 0) TB.chain[i].params = paramsFor(t);
    toast("参数已改，正在按新参数重算这一刀（其它刀的数字不变）");
    TB.refreshStack(true);
  };

  TB.retire = function () {
    var k = TB.drawer;
    if (!k) return;
    var el = document.getElementById("tbxReason");
    var reason = el ? String(el.value || "").trim() : "";
    if (!reason) {
      toast("淘汰必须写原因 —— 半年后靠它回忆为什么砍的");
      if (el) el.focus();
      return;
    }
    setStatus(k, "retired", reason);
  };

  TB.restore = function () {
    var k = TB.drawer;
    if (!k) return;
    setStatus(k, "experiment", "");
  };

  function setStatus(k, st, reason) {
    api("/api/toolbox/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool_key: k, status: st, reason: reason })
    }).then(function (d) {
      if (!d || !d.ok) { toast((d && d.error) || "没能改成功"); return; }
      toast(st === "retired" ? "已淘汰，原因已记下" : "已改回试验，可以再试了");
      // 淘汰后这一刀不能再留在筛道里，否则下次求交会报「已淘汰」
      if (st === "retired") {
        var i = chainIdx(k);
        if (i >= 0) TB.chain.splice(i, 1);
      }
      TB.drawer = null;
      paintDrawer();
      TB.load(true);
    }).catch(function (err) { toast("没能改成功：" + errText(err)); });
  }

  /* ======================= 求交（唯一的重活入口，但只读缓存） ======================= */
  TB.refreshStack = function (userAction) {
    var my = ++TB._seq;
    if (userAction) TB._pollStart = 0;
    TB.busy = true;
    var wall = document.getElementById("tbxWall");
    if (wall) wall.className = "tbx-wall tbx-busy";
    var url = "/api/toolbox/stack?limit=" + LIMIT
      + "&tools=" + encodeURIComponent(JSON.stringify(spec()));
    api(url).then(function (d) {
      if (my !== TB._seq) return;                 // 有更新的请求在飞，丢掉这次（防乱序覆盖）
      TB.busy = false;
      if (!d || d.ok === false) {
        TB.err = "筛台读取失败：" + ((d && d.error) || "空返回");
        TB.paint();
        return;
      }
      TB.stack = d;
      TB.counted = d.counted || {};
      TB.err = "";
      TB.paint();
      TB.schedulePoll();
    }).catch(function (err) {
      if (my !== TB._seq) return;
      TB.busy = false;
      // 只加一行红字，**不**清空筛道 —— 把页面清空等于把用户的思路也清空了
      if (err && err.status === 404) {
        // 静态资源是新的、后端还是旧进程：这份代码上线要重启一次服务才生效。
        // 说清楚比让人对着「读取失败」猜半小时强。
        TB.err = "筛台的后端还没生效（当前服务是旧版本）。把启动窗口关掉、"
          + "重新双击 start_viewer.bat 就好，页面不用改。";
      } else {
        TB.err = "筛台读取失败：" + errText(err) + "（筛道保持不变，可以点重试）";
      }
      TB.paint();
    });
  };

  function needsPoll() {
    var s = TB.stack;
    if (!s) return false;
    if (s.pending) return true;
    var errs = s.errors || {};
    for (var k in TB.counted) {
      if (!Object.prototype.hasOwnProperty.call(TB.counted, k)) continue;
      if (TB.counted[k] == null && !errs[k]) return true;
    }
    return false;
  }

  TB.schedulePoll = function () {
    if (TB._poll) { clearTimeout(TB._poll); TB._poll = null; }
    if (!needsPoll()) { TB._pollStart = 0; return; }
    if (!TB._pollStart) TB._pollStart = Date.now();
    if (Date.now() - TB._pollStart > POLL_MAX) {
      TB._pollStart = 0;
      toast("后台还在算（第一次要把全市场扫一遍，一个工具 6~12 秒，串着算），"
        + "算完这一页会自动填上；你可以先看别的。");
      return;
    }
    TB._poll = setTimeout(function () {
      TB._poll = null;
      TB.refreshStack(false);
    }, POLL_MS);
  };
})();
