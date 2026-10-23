/* K12 自适应学习引擎 · 前端（无框架，SVG 图谱渲染） */
"use strict";

const $ = (id) => document.getElementById(id);
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
};
const post = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

const state = {
  groups: [], band: null, subject: null,
  student: null, graph: null,
  user: null,             // 当前登录用户 {id, username, is_guest, student_id}
  viewMode: "chain",      // "chain"(知识链，默认) | "map"(全景依赖图)
  selected: null, rootCauses: new Set(), planNodes: new Set(),
  pos: {}, view: null,
  kpDetail: null, figure: null, figureBusy: false,
  scope: "all",            // all | depth3 | depth5 | depth8 | focus
  focusRoot: null,         // 聚焦模式的根节点
  cfg: { mastered_threshold: 0.85, low_confidence_hint: 0.6 },
  quiz: null,              // 当前题目 {kp_id, seed, options, answered}
};
let loadSeq = 0;   // 请求序号：丢弃过期响应，避免竞态

function showError(msg) {
  const el = $("errorBanner");
  el.textContent = "⚠ " + msg;
  el.classList.remove("hidden");
  clearTimeout(showError._t);
  showError._t = setTimeout(() => el.classList.add("hidden"), 6000);
}

/* ---------------- 初始化 ---------------- */

/* 把学生列表写入下拉框并定当前学生：
   优先登录用户绑定的档案，其次自己的档案，再退回演示/未归属档案。 */
function setStateStudents(students) {
  const mine = students.filter(s => s.mine);
  const pickId = (state.user && state.user.student_id) || (mine[0] && mine[0].id) ||
                 (students[0] && students[0].id) || null;
  fillSelect($("studentSel"), students.map(s => s.id),
             students.map(s =>
               `${s.name} · ${s.model.toUpperCase()}${s.is_demo ? " · 演示" : ""}`));
  state.student = pickId;
  if (pickId) $("studentSel").value = pickId;
}

/* ---------------- 认证（登录/注册/游客） ---------------- */

async function refreshAuth() {
  try {
    const r = await api("/api/auth/me");
    state.user = r.user;
  } catch (_) { state.user = null; }
  renderAuthBar();
}

function renderAuthBar() {
  const who = $("authWho"), loginBtn = $("loginBtn"), llmBtn = $("llmBtn");
  if (state.user) {
    who.classList.remove("hidden");
    who.textContent = state.user.is_guest
      ? "游客模式 · 数据不长期保留"
      : state.user.username;
    who.title = state.user.is_guest ? "注册后数据可长期保存" : "";
    loginBtn.classList.add("hidden");
    $("logoutBtn").classList.remove("hidden");
    // LLM 设置是全局配置：注册用户可见，游客隐藏
    llmBtn.classList.toggle("hidden", !!state.user.is_guest);
  } else {
    who.classList.add("hidden");
    loginBtn.classList.remove("hidden");
    $("logoutBtn").classList.add("hidden");
    llmBtn.classList.add("hidden");
  }
}

let authMode = "login";
function openAuth(mode = "login", notice = "") {
  authMode = mode;
  $("authTitle").textContent = mode === "login" ? "登录" : "注册新账号";
  $("tabLogin").classList.toggle("on", mode === "login");
  $("tabRegister").classList.toggle("on", mode === "register");
  $("authSubmit").textContent = mode === "login" ? "登录" : "注册";
  $("authErr").classList.toggle("hidden", !notice);
  $("authErr").textContent = notice || "";
  $("authUser").value = ""; $("authPass").value = "";
  $("authModal").classList.remove("hidden");
  $("authUser").focus();
}
function closeAuth() { $("authModal").classList.add("hidden"); }

async function authSubmit() {
  const username = $("authUser").value.trim();
  const password = $("authPass").value;
  try {
    const path = authMode === "login" ? "/api/auth/login" : "/api/auth/register";
    const r = await post(path, { username, password });
    state.user = r.user;
    closeAuth();
    await reloadSession();
    showError(`欢迎，${state.user.username}！已切换到你的学习档案。`);
  } catch (e) {
    $("authErr").classList.remove("hidden");
    $("authErr").textContent = e.message;
  }
}

async function authGuest() {
  try {
    const r = await post("/api/auth/guest", {});
    state.user = r.user;
    closeAuth();
    await reloadSession();
  } catch (e) {
    $("authErr").classList.remove("hidden");
    $("authErr").textContent = e.message;
  }
}

/* 登录/注册/游客成功后：刷新用户与学生列表，切到自己的档案 */
/* 登录/注册/游客成功后：刷新用户与学生列表，切到自己的档案 */
async function reloadSession() {
  renderAuthBar();
  const students = await api("/api/students");
  const mine = students.filter(s => s.mine);
  const wanted = (state.user && state.user.student_id) || (mine[0] && mine[0].id);
  if (wanted && students.some(s => s.id === wanted)) {
    state.student = wanted; $("studentSel").value = wanted;
  } else {
    setStateStudents(students);
  }
  state.selected = null;
  if (state.viewMode === "map") await loadGraph(); else await enterChainView();
}

/* ---------------- LLM 出题设置面板 ----------------
   三级信息源：服务端下发预设(provider/base_url/model)→ 面板可直接套用；
   key 永不回传(只显示末4位)；保存热生效并写回 .env，重启不变。 */

const llmProviderLabels = {
  go: "OpenCode Go", deepseek: "DeepSeek", dashscope: "通义百炼",
  zhipu: "智谱", opencode: "OpenCode 标准", ollama: "Ollama 本地",
};
let llmProviders = [];

function llmStatusLine(s) {
  if (!s) return "";
  return s.enabled
    ? `<span class="ok">已启用</span> · 模型 <b>${esc(s.model)}</b>`
    : `<span class="dim">未启用（当前走 📝 模板兜底）</span>`;
}

function renderLlmResult(text, ok) {
  const box = $("llmResult");
  box.classList.remove("hidden");
  box.style.color = ok === undefined ? "inherit" : (ok ? "#1d7a46" : "#c0392b");
  box.textContent = text;
}

