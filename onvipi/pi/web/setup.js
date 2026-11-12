/* OnviPi setup wizard.
   Collects everything first and changes the network only at the very end – after the
   user has confirmed that the access details are saved. Only the essentials are asked;
   everything else has a sensible default behind "More options". */
"use strict";

const STEPS = ["Welcome", "Camera", "Password", "Network", "Updates", "Save your details"];
const PASSWORD_RE = /^[A-Za-z0-9._~!*+=-]{8,64}$/;
const root = document.getElementById("step");

let S = null;              // state from the device (/api/setup/state)
let editors = [];          // camera forms of the current camera step
const A = {                // answers
  user: "", password: "", timezone: "", country: "",
  useWifi: true, iface: "", ssid: "", psk: "", hidden: false, secure: true,
  updCheck: true, updInstall: true,
};

let bar = null;            // buttons for the fixed action bar of the step being built
let lastStep = -1;

function show(step, ...content) {
  clear(document.getElementById("progress"),
    STEPS.map((_, i) => h("i", { class: i <= step ? "done" : "" })));
  document.getElementById("stepname").textContent =
    step < STEPS.length ? `${step + 1} / ${STEPS.length}` : "";
  // slide in from the side the user is moving to, like pages of an app
  root.className = step === lastStep ? "" : step > lastStep ? "enter-next" : "enter-back";
  lastStep = step;
  clear(root, content);
  clear(document.getElementById("bar-inner"), bar);
  document.getElementById("bar").classList.toggle("hidden", !bar);
  document.body.classList.toggle("has-bar", !!bar);
  bar = null;
  window.scrollTo(0, 0);
}

/* Back (left) and the main button (right) go into the fixed bar at the bottom –
   always at the same place, whatever the length of the page. */
function nav(back, next) {
  if (next) next.classList.add("grow");
  bar = [back ? h("button", { class: "btn secondary back", onclick: back }, "Back") : null, next];
  return null;
}

/* ------------------------------------------------------------------ 1 · Welcome */
function stepWelcome() {
  const item = (text) => h("li", {}, icon("check"), h("span", {}, text));
  show(0, h("div", { class: "card" },
    h("div", { class: "hero" }, icon("camera")),
    h("h1", { class: "center" }, "Let’s set up your camera"),
    h("p", { class: "center muted" }, "It takes about three minutes. You will:"),
    h("ul", { class: "checklist" },
      item("Name your camera and check the picture"),
      item("Choose a password"),
      item("Connect the camera to your home network"))),
  nav(null, h("button", { class: "btn", onclick: stepCameras }, "Get started")));
}

/* ------------------------------------------------------------------ 2 · Camera */
async function saveCameras() {
  const cams = editors.map((e) => e.value()).filter((v, i) => v.enabled || !S.cameras[i].new);
  if (!cams.length) return;
  const res = await api("setup/cameras", { cameras: cams });
  S.hostname = res.hostname || S.hostname;
}

async function stepCameras() {
  show(1, h("div", { class: "card center" }, h("div", { class: "spinner big" }), "Looking for cameras …"));
  try {
    S = await api("setup/state");
  } catch (e) { return failed(e); }
  const cams = S.cameras.filter((c) => c.present || !c.new);
  if (!cams.length) {
    return show(1, h("div", { class: "card" },
      h("div", { class: "hero" }, icon("camera")),
      h("h1", { class: "center" }, "No camera found"),
      h("p", { class: "center muted" },
        "Plug a USB webcam into the Raspberry Pi, wait a few seconds and search again."),
      h("p", { class: "center small", style: "margin-top:14px" },
        h("a", { href: "#", onclick: (e) => { e.preventDefault(); stepPassword(); } },
          "Continue without a camera – I will add one later"))),
    nav(stepWelcome, h("button", { class: "btn", onclick: stepCameras }, icon("refresh"), "Search again")));
  }
  S.cameras = cams;
  const single = cams.length === 1;
  editors = cams.map((cam) => cameraEditor(cam, {
    single,
    onApply: (btn) => busy(btn, async () => {
      await saveCameras();
      editors.forEach((e) => e.preview.reload && e.preview.reload(6000));
      toast("Applied – the live view restarts in a few seconds.");
    }),
  }));
  const next = h("button", { class: "btn", onclick: () => busy(next, async () => {
    if (!single && !editors.some((e) => e.value().enabled)) {
      throw new Error("Please switch on at least one camera.");
    }
    await saveCameras();
    stepPassword();
  }) }, "Next");
  show(1,
    h("div", { class: "page-head" },
      h("h1", {}, single ? "Is this the right picture?" : `${cams.length} cameras found`),
      h("p", {}, single ? "Give the camera a name. You can change everything later."
        : "Name each camera and switch off the ones you do not want to use.")),
    editors.map((e) => e.el),
    nav(stepWelcome, next));
}

