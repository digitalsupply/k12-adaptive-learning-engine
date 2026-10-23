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
  hw: null,                // 作业作答上下文 {cid, aid, item, title, total}
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
  const roleBtn = $("roleBtn");
  if (state.user) {
    who.classList.remove("hidden");
    who.textContent = state.user.is_guest
      ? "游客模式 · 数据不长期保留"
      : `${state.user.username}`;
    who.title = state.user.is_guest ? "注册后数据可长期保存"
      : (state.user.role === "teacher" ? "教师视角：教材脉络 · 班级学情" : "学生视角：自适应学习 · 自我巩固");
    loginBtn.classList.add("hidden");
    $("logoutBtn").classList.remove("hidden");
    // 角色按钮(自选角色,师生平等,随时切换)
    if (!state.user.is_guest) {
      roleBtn.classList.remove("hidden");
      roleBtn.textContent = state.user.role === "teacher" ? "🧑‍🏫 教师" : "🎒 学生";
      roleBtn.title = "点击切换角色（师生平等，随时可换）";
    } else {
      roleBtn.classList.add("hidden");
    }
    // 视角下发：审题台是教师专属；班级两方都进（学生=加入+做作业,教师=建班+布置+学情）
    llmBtn.classList.toggle("hidden", !!state.user.is_guest);
    $("classBtn").classList.toggle("hidden", !!state.user.is_guest);
    $("reviewBtn").classList.toggle("hidden",
      !!state.user.is_guest || state.user.role !== "teacher");
  } else {
    who.classList.add("hidden");
    roleBtn.classList.add("hidden");
    loginBtn.classList.remove("hidden");
    $("logoutBtn").classList.add("hidden");
    llmBtn.classList.add("hidden");
    $("classBtn").classList.add("hidden");
    $("reviewBtn").classList.add("hidden");
  }
}

/* 切换自选角色：教师⇄学生,立即生效并重渲染入口 */
async function switchRole() {
  if (!state.user || state.user.is_guest) return;
  const next = state.user.role === "teacher" ? "student" : "teacher";
  const ok = window.confirm(next === "teacher"
    ? "切换为「教师」视角？将开放建班、布置作业、审题台等权限。"
    : "切换为「学生」视角？以自学为主,提交的修订需教师放行。");
  if (!ok) return;
  try {
    const r = await post("/api/auth/role", { role: next });
    state.user = r.user;
    renderAuthBar();
    showError(`已切换为${next === "teacher" ? "🧑‍🏫 教师" : "🎒 学生"}视角。`);
  } catch (e) { showError(e.message); }
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
  $("rolePick").classList.toggle("hidden", mode !== "register");
  $("authModal").classList.remove("hidden");
  $("authUser").focus();
}
function closeAuth() { $("authModal").classList.add("hidden"); }

