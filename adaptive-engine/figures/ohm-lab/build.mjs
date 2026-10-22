/**
 * 生成「欧姆定律实验台」。
 *
 * 运行：node figures/ohm-lab/build.mjs
 * 依赖：本机的 Anatomy skill（默认 /root/.agents/skills/anatomy）。
 * 生成的是静态 HTML，运行时不需要 Node、LLM 或网络。
 */

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import {
  AMMETER_MAX,
  CHART,
  MODES,
  RHEOSTAT_MAX,
  SOURCE_V,
  VOLTMETER_MAX,
  chartPoint,
  clamp01,
  formatReadout,
  guideLine,
  needleAngle,
  needleSegment,
  solve,
} from "./model.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const SKILL = process.env.ANATOMY_HOME || "/root/.agents/skills/anatomy";
const k = await import(pathToFileURL(join(SKILL, "kit/iso-kit.mjs")).href);

const WIDTH = 640;
const HEIGHT = 460;
const INITIAL_ALPHA = 0.35;

const PLATE = { x: -155, y: -85, w: 310, d: 170, r: 12 };
const PLATE_H = 12;
const FOOT_H = 5;

const SUPPLY = { x: -148, y: -72, w: 66, d: 46, r: 4 };
const SUPPLY_H = 34;
const KEY = [-128, -50];
const LAMP = [-105, -50];
const FUSE = [-92, -34];
const SUPPLY_PLUS = [-136, -31];
const SUPPLY_MINUS = [-120, -31];

const RHEO = { x: -55, y: -58, w: 110, d: 30, r: 3 };
const RHEO_H = 10;
const RHEO_WIRE_Y = -46;
const RHEO_WIRE_Z = 22.2;
const RHEO_X0 = -38;
const RHEO_X1 = 38;
const RHEO_TERMINAL_L = [-46.5, -38];
const RHEO_TERMINAL_R = [46.5, -38];

const SHELF = { x: 63, y: -72, w: 82, d: 48, r: 4 };
const SHELF_H = 10;
const METERS = [
  { key: "A", cx: 83, cy: -50, r: 16, face: 24.55 },
  { key: "V", cx: 125, cy: -50, r: 16, face: 24.55 },
];
const NEEDLE_R = 17.2;
const NEEDLE_Z = 24.58;
const METER_TERMINALS = [
  [75, -29],
  [95, -29],
  [113, -29],
  [133, -29],
];

const LOAD = { x: -112, y: 18, w: 96, d: 44, r: 4 };
const LOAD_H = 8;
const RESISTORS = [
  { x: -88, y: 42, label: "R1" },
  { x: -58, y: 42, label: "R2" },
];
const RES_R = 5.5;
const MODE = [-28, 42];
const LOAD_TERMINALS = [
  [-104, 24],
  [-96, 24],
  [-32, 24],
  [-24, 24],
];

const BODY = { x: 50, y: 14, w: 96, d: 52, r: 4 };
const BODY_H = 10;
const PAPER = { x: 56, y: 20, w: 84, d: 40, r: 1 };
const PAPER_Z = 10.5;
const TRACE_Z = PAPER_Z + 0.14;
const PLOT = CHART;
const ROLLER_BASE_Y = 40;
const PEN_ORIGIN = [PLOT.x0, PLOT.y0];
const MODE_Z = 17.25;
const KEY_Z = 41.15;

const PROJECTION_POINTS = [
  ...k.boxCorners(PLATE, -PLATE_H - FOOT_H, 0),
  ...k.boxCorners(SUPPLY, 0, 42),
  ...k.boxCorners(RHEO, 0, 33),
  ...k.boxCorners(SHELF, 0, 25),
  ...k.boxCorners(LOAD, 0, 34),
  ...k.boxCorners(BODY, 0, 17.5),
  [PLOT.x0 + PLOT.w, PLOT.y0, 18],
  [LAMP[0], LAMP[1], 39],
  [KEY[0], KEY[1], 42],
];
const P = k.fitProjection(PROJECTION_POINTS, WIDTH, HEIGHT, {
  pad: 38,
  azimuth: 42,
  elevation: 32,
});

const at = (point) => k.iso(point, P);
const S = (paths, style) => k.solidSvg(paths, style);
const L = (d, style = {}) => k.lineSvg(d, style);
const D = (points, style = {}) => k.dotsSvg(points, style);
const TOP = (from, to, z) => k.onTop([from, to], z, P);
const PLAN = (plan, z, steps = 8) => k.onTop(k.ringOf(plan, steps), z, P, true);

function inset(plan, by) {
  return k.insetPlan(plan, by);
}

function screw(x, y, z, r = 2.1, tone = "mid") {
  return `${L(k.ring(x, y, r, z, P, 28), { tone, className: "ohm-screw" })}${D(
    [at([x, y, z])],
    { size: 0.75, tone: "lo" },
  )}`;
}

function plusMark(x, y, z, tone = "mid") {
  return `${L(TOP([x - 1.1, y], [x + 1.1, y], z), { tone, free: true })}${L(
    TOP([x, y - 1.1], [x, y + 1.1], z),
    { tone, free: true },
  )}`;
}

function minusMark(x, y, z, tone = "mid") {
  return L(TOP([x - 1.2, y], [x + 1.2, y], z), { tone, free: true });
}

function text(x, y, z, value, { size = 3.2, tone = "mid", anchor = "middle" } = {}) {
  return k.faceTextSvg(k.topMatrix([x, y], z, P, "x"), value, { size, tone, anchor });
}

/* 站在屏幕坐标上的标签：不跟随等距平面，保证坐标轴刻度可读 */
function screenText(x, y, z, value, { size = 4.8, tone = "hi", anchor = "middle" } = {}) {
  const [sx, sy] = at([x, y, z]);
  return `<text class="iso-face-text ohm-screen-text" data-tone="${tone}" x="${sx.toFixed(1)}" y="${sy.toFixed(1)}" font-size="${size}" text-anchor="${anchor}">${value}</text>`;
}

function pointerD([cx, cy], length, angle, z) {
  const a = (angle * Math.PI) / 180;
  return k.lineOnTop([cx, cy], [cx + Math.sin(a) * length, cy - Math.cos(a) * length], z, P);
}

function boxScreen(plan, z0, z1, pad = 0) {
  const points = k.boxCorners(plan, z0, z1).map(at);
  const xs = points.map(([x]) => x);
  const ys = points.map(([, y]) => y);
  return {
    x: Math.min(...xs) - pad,
    y: Math.min(...ys) - pad,
    w: Math.max(...xs) - Math.min(...xs) + pad * 2,
    h: Math.max(...ys) - Math.min(...ys) + pad * 2,
  };
}

/* ------------------------------------------------------------------ */
/* 底座                                                                */
/* ------------------------------------------------------------------ */

