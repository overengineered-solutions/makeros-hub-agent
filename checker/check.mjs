// ps-checker c564b6b06692, format 1: bundled from pstation by scripts/build-checker.mjs. Do not edit; rebuild it.

// src/checker/main.ts
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { deflateSync } from "node:zlib";

// src/lib/printability.ts
import { inflateRawSync } from "node:zlib";

// src/lib/threemf-zip.ts
var ZipError = class extends Error {
};
var MAX_SLICE_INFO = 1024 * 1024;
function zipDirectory(bytes) {
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let eocd = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65557); i--) if (dv.getUint32(i, true) === 101010256) {
    eocd = i;
    break;
  }
  if (eocd < 0) throw new ZipError("The 3MF zip is damaged: no directory at its end.");
  const count = dv.getUint16(eocd + 10, true);
  let p = dv.getUint32(eocd + 16, true);
  const out = [];
  const seen = /* @__PURE__ */ new Set();
  for (let e = 0; e < count; e++) {
    if (p + 46 > bytes.length || dv.getUint32(p, true) !== 33639248) throw new ZipError("The 3MF zip directory is damaged.");
    const nlen = dv.getUint16(p + 28, true), xlen = dv.getUint16(p + 30, true), clen = dv.getUint16(p + 32, true);
    const name = new TextDecoder().decode(bytes.subarray(p + 46, p + 46 + nlen));
    if (seen.has(name)) throw new ZipError("The 3MF zip lists an entry twice.");
    seen.add(name);
    out.push({
      name,
      method: dv.getUint16(p + 10, true),
      crc: dv.getUint32(p + 16, true),
      csize: dv.getUint32(p + 20, true),
      usize: dv.getUint32(p + 24, true),
      local: dv.getUint32(p + 42, true)
    });
    p += 46 + nlen + xlen + clen;
  }
  return out;
}
function entryData(bytes, e) {
  if (e.csize === 4294967295 || e.usize === 4294967295) throw new ZipError("The 3MF uses a zip format the check does not read (zip64).");
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (e.local + 30 > bytes.length || dv.getUint32(e.local, true) !== 67324752) throw new ZipError("The 3MF zip entry is damaged.");
  const nlen = dv.getUint16(e.local + 26, true), start = e.local + 30 + nlen + dv.getUint16(e.local + 28, true);
  if (start + e.csize > bytes.length || dv.getUint16(e.local + 8, true) !== e.method || new TextDecoder().decode(bytes.subarray(e.local + 30, e.local + 30 + nlen)) !== e.name) throw new ZipError("The 3MF zip entry is damaged.");
  return { start, data: bytes.subarray(start, start + e.csize) };
}

// src/lib/print-scale.ts
var UNIT_MM = { millimeter: 1, centimeter: 10, meter: 1e3, inch: 25.4, foot: 304.8, micron: 1e-3 };
var UNIT_KEYS = Object.keys(UNIT_MM);
var readUnits = (v) => v === "file" || UNIT_KEYS.includes(v) ? v : null;
var FACTOR_MIN = 1e-3;
var FACTOR_MAX = 1e3;
var sizeFactor = (units, readIn, scale) => units === "file" ? scale : UNIT_MM[units] / UNIT_MM[readIn] * scale;
var NOZZLE_MM = 0.4;
var LAYER_MM = 0.2;
var MIN_WALL_MM = 2 * NOZZLE_MM + 0.05;
var MIN_SLAB_MM = 2 * LAYER_MM;

