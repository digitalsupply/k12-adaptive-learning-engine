import assert from "node:assert/strict";
import test from "node:test";

import {
  AMMETER_MAX,
  CHART,
  MODES,
  RHEOSTAT_MAX,
  SOURCE_V,
  VOLTMETER_MAX,
  chartPoint,
  formatReadout,
  guideLine,
  needleSegment,
  reachableLine,
  solve,
} from "./model.mjs";

const close = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} ≉ ${b}`);

test("开关断开时电流、电压都为零", () => {
  for (const mode of MODES) {
    for (const alpha of [0, 0.5, 1]) {
      const s = solve({ alpha, mode: mode.id, closed: false });
      assert.equal(s.I, 0);
      assert.equal(s.U, 0);
      assert.equal(s.P, 0);
      assert.equal(s.ratio, mode.r);
    }
  }
});

test("滑片在最左时变阻器为 0，端电压为 6 V", () => {
  for (const mode of MODES) {
    const s = solve({ alpha: 0, mode: mode.id });
    close(s.U, SOURCE_V);
    close(s.I, SOURCE_V / mode.r);
    close(s.totalR, mode.r);
  }
});

test("单个、串联、并联的等效电阻分别是 10、20、5 Ω", () => {
  const atMax = Object.fromEntries(MODES.map((mode) => [mode.id, solve({ alpha: 0, mode: mode.id })]));
  close(atMax.single.R, 10);
  close(atMax.series.R, 20);
  close(atMax.parallel.R, 5);
  close(atMax.single.I, 0.6);
  close(atMax.series.I, 0.3);
  close(atMax.parallel.I, 1.2);
});

test("任意滑片位置都满足 U/I = R，并且 U 随 I 线性", () => {
  for (const mode of MODES) {
    for (let index = 0; index <= 20; index++) {
      const s = solve({ alpha: index / 20, mode: mode.id });
      close(s.ratio, mode.r, 1e-9);
      close(s.U, s.I * mode.r, 1e-9);
      close(s.P, s.U * s.I, 1e-12);
      assert.ok(s.I <= AMMETER_MAX + 1e-12);
      assert.ok(s.U <= VOLTMETER_MAX + 1e-12);
    }
  }
});

test("滑片向右，接入电阻变大，电流和端电压一起变小", () => {
  for (const mode of MODES) {
    let previous = solve({ alpha: 0, mode: mode.id });
    for (let index = 1; index <= 20; index++) {
      const next = solve({ alpha: index / 20, mode: mode.id });
      assert.ok(next.rheostat > previous.rheostat);
      assert.ok(next.totalR > previous.totalR);
      assert.ok(next.I < previous.I);
      assert.ok(next.U < previous.U);
      previous = next;
    }
  }
});

test("滑片到最右时，串联 20 Ω 的读数最低，并联 5 Ω 的读数最高", () => {
  const states = Object.fromEntries(MODES.map((mode) => [mode.id, solve({ alpha: 1, mode: mode.id })]));
  assert.ok(states.series.I < states.single.I);
  assert.ok(states.single.I < states.parallel.I);
  close(states.series.totalR, 40);
  close(states.single.totalR, 30);
  close(states.parallel.totalR, 25);
});

test("图线坐标落在绘图区内，并且 6 V 端点贴住右边界", () => {
  for (const mode of MODES) {
    for (const point of guideLine(mode.id)) {
      assert.ok(point.x >= CHART.x0 - 1e-9 && point.x <= CHART.x0 + CHART.w + 1e-9);
      assert.ok(point.y >= CHART.y0 - CHART.h - 1e-9 && point.y <= CHART.y0 + 1e-9);
    }
    const end = guideLine(mode.id)[1];
    close(end.x, CHART.x0 + CHART.w);
    close(end.y, CHART.y0 - ((SOURCE_V / mode.r) / CHART.imax) * CHART.h);
  }
  const origin = chartPoint(0, 0);
  close(origin.x, CHART.x0);
  close(origin.y, CHART.y0);
});

test("可达线段从最大读数走到变阻器 20 Ω 的读数", () => {
  for (const mode of MODES) {
    const line = reachableLine(mode.id, 12);
    assert.equal(line.length, 13);
    close(line[0].U, SOURCE_V);
    close(line[0].I, SOURCE_V / mode.r);
    const last = line[line.length - 1];
    close(last.rheostat, RHEOSTAT_MAX);
    close(last.I, SOURCE_V / (mode.r + RHEOSTAT_MAX));
    for (let index = 1; index < line.length; index++) {
      assert.ok(line[index].I < line[index - 1].I);
      assert.ok(line[index].U < line[index - 1].U);
      close(line[index].ratio, mode.r);
    }
  }
});

test("指针角度覆盖 -120° 到 +120°，且线段长度合理", () => {
  const zero = needleSegment([0, 0], 17, 0);
  const full = needleSegment([0, 0], 17, 1);
  close(zero.angle, (-120 * Math.PI) / 180);
  close(full.angle, (120 * Math.PI) / 180);
  close(Math.hypot(zero.tip[0], zero.tip[1]), 13);
  close(Math.hypot(full.tip[0], full.tip[1]), 13);
  const mid = needleSegment([0, 0], 17, 0.5);
  close(mid.angle, 0);
  close(mid.tip[0], 0);
  close(mid.tip[1], -13);
});

test("读数格式包含接法、电阻、电压和电流", () => {
  const text = formatReadout(solve({ alpha: 0.5, mode: "series" }));
  assert.match(text, /串联/);
  assert.match(text, /20Ω/);
  assert.match(text, /4\.00 V/);
  assert.match(text, /0\.200 A/);
  assert.equal(formatReadout(solve({ alpha: 0.5, mode: "single", closed: false })), "断开 · 0 V · 0.000 A");
});