async function openLlm() {
  try {
    const s = await api("/api/llm");
    llmProviders = s.providers || [];
    const sel = $("llmPreset");
    sel.innerHTML = "";
    sel.add(new Option("预设服务商（选一个自动填好下面两项）", ""));
    llmProviders.forEach((p, i) =>
      sel.add(new Option(`${llmProviderLabels[p.key] || p.key} · ${p.model}`, p.key), i));
    $("llmBase").value = s.base_url;
    $("llmModel").value = s.model;
    $("llmMaxTokens").value = s.max_tokens || "";
    $("llmKey").placeholder = s.key_set
      ? `已配置（末4位 ${s.key_hint}）· 留空不修改`
      : "sk-...";
    $("llmKey").value = "";
    $("llmStatus").innerHTML = llmStatusLine(s);
    renderLlmResult("");
    $("llmModal").classList.remove("hidden");
  } catch (e) { showError(e.message); }
}

function closeLlm() { $("llmModal").classList.add("hidden"); }

function llmPayload() {
  const mt = parseInt($("llmMaxTokens").value, 10);
  return {
    base_url: $("llmBase").value.trim(),
    model: $("llmModel").value.trim(),
    api_key: $("llmKey").value.trim(),     // 留空 = 沿用现有 key
    max_tokens: Number.isFinite(mt) && mt > 0 ? mt : undefined,
  };
}

async function llmTest() {
  const p = llmPayload();
  if (!p.base_url || !p.model) { renderLlmResult("先填 接口地址 和 模型名", false); return; }
  const btn = $("llmTest");
  btn.disabled = true; btn.textContent = "测试中…";
  renderLlmResult("测试中（免费/推理模型可能要十~几十秒）…");
  try {
    const r = await post("/api/llm/test", p);
    if (r.ok) {
      renderLlmResult(`✓ ${r.latency_ms}ms · ${r.model} 回复：「${r.reply}」`, true);
    } else {
      renderLlmResult(`✗ ${String(r.error).slice(0, 160)}`, false);
    }
  } catch (e) {
    renderLlmResult(`✗ ${e.message}`, false);
  } finally {
    btn.disabled = false; btn.textContent = "测试连通性";
  }
}

async function llmSave(disable = false) {
  const btn = disable ? $("llmDisable") : $("llmSave");
  if (!disable && (!$("llmBase").value.trim() || !$("llmModel").value.trim())) {
    renderLlmResult("启用需要 接口地址 + API Key + 模型名 三项齐备", false); return;
  }
  btn.disabled = true;
  try {
    const body = disable ? { base_url: "", model: "", api_key: "" } : llmPayload();
    const r = await post("/api/llm/save", body);
    $("llmStatus").innerHTML = llmStatusLine(r);
    $("llmKey").value = "";
    $("llmKey").placeholder = r.key_set
      ? `已配置（末4位 ${r.key_hint}）· 留空不修改`
      : "sk-...";
    renderLlmResult(disable
      ? "已停用 AI 出题，恢复模板兜底；想再开启直接选预设+填模型即可。"
      : "已保存并热生效，重启服务也保持。", true);
  } catch (e) {
    renderLlmResult(`✗ ${e.message}`, false);
  } finally {
    btn.disabled = false;
  }
}

async function init() {
  await refreshAuth();                       // 会话：谁在登录（含游客）
  state.groups = await api("/api/groups");
  // 阈值由后端下发，避免前后端各写一份魔法数字
  try { state.cfg = await api("/api/config"); } catch (_) { /* 保留默认值 */ }
  const bands = [...new Set(state.groups.map(g => g.band))];
  fillSelect($("bandSel"), bands);
  state.band = bands.includes("初中") ? "初中" : bands[0];
  $("bandSel").value = state.band;
  refreshSubjectOptions();

  const students = await api("/api/students");
  setStateStudents(students);

  $("bandSel").onchange = () => {
    closeFigure();
    state.band = $("bandSel").value;
    resetFocus();
    refreshSubjectOptions();
    reloadView();
  };
  $("subjectSel").onchange = () => { closeFigure(); state.subject = $("subjectSel").value; resetFocus(); reloadView(); };
  $("studentSel").onchange = () => { closeFigure(); state.student = $("studentSel").value; reloadView(); };
  $("newStudentBtn").onclick = async () => {
    const name = prompt("学生姓名：", "学生" + (Date.now() % 1000));
    if (!name) return;
    const model = (prompt("掌握度模型 (bkt/elo)：", "bkt") || "bkt").trim();
    try {
      const s = await post("/api/students", { name, model: ["bkt", "elo"].includes(model) ? model : "bkt" });
      const sel = $("studentSel");
      sel.add(new Option(`${s.name} · ${s.model.toUpperCase()}`, s.id));
      sel.value = s.id; state.student = s.id;
      reloadView();
    } catch (e) {
      if (/401|登录/.test(e.message)) openAuth("login", "创建学生前请先登录（或用游客模式）。");
      else showError(e.message);
    }
  };

  $("loginBtn").onclick = () => openAuth("login");
  $("tabLogin").onclick = () => openAuth("login");
  $("tabRegister").onclick = () => openAuth("register");
  $("authSubmit").onclick = authSubmit;
  $("authGuest").onclick = authGuest;
  $("authClose").onclick = closeAuth;
  $("authModal").addEventListener("click", (e) => {
    if (e.target === $("authModal")) closeAuth();
  });
  $("authPass").addEventListener("keydown", (e) => { if (e.key === "Enter") authSubmit(); });
  $("authUser").addEventListener("keydown", (e) => { if (e.key === "Enter") $("authPass").focus(); });
  $("logoutBtn").onclick = async () => {
    await post("/api/auth/logout", {}).catch(() => {});
    location.reload();
  };

  /* ---- LLM 设置面板绑定 ---- */
  $("llmBtn").onclick = openLlm;
  $("llmClose").onclick = closeLlm;
  $("llmModal").addEventListener("click", (e) => {
    if (e.target === $("llmModal")) closeLlm();
  });
  $("llmPreset").onchange = (e) => {
    const p = llmProviders.find(p => p.key === e.target.value);
    if (p) { $("llmBase").value = p.base_url; $("llmModel").value = p.model; }
  };
  $("llmTest").onclick = llmTest;
  $("llmSave").onclick = () => llmSave();
  $("llmDisable").onclick = () => llmSave(true);

  $("showMastered").onchange = renderGraph;
  $("fitBtn").onclick = () => fitView(true);
  $("scopeSel").onchange = async (e) => {
    state.scope = e.target.value;
    if (state.scope !== "focus") state.focusRoot = null;
    $("clearScopeBtn").classList.toggle("hidden", state.scope !== "focus");
    if (state.scope === "focus") {
      if (!state.selected) { showError("先在图里选一个知识点再聚焦"); state.scope = "all"; e.target.value = "all"; $("clearScopeBtn").classList.add("hidden"); return; }
      state.focusRoot = state.selected;
    }
    await loadGraph();
  };
  $("clearScopeBtn").onclick = async () => {
    state.scope = "all"; state.focusRoot = null;
    $("scopeSel").value = "all";
    $("clearScopeBtn").classList.add("hidden");
    await loadGraph();
  };
  setupSearch();
  await enterChainView();                    // 默认知识链；全景图由切换钮进入
}

