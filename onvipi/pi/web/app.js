/* OnviPi settings interface: login, navigation and one function per page.
   Everyday settings are visible by default; "Expert mode" (bottom of the menu) adds
   the technical ones (fixed IP, SSH, terminal, MQTT, stream addresses). */
"use strict";

const appEl = document.getElementById("app");
let state = null;           // /api/state
let pageTimer = null;       // refresh timer of the current page
let expert = false;
try { expert = localStorage.getItem("onvipi_expert") === "1"; } catch (e) { /* private mode */ }

const PAGES = [
  { id: "overview", title: "Overview", icon: "home", render: pageOverview },
  { id: "cameras", title: "Cameras", icon: "camera", render: pageCameras },
  { id: "recordings", title: "Recordings", icon: "film", render: pageRecordings },
  { id: "network", title: "Network", icon: "wifi", render: pageNetwork },
  { id: "connect", title: "Connect", icon: "link", render: pageConnect },
  { id: "updates", title: "Updates", icon: "download", render: pageUpdates },
  { id: "system", title: "System", icon: "sliders", render: pageSystem },
  { id: "terminal", title: "Terminal", icon: "terminal", render: pageTerminal, expert: true },
];

/* ------------------------------------------------------------------ login */
function showLogin(message) {
  clearTimeout(pageTimer);
  const pw = passwordInput({ autocomplete: "current-password", name: "password" });
  const error = h("div", {}, message ? banner("error", message) : null);
  const button = h("button", { class: "btn block", type: "submit" }, "Log in");
  const form = h("form", { onsubmit: (e) => {
    e.preventDefault();
    busy(button, async () => {
      try {
        await api("login", { password: pw.input.value });
      } catch (err) {
        clear(error, banner("error", err.message));
        pw.input.select();
        return;
      }
      start();
    });
  } },
  h("input", { type: "text", name: "username", autocomplete: "username", value: state.user,
    class: "hidden", readOnly: true }),
  field("Password", pw), button);
  clear(appEl, h("div", { class: "login" },
    h("div", { class: "card" },
      h("div", { class: "hero" }, icon("camera")),
      h("h1", { class: "center" }, state.name),
      h("p", { class: "center muted" }, "Log in to manage your camera."),
      error, form),
    h("p", { class: "center muted small" },
      "Forgot the password? Create an empty file named ", h("code", {}, "onvipi-reset"),
      " on the SD card’s boot partition and restart the device.")));
  pw.input.focus();
}

window.onUnauthorized = () => { if (state && state.authenticated) { state.authenticated = false; showLogin("Please log in again."); } };

async function logout() {
  try { await api("logout", {}); } catch (e) { /* ignore */ }
  state.authenticated = false;
  showLogin();
}

/* ------------------------------------------------------------------ shell */
function visiblePages() { return PAGES.filter((p) => !p.expert || expert); }

function currentPage() {
  const id = location.hash.replace(/^#\/?/, "");
  return visiblePages().find((p) => p.id === id) || PAGES[0];
}

function expertSwitch() {
  return toggle("Expert mode", expert, (e) => {
    expert = e.target.checked;
    try { localStorage.setItem("onvipi_expert", expert ? "1" : "0"); } catch (err) { /* ignore */ }
    renderShell();
  });
}

function renderShell() {
  clearTimeout(pageTimer);
  if (window.closeSheet) window.closeSheet();
  const page = currentPage();
  document.title = `${page.title} · ${state.name}`;
  const brand = () => h("div", { class: "brand" },
    h("span", { class: "logo" }, icon("camera")), h("span", {}, state.name));
  // Phone: the first three pages plus "More" (a sheet with the rest). Desktop: all in the sidebar.
  const PRIMARY = 3;
  const link = (p, cls) => h("a", { href: "#/" + p.id, class: (p.id === page.id ? "active " : "") + (cls || ""),
    "aria-current": p.id === page.id ? "page" : null }, icon(p.icon), h("span", {}, p.short || p.title));
  const pages = visiblePages();
  let sheet = null;
  const closeSheet = () => { if (sheet) { sheet.forEach((el) => el.remove()); sheet = null; } };
  const moreBtn = h("button", { type: "button", class: "more-btn" + (pages.indexOf(page) >= PRIMARY ? " active" : ""),
    onclick: () => {
      if (sheet) return closeSheet();
      sheet = [h("div", { class: "sheet-back", onclick: closeSheet }),
        h("div", { class: "sheet" }, pages.slice(PRIMARY).map((p) => h("a", { href: "#/" + p.id,
          class: p.id === page.id ? "active" : "", onclick: closeSheet }, icon(p.icon), p.title)),
        h("div", { class: "sheet-foot" }, expertSwitch(),
          h("button", { class: "btn ghost small", onclick: logout }, icon("logout"), "Log out")))];
      document.body.append(...sheet);
    } }, icon("more"), h("span", {}, "More"));
  window.closeSheet = closeSheet;
  const nav = h("nav", { class: "nav", "aria-label": "Main" }, brand(),
    pages.map((p, i) => link(p, i >= PRIMARY ? "extra" : "")), moreBtn,
    h("div", { class: "nav-foot" }, expertSwitch(),
      h("button", { class: "btn ghost", style: "margin-top:8px", onclick: logout }, icon("logout"), "Log out")));
  const content = h("div", { class: "content" },
    h("div", { class: "center", style: "padding:48px 0" }, h("div", { class: "spinner big" })));
  clear(appEl, h("div", { class: "shell" }, nav,
    h("div", { class: "main" },
      h("header", { class: "topbar" },
        h("span", { class: "logo" }, icon(page.icon)),
        h("div", { class: "topbar-title" }, h("b", {}, page.title), h("span", {}, state.name)),
        h("button", { class: "btn ghost small", onclick: logout, "aria-label": "Log out" }, icon("logout"))),
      content)));
  const render = async () => {
    try {
      const nodes = await page.render(render);
      if (currentPage() !== page || !content.isConnected) return;
      content.classList.add("ready");
      clear(content, nodes);
    } catch (e) {
      if (e.status === 401) return;
      clear(content, banner("error", e.message),
        h("button", { class: "btn secondary", onclick: render }, "Try again"));
    }
  };
  render();
}

function pageHead(title, text) {
  return h("div", { class: "page-head" }, h("h1", {}, title), text ? h("p", {}, text) : null);
}

/* A changed IP address must be confirmed, otherwise the device restores the old one.
   Being able to reach the device after the change is the confirmation. */
async function confirmIpChange(info) {
  if (!info || !info.applied) return;
  try {
    const res = await api("network/ip/confirm", {});
    if (res.confirmed) toast("New network settings confirmed.");
  } catch (e) { /* retried with the next status update */ }
}

/* ------------------------------------------------------------------ overview */
function lastMotion(c) {
  const st = c.state;
  if (!c.motion) return " · motion detection off";
  if (!st || !st.analysing) return "";
  if (st.motion) return " · movement right now";
  if (!st.last) return " · no movement so far";
  return " · last movement " + new Date(st.last[1] * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function tile(name, value, percent, level) {
  return h("div", { class: "tile" }, h("div", { class: "value" }, value), h("div", { class: "name" }, name),
    percent === undefined || percent === null ? null
      : h("div", { class: "meter " + (level || "") }, h("span", { style: `width:${Math.min(100, percent)}%` })));
}

function ifaceLine(i) {
  const connected = i.state === "connected";
  const detail = i.state === "hotspot" ? "Setup hotspot"
    : connected ? [i.ssid, i.ip].filter(Boolean).join(" · ") : "Not connected";
  return h("li", {}, icon(i.type === "wifi" ? "wifi" : "cable"),
    h("div", { class: "grow" }, h("div", { class: "title" }, i.label), h("div", { class: "muted small" }, detail)),
    connected && i.signal !== null ? signalBars(i.signal) : null,
    h("span", { class: "badge " + (connected ? "ok" : "") }, connected ? "Connected" : i.state === "hotspot" ? "Hotspot" : "Off"));
}

async function pageOverview() {
  const s = await api("status");
  confirmIpChange(s.ip_change);
  const active = s.cameras.filter((c) => c.enabled);
  const tiles = h("div", { class: "tiles" });
  const fill = (st) => {
    const lvl = (v, warn, bad) => (v >= bad ? "error" : v >= warn ? "warn" : "");
    clear(tiles,
      tile("Processor", st.cpu_percent === null ? "–" : st.cpu_percent + " %", st.cpu_percent, lvl(st.cpu_percent, 75, 92)),
      tile("Temperature", st.cpu_temp === null ? "–" : st.cpu_temp + " °C", st.cpu_temp, lvl(st.cpu_temp, 70, 80)),
      tile("Memory", st.mem_percent === null ? "–" : st.mem_percent + " %", st.mem_percent, lvl(st.mem_percent, 80, 93)),
      tile("Running for", formatDuration(st.uptime)));
  };
  fill(s.stats);
  const poll = async () => {
    if (!tiles.isConnected) return;
    try {
      const next = await api("status");
      fill(next.stats);
      confirmIpChange(next.ip_change);
    } catch (e) { /* shown on the next manual action */ }
    pageTimer = setTimeout(poll, 5000);
  };
  pageTimer = setTimeout(poll, 5000);

  return [
    pageHead(s.name, "Everything at a glance."),
    s.hotspot && state.hotspot ? banner("warn", h("b", {}, "The camera has no network connection."),
      " You are connected through its setup hotspot. Open ", h("a", { href: "#/network" }, "Network"),
      " to connect it to your Wi-Fi.") : null,
    s.warnings.map((w) => banner("warn", w)),
    s.update.available && !s.update.auto ? banner("info", h("b", {}, `Version ${s.update.latest} is available. `),
      h("a", { href: "#/updates" }, "Open Updates")) : null,
    active.length ? h("div", { class: "cams" + (active.length > 1 ? " multi" : "") },
      active.map((c) => h("div", { class: "card" },
        h("div", { class: "card-head" },
          h("div", { style: "flex:1;min-width:0" }, h("h2", {}, c.name),
            h("div", { class: "sub" }, `${c.size} · ${c.fps} fps`, lastMotion(c))),
          s.recording.active.includes(c.id) ? h("span", { class: "badge accent" }, "Recording") : null,
          h("span", { class: "badge " + (c.present ? "ok" : "error") }, c.present ? "Live" : "Not connected")),
        c.present ? livePreview(c.id, pictureShape(c)) : h("div", { class: "shot" },
          h("div", { class: "placeholder" }, "Plug the camera in again.")))))
      : h("div", { class: "card center" }, h("p", { class: "muted" }, "No camera is active."),
        h("a", { class: "btn", href: "#/cameras" }, "Set up a camera")),
    tiles,
    h("div", { class: "grid two" },
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Network")),
        h("ul", { class: "list" }, s.interfaces.map(ifaceLine))),
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "This device")),
        h("dl", { class: "kv" },
          h("dt", {}, "Address"), h("dd", {}, `http://${s.hostname}.local`),
          h("dt", {}, "IP address"), h("dd", {}, s.ip || "–"),
          h("dt", {}, "Version"), h("dd", {}, s.version)))),
  ];
}

