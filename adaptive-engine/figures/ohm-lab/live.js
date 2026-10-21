/*__MODEL__*/
/*__GEOM__*/
/*__DATA__*/

const { P, MODES, CHART, INITIAL, METERS, NEEDLE_R, NEEDLE_Z } = DATA;
const SOURCE_V = DATA.SOURCE_V;
const RHEOSTAT_MAX = DATA.RHEOSTAT_MAX;
const AMMETER_MAX = DATA.AMMETER_MAX;
const VOLTMETER_MAX = DATA.VOLTMETER_MAX;
const MODE_BY_ID = Object.fromEntries(MODES.map((mode) => [mode.id, mode]));

const stage = document.querySelector(".ohm-stage");
const svg = stage ? stage.querySelector("svg") : null;
if (!stage || !svg) throw new Error("ohm-lab: stage or svg missing");

const readout = document.querySelector("[data-readout]");
const hint = document.querySelector('[data-corner="hint"]');
const announceEl = stage.querySelector("[data-announce]");
const statusEl = stage.querySelector("[data-check-status]");
const checkButtons = [...stage.querySelectorAll("[data-check]")];
const carriage = svg.querySelector("[data-carriage]");
const modePointer = svg.querySelector(".ohm-mode-pointer");
const keyPointer = svg.querySelector(".ohm-key-pointer");
const pen = svg.querySelector("[data-pen]");
const roller = svg.querySelector("[data-roller]");
const arms = [...svg.querySelectorAll("[data-arm]")];
const traceEls = [...svg.querySelectorAll("[data-trace]")];
const traceEnds = [...svg.querySelectorAll("[data-trace-end]")];
const needleEls = new Map([...svg.querySelectorAll("[data-needle]")].map((el) => [el.dataset.needle, el]));

const still = window.matchMedia("(prefers-reduced-motion: reduce)");
const embedded = window.parent !== window;
const params = new URLSearchParams(location.search);
const KP_ID = params.get("kp") || "";
const ORIGIN = location.origin === "null" ? "*" : location.origin;

const initial = solve({ alpha: INITIAL.alpha, mode: MODES[INITIAL.modeIndex].id, closed: true });
const state = {
  alpha: INITIAL.alpha,
  target: INITIAL.alpha,
  mode: INITIAL.modeIndex,
  closed: INITIAL.closed,
  dragging: false,
  pointerId: null,
  touring: false,
  clock: 0,
  last: 0,
};
const traces = MODES.map((_, index) => (index === INITIAL.modeIndex ? INITIAL.alpha : 0));
const needleState = METERS.map((meter) => {
  const value = meter.key === "A" ? initial.needle.A : initial.needle.V;
  return { value, target: value, velocity: 0 };
});
const pointerState = {
  mode: MODES[INITIAL.modeIndex].angle,
  modeTarget: MODES[INITIAL.modeIndex].angle,
  key: 45,
  keyTarget: 45,
};

let raf = 0;
let visible = true;
let tourTimer = 0;

const follow = (value, target, tau, dt) =>
  still.matches ? target : value + (target - value) * (1 - Math.exp(-dt / Math.max(0.001, tau)));

function smooth(edge0, edge1, value) {
  const t = Math.min(1, Math.max(0, (value - edge0) / (edge1 - edge0 || 1)));
  return t * t * (3 - 2 * t);
}

function announce(text) {
  if (announceEl) announceEl.textContent = text;
}

function send(message) {
  if (!embedded) return;
  try {
    window.parent.postMessage({ ...message, kp_id: KP_ID }, ORIGIN);
  } catch {
    /* 跨域或已卸载，忽略 */
  }
}

function reportHeight() {
  const height = Math.ceil(document.body.getBoundingClientRect().height);
  if (height > 0) send({ type: "height", height });
}

/* ---------------- 巡演 ---------------- */

