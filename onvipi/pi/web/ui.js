/* OnviPi – helpers shared by the setup wizard (setup.js) and the settings interface (app.js).
   Plain browser JavaScript on purpose: no build step, works offline on the setup hotspot. */
"use strict";

const ICONS = {
  home: '<path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10v9.5h13V10"/>',
  camera: '<path d="M4 8h3l1.8-2.5h6.4L17 8h3v11H4z"/><circle cx="12" cy="13.2" r="3.3"/>',
  wifi: '<path d="M2.5 9.5a14 14 0 0 1 19 0"/><path d="M6 13a9 9 0 0 1 12 0"/><path d="M9.3 16.3a4.2 4.2 0 0 1 5.4 0"/><circle cx="12" cy="19.3" r=".6"/>',
  cable: '<rect x="4" y="13" width="16" height="7" rx="1.5"/><path d="M8 13V9h8v4"/><path d="M12 9V4"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3A4 4 0 0 0 11 18.700l1-1"/>',
  sliders: '<path d="M4 7h9"/><path d="M17 7h3"/><circle cx="15" cy="7" r="2"/><path d="M4 17h3"/><path d="M11 17h9"/><circle cx="9" cy="17" r="2"/>',
  terminal: '<rect x="3" y="4.5" width="18" height="15" rx="2"/><path d="m7.500 10 3 2.200-3 2.300"/><path d="M13 14.500h3.500"/>',
  logout: '<path d="M14.500 4.500H19v15h-4.500"/><path d="m9 8-4 4 4 4"/><path d="M5 12h10"/>',
  check: '<path d="m5 12.500 4.500 4.500L19 7.500"/>',
  alert: '<path d="M12 4 2.800 19.500h18.400z"/><path d="M12 10v4.500"/><circle cx="12" cy="17" r=".5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.500"/><circle cx="12" cy="7.800" r=".5"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.600-5.900"/><path d="M20 4.500V9h-4.500"/>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  eye: '<path d="M2.500 12S6 5.500 12 5.500 21.500 12 21.500 12 18 18.500 12 18.500 2.500 12 2.500 12z"/><circle cx="12" cy="12" r="2.800"/>',
  copy: '<rect x="8.500" y="8.500" width="11" height="11" rx="2"/><path d="M5 15.500V6.500a2 2 0 0 1 2-2h8.500"/>',
  download: '<path d="M12 4v11"/><path d="m7.500 11 4.500 4.500 4.500-4.500"/><path d="M5 19.500h14"/>',
  power: '<path d="M12 3.500v8"/><path d="M6.800 6.800a7.500 7.500 0 1 0 10.400 0"/>',
  film: '<rect x="3.500" y="5" width="17" height="14" rx="2"/><path d="m10 9.500 4.500 2.500-4.500 2.500z"/>',
  more: '<circle cx="5.500" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="18.500" cy="12" r="1"/>',
  trash: '<path d="M4.500 7h15"/><path d="M9.500 7V4.500h5V7"/><path d="M6.500 7l.800 12.500h9.400L17.500 7"/>',
  close: '<path d="m6 6 12 12"/><path d="M18 6 6 18"/>',
  upload: '<path d="M12 15.500V4.500"/><path d="m7.500 9 4.500-4.500L16.500 9"/><path d="M5 19.500h14"/>',
  shield: '<path d="M12 3.500 5 6v5.500c0 4.300 2.900 7.400 7 9 4.100-1.600 7-4.700 7-9V6z"/>',
};

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = ICONS[name] || "";
  return svg;
}

/* h("div", {class: "card", onclick: fn}, child, "text", [more, children]) */
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = value;
    else if (key in el && key !== "list" && key !== "form") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  const add = (child) => {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) child.forEach(add);
    else el.append(child.nodeType ? child : document.createTextNode(String(child)));
  };
  children.forEach(add);
  return el;
}

function clear(el, ...children) {
  el.replaceChildren();
  children.flat(Infinity).forEach((c) => c && el.append(c));
  return el;
}

class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