/* ------------------------------------------------------------------ cameras */
async function pageCameras(rerender) {
  const data = await api("cameras");
  const activeCount = data.cameras.filter((c) => c.enabled).length;
  const editors = [];
  const pollMotion = async () => {   // live reading for the meters under the sensitivity sliders
    if (!editors.length || !editors[0].el.isConnected) return;
    try {
      const st = await api("motion");
      editors.forEach((e) => e.showMotion(st[e.id]));
    } catch (e) { /* next round */ }
    pageTimer = setTimeout(pollMotion, 1500);
  };
  pageTimer = setTimeout(pollMotion, 800);
  const cards = data.cameras.map((cam) => {
    const ed = cameraEditor(cam, { motion: true });
    ed.id = cam.id;
    if (!cam.new) editors.push(ed);
    const save = h("button", { class: "btn", onclick: () => busy(save, async () => {
      await api("cameras", { cameras: [ed.value()] });
      toast("Saved. The picture restarts – this takes a few seconds.");
      await sleep(1500);
      rerender();
    }) }, cam.new ? "Add camera" : "Save");
    const remove = !cam.present && !cam.new ? h("button", { class: "btn secondary", onclick: async () => {
      if (!await confirmDialog({ title: `Remove “${cam.name}”?`, danger: true, confirm: "Remove",
        text: "The camera disappears from this list. In connected apps such as Home Assistant you have to delete the device yourself." })) return;
      busy(remove, async () => { await api("cameras/remove", { id: cam.id }); rerender(); });
    } }, "Remove") : null;
    if (expert && !cam.new) {
      ed.el.append(h("details", { class: "more" }, h("summary", {}, "Addresses"),
        h("div", { class: "more-body" }, h("dl", { class: "kv" },
          h("dt", {}, "ONVIF"), h("dd", {}, h("code", {}, `${data.ip || "<ip>"}:${cam.port}`)),
          h("dt", {}, "RTSP"), h("dd", {}, h("code", {}, `rtsp://${data.ip || "<ip>"}:8554/${cam.id}`)),
          h("dt", {}, "Device ID"), h("dd", {}, h("code", {}, cam.mac || "assigned when online")),
          h("dt", {}, "Source"), h("dd", {}, h("code", {}, cam.device))))));
    }
    ed.el.append(h("div", { class: "actions" }, save, remove));
    return ed.el;
  });
  return [
    pageHead("Cameras", "Picture, movement and privacy for every camera. Connected apps see each active camera as its own device."),
    activeCount >= 2 ? banner("info", `${activeCount} cameras are active. Each one needs computing power – `,
      "if the picture stutters, lower the resolution or frame rate under “Picture options”.") : null,
    cards.length ? h("div", { class: "cams" + (cards.length > 1 ? " multi" : " narrow") }, cards)
      : h("div", { class: "card center" }, h("div", { class: "hero" }, icon("camera")),
        h("h2", {}, "No camera found"),
        h("p", { class: "muted" }, "Plug a USB webcam into the Raspberry Pi and search again."),
        h("button", { class: "btn", onclick: rerender }, icon("refresh"), "Search again")),
  ];
}

