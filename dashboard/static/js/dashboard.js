/**
 * dashboard.js — NEUROSENSE real-time dashboard
 * Consumes SSE stream from /stream and updates Chart.js charts + metric cards.
 */

"use strict";

// ── Configuration ─────────────────────────────────────────────────────────
const MAX_POINTS = 60; // rolling window (60 data points ≈ 60 s at 1 Hz)
const RECONNECT_MS = 3000; // reconnect delay after SSE error

// ── Chart defaults ────────────────────────────────────────────────────────
Chart.defaults.color = "#8b949e";
Chart.defaults.borderColor = "#30363d";
Chart.defaults.font.family =
  '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
Chart.defaults.font.size = 11;

const CHART_OPTIONS = (yLabel, suggestedMin, suggestedMax) => ({
  animation: false,
  responsive: true,
  interaction: { mode: "index", intersect: false },
  plugins: {
    legend: { labels: { boxWidth: 12, padding: 14 } },
    tooltip: {
      backgroundColor: "#161b22",
      borderColor: "#30363d",
      borderWidth: 1,
    },
  },
  scales: {
    x: { grid: { color: "#21262d" }, ticks: { maxTicksLimit: 8 } },
    y: {
      grid: { color: "#21262d" },
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
};

// ── Chart: Heart Rate & SpO2 ─────────────────────────────────────────────
const chartHR = new Chart(document.getElementById("chart-hr"), {
  type: "line",
  data: {
    labels,
    datasets: [
      makeDataset("Heart Rate (BPM)", "#f85149", buf.hr),
      makeDataset("SpO₂ (%)", "#58a6ff", buf.spo2),
    ],
  },
  options: CHART_OPTIONS("", 40, 105),
});

// ── Chart: Temperature & Humidity ────────────────────────────────────────
const chartEnv = new Chart(document.getElementById("chart-env"), {
  type: "line",
  data: {
    labels,
    datasets: [
      makeDataset("Temperature (°C)", "#d29922", buf.temp),
      makeDataset("Humidity (%RH)", "#3fb950", buf.hum),
    ],
  },
  options: CHART_OPTIONS("", 0, 100),
});

// ── Chart: Pressure ───────────────────────────────────────────────────────
const chartPres = new Chart(document.getElementById("chart-pres"), {
  type: "line",
  data: {
    labels,
    datasets: [makeDataset("Pressure (hPa)", "#bc8cff", buf.pres)],
  },
  options: CHART_OPTIONS("hPa", 950, 1060),
});

// ── Chart: GSR / EDA ──────────────────────────────────────────────────────
const chartGSR = new Chart(document.getElementById("chart-gsr"), {
  type: "line",
  data: {
    labels,
    datasets: [makeDataset("Conductance (µS)", "#79c0ff", buf.gsr)],
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
  setMetric("val-hum", d.humidity_percent, 1, true);
  setMetric("val-pres", d.pressure_hpa, 1, true);
  setMetric("val-gsr", d.gsr_conductance_us, 4, true);
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

    chartHR.update();
    chartEnv.update();
    chartPres.update();
    chartGSR.update();

    // Update metric cards
    updateCards(d);
    updateAlert(d);

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