function buildBase() {
  const out = [];
  out.push(
    S(k.slabOf(PLATE, -PLATE_H - FOOT_H, PLATE_H + FOOT_H, P, 12, 0.8), {
      tone: "lo",
      halo: true,
      crease: "faint",
    }),
  );
  for (const [x, y] of [
    [-138, -70],
    [138, -70],
    [-138, 70],
    [138, 70],
  ]) {
    out.push(S(k.cylinder(x, y, 5.2, -17, 5, P, 24, 0.5), { tone: "mid" }));
    out.push(S(k.cylinder(x, y, 3.4, -18.8, 1.8, P, 20, 0.2), { tone: "lo" }));
  }
  out.push(S(k.slabOf(PLATE, -PLATE_H, PLATE_H, P, 12, 0.6), { tone: "hi", className: "ohm-base" }));
  out.push(L(PLAN(PLATE, 0.06, 12), { tone: "lo" }));
  out.push(L(PLAN(inset(PLATE, 11), 0.08, 12), { tone: "faint" }));
  for (const [x, y] of [
    [-142, -74],
    [142, -74],
    [-142, 74],
    [142, 74],
  ]) {
    out.push(screw(x, y, 0.1, 2.4, "mid"));
  }

  // 四角的定位刻线，让大面积底板有仪表台的秩序
  for (const [x, y, sx, sy] of [
    [-136, -66, 1, 1],
    [136, -66, -1, 1],
    [-136, 66, 1, -1],
    [136, 66, -1, -1],
  ]) {
    out.push(L(TOP([x, y], [x + sx * 9, y], 0.09), { tone: "faint" }));
    out.push(L(TOP([x, y], [x, y + sy * 9], 0.09), { tone: "faint" }));
  }

  // 模块铭牌：把物体和它代表的知识点对上
  out.push(text(-115, -10, 0.12, "电源", { size: 6.4, tone: "hi" }));
  out.push(text(0, -10, 0.12, "滑线变阻器", { size: 6.4, tone: "hi" }));
  out.push(text(-64, 67, 0.12, "负载", { size: 6.4, tone: "hi" }));
  out.push(text(98, 71.5, 0.12, "记录仪", { size: 6.4, tone: "hi" }));

  const LABEL = { x: -146, y: 70, w: 70, d: 10, r: 1.4 };
  out.push(S(k.slabOf(LABEL, 0.1, 1.2, P, 5, 0.2), { tone: "hi", crease: "none" }));
  out.push(L(PLAN(LABEL, 1.3, 5), { tone: "mid" }));
  out.push(text(-111, 75, 1.35, "U = I R", { size: 6.2, tone: "hi" }));
  out.push(text(-111, 78.9, 1.35, "6 V · 0–20 Ω", { size: 3.8, tone: "mid" }));
  out.push(screw(-142, 75, 1.3, 1.25, "mid"));
  out.push(screw(-80, 75, 1.3, 1.25, "mid"));
  return out.join("");
}

/* ------------------------------------------------------------------ */
/* 电源                                                                */
/* ------------------------------------------------------------------ */

function buildSupply() {
  const out = [];
  out.push(S(k.slabOf(SUPPLY, 0, 4, P, 6, 0.4), { tone: "lo" }));
  out.push(S(k.slabOf(SUPPLY, 4, SUPPLY_H - 4, P, 6, 0.9), { tone: "mid" }));
  out.push(L(k.sideSeam(SUPPLY, 26, P, 6), { tone: "faint" }));
  out.push(S(k.slabOf(inset(SUPPLY, 4), SUPPLY_H, 0.55, P, 4, 0.2), { tone: "hi", crease: "none" }));
  out.push(L(PLAN(inset(SUPPLY, 5), SUPPLY_H + 0.55, 4), { tone: "lo" }));

  for (const [x, y] of [
    [-144, -68],
    [-86, -68],
    [-86, -30],
  ]) {
    out.push(screw(x, y, SUPPLY_H + 0.6, 2.0, "lo"));
  }

  out.push(D(k.dotGrid(-100, -67, 4, 2, 3.1, SUPPLY_H + 0.65, P), { size: 0.6, tone: "mid" }));
  out.push(text(-143, -58, SUPPLY_H + 0.7, "6 V DC", { size: 4.6, tone: "hi", anchor: "start" }));

  // 启动钥匙
  out.push(S(k.cylinder(KEY[0], KEY[1], 6.1, SUPPLY_H, 1.9, P, 32, 0.35), { tone: "mid" }));
  out.push(S(k.cylinder(KEY[0], KEY[1], 4.3, SUPPLY_H + 1.9, 4.9, P, 28, 0.45), { tone: "hi", lit: true }));
  out.push(
    L(
      k.knurl(KEY[0], KEY[1], 4.3, SUPPLY_H + 2.3, SUPPLY_H + 6.4, 14, P, 0.35),
      { tone: "lo" },
    ),
  );
  out.push(L(k.ring(KEY[0], KEY[1], 3.5, SUPPLY_H + 6.85, P, 28), { tone: "mid" }));
  out.push(
    L(pointerD(KEY, 3.5, 45, KEY_Z), {
      tone: "lit",
      className: "ohm-pointer ohm-key-pointer",
      free: "end",
    }),
  );

  // 指示灯：先画光晕，再画灯罩
  const glow = at([LAMP[0], LAMP[1], 38.6]);
  out.push(
    `<circle class="ohm-glow" data-glow="" cx="${glow[0].toFixed(1)}" cy="${glow[1].toFixed(1)}" r="15" fill="url(#ohm-lamp-glow)"/>`,
  );
  out.push(S(k.cylinder(LAMP[0], LAMP[1], 4.1, SUPPLY_H, 1.9, P, 28, 0.3), { tone: "mid" }));
  out.push(S(k.cylinder(LAMP[0], LAMP[1], 3.1, SUPPLY_H + 1.9, 2.7, P, 28, 0.6), { tone: "hi", lit: true }));
  out.push(L(k.ring(LAMP[0], LAMP[1], 3.5, SUPPLY_H + 4.65, P, 28), { tone: "mid" }));
  const lampTop = at([LAMP[0], LAMP[1], SUPPLY_H + 4.7]);
  out.push(`<circle cx="${lampTop[0].toFixed(1)}" cy="${lampTop[1].toFixed(1)}" r="2.25" fill="var(--ohm-glow-core)" opacity=".72"/>`);

  // 保险丝
  out.push(S(k.cylinder(FUSE[0], FUSE[1], 2.8, SUPPLY_H, 5.6, P, 24, 0.3), { tone: "hi" }));
  out.push(L(k.sideArc(FUSE[0], FUSE[1], 2.8, SUPPLY_H + 2.8, P, 20), { tone: "faint" }));
  out.push(S(k.cylinder(FUSE[0], FUSE[1], 3.2, SUPPLY_H + 5.6, 1.4, P, 24, 0.2), { tone: "mid" }));
  out.push(L(k.ring(FUSE[0], FUSE[1], 3.2, SUPPLY_H + 7.0, P, 24), { tone: "lo" }));

  // 输出接线柱
  for (const [x, y, plus] of [
    [SUPPLY_PLUS[0], SUPPLY_PLUS[1], true],
    [SUPPLY_MINUS[0], SUPPLY_MINUS[1], false],
  ]) {
    out.push(S(k.cylinder(x, y, 2.7, SUPPLY_H, 4.0, P, 24, 0.3), { tone: "mid" }));
    out.push(L(k.ring(x, y, 3.3, SUPPLY_H + 4.0, P, 24), { tone: plus ? "red" : "hi" }));
    out.push(D([at([x, y, SUPPLY_H + 4.05])], { size: 0.9, tone: "hi" }));
    out.push(plus ? plusMark(x, y, SUPPLY_H + 4.15, "red") : minusMark(x, y, SUPPLY_H + 4.15, "hi"));
  }

  return out.join("");
}

/* ------------------------------------------------------------------ */
/* 滑线变阻器                                                          */
/* ------------------------------------------------------------------ */