// src/lib/printability.ts
var MAX_FILE_BYTES = 32 * 1024 * 1024;
var MAX_TRIANGLES = 3e5;
var MAX_INFLATED_BYTES = 32 * 1024 * 1024;
var RAY_SAMPLES = 6e3;
var MIB = (n) => `${Math.round(n / 1048576)} MB`;
var TOO_BIG = `The file is over ${MIB(MAX_FILE_BYTES)}, too big to check here. A binary STL or a 3MF of the same model is usually much smaller: export it as one of those.`;
var BUILD_VOLUMES = {
  "A1 Mini": [180, 180, 180],
  "P2S": [256, 256, 256],
  "X1C": [256, 256, 256],
  "H2D": [325, 320, 325],
  "Sovol SV08 MAX": [500, 500, 500],
  "Elegoo OrangeStorm Giga": [800, 800, 1e3]
  // Elegoo's stated volume; OrcaSlicer's profile allows 810 x 805 x 1010
};
var SUPPORT_SLOPE_DEG = 30;
var OVERHANG_NZ = -Math.cos(SUPPORT_SLOPE_DEG * Math.PI / 180);
var SELF_SUPPORT_MM = 1.5;
var BRIDGE_MM = 20;
var WARP_BASE_MM = 100;
var WARP_CONTACT_MM2 = 5e3;
var PARALLEL = Math.cos(20 * Math.PI / 180);
var PrintabilityError = class extends Error {
};
function parseModel(bytes, fileName = "") {
  const refused = budgetRefusal(bytes, fileName);
  if (refused) throw new PrintabilityError(refused);
  const lower = fileName.toLowerCase();
  if (bytes[0] === 80 && bytes[1] === 75) return parse3mf(bytes);
  if (lower.endsWith(".obj")) return parseObj(new TextDecoder().decode(bytes));
  return parseStl(bytes);
}
var tooDetailed = (n) => new PrintabilityError(`This file is too detailed to check here: ${Math.round(n).toLocaleString("en-US")} triangles; the limit is ${MAX_TRIANGLES.toLocaleString("en-US")}. Reduce its detail (fewer triangles) in your design program and check it again.`);
function budgetRefusal(bytes, fileName = "") {
  if (bytes.length > MAX_FILE_BYTES) return TOO_BIG;
  const buf = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let n = 0;
  if (bytes[0] === 80 && bytes[1] === 75) {
    const xml = modelBytes(bytes);
    return xml !== null && xml > MAX_INFLATED_BYTES ? `This 3MF unpacks to ${MIB(xml)} of model, too detailed to check here; the limit is ${MIB(MAX_INFLATED_BYTES)}. Reduce its detail (fewer triangles) and check it again.` : null;
  }
  if (fileName.toLowerCase().endsWith(".obj")) {
    for (let at = buf.indexOf("f ", 0, "latin1"); at >= 0; at = buf.indexOf("f ", at + 2, "latin1")) {
      if (at > 0 && buf[at - 1] !== 10) continue;
      let corners = 0;
      for (let i = at + 2, word = false; i < buf.length && buf[i] !== 10; i++) {
        const space = buf[i] === 32 || buf[i] === 9 || buf[i] === 13;
        if (!space && !word) corners++;
        word = !space;
      }
      n += Math.max(0, corners - 2);
      if (n > MAX_TRIANGLES) break;
    }
  } else if (bytes.length >= 84 && 84 + buf.readUInt32LE(80) * 50 === bytes.length) n = buf.readUInt32LE(80);
  else {
    let corners = 0;
    for (let at = buf.indexOf("vertex", 0, "latin1"); at >= 0 && corners <= MAX_TRIANGLES * 3; at = buf.indexOf("vertex", at + 6, "latin1")) corners++;
    n = Math.ceil(corners / 3);
  }
  return n > MAX_TRIANGLES ? tooDetailed(n).message : null;
}
function modelBytes(bytes) {
  let entries;
  try {
    entries = zipDirectory(bytes);
  } catch (e) {
    if (e instanceof ZipError) return null;
    throw e;
  }
  return entries.reduce((total, e) => /\.model$/i.test(e.name) ? total + e.usize : total, 0);
}
function parseStl(bytes) {
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (bytes.length >= 84) {
    const n = dv.getUint32(80, true);
    if (84 + n * 50 === bytes.length) {
      if (n > MAX_TRIANGLES) throw tooDetailed(n);
      const out = new Float32Array(n * 9);
      for (let i = 0; i < n; i++) {
        const o = 84 + i * 50 + 12;
        for (let k = 0; k < 9; k++) out[i * 9 + k] = dv.getFloat32(o + k * 4, true);
      }
      return { kind: "stl", positions: out, triangles: n };
    }
  }
  const text = new TextDecoder().decode(bytes);
  if (!/^\s*solid/i.test(text) || !/vertex/i.test(text)) throw new PrintabilityError("This does not look like an STL, OBJ or 3MF file.");
  const nums = [];
  for (const m of text.matchAll(/vertex\s+(\S+)\s+(\S+)\s+(\S+)/gi)) {
    nums.push(+m[1], +m[2], +m[3]);
    if (nums.length > MAX_TRIANGLES * 9) throw tooDetailed(nums.length / 9);
  }
  if (nums.length % 9 !== 0 || nums.some((v) => !Number.isFinite(v))) throw new PrintabilityError("The STL text is damaged: its vertices do not make whole triangles.");
  return { kind: "stl", positions: Float32Array.from(nums), triangles: nums.length / 9 };
}
function parseObj(text) {
  const v = [];
  const tris = [];
  const names = [null], index = /* @__PURE__ */ new Map(), of = [];
  let group = 0;
  for (let at = 0; at < text.length; ) {
    let end = text.indexOf("\n", at);
    if (end < 0) end = text.length;
    const line = text.charCodeAt(end - 1) === 13 ? text.slice(at, end - 1) : text.slice(at, end);
    at = end + 1;
    if (line.startsWith("v ")) {
      const p = line.slice(2).trim().split(/\s+/).map(Number);
      v.push(p[0], p[1], p[2]);
    } else if (line.startsWith("o ") || line.startsWith("g ")) {
      const name = cleanName(line.slice(2));
      if (name && !index.has(name)) {
        index.set(name, names.length);
        names.push(name);
      }
      group = name ? index.get(name) : 0;
    } else if (line.startsWith("f ")) {
      const count = v.length / 3;
      const idx = line.slice(2).trim().split(/\s+/).map((t) => {
        const i = parseInt(t.split("/")[0], 10);
        return i < 0 ? count + i : i - 1;
      });
      for (let k = 1; k + 1 < idx.length; k++) {
        for (const i of [idx[0], idx[k], idx[k + 1]]) {
          if (!(i >= 0 && i < count)) throw new PrintabilityError("The OBJ file refers to a vertex it does not have.");
          tris.push(v[i * 3], v[i * 3 + 1], v[i * 3 + 2]);
        }
        of.push(group);
        if (tris.length > MAX_TRIANGLES * 9) throw tooDetailed(tris.length / 9);
      }
    }
  }
  if (!tris.length) throw new PrintabilityError("The OBJ file has no faces.");
  return {
    kind: "obj",
    positions: Float32Array.from(tris),
    triangles: tris.length / 9,
    objects: { names, from: names.map((n) => n === null ? "faces before any group" : `group ${n}`), of: Uint32Array.from(of) }
  };
}
function parse3mf(bytes) {
  const parts = unzipModels(bytes);
  const objects = /* @__PURE__ */ new Map();
  const rootPath = [...parts.keys()].find((k) => /3dmodel\.model$/i.test(k)) ?? [...parts.keys()][0];
  const said = attrs(/<model\b([^>]*)>/.exec(parts.get(rootPath) ?? "")?.[1] ?? "").unit;
  const stated = Object.keys(UNIT_MM).includes(said) ? said : void 0;
  const unit = stated ? UNIT_MM[stated] : 1;
  let defined = 0;
  for (const xml of parts.values()) for (let at = xml.indexOf("<triangle"); at >= 0 && defined <= MAX_TRIANGLES; at = xml.indexOf("<triangle", at + 9)) if (xml.charCodeAt(at + 9) !== 115) defined++;
  if (defined > MAX_TRIANGLES) throw tooDetailed(defined);
  const damaged = () => new PrintabilityError("The 3MF is damaged: an object's id is not a whole number.");
  for (const [path, xml] of parts) {
    for (const m of xml.matchAll(/<object\b([^>]*)>([\s\S]*?)<\/object>/g)) {
      const { id, name } = attrs(m[1]);
      if (!OBJECT_ID.test(id ?? "")) throw damaged();
      const body = m[2];
      const o = { id, name: cleanName(name ?? ""), comps: [] };
      const mesh = /<mesh\b[\s\S]*?<\/mesh>/.exec(body)?.[0];
      if (mesh) {
        o.verts = [];
        for (const vm of mesh.matchAll(/<vertex\b([^>]*?)\/?>/g)) {
          const a = attrs(vm[1]);
          o.verts.push(+a.x, +a.y, +a.z);
        }
        o.tris = [];
        for (const tm of mesh.matchAll(/<triangle\b([^>]*?)\/?>/g)) {
          const a = attrs(tm[1]);
          o.tris.push(+a.v1, +a.v2, +a.v3);
        }
      }
      for (const cm of body.matchAll(/<component\b([^>]*?)\/?>/g)) {
        const a = attrs(cm[1]);
        if (!OBJECT_ID.test(a.objectid ?? "")) throw damaged();
        o.comps.push({ id: a.objectid, path: normPath(a["p:path"] ?? path), m: matrix(a.transform) });
      }
      objects.set(`${path}#${id}`, o);
    }
  }
  const items = [...(parts.get(rootPath) ?? "").matchAll(/<item\b([^>]*?)\/?>/g)].map((m) => attrs(m[1]));
  if (items.some((it) => !OBJECT_ID.test(it.objectid ?? ""))) throw damaged();
  const out = [];
  const names = [], from = [], of = [];
  let placed = 0;
  const place = (key, m, depth, path, named) => {
    const o = objects.get(key);
    if (!o || depth > 8) return;
    if (++placed > MAX_PLACEMENTS) throw new PrintabilityError(`This 3MF places more than ${MAX_PLACEMENTS.toLocaleString("en-US")} objects, too many to check here.`);
    const here = `${path} > object ${o.id}`, name = o.name ?? named;
    if (o.verts && o.tris) {
      for (let i = 0; i < o.tris.length; i++) {
        const vi = o.tris[i];
        if (!(vi >= 0 && vi * 3 + 2 < o.verts.length)) throw new PrintabilityError("The 3MF refers to a vertex it does not have.");
        const x = o.verts[vi * 3], y = o.verts[vi * 3 + 1], z = o.verts[vi * 3 + 2];
        out.push((x * m[0] + y * m[3] + z * m[6] + m[9]) * unit, (x * m[1] + y * m[4] + z * m[7] + m[10]) * unit, (x * m[2] + y * m[5] + z * m[8] + m[11]) * unit);
      }
      if (out.length > MAX_TRIANGLES * 9) throw tooDetailed(out.length / 9);
      for (let t = 0; t < o.tris.length / 3; t++) of.push(names.length);
      names.push(name);
      from.push(here);
    }
    for (const c of o.comps) place(`${c.path}#${c.id}`, compose(c.m, m), depth + 1, here, name);
  };
  items.forEach((it, i) => place(`${rootPath}#${it.objectid}`, matrix(it.transform), 0, `item ${i + 1}`, null));
  if (!out.length) throw new PrintabilityError("The 3MF has no printable objects on its build plate.");
  return {
    kind: "3mf",
    positions: Float32Array.from(out),
    triangles: out.length / 9,
    ...stated ? { unit: stated } : {},
    objects: { names, from, of: Uint32Array.from(of) }
  };
}
function unzipModels(bytes) {
  const out = /* @__PURE__ */ new Map();
  const read = [];
  let inflated = 0;
  let entries;
  try {
    entries = zipDirectory(bytes);
  } catch (e) {
    throw e instanceof ZipError ? new PrintabilityError(e.message) : e;
  }
  for (const entry of entries) {
    const { name, method, csize, usize, local } = entry;
    if (!/\.model$/i.test(name)) continue;
    if (csize === 4294967295 || usize === 4294967295) throw new PrintabilityError("The 3MF uses a zip format the check does not read (zip64).");
    if (inflated + usize > MAX_INFLATED_BYTES) throw new PrintabilityError(`This 3MF unpacks to more than ${MIB(MAX_INFLATED_BYTES)} of model, too detailed to check here. Reduce its detail (fewer triangles) and check it again.`);
    let start, data;
    try {
      ({ start, data } = entryData(bytes, entry));
    } catch (e) {
      throw e instanceof ZipError ? new PrintabilityError(e.message) : e;
    }
    if (read.some(([a, b]) => local < b && start + csize > a)) throw new PrintabilityError("The 3MF zip entry is damaged.");
    read.push([local, start + csize]);
    let raw = null;
    try {
      raw = method === 0 ? data : method === 8 ? inflateRawSync(data, { maxOutputLength: Math.max(usize, 1) }) : null;
    } catch {
      throw new PrintabilityError("The 3MF zip entry is damaged.");
    }
    if (!raw) throw new PrintabilityError("The 3MF uses a compression the check does not read.");
    if (raw.length !== usize) throw new PrintabilityError("The 3MF zip entry is damaged.");
    inflated += raw.length;
    out.set(normPath(name), new TextDecoder().decode(raw));
  }
  if (!out.size) throw new PrintabilityError("The 3MF has no model inside.");
  return out;
}
var normPath = (p) => p.replace(/^\/+/, "");
var OBJECT_ID = /^[1-9]\d{0,9}$/;
var MAX_PLACEMENTS = 2e4;
var cleanName = (s) => s.replace(/[\u0000-\u001f\u007f-\u009f‪-‮⁦-⁩]/g, " ").replace(/\s+/g, " ").trim().slice(0, 60) || null;
function attrs(s) {
  const o = {};
  for (const m of s.matchAll(/([\w:]+)\s*=\s*"([^"]*)"/g)) o[m[1]] = m[2];
  return o;
}
function matrix(t) {
  const m = (t ?? "").trim().split(/\s+/).map(Number);
  return m.length === 12 && m.every(Number.isFinite) ? m : [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0];
}
function compose(a, b) {
  const r = new Array(12).fill(0);
  for (let i = 0; i < 4; i++) for (let j = 0; j < 3; j++) {
    r[i * 3 + j] = (i < 3 ? 0 : b[9 + j]) + a[i * 3] * b[j] + a[i * 3 + 1] * b[3 + j] + a[i * 3 + 2] * b[6 + j];
  }
  return r;
}
function toBinaryStl(mesh) {
  const P = mesh.positions, n = mesh.triangles;
  const out = new Uint8Array(84 + n * 50), dv = new DataView(out.buffer);
  out.set(new TextEncoder().encode("Procrastination Station checked mesh"));
  dv.setUint32(80, n, true);
  for (let t = 0; t < n; t++) {
    const o = t * 9, b = 84 + t * 50;
    const ax = P[o + 3] - P[o], ay = P[o + 4] - P[o + 1], az = P[o + 5] - P[o + 2];
    const bx = P[o + 6] - P[o], by = P[o + 7] - P[o + 1], bz = P[o + 8] - P[o + 2];
    const cx = ay * bz - az * by, cy = az * bx - ax * bz, cz = ax * by - ay * bx, len = Math.hypot(cx, cy, cz) || 1;
    dv.setFloat32(b, cx / len, true);
    dv.setFloat32(b + 4, cy / len, true);
    dv.setFloat32(b + 8, cz / len, true);
    for (let k = 0; k < 9; k++) dv.setFloat32(b + 12 + k * 4, P[o + k], true);
  }
  return out;
}
function sizeMesh(mesh, units, scale) {
  const readIn = mesh.unit ?? "millimeter";
  const factor = sizeFactor(units, readIn, scale);
  if (!(factor >= FACTOR_MIN && factor <= FACTOR_MAX)) throw new PrintabilityError("That size is outside what the shop's slicer takes: from 1:1000 of a file in millimetres up to a file in metres at full size.");
  if (factor !== 1) for (let i = 0; i < mesh.positions.length; i++) mesh.positions[i] *= factor;
  return { factor, units: units === "file" ? readIn : units, fromFile: units === "file" && !!mesh.unit };
}
function analyze(mesh, models = Object.keys(BUILD_VOLUMES), minWallMm = MIN_WALL_MM, stage = () => {
}) {
  const P = mesh.positions, n = mesh.triangles;
  if (!n) throw new PrintabilityError("The file has no triangles.");
  stage("measuring");
  for (let i = 0; i < P.length; i++) if (!Number.isFinite(P[i])) throw new PrintabilityError("The file has coordinates that are not numbers.");
  let minX = Infinity, minY = Infinity, minZ = Infinity, maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
  for (let i = 0; i < P.length; i += 3) {
    minX = Math.min(minX, P[i]);
    maxX = Math.max(maxX, P[i]);
    minY = Math.min(minY, P[i + 1]);
    maxY = Math.max(maxY, P[i + 1]);
    minZ = Math.min(minZ, P[i + 2]);
    maxZ = Math.max(maxZ, P[i + 2]);
  }
  const size = [maxX - minX, maxY - minY, maxZ - minZ];
  const normals = new Float32Array(n * 3), areas = new Float32Array(n);
  let volume = 0, area = 0;
  for (let t = 0; t < n; t++) {
    const o = t * 9;
    const ax = P[o + 3] - P[o], ay = P[o + 4] - P[o + 1], az = P[o + 5] - P[o + 2];
    const bx = P[o + 6] - P[o], by = P[o + 7] - P[o + 1], bz = P[o + 8] - P[o + 2];
    const cx = ay * bz - az * by, cy = az * bx - ax * bz, cz = ax * by - ay * bx;
    const len = Math.hypot(cx, cy, cz);
    areas[t] = len / 2;
    area += len / 2;
    if (len > 0) {
      normals[t * 3] = cx / len;
      normals[t * 3 + 1] = cy / len;
      normals[t * 3 + 2] = cz / len;
    }
    volume += (P[o] * (P[o + 4] * P[o + 8] - P[o + 5] * P[o + 7]) - P[o + 1] * (P[o + 3] * P[o + 8] - P[o + 5] * P[o + 6]) + P[o + 2] * (P[o + 3] * P[o + 7] - P[o + 4] * P[o + 6])) / 6;
  }
  const inverted = volume < 0;
  const flip = inverted ? -1 : 1;
  volume = Math.abs(volume);
  const { tri, ...topo } = edgeTopology(P, n);
  const closed = { ...topo, inverted };
  const marks = new Uint8Array(n);
  let bv = null;
  const bvh = () => bv ??= buildBvh(P, n);
  stage("parts");
  const parts = partsOf(P, n, tri, mesh.objects, normals, flip, minZ, bvh);
  let contact = 0, bx0 = Infinity, by0 = Infinity, bx1 = -Infinity, by1 = -Infinity;
  for (let t = 0; t < n; t++) {
    const o = t * 9;
    if (P[o + 2] - minZ < 0.05 && P[o + 5] - minZ < 0.05 && P[o + 8] - minZ < 0.05 && normals[t * 3 + 2] * flip < -0.9) {
      contact += areas[t];
      marks[t] = 1;
      for (let k = 0; k < 9; k += 3) {
        bx0 = Math.min(bx0, P[o + k]);
        bx1 = Math.max(bx1, P[o + k]);
        by0 = Math.min(by0, P[o + k + 1]);
        by1 = Math.max(by1, P[o + k + 1]);
      }
    }
  }
  const base = contact ? [bx1 - bx0, by1 - by0].sort((a, b) => b - a).map(round1) : [0, 0];
  const footprint = size[0] * size[1];
  const best = bestFlatSide(P, n, normals, areas, flip);
  stage("supports");
  const overhang = overhangs(P, n, tri, normals, areas, flip, minZ, marks);
  stage("walls");
  const { thinTris, ...thin } = { ...thickness(P, n, normals, areas, flip, closed.boundaryEdges === 0 || closed.boundaryEdges < n * 0.01, minWallMm, bvh), minWallMm };
  const shell = Math.min(volume, area * 0.84), infill = Math.max(0, volume - shell) * 0.15;
  const solid = shell + infill;
  const grams = { pla: round1(solid * 124e-5), petg: round1(solid * 127e-5) };
  const fits = models.filter((m) => BUILD_VOLUMES[m]).map((model) => {
    const v = BUILD_VOLUMES[model];
    const asIs = size[0] <= v[0] && size[1] <= v[1] && size[2] <= v[2];
    const s = [...size].sort((a, b) => a - b), w = [...v].sort((a, b) => a - b);
    return { model, asIs, turned: !asIs && s.every((d, i) => d <= w[i]) };
  });
  const report = {
    kind: mesh.kind,
    triangles: n,
    size: size.map(round1),
    extent: size,
    volumeCm3: round1(volume / 1e3),
    areaCm2: round1(area / 100),
    closed,
    fits,
    firstLayer: { contactMm2: round1(contact), footprintMm2: round1(footprint), bestSideMm2: round1(best.area), bestSide: best.area > contact * 2 && best.area > 25 ? best.name : null, baseMm: base },
    overhangCm2: round1(overhang / 100),
    thin,
    grams,
    parts,
    findings: [],
    marks
  };
  report.findings = findingsFor(report);
  if (report.findings.some((f) => f.id === "too-thin" || f.id === "thin")) for (const t of thinTris) marks[t] = 3;
  return report;
}
function edgeTopology(P, n) {
  const corners = n * 3;
  let cap = 1;
  while (cap < corners * 2) cap *= 2;
  const first = new Int32Array(cap).fill(-1);
  const tri = new Uint32Array(corners);
  const q = (i) => Math.round(P[i] * 1e3);
  let V = 0;
  for (let c = 0; c < corners; c++) {
    const x = q(c * 3), y = q(c * 3 + 1), z = q(c * 3 + 2);
    let slot = (Math.imul(x | 0, 73856093) ^ Math.imul(y | 0, 19349663) ^ Math.imul(z | 0, 83492791)) & cap - 1;
    for (; ; ) {
      const r = first[slot];
      if (r < 0) {
        first[slot] = c;
        tri[c] = V++;
        break;
      }
      if (q(r * 3) === x && q(r * 3 + 1) === y && q(r * 3 + 2) === z) {
        tri[c] = tri[r];
        break;
      }
      slot = slot + 1 & cap - 1;
    }
  }
  const keys = new Float64Array(corners);
  let m = 0;
  for (let t = 0; t < n; t++) {
    if (collapsed(tri, t)) continue;
    for (let k = 0; k < 3; k++) {
      const a = tri[t * 3 + k], b = tri[t * 3 + (k + 1) % 3];
      keys[m++] = (Math.min(a, b) * V + Math.max(a, b)) * 2 + (a < b ? 1 : 0);
    }
  }
  const sorted = keys.subarray(0, m).sort();
  let boundaryEdges = 0, nonManifoldEdges = 0, flippedEdges = 0;
  for (let i = 0; i < m; ) {
    const edge = Math.floor(sorted[i] / 2);
    let count = 0, net = 0;
    do {
      count++;
      net += sorted[i] % 2 ? 1 : -1;
      i++;
    } while (i < m && Math.floor(sorted[i] / 2) === edge);
    if (count === 1) boundaryEdges++;
    else if (count > 2) nonManifoldEdges++;
    else if (net !== 0) flippedEdges++;
  }
  return { boundaryEdges, nonManifoldEdges, flippedEdges, tri };
}
var collapsed = (tri, t) => tri[t * 3] === tri[t * 3 + 1] || tri[t * 3 + 1] === tri[t * 3 + 2] || tri[t * 3] === tri[t * 3 + 2];
var PART_GAP_MM = LAYER_MM;
var MAX_PARTS = 2e3;
var PART_RAYS = 2e5;
var r2 = (v) => Math.round(v * 100) / 100;
var tetra = (P, o) => (
  // six times the signed volume under one triangle, from the origin
  P[o] * (P[o + 4] * P[o + 8] - P[o + 5] * P[o + 7]) - P[o + 1] * (P[o + 3] * P[o + 8] - P[o + 5] * P[o + 6]) + P[o + 2] * (P[o + 3] * P[o + 7] - P[o + 4] * P[o + 6])
);
function partWords(p) {
  if (p.name) return p.pieces > 1 ? `${p.name} (part ${p.piece} of ${p.pieces})` : p.name;
  return `a part ${[0, 1, 2].map((k) => round1(p.max[k] - p.min[k])).join(" × ")} mm`;
}
function partsOf(P, n, tri, objects, normals, flip, minZ, bvh) {
  const G = objects?.names.length ?? 1, of = objects && G > 1 ? objects.of : null;
  const order = new Uint32Array(n);
  if (of) {
    const at = new Uint32Array(G + 1);
    for (let t = 0; t < n; t++) at[of[t] + 1]++;
    for (let g = 0; g < G; g++) at[g + 1] += at[g];
    for (let t = 0; t < n; t++) order[at[of[t]]++] = t;
  } else for (let t = 0; t < n; t++) order[t] = t;
  const parent = new Int32Array(n).map((_, i) => i);
  const find = (x) => {
    while (parent[x] !== x) {
      parent[x] = parent[parent[x]];
      x = parent[x];
    }
    return x;
  };
  let V = 0;
  for (let i = 0; i < tri.length; i++) if (tri[i] >= V) V = tri[i] + 1;
  const last = new Int32Array(V).fill(-1), shared = [];
  for (let i = 0; i < n; i++) {
    const t = order[i];
    for (let k = 0; k < 3; k++) {
      const v = tri[t * 3 + k], u = last[v];
      if (u >= 0) {
        if (!of || of[u] === of[t]) {
          const a = find(u), b = find(t);
          if (a !== b) parent[a] = b;
        } else if (shared.length < 3e4) shared.push(u, t, P[t * 9 + k * 3 + 2]);
      }
      last[v] = t;
    }
  }
  const part = new Int32Array(n), id = new Int32Array(n).fill(-1);
  let count = 0;
  for (let t = 0; t < n; t++) {
    const r = find(t);
    if (id[r] < 0) id[r] = count++;
    part[t] = id[r];
  }
  const measure = () => {
    const s = { tris: new Int32Array(count), first: new Int32Array(count).fill(-1), box: new Float64Array(count * 6), vol: new Float64Array(count) };
    for (let p = 0; p < count; p++) s.box.set([Infinity, Infinity, Infinity, -Infinity, -Infinity, -Infinity], p * 6);
    for (let t = 0; t < n; t++) {
      const p = part[t], o = t * 9, b = p * 6;
      s.tris[p]++;
      if (s.first[p] < 0) s.first[p] = t;
      s.vol[p] += tetra(P, o);
      for (let k = 0; k < 9; k++) {
        const a = k % 3, v = P[o + k];
        if (v < s.box[b + a]) s.box[b + a] = v;
        if (v > s.box[b + 3 + a]) s.box[b + 3 + a] = v;
      }
    }
    return s;
  };
  let st = measure();
  const objOf = (p) => of ? of[st.first[p]] : 0;
  const judgeable = count > 1 && count <= MAX_PARTS;
  if (judgeable) {
    const bv = bvh();
    const into = Int32Array.from({ length: count }, (_, i) => i);
    let folded = false;
    const boxIn = (c, p) => [0, 1, 2].every((a) => st.box[p * 6 + a] <= st.box[c * 6 + a] && st.box[c * 6 + 3 + a] <= st.box[p * 6 + 3 + a]);
    for (let c = 0; c < count; c++) {
      if (st.vol[c] * flip >= 0) continue;
      const o = st.first[c] * 9;
      let best = -1;
      for (let p = 0; p < count; p++) {
        if (p === c || objOf(p) !== objOf(c) || st.vol[p] * flip <= 0 || !boxIn(c, p) || best >= 0 && st.vol[p] * flip >= st.vol[best] * flip) continue;
        if (crossings(bv, P, P[o], P[o + 1], P[o + 2], 0.8017837, 0.5345225, 0.2672612, (u) => part[u] === p) % 2 === 1) best = p;
      }
      if (best >= 0) {
        into[c] = best;
        folded = true;
      }
    }
    if (folded) {
      const renum = new Int32Array(count).fill(-1);
      let k = 0;
      for (let p = 0; p < count; p++) if (into[p] === p) renum[p] = k++;
      for (let t = 0; t < n; t++) part[t] = renum[into[part[t]]];
      count = k;
      st = measure();
    }
  }
  const box = st.box, GAP = PART_GAP_MM + 1e-3;
  const nth = new Int32Array(count), per = new Int32Array(G);
  for (let p = 0; p < count; p++) nth[p] = ++per[objOf(p)];
  const words = (p) => partWords({
    name: objects?.names[objOf(p)] ?? null,
    piece: nth[p],
    pieces: per[objOf(p)],
    min: [box[p * 6], box[p * 6 + 1], box[p * 6 + 2]],
    max: [box[p * 6 + 3], box[p * 6 + 4], box[p * 6 + 5]]
  });
  const low = (p) => box[p * 6 + 2];
  if (count > MAX_PARTS) return { count, judged: false, listed: false, items: [], inTheAir: [] };
  const links = Array.from({ length: count }, () => /* @__PURE__ */ new Set());
  const rests = Array.from({ length: count }, () => /* @__PURE__ */ new Set());
  const link = (a, b) => {
    if (a !== b) {
      links[a].add(b);
      links[b].add(a);
    }
  };
  const rest = (p, q) => {
    if (p !== q && low(q) <= low(p) + GAP) rests[p].add(q);
  };
  for (let i = 0; i < shared.length; i += 3) {
    const a = part[shared[i]], b = part[shared[i + 1]], z = shared[i + 2];
    link(a, b);
    if (z <= low(a) + GAP) rest(a, b);
    if (z <= low(b) + GAP) rest(b, a);
  }
  const held = new Array(count).fill(true);
  const inTheAir = [];
  let judged = true;
  if (count > 1) {
    const bv = bvh();
    let rays = 0;
    const meets = (a, b, top) => {
      const lo = [box[b * 6] - GAP, box[b * 6 + 1] - GAP, box[b * 6 + 2] - GAP], hi = [box[b * 6 + 3] + GAP, box[b * 6 + 4] + GAP, Math.min(box[b * 6 + 5], top) + GAP];
      if (hi[2] < lo[2]) return false;
      const near = [];
      inBox(bv, P, lo, hi, (t) => {
        if (part[t] === a) near.push(t);
        return near.length < 4096;
      });
      const step = Math.max(1, Math.floor(near.length / 8));
      for (let i = 0; i < near.length; i += step) {
        const t = near[i], o = t * 9;
        const nx = normals[t * 3] * flip, ny = normals[t * 3 + 1] * flip, nz = normals[t * 3 + 2] * flip;
        if (!nx && !ny && !nz) continue;
        const cx = (P[o] + P[o + 3] + P[o + 6]) / 3, cy = (P[o + 1] + P[o + 4] + P[o + 7]) / 3, cz = (P[o + 2] + P[o + 5] + P[o + 8]) / 3;
        const lowest = [0, 3, 6].reduce((m, k) => P[o + k + 2] < P[o + m + 2] ? k : m, 0);
        const points = top === Infinity ? [[cx, cy, cz], [(cx + P[o]) / 2, (cy + P[o + 1]) / 2, (cz + P[o + 2]) / 2]] : [[P[o + lowest] + (cx - P[o + lowest]) * 0.02, P[o + lowest + 1] + (cy - P[o + lowest + 1]) * 0.02, P[o + lowest + 2] + (cz - P[o + lowest + 2]) * 0.02]];
        for (const [x, y, z] of points) {
          if (z > top + GAP) continue;
          rays++;
          const h = nearest(bv, P, x - nx * GAP, y - ny * GAP, z - nz * GAP, nx, ny, nz, (u) => part[u] === b);
          if (h && ((normals[h.t * 3] * nx + normals[h.t * 3 + 1] * ny + normals[h.t * 3 + 2] * nz) * flip > 0.3 || h.d <= 2 * GAP)) return true;
        }
      }
      return false;
    };
    const byX = Array.from({ length: count }, (_, i) => i).sort((a, b) => box[a * 6] - box[b * 6]);
    pairs: for (let i = 0; i < count; i++) {
      const a = byX[i];
      for (let j = i + 1; j < count; j++) {
        const b = byX[j];
        if (box[b * 6] > box[a * 6 + 3] + GAP) break;
        if ([1, 2].some((k) => box[b * 6 + k] > box[a * 6 + 3 + k] + GAP || box[a * 6 + k] > box[b * 6 + 3 + k] + GAP)) continue;
        if (!links[a].has(b) && (meets(a, b, Infinity) || meets(b, a, Infinity))) link(a, b);
        if (links[a].has(b)) {
          if (!rests[a].has(b) && low(b) <= low(a) + GAP && (meets(a, b, low(a) + GAP) || meets(b, a, low(a) + GAP))) rest(a, b);
          if (!rests[b].has(a) && low(a) <= low(b) + GAP && (meets(b, a, low(b) + GAP) || meets(a, b, low(b) + GAP))) rest(b, a);
        }
        if (rays > PART_RAYS) {
          judged = false;
          break pairs;
        }
      }
    }
    const below = new Array(count);
    const from = new Int32Array(count + 1), byPart = new Int32Array(n);
    for (let t = 0; t < n; t++) from[part[t] + 1]++;
    for (let p = 0; p < count; p++) from[p + 1] += from[p];
    for (let t = 0, at = from.slice(0, count); t < n; t++) byPart[at[part[t]]++] = t;
    for (let p = 0; p < count && judged; p++) {
      if (low(p) - minZ <= GAP) continue;
      const corners = [];
      for (let i = from[p]; i < from[p + 1] && corners.length < 3 * 4096; i++) {
        const o = byPart[i] * 9;
        for (let k = 0; k < 9; k += 3) if (P[o + k + 2] <= low(p) + GAP) corners.push(P[o + k], P[o + k + 1], P[o + k + 2]);
      }
      const m = corners.length / 3, step = Math.max(1, Math.floor(m / 16));
      let gap = Infinity, over = null;
      for (let i = 0; i < m; i += step) {
        const x = corners[i * 3], y = corners[i * 3 + 1], z = corners[i * 3 + 2];
        rays++;
        const h = nearest(bv, P, x, y, z + GAP, 0, 0, -1, (u) => part[u] !== p);
        if (h && (-normals[h.t * 3 + 2] * flip > 0.3 || h.d <= 2 * GAP)) {
          link(p, part[h.t]);
          rest(p, part[h.t]);
          gap = 0;
          break;
        }
        if (h && h.d - GAP < gap) {
          gap = h.d - GAP;
          over = part[h.t];
        } else if (!h && z - minZ < gap) {
          gap = z - minZ;
          over = null;
        }
      }
      below[p] = { mm: gap, over };
      if (rays > PART_RAYS) judged = false;
    }
    if (judged) {
      const on = Array.from({ length: count }, () => []);
      for (let p = 0; p < count; p++) for (const q of rests[p]) on[q].push(p);
      for (let p = 0; p < count; p++) held[p] = low(p) - minZ <= GAP;
      const queue = held.flatMap((h, p) => h ? [p] : []);
      while (queue.length) {
        const q = queue.pop();
        for (const p of on[q]) if (!held[p]) {
          held[p] = true;
          queue.push(p);
        }
      }
      const seen = new Uint8Array(count);
      for (let p = 0; p < count; p++) {
        if (held[p] || seen[p]) continue;
        const group = [p];
        seen[p] = 1;
        for (let i = 0; i < group.length; i++) for (const q of links[group[i]]) if (!held[q] && !seen[q]) {
          seen[q] = 1;
          group.push(q);
        }
        const lowest = group.reduce((a, q) => low(q) < low(a) ? q : a);
        const b = below[lowest] ?? { mm: low(lowest) - minZ, over: null };
        inTheAir.push({ part: lowest, words: words(lowest), mm: r2(b.mm), over: b.over === null ? "the bed" : words(b.over), overPart: b.over, resting: group.length - 1 });
      }
    }
  }
  const items = [];
  for (let p = 0; p < count; p++) {
    const g = objOf(p);
    items.push({
      name: objects?.names[g] ?? null,
      from: objects?.from[g] ?? null,
      piece: nth[p],
      pieces: per[g],
      triangles: st.tris[p],
      firstTriangle: st.first[p],
      min: [r2(box[p * 6]), r2(box[p * 6 + 1]), r2(box[p * 6 + 2])],
      max: [r2(box[p * 6 + 3]), r2(box[p * 6 + 4]), r2(box[p * 6 + 5])],
      lowestMm: r2(low(p) - minZ),
      held: judged ? held[p] : null,
      touches: [...links[p]].sort((a, b) => a - b)
    });
  }
  return { count, judged, listed: true, items, inTheAir: judged ? inTheAir : [] };
}
function crossings(bv, P, ox, oy, oz, dx, dy, dz, take) {
  const ix = 1 / dx, iy = 1 / dy, iz = 1 / dz;
  let hits = 0;
  const stack = [0];
  while (stack.length) {
    const node = stack.pop();
    const b = node * 3;
    let t0 = ((ix >= 0 ? bv.min[b] : bv.max[b]) - ox) * ix, t1 = ((ix >= 0 ? bv.max[b] : bv.min[b]) - ox) * ix;
    const ty0 = ((iy >= 0 ? bv.min[b + 1] : bv.max[b + 1]) - oy) * iy, ty1 = ((iy >= 0 ? bv.max[b + 1] : bv.min[b + 1]) - oy) * iy;
    if (ty0 > t0) t0 = ty0;
    if (ty1 < t1) t1 = ty1;
    const tz0 = ((iz >= 0 ? bv.min[b + 2] : bv.max[b + 2]) - oz) * iz, tz1 = ((iz >= 0 ? bv.max[b + 2] : bv.min[b + 2]) - oz) * iz;
    if (tz0 > t0) t0 = tz0;
    if (tz1 < t1) t1 = tz1;
    if (t0 > t1 || t1 < 0) continue;
    if (bv.left[node] < 0) {
      for (let i = bv.start[node]; i < bv.start[node] + bv.count[node]; i++) {
        const t = bv.order[i];
        if (!take(t)) continue;
        const d = hitTriangle(P, t * 9, ox, oy, oz, dx, dy, dz);
        if (d !== null && d > 1e-6) hits++;
      }
    } else stack.push(bv.left[node], bv.right[node]);
  }
  return hits;
}
function overhangs(P, n, tri, normals, areas, flip, minZ, marks) {
  const cand = new Uint8Array(n);
  let any = false;
  for (let t = 0; t < n; t++) {
    if (marks[t] || collapsed(tri, t)) continue;
    const o = t * 9;
    const mid = (P[o + 2] + P[o + 5] + P[o + 8]) / 3;
    if (normals[t * 3 + 2] * flip < OVERHANG_NZ && mid - minZ > 0.2) {
      cand[t] = 1;
      any = true;
    }
  }
  if (!any) return 0;
  const V = tri.reduce((m, v) => v > m ? v : m, 0) + 1;
  const edges = /* @__PURE__ */ new Map();
  const key = (a, b) => Math.min(a, b) * V + Math.max(a, b);
  for (let t = 0; t < n; t++) if (cand[t]) for (let k = 0; k < 3; k++) edges.set(key(tri[t * 3 + k], tri[t * 3 + (k + 1) % 3]), []);
  for (let t = 0; t < n; t++) {
    if (collapsed(tri, t)) continue;
    for (let k = 0; k < 3; k++) edges.get(key(tri[t * 3 + k], tri[t * 3 + (k + 1) % 3]))?.push(t);
  }
  const parent = new Int32Array(n).map((_, i) => i);
  const find = (x) => {
    while (parent[x] !== x) {
      parent[x] = parent[parent[x]];
      x = parent[x];
    }
    return x;
  };
  for (const ts of edges.values()) {
    const c = ts.filter((t) => cand[t]);
    for (let i = 1; i < c.length; i++) parent[find(c[i])] = find(c[0]);
  }
  const regions = /* @__PURE__ */ new Map();
  const region = (t) => {
    const r = find(t);
    let g = regions.get(r);
    if (!g) regions.set(r, g = { area: 0, level: 0, outline: 0, held: [], tris: [] });
    return g;
  };
  for (let t = 0; t < n; t++) {
    if (!cand[t]) continue;
    const g = region(t);
    g.area += areas[t];
    g.level += areas[t] * Math.abs(normals[t * 3 + 2]);
    g.tris.push(t);
    for (let k = 0; k < 3; k++) {
      const ka = t * 3 + k, kb = t * 3 + (k + 1) % 3;
      const across = edges.get(key(tri[ka], tri[kb])).filter((u2) => u2 !== t);
      if (across.some((u2) => cand[u2])) continue;
      const a = t * 9 + k * 3, b = t * 9 + (k + 1) % 3 * 3, c = t * 9 + (k + 2) % 3 * 3;
      const ex = P[b] - P[a], ey = P[b + 1] - P[a + 1], len = Math.hypot(ex, ey);
      if (!len) continue;
      g.outline += len;
      const u = across[0];
      if (u === void 0 || !runsDown(P, u, a, b)) continue;
      let ox = ey / len, oy = -ex / len;
      if (ox * (P[c] - P[a]) + oy * (P[c + 1] - P[a + 1]) > 0) {
        ox = -ox;
        oy = -oy;
      }
      g.held.push({ x: (P[a] + P[b]) / 2, y: (P[a + 1] + P[b + 1]) / 2, ox, oy });
    }
  }
  let total = 0;
  for (const g of regions.values()) {
    const width = g.outline > 0 ? 2 * g.level / g.outline : Math.sqrt(g.level);
    if (width <= SELF_SUPPORT_MM) continue;
    const span = bridgeSpan(g.held);
    if (span !== null && span <= BRIDGE_MM) continue;
    total += g.area;
    for (const t of g.tris) marks[t] = 2;
  }
  return total;
}
function bridgeSpan(held) {
  const step = Math.ceil(held.length / 300);
  let span = null;
  for (let i = 0; i < held.length; i += step) for (let j = 0; j < held.length; j += step) {
    const a = held[i], b = held[j];
    if (a.ox * b.ox + a.oy * b.oy > -0.5) continue;
    const across = -((b.x - a.x) * a.ox + (b.y - a.y) * a.oy);
    if (across > 0 && (span === null || across > span)) span = across;
  }
  return span;
}
function runsDown(P, u, a, b) {
  const o = u * 9;
  let far = o;
  let best = -1;
  const ex = P[b] - P[a], ey = P[b + 1] - P[a + 1], ez = P[b + 2] - P[a + 2], el = Math.hypot(ex, ey, ez) || 1;
  for (let k = 0; k < 9; k += 3) {
    const vx2 = P[o + k] - P[a], vy2 = P[o + k + 1] - P[a + 1], vz2 = P[o + k + 2] - P[a + 2];
    const along2 = (vx2 * ex + vy2 * ey + vz2 * ez) / el;
    const d = vx2 * vx2 + vy2 * vy2 + vz2 * vz2 - along2 * along2;
    if (d > best) {
      best = d;
      far = o + k;
    }
  }
  const vx = P[far] - P[a], vy = P[far + 1] - P[a + 1], vz = P[far + 2] - P[a + 2];
  const along = (vx * ex + vy * ey + vz * ez) / (el * el);
  const px = vx - along * ex, py = vy - along * ey, pz = vz - along * ez;
  return pz < -0.3 * Math.hypot(px, py, pz);
}
function bestFlatSide(P, n, normals, areas, flip) {
  const plane = (t) => {
    const nx = normals[t * 3] * flip, ny = normals[t * 3 + 1] * flip, nz = normals[t * 3 + 2] * flip;
    const d = nx * P[t * 9] + ny * P[t * 9 + 1] + nz * P[t * 9 + 2];
    return { nx, ny, nz, d, k: [Math.round(nx * 50), Math.round(ny * 50), Math.round(nz * 50), Math.round(d * 10)] };
  };
  let cap = 1;
  while (cap < n * 2) cap *= 2;
  const slots = new Int32Array(cap).fill(-1), firstOf = new Int32Array(n), sum = new Float64Array(n);
  let G = 0;
  for (let t = 0; t < n; t++) {
    if (!areas[t]) continue;
    const { k } = plane(t);
    let slot = (Math.imul(k[0], 73856093) ^ Math.imul(k[1], 19349663) ^ Math.imul(k[2], 83492791) ^ Math.imul(k[3] | 0, 2654435761)) & cap - 1;
    for (; ; ) {
      const g = slots[slot];
      if (g < 0) {
        slots[slot] = G;
        firstOf[G] = t;
        sum[G++] = areas[t];
        break;
      }
      const o = plane(firstOf[g]).k;
      if (o[0] === k[0] && o[1] === k[1] && o[2] === k[2] && o[3] === k[3]) {
        sum[g] += areas[t];
        break;
      }
      slot = slot + 1 & cap - 1;
    }
  }
  const best = [];
  for (let g = 0; g < G; g++) {
    if (best.length === 24 && sum[g] <= sum[best[23]]) continue;
    let i = best.length;
    while (i > 0 && sum[best[i - 1]] < sum[g]) i--;
    best.splice(i, 0, g);
    if (best.length > 24) best.pop();
  }
  const top = best.map((g) => ({ ...plane(firstOf[g]), area: sum[g] }));
  for (const g of top) {
    let max = -Infinity;
    for (let i = 0; i < P.length; i += 3) max = Math.max(max, g.nx * P[i] + g.ny * P[i + 1] + g.nz * P[i + 2]);
    if (max - g.d < 0.05) return { area: g.area, name: sideName(g.nx, g.ny, g.nz) };
  }
  return { area: 0, name: "" };
}
function sideName(x, y, z) {
  const a = [[Math.abs(x), x > 0 ? "right" : "left"], [Math.abs(y), y > 0 ? "back" : "front"], [Math.abs(z), z > 0 ? "top" : "bottom"]];
  const [v, name] = a.sort((p, q) => q[0] - p[0])[0];
  return v > 0.97 ? `its ${name} face` : "its largest flat face";
}
function thickness(P, n, normals, areas, flip, solidEnough, minWall, built) {
  const thinTris = [];
  if (!solidEnough) return { sampled: 0, under04Pct: 0, under08Pct: 0, minMm: null, printsAt: null, thinTris };
  const bvh = built();
  const cum = new Float64Array(n);
  let acc = 0;
  for (let t = 0; t < n; t++) {
    acc += areas[t];
    cum[t] = acc;
  }
  let seed = 12345;
  const rand = () => (seed = seed * 1103515245 + 12345 & 2147483647) / 2147483647;
  const samples = Math.min(RAY_SAMPLES, n * 4);
  let hit = 0, u04 = 0, u08 = 0, min = Infinity;
  const needs = [];
  for (let s = 0; s < samples; s++) {
    const r = rand() * acc;
    let lo = 0, hi = n - 1;
    while (lo < hi) {
      const mid = lo + hi >> 1;
      if (cum[mid] < r) lo = mid + 1;
      else hi = mid;
    }
    const t = lo, o = t * 9;
    let a = rand(), b = rand();
    if (a + b > 1) {
      a = 1 - a;
      b = 1 - b;
    }
    const px = P[o] + a * (P[o + 3] - P[o]) + b * (P[o + 6] - P[o]);
    const py = P[o + 1] + a * (P[o + 4] - P[o + 1]) + b * (P[o + 7] - P[o + 1]);
    const pz = P[o + 2] + a * (P[o + 5] - P[o + 2]) + b * (P[o + 8] - P[o + 2]);
    const dx = -normals[t * 3] * flip, dy = -normals[t * 3 + 1] * flip, dz = -normals[t * 3 + 2] * flip;
    const h = raycast(bvh, P, normals, flip, px + dx * 1e-4, py + dy * 1e-4, pz + dz * 1e-4, dx, dy, dz, t);
    if (h === null) continue;
    hit++;
    if (h.facing < PARALLEL) continue;
    const d = h.d;
    const vertical = Math.abs(dz) > 0.7, fail = vertical ? LAYER_MM : NOZZLE_MM, warn = vertical ? MIN_SLAB_MM : minWall;
    needs.push(warn / d);
    if (d < warn) min = Math.min(min, d);
    if (d < fail) {
      u04++;
      thinTris.push(t);
    } else if (d < warn) {
      u08++;
      thinTris.push(t);
    }
  }
  needs.sort((a, b) => b - a);
  const printsAt = needs.length ? needs[Math.floor(needs.length * 1e-3)] : null;
  return { sampled: hit, under04Pct: hit ? round1(u04 / hit * 100) : 0, under08Pct: hit ? round1((u04 + u08) / hit * 100) : 0, minMm: min < Infinity ? Math.round(min * 100) / 100 : null, printsAt, thinTris };
}
function buildBvh(P, n) {
  const cap = n * 2;
  const bv = { min: new Float32Array(cap * 3), max: new Float32Array(cap * 3), left: new Int32Array(cap).fill(-1), right: new Int32Array(cap).fill(-1), start: new Int32Array(cap), count: new Int32Array(cap), order: new Int32Array(n) };
  const cen = new Float32Array(n * 3);
  for (let t = 0; t < n; t++) {
    bv.order[t] = t;
    for (let k = 0; k < 3; k++) cen[t * 3 + k] = (P[t * 9 + k] + P[t * 9 + 3 + k] + P[t * 9 + 6 + k]) / 3;
  }
  let nodes = 0;
  const stack = [[nodes++, 0, n]];
  while (stack.length) {
    const [node, start, end] = stack.pop();
    let mnx = Infinity, mny = Infinity, mnz = Infinity, mxx = -Infinity, mxy = -Infinity, mxz = -Infinity;
    const cmn = [Infinity, Infinity, Infinity], cmx = [-Infinity, -Infinity, -Infinity];
    for (let i2 = start; i2 < end; i2++) {
      const t = bv.order[i2];
      for (let k = 0; k < 9; k += 3) {
        const x = P[t * 9 + k], y = P[t * 9 + k + 1], z = P[t * 9 + k + 2];
        if (x < mnx) mnx = x;
        if (x > mxx) mxx = x;
        if (y < mny) mny = y;
        if (y > mxy) mxy = y;
        if (z < mnz) mnz = z;
        if (z > mxz) mxz = z;
      }
      for (let k = 0; k < 3; k++) {
        cmn[k] = Math.min(cmn[k], cen[t * 3 + k]);
        cmx[k] = Math.max(cmx[k], cen[t * 3 + k]);
      }
    }
    bv.min.set([mnx, mny, mnz], node * 3);
    bv.max.set([mxx, mxy, mxz], node * 3);
    const span = [cmx[0] - cmn[0], cmx[1] - cmn[1], cmx[2] - cmn[2]];
    const axis = span[0] >= span[1] && span[0] >= span[2] ? 0 : span[1] >= span[2] ? 1 : 2;
    if (end - start <= 4 || span[axis] <= 0) {
      bv.start[node] = start;
      bv.count[node] = end - start;
      continue;
    }
    const mid = (cmn[axis] + cmx[axis]) / 2;
    let i = start, j = end - 1;
    while (i <= j) {
      if (cen[bv.order[i] * 3 + axis] < mid) i++;
      else {
        const tmp = bv.order[i];
        bv.order[i] = bv.order[j];
        bv.order[j] = tmp;
        j--;
      }
    }
    const split = i === start || i === end ? start + end >> 1 : i;
    const l = nodes++, r = nodes++;
    bv.left[node] = l;
    bv.right[node] = r;
    stack.push([l, start, split], [r, split, end]);
  }
  return bv;
}
function raycast(bv, P, normals, flip, ox, oy, oz, dx, dy, dz, skip) {
  const ix = 1 / dx, iy = 1 / dy, iz = 1 / dz;
  let best = Infinity, bestFacing = 0;
  const stack = [0];
  while (stack.length) {
    const node = stack.pop();
    const b = node * 3;
    let t0 = ((ix >= 0 ? bv.min[b] : bv.max[b]) - ox) * ix, t1 = ((ix >= 0 ? bv.max[b] : bv.min[b]) - ox) * ix;
    const ty0 = ((iy >= 0 ? bv.min[b + 1] : bv.max[b + 1]) - oy) * iy, ty1 = ((iy >= 0 ? bv.max[b + 1] : bv.min[b + 1]) - oy) * iy;
    if (ty0 > t0) t0 = ty0;
    if (ty1 < t1) t1 = ty1;
    const tz0 = ((iz >= 0 ? bv.min[b + 2] : bv.max[b + 2]) - oz) * iz, tz1 = ((iz >= 0 ? bv.max[b + 2] : bv.min[b + 2]) - oz) * iz;
    if (tz0 > t0) t0 = tz0;
    if (tz1 < t1) t1 = tz1;
    if (t0 > t1 || t1 < 0 || t0 > best) continue;
    if (bv.left[node] < 0) {
      for (let i = bv.start[node]; i < bv.start[node] + bv.count[node]; i++) {
        const t = bv.order[i];
        if (t === skip) continue;
        const facing = (normals[t * 3] * dx + normals[t * 3 + 1] * dy + normals[t * 3 + 2] * dz) * flip;
        if (facing < 0.3) continue;
        const d = hitTriangle(P, t * 9, ox, oy, oz, dx, dy, dz);
        if (d !== null && d > 1e-4 && d < best) {
          best = d;
          bestFacing = facing;
        }
      }
    } else stack.push(bv.left[node], bv.right[node]);
  }
  return best === Infinity ? null : { d: best, facing: bestFacing };
}
function nearest(bv, P, ox, oy, oz, dx, dy, dz, take) {
  const ix = 1 / dx, iy = 1 / dy, iz = 1 / dz;
  let best = Infinity, bestT = -1;
  const stack = [0];
  while (stack.length) {
    const node = stack.pop();
    const b = node * 3;
    let t0 = ((ix >= 0 ? bv.min[b] : bv.max[b]) - ox) * ix, t1 = ((ix >= 0 ? bv.max[b] : bv.min[b]) - ox) * ix;
    const ty0 = ((iy >= 0 ? bv.min[b + 1] : bv.max[b + 1]) - oy) * iy, ty1 = ((iy >= 0 ? bv.max[b + 1] : bv.min[b + 1]) - oy) * iy;
    if (ty0 > t0) t0 = ty0;
    if (ty1 < t1) t1 = ty1;
    const tz0 = ((iz >= 0 ? bv.min[b + 2] : bv.max[b + 2]) - oz) * iz, tz1 = ((iz >= 0 ? bv.max[b + 2] : bv.min[b + 2]) - oz) * iz;
    if (tz0 > t0) t0 = tz0;
    if (tz1 < t1) t1 = tz1;
    if (t0 > t1 || t1 < 0 || t0 > best) continue;
    if (bv.left[node] < 0) {
      for (let i = bv.start[node]; i < bv.start[node] + bv.count[node]; i++) {
        const t = bv.order[i];
        if (!take(t)) continue;
        const d = hitTriangle(P, t * 9, ox, oy, oz, dx, dy, dz);
        if (d !== null && d > 0 && d < best) {
          best = d;
          bestT = t;
        }
      }
    } else stack.push(bv.left[node], bv.right[node]);
  }
  return bestT < 0 ? null : { d: best, t: bestT };
}
function inBox(bv, P, lo, hi, visit) {
  const stack = [0];
  while (stack.length) {
    const node = stack.pop();
    if ([0, 1, 2].some((a) => bv.min[node * 3 + a] > hi[a] || bv.max[node * 3 + a] < lo[a])) continue;
    if (bv.left[node] >= 0) {
      stack.push(bv.left[node], bv.right[node]);
      continue;
    }
    for (let i = bv.start[node]; i < bv.start[node] + bv.count[node]; i++) {
      const t = bv.order[i], o = t * 9;
      if ([0, 1, 2].some((a) => Math.min(P[o + a], P[o + 3 + a], P[o + 6 + a]) > hi[a] || Math.max(P[o + a], P[o + 3 + a], P[o + 6 + a]) < lo[a])) continue;
      if (!visit(t)) return;
    }
  }
}
function hitTriangle(P, o, ox, oy, oz, dx, dy, dz) {
  const e1x = P[o + 3] - P[o], e1y = P[o + 4] - P[o + 1], e1z = P[o + 5] - P[o + 2];
  const e2x = P[o + 6] - P[o], e2y = P[o + 7] - P[o + 1], e2z = P[o + 8] - P[o + 2];
  const px = dy * e2z - dz * e2y, py = dz * e2x - dx * e2z, pz = dx * e2y - dy * e2x;
  const det = e1x * px + e1y * py + e1z * pz;
  if (Math.abs(det) < 1e-12) return null;
  const inv = 1 / det;
  const tx = ox - P[o], ty = oy - P[o + 1], tz = oz - P[o + 2];
  const u = (tx * px + ty * py + tz * pz) * inv;
  if (u < 0 || u > 1) return null;
  const qx = ty * e1z - tz * e1y, qy = tz * e1x - tx * e1z, qz = tx * e1y - ty * e1x;
  const v = (dx * qx + dy * qy + dz * qz) * inv;
  if (v < 0 || u + v > 1) return null;
  return (e2x * qx + e2y * qy + e2z * qz) * inv;
}
var round1 = (v) => Math.round(v * 10) / 10;
var mm = (v) => `${round1(v)} mm`;
var upper = (s) => s.charAt(0).toUpperCase() + s.slice(1);
function findingsFor(r) {
  const f = [];
  const [x, y, z] = r.size, longest = Math.max(x, y, z);
  if (longest < 4) f.push({ id: "units-small", level: "warn", title: "It is tiny: maybe the wrong units", detail: `The whole part is ${mm(longest)} across. If it was drawn in inches it is ${mm(longest * 25.4)}; in centimetres ${mm(longest * 10)}. Scale it in your slicer if so.` });
  if (longest > 1500) f.push({ id: "units-large", level: "warn", title: "It is huge: maybe the wrong units", detail: `The part is ${mm(longest)} across. If it was drawn in micrometres or tenths of a millimetre, scale it down.` });
  const anyFit = r.fits.some((p) => p.asIs || p.turned);
  if (!anyFit) f.push({ id: "too-big", level: "fail", title: "Too big for any of our printers", detail: `At ${mm(x)} by ${mm(y)} by ${mm(z)} it needs splitting into pieces, or scaling down.` });
  else if (!r.fits.some((p) => p.asIs)) f.push({ id: "turn-to-fit", level: "warn", title: "Fits only if turned", detail: `It fits ${r.fits.filter((p) => p.turned).map((p) => p.model).join(", ")} once turned on its side.` });
  const c = r.closed;
  if (c.boundaryEdges) f.push({ id: "open", level: c.boundaryEdges > r.triangles * 0.01 ? "fail" : "warn", title: "The surface is not closed", detail: `${c.boundaryEdges} open edge(s): gaps in the surface. A slicer can usually repair small gaps; large ones print as holes or not at all. Export it again as a solid, or run a repair.` });
  if (c.nonManifoldEdges) f.push({ id: "shared-edges", level: "warn", title: "Pieces touch along an edge", detail: `${c.nonManifoldEdges} edge(s) are shared by more than two faces. That is not a hole: it usually means two pieces of the model meet along a line, or an inside face was left in. The slicer prints it, but the part can be weak where pieces only touch. Joining them into one solid in your design program fixes it.` });
  if (c.inverted) f.push({ id: "inverted", level: "warn", title: "It is inside out", detail: "Every face points inward. Most slicers fix this on import; if yours shows it dark or empty, flip the normals." });
  else if (c.flippedEdges) f.push({ id: "flipped", level: "warn", title: "Some faces are flipped", detail: `${c.flippedEdges} edge(s) join faces that point opposite ways. The slicer may leave holes there.` });
  const fl = r.firstLayer;
  if (fl.contactMm2 < 1) f.push({ id: "no-first-layer", level: "fail", title: "It barely touches the bed", detail: `Only ${fl.contactMm2} mm² of the part sits flat on the bed: a point or an edge will not hold.${fl.bestSide ? ` Lay it on ${fl.bestSide} (${Math.round(fl.bestSideMm2)} mm²).` : " Add a flat base, a brim, or supports under it."}` });
  else if (z > 10 && fl.contactMm2 < fl.footprintMm2 * 0.15) f.push({ id: "small-first-layer", level: "warn", title: "A small first layer", detail: `${Math.round(fl.contactMm2)} mm² on the bed under a part ${mm(z)} tall. Add a brim${fl.bestSide ? `, or lay it on ${fl.bestSide} (${Math.round(fl.bestSideMm2)} mm²)` : ""}.` });
  else if (fl.bestSide) f.push({ id: "better-side", level: "ok", title: "A flatter side is available", detail: `Lying on ${fl.bestSide} puts ${Math.round(fl.bestSideMm2)} mm² on the bed instead of ${Math.round(fl.contactMm2)} mm².` });
  const air = r.parts.inTheAir;
  if (air.length) {
    const alike = /* @__PURE__ */ new Map();
    for (const a of air) alike.set(`${round1(a.mm)}|${a.over}`, [...alike.get(`${round1(a.mm)}|${a.over}`) ?? [], a]);
    const said = [...alike.values()].slice(0, 4).map((g) => g.length === 1 ? `${upper(g[0].words)} starts ${mm(g[0].mm)} above ${g[0].over}` : `${g.length} parts start ${mm(g[0].mm)} above ${g[0].over} (${g.slice(0, 3).map((a) => a.words).join(", ")}${g.length > 3 ? " and more" : ""})`);
    const resting = air.reduce((s, a) => s + a.resting, 0), them = air.length > 1 ? "them" : "it";
    f.push({
      id: "in-the-air",
      level: "warn",
      title: air.length === 1 ? "A part starts in the air" : `${air.length} parts start in the air`,
      detail: `${said.join("; ")}${alike.size > 4 ? "; and others" : ""}.${resting ? ` ${resting} more part${resting > 1 ? "s rest" : " rests"} on ${them}.` : ""} Nothing holds ${them} up there: without supports the printer lays the first layers on air. Move ${them} down onto the bed or onto the part below in your design program, or turn on supports.`
    });
  }
  if (r.parts.count > 1 && !r.parts.judged) f.push({ id: "parts-unchecked", level: "warn", title: "Could not check every part", detail: r.parts.count > MAX_PARTS ? `It has ${r.parts.count.toLocaleString("en-US")} separate parts, more than the ${MAX_PARTS.toLocaleString("en-US")} the check looks at one by one, so it did not check which of them would start in the air. Look over the parts in your design program, or join them into fewer solids.` : "Its parts meet in so many places that the check could not look at them all, so it did not check which of them would start in the air. Look over the parts in your design program." });
  if (fl.baseMm[0] >= WARP_BASE_MM && fl.contactMm2 >= WARP_CONTACT_MM2) f.push({ id: "warp", level: "warn", title: "A big flat base: its corners may lift", detail: `${Math.round(fl.contactMm2 / 100)} cm² sits flat on the bed across ${mm(fl.baseMm[0])} by ${mm(fl.baseMm[1])}. As a big part cools it shrinks, and its corners can curl up off the bed. A brim holds them down; a closed printer (the P2S or X1C) keeps the air around it warm and still; rounded corners in the design help most.` });
  if (r.overhangCm2 >= 0.5) f.push({ id: "overhang", level: "warn", title: "Needs supports", detail: `${r.overhangCm2} cm² hangs out over open air at less than ${SUPPORT_SLOPE_DEG} degrees from level, marked orange in the picture: our slicer holds that up with supports. Turn on supports, or turn the part so those faces point up.` });
  const t = r.thin;
  if (t.minMm !== null && t.under04Pct >= 0.5) f.push({ id: "too-thin", level: "fail", title: "Parts of it are too thin to print", detail: `About ${t.under04Pct}% of the sampled surface sits on a wall thinner than the printer can lay down (thinnest ${mm(t.minMm)}), marked red: under 0.4 mm side to side, one nozzle line, or 0.2 mm top to bottom, one layer. Those areas will be missing. Make walls at least ${+t.minWallMm.toFixed(2)} mm, two lines across, and flat parts at least ${mm(MIN_SLAB_MM)}.` });
  else if (t.minMm !== null && t.under08Pct >= 1) f.push({ id: "thin", level: "warn", title: "Some walls are very thin", detail: `About ${t.under08Pct}% of the sampled surface sits on a thin wall (thinnest ${mm(t.minMm)}), marked red. It prints as one or two lines or layers and breaks easily.` });
  const base = Math.max(1, Math.min(x, y));
  if (z > 30 && z / base > 8) f.push({ id: "tall-thin", level: "warn", title: "Tall and slender", detail: `${mm(z)} tall on a ${mm(base)} base: it may wobble or snap while printing. Add a brim, print it lying down, or print several together.` });
  if (!f.some((x2) => x2.level !== "ok")) f.unshift({ id: "looks-good", level: "ok", title: "Looks printable", detail: "This check found no problems. Small details may still be missed. A slice with our own settings gives the exact time and material." });
  return f;
}
var COLORS = { 0: [120, 150, 190], 1: [70, 170, 110], 2: [235, 150, 50], 3: [220, 60, 60] };
function render(mesh, marks, width = 640, height = 480) {
  const P = mesh.positions, n = mesh.triangles;
  const yaw = -Math.PI / 4, pitch = Math.atan(1 / Math.SQRT2);
  const cy = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
  const proj = new Float32Array(P.length);
  let minU = Infinity, maxU = -Infinity, minV = Infinity, maxV = -Infinity;
  for (let i = 0; i < P.length; i += 3) {
    const x = P[i] * cy - P[i + 1] * sy, yy = P[i] * sy + P[i + 1] * cy, z = P[i + 2];
    const u = x, v = z * cp + yy * sp, depth = z * sp - yy * cp;
    proj[i] = u;
    proj[i + 1] = v;
    proj[i + 2] = depth;
    minU = Math.min(minU, u);
    maxU = Math.max(maxU, u);
    minV = Math.min(minV, v);
    maxV = Math.max(maxV, v);
  }
  const scale = 0.9 * Math.min(width / Math.max(maxU - minU, 1e-6), height / Math.max(maxV - minV, 1e-6));
  const offU = (width - (maxU - minU) * scale) / 2, offV = (height - (maxV - minV) * scale) / 2;
  const img = new Uint8Array(width * height * 4);
  for (let i = 0; i < width * height; i++) {
    img[i * 4] = 248;
    img[i * 4 + 1] = 248;
    img[i * 4 + 2] = 250;
    img[i * 4 + 3] = 255;
  }
  const zbuf = new Float32Array(width * height).fill(-Infinity);
  const light = [0.4, -0.5, 0.77];
  for (let t = 0; t < n; t++) {
    const o = t * 9;
    const sx = [0, 1, 2].map((k) => (proj[o + k * 3] - minU) * scale + offU);
    const syy = [0, 1, 2].map((k) => height - ((proj[o + k * 3 + 1] - minV) * scale + offV));
    const sz = [0, 1, 2].map((k) => proj[o + k * 3 + 2]);
    const ax = P[o + 3] - P[o], ay = P[o + 4] - P[o + 1], az = P[o + 5] - P[o + 2], bx = P[o + 6] - P[o], by = P[o + 7] - P[o + 1], bz = P[o + 8] - P[o + 2];
    let nx = ay * bz - az * by, ny = az * bx - ax * bz, nz = ax * by - ay * bx;
    const len = Math.hypot(nx, ny, nz) || 1;
    nx /= len;
    ny /= len;
    nz /= len;
    const shade = 0.35 + 0.65 * Math.abs(nx * light[0] + ny * light[1] + nz * light[2]);
    const [r, g, b] = COLORS[marks[t]] ?? COLORS[0];
    const x0 = Math.max(0, Math.floor(Math.min(...sx))), x1 = Math.min(width - 1, Math.ceil(Math.max(...sx)));
    const y0 = Math.max(0, Math.floor(Math.min(...syy))), y1 = Math.min(height - 1, Math.ceil(Math.max(...syy)));
    const area = (sx[1] - sx[0]) * (syy[2] - syy[0]) - (sx[2] - sx[0]) * (syy[1] - syy[0]);
    if (Math.abs(area) < 1e-9) continue;
    for (let py = y0; py <= y1; py++) for (let px = x0; px <= x1; px++) {
      const qx = px + 0.5, qy = py + 0.5;
      const w0 = ((sx[1] - qx) * (syy[2] - qy) - (sx[2] - qx) * (syy[1] - qy)) / area;
      const w1 = ((sx[2] - qx) * (syy[0] - qy) - (sx[0] - qx) * (syy[2] - qy)) / area;
      const w2 = 1 - w0 - w1;
      if (w0 < 0 || w1 < 0 || w2 < 0) continue;
      const d = w0 * sz[0] + w1 * sz[1] + w2 * sz[2];
      const idx = py * width + px;
      if (d <= zbuf[idx]) continue;
      zbuf[idx] = d;
      img[idx * 4] = r * shade;
      img[idx * 4 + 1] = g * shade;
      img[idx * 4 + 2] = b * shade;
    }
  }
  return img;
}

