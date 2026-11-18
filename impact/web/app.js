"use strict";

const $ = (s) => document.querySelector(s);
let selectedBatch = null;
let lastBatchStatus = {};
let pkgPolling = false;

const esc = (s) => String(s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtA = (c) => {
  const neg = c < 0;
  const v = Math.abs(c) / 100;
  return (neg ? "-¥" : "¥") + v.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};
const fmtDeltaA = (v) => (v > 0 ? "+" : "") + fmtA(v);
const fmtDeltaQ = (v) => (v > 0 ? "+" : "") + v.toLocaleString("zh-CN");
const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "zero");

async function api(path, opts) {
  const r = await fetch(path, opts);
  const t = await r.text();
  let d;
  try { d = JSON.parse(t); } catch { d = { error: t }; }
  if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
  return d;
}

function submitMsg(text, isErr) {
  const el = $("#submitMsg");
  el.textContent = text;
  el.className = isErr ? "neg" : "pos";
}

/* ---------------- ① submit a revision batch ---------------- */

async function fillExample() {
  try {
    const d = await api("/api/sample-details?limit=50");
    const det = d.details;
    if (det.length < 3) { submitMsg("样本明细不足", true); return; }
    const idx = [...det.keys()].sort(() => Math.random() - 0.5).slice(0, 3);
    const [a, b, c] = idx.map((i) => det[i]);
    const regions = ["North", "South", "East", "West", "Central", "Lakes"];
    const newRegion = regions.find((r) => r !== b.region) || "North";
    const example = {
      changes: [
        { detail_id: a.detail_id, amount: a.amount + 10000, qty: a.qty + 2 },
        { detail_id: b.detail_id, region: newRegion },
        { detail_id: c.detail_id, delete: true },
        { detail_id: "DX" + String(Date.now()).slice(-6), region: "East", product: "P-03", channel: "online", amount: 88000, qty: 22 },
      ],
    };
    $("#changesJson").value = JSON.stringify(example, null, 2);
    submitMsg("已填入示例（基于当前真实明细 ID），可直接提交。", false);
  } catch (e) {
    submitMsg("获取示例失败: " + e.message, true);
  }
}