/* ------------------------------------------------------------------ 3 · Password */
function stepPassword() {
  const pw1 = passwordInput({ autocomplete: "new-password", value: A.password, maxLength: 64 });
  const pw2 = passwordInput({ autocomplete: "new-password", value: A.password, maxLength: 64 });
  const user = h("input", { type: "text", value: A.user || S.username, maxLength: 32,
    autocapitalize: "off", autocorrect: "off", spellcheck: false, autocomplete: "username" });
  const error = h("div");
  const kept = S.draft.has_password && !A.password;
  const next = h("button", { class: "btn", onclick: () => {
    const name = user.value.trim().toLowerCase();
    const p = pw1.input.value;
    let msg = "";
    if (!/^[a-z][a-z0-9_-]{0,31}$/.test(name)) {
      msg = "Username: lowercase letters, digits, - and _, starting with a letter.";
    } else if (!(kept && !p) && !PASSWORD_RE.test(p)) {
      msg = "Password: " + S.password_hint;
    } else if (p !== pw2.input.value) {
      msg = "The two passwords are not the same.";
    }
    if (msg) return clear(error, banner("error", msg));
    A.user = name;
    A.password = p;
    stepNetwork();
  } }, "Next");
  show(2,
    h("div", { class: "card" },
      h("div", { class: "hero" }, icon("lock")),
      h("h1", { class: "center" }, "Choose a password"),
      h("p", { class: "center muted" },
        "One password for everything: this settings page and Home Assistant."),
      error,
      kept ? banner("ok", "The password you entered before is kept. Leave the fields empty to keep it.") : null,
      field("Password", pw1, S.password_hint),
      field("Repeat password", pw2),
      h("details", { class: "more" },
        h("summary", {}, "More options"),
        h("div", { class: "more-body" },
          field("Username", user, "Used together with the password. Most people keep “admin”.")))),
    nav(stepCameras, next));
}

/* ------------------------------------------------------------------ 4 · Network */
function guessRegion() {
  if (!A.timezone) {
    let tz = "";
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { /* old browser */ }
    A.timezone = S.timezones.includes(tz) ? tz : S.timezone;
  }
  if (!A.country) {
    const region = ((navigator.language || "").split("-")[1] || "").toUpperCase();
    A.country = S.countries.some((c) => c[0] === region) ? region : S.country;
  }
}

