/**
 * MJPEG: stejný origin (/mjpeg/stream.mjpeg). Nginx aliasy: /video/stream.mjpeg, /stream.mjpeg, /api/stream_mjpeg.
 * UI musí jít přes Nginx (typ. :80), ne přímo na :8080 — jinak FastAPI /mjpeg/ neumí a <img> dostane 404.
 * Nepoužívat crossOrigin u <img> – u multipart streamu to umí rozbít prohlížeč.
 */

const MJPEG_PATH = "/mjpeg/stream.mjpeg";

const presets = [
  {
    label: "Demo · soubor v image (doporučeno)",
    uri: "file:///opt/rpy/assets/sample.mp4",
  },
  {
    label: "Demo · HTTP MP4 (samplelib)",
    uri: "https://samplelib.com/lib/preview/mp4/sample-5s.mp4",
  },
  {
    label: "RTSP (vlastní kamera)",
    uri: "rtsp://user:pass@192.168.1.33:8554/stream",
  },
  {
    label: "YouTube (yt-dlp v kontejneru)",
    uri: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  },
];

function $(id) {
  return document.getElementById(id);
}

let appAlertTimer = null;

/** Normalizuje tělo chyby z FastAPI (`detail` string | objekt | pole) i přímé `{ code, message }`. */
function formatApiError(j, res) {
  if (!j || typeof j !== "object") return res.statusText || "Unknown error";
  if (j.code && j.message) return `${j.code}: ${j.message}`;
  const d = j.detail;
  if (d && typeof d === "object" && !Array.isArray(d) && (d.message != null || d.code != null)) {
    return [d.code, d.message].filter((x) => x != null && x !== "").join(" — ");
  }
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => x.msg || JSON.stringify(x)).join("; ");
  return j.message || res.statusText;
}

/**
 * Globální lišta (role=status). `level` null / prázdná zpráva = skrýt.
 * @param {"error"|"warn"|"info"|""} level
 * @param {number} autoDismissMs 0 = bez auto-úklidu
 */
function setAppAlert(level, message, autoDismissMs) {
  const el = $("appAlert");
  if (!el) return;
  if (appAlertTimer) {
    clearTimeout(appAlertTimer);
    appAlertTimer = null;
  }
  if (!level || !message) {
    el.hidden = true;
    el.textContent = "";
    el.className = "app-alert";
    document.body.classList.remove("has-app-alert");
    return;
  }
  el.hidden = false;
  el.className = `app-alert app-alert--${level}`;
  el.textContent = message;
  document.body.classList.add("has-app-alert");
  if (autoDismissMs > 0) {
    appAlertTimer = setTimeout(() => {
      setAppAlert("", "");
    }, autoDismissMs);
  }
}

async function fetchHealth() {
  try {
    const r = await fetch("/health");
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      setAppAlert("error", `Backend /health: ${formatApiError(j, r)}`, 0);
    }
  } catch (e) {
    setAppAlert("error", `Backend /health: ${String(e)}`, 0);
  }
}

function mjpegUrl() {
  const u = new URL(MJPEG_PATH, window.location.origin);
  u.searchParams.set("t", String(Date.now()));
  return u.toString();
}

function initSources() {
  const ul = $("sources");
  presets.forEach((p) => {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = p.label;
    b.addEventListener("click", () => {
      $("srcUri").value = p.uri;
    });
    li.appendChild(b);
    ul.appendChild(li);
  });
}

/** Přepočet letterboxu u object-fit: contain — overlay musí sedět na video, ne na celý box. */
function layoutOverlay() {
  const img = $("mjpeg");
  const svg = $("overlay");
  const stage = $("videoStage");
  if (!img || !svg || !stage) return;

  const cw = stage.clientWidth;
  const ch = stage.clientHeight;
  const nw = img.naturalWidth || 0;
  const nh = img.naturalHeight || 0;
  if (cw <= 0 || ch <= 0) return;

  let dispW = cw;
  let dispH = ch;
  let ox = 0;
  let oy = 0;

  if (nw > 0 && nh > 0) {
    const scale = Math.min(cw / nw, ch / nh);
    dispW = nw * scale;
    dispH = nh * scale;
    ox = (cw - dispW) / 2;
    oy = (ch - dispH) / 2;
  }

  svg.style.left = `${ox}px`;
  svg.style.top = `${oy}px`;
  svg.style.width = `${dispW}px`;
  svg.style.height = `${dispH}px`;
}