function tourAt(time) {
  const cycle = 16;
  const u = ((time % cycle) + cycle) % cycle;
  if (u < 4.0) return { mode: 0, alpha: smooth(0, 4.0, u) };
  if (u < 4.6) return { mode: 0, alpha: 1 };
  if (u < 6.0) return { mode: 0, alpha: 1 - smooth(4.6, 6.0, u) };
  if (u < 6.2) return { mode: 1, alpha: 0 };
  if (u < 9.0) return { mode: 1, alpha: smooth(6.2, 9.0, u) };
  if (u < 9.6) return { mode: 1, alpha: 1 };
  if (u < 11.0) return { mode: 1, alpha: 1 - smooth(9.6, 11.0, u) };
  if (u < 11.2) return { mode: 2, alpha: 0 };
  if (u < 14.0) return { mode: 2, alpha: smooth(11.2, 14.0, u) };
  if (u < 14.6) return { mode: 2, alpha: 1 };
  return { mode: 2, alpha: 1 - smooth(14.6, 16.0, u) };
}

function scheduleTour(delay = 1400) {
  if (tourTimer) {
    clearTimeout(tourTimer);
    tourTimer = 0;
  }
  if (still.matches) return;
  tourTimer = window.setTimeout(() => {
    tourTimer = 0;
    if (still.matches || !state.closed) return;
    state.clock = 0;
    state.touring = true;
    state.last = 0;
    wake();
  }, delay);
}

function stopTour() {
  state.touring = false;
  if (tourTimer) {
    clearTimeout(tourTimer);
    tourTimer = 0;
  }
}

function interact() {
  stopTour();
  scheduleTour(3600);
}

/* ---------------- 状态切换 ---------------- */

function setMode(index, { fromTour = false } = {}) {
  const next = ((index % MODES.length) + MODES.length) % MODES.length;
  if (next === state.mode) return;
  state.mode = next;
  pointerState.modeTarget = MODES[next].angle;
  if (state.closed) traces[next] = Math.max(traces[next], state.alpha);
  if (!fromTour) {
    announce(`接法：${MODES[next].label}，等效电阻 ${MODES[next].r} 欧姆`);
  }
  wake();
}

function setAlpha(value, { immediate = false } = {}) {
  state.target = clamp01(value);
  if (immediate) state.alpha = state.target;
  wake();
}

function toggle() {
  state.closed = !state.closed;
  announce(`电源${state.closed ? "闭合" : "断开"}`);
  if (!state.closed) stopTour();
  else scheduleTour(1200);
  wake();
}

/* ---------------- 指针交互 ---------------- */

function toSvg(event) {
  const matrix = svg.getScreenCTM();
  if (!matrix) return null;
  const point = svg.createSVGPoint();
  point.x = event.clientX;
  point.y = event.clientY;
  return point.matrixTransform(matrix.inverse());
}

function distance2(point, target) {
  const dx = point.x - target.x;
  const dy = point.y - target.y;
  return dx * dx + dy * dy;
}

function hitAt(point) {
  if (distance2(point, DATA.KEY_HIT) <= DATA.KEY_HIT.r * DATA.KEY_HIT.r) return "key";
  if (distance2(point, DATA.MODE_HIT) <= DATA.MODE_HIT.r * DATA.MODE_HIT.r) return "mode";
  const box = DATA.RHEO_HIT;
  if (point.x >= box.x && point.x <= box.x + box.w && point.y >= box.y && point.y <= box.y + box.h) {
    return "rheostat";
  }
  return null;
}

function alphaFromPoint(point) {
  const [a, b] = DATA.RAIL;
  const vx = b[0] - a[0];
  const vy = b[1] - a[1];
  const length2 = vx * vx + vy * vy || 1;
  return clamp01(((point.x - a[0]) * vx + (point.y - a[1]) * vy) / length2);
}

function onPointerDown(event) {
  if (event.button !== undefined && event.button !== 0) return;
  const point = toSvg(event);
  if (!point) return;
  const hit = hitAt(point);
  interact();
  if (hit === "key") {
    event.preventDefault();
    toggle();
    return;
  }
  if (hit === "mode") {
    event.preventDefault();
    setMode((state.mode + 1) % MODES.length);
    return;
  }
  if (hit === "rheostat") {
    event.preventDefault();
    state.dragging = true;
    state.pointerId = event.pointerId;
    try {
      stage.setPointerCapture(event.pointerId);
    } catch {
      /* 某些浏览器在动画帧外捕获会失败 */
    }
    stage.style.cursor = "ew-resize";
    setAlpha(alphaFromPoint(point), { immediate: true });
  }
}