// src/lib/check-job.ts
var CHECKER_FORMAT = 1;
var MAX_REPORT_CHARS = 6e5;
function checkFile(input, stage = () => {
}) {
  try {
    stage("reading");
    const mesh = parseModel(input.bytes, input.name);
    const stl = input.stl ? toBinaryStl(mesh) : null;
    const sized = sizeMesh(mesh, input.units, input.scale);
    const { marks, ...report } = analyze(mesh, input.models, input.minWall, stage);
    let rgba = null;
    if (input.picture) {
      stage("picture");
      rgba = render(mesh, marks);
    }
    return { ok: true, report: bounded(report), sized, rgba, stl };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e), plain: e instanceof PrintabilityError };
  }
}
function bounded(report) {
  if (JSON.stringify(report).length <= MAX_REPORT_CHARS) return report;
  const air = report.parts.inTheAir.map((a) => ({ ...a, part: null, overPart: null }));
  const trimmed = { ...report, parts: { ...report.parts, listed: false, items: [], inTheAir: air } };
  return JSON.stringify(trimmed).length <= MAX_REPORT_CHARS ? trimmed : { ...trimmed, parts: { ...trimmed.parts, inTheAir: air.slice(0, 100) } };
}

// src/checker/main.ts
var CHECKER_VERSION = true ? "c564b6b06692" : "source";
var JOB = process.env.PS_JOB_DIR ?? "/job";
function readJob(raw) {
  if (!raw || typeof raw !== "object") return "job.json is not an object";
  const j = raw;
  if (j.probe === true) return { probe: true };
  const units = readUnits(j.units);
  const models = Array.isArray(j.models) && j.models.length <= 8 && j.models.every((m) => typeof m === "string" && m.length <= 40) ? j.models : null;
  if (typeof j.name !== "string" || !/^model\.(stl|obj|3mf)$/i.test(j.name)) return "the job names no model file";
  if (!units) return "the job's units are not ones the check reads";
  if (typeof j.scale !== "number" || !Number.isFinite(j.scale) || j.scale <= 0) return "the job's scale is not a number above 0";
  if (!models) return "the job's printers are not a short list of names";
  if (typeof j.minWall !== "number" || !(j.minWall >= 0.1 && j.minWall <= 10)) return "the job's minimum wall is out of range";
  if (typeof j.picture !== "boolean" || typeof j.stl !== "boolean") return "the job does not say whether to draw the picture or keep the slicer's copy";
  return { name: j.name, units, scale: j.scale, models, minWall: j.minWall, picture: j.picture, stl: j.stl };
}
function png(rgba, width, height) {
  const row = width * 4 + 1, raw = Buffer.alloc(row * height);
  for (let y = 0; y < height; y++) raw.set(rgba.subarray(y * width * 4, (y + 1) * width * 4), y * row + 1);
  const chunk = (type, data) => {
    const body = Buffer.concat([Buffer.from(type, "latin1"), data]), out = Buffer.alloc(12 + data.length);
    out.writeUInt32BE(data.length, 0);
    body.copy(out, 4);
    out.writeUInt32BE(crc32(body), 8 + data.length);
    return out;
  };
  const head = Buffer.alloc(13);
  head.writeUInt32BE(width, 0);
  head.writeUInt32BE(height, 4);
  head[8] = 8;
  head[9] = 6;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk("IHDR", head), chunk("IDAT", deflateSync(raw)), chunk("IEND", Buffer.alloc(0))]);
}
var CRC = Uint32Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 3988292384 ^ c >>> 1 : c >>> 1;
  return c >>> 0;
});
function crc32(b) {
  let c = 4294967295;
  for (let i = 0; i < b.length; i++) c = CRC[(c ^ b[i]) & 255] ^ c >>> 8;
  return (c ^ 4294967295) >>> 0;
}
function runJob(dir) {
  const out = join(dir, "out");
  mkdirSync(out, { recursive: true });
  const write = (body) => writeFileSync(join(out, "report.json"), JSON.stringify({ format: CHECKER_FORMAT, version: CHECKER_VERSION, ...body }));
  let job;
  try {
    job = readJob(JSON.parse(readFileSync(join(dir, "job.json"), "utf8")));
  } catch {
    job = "job.json does not read";
  }
  if (typeof job === "string") return write({ ok: false, error: job, plain: false });
  if ("probe" in job) return write({ probe: true, maxTriangles: MAX_TRIANGLES, maxFileBytes: MAX_FILE_BYTES, maxModelBytes: MAX_INFLATED_BYTES });
  const file = readdirSync(dir).find((f) => f.toLowerCase() === job.name.toLowerCase());
  if (!file) return write({ ok: false, error: "the job's model file is missing", plain: false });
  const r = checkFile({ ...job, bytes: new Uint8Array(readFileSync(join(dir, file))) });
  if (!r.ok) return write(r);
  if (r.rgba) writeFileSync(join(out, "picture.png"), png(r.rgba, 640, 480));
  if (r.stl) writeFileSync(join(out, "model.stl"), r.stl);
  write({ ok: true, report: r.report, sized: r.sized });
}
function selftest() {
  const cube = Buffer.alloc(84 + 12 * 50);
  const p = (i) => [i & 1 ? 20 : 0, i & 2 ? 20 : 0, i & 4 ? 20 : 0];
  const tris = [[0, 2, 3, 1], [4, 5, 7, 6], [0, 1, 5, 4], [2, 6, 7, 3], [0, 4, 6, 2], [1, 3, 7, 5]].flatMap(([a, b, c, d]) => [[a, b, c], [a, c, d]]);
  cube.writeUInt32LE(12, 80);
  tris.forEach((t, i) => t.forEach((v, k) => p(v).forEach((c, j) => cube.writeFloatLE(c, 84 + i * 50 + 12 + k * 12 + j * 4))));
  const r = checkFile({ bytes: cube, name: "model.stl", units: "file", scale: 1, models: ["P2S"], minWall: 0.84, picture: true, stl: true });
  const ok = r.ok && r.report.findings.map((f) => f.id).join() === "looks-good" && r.report.parts.count === 1 && !!r.rgba && png(r.rgba, 640, 480).length > 100;
  console.log(ok ? "selftest ok" : `selftest failed: ${JSON.stringify(r).slice(0, 300)}`);
  process.exitCode = ok ? 0 : 1;
}
if (true) {
  if (process.argv[2] === "selftest") selftest();
  else if (process.argv[2] === "version") console.log(JSON.stringify({ format: CHECKER_FORMAT, version: CHECKER_VERSION }));
  else runJob(JOB);
}
export {
  CHECKER_VERSION,
  png,
  readJob,
  runJob
};