/* ------------------------------------------------------------------ network */
function wifiConnectCard(n, rerender) {
  const sel = { ssid: "", hidden: false, secure: true, iface: n.wifi_iface };
  let networks = n.networks;
  const list = h("div", { class: "nets" });
  const manual = h("input", { type: "text", maxLength: 32, autocapitalize: "off", spellcheck: false });
  const manualBox = field("Network name (SSID)", manual);
  const psk = passwordInput({ autocomplete: "off", maxLength: 63 });
  const pskBox = field("Wi-Fi password", psk);
  const connect = h("button", { class: "btn", onclick: async () => {
    const ssid = sel.hidden ? manual.value.trim() : sel.ssid;
    const key = pskBox.classList.contains("hidden") ? "" : psk.input.value;
    if (!ssid) return toast("Please choose a Wi-Fi network.", "error");
    if (!await confirmDialog({ title: `Connect to “${ssid}”?`, confirm: "Connect",
      text: "The camera switches to this network. This page may be unreachable for a moment; "
        + "afterwards open it again at http://" + n.hostname + ".local. If the connection fails, "
        + "the camera goes back to the previous network." })) return;
    busy(connect, async () => {
      await api("network/wifi", { ssid, psk: key, hidden: sel.hidden, iface: sel.iface });
      for (let i = 0; i < 60; i++) {
        await sleep(2000);
        let st;
        try { st = await api("network/wifi-status"); } catch (e) { continue; }
        if (st.phase === "done") { toast(`Connected to “${ssid}”.`); return rerender(); }
        if (st.phase === "failed") throw new Error(st.error);
      }
      throw new Error("No answer from the camera. Check that you are on the same network.");
    });
  } }, "Connect");
  const actions = h("div", { class: "actions" }, connect);
  const sync = () => {
    manualBox.classList.toggle("hidden", !sel.hidden);
    pskBox.classList.toggle("hidden", !(sel.hidden || (sel.ssid && sel.secure)));
    actions.classList.toggle("hidden", !(sel.hidden || sel.ssid));
  };
  const renderList = () => {
    const rows = networks.map((net) => h("button", { type: "button",
      class: "net" + (!sel.hidden && sel.ssid === net.ssid ? " selected" : ""),
      onclick: () => { sel.ssid = net.ssid; sel.hidden = false; sel.secure = net.secure; renderList(); } },
    h("span", { class: "ssid" }, net.ssid), net.secure ? icon("lock") : null, signalBars(net.signal)));
    rows.push(h("button", { type: "button", class: "net" + (sel.hidden ? " selected" : ""),
      onclick: () => { sel.hidden = true; sel.secure = true; renderList(); manual.focus(); } },
    h("span", { class: "ssid muted" }, "Other network …")));
    clear(list, rows);
    sync();
  };
  const scan = h("button", { class: "btn secondary small", onclick: () => busy(scan, async () => {
    const res = await api("network/scan", { iface: sel.iface });
    networks = res.networks;
    if (!res.fresh) toast("The Wi-Fi adapter is busy with the setup hotspot – showing the last list.");
    renderList();
  }) }, icon("refresh"), "Search networks");
  const adapter = n.wifi_devices.length > 1 ? field("Wi-Fi adapter",
    select(n.wifi_devices.map((d) => [d.iface, d.label]), sel.iface, { onchange: (e) => { sel.iface = e.target.value; } }),
    "Choose which adapter connects to your home network. A USB adapter with an external antenna usually reaches further.") : null;
  renderList();
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("div", { style: "flex:1" }, h("h2", {}, "Connect to Wi-Fi"),
      h("div", { class: "sub" }, "Change the network or add another one.")), scan),
    adapter, list, h("div", { style: "height:14px" }), manualBox, pskBox, actions);
}

function ipCard(iface, n, rerender) {
  const body = h("div", {}, h("div", { class: "spinner" }));
  api("network/ip?connection=" + encodeURIComponent(iface.connection)).then((cfg) => {
    const method = select([["auto", "Automatic (DHCP) – recommended"], ["manual", "Fixed IP address"]], cfg.method);
    const address = h("input", { type: "text", inputMode: "decimal", value: cfg.address || iface.ip, placeholder: "192.168.1.50" });
    const prefix = h("input", { type: "number", min: 1, max: 30, value: cfg.prefix || iface.prefix || 24 });
    const gateway = h("input", { type: "text", inputMode: "decimal", value: cfg.gateway || iface.gateway, placeholder: "192.168.1.1" });
    const dns = h("input", { type: "text", value: (cfg.dns.length ? cfg.dns : iface.dns).join(", "), placeholder: "192.168.1.1" });
    const manualBox = h("div", {},
      h("div", { class: "fields two" }, field("IP address", address), field("Subnet prefix", prefix, "24 means 255.255.255.0")),
      h("div", { class: "fields two" }, field("Router (gateway)", gateway), field("DNS server", dns, "Usually the same as the router.")));
    const sync = () => manualBox.classList.toggle("hidden", method.value !== "manual");
    method.onchange = sync;
    sync();
    const apply = h("button", { class: "btn", onclick: async () => {
      if (!await confirmDialog({ title: "Change the network settings?", confirm: "Apply",
        text: `If the camera cannot be reached with the new settings, it restores the current ones after ${Math.round(n.rollback_seconds / 60)} minutes. `
          + "Nothing can lock you out permanently." })) return;
      busy(apply, async () => {
        const res = await api("network/ip", { connection: iface.connection, method: method.value,
          address: address.value, prefix: prefix.value, gateway: gateway.value, dns: dns.value });
        const target = res.address && location.hostname !== n.hostname + ".local" && res.address !== location.hostname
          ? `http://${res.address}/` : null;
        clear(body, banner("warn", h("b", {}, "Applying the new settings …"), h("br"),
          target ? ["Open ", h("a", { href: target }, target), " and log in to keep them. "]
            : "This page confirms them automatically as soon as the camera answers again. ",
          "Otherwise the previous settings come back by themselves."));
        for (let i = 0; i < 70; i++) {
          await sleep(3000);
          try {
            const r = await api("network/ip/confirm", {});
            if (r.confirmed) { toast("New network settings are active."); return rerender(); }
            const now = await api("network");
            if (!now.ip_change) { toast("The previous network settings were restored.", "error"); return rerender(); }
          } catch (e) { /* not reachable (yet) */ }
        }
      });
    } }, "Apply");
    clear(body, field("Address assignment", method), manualBox, h("div", { class: "actions" }, apply));
  }).catch((e) => clear(body, banner("error", e.message)));
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("div", {}, h("h2", {}, "IP address · " + iface.label),
      h("div", { class: "sub" }, "Connection “" + iface.connection + "”"))), body);
}

