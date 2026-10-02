"use strict";

const byId = (id) => document.getElementById(id);
const form = byId("filters");
const coverageFields = [
  ["Known round start", "rows_with_known_start"],
  ["Pre-round observation time", "rows_with_pre_observation"],
  ["Pre-round data", "rows_with_pre_data"],
  ["Post-round data", "rows_with_post_data"],
  ["Raw data", "rows_with_raw_data"],
];
const errors = {
  INVALID_FILTERS: "These filters are invalid. Check the source and UTC dates; the start must precede the end.",
  DATABASE_NOT_READY: "The database is not ready. Run python main.py in the project terminal, then refresh.",
  DATABASE_UNAVAILABLE: "The database could not be read. Check the application configuration and file permissions, then refresh.",
  ANALYSIS_UNAVAILABLE: "This selection cannot be analyzed. It may exceed 100,000 rounds or contain invalid stored data. Narrow the filters, or check the records using the command-line tools.",
  ANALYSIS_BUSY: "The dashboard is processing other requests. Try refreshing shortly.",
};
let currentReport = null;
let activeFilters = new URLSearchParams();
let requestController = null;
let requestNumber = 0;

function text(id, value) { byId(id).textContent = value; }
function number(value) { return value.toLocaleString("en-US"); }
function multiplier(value) { return value === null ? "—" : `${value}×`; }
function utc(value) {
  if (value === null) return "—";
  return value.replace("T", " ").replace(/\.000000/, "").replace(/(?:Z|\+00:00)$/, " UTC");
}
function progress(value, maximum, label) {
  const element = document.createElement("progress");
  element.max = maximum || 1;
  element.value = value;
  element.setAttribute("aria-label", label);
  return element;
}

function renderCoverage(quality, selected) {
  const container = byId("coverage");
  container.replaceChildren();
  for (const [label, key] of coverageFields) {
    const count = quality === null ? 0 : quality[key];
    const row = document.createElement("div");
    row.className = "coverage-row";
    const heading = document.createElement("div");
    const name = document.createElement("span");
    name.textContent = label;
    const amount = document.createElement("strong");
    amount.textContent = quality === null ? "—" : `${number(count)} / ${number(selected)}`;
    heading.append(name, amount);
    row.append(heading, progress(count, selected, `${label}: ${amount.textContent}`));
    container.append(row);
  }
}

function clearReport() {
  currentReport = null;
  byId("download").disabled = true;
  for (const id of ["selected-rounds", "mean", "median", "maximum", "delay", "repeated", "first", "last", "source-count"]) text(id, "—");
  text("stored-rounds", "No report available");
  text("minimum", "Lowest multiplier: —");
  text("updated", "—");
  byId("buckets").replaceChildren();
  byId("buckets").hidden = true;
  byId("distribution-empty").hidden = false;
  byId("distribution-empty").querySelector("h3").textContent = "No report available";
  byId("distribution-empty").querySelector("p").textContent = "The distribution will appear when a report is available.";
  renderCoverage(null, 0);
}

function renderMetadata(metadata) {
  text("sidebar-schema", metadata.schema_version);
  text("environment", metadata.environment);
  text("database-state", "Ready · read only");
  byId("database-state").className = "";
  const options = byId("source-options");
  options.replaceChildren();
  for (const source of metadata.sources) {
    const option = document.createElement("option");
    option.value = source;
    options.append(option);
  }
  byId("suggestions-note").hidden = !metadata.source_suggestions_truncated;
}