function stepNetwork(errorText) {
  guessRegion();
  const net = S.network;
  const hasWifi = net.wifi_devices.length > 0;
  if (!A.iface) A.iface = net.wifi_iface;
  if (!A.ssid && S.draft.wifi) A.ssid = S.draft.wifi.ssid;
  if (!net.ethernet) A.useWifi = true;
  else if (!S.draft.wifi && !A.ssid) A.useWifi = false;

  const list = h("div", { class: "nets" });
  const manual = h("input", { type: "text", maxLength: 32, autocapitalize: "off",
    autocorrect: "off", spellcheck: false, value: A.hidden ? A.ssid : "" });
  const manualBox = field("Network name (SSID)", manual);
  const psk = passwordInput({ autocomplete: "off", maxLength: 63, value: A.psk });
  const pskBox = field("Wi-Fi password", psk);

  const sync = () => {
    manualBox.classList.toggle("hidden", !A.hidden);
    pskBox.classList.toggle("hidden", !(A.hidden || (A.ssid && A.secure)));
  };
  const renderList = () => {
    const rows = net.networks.map((n) => h("button", {
      type: "button", class: "net" + (!A.hidden && A.ssid === n.ssid ? " selected" : ""),
      onclick: () => { A.ssid = n.ssid; A.hidden = false; A.secure = n.secure; clear(error); renderList(); psk.input.focus(); },
    }, h("span", { class: "ssid" }, n.ssid), n.secure ? icon("lock") : null, signalBars(n.signal)));
    rows.push(h("button", { type: "button", class: "net" + (A.hidden ? " selected" : ""),
      onclick: () => { A.hidden = true; A.secure = true; renderList(); manual.focus(); } },
    h("span", { class: "ssid muted" }, "Other network …")));
    clear(list, rows);
    const known = net.networks.find((n) => n.ssid === A.ssid);
    if (known && !A.hidden) A.secure = known.secure;
    sync();
  };
  const rescan = h("button", { type: "button", class: "btn ghost small", onclick: () => busy(rescan, async () => {
    const res = await api("setup/scan", { iface: A.iface });
    net.networks = res.networks;
    if (!res.fresh) toast("The list is from before the setup Wi-Fi started – it cannot scan right now.");
    renderList();
  }) }, icon("refresh"), "Search again");

  const adapter = net.wifi_devices.length > 1 ? field("Wi-Fi adapter",
    select(net.wifi_devices.map((d) => [d.iface, d.label]), A.iface,
      { onchange: (e) => { A.iface = e.target.value; } }),
    "A USB adapter with an external antenna usually has the better range.") : null;

  const wifiBox = h("div", {},
    adapter,
    h("div", { class: "row", style: "margin-bottom:6px" },
      h("span", { class: "label", style: "margin:0;flex:1" }, "Choose your Wi-Fi"), rescan),
    list, h("div", { style: "height:14px" }), manualBox, pskBox);

  const wifiSwitch = net.ethernet && hasWifi
    ? toggle("Also connect to Wi-Fi", A.useWifi, (e) => { A.useWifi = e.target.checked; wifiBox.classList.toggle("hidden", !A.useWifi); })
    : null;
  wifiBox.classList.toggle("hidden", !A.useWifi || !hasWifi);

  const tz = select(S.timezones, A.timezone, { onchange: (e) => { A.timezone = e.target.value; } });
  const country = select(S.countries.map((c) => [c[0], `${c[1]} (${c[0]})`]), A.country,
    { onchange: (e) => { A.country = e.target.value; } });

  const error = h("div", {}, errorText ? banner("error", errorText) : null);
  const next = h("button", { class: "btn", onclick: () => {
    let msg = "";
    if (A.useWifi && hasWifi) {
      if (A.hidden) A.ssid = manual.value.trim();
      A.psk = pskBox.classList.contains("hidden") ? "" : psk.input.value;
      if (!A.ssid) msg = "Please choose your Wi-Fi network.";
      else if (A.psk && (A.psk.length < 8 || A.psk.length > 63)) {
        msg = "The Wi-Fi password must be 8 to 63 characters long.";
      } else if (!A.psk && A.secure) msg = "Please enter the Wi-Fi password.";
    } else if (!net.ethernet) {
      msg = "No Wi-Fi adapter found. Please plug in a network cable and reload this page.";
    }
    if (msg) { clear(error, banner("error", msg)); window.scrollTo(0, 0); return; }
    stepUpdates();
  } }, "Next");

  renderList();
  show(3,
    h("div", { class: "page-head" },
      h("h1", {}, "Connect to your network"),
      h("p", {}, net.ethernet ? "The camera is already connected by cable."
        : "Choose the Wi-Fi the camera should use.")),
    error,
    net.ethernet ? banner("ok", h("b", {}, "Connected by network cable"),
      net.ip ? ` · address ${net.ip}` : "", h("br"),
      "Nothing else is needed here. Wi-Fi is optional.") : null,
    h("div", { class: "card" },
      wifiSwitch ? h("div", { class: "field" }, wifiSwitch) : null,
      hasWifi ? wifiBox : (net.ethernet ? h("p", { class: "muted" }, "No Wi-Fi adapter found – the camera uses the cable.") : null),
      h("details", { class: "more" },
        h("summary", {}, "More options"),
        h("div", { class: "more-body" },
          field("Time zone", tz, "Used for the date and time shown in the picture."),
          field("Wi-Fi country", country, "Sets the Wi-Fi channels allowed in your country."),
          h("p", { class: "muted small" },
            "A fixed IP address can be set later on the settings page under Network.")))),
    nav(stepPassword, next));
}