async function pageNetwork(rerender) {
  const n = await api("network");
  confirmIpChange(n.ip_change);
  const active = n.interfaces.filter((i) => i.state === "connected" && i.connection);
  const hasWifi = n.wifi_devices.length > 0;
  const nodes = [
    pageHead("Network", "How the camera is connected to your home network."),
    n.hotspot ? banner("warn", h("b", {}, "The setup hotspot is active"), " because the camera has no network. ",
      "Choose your Wi-Fi below", n.stays_connected ? "." : " – the hotspot switches off while it connects.") : null,
    h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Connections")),
      h("ul", { class: "list" }, n.interfaces.map((i) => {
        const li = ifaceLine(i);
        if (expert && i.mac) li.querySelector(".grow").append(h("div", { class: "muted small" }, `${i.iface} · ${i.mac}`));
        return li;
      })),
      active.some((i) => i.type === "ethernet") && !active.some((i) => i.type === "wifi")
        ? h("p", { class: "muted small", style: "margin-top:12px" }, "The camera uses the network cable. Wi-Fi is not needed.") : null),
    hasWifi ? wifiConnectCard(n, rerender) : null,
  ];
  if (n.saved.length) {
    nodes.push(h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Saved Wi-Fi networks")),
      h("ul", { class: "list" }, n.saved.map((name) => {
        const inUse = active.some((i) => i.connection === name);
        const btn = h("button", { class: "btn secondary small", disabled: inUse, onclick: async () => {
          if (!await confirmDialog({ title: `Forget “${name.replace(/^wifi-/, "")}”?`, confirm: "Forget", danger: true,
            text: "The camera will no longer connect to this network automatically." })) return;
          busy(btn, async () => { await api("network/forget", { name }); rerender(); });
        } }, "Forget");
        return h("li", {}, icon("wifi"), h("div", { class: "grow" }, h("div", { class: "title" }, name.replace(/^wifi-/, ""))),
          inUse ? h("span", { class: "badge ok" }, "In use") : btn);
      }))));
  }
  if (expert) {
    active.forEach((i) => nodes.push(ipCard(i, n, rerender)));
    const host = h("input", { type: "text", value: n.hostname, maxLength: 63, autocapitalize: "off", spellcheck: false });
    const country = select(n.countries.map((c) => [c[0], `${c[1]} (${c[0]})`]), n.country);
    const save = h("button", { class: "btn", onclick: () => busy(save, async () => {
      const res = await api("system/settings", { hostname: host.value, country: country.value });
      toast("Saved.");
      if (res.hostname !== n.hostname) toast(`The new address is http://${res.hostname}.local`);
      rerender();
    }) }, "Save");
    nodes.push(h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Name on the network")),
      h("div", { class: "fields two" },
        field("Hostname", host, "The settings page is reachable at http://<hostname>.local"),
        field("Wi-Fi country", country, "Sets the Wi-Fi channels allowed in your country.")),
      h("div", { class: "actions" }, save)));
  } else {
    nodes.push(h("p", { class: "muted small center" }, "Fixed IP address, hostname and more: switch on Expert mode."));
  }
  return nodes;
}

/* ------------------------------------------------------------------ connect */
function copyField(text) {
  const btn = h("button", { type: "button", class: "btn secondary small", "aria-label": "Copy", onclick: () => {
    const done = () => toast("Copied.");
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text).then(done);
    const area = h("textarea", { value: text, style: "position:fixed;opacity:0" });
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
    done();
  } }, icon("copy"));
  return h("div", { class: "input-group" }, h("code", { style: "flex:1;padding:9px 10px" }, text), btn);
}

async function pageConnect(rerender) {
  const d = await api("homeassistant");
  const many = d.cameras.length > 1;
  const host = d.ip || d.hostname + ".local";
  const linked = d.cameras.some((c) => c.subscribed);
  const nodes = [
    pageHead("Connect", "Use the camera on its own, with Home Assistant, or with any app that speaks ONVIF or RTSP."),
    !d.cameras.length ? banner("warn", "No camera is active yet. Set one up under ", h("a", { href: "#/cameras" }, "Cameras"), " first.") : null,
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("div", { style: "flex:1" }, h("h2", {}, "On its own"),
        h("div", { class: "sub" }, "Nothing else is needed"))),
      h("ul", { class: "checklist" },
        h("li", {}, icon("check"), h("span", {}, h("b", {}, "Watch live"), " on the ", h("a", { href: "#/overview" }, "overview"), " – from any phone or computer in your home network.")),
        h("li", {}, icon("check"), h("span", {}, h("b", {}, "Record"), " when something moves or all the time, and look through the clips under ",
          h("a", { href: "#/recordings" }, "Recordings"), ".")),
        h("li", {}, icon("check"), h("span", {}, h("b", {}, "Like an app:"), " choose “Add to Home Screen” in your phone’s browser menu.")))),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("div", { style: "flex:1" }, h("h2", {}, "Home Assistant"),
        h("div", { class: "sub" }, "Live view and a motion sensor for each camera")),
        d.cameras.length ? h("span", { class: "badge " + (linked ? "ok" : "") }, linked ? "Connected" : "Not connected") : null),
      h("ol", { class: "steps" },
        h("li", {}, "In Home Assistant open ", h("b", {}, "Settings → Devices & services"), "."),
        h("li", {}, "Choose ", h("b", {}, "Add integration"), " and search for ", h("b", {}, "ONVIF"), "."),
        h("li", {}, "Select ", many ? "one of your cameras: " : "the camera ",
          d.cameras.map((c, i) => [i ? ", " : "", h("b", {}, `“${c.onvif_name}”`)]), "."),
        h("li", {}, "Enter the username ", h("code", {}, d.user), " and your OnviPi password."),
        many ? h("li", {}, "Repeat this for every camera.") : null),
      h("details", { class: "more" }, h("summary", {}, "The camera is not listed?"),
        h("div", { class: "more-body" },
          h("p", {}, "Home Assistant and the camera must be in the same network (no guest Wi-Fi). Otherwise choose ",
            h("b", {}, "Configure manually"), " and enter:"),
          h("dl", { class: "kv" },
            h("dt", {}, "Host"), h("dd", {}, h("code", {}, host)),
            d.cameras.map((c) => [h("dt", {}, many ? `Port for “${c.name}”` : "Port"), h("dd", {}, h("code", {}, String(c.port)))]),
            h("dt", {}, "Username"), h("dd", {}, h("code", {}, d.user))))),
      h("details", { class: "more" }, h("summary", {}, "Good to know"),
        h("div", { class: "more-body" }, h("ul", { class: "checklist" },
          h("li", {}, icon("check"), h("span", {}, "The motion sensor in Home Assistant and the recordings here react to the same movements.")),
          h("li", {}, icon("check"), h("span", {}, "The button ", h("b", {}, "Reboot"), " restarts the whole Raspberry Pi",
            many ? " – all cameras together." : ".")),
          h("li", {}, icon("check"), h("span", {}, h("b", {}, "Set system date and time"),
            " is only needed in a network without internet.")),
          h("li", {}, icon("check"), h("span", {}, "After changing the password here, choose ", h("b", {}, "Reconfigure"),
            " on the ONVIF device.")))))),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("div", {}, h("h2", {}, "Other apps and recorders"),
        h("div", { class: "sub" }, "Anything that speaks ONVIF or RTSP: recorders (NVR), NAS, VLC, …"))),
      h("p", { class: "muted small" }, "Username ", h("code", {}, d.user), " and your OnviPi password. Put them in where it says “user:password”."),
      d.cameras.map((c) => h("div", { class: "field" },
        h("span", { class: "label" }, many ? c.name : "Addresses"),
        h("dl", { class: "kv" },
          h("dt", {}, "ONVIF"), h("dd", {}, copyField(`${host}:${c.port}`)),
          h("dt", {}, "Video (RTSP)"), h("dd", {}, copyField(`rtsp://user:password@${host}:8554/${c.id}`)),
          c.substream ? h("dt", {}, "Small video") : null,
          c.substream ? h("dd", {}, copyField(`rtsp://user:password@${host}:8554/${c.id}_sub`)) : null,
          h("dt", {}, "Still picture"), h("dd", {}, copyField(`http://${host}:${c.port}/snapshot.jpg`)))))),
  ];
  if (expert) {
    const m = d.mqtt;
    const host = h("input", { type: "text", value: m.host, placeholder: "homeassistant.local", autocapitalize: "off", spellcheck: false });
    const port = h("input", { type: "number", value: m.port, min: 1, max: 65535 });
    const user = h("input", { type: "text", value: m.user, autocapitalize: "off", spellcheck: false, autocomplete: "off" });
    const pw = passwordInput({ autocomplete: "off", placeholder: m.has_password ? "(unchanged)" : "" });
    const interval = h("input", { type: "number", value: m.snapshot_interval, min: 0, max: 3600 });
    const save = h("button", { class: "btn", onclick: () => busy(save, async () => {
      await api("mqtt", { host: host.value, port: port.value, user: user.value, password: pw.input.value, snapshot_interval: interval.value });
      toast(host.value ? "MQTT settings saved." : "MQTT switched off.");
      rerender();
    }) }, "Save");
    nodes.push(h("div", { class: "card" },
      h("div", { class: "card-head" }, h("div", {}, h("h2", {}, "Diagnostic sensors via MQTT"), h("div", { class: "sub" }, "For Home Assistant · optional, needs an MQTT broker"))),
      h("p", { class: "muted small" }, "Adds IP address, processor temperature and Wi-Fi signal as sensors to the camera device in Home Assistant. ",
        "Leave everything empty to switch it off."),
      h("div", { class: "fields two" }, field("Broker address", host), field("Port", port)),
      h("div", { class: "fields two" }, field("Username", user), field("Password", pw)),
      field("Still picture every … seconds", interval, "0 switches the extra still-picture camera off."),
      h("div", { class: "actions" }, save)));
  }
  return nodes;
}