function onPointerMove(event) {
  const point = toSvg(event);
  if (!point) return;
  if (state.dragging && event.pointerId === state.pointerId) {
    event.preventDefault();
    setAlpha(alphaFromPoint(point), { immediate: true });
    return;
  }
  const hit = hitAt(point);
  stage.style.cursor = hit === "rheostat" ? "ew-resize" : hit ? "pointer" : "default";
}

function endDrag(event) {
  if (!state.dragging) return;
  state.dragging = false;
  if (event && state.pointerId !== null) {
    try {
      stage.releasePointerCapture(state.pointerId);
    } catch {
      /* 已释放 */
    }
  }
  state.pointerId = null;
  stage.style.cursor = "default";
}

/* ---------------- 键盘 ---------------- */

function onKey(event) {
  if (event.target !== stage) return;
  const step = event.shiftKey ? 0.1 : 0.025;
  let handled = true;
  if (event.key === "ArrowLeft" || event.key === "ArrowDown") {
    interact();
    setAlpha(state.target - step);
  } else if (event.key === "ArrowRight" || event.key === "ArrowUp") {
    interact();
    setAlpha(state.target + step);
  } else if (event.key === "Home") {
    interact();
    setAlpha(0);
  } else if (event.key === "End") {
    interact();
    setAlpha(1);
  } else if (event.key === "1" || event.key === "2" || event.key === "3") {
    interact();
    setMode(Number(event.key) - 1);
  } else if (event.key === " " || event.key === "Enter") {
    interact();
    toggle();
  } else {
    handled = false;
  }
  if (handled) event.preventDefault();
}

/* ---------------- 绘制 ---------------- */

function pointerPath(pivot, length, angle, z) {
  const a = (angle * Math.PI) / 180;
  return lineOnTop(
    [pivot[0], pivot[1]],
    [pivot[0] + Math.sin(a) * length, pivot[1] - Math.cos(a) * length],
    z,
    P,
  );
}

function tracePath(index, reach) {
  if (reach < 0.002) return "";
  const points = [];
  const count = 32;
  for (let step = 0; step <= count; step++) {
    const alpha = (step / count) * reach;
    const value = solve({ alpha, mode: MODES[index].id, closed: true });
    const point = chartPoint(value.U, value.I);
    points.push(iso([point.x, point.y, DATA.TRACE_Z], P));
  }
  return pathOf(points);
}

function traceEndPoint(index, reach) {
  const value = solve({ alpha: reach, mode: MODES[index].id, closed: true });
  const point = chartPoint(value.U, value.I);
  return iso([point.x, point.y, DATA.TRACE_Z + 0.02], P);
}

