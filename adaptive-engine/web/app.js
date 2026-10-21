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
  selected: null, rootCauses: new Set(), planNodes: new Set(),
  pos: {}, view: null,
  kpDetail: null, figure: null, figureBusy: false,
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

async function init() {
  state.groups = await api("/api/groups");
  const bands = [...new Set(state.groups.map(g => g.band))];
  fillSelect($("bandSel"), bands);
  state.band = bands.includes("初中") ? "初中" : bands[0];
  $("bandSel").value = state.band;
  refreshSubjectOptions();

  const students = await api("/api/students");
  if (students.length === 0) students.push(await post("/api/students", { name: "演示学生", model: "bkt" }));
  fillSelect($("studentSel"), students.map(s => s.id),
             students.map(s => `${s.name} · ${s.model.toUpperCase()}`));
  state.student = students[0].id;
  $("studentSel").value = state.student;

  $("bandSel").onchange = () => {
    closeFigure();
    state.band = $("bandSel").value;
    refreshSubjectOptions();
    loadGraph();
  };
  $("subjectSel").onchange = () => { closeFigure(); state.subject = $("subjectSel").value; loadGraph(); };
  $("studentSel").onchange = () => { closeFigure(); state.student = $("studentSel").value; loadGraph(); };
  $("newStudentBtn").onclick = async () => {
    const name = prompt("学生姓名：", "学生" + (Date.now() % 1000));
    if (!name) return;
    const model = (prompt("掌握度模型 (bkt/elo)：", "bkt") || "bkt").trim();
    try {
      const s = await post("/api/students", { name, model: ["bkt", "elo"].includes(model) ? model : "bkt" });
      const sel = $("studentSel");
      sel.add(new Option(`${s.name} · ${s.model.toUpperCase()}`, s.id));
      sel.value = s.id; state.student = s.id;
      loadGraph();
    } catch (e) { showError(e.message); }
  };
  $("showMastered").onchange = renderGraph;
  $("fitBtn").onclick = () => fitView(true);
  setupSearch();
  await loadGraph();
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

/* ---------------- 图谱加载 ---------------- */

async function loadGraph() {
  if (!state.subject) return;
  if (!state.student) { showError("尚未选择学生"); return; }   // 防止 student=null 请求
  const seq = ++loadSeq;
  try {
    const g = await api(`/api/graph/${encodeURIComponent(state.band)}/${encodeURIComponent(state.subject)}?student=${encodeURIComponent(state.student)}`);
    if (seq !== loadSeq) return;                       // 过期响应直接丢弃
    state.graph = g;
    // 切换科目后原选中节点可能已不在图中
    if (state.selected && !g.nodes.some(n => n.id === state.selected)) state.selected = null;
    state.rootCauses.clear(); state.planNodes.clear();
    state.view = null;                                  // 新图重新适配视口
    const s = g.stats;
    $("graphStats").textContent =
      `${state.band}/${state.subject} · ${g.nodes.length} 知识点 · ${g.edges.length} 关系 · 最深 ${s.max_depth} 层` +
      (s.cycles.length ? ` · ⚠ ${s.cycles.length} 处循环依赖` : "");
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
  if (node.p >= 0.85) return "#27ae60";
  if (node.p >= 0.5) return "#f39c12";
  return "#e74c3c";
}

/* ---------------- 渲染 ---------------- */

function renderGraph() {
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
  state.graph.edges.forEach(e => {
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
  state.graph.nodes.forEach(n => {
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

    const t = mk("text");
    t.setAttribute("y", -14); t.setAttribute("text-anchor", "middle");
    t.textContent = n.name.length > 9 ? n.name.slice(0, 8) + "…" : n.name;
    g.appendChild(t);
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
  renderGraph();
  const svg = $("svg");
  if (svg && !svg.classList.contains("no-labels")) {
    // 只在用户已放大时保持视口，避免每次点击都跳动
  }
  try {
    const d = await api(`/api/kp/${kpId}?student=${encodeURIComponent(state.student)}`);
    renderKpCard(d);
    centerOn(kpId);
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
  if (d.confidence < 0.6) {
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
  document.querySelectorAll("[data-close]").forEach(b => b.onclick = () => $(b.dataset.close).classList.add("hidden"));
}

function kv(title, inner) { return `<div class="kv"><b>${title}</b><ul>${inner}</ul></div>`; }
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

/* ---------------- 作答 / 归因 / 规划 ---------------- */

async function record(correct) {
  if (!state.selected) return;
  try {
    const r = await post(`/api/students/${state.student}/attempt`,
                         { kp_id: state.selected, correct });
    const up = r.p_after >= r.p_before;
    $("attemptFeedback").innerHTML =
      `<span class="${up || r.mastered ? "up" : "down"}">${correct ? "✓" : "✗"} ` +
      `掌握度 ${(r.p_before * 100).toFixed(0)}% → ${(r.p_after * 100).toFixed(0)}%` +
      `${r.mastered ? "　🎉 已掌握！" : ""}</span>` +
      (r.diagnosis?.length ? `<div style="margin-top:6px">⚡ 疑似根因：` +
        r.diagnosis.slice(0, 2).map(c => `<b>${esc(c.name)}</b>(掌握 ${(c.p_mastery * 100).toFixed(0)}%)`).join("、") + `</div>` : "");
    updateMasteryAfterAttempt(r);
    await refreshGraphData();
    sendMasteryToFigure(r.p_after);
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
  const g = await api(`/api/graph/${encodeURIComponent(state.band)}/${encodeURIComponent(state.subject)}?student=${encodeURIComponent(state.student)}`);
  state.graph = g;
  renderGraph();
}

$("btnCorrect").onclick = () => record(true);
$("btnWrong").onclick = () => record(false);

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
