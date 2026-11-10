// Raziel Hub - the universe on the page: her galaxy turning in the middle, each being a galaxy around her.
// Selecting one flies into it (its helpers orbit as small galaxies). Drawn on one canvas; the names are real
// buttons on top of it. Everything is built with DOM calls (the page's policy allows no inline HTML/CSS).
"use strict";

const Universe = (() => {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const SPEED = { idle: 1, listening: 1.3, thinking: 2.6, speaking: 1.7, sleeping: 0.25, unknown: 0.6 };
  const SVG = "http://www.w3.org/2000/svg";
  const FRAME_MS = 33;                 // ~30 fps: she shares this PC's CPU and GPU with the language model
  const el = {};
  let ctx, sx, dots = [], list = [], nexus = null, shown = null, builtKeys = "", onSelect = () => {};
  let z = 0, zGoal = 0, rot = 0, turn = 0, speed = 1, state = "idle", last = 0, lastStars = 0, visible = true;

  const rgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)).join(",");
  function particles(n) {
    const out = [];
    for (let i = 0; i < n; i++) {
      const r = Math.pow(Math.random(), 1.5);
      out.push({ r, a: (i % 2) * Math.PI + r * 5.6 + (Math.random() - 0.5) * (1.1 * (1 - r) + 0.3),
                 s: 0.5 + Math.random() * 1.1, k: Math.random() });
    }
    return out;
  }
  const nexusParts = particles(700), nodeParts = particles(36);
  const node = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; };

  function init(opts) {
    onSelect = opts.onSelect || onSelect;
    for (const id of ["stage", "scene", "stars", "tag", "gals", "inside", "ticks", "glyph", "iname", "isub", "nodes", "back"]) {
      el[id] = document.getElementById(id);
    }
    ctx = el.scene.getContext("2d");
    sx = el.stars.getContext("2d");
    const ring = document.createElementNS(SVG, "circle");
    for (const [k, v] of [["cx", 50], ["cy", 50], ["r", 43], ["fill", "none"], ["stroke", "currentColor"], ["stroke-width", 0.7]]) ring.setAttribute(k, v);
    el.ticks.append(ring);
    for (let a = 0; a < 48; a++) {
      const th = a * Math.PI / 24, r1 = a % 4 === 0 ? 45 : 47.5, line = document.createElementNS(SVG, "line");
      for (const [k, v] of [["x1", 50 + r1 * Math.cos(th)], ["y1", 50 + r1 * Math.sin(th)], ["x2", 50 + 50 * Math.cos(th)],
                            ["y2", 50 + 50 * Math.sin(th)], ["stroke", "currentColor"], ["stroke-width", 0.5]]) line.setAttribute(k, v);
      el.ticks.append(line);
    }
    el.back.addEventListener("click", () => onSelect("nexus"));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && zGoal === 1 && !document.querySelector("dialog[open]")) onSelect("nexus"); });
    window.addEventListener("resize", size);
    if ("IntersectionObserver" in window) {
      new IntersectionObserver((entries) => { visible = entries[0].isIntersecting; }).observe(el.stage);
    }
    size();
    if (!reduce) requestAnimationFrame(tick);
  }

  function setBeings(beings) {
    const keys = beings.map((b) => b.key).join(",");
    if (!beings.length || keys === builtKeys) return;          // the same beings as last time: nothing to rebuild
    builtKeys = keys;
    while (el.gals.firstChild) el.gals.removeChild(el.gals.firstChild);
    nexus = null; list = [];
    beings.forEach((b) => {
      const g = { key: b.key, name: b.name, title: b.title, role: b.role, glyph: b.glyph, color: b.color, rgb: rgb(b.color),
                  helpers: (b.helpers || []).slice(0, 6), l: 0, on: false, x: 0, y: 0 };
      if (b.key === "nexus") {
        g.el = node("div", "gal nexus");
        g.hit = g.el.appendChild(node("div", "hit"));
        g.el.append(node("span", "name", b.name));
        g.sub = g.el.appendChild(node("span", "sub", ""));
        nexus = g;
      } else {
        g.parts = particles(200); g.d = [1, 0.9, 1, 0.93, 1, 0.88][list.length % 6];
        g.el = node("button", "gal");
        g.el.style.setProperty("--col", b.color);
        g.el.setAttribute("aria-label", `Open ${b.name}, ${b.title}`);
        g.hit = g.el.appendChild(node("div", "hit"));
        g.el.append(node("span", "name", b.name), node("span", "sub", b.title));
        g.el.addEventListener("click", () => onSelect(b.key));
        list.push(g);
      }
      el.gals.append(g.el);
    });
    while (el.nodes.firstChild) el.nodes.removeChild(el.nodes.firstChild);
    el.nodeEls = [0, 1, 2, 3, 4, 5].map(() => el.nodes.appendChild(node("span", "node")));
    draw(0);
  }

  function setFocus(key) {
    const b = list.find((g) => g.key === key) || null;
    const was = zGoal;
    zGoal = b ? 1 : 0;
    if (b) {
      shown = b;
      el.inside.style.setProperty("--col", b.color);
      el.ticks.style.color = b.color;
      el.glyph.textContent = b.glyph;
      el.iname.textContent = b.name;
      el.isub.textContent = b.title.toLowerCase().includes(String(b.role).toLowerCase()) ? b.title : `${b.title} · ${b.role}`;
      el.nodeEls.forEach((n, i) => { n.textContent = b.helpers[i] || ""; });
    }
    el.back.hidden = !b;
    if (b && !was) el.back.focus({ preventScroll: true });
    else if (!b && was && shown) shown.el.focus({ preventScroll: true });
    if (reduce) { z = zGoal; draw(0); }
  }

  function setState(s, text) {
    state = s;
    if (nexus) nexus.sub.textContent = text || s;
    if (reduce) draw(0);
  }

  function setActive(key) {
    list.forEach((g) => { g.on = g.key === key; if (reduce) g.l = g.on ? 1 : 0; });
    if (reduce) draw(0);
  }

  // ------------------------------------------------------------------ drawing

  function spiral(x, y, R, col, r, parts, tilt) {
    const glow = ctx.createRadialGradient(x, y, 0, x, y, R);
    glow.addColorStop(0, "rgba(255,246,225,.95)"); glow.addColorStop(0.14, `rgba(${col},.5)`);
    glow.addColorStop(0.55, `rgba(${col},.1)`); glow.addColorStop(1, `rgba(${col},0)`);
    ctx.fillStyle = glow; ctx.beginPath(); ctx.arc(x, y, R, 0, 6.283); ctx.fill();
    const u = Math.max(0.45, Math.min(1.4, R / 110));
    for (const p of parts) {
      const ang = p.a + r * (1.7 - p.r);
      ctx.fillStyle = `rgba(${p.k < 0.68 ? col : "240,244,255"},${(0.25 + 0.65 * (1 - p.r)).toFixed(2)})`;
      ctx.beginPath(); ctx.arc(x + Math.cos(ang) * R * p.r, y + Math.sin(ang) * R * p.r * tilt, p.s * u, 0, 6.283); ctx.fill();
    }
  }

  function dotRing(x, y, rx, ry, col, phase, n, alpha, sizePx) {
    ctx.fillStyle = `rgba(${col},${alpha})`;
    for (let i = 0; i < n; i++) {
      const th = i / n * 6.283 + phase, px = Math.cos(th) * rx, py = Math.sin(th) * ry;
      ctx.beginPath(); ctx.arc(x + px * 0.989 + py * 0.149, y - px * 0.149 + py * 0.989, sizePx, 0, 6.283); ctx.fill();
    }
  }

  function draw(t) {
    if (!ctx || !nexus) return;
    const W = el.stage.clientWidth, H = el.stage.clientHeight, m = Math.min(W, H), cx = W / 2, cy = H * 0.55;
    const rx = Math.min(W * 0.37, m * 0.62), ry = Math.min(H * 0.33, m * 0.42), gold = nexus.rgb;
    ctx.clearRect(0, 0, W, H); ctx.globalCompositeOperation = "lighter";
    list.forEach((b, i) => {
      const ang = (i * 360 / list.length - 90) * Math.PI / 180 + turn;
      b.x = cx + Math.cos(ang) * rx * b.d; b.y = cy + Math.sin(ang) * ry * b.d;
      if (!reduce) b.l += ((b.on ? 1 : 0) - b.l) * 0.12;
    });
    const F = shown || { x: cx, y: cy }, fx = cx + (F.x - cx) * z, fy = cy + (F.y - cy) * z, S = 1 + z * 1.8, ov = 1 - z;
    const T = (x, y) => [cx + (x - fx) * S, cy + (y - fy) * S];

    el.gals.style.opacity = ov; el.gals.style.visibility = ov < 0.05 ? "hidden" : "visible";
    el.tag.style.opacity = ov;
    if (ov > 0.01) {
      ctx.globalAlpha = ov;
      const n0 = T(cx, cy), NR = m * 0.15 * S;
      dotRing(n0[0], n0[1], NR * 1.75, NR * 0.55, gold, turn * 9, 80, 0.75, 1.2);
      dotRing(n0[0], n0[1], NR * 2.35, NR * 0.82, gold, -turn * 6, 110, 0.35, 1);
      spiral(n0[0], n0[1], NR, gold, rot, nexusParts, 0.82);
      nexus.el.style.left = n0[0] + "px"; nexus.el.style.top = (n0[1] + NR * 0.62) + "px";
      for (const b of list) {
        const p = T(b.x, b.y), R = m * 0.058 * S * (1 + 0.7 * b.l);
        if (b.l > 0.02) {
          ctx.fillStyle = `rgba(${b.rgb},${b.l.toFixed(2)})`;
          const off = (t / 700) % 1;
          for (let j = 0; j < 16; j++) {
            const q = (j + off) / 16;
            ctx.beginPath(); ctx.arc(n0[0] + (p[0] - n0[0]) * q, n0[1] + (p[1] - n0[1]) * q, 1.6, 0, 6.283); ctx.fill();
          }
          ctx.globalAlpha = ov * b.l;
          spiral(p[0], p[1], R * 1.6, b.rgb, 0, [], 1);
          ctx.globalAlpha = ov;
        }
        spiral(p[0], p[1], R, b.rgb, rot * 1.6 + b.d * 9, b.parts, 0.8);
        b.el.style.left = p[0] + "px"; b.el.style.top = (p[1] - R * 0.8) + "px"; b.hit.style.height = (R * 1.6) + "px";
      }
    }
    el.inside.hidden = z < 0.02; el.inside.style.opacity = z;
    el.nodes.style.opacity = z; el.nodes.style.visibility = z < 0.05 ? "hidden" : "visible";
    if (z > 0.01 && shown) {
      ctx.globalAlpha = z;
      const ix = W / 2, iy = H * 0.68, M = Math.min(W * 0.47, H * 0.52), col = shown.rgb, rings = [0.42, 0.7, 1];
      const neb = ctx.createRadialGradient(ix, iy, 0, ix, iy, M * 1.25);
      neb.addColorStop(0, `rgba(${col},.34)`); neb.addColorStop(0.5, `rgba(${col},.12)`); neb.addColorStop(1, `rgba(${col},0)`);
      ctx.fillStyle = neb; ctx.beginPath(); ctx.arc(ix, iy, M * 1.25, 0, 6.283); ctx.fill();
      ctx.lineWidth = 1; ctx.strokeStyle = `rgba(${col},.4)`;
      for (const f of rings) { ctx.beginPath(); ctx.ellipse(ix, iy, M * f, M * f * 0.4, -0.15, 0, 6.283); ctx.stroke(); }
      spiral(ix, iy, M * 0.22, col, rot * 1.5, shown.parts, 0.5);
      shown.helpers.forEach((name, j) => {
        const f = rings[j % 3], th = (j % 3) * 2.1 + Math.floor(j / 3) * Math.PI + turn * (j % 3 === 1 ? -7 : 7);
        const px = Math.cos(th) * M * f, py = Math.sin(th) * M * f * 0.4;
        const x = ix + px * 0.989 + py * 0.149, y = iy - px * 0.149 + py * 0.989;
        spiral(x, y, 13, j % 2 ? "240,244,255" : col, rot * 3 + j, nodeParts, 0.8);
        el.nodeEls[j].style.left = x + "px"; el.nodeEls[j].style.top = (y + 14) + "px";
      });
    }
    ctx.globalAlpha = 1; ctx.globalCompositeOperation = "source-over";
  }

  function stars(t) {
    sx.clearRect(0, 0, el.stars.clientWidth, el.stars.clientHeight);
    for (const o of dots) {
      sx.globalAlpha = reduce ? 0.7 : 0.3 + 0.6 * Math.abs(Math.sin(o.p + t / 1500 * o.s));
      sx.fillStyle = o.c; sx.beginPath(); sx.arc(o.x, o.y, o.r, 0, 6.283); sx.fill();
    }
    sx.globalAlpha = 1;
  }

  function size() {
    const d = Math.min(window.devicePixelRatio || 1, 2);
    el.scene.width = el.stage.clientWidth * d; el.scene.height = el.stage.clientHeight * d; ctx.setTransform(d, 0, 0, d, 0, 0);
    el.stars.width = el.stars.clientWidth * d; el.stars.height = el.stars.clientHeight * d; sx.setTransform(d, 0, 0, d, 0, 0);
    const n = Math.round(el.stars.clientWidth * el.stars.clientHeight / 6500), tints = ["#E8ECFF", "#E8ECFF", "#E8ECFF", "#BFD4FF", "#FFE2B8"];
    dots = [];
    for (let i = 0; i < n; i++) {
      dots.push({ x: Math.random() * el.stars.clientWidth, y: Math.random() * el.stars.clientHeight,
                  r: Math.random() * 1.1 + 0.25, p: Math.random() * 6.28, s: 0.4 + Math.random() * 1.2, c: tints[i % 5] });
    }
    stars(0); draw(0);
  }

  function tick(t) {
    requestAnimationFrame(tick);
    if (t - lastStars > 120) { lastStars = t; stars(t); }
    const dt = t - last;
    if (dt < FRAME_MS || !visible) { if (!visible) last = t; return; }
    last = t;
    const step = Math.min(dt, 80);
    speed += ((SPEED[state] || 1) - speed) * 0.06;
    rot += step * 0.00028 * speed; turn += step * 0.000035 * speed;
    z += (zGoal - z) * 0.16; if (Math.abs(zGoal - z) < 0.003) z = zGoal;
    draw(t);
  }

  return { init, setBeings, setFocus, setState, setActive };
})();