function draw(value) {
  if (carriage) {
    carriage.setAttribute("transform", translateAlong(P, [state.alpha * DATA.CARRIAGE_TRAVEL, 0, 0]));
  }

  METERS.forEach((meter, index) => {
    const el = needleEls.get(meter.key);
    if (!el) return;
    const segment = needleSegment([meter.cx, meter.cy], NEEDLE_R, needleState[index].value);
    el.setAttribute("d", lineOnTop(segment.tail, segment.tip, NEEDLE_Z, P));
  });

  if (modePointer) {
    modePointer.setAttribute(
      "d",
      pointerPath([DATA.MODE_PIVOT[0], DATA.MODE_PIVOT[1]], 6.8, pointerState.mode, DATA.MODE_PIVOT[2]),
    );
  }
  if (keyPointer) {
    keyPointer.setAttribute(
      "d",
      pointerPath([DATA.KEY_PIVOT[0], DATA.KEY_PIVOT[1]], 3.5, pointerState.key, DATA.KEY_PIVOT[2]),
    );
  }

  stage.toggleAttribute("data-on", state.closed);
  if (readout) readout.textContent = formatReadout(value);

  arms.forEach((arm, index) => {
    arm.style.display = index === state.mode ? "" : "none";
  });

  const end = chartPoint(SOURCE_V, SOURCE_V / MODES[state.mode].r);
  if (roller) {
    roller.setAttribute("transform", translateAlong(P, [0, end.y - DATA.ROLLER_BASE_Y, 0]));
  }
  if (pen) {
    const point = chartPoint(value.U, value.I);
    pen.setAttribute(
      "transform",
      translateAlong(P, [point.x - DATA.PEN_ORIGIN[0], point.y - DATA.PEN_ORIGIN[1], 0]),
    );
  }

  traceEls.forEach((el, index) => {
    const reach = traces[index];
    const d = tracePath(index, reach);
    el.setAttribute("d", d);
    el.style.display = d ? "" : "none";
    el.setAttribute("data-tone", index === state.mode ? "lit" : "lo");
    const dot = traceEnds[index];
    if (dot) {
      const point = traceEndPoint(index, reach);
      dot.setAttribute("cx", point[0].toFixed(1));
      dot.setAttribute("cy", point[1].toFixed(1));
      dot.style.display = reach > 0.002 ? "" : "none";
      dot.toggleAttribute("data-current", index === state.mode && reach > 0.002);
    }
  });
}

/* ---------------- 主循环 ---------------- */

function frame(time) {
  raf = 0;
  const dt = state.last ? Math.min(0.05, Math.max(0.001, (time - state.last) / 1000)) : 1 / 60;
  state.last = time;

  if (state.touring) {
    if (still.matches || !state.closed) {
      state.touring = false;
    } else {
      state.clock += dt;
      const target = tourAt(state.clock);
      if (target.mode !== state.mode) setMode(target.mode, { fromTour: true });
      state.target = target.alpha;
    }
  }

  state.alpha = follow(state.alpha, state.target, 0.16, dt);
  if (Math.abs(state.alpha - state.target) < 0.0003) state.alpha = state.target;

  const value = solve({ alpha: state.alpha, mode: MODES[state.mode].id, closed: state.closed });
  if (state.closed) {
    traces[state.mode] = Math.max(traces[state.mode], state.alpha);
  }

  METERS.forEach((meter, index) => {
    const target = meter.key === "A" ? value.needle.A : value.needle.V;
    const needle = needleState[index];
    needle.target = target;
    if (still.matches) {
      needle.value = target;
      needle.velocity = 0;
      return;
    }
    const steps = 4;
    const sub = dt / steps;
    for (let step = 0; step < steps; step++) {
      const acceleration = (needle.target - needle.value) * 130 - needle.velocity * 15;
      needle.velocity += acceleration * sub;
      needle.value += needle.velocity * sub;
    }
    if (needle.value < 0) {
      needle.value = 0;
      if (needle.velocity < 0) needle.velocity = 0;
    } else if (needle.value > 1) {
      needle.value = 1;
      if (needle.velocity > 0) needle.velocity = 0;
    }
    if (Math.abs(needle.value - needle.target) < 0.0005 && Math.abs(needle.velocity) < 0.002) {
      needle.value = needle.target;
      needle.velocity = 0;
    }
  });

  pointerState.mode = follow(pointerState.mode, pointerState.modeTarget, 0.12, dt);
  pointerState.key = follow(pointerState.key, pointerState.keyTarget, 0.12, dt);
  if (Math.abs(pointerState.mode - pointerState.modeTarget) < 0.02) pointerState.mode = pointerState.modeTarget;
  if (Math.abs(pointerState.key - pointerState.keyTarget) < 0.02) pointerState.key = pointerState.keyTarget;

  draw(value);

  const needleMoving = needleState.some(
    (needle) => Math.abs(needle.value - needle.target) > 0.0005 || Math.abs(needle.velocity) > 0.002,
  );
  const moving =
    state.touring ||
    Math.abs(state.alpha - state.target) > 0.0003 ||
    needleMoving ||
    Math.abs(pointerState.mode - pointerState.modeTarget) > 0.02 ||
    Math.abs(pointerState.key - pointerState.keyTarget) > 0.02;

  if (moving && visible) {
    raf = requestAnimationFrame(frame);
  } else {
    state.last = 0;
  }
}