function fillSelect(sel, values, labels) {
  sel.innerHTML = "";
  values.forEach((v, i) => sel.add(new Option(labels ? labels[i] : v, v)));
}

/* 只负责填科目下拉，不发起请求（避免竞态） */
function refreshSubjectOptions() {
  const subs = state.groups.filter(g => g.band === state.band);
  fillSelect($("subjectSel"), subs.map(s => s.subject));
  if (!subs.some(s => s.subject === state.subject)) {
    state.subject = subs.some(s => s.subject === "数学") ? "数学" : subs[0]?.subject;
  }
  $("subjectSel").value = state.subject;
}

/* 换学段/科目后旧的聚焦根节点不再属于当前图，退回"全部"范围 */
function resetFocus() {
  if (state.scope !== "focus") return;
  state.scope = "all"; state.focusRoot = null;
  $("scopeSel").value = "all";
  $("clearScopeBtn").classList.add("hidden");
}

/* ---------------- 图谱加载 ---------------- */

/* 把当前范围设置翻译成 /api/graph 的查询参数。
   大科目（小学语文 4851 节点）整图会拉 1.4 MB 并生成上万个 SVG 元素，
   所以按层级或聚焦收窄，由服务端裁剪。 */
function graphQuery() {
  const q = new URLSearchParams();
  if (state.student) q.set("student", state.student);
  const depthByMode = { depth3: 3, depth5: 5, depth8: 8 };
  if (depthByMode[state.scope] !== undefined) {
    q.set("depth", String(depthByMode[state.scope]));
  } else if (state.scope === "spine") {
    q.set("spine", "true");
  } else if (state.scope === "focus" && state.focusRoot) {
    q.set("root", state.focusRoot);
  }
  return q.toString();
}

async function loadGraph() {
  if (state.viewMode !== "map") return;            // 知识链模式下不拉大图
  if (!state.subject) return;
  if (!state.student) { showError("尚未选择学生"); return;   // 防止 student=null 请求
    }
  const seq = ++loadSeq;
  try {
    const g = await api(`/api/graph/${encodeURIComponent(state.band)}/${encodeURIComponent(state.subject)}?${graphQuery()}`);
    if (seq !== loadSeq) return;                       // 过期响应直接丢弃
    state.graph = g;
    // 切换科目后原选中节点可能已不在图中
    if (state.selected && !g.nodes.some(n => n.id === state.selected)) state.selected = null;
    state.rootCauses.clear(); state.planNodes.clear();
    state.view = null;                                  // 新图重新适配视口
    const s = g.stats;
    const shown = g.nodes.length, edges = g.edges.length;
    const cropped = g.truncated
      ? `（共 ${g.total_nodes} 个知识点，已按范围裁剪）` : "";
    $("graphStats").textContent =
      `${state.band}/${state.subject} · ${shown} 知识点 · ${edges} 关系 · 最深 ${s.max_depth} 层` +
      cropped + (s.cycles.length ? ` · ⚠ ${s.cycles.length} 处循环依赖` : "");
    renderGraph();
    fitView(true);
    if (state.selected) selectKp(state.selected);
  } catch (e) {
    if (seq !== loadSeq) return;
    showError(`加载 ${state.band}/${state.subject} 失败：${e.message}`);
  }
}

function masteryColor(node) {
  if (!node.attempts) return "#95a5a6";        // 未作答
  if (node.p >= state.cfg.mastered_threshold) return "#27ae60";
  if (node.p >= 0.5) return "#f39c12";
  return "#e74c3c";
}

/* ---------------- 视图模式：知识链（默认）/ 全景图 ---------------- */

/* 全景图把一个大科目画成一团线球。默认视图只铺「当前知识点的先修链」
   ——卡片列表逐张浮现，读得懂；全景依赖图留给需要全科大图的人（切换钮）。 */
function reloadView() {
  return state.viewMode === "map" ? loadGraph() : enterChainView();
}

async function enterChainView() {
  state.viewMode = "chain";
  document.body.classList.remove("mode-map");
  $("viewToggle").textContent = "🗺 全景图";
  $("viewToggle").title = "切换到全景依赖图";
  $("graphWrap").classList.add("hidden");
  $("chainWrap").classList.remove("hidden");
  $("graphStats").textContent =
    `${state.band}/${state.subject} · 知识链视图（全景图请点右上角）`;
  await loadChain(state.selected);
}

function enterMapView() {
  state.viewMode = "map";
  document.body.classList.add("mode-map");
  $("viewToggle").textContent = "⛓ 知识链";
  $("viewToggle").title = "切换回知识链视图";
  $("chainWrap").classList.add("hidden");
  $("graphWrap").classList.remove("hidden");
  loadGraph().then(() => { if (state.selected) centerOn(state.selected); });
}

$("viewToggle").onclick = () => {
  if (state.viewMode === "chain") enterMapView();
  else enterChainView();
};

/* ---------------- 知识链视图 ---------------- */