/* ------------------------------------------------------------------ recordings */
const REC_MODES = [["off", "Off"], ["motion", "When something moves"], ["continuous", "All the time"]];

function dayLabel(day) {
  const d = new Date(Number(day.slice(0, 4)), Number(day.slice(4, 6)) - 1, Number(day.slice(6, 8)));
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const diff = Math.round((today - d) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
}

function clipTime(file) {
  return new Date(file.start * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function storageName(st) {
  if (st.local) return "SD card (internal)";
  return `${st.label || st.model} · ${formatBytes(st.size)}`;
}

/* Movements that belong to a clip: from its start to the start of the next (newer) clip. */
function eventsOf(file, newer, events) {
  const end = newer ? newer.start : file.start + 300;
  return (events || []).filter((e) => e[0] < end && e[1] >= file.start);
}

function playClip(cam, files, index, onDelete, events) {
  let i = index;
  const why = h("p", { class: "muted small", style: "margin:10px 0 0" });
  const video = h("video", { controls: true, autoplay: true, playsInline: true });
  const title = h("h2", { style: "flex:1;margin:0" });
  const url = () => `/recordings/${cam.id}/${files[i].name}`;
  const download = h("a", { class: "btn secondary small" }, icon("download"), "Download");
  const show = () => {
    video.src = url();
    download.href = url() + "?download=1";
    title.textContent = `${cam.name} · ${new Date(files[i].start * 1000).toLocaleString()}`;
    const list = eventsOf(files[i], files[i - 1], events);
    const t = (sec) => new Date(sec * 1000).toLocaleTimeString();
    why.textContent = list.length
      ? "Movement: " + list.map((e) => `${t(e[0])} (${Math.max(1, e[1] - e[0])} s)`).join(", ")
      : (events && events.length ? "No movement in this clip – kept as the lead-in or follow-up of a movement, or recorded all the time."
        : "");
    older.disabled = i >= files.length - 1;
    newer.disabled = i <= 0;
  };
  const close = () => { video.pause(); video.removeAttribute("src"); back.remove(); };
  const older = h("button", { class: "btn secondary small", onclick: () => { i++; show(); } }, "Older");
  const newer = h("button", { class: "btn secondary small", onclick: () => { i--; show(); } }, "Newer");
  const del = h("button", { class: "btn secondary small", onclick: async () => {
    if (!await confirmDialog({ title: "Delete this recording?", confirm: "Delete", danger: true,
      text: "It is removed from the storage and cannot be restored." })) return;
    busy(del, async () => { await api("recordings/delete", { cam: cam.id, files: [files[i].name] }); close(); onDelete(); });
  } }, icon("trash"), "Delete");
  const back = h("div", { class: "modal-back", onclick: (e) => e.target === back && close() },
    h("div", { class: "modal player", role: "dialog", "aria-modal": "true" },
      h("div", { class: "row", style: "margin-bottom:10px" }, title,
        h("button", { class: "btn ghost small", onclick: close, "aria-label": "Close" }, icon("close"))),
      video, why,
      h("div", { class: "actions spread" }, h("div", { class: "row" }, older, newer),
        h("div", { class: "row" }, download, del))));
  document.body.append(back);
  show();
}

async function pageRecordings(rerender) {
  const d = await api("recordings");
  const st = d.status;
  const set = d.settings;
  const on = set.mode !== "off";
  const chosen = d.storages.find((s) => s.uuid === set.uuid);

  // ---- settings
  const mode = select(REC_MODES, set.mode);
  const storage = select(d.storages.map((s) => [s.uuid, storageName(s)]), set.uuid || (d.storages[0] || {}).uuid);
  const days = h("input", { type: "number", min: 1, max: 365, value: set.days });
  const maxGb = h("input", { type: "number", min: 1, step: "any", value: set.max_gb || "", placeholder: "no limit" });
  const sdNote = h("div");
  const syncNote = () => {
    const local = (d.storages.find((s) => s.uuid === storage.value) || {}).local;
    clear(sdNote, local && mode.value !== "off" ? banner("warn", h("b", {}, "Recording to the SD card wears it out."),
      " It works without extra hardware, but a USB stick or SSD is the better choice for recording all the time. ",
      "The camera always keeps 15 % (at least 2 GB) of the card free for the system.") : null);
  };
  mode.onchange = syncNote; storage.onchange = syncNote; syncNote();
  const save = h("button", { class: "btn", onclick: () => busy(save, async () => {
    await api("recordings/settings", { mode: mode.value, uuid: storage.value, days: days.value, max_gb: maxGb.value });
    toast(mode.value === "off" ? "Recording switched off." : "Recording settings saved.");
    await sleep(2500);
    rerender();
  }) }, "Save");
  const settingsCard = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("div", {}, h("h2", {}, "Recording settings"),
      h("div", { class: "sub" }, "Recordings are stored on the camera itself – no cloud."))),
    h("div", { class: "fields two" },
      field("Record", mode, "“When something moves” keeps a clip only if the camera saw movement – including the seconds before it."),
      field("Store on", storage, d.storages.length > 1 ? null
        : "No USB storage found – plug in a USB stick or SSD to use it instead of the SD card.")),
    sdNote,
    h("div", { class: "fields two" },
      field("Overwrite after … days", days, "Recordings older than this are deleted automatically."),
      field("Use at most … GB", maxGb, "Optional. Without a limit the free space is used.")),
    h("ul", { class: "checklist small muted" },
      h("li", {}, icon("shield"), h("span", {}, h("b", {}, "The storage cannot run full. "),
        "When space gets short, the oldest recordings are deleted first – even before the days are over.")),
      h("li", {}, icon("shield"), h("span", {}, "A reserve always stays free",
        " (USB: 5 %, at least 500 MB · SD card: 15 %, at least 2 GB). If other data fills the storage, recording pauses instead of overfilling it.")),
      h("li", {}, icon("shield"), h("span", {}, "Only the camera’s own recordings are ever deleted, never other files on the storage."))),
    h("div", { class: "actions" }, save));

  // ---- status line
  let statusBanner = null;
  if (on && st && st.error) statusBanner = banner("error", h("b", {}, "Recording is not running. "), st.error);
  else if (on && !st) statusBanner = banner("warn", "The recording service is starting …");
  else if (on && st) {
    const used = st.total ? Math.round(100 * (1 - st.free / st.total)) : 0;
    // how many days fit at the rate of the recordings so far
    let fit = null;
    const span = st.oldest ? Date.now() / 1000 - st.oldest : 0;
    if (st.used > 0 && span > 3 * 3600) {
      const perDay = st.used / (span / 86400);
      const room = Math.max(0, st.used + st.free - (st.reserve || 0));
      const days = Math.floor(Math.min(room, st.limit || room) / perDay);
      fit = days < st.days
        ? h("div", { class: "small" }, `At the current rate the space lasts for about ${Math.max(1, days)} day${days === 1 ? "" : "s"} – `,
          `less than the ${st.days} days you set. The oldest recordings are overwritten earlier.`)
        : h("div", { class: "small muted" }, `Enough space for the ${st.days} days you set.`);
    }
    statusBanner = banner(fit && fit.className === "small" ? "warn" : "ok",
      h("b", {}, set.mode === "motion" ? "Recording when something moves" : "Recording all the time"),
      ` · ${chosen ? storageName(chosen) : "storage"} · ${formatBytes(st.free)} free (${used} % used)`,
      st.count ? h("div", { class: "small muted" }, `${st.count} recordings · ${formatBytes(st.used)}`,
        st.limit ? ` of at most ${formatBytes(st.limit)}` : "",
        st.oldest ? ` · oldest from ${new Date(st.oldest * 1000).toLocaleDateString()}` : "") : null,
      fit,
      st.recording.length < d.cameras.length ? h("div", { class: "small muted" },
        `${st.recording.length} of ${d.cameras.length} cameras are recording right now.`) : null);
  }

  // ---- gallery
  const withClips = d.cameras.filter((c) => c.days.length);
  const view = { cam: (withClips[0] || d.cameras[0] || {}).id, day: null, picking: false, picked: new Set(), files: [], events: [], shown: 60 };
  const grid = h("div");
  const camSel = select(d.cameras.map((c) => [c.id, c.name]), view.cam,
    { onchange: (e) => { view.cam = e.target.value; view.day = null; fillDays(); load(); } });
  const daySel = h("select", { onchange: (e) => { view.day = e.target.value; load(); } });
  const pickBtn = h("button", { class: "btn secondary small", onclick: () => {
    view.picking = !view.picking; view.picked.clear(); draw(); } }, "Select");
  const delBtn = h("button", { class: "btn danger small", onclick: async () => {
    const n = view.picked.size;
    if (!await confirmDialog({ title: `Delete ${n} recording${n === 1 ? "" : "s"}?`, confirm: "Delete", danger: true,
      text: "They are removed from the storage and cannot be restored." })) return;
    busy(delBtn, async () => { await api("recordings/delete", { cam: view.cam, files: [...view.picked] }); rerender(); });
  } }, icon("trash"), "Delete");
  const dayBtn = h("button", { class: "btn secondary small", onclick: async () => {
    if (!await confirmDialog({ title: `Delete all recordings of ${dayLabel(view.day)}?`, confirm: "Delete day", danger: true,
      text: `${view.files.length} recordings of this camera are removed and cannot be restored.` })) return;
    busy(dayBtn, async () => { await api("recordings/delete", { cam: view.cam, day: view.day }); rerender(); });
  } }, "Delete whole day");
  const cam = () => d.cameras.find((c) => c.id === view.cam) || { days: [], name: "" };
  const fillDays = () => {
    const list = cam().days;
    if (!view.day || !list.some((x) => x.day === view.day)) view.day = list.length ? list[0].day : null;
    clear(daySel, list.map((x) => h("option", { value: x.day, selected: x.day === view.day },
      `${dayLabel(x.day)} · ${x.count} clip${x.count === 1 ? "" : "s"} · ${formatBytes(x.size)}`)));
  };
  const draw = () => {
    pickBtn.textContent = view.picking ? "Cancel" : "Select";
    delBtn.classList.toggle("hidden", !view.picking);
    delBtn.disabled = !view.picked.size;
    dayBtn.classList.toggle("hidden", view.picking || !view.files.length);
    if (!view.files.length) {
      return clear(grid, h("div", { class: "center muted", style: "padding:28px 0" },
        !on ? "Recording is switched off. Switch it on below."
          : set.mode === "motion" ? "No recordings yet. A clip appears here as soon as something has moved."
            : "No recordings yet."));
    }
    const newest = [...view.files].reverse();   // newest first
    const tiles = newest.slice(0, view.shown).map((f, i) => {
      const picked = view.picked.has(f.name);
      return h("button", { type: "button", class: "clip" + (picked ? " picked" : ""), onclick: () => {
        if (view.picking) { picked ? view.picked.delete(f.name) : view.picked.add(f.name); return draw(); }
        playClip(cam(), newest, i, rerender, view.events);
      } },
      h("img", { loading: "lazy", alt: "", src: `/recordings/${view.cam}/${f.name.replace(".mp4", ".jpg")}`,
        onerror: (e) => e.target.remove() }),
      view.picking ? h("input", { type: "checkbox", class: "pick", checked: picked, tabIndex: -1 }) : null,
      eventsOf(f, newest[i - 1], view.events).length ? h("span", { class: "badge live motion-badge" },
        `${eventsOf(f, newest[i - 1], view.events).length}× movement`) : null,
      h("span", { class: "meta" }, h("span", {}, clipTime(f)), h("span", {}, formatBytes(f.size))));
    });
    clear(grid, h("div", { class: "gallery" }, tiles),
      newest.length > view.shown ? h("div", { class: "center", style: "margin-top:14px" },
        h("button", { class: "btn secondary", onclick: () => { view.shown += 120; draw(); } },
          `Show more (${newest.length - view.shown} older)`)) : null);
  };
  const load = async () => {
    view.picked.clear(); view.shown = 60; view.files = [];
    if (view.cam && view.day) {
      clear(grid, h("div", { class: "center", style: "padding:28px 0" }, h("div", { class: "spinner big" })));
      try {
        const res = await api(`recordings?cam=${view.cam}&day=${view.day}`);
        view.files = res.files;
        view.events = res.events || [];
      } catch (e) { return clear(grid, banner("error", e.message)); }
    }
    draw();
  };
  fillDays();
  load();

  return [
    pageHead("Recordings", "Watch, download and delete what the camera has recorded."),
    statusBanner,
    h("div", { class: "card" },
      h("div", { class: "toolbar" },
        d.cameras.length > 1 ? field("Camera", camSel) : null,
        cam().days.length || d.cameras.length > 1 ? field("Day", daySel) : null,
        h("div", { class: "row" }, pickBtn, delBtn, dayBtn)),
      grid),
    settingsCard,
  ];
}

/* ------------------------------------------------------------------ update, backup, reset */
function when(epoch) {
  if (!epoch) return "never";
  const d = new Date(epoch * 1000);
  const days = Math.floor((new Date().setHours(0, 0, 0, 0) - new Date(d).setHours(0, 0, 0, 0)) / 86400000);
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  if (days === 0) return "today, " + time;
  if (days === 1) return "yesterday, " + time;
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) + ", " + time;
}