/* ------------------------------------------------------------------ 5 · Updates */
function stepUpdates() {
  const note = h("div");
  const install = toggle("Install updates automatically", A.updInstall, (e) => { A.updInstall = e.target.checked; sync(); });
  const check = toggle("Look for updates automatically", A.updCheck, (e) => {
    A.updCheck = e.target.checked;
    if (!A.updCheck) A.updInstall = false;
    sync();
  });
  const sync = () => {
    install.input.checked = A.updCheck && A.updInstall;
    install.input.disabled = !A.updCheck;
    install.style.opacity = A.updCheck ? "" : ".5";
    clear(note, !A.updCheck
      ? banner("warn", h("b", {}, "The camera will not learn about security fixes by itself. "),
        "Look for updates yourself from time to time on the page “Updates”.")
      : !A.updInstall ? banner("info", "The settings page tells you when a new version is available; you install it with one tap.")
        : null);
  };
  sync();
  show(4,
    h("div", { class: "card" },
      h("div", { class: "hero" }, icon("shield")),
      h("h1", { class: "center" }, "Keep the camera up to date"),
      h("p", { class: "center muted" }, "Updates fix security problems and bring improvements. We recommend leaving both switched on."),
      h("div", { class: "field" }, check,
        h("div", { class: "hint" }, "Once a night the camera asks GitHub whether there is a new version. ",
          "Nothing else is sent – this is the only time the camera connects to the internet.")),
      h("div", { class: "field" }, install,
        h("div", { class: "hint" }, "A new version is installed the same night. Picture and recording pause for a few minutes; your settings are kept.")),
      note,
      h("p", { class: "muted small" }, "You can change both later on the page “Updates”.")),
    nav(() => stepNetwork(), h("button", { class: "btn", onclick: stepSave }, "Next")));
}

/* ------------------------------------------------------------------ 6 · Save details */
function detailsText() {
  return [
    `OnviPi camera “${S.cameras.length ? editors[0].value().name : S.name}”`,
    "",
    `Settings page: http://${S.hostname}.local`,
    `Username:      ${A.user}`,
    `Password:      ${A.password || "(the password you chose)"}`,
    "",
    "Home Assistant: Settings > Devices & services > Add integration > ONVIF,",
    "select the camera and enter this username and password.",
    "",
  ].join("\r\n");
}

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const area = h("textarea", { value: text, style: "position:fixed;opacity:0" });
  document.body.append(area);
  area.select();
  const ok = document.execCommand("copy");
  area.remove();
  return ok ? Promise.resolve() : Promise.reject(new Error("Copying is not possible here – please write the details down."));
}

function stepSave() {
  const wifi = A.useWifi && S.network.wifi_devices.length > 0;
  const drops = wifi && S.network.via_hotspot && !S.network.stays_connected;
  const check = h("input", { type: "checkbox", onchange: () => { finish.disabled = !check.checked; } });
  const finish = h("button", { class: "btn", disabled: true, onclick: () => busy(finish, doFinish) },
    wifi ? "Connect and finish" : "Finish setup");
  const copy = h("button", { class: "btn secondary small", onclick: () =>
    copyText(detailsText()).then(() => toast("Copied."), (e) => toast(e.message, "error")) },
  icon("copy"), "Copy");
  const download = h("a", { class: "btn secondary small", download: "onvipi-access.txt",
    href: "data:text/plain;charset=utf-8," + encodeURIComponent(detailsText()) },
  icon("download"), "Save as file");
  show(5,
    h("div", { class: "page-head" },
      h("h1", {}, "Save your access details"),
      h("p", {}, "You need them to open the settings page and to add the camera to Home Assistant. ",
        "Take your time – nothing happens until you tap the button below.")),
    h("div", { class: "card" },
      h("dl", { class: "kv" },
        h("dt", {}, "Settings page"), h("dd", { class: "secret" }, `http://${S.hostname}.local`),
        h("dt", {}, "Username"), h("dd", { class: "secret" }, A.user),
        h("dt", {}, "Password"), h("dd", { class: "secret" }, A.password || "(the password you chose)")),
      h("div", { class: "actions" }, copy, download),
      h("p", { class: "muted small", style: "margin-top:12px" },
        "A screenshot or a note on paper works just as well.")),
    h("div", { class: "card" },
      h("h2", {}, "What happens next"),
      h("ol", { class: "steps" },
        wifi ? h("li", {}, "The camera connects to ", h("b", {}, A.ssid), ".") : h("li", {}, "The camera stays connected by cable."),
        drops ? h("li", {}, "This setup Wi-Fi (", h("b", {}, S.network.ap_ssid),
          ") switches off and your phone returns to your normal Wi-Fi.") : null,
        h("li", {}, "Open ", h("b", {}, `http://${S.hostname}.local`), " and log in with your password."),
        h("li", {}, "The settings page shows how to add the camera to Home Assistant.")),
      drops ? h("p", { class: "muted small" }, "If ", h("b", {}, S.network.ap_ssid),
        " shows up again after a minute, the Wi-Fi password was wrong. Connect to it again and correct it.") : null),
    h("div", { class: "card" },
      h("label", { class: "check" }, check,
        h("span", {}, h("b", {}, "I have saved the username and password."), h("br"),
          h("span", { class: "muted small" }, "Without them you cannot open the settings later.")))),
    nav(stepUpdates, finish));
}