function buildRheostat() {
  const out = [];
  out.push(S(k.slabOf(RHEO, 0, RHEO_H, P, 6, 0.5), { tone: "mid" }));
  out.push(S(k.slabOf(inset(RHEO, 3), RHEO_H, 0.5, P, 4, 0.15), { tone: "hi", crease: "none" }));
  out.push(L(PLAN(inset(RHEO, 4), RHEO_H + 0.5, 4), { tone: "faint" }));
  for (const [x, y] of [
    [-51, -55],
    [51, -55],
    [-51, -31],
    [51, -31],
  ]) {
    out.push(screw(x, y, RHEO_H + 0.55, 1.6, "lo"));
  }

  // 两端瓷座
  for (const x of [-51, 51]) {
    out.push(
      S(k.slabOf({ x: x - 4, y: -54, w: 8, d: 14, r: 1.5 }, RHEO_H, 14, P, 4, 0.4), {
        tone: "mid",
      }),
    );
    out.push(S(k.slabOf({ x: x - 3.2, y: -53.2, w: 6.4, d: 12.4, r: 1.2 }, RHEO_H + 14, 0.5, P, 4, 0.1), {
      tone: "hi",
      crease: "none",
    }));
    out.push(L(k.sideSeam({ x: x - 4, y: -54, w: 8, d: 14, r: 1.5 }, RHEO_H + 9, P, 4), { tone: "faint" }));
  }

  // 电阻丝与两端的瓷套管
  out.push(L(k.lineOnTop([-46.5, RHEO_WIRE_Y], [46.5, RHEO_WIRE_Y], RHEO_WIRE_Z, P), { tone: "hi" }));
  for (const x of [-46.5, 46.5]) {
    out.push(S(k.cylinder(x, RHEO_WIRE_Y, 2.5, RHEO_WIRE_Z - 0.6, 1.2, P, 20, 0.2), { tone: "mid" }));
    out.push(L(k.ring(x, RHEO_WIRE_Y, 2.5, RHEO_WIRE_Z + 0.6, P, 20), { tone: "hi" }));
  }

  // 刻度条
  const SCALE = { x: -46, y: -36, w: 92, d: 7, r: 1 };
  out.push(S(k.slabOf(SCALE, RHEO_H, 0.55, P, 4, 0.1), { tone: "hi", crease: "none" }));
  const ticks = k.topTicks(-42, 42, 4, 5, -35.5, RHEO_H + 0.6, [1.2, 2.5], P);
  out.push(L(ticks.minor, { tone: "mid" }));
  out.push(L(ticks.major, { tone: "hi" }));
  out.push(text(-43, -32.2, RHEO_H + 0.62, "0", { size: 4.0, tone: "hi" }));
  out.push(text(43, -32.2, RHEO_H + 0.62, "20", { size: 4.0, tone: "hi" }));

  // 接线柱
  for (const [x, y] of [RHEO_TERMINAL_L, RHEO_TERMINAL_R]) {
    out.push(S(k.slabOf({ x: x - 1.5, y: -46, w: 3, d: 8, r: 0.4 }, RHEO_H, 0.45, P, 3, 0.1), { tone: "hi", crease: "none" }));
    out.push(S(k.cylinder(x, y, 2.5, RHEO_H, 3.1, P, 24, 0.25), { tone: "mid" }));
    out.push(L(k.ring(x, y, 3.0, RHEO_H + 3.1, P, 24), { tone: "hi" }));
    out.push(D([at([x, y, RHEO_H + 3.15])], { size: 0.8, tone: "lo" }));
  }

  // 滑片组件：整组沿 x 平移
  const carriage = [];
  carriage.push(S(k.slabOf({ x: -45, y: -52, w: 14, d: 14, r: 2 }, 22, 5, P, 4, 0.45), { tone: "hi", lit: true }));
  carriage.push(S(k.slabOf({ x: -43, y: -50, w: 10, d: 10, r: 1.4 }, 27, 0.4, P, 4, 0.1), { tone: "mid", crease: "none" }));
  carriage.push(D([at([-42, -49, 27.5]), at([-34, -49, 27.5])], { size: 0.55, tone: "lo" }));
  carriage.push(S(k.slabOf({ x: -43.2, y: -47.2, w: 2.4, d: 2.4, r: 0.5 }, 10.6, 11.4, P, 3, 0.15), { tone: "mid" }));
  carriage.push(S(k.slabOf({ x: -44.4, y: -48.4, w: 4.8, d: 4.8, r: 1.2 }, 21.3, 1.1, P, 4, 0.2), { tone: "hi", lit: true }));
  // 指向刻度条的机械指针：一根细柱加一条压在刻度上的横片
  carriage.push(S(k.slabOf({ x: -38.5, y: -39.0, w: 1.0, d: 1.2, r: 0.2 }, 10.65, 11.5, P, 3, 0.05), { tone: "mid" }));
  carriage.push(S(k.slabOf({ x: -38.8, y: -38.0, w: 1.6, d: 9.2, r: 0.3 }, 10.65, 0.55, P, 2, 0.05), { tone: "hi", crease: "none" }));
  carriage.push(S(k.cylinder(-38, -46, 4.25, 27, 4.5, P, 28, 0.4), { tone: "hi", lit: true }));
  carriage.push(L(k.knurl(-38, -46, 4.25, 27.4, 31.1, 16, P, 0.35), { tone: "lo" }));
  carriage.push(L(k.ring(-38, -46, 3.55, 31.55, P, 24), { tone: "mid" }));
  carriage.push(
    L(pointerD([-38, -46], 3.55, 90, 31.7), {
      tone: "lit",
      className: "ohm-pointer",
      free: "end",
    }),
  );
  out.push(`<g class="ohm-carriage" data-carriage="" transform="${k.translateAlong(P, [INITIAL_ALPHA * (RHEO_X1 - RHEO_X0), 0, 0])}">${carriage.join("")}</g>`);

  return out.join("");
}

/* ------------------------------------------------------------------ */
/* 双表                                                                */
/* ------------------------------------------------------------------ */

function buildMeters() {
  const out = [];
  out.push(S(k.slabOf(SHELF, 0, SHELF_H, P, 6, 0.5), { tone: "mid" }));
  out.push(S(k.slabOf(inset(SHELF, 3), SHELF_H, 0.5, P, 4, 0.15), { tone: "hi", crease: "none" }));
  out.push(L(PLAN(inset(SHELF, 4), SHELF_H + 0.5, 4), { tone: "faint" }));
  for (const [x, y] of [
    [67, -68],
    [141, -68],
    [67, -28],
    [141, -28],
  ]) {
    out.push(screw(x, y, SHELF_H + 0.55, 1.5, "lo"));
  }

  for (const meter of METERS) {
    const { cx, cy, r } = meter;
    out.push(S(k.cylinder(cx, cy, 7, SHELF_H, 4, P, 32, 0.35), { tone: "mid" }));
    out.push(S(k.cylinder(cx, cy, r, SHELF_H + 4, 10, P, 64, 0.8), { tone: "hi", lit: true }));
    out.push(L(k.sideArc(cx, cy, r, SHELF_H + 8.5, P, 40), { tone: "mid" }));
    out.push(S(k.cylinder(cx, cy, r - 1.8, SHELF_H + 14, 0.55, P, 64, 0.25), { tone: "hi", lit: true }));
    out.push(L(k.ring(cx, cy, r - 1.8, SHELF_H + 14.55, P, 64), { tone: "mid" }));
    out.push(L(k.ring(cx, cy, r - 3.4, SHELF_H + 14.58, P, 64), { tone: "hi" }));
    out.push(L(k.ring(cx, cy, 13.4, SHELF_H + 14.6, P, 64), { tone: "mid" }));
    const ticks = k.radialTicks(cx, cy, 13.4, 24, 4, SHELF_H + 14.6, [1.1, 2.4], P, (-120 * Math.PI) / 180, (240 * Math.PI) / 180);
    out.push(L(ticks.minor, { tone: "mid" }));
    out.push(L(ticks.major, { tone: "hi" }));
    const labels = meter.key === "A" ? ["0", "0.6", "1.2"] : ["0", "3", "6"];
    labels.forEach((label, index) => {
      const angle = (-120 + 120 * index) * Math.PI / 180;
      const x = cx + Math.sin(angle) * 10.7;
      const y = cy - Math.cos(angle) * 10.7;
      out.push(text(x, y, SHELF_H + 14.63, label, { size: 4.0, tone: "hi" }));
    });
    out.push(L(k.ring(cx, cy, 2.4, SHELF_H + 14.62, P, 28), { tone: "hi" }));
    out.push(D([at([cx, cy, SHELF_H + 14.65])], { size: 0.7, tone: "hi" }));
    out.push(S(k.cylinder(cx, cy + 13, 1.35, SHELF_H + 14.55, 0.8, P, 16, 0.1), { tone: "mid" }));
    out.push(L(TOP([cx - 1, cy + 13], [cx + 1, cy + 13], SHELF_H + 15.35), { tone: "mid", free: true }));
    out.push(text(cx, cy + 8.5, SHELF_H + 14.63, meter.key, { size: 5.6, tone: "hi" }));
    const initial = solve({ alpha: INITIAL_ALPHA, mode: "single", closed: true });
    const fraction = meter.key === "A" ? initial.needle.A : initial.needle.V;
    const seg = needleSegment([cx, cy], NEEDLE_R, fraction);
    out.push(
      `<path class="iso-line ohm-needle" data-tone="lit" data-needle="${meter.key}" data-free="" d="${k.lineOnTop(seg.tail, seg.tip, NEEDLE_Z, P)}"/>`,
    );
  }

  for (const [x, y] of METER_TERMINALS) {
    out.push(S(k.cylinder(x, y, 2.4, SHELF_H, 4, P, 20, 0.25), { tone: "mid" }));
    out.push(L(k.ring(x, y, 2.9, SHELF_H + 4, P, 20), { tone: "hi" }));
    out.push(D([at([x, y, SHELF_H + 4.05])], { size: 0.8, tone: "hi" }));
  }

  return out.join("");
}