/* GET without body, POST (JSON) with body. Throws ApiError with a readable message. */
async function api(path, body) {
  let res;
  try {
    res = await fetch("/api/" + path, body === undefined ? { cache: "no-store" } : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
  } catch (e) {
    throw new ApiError("The device is not reachable. Check your connection and try again.", 0);
  }
  let data = {};
  try { data = await res.json(); } catch (e) { /* empty or non-JSON answer */ }
  if (!res.ok) {
    if (res.status === 401 && typeof window.onUnauthorized === "function") window.onUnauthorized();
    throw new ApiError(data.error || `Request failed (${res.status}).`, res.status);
  }
  return data;
}

function toast(message, type) {
  let box = document.getElementById("toasts");
  if (!box) { box = h("div", { id: "toasts", "aria-live": "polite" }); document.body.append(box); }
  const el = h("div", { class: "toast" + (type ? " " + type : "") }, message);
  box.append(el);
  setTimeout(() => el.remove(), type === "error" ? 6000 : 3200);
}

function banner(type, ...content) {
  const icons = { ok: "check", warn: "alert", error: "alert" };
  return h("div", { class: "banner " + (type || ""), role: type === "error" ? "alert" : null },
    icon(icons[type] || "info"), h("div", {}, ...content));
}

/* Runs an async action while the button shows a spinner; reports errors as a toast. */
async function busy(button, action) {
  const old = [...button.childNodes];
  button.disabled = true;
  button.prepend(h("span", { class: "spinner" }));
  try {
    return await action();
  } catch (e) {
    toast(e.message || String(e), "error");
    return undefined;
  } finally {
    button.disabled = false;
    button.replaceChildren(...old);
  }
}

function confirmDialog({ title, text, confirm, danger }) {
  return new Promise((resolve) => {
    const close = (result) => { back.remove(); resolve(result); };
    const back = h("div", { class: "modal-back", onclick: (e) => e.target === back && close(false) },
      h("div", { class: "modal", role: "dialog", "aria-modal": "true" },
        h("h2", {}, title), h("p", { class: "muted" }, text),
        h("div", { class: "actions row end" },
          h("button", { class: "btn secondary", onclick: () => close(false) }, "Cancel"),
          h("button", { class: "btn" + (danger ? " danger" : ""), onclick: () => close(true) },
            confirm || "OK"))));
    document.body.append(back);
    back.querySelector(".btn:last-child").focus();
  });
}

/* Asks for one value (e.g. the password before a factory reset). Resolves to the text or null. */
function promptDialog({ title, text, confirm, danger, password }) {
  return new Promise((resolve) => {
    const close = (result) => { back.remove(); resolve(result); };
    const input = password ? passwordInput({ autocomplete: "current-password" })
      : h("input", { type: "text" });
    const el = password ? input.input : input;
    el.addEventListener("keydown", (e) => { if (e.key === "Enter") close(el.value); });
    const back = h("div", { class: "modal-back", onclick: (e) => e.target === back && close(null) },
      h("div", { class: "modal", role: "dialog", "aria-modal": "true" },
        h("h2", {}, title), h("p", { class: "muted" }, text), input,
        h("div", { class: "actions row end" },
          h("button", { class: "btn secondary", onclick: () => close(null) }, "Cancel"),
          h("button", { class: "btn" + (danger ? " danger" : ""), onclick: () => close(el.value) },
            confirm || "OK"))));
    document.body.append(back);
    el.focus();
  });
}

/* Rectangles on the camera picture (motion areas, privacy masks).
   Draw a new one by dragging on the picture, move one by dragging it, resize it at its
   corners. Values are relative to the picture (0-1), so they survive a change of resolution. */
function zoneEditor(camId, zones, { kind, live }) {
  const MIN = 0.03, SNAP = 0.02, MAX = 8;
  const list = (zones || []).map((z) => ({ ...z }));
  const layer = h("div", { class: "zone-layer" });
  const img = h("img", { alt: "", draggable: false });
  const box = h("div", { class: "zone-box " + kind }, img, layer);
  const count = h("span", { class: "muted small" });
  const pct = (v) => (v * 100).toFixed(2) + "%";
  const place = (el, z) => Object.assign(el.style,
    { left: pct(z.x), top: pct(z.y), width: pct(z.w), height: pct(z.h) });
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  /* what is nearly at the edge belongs to the edge – no thin strip stays visible */
  const snap = (z) => {
    let x0 = z.x, y0 = z.y, x1 = z.x + z.w, y1 = z.y + z.h;
    if (x0 < SNAP) x0 = 0;
    if (y0 < SNAP) y0 = 0;
    if (x1 > 1 - SNAP) x1 = 1;
    if (y1 > 1 - SNAP) y1 = 1;
    return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
  };
  const render = () => {
    clear(layer, list.map((z, i) => {
      const el = h("div", { class: "zone", "data-i": i },
        h("button", { type: "button", class: "zone-del", "aria-label": "Remove area",
          onpointerdown: (e) => e.stopPropagation(),
          onclick: (e) => { e.stopPropagation(); list.splice(i, 1); render(); } }, icon("close")),
        ["nw", "ne", "sw", "se"].map((c) => h("i", { class: "zone-handle " + c, "data-h": c })));
      place(el, z);
      return el;
    }));
    count.textContent = list.length ? `${list.length} of ${MAX} areas · drag to move, pull a corner to resize`
      : "Drag on the picture to draw an area";
  };
  const point = (e) => {
    const r = layer.getBoundingClientRect();
    return { x: clamp((e.clientX - r.left) / r.width, 0, 1), y: clamp((e.clientY - r.top) / r.height, 0, 1) };
  };

  let drag = null;   // {mode: "new" | "move" | corner, el, i, from: point, orig: zone}
  layer.addEventListener("pointerdown", (e) => {
    const zoneEl = e.target.closest(".zone");
    const from = point(e);
    if (zoneEl) {
      const i = Number(zoneEl.dataset.i);
      drag = { mode: e.target.dataset.h || "move", el: zoneEl, i, from, orig: { ...list[i] } };
    } else {
      if (list.length >= MAX) return toast(`At most ${MAX} areas – remove one first.`, "error");
      const el = h("div", { class: "zone draft" });
      layer.append(el);
      drag = { mode: "new", el, i: -1, from, orig: { x: from.x, y: from.y, w: 0, h: 0 } };
    }
    layer.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  const shape = (p) => {
    const o = drag.orig;
    if (drag.mode === "move") {
      return { x: clamp(o.x + p.x - drag.from.x, 0, 1 - o.w), y: clamp(o.y + p.y - drag.from.y, 0, 1 - o.h), w: o.w, h: o.h };
    }
    // new area or corner: the corner opposite to the one being pulled stays where it is
    let ax = drag.from.x, ay = drag.from.y;
    if (drag.mode !== "new") {
      ax = drag.mode.includes("w") ? o.x + o.w : o.x;
      ay = drag.mode.includes("n") ? o.y + o.h : o.y;
    }
    return { x: Math.min(ax, p.x), y: Math.min(ay, p.y), w: Math.abs(p.x - ax), h: Math.abs(p.y - ay) };
  };
  layer.addEventListener("pointermove", (e) => { if (drag) place(drag.el, shape(point(e))); });
  const finish = (e) => {
    if (!drag) return;
    const z = snap(shape(point(e)));
    const d = drag;
    drag = null;
    if (z.w >= MIN && z.h >= MIN) {
      if (d.mode === "new") list.push(z); else list[d.i] = z;
    }   // too small: a new one is dropped, an existing one keeps its old shape
    render();
  };
  layer.addEventListener("pointerup", finish);
  layer.addEventListener("pointercancel", () => { drag = null; render(); });
  img.onload = () => { box.style.aspectRatio = `${img.naturalWidth} / ${img.naturalHeight}`; };
  img.onerror = () => { img.remove(); };
  if (live) img.src = `/api/snapshot?cam=${encodeURIComponent(camId)}&t=${Date.now()}`;
  render();
  const el = h("div", {}, box,
    h("div", { class: "row", style: "margin-top:8px" }, count, h("span", { class: "spacer" }),
      h("button", { type: "button", class: "btn ghost small", onclick: () => { list.length = 0; render(); } },
        "Remove all")));
  return { el, value: () => list.map((z) => ({ x: z.x, y: z.y, w: z.w, h: z.h })) };
}

function field(label, control, hint) {
  const id = control.id || (control.id = "f" + Math.random().toString(36).slice(2, 9));
  return h("div", { class: "field" }, h("label", { for: id }, label), control,
    hint ? h("div", { class: "hint" }, hint) : null);
}

function select(options, value, attrs) {
  const el = h("select", attrs || {});
  for (const opt of options) {
    const [val, text] = Array.isArray(opt) ? opt : [opt, opt];
    el.append(h("option", { value: val, selected: String(val) === String(value) }, text));
  }
  return el;
}

function toggle(label, checked, onchange) {
  const input = h("input", { type: "checkbox", checked: !!checked, onchange });
  const el = h("label", { class: "switch" }, input, h("span", { class: "track" }), label);
  el.input = input;
  return el;
}

function passwordInput(attrs) {
  const input = h("input", Object.assign({ type: "password", autocapitalize: "off",
    autocorrect: "off", spellcheck: false }, attrs));
  const btn = h("button", { type: "button", class: "btn secondary", "aria-label": "Show password",
    onclick: () => { input.type = input.type === "password" ? "text" : "password"; } }, icon("eye"));
  const group = h("div", { class: "input-group" }, input, btn);
  group.input = input;
  return group;
}

function signalBars(percent) {
  const level = percent >= 75 ? 4 : percent >= 50 ? 3 : percent >= 25 ? 2 : 1;
  return h("span", { class: "bars", title: percent + " %" },
    [1, 2, 3, 4].map((i) => h("i", { class: i <= level ? "on" : "" })));
}

/* Live preview of one camera. The stream needs a moment after a change, so a failed
   picture is retried a few times before the placeholder stays. */
function snapshot(camId, { auto } = {}) {
  const img = h("img", { alt: "Camera preview" });
  const placeholder = h("div", { class: "placeholder" }, "Loading preview …");
  const box = h("div", { class: "shot" }, placeholder);
  let tries = 0, timer = null;
  const load = () => {
    clearTimeout(timer);
    const next = new Image();
    next.onload = () => {
      tries = 0;
      img.src = next.src;
      if (!img.isConnected) box.replaceChildren(img, refresh);
      if (auto && box.isConnected) timer = setTimeout(load, auto);
    };
    next.onerror = () => {
      if (!box.isConnected && tries > 0) return;
      if (++tries <= 6) { timer = setTimeout(load, 2500); return; }
      box.replaceChildren(h("div", { class: "placeholder" },
        "No picture yet. Check that the camera is plugged in."), refresh);
    };
    next.src = `/api/snapshot?cam=${encodeURIComponent(camId)}&t=${Date.now()}`;
  };
  const refresh = h("button", { type: "button", class: "refresh", onclick: () => { tries = 0; load(); } },
    icon("refresh"), "Refresh");
  box.reload = (delay) => {
    tries = 0;
    box.replaceChildren(h("div", { class: "placeholder" }, "Updating preview …"));
    timer = setTimeout(load, delay || 0);
  };
  load();
  return box;
}

/* Live picture: go2rtc's player (WebRTC, falling back to MSE/MP4) in a frame. If the
   player is not reachable, a picture that refreshes itself every few seconds is shown instead. */
function pictureShape(cam) {
  const [w, hgt] = String(cam.size || "16x9").split("x").map(Number);
  const turned = cam.rotate === 90 || cam.rotate === 270;
  return turned ? [hgt || 9, w || 16] : [w || 16, hgt || 9];
}

function livePreview(camId, shape) {
  const box = h("div", { class: "shot" }, h("div", { class: "placeholder" }, "Starting live view …"));
  if (shape) {
    box.style.aspectRatio = `${shape[0]} / ${shape[1]}`;
    if (shape[1] > shape[0]) {   // portrait: as tall as is comfortable, centred
      box.style.maxWidth = `calc(min(70vh, 640px) * ${shape[0]} / ${shape[1]})`;
      box.style.marginInline = "auto";
    }
  }
  const start = async () => {
    let ok = false;
    try { ok = (await fetch("/live/stream.html", { method: "HEAD", cache: "no-store" })).ok; } catch (e) { /* offline */ }
    if (!box.isConnected && box.dataset.waited) return;
    box.dataset.waited = "1";
    if (!ok) {   // no player (e.g. the stream service is restarting): self-refreshing picture
      const still = snapshot(camId, { auto: 3000 });
      still.classList.add("auto");
      box.replaceChildren(still);
      return;
    }
    box.replaceChildren(
      h("iframe", { src: `/live/stream.html?src=${encodeURIComponent(camId)}`, title: "Live view",
        allow: "autoplay; fullscreen", loading: "lazy" }),
      h("span", { class: "badge live-badge" }, h("i"), "Live"));
  };
  box.reload = (delay) => {
    box.replaceChildren(h("div", { class: "placeholder" }, "Updating live view …"));
    setTimeout(start, delay || 0);
  };
  setTimeout(start, 0);
  return box;
}

const ROTATIONS = [[0, "Normal"], [90, "Portrait (rotated right)"], [180, "Upside down"],
  [270, "Portrait (rotated left)"]];
const FORMAT_NAMES = { mjpeg: "MJPEG", h264: "H.264", yuyv422: "YUYV (uncompressed)" };

/* Form for one camera. Returns {el, value(), preview}. Used by the wizard and the settings.
   options: {motion: show motion detection, single: hide the on/off switch} */
function cameraEditor(cam, options = {}) {
  // the card title follows the name field while typing, so there is only ever one name on screen
  const title = h("h2", {}, cam.name);
  const name = h("input", { type: "text", maxLength: 48, value: cam.name, autocapitalize: "words",
    oninput: () => { title.textContent = name.value.trim() || cam.name; } });
  const enabled = toggle(cam.new ? "Use this camera" : "Active", cam.new ? true : cam.enabled);
  const rotate = select(ROTATIONS, cam.rotate);
  const overlay = toggle("Show date and time in the picture", cam.overlay);
  const motion = toggle("Motion detection", cam.motion);
  const sensLabel = h("span", {}, String(cam.sensitivity));
  const sens = h("input", { type: "range", min: 0, max: 100, value: cam.sensitivity,
    oninput: () => { sensLabel.textContent = sens.value; } });
  /* Live reading of the detector: the bar is the movement right now, the line the level at
     which it triggers. Makes it visible why (or why not) something counts as movement. */
  const level = h("span");
  const mark = h("i");
  const levelText = h("span", { class: "muted small" }, "Movement right now: waiting for the detector …");
  const meter = h("div", { class: "field" }, h("div", { class: "motion-meter" }, level, mark), levelText);
  const showMotion = (st) => {
    if (!st || !st.analysing) {
      level.style.width = "0";
      mark.style.display = "none";
      levelText.textContent = st ? "The detector is idle. It runs while recording is on or an app is listening for movement."
        : "Movement right now: no reading yet.";
      return;
    }
    const scale = Math.max(st.threshold * 2.5, 1);
    level.style.width = Math.min(100, 100 * st.score / scale) + "%";
    level.className = st.motion ? "on" : "";
    mark.style.display = "";
    mark.style.left = Math.min(100, 100 * st.threshold / scale) + "%";
    levelText.textContent = st.motion ? "Movement detected right now."
      : "No movement. The bar must pass the line to count (saved sensitivity).";
  };

  const audio = toggle("Sound from the camera’s microphone", cam.audio);
  const substream = toggle("Extra low-resolution stream", cam.substream);
  const live = cam.enabled && cam.present && !cam.new;
  const motionZones = options.motion ? zoneEditor(cam.id, cam.motion_zones, { kind: "motion", live }) : null;
  const privacy = options.motion ? zoneEditor(cam.id, cam.privacy, { kind: "privacy", live }) : null;

  const formats = cam.formats || {};
  const fmt = select(Object.keys(formats).map((f) => [f, FORMAT_NAMES[f] || f]), cam.format);
  const size = h("select", {});
  const fps = h("select", {});
  const fillFps = (keep) => {
    const list = (formats[fmt.value] || {})[size.value] || [];
    clear(fps, list.map((f) => h("option", { value: f, selected: keep && f === cam.fps }, f + " fps")));
  };
  const fillSize = (keep) => {
    const sizes = Object.keys(formats[fmt.value] || {});
    clear(size, sizes.map((s) => h("option", { value: s, selected: keep && s === cam.size }, s)));
    fillFps(keep);
  };
  fmt.onchange = () => fillSize(false);
  size.onchange = () => fillFps(false);
  fillSize(true);

  const preview = cam.enabled && cam.present && !cam.new ? livePreview(cam.id, pictureShape(cam)) : h("div", { class: "shot" },
    h("div", { class: "placeholder" }, !cam.present ? "This camera is not connected."
      : cam.new ? "Add this camera to see its picture."
        : "The camera is switched off."));

  const status = !cam.present ? h("span", { class: "badge error" }, "Not connected")
    : cam.new ? h("span", { class: "badge accent" }, "New") : null;

  const el = h("div", { class: "card" },
    h("div", { class: "card-head" },
      h("div", { class: "grow", style: "flex:1;min-width:0" },
        title, h("div", { class: "sub" }, cam.label || "USB camera")),
      status, options.single ? null : enabled),
    preview,
    h("div", { style: "height:14px" }),
    field("Name", name, "For example “Garden” or “Garage”. Connected apps show this name too."),
    options.motion ? h("div", {},
      h("div", { class: "field" }, motion),
      field(h("span", {}, "Sensitivity: ", sensLabel), sens,
        "Higher reacts to smaller movements. Lower it if it triggers all the time."),
      cam.new ? null : meter) : null,
    motionZones ? h("details", { class: "more" },
      h("summary", {}, "Motion areas", cam.motion_zones.length ? ` (${cam.motion_zones.length})` : ""),
      h("div", { class: "more-body" },
        h("p", { class: "muted small" }, "Drag on the picture to mark where movement should count – ",
          "for example the path, but not the street or a tree. Without areas the whole picture counts."),
        motionZones.el)) : null,
    privacy ? h("details", { class: "more" },
      h("summary", {}, "Privacy masks", cam.privacy.length ? ` (${cam.privacy.length})` : ""),
      h("div", { class: "more-body" },
        h("p", { class: "muted small" }, "Drag on the picture to black out areas, for example the neighbour’s garden. ",
          "They are hidden in the live view, in Home Assistant and in recordings."),
        privacy.el)) : null,
    h("details", { class: "more" },
      h("summary", {}, "Picture and sound"),
      h("div", { class: "more-body" },
        field("Orientation", rotate),
        h("div", { class: "field" }, overlay),
        h("div", { class: "field" }, cam.mic ? audio
          : h("span", { class: "muted small" }, "Sound: this camera has no microphone that was recognised."),
        cam.mic ? h("div", { class: "hint" }, "Off by default. Sound is heard in the live view and stored in recordings.") : null),
        h("div", { class: "field" }, substream,
          h("div", { class: "hint" }, "A second, small stream for slow connections. In Home Assistant it appears as an additional camera entity that you can enable.")),
        cam.present ? h("div", { class: "fields three" },
          field("Format", fmt), field("Resolution", size), field("Frame rate", fps)) : null,
        cam.present ? h("div", { class: "hint muted small" },
          "Higher resolution and frame rate need more computing power. 1280x720 is a good choice.")
          : null,
        options.onApply ? h("div", { class: "actions" },
          h("button", { type: "button", class: "btn secondary small",
            onclick: (e) => options.onApply(e.currentTarget) }, "Apply and update preview")) : null)));

  return {
    el, preview, showMotion,
    value() {
      const v = { id: cam.id, device: cam.device, name: name.value.trim() || cam.name,
        enabled: options.single ? true : enabled.input.checked,
        rotate: Number(rotate.value), overlay: overlay.input.checked };
      if (options.motion) {
        v.motion = motion.input.checked; v.sensitivity = Number(sens.value);
        v.motion_zones = motionZones.value(); v.privacy = privacy.value();
      }
      v.substream = substream.input.checked;
      if (cam.mic) v.audio = audio.input.checked;
      if (cam.present && fmt.value) { v.format = fmt.value; v.size = size.value; v.fps = Number(fps.value); }
      return v;
    },
  };
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "–";
  const d = Math.floor(seconds / 86400), hrs = Math.floor((seconds % 86400) / 3600),
    min = Math.floor((seconds % 3600) / 60);
  if (d) return `${d} d ${hrs} h`;
  if (hrs) return `${hrs} h ${min} min`;
  return `${Math.max(1, min)} min`;
}

function formatBytes(bytes) {
  if (!bytes && bytes !== 0) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = bytes;
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
  return (v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)) + " " + units[i];
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