/* ------------------------------------------------------------------ Finish */
async function doFinish() {
  const wifi = A.useWifi && S.network.wifi_devices.length > 0;
  await api("setup/finish", {
    user: A.user, password: A.password, timezone: A.timezone, country: A.country,
    wifi: wifi ? { iface: A.iface, ssid: A.ssid, psk: A.psk, hidden: A.hidden } : null,
    updates: { check: A.updCheck, install: A.updCheck && A.updInstall },
  });
  watchProgress(wifi && S.network.via_hotspot && !S.network.stays_connected);
}

function showWorking(drops) {
  show(STEPS.length,
    h("div", { class: "card" },
      h("div", { class: "spinner big" }),
      h("h1", { class: "center" }, drops ? "Connecting to your Wi-Fi …" : "Finishing setup …"),
      drops ? h("div", {},
        h("p", { class: "center muted" }, "The setup Wi-Fi is switching off now. This page cannot update itself any more."),
        h("ol", { class: "steps" },
          h("li", {}, "Wait about one minute."),
          h("li", {}, "Make sure your phone is back on your normal Wi-Fi."),
          h("li", {}, "Open ", h("a", { href: `http://${S.hostname}.local` }, `http://${S.hostname}.local`),
            " and log in with your password.")),
        banner("warn", "If the Wi-Fi ", h("b", {}, S.network.ap_ssid),
          " shows up again, the Wi-Fi password was wrong. Connect to it again – this assistant reopens at the network step."))
        : h("p", { class: "center muted" }, "This takes up to a minute. Please keep this page open.")));
}

function showDone(progress) {
  const sameNetwork = !S.network.via_hotspot;
  const url = sameNetwork ? "/" : `http://${progress.ip || S.hostname + ".local"}/`;
  show(STEPS.length,
    h("div", { class: "card" },
      h("div", { class: "hero ok" }, icon("check")),
      h("h1", { class: "center" }, "Your camera is ready"),
      h("dl", { class: "kv", style: "margin:16px 0" },
        h("dt", {}, "Settings page"), h("dd", {}, `http://${progress.hostname || S.hostname}.local`),
        progress.ip ? h("dt", {}, "IP address") : null, progress.ip ? h("dd", {}, progress.ip) : null),
      sameNetwork ? null : banner("info", "The setup Wi-Fi switches off in about a minute. ",
        "Connect your phone to your normal Wi-Fi again, then open the settings page."),
      h("p", { class: "center muted small", style: "margin-top:12px" },
        "Log in with your password. The page “Home Assistant” shows the next steps.")),
    nav(null, h("a", { class: "btn", href: url }, "Open the settings page")));
}

async function watchProgress(drops) {
  showWorking(drops);
  for (let unreachable = 0; ;) {
    await sleep(1500);
    let p;
    try {
      p = await api("setup/progress");
      unreachable = 0;
    } catch (e) {
      if (++unreachable > 400) return;   // ~10 minutes: give up silently, the instructions stay
      continue;
    }
    if (p.phase === "done" || p.configured) return showDone(p);
    if (p.phase === "failed") {
      try { S = await api("setup/state"); } catch (e) { /* keep the old state */ }
      return stepNetwork(p.error);
    }
  }
}

/* ------------------------------------------------------------------ Start */
function failed(e) {
  if (e.status === 403) { location.replace("/"); return; }   // already set up
  show(0, h("div", { class: "card" }, banner("error", e.message)),
    nav(null, h("button", { class: "btn", onclick: () => location.reload() }, "Try again")));
}

(async function start() {
  document.getElementById("logo").append(icon("camera"));
  try {
    S = await api("setup/state");
  } catch (e) { return failed(e); }
  if (S.progress.phase === "working") return watchProgress(false);
  if (S.progress.phase === "failed") {
    // second attempt after a wrong Wi-Fi password: the device kept the earlier answers
    A.user = S.draft.user || "";
    A.timezone = S.draft.timezone || "";
    A.country = S.draft.country || "";
    S.cameras = S.cameras.filter((c) => c.present || !c.new);
    editors = S.cameras.map((cam) => cameraEditor(cam, { single: S.cameras.length === 1 }));
    return stepNetwork(S.progress.error);
  }
  stepWelcome();
})();