/* ------------------------------------------------------------------ */
/* 负载：两个 10Ω 电阻 + 接法旋钮                                      */
/* ------------------------------------------------------------------ */

function buildLoad() {
  const out = [];
  out.push(S(k.slabOf(LOAD, 0, LOAD_H, P, 6, 0.5), { tone: "mid" }));
  out.push(S(k.slabOf(inset(LOAD, 3), LOAD_H, 0.5, P, 4, 0.15), { tone: "hi", crease: "none" }));
  out.push(L(PLAN(inset(LOAD, 4), LOAD_H + 0.5, 4), { tone: "faint" }));
  for (const [x, y] of [
    [-108, 22],
    [-20, 22],
    [-108, 58],
    [-20, 58],
  ]) {
    out.push(screw(x, y, LOAD_H + 0.55, 1.5, "mid"));
  }

  for (const resistor of RESISTORS) {
    const { x, y, label } = resistor;
    out.push(S(k.slabOf({ x: x - 9, y: y - 7, w: 18, d: 14, r: 2 }, LOAD_H, 3, P, 4, 0.25), { tone: "mid" }));
    out.push(S(k.cylinder(x, y, RES_R, LOAD_H + 3, 18, P, 36, 0.45), { tone: "hi", lit: true }));
    out.push(S(k.cylinder(x, y, RES_R + 0.5, LOAD_H + 3, 2.2, P, 36, 0.2), { tone: "mid" }));
    out.push(S(k.cylinder(x, y, RES_R + 0.5, LOAD_H + 19, 2.2, P, 36, 0.2), { tone: "mid" }));
    out.push(S(k.cylinder(x, y, 2.1, LOAD_H + 21.2, 1.6, P, 20, 0.15), { tone: "lo" }));
    for (const [z, tone] of [
      [LOAD_H + 7, "mid"],
      [LOAD_H + 9.5, "hi"],
      [LOAD_H + 12, "mid"],
      [LOAD_H + 16, "hi"],
    ]) {
      out.push(L(k.sideArc(x, y, RES_R + 0.55, z, P, 26), { tone }));
    }
    out.push(L(k.sideSeam({ x: x - 9, y: y - 7, w: 18, d: 14, r: 2 }, LOAD_H + 1.4, P, 4), { tone: "faint" }));
    out.push(text(x, y - 10, LOAD_H + 0.65, label, { size: 4.4, tone: "mid" }));
  }

  out.push(text(-68, 57, LOAD_H + 0.6, "2 × 10 Ω", { size: 4.8, tone: "mid" }));
  out.push(text(-28, 56.5, LOAD_H + 0.6, "接法", { size: 4.4, tone: "mid" }));

  out.push(S(k.cylinder(MODE[0], MODE[1], 10, LOAD_H, 3, P, 40, 0.4), { tone: "mid" }));
  out.push(S(k.cylinder(MODE[0], MODE[1], 9, LOAD_H + 3, 6, P, 40, 0.5), { tone: "hi", lit: true }));
  out.push(L(k.knurl(MODE[0], MODE[1], 9, LOAD_H + 3.3, LOAD_H + 8.6, 20, P, 0.4), { tone: "mid" }));
  out.push(L(k.ring(MODE[0], MODE[1], 7.4, LOAD_H + 9.1, P, 36), { tone: "hi" }));
  for (const angle of [-120, 0, 120]) {
    const a = (angle * Math.PI) / 180;
    out.push(
      L(
        k.lineOnTop(
          [MODE[0] + Math.sin(a) * 10.2, MODE[1] - Math.cos(a) * 10.2],
          [MODE[0] + Math.sin(a) * 12.4, MODE[1] - Math.cos(a) * 12.4],
          LOAD_H + 0.58,
          P,
        ),
        { tone: "hi" },
      ),
    );
  }
  out.push(
    L(pointerD(MODE, 6.8, -120, MODE_Z), {
      tone: "lit",
      className: "ohm-pointer ohm-mode-pointer",
      free: "end",
    }),
  );

  for (const [x, y] of LOAD_TERMINALS) {
    out.push(S(k.cylinder(x, y, 2.5, LOAD_H, 4.5, P, 20, 0.25), { tone: "mid" }));
    out.push(L(k.ring(x, y, 3.0, LOAD_H + 4.5, P, 20), { tone: "hi" }));
    out.push(D([at([x, y, LOAD_H + 4.55])], { size: 0.8, tone: "hi" }));
  }

  return out.join("");
}

/* ------------------------------------------------------------------ */
/* 记录仪                                                              */
/* ------------------------------------------------------------------ */

function ruleRing(a, b, width = 1.05) {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const len = Math.hypot(dx, dy) || 1;
  const nx = (-dy / len) * width;
  const ny = (dx / len) * width;
  return [
    [a[0] + nx, a[1] + ny],
    [b[0] + nx, b[1] + ny],
    [b[0] - nx, b[1] - ny],
    [a[0] - nx, a[1] - ny],
  ];
}

function tracePathFor(modeIndex, reach) {
  if (reach < 0.002) return "";
  const points = [];
  const count = 36;
  for (let index = 0; index <= count; index++) {
    const alpha = (index / count) * reach;
    const state = solve({ alpha, mode: MODES[modeIndex].id, closed: true });
    const point = chartPoint(state.U, state.I);
    points.push(at([point.x, point.y, TRACE_Z]));
  }
  return k.pathOf(points);
}

function traceEndFor(modeIndex, reach) {
  const state = solve({ alpha: reach, mode: MODES[modeIndex].id, closed: true });
  const point = chartPoint(state.U, state.I);
  return at([point.x, point.y, TRACE_Z + 0.02]);
}

