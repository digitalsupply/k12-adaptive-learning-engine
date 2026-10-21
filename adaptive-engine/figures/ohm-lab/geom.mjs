/**
 * 欧姆定律实验台的等距投影几何。
 *
 * 这一小段数学与 Anatomy kit（kit/iso-kit.mjs）里的实现逐行等价：
 * build.mjs 在构建时会抽样对比两边的输出，任何漂移都会让构建失败。
 * 之所以单独留一份，是因为浏览器里的 live.js 需要在运行时算指针角度、
 * 笔尖位置和图线轨迹，而 kit 只存在于构建期。
 */

const RADIANS = Math.PI / 180;
const UNIT = Math.sqrt(1.6);

/** 相机参数：由缩放、方位角、仰角算出正交投影的四个三角函数。 */
export function cameraOf({ scale, azimuth = 45, elevation = 30 }) {
  const a = azimuth * RADIANS;
  const e = elevation * RADIANS;
  return { sinA: Math.sin(a), cosA: Math.cos(a), sinE: Math.sin(e), cosE: Math.cos(e), k: scale * UNIT };
}

/** 世界坐标 → 屏幕坐标。 */
export function iso([x, y, z], projection) {
  const { origin } = projection;
  const { sinA, cosA, sinE, cosE, k } = cameraOf(projection);
  return [
    origin[0] + (x * sinA - y * cosA) * k,
    origin[1] + ((x * cosA + y * sinA) * sinE - z * cosE) * k,
  ];
}

const tenth = (value) => {
  const text = (Math.round(value * 10) / 10).toFixed(1).replace(/\.0$/, "");
  return text === "-0" ? "0" : text;
};

/** 点列 → SVG 路径。 */
export function pathOf(points, closed = false) {
  if (!points.length) return "";
  return points.map(([x, y], at) => `${at ? "L" : "M"}${tenth(x)} ${tenth(y)}`).join("") + (closed ? "Z" : "");
}

/** 水平面上的一组点。 */
export function onTop(points, z, projection, closed = false) {
  return pathOf(points.map(([x, y]) => iso([x, y, z], projection)), closed);
}

/** 水平面上的一段线（指针、笔臂）。 */
export const lineOnTop = (from, to, z, projection) => onTop([from, to], z, projection);

/** 投影的某个方向在屏幕上的向量。 */
export function axisVector(projection, [dx, dy, dz]) {
  const origin = iso([0, 0, 0], projection);
  const moved = iso([dx, dy, dz], projection);
  return [moved[0] - origin[0], moved[1] - origin[1]];
}

const fine = (value) => Math.round(value * 100) / 100;

/** 沿世界某个方向平移的 SVG transform。 */
export function translateAlong(projection, by) {
  const [x, y] = axisVector(projection, by);
  return `translate(${fine(x)} ${fine(y)})`;
}