async function authSubmit() {
  const username = $("authUser").value.trim();
  const password = $("authPass").value;
  try {
    const path = authMode === "login" ? "/api/auth/login" : "/api/auth/register";
    const payload = { username, password };
    if (authMode === "register") {
      const sel = document.querySelector("input[name=regRole]:checked");
      payload.role = sel ? sel.value : "student";
    }
    const r = await post(path, payload);
    state.user = r.user;
    closeAuth();
    await reloadSession();
    showError(`欢迎，${state.user.username}！已切换到你的${state.user.role === "teacher" ? "教师" : "学生"}视图。`);
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

/* ---------------- 班级与作业面板 ---------------- */
/* 教师：建班 → 抢先浏览知识点（把当前知识点加入作业草稿）→ 生成作业 →
   之后发放 6 位邀请码给学生。学生：输入邀请码入班 → 点作业题直接作答
   （判分、掌握度、qhash 校验全复用既有链路）。 */

let classDraft = [];          // 作业草稿 [{kp_id, kp_name}]
let classDetailCtx = null;    // 当前打开的班级 {id, name, role, join_code}
let hwCtxCache = new Map();   // aid → 作业详情缓存

async function openClass() { closeLlm(); await renderClassList(); openBox($("classModal")); }

function closeClass() { closeBox($("classModal")); }

/* ---------------- 审题台：抽题预览 → 通过/驳回 → 精选题库 ---------------- */

let revCtx = { kp: null, kpName: "", preview: null, searchTimer: null };

function openReview() {
  closeLlm();
  document.body.classList.add("modal-open");
  $("reviewModal").classList.remove("hidden");
  refreshReviewStats();
}

function closeReview() { closeBox($("reviewModal")); }

async function refreshReviewStats() {
  $("revKpCur").textContent = revCtx.kp
    ? `当前审题对象：${revCtx.kpName}`
    : (state.selectedName ? `未选择——可用当前选中的知识点：${state.selectedName}` : "先搜索，或先在知识图里选中一个知识点");
  $("revDraw").disabled = !(revCtx.kp || state.selected);
  $("revList").disabled = !revCtx.kp;
  try {
    const r = await api("/api/review/curated?limit=1");
    $("reviewStats").textContent =
      `精选题库：通过 ${r.counts.approved} · 待审 ${r.counts.pending || 0} · 驳回 ${r.counts.rejected}`;
  } catch (_) {
    $("reviewStats").textContent = "";     // 非教师（403）等场景静默
  }
}

async function revSearch(q) {
  const list = $("revKpList");
  if (!q || q.length < 2) { list.classList.add("hidden"); return; }
  try {
    const hits = await api(`/api/search?q=${encodeURIComponent(q)}&limit=8`);
    if (!Array.isArray(hits) || !hits.length) { list.classList.add("hidden"); return; }
    list.innerHTML = hits.map(k => `
      <div class="rev-item" data-kp="${esc(k.kp_id)}" data-name="${esc(k.name)}">
        ${esc(k.name)}<span class="tiny dim"> ${esc(k.subject || "")}</span></div>`).join("");
    list.classList.remove("hidden");
    list.querySelectorAll(".rev-item").forEach(el => {
      el.onclick = () => {
        revCtx.kp = el.dataset.kp; revCtx.kpName = el.dataset.name;
        $("revKpQ").value = el.dataset.name;
        list.classList.add("hidden");
        refreshReviewStats();
        $("revDraw").disabled = false;
      };
    });
  } catch (e) { showError(e.message); }
}

async function revDraw() {
  const kpId = revCtx.kp || state.selected;
  if (!kpId) return;
  const btn = $("revDraw");
  btn.disabled = true;
  $("revBody").innerHTML = `<div class="tiny dim" id="revWaiting">正在抽题预览（AI 命题约需 10~40 秒）…</div>`;
  try {
    const q = await post("/api/review/preview", { kp_id: kpId });
    revCtx.preview = q;
    renderRevCard(q);
  } catch (e) {
    revCtx.preview = null;
    $("revBody").innerHTML = `<div class="quiz-none">⚠ ${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

function renderRevCard(q) {
  const letters = "ABCDEFGH";
  const src = q.source === "curated" ? "✅ 精选复用" : ((q.source === "llm" || q.source === "llm-cache") ? `🤖 AI 命题（${esc(q.model || "")}）` : "📝 模板兜底");
  $("revBody").innerHTML = `
    <div class="rev-card">
      <div class="tiny dim">${esc(q.kp_name)} · seed ${q.seed} · ${src} · 精选库 ${q.curated_counts.approved} 道</div>
      <div class="quiz-prompt">${esc(flat(q.prompt))}</div>
      <div class="rev-opts">
        ${q.options.map((o, i) => `
          <div class="rev-opt ${i === q.answer_index ? "ok" : ""}">
            <span class="qletter">${letters[i]}</span>${esc(flat(o))}
            ${i === q.answer_index ? `<b style="margin-left:auto">✓ 正确</b>` : ""}
          </div>`).join("")}
      </div>
      <div class="rev-explain"><b>解析：</b>${esc(q.explain || "")}</div>
      ${q.misconception ? `<div class="rev-explain tiny">针对误区：${esc(q.misconception)}</div>` : ""}
      <div class="cls-actions">
        <button id="revOk" class="primary">✅ 通过，入精选题库</button>
        <button id="revNo">✕ 驳回</button>
        <input id="revNote" placeholder="批注（可选）" style="flex:1 1 140px">
      </div>
    </div>`;
  const body = $("revBody");
  body.querySelector("#revOk").onclick = () => revDecide(q, true);
  body.querySelector("#revNo").onclick = () => revDecide(q, false);
  renderMath(body);
}

async function revDecide(q, approve) {
  const note = $("revNote") ? $("revNote").value : "";
  try {
    const r = await post("/api/review/decide",
      { kp_id: q.kp_id, seed: q.seed, qhash: q.qhash, approve, note });
    $("revBody").innerHTML = `<div class="quiz-none ${approve ? "ok" : "dim"}">${
      approve ? "✅ 已入库：该知识点之后出题将优先复用这道精选题。" : "✕ 已驳回，学生不会看到这道题。"
    }</div>`;
    refreshReviewStats();
  } catch (e) { showError(e.message); }
}

async function revListCurated() {
  const kpId = revCtx.kp;
  if (!kpId) { showError("先搜索选择一个知识点"); return; }
  try {
    const r = await api(`/api/review/curated?kp_id=${encodeURIComponent(kpId)}`);
    const items = r.items.map(it => `
      <div class="cls-row">
        <div><span class="tiny dim">${new Date((it.ts || 0) * 1000).toLocaleString()} · ${esc(it.reviewer || "")}</span>
          ${esc((it.prompt || "").slice(0, 60))}…</div>
        <button data-down="${esc(it.qhash)}">下架</button>
      </div>`).join("") || `<div class="tiny dim">该知识点还没有精选题。</div>`;
    $("revBody").innerHTML = `<div class="rev-list-curated"><b class="tiny">精选题库 · ${esc(revCtx.kpName)}（${r.counts.approved}）</b>${items}</div>`;
    $("revBody").querySelectorAll("[data-down]").forEach(b => {
      b.onclick = async () => {
        try {
          await post("/api/review/unapprove", { qhash: b.dataset.down });
          await revListCurated();
        } catch (e) { showError(e.message); }
      };
    });
  } catch (e) { showError(e.message); }
}

/* 师生共建题库：对当前正在做的题「修改/标记」→ 提交入库。
   教师（班级创建者）直接入库；学生提交进 pending 待教师放行。 */
function openRevEdit(q, flagMode) {
  if (!q || !state.user) return;
  if (state.user.is_guest) { showError("游客不能参与题库共建，请注册后再来"); return; }
  const letters = "ABCDEF";
  // 已知正确项？判定:教师预览的题带 answer_index;正在练的题不带
  const knownAnswer = Number.isInteger(q.answer_index);
  const nOpts = q.options.length;
  document.body.classList.add("modal-open");
  $("reviewModal").classList.remove("hidden");
  $("reviewStats").textContent = flagMode
    ? "🚩 标记模式：不改内容，把问题留给教师（题面不清/答案可疑/超纲/选项雷同…）"
    : "✏️ 修订模式：改完后教师提交即入库；学生提交进待审队列";
  $("revKpCur").textContent = `对象：${q.kp_name} · seed ${q.seed}`;
  $("revBody").innerHTML = `
    <div class="rev-card">
      <div class="tiny dim">原题干（可直接修改）：</div>
      <textarea id="rePrompt" rows="4">${esc(q.prompt)}</textarea>
      <div class="tiny dim" style="margin-top:6px">选项（每行一个，保持风格统一）：</div>
      ${q.options.map((o, i) => `
        <div class="re-opt">
          <label><input type="radio" name="reAns" value="${i}" ${knownAnswer && i === q.answer_index ? "checked" : ""}>${letters[i]}</label>
          <textarea id="reOpt${i}" rows="2">${esc(o)}</textarea>
        </div>`).join("")}
      <label class="tiny re-keep"><input type="radio" name="reAns" value="keep" ${knownAnswer ? "" : "checked"}>保持原答案${knownAnswer ? "（当前是 " + letters[q.answer_index] + "）" : ""}</label>
      <div class="tiny dim" style="margin-top:6px">解析（留空 = 保持原解析；教师修订建议把「为什么对/错在哪」写清楚）：</div>
      <textarea id="reExplain" rows="3" placeholder="留空 = 保持原解析">${esc("")}</textarea>
      <div class="tiny dim" style="margin-top:6px">批注/icon 标记原因（审核人可见）：</div>
      <input id="reNote" placeholder="例：第 B 项数值错误；题干没给条件…" style="width:100%">
      <div class="cls-actions" style="margin-top:8px">
        <button id="reSubmit" class="primary">${flagMode ? "🚩 提交标记" : "✅ 提交修订"}</button>
        <button id="reCancel">取消</button>
      </div>
    </div>`;
  $("reCancel").onclick = closeReview;
  $("reSubmit").onclick = async () => {
    const opts = [];
    for (let i = 0; i < nOpts; i++) opts.push($(`reOpt${i}`).value);
    const ansSel = document.querySelector("input[name=reAns]:checked");
    const ans = ansSel && ansSel.value !== "keep" ? Number(ansSel.value) : null;
    try {
      const r = await post("/api/review/submit-edit", {
        kp_id: q.kp_id, seed: q.seed, qhash: q.qhash,
        prompt: $("rePrompt").value, options: opts,
        answer_index: ans, explain: $("reExplain").value.trim() || null,
        note: $("reNote").value, flag: flagMode,
      });
      $("revBody").innerHTML = `<div class="quiz-none ok" style="color:#1d7a46">${
        r.status === "approved"
          ? "✅ 修订已入精选题库：之后该知识点出题优先用修订版。"
          : "⏳ 已提交，进入待审队列——教师会在审题台的「⏳ 待审」里放行。"}</div>`;
      refreshReviewStats();
    } catch (e) { showError(e.message); }
  };
}

/* 教师放行/驳回学生的修订 */
async function revListPending() {
  const body = $("revBody");
  let r;
  try { r = await api("/api/review/pending"); }
  catch (e) { showError(e.message); return; }
  const items = r.items.map(it => `
    <div class="cls-row">
      <div>
        <b class="tiny">${esc(it.reviewer || "")}</b>
        <span class="tiny dim">${new Date((it.ts || 0) * 1000).toLocaleString()}</span>
        <div class="tiny">${esc((it.prompt || "").slice(0, 80))}…</div>
        ${it.note ? `<div class="tiny dim">批注：${esc(it.note)}</div>` : ""}
      </div>
      <div>
        <button data-ok="${esc(it.qhash)}" class="primary">放行</button>
        <button data-no="${esc(it.qhash)}">驳回</button>
      </div>
    </div>`).join("") || `<div class="tiny dim">没有待审的修订。</div>`;
  body.innerHTML = `<div class="tiny">⏳ 待审（学生提交，放行后进精选题库）</div>${items}`;
  body.querySelectorAll("[data-ok]").forEach(b => {
    b.onclick = async () => {
      try {
        await post("/api/review/decide-edit", { qhash: b.dataset.ok, approve: true });
        await revListPending();
      } catch (e) { showError(e.message); }
    };
  });
  body.querySelectorAll("[data-no]").forEach(b => {
    b.onclick = async () => {
      try {
        await post("/api/review/decide-edit", { qhash: b.dataset.no, approve: false });
        await revListPending();
      } catch (e) { showError(e.message); }
    };
  });
}

/* 弹窗开合的公共小工具：body 滚动锁 + Backdrop 点击关闭 */
let knownKeydown = null;
function openBox(modal) {
  modal.classList.remove("hidden");
  document.body.classList.add("modal-open");
  if (!knownKeydown) {
    knownKeydown = (e) => { if (e.key === "Escape") { closeReview(); closeClass(); closeLlm(); closeAuth(); } };
    document.addEventListener("keydown", knownKeydown);
  }
}
function closeBox(modal) {
  modal.classList.add("hidden");
  if ($("authModal").classList.contains("hidden") &&
      $("llmModal").classList.contains("hidden") &&
      $("classModal").classList.contains("hidden") &&
      $("reviewModal").classList.contains("hidden")) {
    document.body.classList.remove("modal-open");
  }
}

async function renderClassList() {
  classDetailCtx = null;
  const head = $("classHead"), body = $("classBody");
  head.innerHTML = `<h3>班级与作业</h3>`;
  let classes = [];
  try { classes = await api("/api/classes"); } catch (e) { showError(e.message); return; }
  const rows = classes.map(c => `
    <div class="cls-row">
      <div>
        <b>${esc(c.name)}</b>
        <span class="tiny dim">${c.role === "owner" ? "👨‍🏫 教师" : "🎒 学生"}
          · ${c.members} 人 · ${c.assignments} 次作业</span>
        ${c.role === "owner" ? `<span class="tiny">邀请码 <code>${esc(c.join_code)}</code></span>` : ""}
      </div>
      <button data-open="${esc(c.id)}">${c.assignments > 0 ? "进入" : "查看"}</button>
    </div>`).join("") || `<div class="tiny dim">还没有班级。教师可以新建，学生凭邀请码加入。</div>`;
  const isTeacher = state.user && state.user.role === "teacher";
  body.innerHTML = `
    <div>
    ${rows}
    ${isTeacher ? `
    <div class="cls-actions">
      <div>
        <input id="clsNewName" placeholder="新班级名称（1~32 字）">
        <button id="clsCreate" class="primary">新建班级（我当教师）</button>
      </div>
      <div>
        <input id="clsJoinCode" placeholder="6 位邀请码" maxlength="8">
        <button id="clsJoin">加入班级</button>
      </div>
    </div>` : `
    <div class="cls-actions">
      <div>
        <input id="clsJoinCode" placeholder="6 位邀请码" maxlength="8">
        <button id="clsJoin">加入班级</button>
      </div>
    </div>
    <div class="tiny dim" style="margin-top:2px">想建班当教师？点右上角「🧑‍🏫 教师」切换角色。</div>`}
    </div>`;
  const createBtn = $("clsCreate");
  if (createBtn) createBtn.onclick = async () => {
    try {
      const c = await post("/api/classes", { name: $("clsNewName").value });
      classDraft = [];
      await renderClassDetail(c);
    } catch (e) { showError(e.message); }
  };
  $("clsJoin").onclick = async () => {
    try {
      await post("/api/classes/join", { code: $("clsJoinCode").value });
      await renderClassList();
    } catch (e) { showError(e.message); }
  };
  body.querySelectorAll("[data-open]").forEach(b => {
    b.onclick = async () => {
      const c = classes.find(c => c.id === b.dataset.open);
      await renderClassDetail(c);
    };
  });
}

function draftChipsHtml() {
  if (!classDraft.length) return `<span class="tiny dim">还没有选知识点。在知识图谱/链里选中一个知识点后，回这里点「把当前知识点加入草稿」。</span>`;
  return classDraft.map((d, i) =>
    `<span class="chip">${esc(d.kp_name)}
       <a data-rm="${i}" title="移除">✕</a></span>`).join(" ");
}

async function renderClassDetail(c) {
  classDetailCtx = c;
  $("classHead").innerHTML =
    `<button id="clsBack" class="x" title="返回列表">←</button>` +
    `<h3>${esc(c.name)} ${c.role === "owner" ? "· 教师" : ""}</h3>` +
    (c.role === "owner"
      ? `<div class="tiny">邀请码 <code>${esc(c.join_code || "")}</code>（发给学生，他们凭码加入）</div>`
      : `<div class="tiny dim">你在本班是学生身份</div>`);
  const body = $("classBody");
  let list = [];
  try { list = await api(`/api/classes/${encodeURIComponent(c.id)}/assignments`); }
  catch (e) { showError(e.message); return; }
  body.innerHTML = `
    ${c.role === "owner" ? `
      <div class="cls-draft">
        <b>布置新作业</b>
        <div class="chips">${draftChipsHtml()}</div>
        <div class="cls-actions" style="margin-top:6px">
          <button id="clsAddKp" ${state.selectedName ? "" : "disabled"}
            title="${state.selectedName ? "" : "先在左侧知识图里选一个知识点"}">
            ➕ 把当前知识点加入草稿${state.selectedName ? `（${esc(state.selectedName)}）` : ""}</button>
          <input id="asgTitle" placeholder="作业标题（1~60 字）">
          <button id="asgCreate" class="primary" ${classDraft.length ? "" : "disabled"}>生成作业</button>
          <button id="clsClear" class="subtle ${classDraft.length ? "" : "hidden"}">清空草稿</button>
        </div>
      </div>` : ""}
    <div class="cls-list">${list.map(a => `
      <div class="cls-row">
        <div><b>${esc(a.title)}</b>
          <span class="tiny dim">${a.total} 题</span>
          ${c.role === "owner"
            ? `<span class="tiny dim">全班 ${a.class_done}/${a.members} 人完成
                 · 对题 ${a.class_correct}</span>`
            : `<span class="tiny ${a.my_done === a.total ? "ok" : "dim"}">
                 我已做 ${a.my_done}/${a.total}${a.my_done ? ` · 对 ${a.my_correct}` : ""}</span>`}
        </div>
        <div>
          <button data-hw="${esc(a.id)}" data-total="${a.total}">${a.my_done >= a.total ? "查看" : "去做题"}</button>
          ${c.role === "owner" ? `<button data-res="${esc(a.id)}">成绩</button>` : ""}
        </div>
      </div>`).join("") || `<div class="tiny dim">暂无作业。</div>`}</div>`;
  if (c.role === "owner") {
    const addBtn = $("clsAddKp");
    if (addBtn) addBtn.onclick = () => {
      if (!state.selected || !state.selectedName) return;
      if (classDraft.some(d => d.kp_id === state.selected)) { showError("这个知识点已在草稿里"); return; }
      classDraft.push({ kp_id: state.selected, kp_name: state.selectedName });
      const btn = $("asgCreate");
      btn.disabled = false;
      $("clsClear").classList.remove("hidden");
      renderDraftRefresh(c);
    };
    const cr = $("clsClear");
    if (cr) cr.onclick = () => { classDraft = []; renderDraftRefresh(c); };
    const cb = $("asgCreate");
    if (cb) cb.onclick = async () => {
      const title = $("asgTitle").value.trim();
      if (!title || !classDraft.length) { showError("标题和至少一个知识点不能为空"); return; }
      try {
        await post(`/api/classes/${encodeURIComponent(c.id)}/assignments`, {
          title, kp_ids: classDraft.map(d => d.kp_id),
        });
        classDraft = [];
        await renderClassDetail(c);
      } catch (e) { showError(e.message); }
    };
  }
  body.querySelectorAll("[data-hw]").forEach(b => { b.onclick = () => openAssignment(c, b.dataset.hw, b.dataset.total); });
  body.querySelectorAll("[data-res]").forEach(b => {
    b.onclick = async () => await renderResults(c, b.dataset.res);
  });
  const backBtn = $("clsBack");
  if (backBtn) backBtn.onclick = () => { closeBox($("classModal")); openClass(); };
}

function renderDraftRefresh(c) {
  // 只刷新草稿区，避免整面板重绘焦点丢失
  const box = $("classBody").querySelector(".cls-draft");
  if (!box) return;
  box.querySelector(".chips").innerHTML = draftChipsHtml();
  box.querySelectorAll(".chip a[data-rm]").forEach(a => {
    a.onclick = () => { classDraft.splice(Number(a.dataset.rm), 1); renderDraftRefresh(c); };
  });
  $("asgCreate") && ($("asgCreate").disabled = !classDraft.length);
  $("clsClear") && $("clsClear").classList.toggle("hidden", !classDraft.length);
}

async function openAssignment(c, aid, total) {
  const modal = $("classModal"); closeBox(modal);
  try {
    const d = await api(`/api/classes/${encodeURIComponent(c.id)}/assignments/${encodeURIComponent(aid)}`);
    hwCtxCache.set(aid, d);
    await startHomework(d);
  } catch (e) { showError(e.message); }
}

/* 进入某道作业题：走常规知识链 + 出题界面，判分走作业专用的服务端点 */
async function startHomework(d) {
  const next = d.items.findIndex(it => !it.done);
  if (next === -1 && d.items.length) {
    showError(`「${d.title}」已完成全部 ${d.items.length} 题，可在班级面板查看记录。`);
    await openClass();
    await renderClassDetail(classDetailCtx || { id: d.class_id, role: "member", ...d });
    return;
  }
  const it = d.items[Math.max(0, next)];
  state.hw = {
    cid: d.class_id, aid: d.id, item: it.index, seed: it.seed,
    title: d.title, total: d.items.length,
  };
  await enterChainView();
  await selectKp(it.kp_id);
  await askQuestion(it.kp_id, it.seed);
}

/* 教师端：全班 × 各题成绩矩阵 */
async function renderResults(c, aid) {
  const body = $("classBody");
  let m;
  try {
    m = await api(`/api/classes/${encodeURIComponent(c.id)}/assignments/${encodeURIComponent(aid)}/results`);
  } catch (e) { showError(e.message); return; }
  body.innerHTML = `
    <div class="matrix">
      <div class="tiny dim">${esc(m.assignment.title)} · 每列一题 · ✓对 ✗错 · 未作答留白</div>
      <table>
        <tr><th>学生</th>${m.items.map((it, i) => `<th title="${esc(it.kp_name)}">${i + 1}</th>`).join("")}<th>对/总</th></tr>
        ${m.rows.map(r => `<tr>
            <td>${esc(r.username || "-")}</td>
            ${r.per.map(p => `<td class="${p === 1 ? "ok" : p === 0 ? "bad" : ""}">${p === 1 ? "✓" : p === 0 ? "✗" : "·"}</td>`).join("")}
            <td>${r.correct}/${m.items.length}</td>
          </tr>`).join("")}
        <tr class="sum"><td>全班对题</td>
          ${m.per_item_correct.map(ok => `<td>${ok}</td>`).join("")}
          <td>对率合计</td></tr>
        <tr class="sum"><td>全班已做</td>
          ${m.per_item_done.map(n => `<td>${n}</td>`).join("")}
          <td>${m.rows.length} 人</td></tr>
      </table>
    </div>`;
  $("clsBack").onclick = async () => { closeBox($("classModal")); await openClass(); };
  classDetailCtx = c;
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

  /* ---- 班级面板绑定 ---- */
  $("classBtn").onclick = openClass;
  $("classClose").onclick = closeClass;
  $("classModal").addEventListener("click", (e) => {
    if (e.target === $("classModal")) closeClass();
  });

  /* ---- 审题台绑定 ---- */
  $("reviewBtn").onclick = openReview;
  $("reviewClose").onclick = closeReview;
  $("reviewModal").addEventListener("click", (e) => {
    if (e.target === $("reviewModal")) closeReview();
  });
  $("revKpQ").addEventListener("input", (e) => {
    clearTimeout(revCtx.searchTimer);
    revCtx.searchTimer = setTimeout(() => revSearch(e.target.value.trim()), 250);
  });
  $("revDraw").onclick = revDraw;
  $("revList").onclick = revListCurated;
  $("revPending").onclick = revListPending;

  /* ---- 角色切换绑定 ---- */
  $("roleBtn").onclick = switchRole;

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
  state.selectedName = d.name;
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
/* 题面/选项压平：LLM 偶发在 $...$ 前后夹换行,而容器是 pre-wrap——
   换行会被渲染成「公式独占一行,和前后文字分家」。统一折成空格,
   文字与公式保持在同一条文字流里。 */
function flat(s) { return String(s ?? "").replace(/[ \t\r]*\n+[ \t\r]*/g, " ").trim(); }

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
   这里只负责展示和提交选项。
   作业场景：state.hw 有值 → 用作业条目的 (kp, seed) 固定出题（全班同题）。 */
async function askQuestion(kpIdOverride = null, fixedSeed = null) {
  const kpId = kpIdOverride || state.selected;
  if (!kpId) return;
  const hw = state.hw;
  const seed = hw ? hw.seed : (fixedSeed ?? Math.floor(Math.random() * 1e6));
  const slot = $("quizSlot");
  const btn = $("btnAsk");
  btn.disabled = true;
  if (!state.selected || state.selected !== kpId) {
    state.selected = kpId;
    try { await selectKp(kpId); } catch (_) { /* 详情失败不阻塞出题 */ }
  }
  slot.innerHTML = `<div class="quiz-none quiz-loading"><span class="spinner"></span>` +
    (hw ? `作业「${esc(hw.title)}」第 ${hw.item + 1} 题，正在出题…`
        : `正在出题… AI 命题约需 10~40 秒，同一题之后秒回`) + `</div>`;
  try {
    const q = await api(`/api/quiz/${encodeURIComponent(kpId)}?seed=${seed}`);
    state.quiz = { ...q, answered: false };
    renderQuiz();
    $("btnNextQ").classList.toggle("hidden", !!hw);
  } catch (e) {
    // 该知识点没有可用的题目素材（可测评性过低 / 标注缺失）
    state.quiz = null;
    slot.innerHTML = `<div class="quiz-none">⚠ 该知识点暂无可出题素材（可测评性不足或标注缺失），${hw ? "请告诉教师换一个知识点" : "可用下方自评。"}</div>`;
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
  const badge = q.source === "curated"
    ? `<span class="src-badge cur" title="教师审定的精选题（出题优先复用）">✅ 教师精选</span>`
    : isLLM
    ? `<span class="src-badge ai" title="由 ${esc(q.model || "AI")} 生成，仅此题可用">🤖 AI 命题</span>`
    : `<span class="src-badge tpl" title="由知识点标注生成（未配置 LLM 时的兜底）">📝 模板示例</span>`;
  slot.innerHTML = `
    <div class="quiz">
      <div class="quiz-kind">${q.kind === "judge" ? "判断正误" : "单项选择"}${badge}</div>
      <div class="quiz-prompt">${esc(flat(q.prompt))}</div>
      <div class="quiz-options">
        ${q.options.map((o, i) => `
          <button class="qopt" data-choice="${i}">
            <span class="qletter">${letters[i]}</span>${esc(flat(o))}
          </button>`).join("")}
      </div>
      ${(state.user && !state.user.is_guest) ? `
      <div class="q-actions">
        <a id="qaEdit" title="修订题干/选项/解析后提交题库">✏️ 修改本题</a>
        <a id="qaFlag" title="题面/答案/超纲有问题？标记给教师">🚩 标记问题</a>
      </div>` : ""}
    </div>`;
  const qaEdit = slot.querySelector("#qaEdit");
  const qaFlag = slot.querySelector("#qaFlag");
  if (qaEdit) qaEdit.onclick = () => openRevEdit(q, false);
  if (qaFlag) qaFlag.onclick = () => openRevEdit(q, true);
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
  const hw = state.hw;
  const url = hw
    ? `/api/classes/${encodeURIComponent(hw.cid)}/assignments/${encodeURIComponent(hw.aid)}/answer`
    : `/api/students/${encodeURIComponent(state.student)}/answer`;
  const body = hw
    ? { item: hw.item, choice, qhash: q.qhash }
    : { kp_id: q.kp_id, seed: q.seed, choice, qhash: q.qhash };
  try {
    const r = await post(url, body);
    showQuizResult(r, q);
    updateMasteryAfterAttempt(r);
    if (state.viewMode === "map") await refreshGraphData(); else await loadChain(q.kp_id);
    sendMasteryToFigure(r.p_after);
    if (hw && r.assignment) {
      hw.done = r.assignment.done;
      // 结果栏上标注作业进度
      const tag = document.createElement("div");
      tag.className = "qr-hw";
      tag.textContent = `📘 作业「${hw.title}」进度 ${r.assignment.done}/${r.assignment.total}` +
        (r.assignment.done >= r.assignment.total ? " · 已全部完成 🎉" : " · 可在班级面板继续下一题");
      slot.querySelector(".qr").appendChild(tag);
    }
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

$("btnAsk").onclick = () => askQuestion();
$("btnNextQ").onclick = () => askQuestion();
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