async function loadChain(kpId) {
  const wrap = $("chainWrap");
  if (!state.student) {
    wrap.innerHTML = `
      <div class="chain-empty card">
        <h3>开始之前</h3>
        <p>还没有可选的学习档案。<button id="chainGoAuth" class="primary">登录 / 注册</button>
        或<button id="chainGoGuest">以游客身份体验</button></p>
      </div>`;
    const go = $("chainGoAuth"); if (go) go.onclick = () => openAuth("login");
    const g2 = $("chainGoGuest"); if (g2) g2.onclick = authGuest;
    return;
  }
  if (!kpId) { await renderRecommendations(wrap); return; }
  try {
    const d = await api(`/api/kp/${encodeURIComponent(kpId)}/chain?student=${encodeURIComponent(state.student)}`);
    renderChain(d);
  } catch (e) {
    wrap.innerHTML = `<div class="chain-empty card">知识链加载失败：${esc(e.message)}</div>`;
  }
}

/* 未选中知识点：给「推荐下一步」和搜索提示，避免一整面空白 */
async function renderRecommendations(wrap) {
  let recs = [];
  try {
    const r = await api(`/api/students/${encodeURIComponent(state.student)}/recommend?top_n=5`);
    recs = r.recommendations || [];
  } catch (_) { /* 未登录/无档案时推荐端点可能 4xx，静默退化 */ }
  wrap.innerHTML = `
    <div class="chain-intro card">
      <h3>知识链</h3>
      <p class="tiny">在左侧选一个知识点（或直接搜索），这里会沿着它的<b>先修链</b>从根到叶
      逐张卡片浮现，标出你当前位置和掌握度；不再需要在一团线球里找路。</p>
    </div>
    <div class="chain-head">推荐下一步</div>
    <div class="chain-list">
      ${recs.map((n, i) => chainCard({
        id: n.kp_id, name: n.name, p_mastery: n.p_mastery, level: n.p_mastery > 0 ? "learning" : "new",
        delay: i,
      }, { tail: `<div class="chain-why tiny">${esc(n.rationale || "")}</div>` })).join("") ||
        `<div class="chain-empty tiny">没有推荐（先选学段/科目）。</div>`}
    </div>`;
  bindChainCards(wrap);
}

function chainCard(n, opts = {}) {
  const pct = n.p_mastery != null ? `${(n.p_mastery * 100).toFixed(0)}%` : "未开始";
  const lv = n.level || "new";
  const lvTxt = { mastered: "已掌握", learning: "学习中", new: "未作答" }[lv] || lv;
  return `
    <div class="chain-card lv-${lv}" data-kp="${esc(n.id)}" style="animation-delay:${(opts.delay || 0) * 90}ms">
      <div class="chain-card-main">
        <span class="chain-name">${esc(n.name)}</span>
        <span class="chain-pct lv-${lv}">${pct} · ${lvTxt}</span>
      </div>
      ${opts.tail || ""}
    </div>`;
}

function renderChain(d) {
  const wrap = $("chainWrap");
  const chain = d.chain || [];
  const leafIdx = chain.length - 1;
  const cards = chain.map((n, i) => chainCard(n, {
    delay: i,
    arrow: i < leafIdx,
    tail: i === leafIdx
      ? `<div class="chain-why tiny">↑ 当前知识点：先修链自上而下依次铺垫</div>` : "",
  }));
  const deps = (d.dependents || []).map((n, i) =>
    `<button class="dep-chip" data-kp="${esc(n.id)}" style="animation-delay:${i * 60}ms">${esc(n.name)}</button>`);
  wrap.innerHTML = `
    <div class="chain-head">${esc(d.band)} · ${esc(d.subject)} · 先修链（根 → 当前）</div>
    <div class="chain-list">${cards.join("")}</div>
    <div class="chain-head ${deps.length ? "" : "hidden"}">它的下游（学完它才能学）</div>
    <div class="chain-deps">${deps.join("")}</div>`;
  bindChainCards(wrap);
}

function bindChainCards(wrap) {
  wrap.querySelectorAll("[data-kp]").forEach(el => {
    el.onclick = () => selectKp(el.dataset.kp);
  });
}

/* ---------------- 渲染 ---------------- */

function renderGraph() {
  if (state.viewMode === "chain") return;       // 当前视图不是全景图
  const svg = $("svg");
  svg.innerHTML = "";
  if (!state.graph) return;
  const W = svg.clientWidth || 900, H = svg.clientHeight || 600;
  const colorOn = $("showMastered").checked;

  // 按拓扑深度分列（先修在左，后继在右）。
  // 单列节点过多时按 MAX_ROWS 折成多个子列，避免出现几万像素高的细线。
  const byDepth = {};
  state.graph.nodes.forEach(n => (byDepth[n.depth] ||= []).push(n));
  const depths = Object.keys(byDepth).map(Number).sort((a, b) => a - b);
  const MAX_ROWS = 40;
  const rowH = 38, subW = 72, colW = 150;

  const pos = {};
  let cursorX = 80;
  depths.forEach(d => {
    const col = byDepth[d].sort((a, b) => (b.p ?? -1) - (a.p ?? -1));
    const nSub = Math.ceil(col.length / MAX_ROWS);
    col.forEach((n, i) => {
      const sc = Math.floor(i / MAX_ROWS), ri = i % MAX_ROWS;
      pos[n.id] = { x: cursorX + sc * subW, y: 70 + ri * rowH };
    });
    cursorX += (nSub - 1) * subW + colW;
  });
  state.pos = pos;

  // 边
  const edgeG = mk("g");
  const edges = state.graph.edges;
  // 边很多时只画主干（跳过 leads-to），否则上万个 path 会把浏览器拖垮
  const drawLeads = edges.length <= 3000;
  edges.forEach(e => {
    if (!drawLeads && e.type === "leads-to") return;
    const a = pos[e.source], b = pos[e.target];
    if (!a || !b) return;
    const path = mk("path");
    const mx = (a.x + b.x) / 2;
    path.setAttribute("d", `M${a.x},${a.y} C${mx},${a.y} ${mx},${b.y} ${b.x},${b.y}`);
    path.setAttribute("class", "edge" + (e.type === "leads-to" ? " leads" : ""));
    edgeG.appendChild(path);
  });
  svg.appendChild(edgeG);

  // 节点
  const nodeG = mk("g");
  const nodes = state.graph.nodes;
  // 节点多时只给"有信息量"的节点画文字标签：已作答 / 选中 / 根因 / 路径内。
  // 每个 <text> 都是一个 DOM 元素，5000 个节点时全画会明显卡顿。
  const sparseLabels = nodes.length > 1200;
  const labelFor = (n) => !sparseLabels || n.attempts ||
    n.id === state.selected || state.rootCauses.has(n.id) || state.planNodes.has(n.id);
  nodes.forEach(n => {
    const p = pos[n.id]; if (!p) return;
    const g = mk("g");
    let cls = "node";
    if (state.rootCauses.has(n.id)) cls += " root-cause";
    if (state.planNodes.has(n.id)) cls += " in-plan";
    if (state.selected === n.id) cls += " selected";
    if (n.low_confidence) cls += " low-conf";
    g.setAttribute("class", cls);
    g.setAttribute("transform", `translate(${p.x},${p.y})`);

    const c = mk("circle");
    c.setAttribute("r", n.id === state.selected ? 14 : 9);
    c.setAttribute("fill", colorOn ? masteryColor(n) : "#b0bec5");
    c.onclick = () => selectKp(n.id);
    g.appendChild(c);

    const title = mk("title");
    title.textContent = n.name +
      (n.attempts ? `（掌握 ${(n.p * 100).toFixed(0)}%）` : "（未作答）") +
      (n.low_confidence ? " · 低置信度：仅按教材目录标题抽取" : "");
    g.appendChild(title);

    if (labelFor(n)) {
      const t = mk("text");
      t.setAttribute("y", -14); t.setAttribute("text-anchor", "middle");
      t.textContent = n.name.length > 9 ? n.name.slice(0, 8) + "…" : n.name;
      g.appendChild(t);
    }
    nodeG.appendChild(g);
  });
  svg.appendChild(nodeG);

  attachViewControls(svg, W, H);
  applyView();
}