function buildChart() {
  const out = [];
  out.push(S(k.slabOf(BODY, 0, BODY_H, P, 6, 0.6), { tone: "mid", className: "ohm-chart-body" }));
  out.push(S(k.slabOf(inset(BODY, 3), BODY_H, 0.5, P, 4, 0.15), { tone: "hi", crease: "none", className: "ohm-chart-body" }));
  out.push(L(PLAN(inset(BODY, 4), BODY_H + 0.5, 4), { tone: "mid" }));
  for (const [x, y] of [
    [54, 18],
    [142, 18],
    [54, 62],
    [142, 62],
  ]) {
    out.push(screw(x, y, BODY_H + 0.55, 1.6, "mid"));
  }

  // 纸卷
  out.push(S(k.cylinder(58, 17, 6.0, BODY_H, 9.0, P, 28, 0.35), { tone: "mid" }));
  out.push(L(k.sideArc(58, 17, 6.0, BODY_H + 4.5, P, 24), { tone: "mid" }));
  out.push(S(k.cylinder(58, 17, 2.4, BODY_H + 9.0, 0.9, P, 20, 0.1), { tone: "hi" }));

  // 纸
  out.push(S(k.slabOf(PAPER, PAPER_Z, 0.5, P, 3, 0.08), { tone: "hi", crease: "none" }));
  out.push(`<path class="ohm-paper-sheet" d="${PLAN(PAPER, PAPER_Z + 0.51, 3)}"/>`);
  out.push(L(PLAN(PAPER, PAPER_Z + 0.52, 3), { tone: "mid" }));

  // 绘图区边框
  const border = [
    [PLOT.x0, PLOT.y0],
    [PLOT.x0 + PLOT.w, PLOT.y0],
    [PLOT.x0 + PLOT.w, PLOT.y0 - PLOT.h],
    [PLOT.x0, PLOT.y0 - PLOT.h],
  ];
  out.push(L(k.onTop(border, PAPER_Z + 0.54, P, true), { tone: "hi" }));

  // 网格
  for (let u = 1; u < 6; u++) {
    const x = PLOT.x0 + (u / 6) * PLOT.w;
    out.push(L(k.lineOnTop([x, PLOT.y0 - PLOT.h], [x, PLOT.y0], PAPER_Z + 0.53, P), { tone: "faint" }));
    out.push(L(k.lineOnTop([x, PLOT.y0], [x, PLOT.y0 + 2.0], PAPER_Z + 0.53, P), { tone: "mid", free: "end" }));
  }
  for (let i = 1; i < 6; i++) {
    const y = PLOT.y0 - (i / 6) * PLOT.h;
    out.push(L(k.lineOnTop([PLOT.x0, y], [PLOT.x0 + PLOT.w, y], PAPER_Z + 0.53, P), { tone: "faint" }));
    out.push(L(k.lineOnTop([PLOT.x0, y], [PLOT.x0 - 2.0, y], PAPER_Z + 0.53, P), { tone: "mid", free: "end" }));
  }

  // 三条参考线：都落在绘图区边框上
  for (const mode of MODES) {
    const [a, b] = guideLine(mode.id);
    out.push(
      L(k.lineOnTop([a.x, a.y], [b.x, b.y], PAPER_Z + 0.56, P), {
        tone: "mid",
        dotted: true,
        className: "ohm-guide",
      }),
    );
  }

  // 三条笔臂：只显示当前接法
  for (let index = 0; index < MODES.length; index++) {
    const end = guideLine(MODES[index].id)[1];
    const ring = ruleRing(PEN_ORIGIN, [end.x, end.y], 1.05);
    const paths = k.extrude(ring, 11.25, 1.15, P, { convex: true, bevel: 0.15 });
    const arm = `<g class="iso-solid ohm-arm-rule"><path class="iso-fill ohm-arm-fill" d="${paths.fill}"/><path class="iso-top ohm-arm-top" d="${paths.top}"/><path class="iso-line iso-edge" data-tone="hi" d="${paths.outline}"/></g>`;
    const slot = L(k.lineOnTop(PEN_ORIGIN, [end.x, end.y], 12.48, P), {
      tone: "mid",
      className: "ohm-arm-slot",
    });
    out.push(`<g data-arm="${index}"${index === 0 ? ' data-current=""' : ' style="display:none"'}>${arm}${slot}</g>`);
  }

  // 已记录的线段
  for (let index = 0; index < MODES.length; index++) {
    const reach = index === 0 ? INITIAL_ALPHA : 0;
    const d = tracePathFor(index, reach);
    const tone = index === 0 ? "lit" : "mid";
    out.push(`<path class="iso-line ohm-trace" data-tone="${tone}" data-trace="${index}" d="${d}"/>`);
  }

  // 原点轴套
  out.push(S(k.cylinder(PEN_ORIGIN[0], PEN_ORIGIN[1], 2.8, PAPER_Z, 2.4, P, 24, 0.3), { tone: "mid" }));
  out.push(L(k.ring(PEN_ORIGIN[0], PEN_ORIGIN[1], 3.4, PAPER_Z + 2.4, P, 24), { tone: "hi" }));
  out.push(D([at([PEN_ORIGIN[0], PEN_ORIGIN[1], PAPER_Z + 2.45])], { size: 0.8, tone: "hi" }));

  // 右端导轨与滚轮
  out.push(S(k.slabOf({ x: PLOT.x0 + PLOT.w - 0.8, y: PLOT.y0 - PLOT.h, w: 1.6, d: PLOT.h, r: 0.8 }, PAPER_Z, 1.4, P, 3, 0.1), { tone: "mid" }));
  const roller = [];
  roller.push(S(k.cylinder(PLOT.x0 + PLOT.w, ROLLER_BASE_Y, 2.5, 11.2, 3.2, P, 24, 0.3), { tone: "mid" }));
  roller.push(L(k.knurl(PLOT.x0 + PLOT.w, ROLLER_BASE_Y, 2.5, 11.5, 14.1, 12, P, 0.3), { tone: "mid" }));
  roller.push(L(k.ring(PLOT.x0 + PLOT.w, ROLLER_BASE_Y, 2.9, 14.4, P, 24), { tone: "hi" }));
  out.push(`<g class="ohm-roller" data-roller="">${roller.join("")}</g>`);

  // 记录笔：笔架 + 笔杆 + 笔尖
  const pen = [];
  pen.push(S(k.slabOf({ x: -3.8, y: -3.8, w: 7.6, d: 7.6, r: 1.8 }, 12.5, 3.4, P, 4, 0.3), { tone: "hi", lit: true }));
  pen.push(S(k.cylinder(0, 0, 2.6, 15.9, 2.3, P, 24, 0.25), { tone: "mid" }));
  pen.push(L(k.knurl(0, 0, 2.6, 16.2, 17.9, 12, P, 0.25), { tone: "mid" }));
  pen.push(S(k.cylinder(0, 0, 0.65, 10.7, 1.8, P, 16, 0.05), { tone: "hi" }));
  pen.push(S(k.cylinder(0, 0, 1.15, 10.55, 0.25, P, 16, 0.05), { tone: "hi", lit: true }));
  const initial = solve({ alpha: INITIAL_ALPHA, mode: "single", closed: true });
  const initialPoint = chartPoint(initial.U, initial.I);
  out.push(
    `<g class="ohm-pen" data-pen="" transform="${k.translateAlong(P, [initialPoint.x - PEN_ORIGIN[0], initialPoint.y - PEN_ORIGIN[1], 0])}">${pen.join("")}</g>`,
  );

  // 记录终点的小点
  for (let index = 0; index < MODES.length; index++) {
    const reach = index === 0 ? INITIAL_ALPHA : 0;
    const point = traceEndFor(index, reach);
    out.push(
      `<circle class="ohm-trace-end" data-trace-end="${index}"${index === 0 ? ' data-current=""' : ' style="display:none"'} cx="${point[0].toFixed(1)}" cy="${point[1].toFixed(1)}" r="1.7"/>`,
    );
  }

  // 进纸旋钮与撕纸杆
  out.push(S(k.cylinder(142, 58, 4.5, BODY_H, 3.8, P, 28, 0.35), { tone: "hi", lit: true }));
  out.push(L(k.knurl(142, 58, 4.5, BODY_H + 0.4, BODY_H + 3.4, 16, P, 0.3), { tone: "mid" }));
  out.push(L(k.ring(142, 58, 3.7, BODY_H + 3.9, P, 24), { tone: "hi" }));
  out.push(S(k.slabOf({ x: 54, y: 60.4, w: 88, d: 2.2, r: 0.8 }, BODY_H, 0.8, P, 3, 0.1), { tone: "mid" }));

  // 坐标轴的纸质标注
  out.push(screenText(PLOT.x0 + PLOT.w - 1.5, PLOT.y0 - 1.8, PAPER_Z + 0.58, "U / V", { size: 5.2, tone: "hi", anchor: "end" }));
  out.push(screenText(PLOT.x0 + 2.0, PLOT.y0 - PLOT.h + 3.4, PAPER_Z + 0.58, "I / A", { size: 5.2, tone: "hi", anchor: "start" }));

  return out.join("");
}