function renderReport(report) {
  currentReport = report;
  const total = report.selected_rounds;
  text("selected-rounds", number(total));
  text("stored-rounds", `${number(report.total_stored)} rounds in the database`);
  text("mean", multiplier(report.multipliers.mean));
  text("median", multiplier(report.multipliers.median));
  text("maximum", multiplier(report.multipliers.maximum));
  text("minimum", `Lowest multiplier: ${multiplier(report.multipliers.minimum)}`);
  text("first", utc(report.first_result_at));
  text("last", utc(report.last_result_at));
  text("source-count", number(report.source_count));
  const delay = report.quality.collection_delay_seconds.mean;
  text("delay", delay === null ? "—" : `${delay} s`);
  text("repeated", number(report.quality.repeated_result_timestamps));
  text("selection", total ? `${number(total)} completed rounds selected · ${number(report.source_count)} sources` : (report.total_stored ? "No completed rounds match these filters." : "Your database is ready. No rounds have been imported yet."));
  text("updated", `Report generated: ${utc(report.generated_at)}`);
  const buckets = byId("buckets");
  buckets.replaceChildren();
  for (const bucket of report.buckets) {
    const label = bucket.upper_exclusive === null ? `${bucket.lower_inclusive}× and above` : `${bucket.lower_inclusive}× to < ${bucket.upper_exclusive}×`;
    const row = document.createElement("div");
    row.className = "bucket";
    const heading = document.createElement("div");
    heading.className = "bucket-heading";
    const name = document.createElement("span");
    name.textContent = label;
    const amount = document.createElement("span");
    amount.className = "bucket-count";
    amount.append(`${number(bucket.count)} rounds`);
    const percentage = document.createElement("strong");
    percentage.textContent = bucket.percentage === null ? "—" : `${bucket.percentage}%`;
    amount.append(percentage);
    heading.append(name, amount);
    row.append(heading, progress(Number(bucket.percentage || 0), 100, `${label}: ${percentage.textContent}`));
    buckets.append(row);
  }
  buckets.hidden = total === 0;
  byId("distribution-empty").hidden = total !== 0;
  byId("distribution-empty").querySelector("h3").textContent = report.total_stored ? "No matching rounds" : "No rounds to display";
  byId("distribution-empty").querySelector("p").textContent = report.total_stored ? "Adjust the source or UTC date filters to explore stored rounds." : "Import completed rounds to explore their historical distribution.";
  renderCoverage(report.quality, total);
  byId("download").disabled = false;
}

async function getJSON(url, signal) {
  const response = await fetch(url, {signal, cache: "no-store", credentials: "same-origin"});
  const document = await response.json();
  if (!response.ok) throw new Error(errors[document.error] || "The dashboard request failed. Check the terminal and refresh.");
  return document;
}

async function loadReport() {
  if (requestController) requestController.abort();
  requestController = new AbortController();
  const signal = requestController.signal;
  const sequence = ++requestNumber;
  byId("error").hidden = true;
  clearReport();
  text("selection", "Reading the selected historical records…");
  byId("refresh").disabled = true;
  try {
    const query = activeFilters.toString();
    const [metadata, report] = await Promise.all([
      getJSON("/api/status", signal), getJSON(`/api/summary${query ? `?${query}` : ""}`, signal),
    ]);
    if (sequence !== requestNumber) return;
    renderMetadata(metadata);
    renderReport(report);
  } catch (error) {
    if (sequence !== requestNumber || error.name === "AbortError") return;
    text("error", error instanceof TypeError ? "The dashboard could not be reached. Check that python run_dashboard.py is still running, then refresh." : error.message);
    byId("error").hidden = false;
    text("selection", "No report available. Resolve the error and try again.");
    text("database-state", "Report unavailable");
    byId("database-state").className = "status-neutral";
    text("sidebar-schema", "—");
    text("environment", "—");
    byId("source-options").replaceChildren();
    byId("suggestions-note").hidden = true;
  } finally {
    if (sequence === requestNumber) byId("refresh").disabled = false;
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  activeFilters = new URLSearchParams();
  const source = byId("source").value.trim();
  if (source) activeFilters.set("source", source);
  for (const key of ["start", "end"]) {
    // datetime-local has no timezone: these explicitly labeled controls represent UTC.
    if (byId(key).value) activeFilters.set(key, `${byId(key).value}Z`);
  }
  loadReport();
});
byId("reset-filters").addEventListener("click", () => { form.reset(); activeFilters = new URLSearchParams(); loadReport(); });
byId("refresh").addEventListener("click", loadReport);
byId("download").addEventListener("click", () => {
  if (currentReport === null) return;
  const file = new Blob([JSON.stringify(currentReport, null, 2) + "\n"], {type: "application/json"});
  const url = URL.createObjectURL(file);
  const link = document.createElement("a");
  link.href = url;
  link.download = "aie-analysis.json";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

renderCoverage(null, 0);
loadReport();
