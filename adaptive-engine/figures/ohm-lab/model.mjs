/**
 * 欧姆定律实验台的纯模型。
 *
 * 电路：6 V 电源 → 开关 → 滑线变阻器（0..20 Ω）→ 被测电阻 → 电源。
 * 被测电阻有三种接法：单个 10 Ω、两个 10 Ω 串联 20 Ω、两个 10 Ω 并联 5 Ω。
 *
 * 所有指针角度、图线坐标和读数都只从这里来。浏览器里不会调用 LLM，
 * 也不存在第二套“看起来差不多”的公式。
 */

export const SOURCE_V = 6;
export const RHEOSTAT_MAX = 20;
export const FIXED_R = 10;
export const AMMETER_MAX = 1.2;
export const VOLTMETER_MAX = 6;

/** 图线纸上的绘图区（世界坐标，单位与零件一致）。 */
export const CHART = { x0: 60, y0: 56, w: 76, h: 32, umax: 6, imax: 1.2 };

export const MODES = [
  { id: "single", label: "单个", symbol: "1", r: FIXED_R, angle: -120 },
  { id: "series", label: "串联", symbol: "串", r: FIXED_R * 2, angle: 0 },
  { id: "parallel", label: "并联", symbol: "并", r: FIXED_R / 2, angle: 120 },
];

export const MODE_BY_ID = Object.fromEntries(MODES.map((mode) => [mode.id, mode]));

export const clamp01 = (value) => Math.min(1, Math.max(0, Number.isFinite(value) ? value : 0));

/**
 * 给定滑片位置、接法和开关状态，返回这一瞬间的真实电学量。
 * alpha = 0 表示变阻器 0 Ω，alpha = 1 表示 20 Ω。
 */
export function solve({ alpha = 0, mode = "single", closed = true } = {}) {
  const a = clamp01(alpha);
  const spec = MODE_BY_ID[mode] ?? MODES[0];
  const rheostat = a * RHEOSTAT_MAX;
  const totalR = spec.r + rheostat;
  const current = closed ? SOURCE_V / totalR : 0;
  const voltage = current * spec.r;
  const power = voltage * current;
  return {
    alpha: a,
    mode: spec.id,
    label: spec.label,
    R: spec.r,
    rheostat,
    totalR,
    closed: Boolean(closed),
    U: voltage,
    I: current,
    P: power,
    ratio: current > 1e-12 ? voltage / current : spec.r,
    needle: {
      A: clamp01(current / AMMETER_MAX),
      V: clamp01(voltage / VOLTMETER_MAX),
    },
  };
}

/** 把 (U, I) 映射到图线纸上的世界坐标；I 向上增大。 */
export function chartPoint(U, I) {
  const u = Math.min(CHART.umax, Math.max(0, Number.isFinite(U) ? U : 0));
  const i = Math.min(CHART.imax, Math.max(0, Number.isFinite(I) ? I : 0));
  return {
    x: CHART.x0 + (u / CHART.umax) * CHART.w,
    y: CHART.y0 - (i / CHART.imax) * CHART.h,
    u,
    i,
  };
}

/** 理论欧姆线：从原点到该接法在 6 V 时的最大电流点。 */
export function guideLine(mode = "single") {
  const spec = MODE_BY_ID[mode] ?? MODES[0];
  return [chartPoint(0, 0), chartPoint(SOURCE_V, SOURCE_V / spec.r)];
}

/**
 * 该接法在滑片 0..1 上真正能走到的线段。
 * 从 alpha=0（变阻器 0 Ω，电压、电流最大）走到 alpha=1（变阻器 20 Ω）。
 */
export function reachableLine(mode = "single", steps = 48) {
  const count = Math.max(2, Math.round(steps));
  return Array.from({ length: count + 1 }, (_, index) => {
    const state = solve({ alpha: index / count, mode, closed: true });
    return { ...state, chart: chartPoint(state.U, state.I) };
  });
}

/** 指针从 -120° 扫到 +120°，0 在左下，满量程在右下。 */
export function needleAngle(fraction) {
  return (-120 + 240 * clamp01(fraction)) * (Math.PI / 180);
}

/** 表盘上的指针线段（世界坐标），从配重尾到针尖。 */
export function needleSegment([cx, cy], radius, fraction) {
  const angle = needleAngle(fraction);
  const sin = Math.sin(angle);
  const cos = Math.cos(angle);
  return {
    tail: [cx - sin * 3.2, cy + cos * 3.2],
    tip: [cx + sin * (radius - 4), cy - cos * (radius - 4)],
    angle,
  };
}

/** 环上的三角波，用于巡演；u 在 0..1 内。 */
export function smoothstep(edge0, edge1, value) {
  const t = Math.min(1, Math.max(0, (value - edge0) / (edge1 - edge0 || 1)));
  return t * t * (3 - 2 * t);
}

/** 卡片右下角的实时读数。 */
export function formatReadout(state) {
  if (!state.closed) return "断开 · 0 V · 0.000 A";
  return `${state.label} ${state.R}Ω · ${state.U.toFixed(2)} V · ${state.I.toFixed(3)} A`;
}

/** 电流表/电压表量程比例，给 UI 用。 */
export function meterFractions(state) {
  return {
    A: Math.min(1, Math.max(0, state.I / AMMETER_MAX)),
    V: Math.min(1, Math.max(0, state.U / VOLTMETER_MAX)),
  };
}