/* ------------------------------------------------------------------ */
/* 导线                                                                */
/* ------------------------------------------------------------------ */

function roundedPath(points, radius = 5) {
  if (points.length < 2) return "";
  const fmt = (value) => Math.round(value * 10) / 10;
  let d = `M${fmt(points[0][0])} ${fmt(points[0][1])}`;
  for (let index = 1; index < points.length - 1; index++) {
    const prev = points[index - 1];
    const curr = points[index];
    const next = points[index + 1];
    const before = Math.hypot(curr[0] - prev[0], curr[1] - prev[1]) || 1;
    const after = Math.hypot(next[0] - curr[0], next[1] - curr[1]) || 1;
    const cut = Math.min(radius, before / 2, after / 2);
    const ax = curr[0] - ((curr[0] - prev[0]) / before) * cut;
    const ay = curr[1] - ((curr[1] - prev[1]) / before) * cut;
    const bx = curr[0] + ((next[0] - curr[0]) / after) * cut;
    const by = curr[1] + ((next[1] - curr[1]) / after) * cut;
    d += `L${fmt(ax)} ${fmt(ay)}Q${fmt(curr[0])} ${fmt(curr[1])} ${fmt(bx)} ${fmt(by)}`;
  }
  const last = points[points.length - 1];
  d += `L${fmt(last[0])} ${fmt(last[1])}`;
  return d;
}

function cable(route, { tone = "mid", flow = false, sense = false } = {}) {
  const screen = route.map(at);
  const d = roundedPath(screen, 6);
  const cls = sense ? "ohm-cable ohm-sense" : "ohm-cable ohm-main";
  const base = k.lineSvg(d, { tone, className: "ohm-cable-line" });
  const stream = flow ? `<path class="iso-line ohm-flow" data-flow="" d="${d}"/>` : "";
  const ends = screen
    .filter((_, index) => index === 0 || index === screen.length - 1)
    .map(([x, y]) => `<circle class="iso-wire-end ohm-cable-end" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${sense ? 1.2 : 1.5}"/>`)
    .join("");
  return `<g class="${cls}">${base}${stream}${ends}</g>`;
}

function buildWires() {
  const routes = [
    // 电源 + → 变阻器左端
    [
      [SUPPLY_PLUS[0], SUPPLY_PLUS[1], SUPPLY_H + 4],
      [-136, -25, SUPPLY_H + 4],
      [-136, -25, 1],
      [-70, -25, 1],
      [RHEO_TERMINAL_L[0], RHEO_TERMINAL_L[1], RHEO_H + 3.1],
    ],
    // 变阻器右端 → 电流表 +
    [
      [RHEO_TERMINAL_R[0], RHEO_TERMINAL_R[1], RHEO_H + 3.1],
      [58, -30, 1],
      [68, -30, 1],
      [METER_TERMINALS[0][0], METER_TERMINALS[0][1], SHELF_H + 4],
    ],
    // 电流表 − → 负载电流端
    [
      [METER_TERMINALS[1][0], METER_TERMINALS[1][1], SHELF_H + 4],
      [100, -18, 1],
      [-24, -18, 1],
      [LOAD_TERMINALS[3][0], LOAD_TERMINALS[3][1], LOAD_H + 4.5],
    ],
    // 负载电流端 → 电源 −
    [
      [LOAD_TERMINALS[0][0], LOAD_TERMINALS[0][1], LOAD_H + 4.5],
      [-110, 14, 1],
      [-144, 14, 1],
      [-144, -18, 1],
      [-120, -18, 1],
      [-120, -25, 1],
      [-120, -25, SUPPLY_H + 4],
      [SUPPLY_MINUS[0], SUPPLY_MINUS[1], SUPPLY_H + 4],
    ],
  ];
  const senseRoutes = [
    // 电压表 + → 负载电压端
    [
      [METER_TERMINALS[2][0], METER_TERMINALS[2][1], SHELF_H + 4],
      [120, -14, 1],
      [-90, -12, 1],
      [LOAD_TERMINALS[1][0], LOAD_TERMINALS[1][1], LOAD_H + 4.5],
    ],
    // 电压表 − → 负载电压端
    [
      [METER_TERMINALS[3][0], METER_TERMINALS[3][1], SHELF_H + 4],
      [142, -8, 1],
      [-24, -6, 1],
      [LOAD_TERMINALS[2][0], LOAD_TERMINALS[2][1], LOAD_H + 4.5],
    ],
  ];
  return [
    ...routes.map((route) => cable(route, { tone: "mid", flow: true })),
    ...senseRoutes.map((route) => cable(route, { tone: "mid", sense: true })),
  ].join("");
}

/* ------------------------------------------------------------------ */
/* 动态初值与命中区                                                    */
/* ------------------------------------------------------------------ */

const INITIAL = { alpha: INITIAL_ALPHA, modeIndex: 0, closed: true };
const initial = solve({ alpha: INITIAL.alpha, mode: MODES[INITIAL.modeIndex].id, closed: true });

const DATA = {
  P,
  MODES,
  CHART,
  INITIAL,
  METERS: METERS.map((meter) => ({ ...meter, z: NEEDLE_Z })),
  NEEDLE_R,
  NEEDLE_Z,
  MODE_PIVOT: [MODE[0], MODE[1], MODE_Z],
  KEY_PIVOT: [KEY[0], KEY[1], KEY_Z],
  RAIL: [at([RHEO_X0, RHEO_WIRE_Y, 26]), at([RHEO_X1, RHEO_WIRE_Y, 26])],
  CARRIAGE_TRAVEL: RHEO_X1 - RHEO_X0,
  RHEO_HIT: boxScreen({ x: -60, y: -62, w: 120, d: 38 }, 0, 36, 5),
  MODE_HIT: (() => {
    const [x, y] = at([MODE[0], MODE[1], MODE_Z]);
    return { x, y, r: 18 };
  })(),
  KEY_HIT: (() => {
    const [x, y] = at([KEY[0], KEY[1], 39]);
    return { x, y, r: 13 };
  })(),
  TRACE_Z,
  PAPER_TRACE_Z: TRACE_Z,
  PEN_ORIGIN,
  ROLLER_BASE_Y,
  SOURCE_V,
  RHEOSTAT_MAX,
  AMMETER_MAX,
  VOLTMETER_MAX,
  CHART_ORIGIN: [PLOT.x0, PLOT.y0],
};

const RHEO_HIT = DATA.RHEO_HIT;
const MODE_HIT = DATA.MODE_HIT;
const KEY_HIT = DATA.KEY_HIT;
const RAIL = DATA.RAIL;