async function submitBatch() {
  let payload;
  try {
    payload = JSON.parse($("#changesJson").value);
  } catch (e) {
    submitMsg("JSON 解析失败: " + e.message, true);
    return;
  }
  try {
    const r = await api("/api/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    submitMsg(`已接收 ${r.batch_id}（输入边界 v${r.boundary_version}，${r.n_changes} 条变更）`, false);
    await refreshBatches();
    selectBatch(r.batch_id);
  } catch (e) {
    submitMsg("提交失败: " + e.message, true);
  }
}

/* ---------------- ② batch list ---------------- */

const STATUS_LABEL = {
  received: "已接收", computing: "重算中", computed: "已重算", failed: "重算失败",
};
const PKG_LABEL = {
  idle: "未构建", building: "构建中", cancelled: "已取消", done: "已发布", failed: "构建失败",
};

async function refreshBatches() {
  try {
    const d = await api("/api/batches");
    const tb = document.querySelector("#batchTable tbody");
    tb.innerHTML = "";
    for (const b of d.batches) {
      const tr = document.createElement("tr");
      if (b.batch_id === selectedBatch) tr.className = "selected";
      tr.onclick = () => selectBatch(b.batch_id);
      const pkg = b.pkg_status ? PKG_LABEL[b.pkg_status] || b.pkg_status : "未构建";
      tr.innerHTML =
        `<td class="mono">${esc(b.batch_id)}</td>` +
        `<td>v${b.boundary_version}</td>` +
        `<td>${b.n_changes}</td>` +
        `<td><span class="badge s-${b.status}">${STATUS_LABEL[b.status] || b.status}</span></td>` +
        `<td><span class="badge p-${b.pkg_status || "idle"}">${pkg}</span></td>` +
        `<td class="hint">${esc((b.created_at || "").replace("T", " ").slice(0, 19))}</td>`;
      tb.appendChild(tr);
      const prev = lastBatchStatus[b.batch_id];
      if (prev && prev !== b.status && b.status === "computed" && b.batch_id === selectedBatch) {
        loadImpact(b.batch_id);
      }
      lastBatchStatus[b.batch_id] = b.status;
    }
  } catch (e) { /* keep polling quietly */ }
}

/* ---------------- ③ batch detail: impact + package ---------------- */

async function selectBatch(bid) {
  selectedBatch = bid;
  $("#detailCard").style.display = "";
  $("#detailTitle").textContent = bid;
  await Promise.all([loadImpact(bid), refreshPkg()]);
  await refreshBatches();
}

async function loadImpact(bid) {
  const host = $("#impactView");
  try {
    const data = await api(`/api/batches/${bid}/impact`);
    if (data.batch.status !== "computed") {
      host.innerHTML = `<p class="hint">依赖重算进行中（状态：${STATUS_LABEL[data.batch.status] || data.batch.status}）…</p>`;
      return;
    }
    renderImpact(data);
  } catch (e) {
    host.innerHTML = `<p class="neg">加载影响数据失败: ${esc(e.message)}</p>`;
  }
}

function renderImpact(data) {
  const host = $("#impactView");
  let html = `<p class="hint">输入边界：明细版本 ≤ v${data.batch.boundary_version}（本批全部报表共用）</p>`;
  for (const rid of Object.keys(data.reports)) {
    const meta = data.reports[rid];
    const rep = data.impact[rid];
    html += `<h3>${esc(meta.title)} <span class="dim">(${meta.dimensions.join(" × ")})</span></h3>`;
    if (rep.total) {
      const t = rep.total;
      html += `<div class="total">报表合计：金额 ${fmtA(t.old.amount)} → ${fmtA(t.new.amount)} ` +
        `（<span class="${cls(t.delta.amount)}">${fmtDeltaA(t.delta.amount)}</span>），` +
        `数量 ${t.old.qty.toLocaleString("zh-CN")} → ${t.new.qty.toLocaleString("zh-CN")} ` +
        `（<span class="${cls(t.delta.qty)}">${fmtDeltaQ(t.delta.qty)}</span>）</div>`;
    }
    if (!rep.rows.length) {
      html += `<p class="hint">本批未影响该报表的任何聚合行。</p>`;
      continue;
    }
    html += `<table class="impact"><thead><tr>` +
      `<th>维度键</th><th>原值金额</th><th>新值金额</th><th>Δ金额</th>` +
      `<th>原值数量</th><th>新值数量</th><th>Δ数量</th><th></th></tr></thead><tbody>`;
    rep.rows.forEach((row, i) => {
      const key = meta.dimensions.map((d) => row.key[d]).join(" / ");
      const cid = `${rid}-${i}`;
      html += `<tr>` +
        `<td>${esc(key)}</td>` +
        `<td>${fmtA(row.old.amount)}</td><td>${fmtA(row.new.amount)}</td>` +
        `<td class="${cls(row.delta.amount)}">${fmtDeltaA(row.delta.amount)}</td>` +
        `<td>${row.old.qty.toLocaleString("zh-CN")}</td><td>${row.new.qty.toLocaleString("zh-CN")}</td>` +
        `<td class="${cls(row.delta.qty)}">${fmtDeltaQ(row.delta.qty)}</td>` +
        `<td><button class="link" onclick="toggleContribs('${cid}')">展开差额</button></td></tr>`;
      html += `<tr class="contribs" id="cb-${cid}" style="display:none"><td colspan="8">` +
        `<table class="sub"><thead><tr><th>明细</th><th>原金额</th><th>新金额</th><th>Δ金额</th>` +
        `<th>原数量</th><th>新数量</th><th>Δ数量</th></tr></thead><tbody>`;
      for (const c of row.contributions) {
        const oa = c.old ? c.old.amount : 0, na = c.new ? c.new.amount : 0;
        const oq = c.old ? c.old.qty : 0, nq = c.new ? c.new.qty : 0;
        html += `<tr><td class="mono">${esc(c.detail_id)}</td>` +
          `<td>${c.old ? fmtA(oa) : "—"}</td><td>${c.new ? fmtA(na) : "—"}</td>` +
          `<td class="${cls(na - oa)}">${fmtDeltaA(na - oa)}</td>` +
          `<td>${c.old ? oq : "—"}</td><td>${c.new ? nq : "—"}</td>` +
          `<td class="${cls(nq - oq)}">${fmtDeltaQ(nq - oq)}</td></tr>`;
      }
      html += `</tbody></table></td></tr>`;
    });
    html += `</tbody></table>`;
  }
  host.innerHTML = html;
}

window.toggleContribs = (id) => {
  const el = document.getElementById("cb-" + id);
  el.style.display = el.style.display === "none" ? "" : "none";
};

/* ---------------- package panel ---------------- */

async function refreshPkg() {
  if (!selectedBatch) return;
  try {
    const st = await api(`/api/packages/${selectedBatch}/status`);
    renderPkg(st);
    const active = st.latest_task && ["pending", "running"].includes(st.latest_task.status);
    if (active && !pkgPolling) {
      pkgPolling = true;
      setTimeout(async () => { pkgPolling = false; await refreshPkg(); }, 900);
    }
  } catch (e) { /* ignore transient errors */ }
}

function renderPkg(st) {
  const pkg = st.package;
  const task = st.latest_task;
  const active = task && ["pending", "running"].includes(task.status);
  const cancelledOrFailed = task && ["cancelled", "failed"].includes(task.status);

  $("#buildBtn").style.display = !active && !cancelledOrFailed ? "" : "none";
  $("#cancelBtn").style.display = active ? "" : "none";
  $("#resumeBtn").style.display = !active && cancelledOrFailed ? "" : "none";
  $("#downloadBtn").style.display = pkg && pkg.status === "done" ? "" : "none";
  if (pkg && pkg.status === "done") {
    $("#downloadBtn").href = `/api/packages/${selectedBatch}/download`;
  }

  let info = "";
  if (!pkg) {
    info = "尚未构建。";
  } else {
    info = `状态：${PKG_LABEL[pkg.status] || pkg.status} · 当前代际 g${pkg.generation}`;
    if (pkg.published_generation) {
      info += ` · 已发布 g${pkg.published_generation}`;
    }
    if (pkg.artifact_sha256) {
      info += ` · sha256 <span class="mono">${pkg.artifact_sha256.slice(0, 16)}…</span>`;
    }
  }
  if (task && active) {
    info += ` · 任务#${task.task_id} 进度 ${task.done_steps}/${task.total_steps}`;
  }
  if (task && task.status === "superseded") {
    info += ` · 任务#${task.task_id} 已被更新构建取代（未发布）`;
  }
  if (task && task.status === "failed" && task.error) {
    info += ` · <span class="neg">${esc(task.error)}</span>`;
  }
  if (st.checkpoints > 0) {
    info += ` · 已存断点 ${st.checkpoints} 个（恢复时复用）`;
  }
  $("#pkgStatus").innerHTML = info;

  const pct = task && task.total_steps ? Math.round(100 * task.done_steps / task.total_steps) : 0;
  $("#pkgBar").style.width = pct + "%";
}

async function buildPackage() {
  try {
    await api(`/api/packages/${selectedBatch}/build`, { method: "POST" });
    await refreshPkg();
  } catch (e) {
    $("#pkgStatus").innerHTML = `<span class="neg">构建请求失败: ${esc(e.message)}</span>`;
  }
}

async function cancelPackage() {
  try {
    await api(`/api/packages/${selectedBatch}/cancel`, { method: "POST" });
    await refreshPkg();
  } catch (e) {
    $("#pkgStatus").innerHTML = `<span class="neg">取消失败: ${esc(e.message)}</span>`;
  }
}

/* ---------------- boot ---------------- */

$("#fillExample").onclick = fillExample;
$("#submitBtn").onclick = submitBatch;
$("#buildBtn").onclick = buildPackage;
$("#cancelBtn").onclick = cancelPackage;
$("#resumeBtn").onclick = buildPackage;

refreshBatches();
setInterval(refreshBatches, 2000);
