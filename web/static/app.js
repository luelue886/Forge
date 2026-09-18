"use strict";

const TYPE_NAMES = {
  cover: "封面", toc: "目录", section_header: "章节头", text_points: "要点页",
  two_column: "双栏页", table: "表格页", key_metrics: "指标页", closing: "结束页",
};
const TERMINAL = new Set(["DONE", "FAILED"]);

function $(id) { return document.getElementById(id); }

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------- 首页 ---------- */

function initIndexPage() {
  const form = $("upload-form");
  if (!form) return;
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = $("upload-btn"), msg = $("upload-msg");
    const file = $("file").files[0];
    if (!file) return;
    btn.disabled = true;
    msg.textContent = "上传解析中…";
    try {
      const body = new FormData();
      body.append("file", file);
      body.append("skin", $("skin").value);
      const res = await fetch("/api/jobs", { method: "POST", body });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      location.href = `/jobs/${data.job_id}`;
    } catch (e) {
      msg.textContent = `失败：${e.message}`;
      btn.disabled = false;
    }
  });
}

/* ---------- 任务页 ---------- */

function renderOutline(outline) {
  const body = $("outline-body");
  body.innerHTML = (outline || []).map(p =>
    `<tr><td>${p.page_no}</td><td>${esc(TYPE_NAMES[p.type] || p.type)}</td><td>${esc(p.title)}</td></tr>`
  ).join("");
}

function renderPages(jobId, names) {
  $("pages-grid").innerHTML = (names || []).map(n =>
    `<figure><img src="/jobs/${encodeURIComponent(jobId)}/pages/${n}" alt="${n}" loading="lazy"><figcaption>${n}</figcaption></figure>`
  ).join("");
}

async function pollJob(jobId, skinNames = {}) {
  const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
  if (!res.ok) { $("status-text").textContent = "查询失败"; return; }
  const d = await res.json();

  $("status-text").textContent = d.status;
  $("status-text").className = `status st-large st-${d.status}`;
  $("status-detail").textContent = d.detail || "";
  $("job-title").textContent = `${d.source_name} · ${jobId}`;
  $("skin-text").textContent = skinNames[d.skin] || d.skin || "—";

  $("confirm-area").classList.toggle("hidden", d.status !== "PLANNED");
  if (d.status === "PLANNED") renderOutline(d.outline);

  $("error-area").classList.toggle("hidden", d.status !== "FAILED");
  if (d.status === "FAILED") $("error-text").textContent = d.error || "未知错误";

  $("done-area").classList.toggle("hidden", d.status !== "DONE");
  if (d.status === "DONE") $("download-link").href = `/jobs/${encodeURIComponent(jobId)}/download`;

  if ((d.pages_png || []).length) renderPages(jobId, d.pages_png);

  if (TERMINAL.has(d.status)) return;
  setTimeout(() => pollJob(jobId, skinNames), 1000);
}

function initJobPage(jobId, skinNames = {}) {
  pollJob(jobId, skinNames);
  $("confirm-btn").addEventListener("click", async () => {
    $("confirm-btn").disabled = true;
    $("confirm-msg").textContent = "已提交确认…";
    try {
      const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/confirm`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      $("confirm-msg").textContent = "";
      pollJob(jobId, skinNames);
    } catch (e) {
      $("confirm-msg").textContent = `失败：${e.message}`;
      $("confirm-btn").disabled = false;
    }
  });
}

document.addEventListener("DOMContentLoaded", initIndexPage);