function mk(tag) { return document.createElementNS("http://www.w3.org/2000/svg", tag); }

/* ---------------- 视口：适配 / 缩放 / 拖动 ---------------- */

function bounds() {
  const pts = Object.values(state.pos);
  if (!pts.length) return null;
  return {
    minX: Math.min(...pts.map(p => p.x)) - 90,
    maxX: Math.max(...pts.map(p => p.x)) + 110,
    minY: Math.min(...pts.map(p => p.y)) - 50,
    maxY: Math.max(...pts.map(p => p.y)) + 50,
  };
}

function fitView(reset = false) {
  const svg = $("svg");
  const b = bounds();
  if (!b || !svg) return;
  if (reset || !state.view) {
    const W = svg.clientWidth || 900, H = svg.clientHeight || 600;
    const bw = b.maxX - b.minX, bh = b.maxY - b.minY;
    const scale = Math.min(W / bw, H / bh, 1.2);       // 最多放大到 1.2 倍
    const vw = W / scale, vh = H / scale;
    state.view = {
      x: (b.minX + b.maxX) / 2 - vw / 2,
      y: (b.minY + b.maxY) / 2 - vh / 2,
      w: vw, h: vh,
    };
  }
  applyView();
}

function centerOn(kpId, zoom = 1.6) {
  const p = state.pos[kpId];
  const svg = $("svg");
  if (!p || !svg) return;
  const W = svg.clientWidth || 900, H = svg.clientHeight || 600;
  state.view = { x: p.x - W / zoom / 2, y: p.y - H / zoom / 2, w: W / zoom, h: H / zoom };
  applyView(true);
}

function applyView(markCentered = false) {
  const svg = $("svg");
  if (!svg || !state.view) return;
  const v = state.view;
  svg.setAttribute("viewBox", `${v.x} ${v.y} ${v.w} ${v.h}`);
  // 缩得很小时隐藏标签，避免糊成一团
  const W = svg.clientWidth || 900;
  svg.classList.toggle("no-labels", v.w / W > 2.2);
}

function attachViewControls(svg, W, H) {
  svg.onwheel = (e) => {
    e.preventDefault();
    if (!state.view) return;
    const rect = svg.getBoundingClientRect();
    const v = state.view;
    const mx = v.x + (e.clientX - rect.left) / rect.width * v.w;
    const my = v.y + (e.clientY - rect.top) / rect.height * v.h;
    const factor = e.deltaY < 0 ? 0.85 : 1.18;
    const nw = Math.min(20000, Math.max(120, v.w * factor));
    const k = nw / v.w;
    const nh = v.h * k;
    state.view = { x: mx - (mx - v.x) * k, y: my - (my - v.y) * k, w: nw, h: nh };
    applyView();
  };

  let drag = null;
  svg.onmousedown = (e) => { drag = { x: e.clientX, y: e.clientY, v: { ...state.view } }; };
  svg.onmousemove = (e) => {
    if (!drag || !state.view) return;
    const rect = svg.getBoundingClientRect();
    state.view = {
      ...state.view,
      x: drag.v.x - (e.clientX - drag.x) * drag.v.w / rect.width,
      y: drag.v.y - (e.clientY - drag.y) * drag.v.h / rect.height,
    };
    applyView();
  };
  svg.onmouseup = svg.onmouseleave = () => { drag = null; };
}

/* ---------------- 知识点详情 ---------------- */

async function selectKp(kpId) {
  if (state.figure && state.selected !== kpId) closeFigure();
  state.selected = kpId;
  if (state.viewMode === "map") {
    renderGraph();
  }
  try {
    const d = await api(`/api/kp/${kpId}?student=${encodeURIComponent(state.student)}`);
    renderKpCard(d);
    if (state.viewMode === "map") {
      // 只在用户已放大时保持视口，避免每次点击都跳动
      centerOn(kpId);
    } else {
      await loadChain(kpId);
    }
  } catch (e) { showError(e.message); }
}