async function pageUpdates(rerender) {
  const u = await api("system/update");
  const st = u.state;
  const out = h("div");
  const running = ["downloading", "installing"].includes(u.status.phase)
    && Date.now() / 1000 - (u.status.updated || 0) < 1800;

  const follow = async () => {
    clear(out, banner("info", h("b", {}, "Updating …"), " Please keep this page open. It reloads when the update is done. ",
      "The picture is interrupted for a few minutes."));
    for (let i = 0; i < 240; i++) {
      await sleep(5000);
      let now;
      try { now = await api("system/update"); } catch (err) { continue; }   // services restart meanwhile
      if (now.status.phase === "failed") {
        return clear(out, banner("error", h("b", {}, "The update failed. "), now.status.error || "See the log under System."));
      }
      if (now.status.phase === "done") { toast("Update installed."); await sleep(3000); return location.reload(); }
    }
    clear(out, banner("error", "The update did not finish in time. Please check the log under System."));
  };
  const installButton = (latest) => h("button", { class: "btn", onclick: async (e) => {
    const btn = e.currentTarget;
    if (!await confirmDialog({ title: `Install version ${latest}?`, confirm: "Install",
      text: "The camera downloads the update and restarts its services. This takes a few minutes; "
        + "picture and recording are interrupted meanwhile. Your settings are kept." })) return;
    busy(btn, async () => { await api("system/update/install", {}); follow(); });
  } }, icon("download"), `Install version ${latest}`);
  const showAvailable = (info) => clear(out,
    banner("info", h("b", {}, `Version ${info.latest} is available.`), " ",
      info.url ? h("a", { href: info.url, target: "_blank", rel: "noopener" }, "What is new?") : null,
      u.auto ? h("div", { class: "small muted" }, "It is installed automatically tonight – or right now with the button below.") : null),
    h("div", { class: "actions" }, installButton(info.latest)));

  if (running) follow();
  else if (st.available && st.latest) showAvailable(st);
  else if (st.last_check && !st.check_error) clear(out, banner("ok", "This camera is up to date."));
  else if (st.check_error) {
    clear(out, banner("warn", "The last check did not reach the update server. Is the camera connected to the internet?"));
  }

  const check = h("button", { class: "btn secondary", onclick: () => busy(check, async () => {
    const info = await api("system/update/check", {});
    if (info.available) showAvailable(info);
    else { clear(out, banner("ok", `This camera is up to date (version ${info.current}).`)); }
    checked.textContent = "just now";
  }) }, icon("refresh"), "Check for updates");
  const checked = h("dd", {}, when(st.last_check));
  const last = st.last_update;

  const saveAuto = (checkOn, installOn, input, old) => {
    api("system/update/auto", { check: checkOn, install: installOn }).then(() => {
      toast("Saved.");
      rerender();
    }, (err) => { input.checked = old; toast(err.message, "error"); });
  };
  const autoCheck = toggle("Look for updates automatically", u.auto_check,
    (e) => saveAuto(e.target.checked, e.target.checked && u.auto, e.target, u.auto_check));
  const auto = toggle("Install updates automatically", u.auto,
    (e) => saveAuto(true, e.target.checked, e.target, u.auto));
  auto.input.disabled = !u.auto_check;
  if (!u.auto_check) auto.style.opacity = ".5";

  return [
    pageHead("Updates", "Keep the camera secure and up to date."),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("div", { style: "flex:1" }, h("h2", {}, "Version " + u.current),
        h("div", { class: "sub" }, "Installed on this camera")), running ? null : check),
      out,
      h("dl", { class: "kv", style: "margin-top:14px" },
        h("dt", {}, "Last check"), checked,
        last ? h("dt", {}, "Last update") : null,
        last ? h("dd", {}, last.ok ? `Version ${last.version}, ${when(last.time)}`
          : h("span", {}, h("span", { class: "badge error" }, "Failed"), ` ${when(last.time)} – ${last.error || "see the log"}`)) : null)),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "Automatic updates")),
      h("div", { class: "field" }, autoCheck,
        h("div", { class: "hint" }, u.auto_check
          ? "Every night between 3:00 and 4:30 the camera asks GitHub whether there is a new version."
          : "Switched off. The camera never contacts the internet on its own – look for updates yourself from time to time.")),
      h("div", { class: "field" }, auto,
        h("div", { class: "hint" }, u.auto
          ? "A new version is installed the same night. Picture and recording pause for a few minutes."
          : "New versions are only shown here and on the overview; you install them with one tap.")),
      h("ul", { class: "checklist small muted" },
        h("li", {}, icon("shield"), h("span", {}, "Updates come from the project’s releases on GitHub and are checked against the published checksum before installing.")),
        h("li", {}, icon("shield"), h("span", {}, "Your settings, cameras and recordings are kept.")),
        h("li", {}, icon("shield"), h("span", {}, "Looking for updates is the only time the camera connects to the internet.")))),
  ];
}