function hitRects() {
  return `<g class="ohm-hits" aria-hidden="true">
    <rect data-hit="rheostat" x="${RHEO_HIT.x.toFixed(1)}" y="${RHEO_HIT.y.toFixed(1)}" width="${RHEO_HIT.w.toFixed(1)}" height="${RHEO_HIT.h.toFixed(1)}" fill="transparent" pointer-events="all" style="cursor:ew-resize"/>
    <circle data-hit="mode" cx="${MODE_HIT.x.toFixed(1)}" cy="${MODE_HIT.y.toFixed(1)}" r="${MODE_HIT.r}" fill="transparent" pointer-events="all" style="cursor:pointer"/>
    <circle data-hit="key" cx="${KEY_HIT.x.toFixed(1)}" cy="${KEY_HIT.y.toFixed(1)}" r="${KEY_HIT.r}" fill="transparent" pointer-events="all" style="cursor:pointer"/>
  </g>`;
}

/* ------------------------------------------------------------------ */
/* 模型源码注入                                                        */
/* ------------------------------------------------------------------ */

const MODEL_SRC = [
  `const clamp01 = ${clamp01.toString()};`,
  ...[solve, chartPoint, needleAngle, needleSegment, formatReadout].map((fn) =>
    fn.toString().replace(/^export\s+/, ""),
  ),
].join("\n\n");

/* 浏览器里要自己算指针角度和笔尖轨迹，所以把等距几何一起内联进去。
   geom.mjs 与 kit 的实现必须完全一致，构建时抽样对比。 */
const GEOM_SRC = readFileSync(join(HERE, "geom.mjs"), "utf8").replace(/^export /gm, "");

const LIVE = readFileSync(join(HERE, "live.js"), "utf8")
  .replace("/*__MODEL__*/", MODEL_SRC)
  .replace("/*__GEOM__*/", GEOM_SRC)
  .replace("/*__DATA__*/", `const DATA = ${JSON.stringify(DATA)};`);

/* ------------------------------------------------------------------ */
/* 组装                                                                */
/* ------------------------------------------------------------------ */

const figureBody = [
  `<defs><radialGradient id="ohm-lamp-glow" cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="var(--ohm-glow-core)" stop-opacity=".95"/><stop offset=".35" stop-color="var(--ohm-glow-core)" stop-opacity=".38"/><stop offset="1" stop-color="var(--ohm-glow-core)" stop-opacity="0"/></radialGradient></defs>`,
  buildBase(),
  buildSupply(),
  buildRheostat(),
  buildMeters(),
  buildLoad(),
  buildWires(),
  buildChart(),
  hitRects(),
].join("");

const svg = k.figureSvg({
  width: WIDTH,
  height: HEIGHT,
  label: "欧姆定律实验台：6 V 电源、滑线变阻器、两个 10 Ω 电阻、电流表、电压表和 I-U 记录仪",
  body: figureBody,
  standalone: false,
  className: "ohm-svg",
});

const body = `<div class="iso-stage ohm-stage"${INITIAL.closed ? " data-on" : ""} data-mastery="unknown" tabindex="0" role="group" aria-label="欧姆定律实验台。左右方向键调节滑线变阻器，数字 1、2、3 切换单个、串联、并联接法，空格开关电源。">${svg}<div class="ohm-actions"><button type="button" data-check="yes">我看懂了这条线</button><button type="button" data-check="no">还不太明白</button><span class="ohm-status" data-check-status></span></div><span class="ohm-sr" data-announce aria-live="polite"></span></div>`;

const plate = k.plateHtml({
  fig: "Fig 1",
  title: "欧姆定律实验台",
  hint: "拖动滑片 · 1/2/3 换接法 · 空格开关",
  readout: formatReadout(initial),
  keys: [
    { mark: "lit", label: "指针与笔尖：读数和图线由同一个模型驱动" },
    { mark: "raised", label: "滑线变阻器：向右增加 0–20 Ω" },
    { mark: "dotted", label: "三条参考线：并联 5 Ω、单个 10 Ω、串联 20 Ω" },
  ],
  caption:
    "6 V 电源、0–20 Ω 滑线变阻器和两个 10 Ω 电阻。滑片向右移，总电阻变大，电压表与电流表同时回落；笔臂的倾角就是 1/R，记录纸上的三条参考线分别对应 5 Ω、10 Ω、20 Ω。",
  body,
});