function renderKpCard(d) {
  $("emptyHint").classList.add("hidden");
  $("kpCard").classList.remove("hidden");
  $("kpName").textContent = d.name;
  $("kpMeta").textContent =
    `${d.grade_band} · ${d.subject} · ${d.grades} · 类型 ${d.kp_type_label} · 难度 ${d.difficulty.toFixed(2)}`;
  state.kpDetail = d;
  const m = d.mastery;
  setMasteryBar(m);

  const slot = $("figureSlot");
  if (slot) {
    if (d.figure) {
      slot.classList.remove("hidden");
      slot.innerHTML = `<div class="figure-teaser">
        <div class="figure-teaser-art">⚡</div>
        <div class="ft-main">
          <b>${esc(d.figure.title || "交互实验台")}</b>
          <span class="tiny">${esc(d.figure.subtitle || d.figure.description || "")}</span>
        </div>
        <button id="openFigureBtn" type="button">打开实验台 →</button>
      </div>`;
      $("openFigureBtn").onclick = () => openFigure(d.figure);
    } else {
      slot.classList.add("hidden");
      slot.innerHTML = "";
    }
  }

  let html = "";
  if (d.confidence < (state.cfg.low_confidence_hint ?? 0.6)) {
    html += `<div class="kv" style="color:#b9770e">⚠ 低置信度条目（${(d.confidence * 100).toFixed(0)}%）：仅按教材目录标题抽取，缺少教材正文，建议人工核对后再用于教学。</div>`;
  }
  if (m && m.mastered) html += `<div class="kv" style="color:var(--green)">✅ 已掌握</div>`;
  if (d.learning_objectives?.length) html += kv("学习目标", d.learning_objectives.map(o => `<li>${esc(o)}</li>`).join(""));
  if (d.misconceptions?.length) html += kv("常见误区", d.misconceptions.map(o => `<li>⚠ ${esc(o)}</li>`).join(""));
  if (d.chain_to_root?.length > 1) {
    html += `<div class="kv"><b>先修依赖链</b><div class="chain">` +
      d.chain_to_root.map(n => `<span class="n">${esc(n)}</span>`).join('<span class="arr">←</span>') +
      `</div></div>`;
  }
  if (d.dependents?.length) html += kv("被这些知识点依赖", d.dependents.slice(0, 8).map(o => `<li>${esc(o)}</li>`).join(""));
  if (d.textbooks?.length) {
    const tb = d.textbooks[0];
    html += `<div class="kv"><b>教材出处</b>${esc(tb.title || "")} ${esc((tb.chapter_path || []).join(" / "))}</div>`;
  }
  $("kpBody").innerHTML = html;
  $("attemptFeedback").innerHTML = "";
  // 换了知识点就换题：清掉上一题的选项和结果
  state.quiz = null;
  $("quizSlot").innerHTML = "";
  $("btnNextQ").classList.add("hidden");
  document.querySelectorAll("[data-close]").forEach(b => b.onclick = () => $(b.dataset.close).classList.add("hidden"));
}

function kv(title, inner) { return `<div class="kv"><b>${title}</b><ul>${inner}</ul></div>`; }
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

/* 题面/解析里的 $...$、\(..\)、\[..\] 渲染成真正的数学公式（KaTeX 本地内置）。
   throwOnError=false：个别写错的公式退回原样显示，不让整题白屏。 */
function renderMath(el) {
  if (el && window.renderMathInElement) {
    renderMathInElement(el, {
      delimiters: [
        { left: "\\[", right: "\\]", display: true },
        { left: "\\(", right: "\\)", display: false },
        { left: "$$", right: "$$", display: true },
        { left: "$", right: "$", display: false },
      ],
      throwOnError: false,
    });
  }
}

/* ---------------- 出题 / 作答 ---------------- */

/* 出一道题。题目由服务端按 (kp_id, seed) 确定性生成，判分也在服务端，
   这里只负责展示和提交选项。 */
async function askQuestion() {
  if (!state.selected) return;
  const slot = $("quizSlot");
  const btn = $("btnAsk");
  btn.disabled = true;
  slot.innerHTML = `<div class="quiz-none quiz-loading"><span class="spinner"></span>` +
    `正在出题… AI 命题约需 10~40 秒，同一题之后秒回</div>`;
  try {
    const seed = Math.floor(Math.random() * 1e6);
    const q = await api(`/api/quiz/${encodeURIComponent(state.selected)}?seed=${seed}`);
    state.quiz = { ...q, answered: false };
    renderQuiz();
    $("btnNextQ").classList.remove("hidden");
  } catch (e) {
    // 该知识点没有可用的题目素材（可测评性过低 / 标注缺失）
    state.quiz = null;
    slot.innerHTML = `<div class="quiz-none">⚠ 该知识点暂无可出题素材（可测评性不足或标注缺失），可用下方自评。</div>`;
    $("btnNextQ").classList.add("hidden");
  } finally {
    btn.disabled = false;
  }
}

function renderQuiz() {
  const slot = $("quizSlot");
  const q = state.quiz;
  if (!q) { slot.innerHTML = ""; return; }
  const letters = "ABCDEFGH";
  const isLLM = q.source === "llm" || q.source === "llm-cache";
  const badge = isLLM
    ? `<span class="src-badge ai" title="由 ${esc(q.model || "AI")} 生成，仅此题可用">🤖 AI 命题</span>`
    : `<span class="src-badge tpl" title="由知识点标注生成（未配置 LLM 时的兜底）">📝 模板示例</span>`;
  slot.innerHTML = `
    <div class="quiz">
      <div class="quiz-kind">${q.kind === "judge" ? "判断正误" : "单项选择"}${badge}</div>
      <div class="quiz-prompt">${esc(q.prompt)}</div>
      <div class="quiz-options">
        ${q.options.map((o, i) => `
          <button class="qopt" data-choice="${i}">
            <span class="qletter">${letters[i]}</span>${esc(o)}
          </button>`).join("")}
      </div>
    </div>`;
  slot.querySelectorAll(".qopt").forEach(b => {
    b.onclick = () => submitAnswer(Number(b.dataset.choice));
  });
  renderMath(slot);
}

async function submitAnswer(choice) {
  const q = state.quiz;
  if (!q || q.answered) return;
  q.answered = true;
  const slot = $("quizSlot");
  slot.querySelectorAll(".qopt").forEach(b => { b.disabled = true; });
  try {
    const r = await post(`/api/students/${encodeURIComponent(state.student)}/answer`, {
      kp_id: q.kp_id, seed: q.seed, choice, qhash: q.qhash,
    });
    showQuizResult(r, q);
    updateMasteryAfterAttempt(r);
    if (state.viewMode === "map") await refreshGraphData(); else await loadChain(q.kp_id);
    sendMasteryToFigure(r.p_after);
  } catch (e) {
    showError(e.message);
    q.answered = false;
    if (/409|刷新/.test(e.message)) {
      // 这题服务端已没有任何判分信息（极少发生：答案库和出题缓存都没了，
      // 基本只出现在服务部署/清理瞬间）。停在原地说明情况，不自动跳题。
      state.quiz = null;
      slot.innerHTML = `<div class="quiz-none">⚠ 这道题的判分信息已失效（多半是服务端刚更新过），` +
        `暂时给不出解析。点「出一题」换一道新题继续。</div>`;
    } else {
      renderQuiz();
    }
  }
}