function backupCard(rerender) {
  const file = h("input", { type: "file", accept: ".json,application/json", class: "hidden", onchange: async () => {
    const f = file.files[0];
    file.value = "";
    if (!f) return;
    let data;
    try { data = JSON.parse(await f.text()); } catch (e) { return toast("This file is not an OnviPi backup.", "error"); }
    if (!await confirmDialog({ title: "Restore this backup?", confirm: "Restore",
      text: `All settings are replaced by the backup from ${String(data.created || "?").slice(0, 10)} `
        + `(device “${data.hostname || "?"}”), including the password and saved Wi-Fi networks.` })) return;
    busy(restore, async () => {
      await api("system/restore", data);
      toast("Backup restored. The camera restarts its services – log in with the password from the backup.");
      await sleep(3000);
      location.reload();
    });
  } });
  const restore = h("button", { class: "btn secondary", onclick: () => file.click() }, icon("upload"), "Restore backup");
  const reset = h("button", { class: "btn danger", onclick: async () => {
    const pw = await promptDialog({ title: "Reset to factory settings?", confirm: "Reset everything", danger: true, password: true,
      text: "Cameras, network, name and password are deleted and the device restarts with the setup assistant, "
        + "like a freshly flashed card. Recordings on the storage are kept. Enter your password to confirm." });
    if (pw === null) return;
    busy(reset, async () => {
      await api("system/reset", { password: pw });
      clear(appEl, h("div", { class: "login" }, h("div", { class: "card center" },
        h("div", { class: "hero" }, icon("power")), h("h1", {}, "Resetting …"),
        h("p", { class: "muted" }, "The device restarts. After two to three minutes the Wi-Fi “OnviPi-Setup-…” appears – ",
          "connect to it to set the camera up again."))));
    });
  } }, "Factory reset");
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("div", {}, h("h2", {}, "Backup and reset"),
      h("div", { class: "sub" }, "Move the settings to another device or start over"))),
    h("p", { class: "muted small" }, "The backup file contains all settings including your password and Wi-Fi passwords – keep it private. ",
      "Restored on a replacement device, connected apps such as Home Assistant keep recognising the cameras."),
    file,
    h("div", { class: "actions" },
      h("a", { class: "btn secondary", href: "/api/system/backup" }, icon("download"), "Download backup"), restore, reset));
}