function drawBoxes(detections) {
  const svg = $("overlay");
  if (!detections || !detections.detections || !detections.detections.length) {
    svg.innerHTML = "";
    return;
  }
  svg.setAttribute("viewBox", "0 0 1 1");
  const frag = document.createDocumentFragment();
  detections.detections.forEach((d) => {
    const b = d.box;
    const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    r.setAttribute("x", String(b.x));
    r.setAttribute("y", String(b.y));
    r.setAttribute("width", String(b.w));
    r.setAttribute("height", String(b.h));
    r.setAttribute("rx", "0.008");
    frag.appendChild(r);
  });
  svg.innerHTML = "";
  svg.appendChild(frag);
}

let charts = {};

function ensureCharts() {
  if (typeof Chart === "undefined") return;
  const baseOpts = {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { intersect: false, mode: "index" },
    plugins: {
      legend: { display: false },
      tooltip: {
        enabled: true,
        titleFont: { size: 11 },
        bodyFont: { size: 11 },
      },
    },
    scales: {
      x: {
        display: false,
        grid: { display: false },
      },
      y: {
        beginAtZero: true,
        grid: { color: "rgba(255,255,255,0.07)" },
        ticks: {
          maxTicksLimit: 5,
          font: { size: 9 },
          color: "#8b8b96",
        },
      },
    },
    layout: {
      padding: { left: 2, right: 4, top: 4, bottom: 2 },
    },
  };
  const common = {
    type: "line",
    options: baseOpts,
  };
  if (!charts.lat) {
    charts.lat = new Chart($("chLat"), {
      ...common,
      data: {
        labels: [],
        datasets: [
          {
            label: "Latency ms",
            data: [],
            borderColor: "#3b82f6",
            backgroundColor: "rgba(59,130,246,0.08)",
            fill: true,
            tension: 0.25,
            borderWidth: 1.5,
            pointRadius: 0,
            pointHoverRadius: 3,
          },
        ],
      },
    });
  }
  if (!charts.fps) {
    charts.fps = new Chart($("chFps"), {
      ...common,
      data: {
        labels: [],
        datasets: [
          {
            label: "FPS",
            data: [],
            borderColor: "#22c55e",
            backgroundColor: "rgba(34,197,94,0.08)",
            fill: true,
            tension: 0.25,
            borderWidth: 1.5,
            pointRadius: 0,
            pointHoverRadius: 3,
          },
        ],
      },
    });
  }
  if (!charts.temp) {
    charts.temp = new Chart($("chTemp"), {
      ...common,
      data: {
        labels: [],
        datasets: [
          {
            label: "SoC °C",
            data: [],
            borderColor: "#f97316",
            tension: 0.25,
            borderWidth: 1.5,
            pointRadius: 0,
            pointHoverRadius: 3,
          },
          {
            label: "Hailo °C",
            data: [],
            borderColor: "#a855f7",
            tension: 0.25,
            borderWidth: 1.5,
            pointRadius: 0,
            pointHoverRadius: 3,
          },
        ],
      },
      options: {
        ...baseOpts,
        plugins: {
          ...baseOpts.plugins,
          legend: {
            display: true,
            position: "top",
            align: "end",
            labels: {
              boxWidth: 10,
              boxHeight: 8,
              font: { size: 9 },
              color: "#a1a1aa",
              usePointStyle: true,
              padding: 6,
            },
          },
        },
      },
    });
  }
}

function pushChart(ch, val, datasetIndex) {
  const ds = ch.data.datasets[datasetIndex ?? 0].data;
  const labels = ch.data.labels;
  const n = 40;
  ds.push(val);
  if (datasetIndex === undefined || datasetIndex === 0) {
    labels.push("");
  }
  if (ds.length > n) {
    ds.shift();
    if (datasetIndex === undefined || datasetIndex === 0) {
      labels.shift();
    }
  }
  ch.update("none");
}

const streamState = {
  lastLoadAt: 0,
  _startedAt: 0,
  staleTimer: null,
  STALE_MS: 8000,
};

function setStreamPill(state, text) {
  const el = $("streamStatus");
  if (!el) return;
  el.dataset.state = state;
  el.textContent = text;
}

