/**
 * dashboard.js — NEUROSENSE real-time dashboard
 * Consumes SSE stream from /stream and updates Chart.js charts + metric cards.
 */

"use strict";

// ── Configuration ─────────────────────────────────────────────────────────
const MAX_POINTS = 60; // rolling window (60 data points ≈ 60 s at 1 Hz)
const RECONNECT_MS = 3000; // reconnect delay after SSE error

// ── Chart defaults ────────────────────────────────────────────────────────
Chart.defaults.color = "#52697e";
Chart.defaults.borderColor = "#d0d9e4";
Chart.defaults.font.family =
  '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
Chart.defaults.font.size = 11;

const CHART_OPTIONS = (yLabel, suggestedMin, suggestedMax) => ({
  animation: false,
  responsive: true,
  interaction: { mode: "index", intersect: false },
  plugins: {
    legend: { labels: { boxWidth: 12, padding: 14 } },
    tooltip: {
      backgroundColor: "#ffffff",
      titleColor: "#1a2b3c",
      bodyColor: "#52697e",
      borderColor: "#d0d9e4",
      borderWidth: 1,
    },
  },
  scales: {
    x: { grid: { color: "#e8edf3" }, ticks: { maxTicksLimit: 8 } },
    y: {
      grid: { color: "#e8edf3" },
      title: { display: !!yLabel, text: yLabel },
      suggestedMin,
      suggestedMax,
    },
  },
});

// ── Shared label buffer (timestamps) ─────────────────────────────────────
const labels = [];

function pushLabel(ts) {
  const d = ts ? new Date(ts) : new Date();
  const formatted = d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  labels.push(formatted);
  if (labels.length > MAX_POINTS) labels.shift();
}

function makeDataset(label, color, data) {
  return {
    label,
    data,
    borderColor: color,
    backgroundColor: color + "22",
    borderWidth: 1.5,
    pointRadius: 0,
    tension: 0.3,
    fill: false,
  };
}

function makeBuffer() {
  return [];
}

function pushVal(buf, val) {
  buf.push(val !== null && val !== undefined ? val : NaN);
  if (buf.length > MAX_POINTS) buf.shift();
}

// ── Data buffers ──────────────────────────────────────────────────────────
const buf = {
  hr: makeBuffer(),
  spo2: makeBuffer(),
  temp: makeBuffer(),
  hum: makeBuffer(),
  pres: makeBuffer(),
  gsr: makeBuffer(),
  ads: makeBuffer(),
};

// ── Chart: Heart Rate & SpO2 ─────────────────────────────────────────────
const chartHR = new Chart(document.getElementById("chart-hr"), {
  type: "line",
  data: {
    labels,
    datasets: [
      makeDataset("Heart Rate (BPM)", "#b71c1c", buf.hr),
      makeDataset("SpO\u2082 (%)", "#1a5fad", buf.spo2),
    ],
  },
  options: CHART_OPTIONS("", 40, 105),
});

// ── Chart: Temperature & Humidity ──────────────────────────────────────
const chartEnv = new Chart(document.getElementById("chart-env"), {
  type: "line",
  data: {
    labels,
    datasets: [
      makeDataset("Temperature (\u00b0C)", "#c25d00", buf.temp),
      makeDataset("Humidity (%RH)", "#1e7845", buf.hum),
    ],
  },
  options: CHART_OPTIONS("", 0, 100),
});

// ── Chart: Pressure ───────────────────────────────────────────────────────
const chartPres = new Chart(document.getElementById("chart-pres"), {
  type: "line",
  data: {
    labels,
    datasets: [makeDataset("Pressure (hPa)", "#5e35b1", buf.pres)],
  },
  options: CHART_OPTIONS("hPa", 950, 1060),
});

// ── Chart: GSR / EDA ──────────────────────────────────────────────────────
const chartGSR = new Chart(document.getElementById("chart-gsr"), {
  type: "line",
  data: {
    labels,
    datasets: [makeDataset("Conductance (µS)", "#0277bd", buf.gsr)],
  },
  options: CHART_OPTIONS("µS", 0, 50),
});

// ── Metric card helpers ───────────────────────────────────────────────────
function setMetric(id, value, decimals = 1, valid = true) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent =
    value !== null && value !== undefined
      ? Number(value).toFixed(decimals)
      : "—";
  el.closest(".metric-card").classList.toggle("invalid", !valid);
}

function updateCards(d) {
  setMetric("val-hr", d.heart_rate_bpm, 0, d.hr_valid);
  setMetric("val-spo2", d.spo2_percent, 1, d.spo2_valid);
  setMetric("val-temp", d.temperature_celsius, 1, true);
  setMetric(
    "val-hum",
    d.humidity_percent,
    1,
    d.humidity_percent !== null && d.humidity_percent !== undefined,
  );
  setMetric("val-pres", d.pressure_hpa, 1, true);
  setMetric("val-gsr", d.gsr_conductance_us, 4, true);
  setMetric(
    "val-ads",
    d.ads1_ch0_V,
    4,
    d.ads1_ch0_V !== null && d.ads1_ch0_V !== undefined,
  );
}