/* ------------------------------------------------------------------ system */
async function pageSystem(rerender) {
  const s = await api("system");
  const cur = passwordInput({ autocomplete: "current-password" });
  const pw1 = passwordInput({ autocomplete: "new-password", maxLength: 64 });
  const pw2 = passwordInput({ autocomplete: "new-password", maxLength: 64 });
  const savePw = h("button", { class: "btn", onclick: () => busy(savePw, async () => {
    if (pw1.input.value !== pw2.input.value) throw new Error("The two new passwords are not the same.");
    await api("system/password", { current: cur.input.value, password: pw1.input.value });
    toast("Password changed. Enter the new one in connected apps too (Home Assistant: Reconfigure).");
    rerender();
  }) }, "Change password");

  const tz = select(s.timezones, s.timezone);
  const saveTz = h("button", { class: "btn", onclick: () => busy(saveTz, async () => {
    await api("system/settings", { timezone: tz.value });
    toast("Time zone saved.");
  }) }, "Save");

  const restart = h("button", { class: "btn secondary", onclick: () => busy(restart, async () => {
    await api("system/restart", {});
    toast("The camera services are restarting.");
  }) }, icon("refresh"), "Restart camera");
  const reboot = h("button", { class: "btn secondary", onclick: async () => {
    if (!await confirmDialog({ title: "Restart the device?", confirm: "Restart",
      text: "The Raspberry Pi restarts. The camera is back after about one minute." })) return;
    busy(reboot, async () => { await api("system/reboot", {}); toast("Restarting … this page reconnects in about a minute."); });
  } }, icon("power"), "Restart device");

  const nodes = [
    pageHead("System", "Password, time and maintenance."),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("div", {}, h("h2", {}, "Password"),
        h("div", { class: "sub" }, `One password for this page and connected apps · username “${s.user}”`))),
      field("Current password", cur),
      h("div", { class: "fields two" }, field("New password", pw1, s.password_hint), field("Repeat new password", pw2)),
      h("div", { class: "actions" }, savePw)),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "Time zone")),
      field("Time zone", tz, "Used for the date and time shown in the picture."),
      h("div", { class: "actions" }, saveTz)),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("div", {}, h("h2", {}, "Maintenance"), h("div", { class: "sub" }, "Version " + s.version))),
      h("p", { class: "muted small" }, "“Restart camera” helps when the picture hangs. “Restart device” restarts the whole Raspberry Pi."),
      h("div", { class: "actions" }, restart, reboot,
        h("a", { class: "btn secondary", href: "/api/system/logs" }, icon("download"), "Download log"))),
    backupCard(rerender),
  ];
  if (expert) {
    const ssh = toggle("SSH access", s.ssh, (e) => {
      const want = e.target.checked;
      api("system/ssh", { enable: want }).then((r) => { e.target.checked = r.ssh; toast(r.ssh ? "SSH is on." : "SSH is off."); },
        (err) => { e.target.checked = !want; toast(err.message, "error"); });
    });
    const who = s.login_user || s.system_users.join(", ") || "–";
    nodes.push(h("div", { class: "card" },
      h("div", { class: "card-head" }, h("div", {}, h("h2", {}, "Remote access"), h("div", { class: "sub" }, "For experts"))),
      h("div", { class: "field" }, ssh,
        h("div", { class: "hint" }, `Log in as “${who}”`, s.login_user ? " with your OnviPi password." : " with the password of that system user."),
        s.ssh && s.ssh_keys.length ? h("details", { class: "more" },
          h("summary", {}, "Fingerprint of this device"),
          h("div", { class: "more-body" },
            h("p", { class: "muted small" }, "When you connect for the first time, your SSH program shows one of these. ",
              "If it warns that the key has changed – for example after re-flashing the SD card – remove the old entry ",
              "on your computer with ", h("code", {}, `ssh-keygen -R ${location.hostname}`), "."),
            s.ssh_keys.map((k) => h("div", {}, h("code", {}, k))))) : null),
      h("div", { class: "field" }, h("span", { class: "label" }, "Terminal in the browser"),
        h("div", { class: "row" },
          h("span", { class: "badge " + (s.terminal.active ? "warn" : "") },
            s.terminal.active ? `On for ${formatDuration(s.terminal.seconds_left)}` : "Off"),
          h("a", { class: "btn secondary small", href: "#/terminal" }, icon("terminal"), "Open terminal page")))));
  }
  nodes.push(h("div", { class: "row end" }, h("button", { class: "btn ghost", onclick: logout }, icon("logout"), "Log out")));
  return nodes;
}

/* ------------------------------------------------------------------ terminal */
async function pageTerminal(rerender) {
  const s = await api("system");
  const t = s.terminal;
  const head = pageHead("Terminal", "Command line of the Raspberry Pi, right in the browser.");
  if (!t.available) {
    return [head, banner("warn", "The terminal component (ttyd) is not installed on this device. ",
      "Install it with ", h("code", {}, "sudo apt install ttyd"), " or use SSH.")];
  }
  if (!t.active) {
    const minutes = select([[15, "15 minutes"], [30, "30 minutes"], [60, "1 hour"], [120, "2 hours"]], 30);
    const on = h("button", { class: "btn", onclick: () => busy(on, async () => {
      await api("system/terminal", { enable: true, minutes: Number(minutes.value) });
      await sleep(800);
      rerender();
    }) }, icon("power"), "Switch on");
    return [head, h("div", { class: "card" },
      h("div", { class: "hero" }, icon("shield")),
      h("h2", { class: "center" }, "The terminal is switched off"),
      h("p", { class: "center muted" }, "It gives full control over the device, so it only runs when you switch it on – ",
        "and switches itself off again after the chosen time and after every restart."),
      field("Keep it on for", minutes),
      on,
      h("p", { class: "muted small", style: "margin-top:12px" },
        "The connection is not encrypted. Use it in your home network only, never through a forwarded port."))];
  }
  const who = s.login_user || s.system_users[0] || "your user";
  const left = h("span", { class: "badge warn" }, "On for " + formatDuration(t.seconds_left));
  const off = h("button", { class: "btn secondary small", onclick: () => busy(off, async () => {
    await api("system/terminal", { enable: false });
    rerender();
  }) }, icon("power"), "Switch off");
  pageTimer = setTimeout(rerender, Math.min(t.seconds_left + 2, 3600) * 1000);
  return [head,
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("div", { style: "flex:1" }, h("h2", {}, "Terminal"),
        h("div", { class: "sub" }, s.login_user ? `Signed in as “${s.login_user}” · sudo asks for your OnviPi password`
          : `Log in as “${who}”`)), left, off),
      h("iframe", { class: "terminal-frame", src: "/terminal/", title: "Terminal" }))];
}

/* ------------------------------------------------------------------ start */
async function start() {
  try {
    state = await api("state");
  } catch (e) {
    clear(appEl, h("div", { class: "login" }, h("div", { class: "card" }, banner("error", e.message),
      h("button", { class: "btn block", onclick: start }, "Try again"))));
    return;
  }
  if (!state.configured) { location.replace("/"); return; }   // setup wizard
  if (!state.authenticated) return showLogin();
  if (!location.hash) history.replaceState(null, "", "#/overview");
  renderShell();
}

window.addEventListener("hashchange", () => { if (state && state.authenticated) renderShell(); });
start();