function showQuizResult(r, q) {
  const slot = $("quizSlot");
  const letters = "ABCDEFGH";
  // 把选项标成 对 / 错
  slot.querySelectorAll(".qopt").forEach(b => {
    const i = Number(b.dataset.choice);
    if (i === r.correct_index) b.classList.add("q-right");
    else if (i === r.choice) b.classList.add("q-wrong");
  });
  const picked = r.choice === r.correct_index
    ? "答对了"
    : `答错了（你选的是 ${letters[r.choice]}）`;
  const lines = [];
  lines.push(`<div class="qr-head ${r.correct ? "ok" : "bad"}">
    ${r.correct ? "✓" : "✗"} ${picked}　掌握度 ${(r.p_before * 100).toFixed(0)}% → ${(r.p_after * 100).toFixed(0)}%
    ${r.mastered ? "　🎉 已掌握" : ""}</div>`);
  lines.push(`<div class="qr-explain">${esc(r.explain).replace(/\n/g, "<br>")}</div>`);
  if (!r.correct && r.misconception) {
    lines.push(`<div class="qr-mis">💡 你很可能持有这个误区：<b>${esc(r.misconception)}</b></div>`);
  }
  if (r.diagnosis?.length) {
    lines.push(`<div class="qr-diag">⚡ 沿先修链回溯，疑似根因：` +
      r.diagnosis.slice(0, 2).map(c => `<b>${esc(c.name)}</b>（掌握 ${(c.p_mastery * 100).toFixed(0)}%）`).join("、") +
      `</div>`);
  }
  slot.insertAdjacentHTML("beforeend", `<div class="qr">${lines.join("")}</div>`);
  renderMath(slot.querySelector(".qr:last-child"));
}

/* ---------------- 自评（没有题目时的弱证据） ---------------- */

async function recordSelfAssess(strong) {
  if (!state.selected) return;
  try {
    const r = await post(`/api/students/${encodeURIComponent(state.student)}/attempt`, {
      kp_id: state.selected, correct: strong,
    });
    $("attemptFeedback").innerHTML =
      `<span class="${strong ? "up" : "down"}">自评：${strong ? "我已掌握" : "还不熟"}　` +
      `掌握度 ${(r.p_before * 100).toFixed(0)}% → ${(r.p_after * 100).toFixed(0)}%</span>`;
    updateMasteryAfterAttempt(r);
    await refreshGraphData();
  } catch (e) { showError(e.message); }
}

/* ---------------- 掌握度条 / 图内作答闭环 ---------------- */

function setMasteryBar(m) {
  const bar = $("masteryBar");
  if (!bar) return;
  const p = m ? Number(m.p) || 0 : 0;
  bar.querySelector(".fill").style.width = `${Math.max(0, Math.min(1, p)) * 100}%`;
  bar.querySelector(".txt").textContent = m
    ? `掌握度 ${(p * 100).toFixed(0)}%  ·  作答 ${m.attempts} 次（对 ${m.correct}）`
    : "尚未作答";
}

function updateMasteryAfterAttempt(r) {
  if (!state.kpDetail || state.kpDetail.kp_id !== r.kp_id) return;
  const m = state.kpDetail.mastery || { p: 0, attempts: 0, correct: 0 };
  m.p = r.p_after;
  m.attempts = (m.attempts || 0) + 1;
  m.correct = (m.correct || 0) + (r.correct ? 1 : 0);
  m.mastered = r.mastered;
  state.kpDetail.mastery = m;
  setMasteryBar(m);
}

function openFigure(fig) {
  if (!state.selected || !fig) return;
  state.figure = fig;
  state.figureBusy = false;
  $("figureTitle").textContent = fig.title || "交互实验台";
  const kpName = state.kpDetail?.name || "";
  $("figureSub").textContent = [kpName, fig.subtitle].filter(Boolean).join(" · ");
  $("figureStandalone").href = fig.standalone_url || fig.embed_url || "#";
  $("figureStatus").textContent = "拖滑片、换接法或图内自评；读数与图线来自同一个电学模型，作答会写回知识图谱。";
  const frame = $("figureFrame");
  frame.style.height = "720px";
  const q = new URLSearchParams({
    kp: state.selected,
    student: state.student || "",
    v: fig.version || "1",
  });
  frame.src = `${fig.embed_url || fig.url}?${q.toString()}`;
  $("figureModal").classList.remove("hidden");
  document.body.classList.add("modal-open");
}

function closeFigure() {
  const modal = $("figureModal");
  if (!modal || modal.classList.contains("hidden")) { state.figure = null; return; }
  modal.classList.add("hidden");
  document.body.classList.remove("modal-open");
  const frame = $("figureFrame");
  if (frame) frame.src = "about:blank";
  $("figureStatus").textContent = "";
  state.figure = null;
  state.figureBusy = false;
}

function sendMasteryToFigure(p) {
  const frame = $("figureFrame");
  if (!state.figure || !frame?.contentWindow || !state.selected) return;
  const value = Number(p);
  frame.contentWindow.postMessage({
    type: "mastery",
    kp_id: state.selected,
    p: Number.isFinite(value) ? value : 0,
  }, location.origin);
}