function setWsPill(ok, text) {
  const el = $("wsStatus");
  if (!el) return;
  el.dataset.state = ok ? "ok" : "dead";
  el.textContent = text;
}

function showStreamError(show) {
  const fb = $("streamFallback");
  if (fb) fb.hidden = !show;
}

function attachMjpegHandlers(img) {
  img.addEventListener("load", () => {
    streamState.lastLoadAt = Date.now();
    setStreamPill("live", "MJPEG · živě");
    showStreamError(false);
    layoutOverlay();
  });
  img.addEventListener("error", () => {
    setStreamPill("dead", "MJPEG · chyba");
    showStreamError(true);
  });
}

function startStreamStaleWatch() {
  if (streamState.staleTimer) clearInterval(streamState.staleTimer);
  streamState.staleTimer = setInterval(() => {
    const img = $("mjpeg");
    if (!img || !img.src) return;
    if ($("streamFallback") && !$("streamFallback").hidden) return;
    const now = Date.now();
    if (!streamState.lastLoadAt) {
      if (img.complete === false && now - (streamState._startedAt || now) > 12000) {
        setStreamPill("dead", "MJPEG · bez odpovědi");
        showStreamError(true);
      }
      return;
    }
    const age = now - streamState.lastLoadAt;
    if (age > streamState.STALE_MS) {
      setStreamPill("stale", "MJPEG · bez dat");
    }
  }, 2000);
}

function reloadMjpeg() {
  const img = $("mjpeg");
  if (!img) return;
  setStreamPill("unknown", "MJPEG · načítám…");
  showStreamError(false);
  img.removeAttribute("src");
  streamState._startedAt = Date.now();
  requestAnimationFrame(() => {
    img.src = mjpegUrl();
  });
}

async function swapSource() {
  const uri = $("srcUri").value.trim();
  if (!uri) return;
  $("swapState").textContent = "Přepínám…";
  try {
    const r = await fetch("/api/v1/source", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uri, label: "hot-swap" }),
    });
    const raw = await r.text();
    let j = {};
    try {
      j = JSON.parse(raw);
    } catch {
      j = {};
    }
    if (!r.ok) {
      const msg = formatApiError(j, r) || raw || r.statusText;
      $("swapState").textContent = ("HTTP " + r.status + ": " + msg).slice(0, 400);
      setAppAlert("warn", `Zdroj: ${msg}`.slice(0, 500), 12000);
      return;
    }
    $("swapState").textContent = j.state || "OK";
    setTimeout(reloadMjpeg, 600);
  } catch (e) {
    $("swapState").textContent = String(e);
    setAppAlert("error", `Zdroj: ${String(e)}`, 0);
  }
}

async function patchModel() {
  const c = parseFloat($("conf").value);
  try {
    const r = await fetch("/api/v1/model", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confidence_threshold: c }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      setAppAlert("warn", `Model: ${formatApiError(j, r)}`, 10000);
      return;
    }
    setAppAlert("info", "Prah confidence uložen.", 4500);
  } catch (e) {
    setAppAlert("error", `Model: ${String(e)}`, 0);
  }
}

let wsBackoff = 1000;
const WS_BACKOFF_MAX = 30000;
/** Poslední pipeline chyba z WS — banner jen při změně textu, ne každých 250 ms. */
let lastPipelineErrorBanner = "";

function initWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/telemetry`);
  setWsPill(false, "WS …");

  ws.onopen = () => {
    wsBackoff = 1000;
    setWsPill(true, "WS · OK");
  };

  ws.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch (err) {
      setAppAlert("warn", `WS telemetrie: neplatný JSON (${String(err)})`, 8000);
      return;
    }
    const tel = msg.telemetry || {};
    const det = msg.detections || {};
    if (msg._meta && msg._meta.redis_degraded && msg._meta.streak === 1) {
      setAppAlert(
        "warn",
        "Telemetrie: dočasný výpadek Redis — data v UI mohou zaostávat.",
        14000,
      );
    }
    window.__lastDetections = det;
    $("stateBadge").textContent = tel.pipeline_state || "—";
    drawBoxes(det);
    layoutOverlay();

    const m = $("metrics");
    const parts = [];
    if (tel.inference_latency_ms != null) {
      parts.push(`Latence ${tel.inference_latency_ms.toFixed(1)} ms`);
    }
    if (tel.fps != null) {
      parts.push(`FPS ${tel.fps.toFixed(1)}`);
    }
    if (tel.soc_temp_c != null) {
      parts.push(`SoC ${tel.soc_temp_c.toFixed(1)} °C`);
    }
    if (tel.hailo_temp_c != null) {
      parts.push(`Hailo ${tel.hailo_temp_c.toFixed(1)} °C`);
    }
    if (tel.bitrate_kbps != null) {
      parts.push(`Bitrate ${tel.bitrate_kbps.toFixed(0)} kb/s`);
    }
    if (tel.packet_loss_pct != null) {
      parts.push(`Ztráta paketů ${tel.packet_loss_pct.toFixed(2)} %`);
    }
    if (tel.camera_connected === false) {
      parts.push("kamera: offline");
    }
    if (tel.last_error) {
      parts.push("chyba: " + String(tel.last_error).slice(0, 160));
      const errStr = String(tel.last_error).slice(0, 280);
      if (errStr !== lastPipelineErrorBanner) {
        lastPipelineErrorBanner = errStr;
        setAppAlert("warn", `Pipeline: ${errStr}`, 12000);
      }
    } else {
      lastPipelineErrorBanner = "";
    }
    m.textContent = parts.join(" · ");

    const diag = $("pipelineDiagnostics");
    if (diag) {
      const lines = [];
      if (tel.last_error) lines.push(String(tel.last_error));
      const ex = tel.extra || {};
      if (ex.resolution_error) lines.push("Zdroj: " + ex.resolution_error);
      if (ex.last_gst_error && String(ex.last_gst_error) !== String(tel.last_error)) {
        lines.push("GStreamer: " + ex.last_gst_error);
      }
      if (ex.recovery_cycles > 0) {
        lines.push("Obnovení pipeline: " + ex.recovery_cycles);
      }
      if (ex.configured_uri) lines.push("Nastaveno: " + ex.configured_uri);
      if (ex.playback_uri && ex.playback_uri !== ex.configured_uri) {
        lines.push("Přehrávání: " + ex.playback_uri);
      }
      const ingress = ex.ingress_mode || ex.rtsp_mode;
      if (ingress) {
        lines.push("Režim ingestu: " + ingress);
      }
      if (lines.length) {
        diag.hidden = false;
        diag.textContent = [...new Set(lines)].join("\n");
      } else {
        diag.hidden = true;
        diag.textContent = "";
      }
    }

    ensureCharts();
    if (charts.lat && tel.inference_latency_ms != null) {
      pushChart(charts.lat, tel.inference_latency_ms, 0);
    }
    if (charts.fps && tel.fps != null) {
      pushChart(charts.fps, tel.fps, 0);
    }
    if (charts.temp) {
      if (tel.soc_temp_c != null) {
        pushChart(charts.temp, tel.soc_temp_c, 0);
      }
      if (tel.hailo_temp_c != null) {
        const ds1 = charts.temp.data.datasets[1].data;
        const n = 40;
        ds1.push(tel.hailo_temp_c);
        if (ds1.length > n) ds1.shift();
        charts.temp.update("none");
      }
    }
  };

  ws.onerror = () => setWsPill(false, "WS · chyba");

  ws.onclose = () => {
    setWsPill(false, "WS · offline");
    setTimeout(() => {
      wsBackoff = Math.min(wsBackoff * 1.5, WS_BACKOFF_MAX);
      initWs();
    }, wsBackoff);
  };
}

let eventsRedisWarned = false;

async function loadEvents() {
  let r;
  let j = {};
  try {
    r = await fetch("/api/v1/events?count=20");
    j = await r.json().catch(() => ({}));
  } catch (e) {
    setAppAlert("warn", `Události: ${String(e)}`, 8000);
    return;
  }
  if (!r.ok) {
    setAppAlert("warn", `Události: ${formatApiError(j, r)}`, 10000);
    return;
  }
  if (j.error === "redis_failed" && !eventsRedisWarned) {
    eventsRedisWarned = true;
    setAppAlert(
      "warn",
      "Události: Redis nedostupný — seznam může být neúplný.",
      12000,
    );
  }
  const log = $("log");
  log.innerHTML = "";
  (j.events || []).forEach((e) => {
    const row = document.createElement("div");
    row.className = "log-row";
    const im = document.createElement("img");
    const name =
      e.snapshot_name || (e.snapshot || "").split("/").pop() || "";
    im.src = name ? `/api/v1/snapshots/${encodeURIComponent(name)}` : "";
    im.alt = "";
    row.appendChild(im);
    const t = document.createElement("span");
    const attrs = e.attributes && Object.keys(e.attributes).length
      ? ` · ${JSON.stringify(e.attributes)}`
      : "";
    t.textContent = `${e.label || e.kind || "event"} · f${e.frame_id ?? "?"}${attrs}`;
    row.appendChild(t);
    log.appendChild(row);
  });
}

function renderRecordingUI(catalog, policy, mount) {
  const form = document.createElement("div");
  form.className = "recording-form";

  const mc = document.createElement("label");
  mc.className = "rec-row";
  mc.textContent = "Min. confidence ";
  const mcIn = document.createElement("input");
  mcIn.type = "number";
  mcIn.step = "0.01";
  mcIn.min = "0";
  mcIn.max = "1";
  mcIn.id = "recMinConf";
  mcIn.value = String(policy.min_confidence ?? 0.45);
  mc.appendChild(mcIn);
  form.appendChild(mc);

  const snap = document.createElement("label");
  snap.className = "rec-row";
  const snapCb = document.createElement("input");
  snapCb.type = "checkbox";
  snapCb.id = "recSnap";
  snapCb.checked = !!policy.store_snapshots;
  snap.appendChild(snapCb);
  snap.appendChild(document.createTextNode(" Ukládat snímky (JPEG)"));
  form.appendChild(snap);

  const mem = document.createElement("label");
  mem.className = "rec-row";
  mem.textContent = "Max událostí / min ";
  const memIn = document.createElement("input");
  memIn.type = "number";
  memIn.id = "recMaxEv";
  memIn.min = "1";
  memIn.value = String(policy.max_events_per_minute ?? 120);
  mem.appendChild(memIn);
  form.appendChild(mem);

  const enabledSet = new Set(
    (policy.enabled_labels || []).map((x) => String(x).toLowerCase()),
  );
  const attrMap = policy.attributes_for_label || {};

  (catalog.entities || []).forEach((ent) => {
    const primary = (ent.match_labels && ent.match_labels[0]) || ent.id;
    const box = document.createElement("div");
    box.className = "recording-entity";
    const h = document.createElement("div");
    h.className = "recording-entity-title";
    h.textContent = ent.label_cs;
    box.appendChild(h);
    const enCb = document.createElement("input");
    enCb.type = "checkbox";
    enCb.dataset.role = "en-label";
    enCb.dataset.label = primary;
    enCb.checked = enabledSet.has(String(primary).toLowerCase());
    const enL = document.createElement("label");
    enL.className = "rec-row";
    enL.appendChild(enCb);
    enL.appendChild(
      document.createTextNode(` Zaznamenávat třídu „${primary}“`),
    );
    box.appendChild(enL);

    (ent.attributes || []).forEach((at) => {
      const row = document.createElement("label");
      row.className = "recording-attr";
      const a = document.createElement("input");
      a.type = "checkbox";
      a.dataset.role = "attr";
      a.dataset.label = primary;
      a.dataset.attr = at.id;
      const pk = String(primary).toLowerCase();
      const cur = attrMap[pk] || [];
      a.checked = cur.includes(at.id);
      row.appendChild(a);
      row.appendChild(document.createTextNode(` ${at.label_cs}`));
      if (at.requires_capability) {
        const sp = document.createElement("span");
        sp.className = "hint";
        sp.textContent = " (data z modelu)";
        row.appendChild(sp);
      }
      box.appendChild(row);
    });
    form.appendChild(box);
  });

  const btn = document.createElement("button");
  btn.type = "button";
  btn.textContent = "Uložit politiku ukládání";
  btn.addEventListener("click", () => saveRecordingPolicy(form));
  form.appendChild(btn);

  const status = document.createElement("div");
  status.id = "recPolicyStatus";
  status.className = "hint";
  form.appendChild(status);

  mount.appendChild(form);
}

async function saveRecordingPolicy(formRoot) {
  const minC = parseFloat($("recMinConf").value);
  const storeSn = $("recSnap").checked;
  const maxEv = parseInt($("recMaxEv").value, 10);
  const enabled_labels = [];
  const attributes_for_label = {};
  formRoot.querySelectorAll('input[data-role="en-label"]:checked').forEach((cb) => {
    enabled_labels.push(String(cb.dataset.label).toLowerCase());
  });
  formRoot.querySelectorAll('input[data-role="attr"]:checked').forEach((cb) => {
    const lab = cb.dataset.label.toLowerCase();
    const aid = cb.dataset.attr;
    if (!attributes_for_label[lab]) attributes_for_label[lab] = [];
    attributes_for_label[lab].push(aid);
  });
  const body = {
    min_confidence: minC,
    store_snapshots: storeSn,
    max_events_per_minute: maxEv,
    enabled_labels: [...new Set(enabled_labels)],
    attributes_for_label,
  };
  const st = $("recPolicyStatus");
  try {
    const res = await fetch("/api/v1/recording/policy", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) {
      const msg = formatApiError(j, res);
      st.textContent = msg;
      setAppAlert("warn", `Politika záznamu: ${msg}`, 12000);
      return;
    }
    st.textContent = "Uloženo — ai_core obdrží přes Redis.";
    setAppAlert("info", "Politika záznamu uložena.", 5000);
  } catch (e) {
    st.textContent = String(e);
    setAppAlert("error", `Politika záznamu: ${String(e)}`, 0);
  }
}

async function initRecording() {
  const mount = $("recordingPanel");
  if (!mount) return;
  try {
    const [catRes, polRes] = await Promise.all([
      fetch("/api/v1/recording/catalog"),
      fetch("/api/v1/recording/policy"),
    ]);
    const catalog = await catRes.json();
    const polJson = await polRes.json();
    if (!catRes.ok) {
      mount.textContent = `Katalog tříd nedostupný: ${formatApiError(catalog, catRes)}`;
      setAppAlert("warn", `Katalog záznamu: ${formatApiError(catalog, catRes)}`, 0);
      return;
    }
    if (!polRes.ok) {
      mount.textContent = "Politika nedostupná (spusťte PostgreSQL a web).";
      setAppAlert(
        "warn",
        `Politika: ${formatApiError(polJson, polRes)}`,
        0,
      );
      return;
    }
    if (polJson.source === "error") {
      setAppAlert(
        "warn",
        `Politika záznamu: čtení z DB selhalo (${polJson.error_code || "?"}) — zobrazeny výchozí hodnoty.`,
        0,
      );
    }
    mount.innerHTML = "";
    renderRecordingUI(catalog, polJson.policy, mount);
  } catch (e) {
    mount.textContent = String(e);
  }
}

function initViewToggle() {
  const main = $("viewMain");
  const ai = $("viewAi");
  const wrap = document.querySelector(".video-wrap");
  if (!main || !ai || !wrap) return;
  main.addEventListener("click", () => {
    main.classList.add("active");
    ai.classList.remove("active");
    wrap.classList.remove("view-ai-mode");
  });
  ai.addEventListener("click", () => {
    ai.classList.add("active");
    main.classList.remove("active");
    wrap.classList.add("view-ai-mode");
  });
}

window.addEventListener("DOMContentLoaded", () => {
  fetchHealth();
  initSources();
  initViewToggle();

  const img = $("mjpeg");
  attachMjpegHandlers(img);
  streamState.lastLoadAt = 0;
  streamState._startedAt = Date.now();
  setStreamPill("unknown", "MJPEG · načítám…");
  img.src = mjpegUrl();
  startStreamStaleWatch();

  $("btnReloadStream")?.addEventListener("click", reloadMjpeg);
  $("btnRetryStream")?.addEventListener("click", reloadMjpeg);

  $("btnFs")?.addEventListener("click", () => {
    const w = $("videoWrap");
    if (!w) return;
    if (!document.fullscreenElement) {
      w.requestFullscreen?.();
    } else {
      document.exitFullscreen?.();
    }
  });

  $("conf").addEventListener("input", () => {
    $("confVal").textContent = $("conf").value;
  });
  $("btnSwap").addEventListener("click", swapSource);
  $("btnModel").addEventListener("click", patchModel);
  initRecording();
  initWs();
  setInterval(loadEvents, 4000);
  loadEvents();

  window.addEventListener("resize", () => {
    layoutOverlay();
    const det = window.__lastDetections;
    if (det) drawBoxes(det);
  });
});