const CUSTOM_CSS = `
*{box-sizing:border-box}
html{color-scheme:light dark}
body{margin:0;background:var(--anatomy-page);color:var(--anatomy-text);font:14px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI","Noto Sans SC",sans-serif;-webkit-font-smoothing:antialiased}
body.ohm-embed{background:transparent}
.iso:not([data-theme="light"]){
  --anatomy-page:#0d0d0d;--anatomy-card:#151515;--anatomy-text:#b4b4b4;--anatomy-strong:#f0f0f0;--anatomy-muted:#858585;
  --anatomy-paper:#181818;--anatomy-face:#202020;--anatomy-top:#242424;
  --anatomy-shade-0:#111111;--anatomy-shade-1:#171717;--anatomy-shade-2:#1d1d1d;--anatomy-shade-3:#222222;
  --anatomy-lit-top:#2d2d2d;--anatomy-lit-shade:#242424;
  --anatomy-hi:#e6e6e6;--anatomy-mid:#a2a2a2;--anatomy-lo:#626262;--anatomy-faint:#3a3a3a;--anatomy-lit:#ffffff;--anatomy-weight:.9px;
  --anatomy-halo:#050505;--anatomy-halo-opacity:.9;--anatomy-dot:#f0f0f0;
  --ohm-live:#ffb15e;--ohm-glow-core:#ffcf86;--ohm-paper:#1b1a16;--ohm-paper-line:#8a8a8a;
  --ohm-body:#1c1c1c;--ohm-body-top:#292929;--ohm-body-shade:#171717;
  --ohm-arm:#545454;--ohm-arm-top:#6c6c6c;--ohm-base-top:#242424;--ohm-base-shade:#171717;--ohm-wire:#d0d0d0;
}
.iso[data-theme="light"]{
  --anatomy-page:#f1efe9;--anatomy-card:#fbfaf6;--anatomy-text:#4f4f52;--anatomy-strong:#19191b;--anatomy-muted:#85858a;
  --anatomy-paper:#fbfaf6;--anatomy-face:#ffffff;--anatomy-top:#ffffff;
  --anatomy-shade-0:#dedbd3;--anatomy-shade-1:#e7e4dc;--anatomy-shade-2:#efede6;--anatomy-shade-3:#f5f3ed;
  --anatomy-lit-top:#ffffff;--anatomy-lit-shade:#efede8;
  --anatomy-hi:#1b1b1e;--anatomy-mid:#6f6f76;--anatomy-lo:#a8a8ac;--anatomy-faint:#d2d0c8;--anatomy-lit:#000000;--anatomy-weight:.9px;
  --anatomy-halo:#8a8a80;--anatomy-halo-opacity:.3;--anatomy-dot:#3a3a3e;
  --ohm-live:#b45309;--ohm-glow-core:#d9820a;--ohm-paper:#f7f1e2;--ohm-paper-line:#7a7a7a;
  --ohm-body:#e9e6de;--ohm-body-top:#f4f1e9;--ohm-body-shade:#dedbd3;
  --ohm-arm:#b9b6ad;--ohm-arm-top:#d3d0c7;--ohm-base-top:#f7f3e9;--ohm-base-shade:#e7e2d6;--ohm-wire:#3f3f45;
}
.ohm-page{width:min(720px,100% - 32px);margin:48px auto 80px}
.ohm-page.embed{width:100%;margin:0;padding:0}
.iso-figure{margin:0}
.iso-plate{padding:52px 24px 50px}
.iso-stage{position:relative;border-radius:2px;outline:none;touch-action:pan-y}
.iso-stage:focus-visible{outline:1px solid var(--anatomy-mid);outline-offset:6px}
.iso-stage .iso-svg{user-select:none;-webkit-user-select:none}
.ohm-stage .iso-face-text{font-weight:600}
.ohm-base .iso-top{fill:var(--ohm-base-top)}
.ohm-base .iso-fill{fill:var(--ohm-base-shade)}
.ohm-base .iso-shade{fill:var(--ohm-base-shade)}
.ohm-chart-body .iso-fill{fill:var(--ohm-body)}
.ohm-chart-body .iso-top{fill:var(--ohm-body-top)}
.ohm-chart-body .iso-shade{fill:var(--ohm-body-shade)}
.ohm-chart-body .iso-edge{stroke:var(--anatomy-mid)}
.ohm-paper-sheet{fill:var(--ohm-paper);stroke:none}
.ohm-arm-fill{fill:var(--ohm-arm);stroke:none}
.ohm-arm-top{fill:var(--ohm-arm-top);stroke:none}
.ohm-stage[data-on] .ohm-glow{opacity:.9}
.ohm-glow{opacity:0;mix-blend-mode:screen;pointer-events:none;transition:opacity .45s var(--anatomy-ease)}
.iso[data-theme="light"] .ohm-glow{mix-blend-mode:multiply;opacity:0}
.iso[data-theme="light"] .ohm-stage[data-on] .ohm-glow{opacity:.4}
.iso .ohm-stage .ohm-cable-line{stroke:var(--ohm-wire);stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.iso .ohm-stage .ohm-sense .ohm-cable-line{stroke-width:1.1;opacity:.62}
.ohm-cable-end{fill:var(--ohm-paper);stroke:var(--anatomy-mid)}
.ohm-flow{stroke:var(--ohm-live);stroke-width:1.45;stroke-dasharray:2 10;opacity:0;transition:opacity .35s var(--anatomy-ease)}
.ohm-stage[data-on] .ohm-flow{opacity:.5;animation:iso-flow 1900ms linear infinite}
.ohm-needle{stroke-width:1.2;stroke-linecap:round}
.ohm-pointer{stroke-width:1.2;stroke-linecap:round}
.ohm-trace{stroke-width:1.35;stroke-linecap:round;stroke-linejoin:round;opacity:.55}
.ohm-trace[data-tone="hi"]{opacity:.95}
.ohm-screen-text{paint-order:stroke;stroke:var(--ohm-paper);stroke-width:1.5px;stroke-linejoin:round}
.ohm-guide{stroke-width:.9!important;opacity:.48}
.ohm-arm-slot{opacity:.5}
.ohm-trace-end{fill:var(--anatomy-mid);stroke:none;opacity:.75}
.ohm-trace-end[data-current]{fill:var(--ohm-live);opacity:1}
.ohm-actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:14px 2px 0;font-size:12px;color:var(--anatomy-muted)}
.ohm-actions button{border:1px solid var(--anatomy-mid);background:transparent;color:var(--anatomy-text);border-radius:999px;padding:5px 11px;font:inherit;cursor:pointer;transition:border-color .18s,color .18s,background .18s}
.ohm-actions button:hover{border-color:var(--anatomy-hi);color:var(--anatomy-strong)}
.ohm-actions button[data-check="yes"]:hover{border-color:var(--anatomy-green);color:var(--anatomy-green)}
.ohm-actions button[data-check="no"]:hover{border-color:var(--anatomy-red);color:var(--anatomy-red)}
.ohm-actions button:disabled{opacity:.45;cursor:progress}
.ohm-status{font-size:11px;color:var(--anatomy-muted)}
.ohm-sr{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
.ohm-hits rect,.ohm-hits circle{pointer-events:all}
@media (prefers-reduced-motion:reduce){.ohm-flow{animation:none!important}.ohm-glow{transition:none}}
@media (max-width:560px){
  .ohm-page{width:100%;margin:0;padding:8px}
  .iso-plate{padding:48px 10px 62px}
  .iso-plate-corner{font-size:9.5px}
  .ohm-actions{gap:6px;margin-top:10px}
  .ohm-actions button{padding:5px 9px;font-size:11px}
}
/* 窄屏时提示与读数上下分行，避免互相压字 */
@media (max-width:720px){
  .iso-plate-corner[data-corner="hint"]{bottom:26px;max-width:62%;white-space:normal;line-height:1.35}
  .iso-plate-corner[data-corner="readout"]{bottom:4px;left:0;right:0;text-align:left}
}
`;

function page(theme, { embed = false } = {}) {
  const css = `${k.ISO_CSS}${CUSTOM_CSS}`;
  const script = LIVE.replace(/<\/script/gi, "<\\/script");
  const bodyClass = embed ? "ohm-page embed" : "ohm-page";
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>欧姆定律实验台 · K12 自适应学习引擎</title><link rel="icon" href="data:,"><style>${css}</style></head><body class="iso${embed ? " ohm-embed" : ""}" data-theme="${theme}"><main class="${bodyClass}">${plate}</main><script type="module">${script}</script></body></html>`;
}

const targets = [
  ["embed.html", "light", { embed: true }],
  ["embed-dark.html", "dark", { embed: true }],
  ["ohm-lab.html", "dark", {}],
  ["ohm-lab-light.html", "light", {}],
  // 早期版本还输出过一个 index.html，与 ohm-lab.html 逐字节相同且无人引用，
  // 白占 289 KB 仓库体积，已移除。
];

mkdirSync(HERE, { recursive: true });
for (const [file, theme, options] of targets) {
  writeFileSync(join(HERE, file), page(theme, options), "utf8");
}

// 让浏览器拿到的脚本先过一遍语法检查。
const checkFile = "/tmp/opencode/ohm-live.mjs";
mkdirSync("/tmp/opencode", { recursive: true });
writeFileSync(checkFile, LIVE, "utf8");
const check = spawnSync(process.execPath, ["--check", checkFile], { encoding: "utf8" });
if (check.status !== 0) {
  console.error(check.stderr || check.stdout);
  throw new Error("live.js 注入后语法检查失败");
}

const solids = (figureBody.match(/class="iso-solid/g) || []).length;
const lines = (figureBody.match(/class="iso-line/g) || []).length;
const bytes = readFileSync(join(HERE, "embed.html")).length;
console.log(`ohm-lab: ${solids} solids, ${lines} line paths, embed ${(bytes / 1024).toFixed(1)} kB`);
console.log(`projection scale ${P.scale.toFixed(3)} · origin ${P.origin.map((value) => value.toFixed(1)).join(", ")}`);

/* geom.mjs 必须和 kit 逐字等价：抽样对比，任何漂移都让构建失败 */
{
  const geom = await import(pathToFileURL(join(HERE, "geom.mjs")).href);
  const probes = [
    [[0, 0, 0], [10, -5, 3], 12],
    [[-104, 24], [-24, 24], 24.6],
    [[83, -50], [125, -50], 14.2],
  ];
  for (const [from, to, z] of probes) {
    const mine = geom.lineOnTop(from, to, z, P);
    const theirs = k.lineOnTop(from, to, z, P);
    if (mine !== theirs) {
      throw new Error(`geom.mjs 与 kit 不一致：lineOnTop ${mine} != ${theirs}`);
    }
  }
  for (const by of [[3, 0, 0], [0, 5, 0], [0, 0, 2], [1.25, -0.5, 0]]) {
    const mine = geom.translateAlong(P, by);
    const theirs = k.translateAlong(P, by);
    if (mine !== theirs) {
      throw new Error(`geom.mjs 与 kit 不一致：translateAlong ${mine} != ${theirs}`);
    }
  }
  console.log("geom check ok · 与 kit 逐字一致");
}
console.log(`wrote ${targets.map(([file]) => file).join(", ")}`);