async function handleFigureAttempt(data) {
  if (state.figureBusy || !state.figure || !state.selected) return;
  if (data.kp_id && data.kp_id !== state.selected) return;
  state.figureBusy = true;
  const status = $("figureStatus");
  status.textContent = "正在写回掌握度…";
  try {
    const r = await post(`/api/students/${state.student}/attempt`, {
      kp_id: state.selected,
      correct: !!data.correct,
    });
    const rd = data.reading;
    const rdText = rd
      ? `　读数 U ${rd.U} V · I ${rd.I} A · R ${rd.R} Ω`
      : "";
    status.innerHTML = `<span class="${data.correct ? "ok" : "bad"}">${data.correct ? "✓" : "✗"} 已写回</span>` +
      `　掌握度 ${(r.p_before * 100).toFixed(0)}% → ${(r.p_after * 100).toFixed(0)}%` +
      `${r.mastered ? "　🎉 已掌握" : ""}<span class="rd">${rdText}</span>`;
    updateMasteryAfterAttempt(r);
    await refreshGraphData();
    sendMasteryToFigure(r.p_after);
  } catch (e) {
    status.textContent = "写回失败：" + e.message;
    showError(e.message);
  } finally {
    state.figureBusy = false;
  }
}

window.addEventListener("message", (event) => {
  if (event.origin !== location.origin) return;
  const data = event.data || {};
  if (data.type === "height") {
    const h = Number(data.height);
    if (state.figure && Number.isFinite(h)) {
      $("figureFrame").style.height = `${Math.max(520, Math.min(1200, Math.ceil(h)))}px`;
    }
    return;
  }
  if (data.type === "ready") {
    sendMasteryToFigure(state.kpDetail?.mastery?.p ?? 0);
    return;
  }
  if (data.type === "attempt") handleFigureAttempt(data);
});

$("figureClose").onclick = closeFigure;
$("figureModal").addEventListener("click", (e) => {
  if (e.target === $("figureModal")) closeFigure();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.figure) closeFigure();
});

async function refreshGraphData() {
  if (state.viewMode !== "chain") {
    const g = await api(`/api/graph/${encodeURIComponent(state.band)}/${encodeURIComponent(state.subject)}?${graphQuery()}`);
    state.graph = g;
    renderGraph();
  }
  // 知识链模式：重载当前链（掌握度章/等级会变）
  if (state.selected) await loadChain(state.selected);
}

$("btnAsk").onclick = askQuestion;
$("btnNextQ").onclick = askQuestion;
$("btnCorrect").onclick = () => recordSelfAssess(true);
$("btnWrong").onclick = () => recordSelfAssess(false);

$("btnDiagnose").onclick = async () => {
  if (!state.selected) return;
  try {
    const d = await post(`/api/students/${state.student}/diagnose`, { kp_ids: [state.selected] });
    $("diagCard").classList.remove("hidden");
    $("planCard").classList.add("hidden");
    state.rootCauses = new Set(d.root_causes.map(c => c.kp_id));
    renderGraph();
    $("diagBody").innerHTML = d.root_causes.length === 0
      ? `<p class="tiny">没有找到弱先修——这个知识点本身可能就是病根。</p>`
      : d.root_causes.map(c => `
        <div class="cause">
          <b>${esc(c.name)}</b><span class="score">得分 ${c.score.toFixed(2)}</span>
          <div class="why">${esc(c.reason)}</div>
          ${(c.explanations || []).slice(0, 2).map(e =>
            `<div class="path">解释「${esc(e.symptom)}」：${e.path.map(esc).join(" ← ")}</div>`).join("")}
        </div>`).join("");
  } catch (e) { showError(e.message); }
};

$("btnPlan").onclick = async () => {
  if (!state.selected) return;
  try {
    const p = await post(`/api/students/${state.student}/plan`, { goal_id: state.selected });
    $("planCard").classList.remove("hidden");
    $("diagCard").classList.add("hidden");
    state.planNodes = new Set(p.stages.flatMap(s => s.nodes.map(n => n.kp_id)));
    renderGraph();
    let html = `<div class="tiny">目标 <b>${esc(p.goal.name)}</b>：${p.total_nodes} 个知识点，约 ${Math.round(p.total_minutes)} 分钟` +
      `（跳过已掌握 ${p.skipped_mastered} 个${p.truncated ? "，路径过长已截断" : ""}）</div>`;
    html += p.stages.map(s => `
      <div class="stage">
        <div class="hd">第 ${s.stage + 1} 阶段</div>
        ${s.nodes.map(n => `
          <div class="item" data-kp="${n.kp_id}">
            <span>${esc(n.name)}<span class="why">${esc(n.reason)} · 预计 ${n.est_minutes} 分钟${n.misconceptions?.length ? " · ⚠有易错点" : ""}</span></span>
            <span class="p">${(n.p_mastery * 100).toFixed(0)}%</span>
          </div>`).join("")}
      </div>`).join("");
    $("planBody").innerHTML = html;
    $("planBody").querySelectorAll(".item").forEach(el => el.onclick = () => selectKp(el.dataset.kp));
  } catch (e) { showError(e.message); }
};

/* ---------------- 搜索 ---------------- */

function setupSearch() {
  const box = $("searchBox"), res = $("searchResults");
  box.onkeydown = async (e) => {
    if (e.key !== "Enter") return;
    const q = box.value.trim();
    if (!q) { res.innerHTML = ""; return; }
    try {
      const hits = await api(`/api/search?q=${encodeURIComponent(q)}&limit=12`);
      res.innerHTML = hits.length === 0 ? `<div>无结果</div>` :
        hits.map(h => `<div data-kp="${h.kp_id}">${esc(h.name)}<span class="m">${h.grade_band}·${h.subject}·${h.grades}</span></div>`).join("");
      res.querySelectorAll("div[data-kp]").forEach(el => el.onclick = async () => {
        res.innerHTML = ""; box.value = "";
        try {
          const loc = await api(`/api/kp/${el.dataset.kp}`);
          if (loc.grade_band !== state.band || loc.subject !== state.subject) {
            // 先同步切好学段/科目，再只加载一次（旧代码在这里会并发加载旧科目）
            state.band = loc.grade_band;
            $("bandSel").value = state.band;
            refreshSubjectOptions();
            state.subject = loc.subject;
            $("subjectSel").value = state.subject;
            await loadGraph();
          }
          await selectKp(el.dataset.kp);
        } catch (err) { showError(err.message); }
      });
    } catch (err) { showError(err.message); }
  };
  box.onblur = () => setTimeout(() => (res.innerHTML = ""), 200);
}

init().catch(err => {
  console.error(err);
  const el = $("errorBanner");
  el.textContent = "初始化失败：" + err.message;
  el.classList.remove("hidden");
});