// ── Alert banner ──────────────────────────────────────
const alertBanner = document.getElementById("alert-banner");
const alertText = document.getElementById("alert-text");

function updateAlert(d) {
  if (d.alert_active) {
    alertText.textContent =
      "PERINGATAN: " + (d.alert_reasons || "kondisi bahaya terdeteksi");
    alertBanner.classList.add("visible");
  } else {
    alertBanner.classList.remove("visible");
  }
}

// ── SSE connection ────────────────────────────────────────────────────────
const statusDot = document.getElementById("status-dot");
const lastUpdate = document.getElementById("last-update");
const footerTs = document.getElementById("footer-ts");

function connect() {
  const es = new EventSource("/stream");

  es.onopen = () => {
    statusDot.className = "live";
    console.info("[NEUROSENSE] SSE connected.");
  };

  es.onmessage = (event) => {
    let d;
    try {
      d = JSON.parse(event.data);
    } catch (_) {
      return;
    }

    // Update charts
    pushLabel(d.timestamp_utc);
    pushVal(buf.hr, d.heart_rate_bpm);
    pushVal(buf.spo2, d.spo2_percent);
    pushVal(buf.temp, d.temperature_celsius);
    pushVal(buf.hum, d.humidity_percent);
    pushVal(buf.pres, d.pressure_hpa);
    pushVal(buf.gsr, d.gsr_conductance_us);
    pushVal(buf.ads, d.ads1_ch0_V);

    chartHR.update();
    chartEnv.update();
    chartPres.update();
    chartGSR.update();

    // Update metric cards
    updateCards(d);
    updateAlert(d);

    // Sensor stale: grey out all metric cards when sensors stop updating
    document.querySelectorAll(".metric-card").forEach((card) => {
      card.classList.toggle("stale", !!d.sensor_stale);
    });

    // Disk space warning (threshold: < 2 GB)
    const diskWarn = document.getElementById("disk-warning");
    const diskText = document.getElementById("disk-warning-text");
    if (diskWarn && d.disk_free_gb !== null && d.disk_free_gb !== undefined) {
      if (d.disk_free_gb < 2) {
        diskText.textContent = `Disk space low — ${d.disk_free_gb} GB remaining. Recording may fail soon.`;
        diskWarn.classList.add("visible");
      } else {
        diskWarn.classList.remove("visible");
      }
    }

    // Update camera FPS badge
    const fpsBadge = document.getElementById("camera-fps");
    if (fpsBadge) {
      fpsBadge.textContent =
        d.camera_fps !== null && d.camera_fps !== undefined
          ? d.camera_fps.toFixed(1) + " fps"
          : "— fps";
    }

    // Update timestamps
    const now = new Date().toLocaleTimeString();
    lastUpdate.textContent = `Last update: ${now}`;
    footerTs.textContent = d.timestamp_utc || now;
  };

  es.onerror = () => {
    statusDot.className = "error";
    lastUpdate.textContent = "Connection lost — reconnecting…";
    es.close();
    setTimeout(connect, RECONNECT_MS);
  };
}

connect();

// ── Camera status ─────────────────────────────────────────────────────────
const camStatus = document.getElementById("camera-status");
const camSection = document.getElementById("camera-section");

function onCameraLoad() {
  if (camStatus) {
    camStatus.textContent = "Live";
    camStatus.className = "";
  }
}

function onCameraError() {
  if (camStatus) {
    camStatus.textContent = "Camera not available";
    camStatus.className = "error";
  }
  // Hide section if camera endpoint returns error (503)
  const feed = document.getElementById("camera-feed");
  if (feed) feed.style.display = "none";
  if (camSection) camSection.style.display = "none";
}

// ── GSR Recalibration ─────────────────────────────────────────────────────
async function recalibrateGSR() {
  const btn = document.getElementById("btn-recal");
  const status = document.getElementById("recal-status");

  btn.disabled = true;
  status.className = "busy";

  // Countdown: ask user to release sensor
  const messages = [
    "Lepas sensor… 3",
    "Lepas sensor… 2",
    "Lepas sensor… 1",
    "Mengambil baseline…",
  ];
  for (const msg of messages) {
    status.textContent = msg;
    await new Promise((r) => setTimeout(r, 1000));
  }

  try {
    const res = await fetch("/recalibrate/gsr", { method: "POST" });
    const data = await res.json();
    if (data.status === "ok") {
      const b10 = data.baseline_10bit ?? "?";
      const mx = data.max_conductance_us ?? "?";
      status.className = "ok";
      status.textContent = `✓ Baseline ${b10} (max ≈ ${mx} µS)`;
    } else {
      status.className = "err";
      status.textContent = "✗ " + (data.message || "error");
    }
  } catch (e) {
    status.className = "err";
    status.textContent = "✗ Koneksi gagal";
  }

  btn.disabled = false;
  // Clear status after 6 s
  setTimeout(() => {
    status.textContent = "";
    status.className = "";
  }, 6000);
}