function wake() {
  if (!visible || raf) return;
  raf = requestAnimationFrame(frame);
}

/* ---------------- 自适应脚手架 ---------------- */

function setMastery(value) {
  const p = Number(value);
  const known = Number.isFinite(p) ? clamp01(p) : null;
  const level = known === null ? "unknown" : known < 0.4 ? "low" : known < 0.75 ? "mid" : "high";
  stage.dataset.mastery = level;
  if (!hint) return;
  if (level === "low") hint.textContent = "先拖滑片，盯住电流表";
  else if (level === "mid") hint.textContent = "比较三条参考线的斜率";
  else if (level === "high") hint.textContent = "先预测，再拖到最右验证";
  else hint.textContent = "拖动滑片 · 1/2/3 换接法 · 空格开关";
}

/* ---------------- 自评写回 ---------------- */

checkButtons.forEach((button) => {
  button.addEventListener("click", () => {
    const correct = button.dataset.check === "yes";
    const value = solve({ alpha: state.alpha, mode: MODES[state.mode].id, closed: state.closed });
    send({
      type: "attempt",
      correct,
      reading: {
        U: Number(value.U.toFixed(3)),
        I: Number(value.I.toFixed(3)),
        R: value.R,
        mode: value.mode,
        alpha: Number(value.alpha.toFixed(3)),
        closed: value.closed,
      },
    });
    if (statusEl) {
      statusEl.textContent = embedded ? "已写回知识图谱" : "演示模式：在知识图谱中打开可写回";
    }
    checkButtons.forEach((item) => {
      item.disabled = true;
    });
    window.setTimeout(() => {
      checkButtons.forEach((item) => {
        item.disabled = false;
      });
    }, 1200);
  });
});

/* ---------------- 消息 ---------------- */

window.addEventListener("message", (event) => {
  if (embedded && event.source !== window.parent) return;
  if (ORIGIN !== "*" && event.origin !== ORIGIN) return;
  const data = event.data || {};
  if (data.type !== "mastery") return;
  if (data.kp_id && KP_ID && data.kp_id !== KP_ID) return;
  setMastery(data.p);
});

/* ---------------- 启动 ---------------- */

stage.addEventListener("pointerdown", onPointerDown);
stage.addEventListener("pointermove", onPointerMove);
stage.addEventListener("pointerup", endDrag);
stage.addEventListener("pointercancel", endDrag);
stage.addEventListener("pointerleave", (event) => {
  if (!state.dragging) stage.style.cursor = "default";
  if (event.buttons === 0) endDrag(event);
});
stage.addEventListener("keydown", onKey);
stage.addEventListener("focus", () => {
  announce(
    `欧姆定律实验台。${MODES[state.mode].label}接法，等效电阻 ${MODES[state.mode].r} 欧姆，电源${
      state.closed ? "闭合" : "断开"
    }。左右方向键调节滑片，数字 1、2、3 换接法，空格开关。`,
  );
});

if ("ResizeObserver" in window) {
  new ResizeObserver(reportHeight).observe(document.body);
}
window.addEventListener("resize", reportHeight);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
    visible = false;
  } else {
    visible = true;
    wake();
  }
});
if ("IntersectionObserver" in window) {
  new IntersectionObserver(
    ([entry]) => {
      visible = entry.isIntersecting;
      if (visible) wake();
      else if (raf) {
        cancelAnimationFrame(raf);
        raf = 0;
      }
    },
    { threshold: 0.04 },
  ).observe(stage);
}

draw(solve({ alpha: state.alpha, mode: MODES[state.mode].id, closed: state.closed }));
scheduleTour(1400);
wake();

requestAnimationFrame(() => {
  send({ type: "ready", figure: "ohm-lab" });
  reportHeight();
});
window.setTimeout(reportHeight, 160);
