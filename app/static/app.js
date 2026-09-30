"use strict";

const MAX_STATIONS = 12;
const MAX_PIPES = 36;

const stationBody = document.querySelector("#stationTable tbody");
const pipeBody = document.querySelector("#pipeTable tbody");
const counter = document.querySelector("#counter");
const requestPreview = document.querySelector("#requestPreview");
const resultCard = document.querySelector("#resultCard");
const statusBanner = document.querySelector("#statusBanner");
const resultBody = document.querySelector("#resultBody");

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c != null) node.appendChild(c);
  return node;
}

// ------------------------------------------------------------ 动态行
function addStationRow(id = "", balance = "") {
  if (stationBody.rows.length >= MAX_STATIONS) return;
  const tr = el("tr");
  const idInput = el("input", { type: "text", maxlength: "32", value: id });
  const balInput = el("input", { type: "text", inputmode: "integer", value: balance });
  tr.appendChild(el("td", {}, idInput));
  tr.appendChild(el("td", {}, balInput));
  tr.appendChild(el("td", {}, el("button", {
    type: "button", text: "删除",
    onclick: () => { tr.remove(); renderCounter(); },
  })));
  stationBody.appendChild(tr);
  renderCounter();
}

function stationIds() {
  return [...stationBody.rows].map(row =>
    row.querySelector("input").value.trim());
}

function addPipeRow(p = {}) {
  if (pipeBody.rows.length >= MAX_PIPES) return;
  const tr = el("tr");
  const idIn = el("input", { type: "text", maxlength: "32", value: p.id || "" });
  const fromSel = el("select");
  const toSel = el("select");
  const loIn = el("input", { type: "text", inputmode: "integer", value: p.lo ?? "" });
  const hiIn = el("input", { type: "text", inputmode: "integer", value: p.hi ?? "" });
  const costIn = el("input", { type: "text", inputmode: "integer",
                               value: p.cost ?? "" });
  const refresh = () => {
    const ids = stationIds();
    for (const [sel, cur] of [[fromSel, p.from], [toSel, p.to]]) {
      const old = sel.value || cur;
      sel.innerHTML = "";
      sel.appendChild(el("option", { value: "", text: "选择…" }));
      for (const sid of ids) sel.appendChild(el("option", { value: sid, text: sid }));
      if (old && ids.includes(old)) sel.value = old;
    }
  };
  stationBody.addEventListener("change", refresh);
  stationBody.addEventListener("input", refresh);
  tr.appendChild(el("td", {}, idIn));
  tr.appendChild(el("td", {}, fromSel));
  tr.appendChild(el("td", {}, toSel));
  tr.appendChild(el("td", {}, loIn));
  tr.appendChild(el("td", {}, hiIn));
  tr.appendChild(el("td", {}, costIn));
  tr.appendChild(el("td", {}, el("button", {
    type: "button", text: "删除",
    onclick: () => { tr.remove(); renderCounter(); },
  })));
  pipeBody.appendChild(tr);
  refresh();
  renderCounter();
}

function renderCounter() {
  counter.textContent =
    `站点 ${stationBody.rows.length}/${MAX_STATIONS} · 管路 ${pipeBody.rows.length}/${MAX_PIPES}`;
}

// ------------------------------------------------------------ 构造请求
function safeInt(v, loc) {
  const t = String(v ?? "").trim();
  if (!/^[+-]?\d+$/.test(t)) throw { loc, message: "必须是整数" };
  return Number(t);
}

function buildPayload() {
  const stations = [];
  const ids = new Set();
  [...stationBody.rows].forEach((row, i) => {
    const inputs = row.querySelectorAll("input");
    const sid = inputs[0].value.trim();
    if (!sid) throw { loc: `stations[${i}].id`, message: "站点标识不能为空" };
    if (ids.has(sid)) throw { loc: `stations[${i}].id`, message: `站点重复: ${sid}` };
    ids.add(sid);
    const balance = safeInt(inputs[1].value, `stations[${i}].balance`);
    stations.push({ id: sid, balance });
  });

  const pipes = [];
  const eids = new Set();
  [...pipeBody.rows].forEach((row, i) => {
    const inputs = row.querySelectorAll("input");
    const selects = row.querySelectorAll("select");
    const eid = inputs[0].value.trim() || `e${i}`;
    if (eids.has(eid)) throw { loc: `pipes[${i}].id`, message: `管路标识重复: ${eid}` };
    eids.add(eid);
    if (!selects[0].value) throw { loc: `pipes[${i}].from`, message: "请选择起点" };
    if (!selects[1].value) throw { loc: `pipes[${i}].to`, message: "请选择终点" };
    if (selects[0].value === selects[1].value)
      throw { loc: `pipes[${i}]`, message: "起点终点不能相同" };
    const lo = safeInt(inputs[1].value, `pipes[${i}].lo`);
    const hi = safeInt(inputs[2].value, `pipes[${i}].hi`);
    const cost = safeInt(inputs[3].value, `pipes[${i}].cost`);
    pipes.push({ id: eid, from: selects[0].value, to: selects[1].value,
                 lo, hi, cost });
  });
  return { stations, pipes };
}

// ------------------------------------------------------------ 渲染结果
function rcClass(rc) { return rc < 0 ? "rc-neg" : rc === 0 ? "rc-zero" : "rc-pos"; }

function renderOptimal(r) {
  const boxes = el("div", { class: "kv" },
    el("div", { class: "box" },
      el("div", { class: "label", text: "总成本" }),
      el("div", { class: "value", text: r.total_cost })),
    el("div", { class: "box" },
      el("div", { class: "label", text: "消除的负环数" }),
      el("div", { class: "value", text: r.cycles_cancelled })),
    el("div", { class: "box" },
      el("div", { class: "label", text: "算法" }),
      el("div", { style: "font-size:12px", text: r.algorithm })),
  );
  resultBody.appendChild(boxes);

  const potRows = Object.entries(r.potentials)
    .map(([s, p]) => el("tr", {},
      el("td", { text: s }), el("td", { class: "mono", text: p })));
  resultBody.appendChild(el("h3", { text: "站点势 π（正残量边 c + π[u] − π[v] ≥ 0）" }));
  resultBody.appendChild(el("div", { class: "table-wrap" },
    el("table", { class: "result mono" },
      el("thead", {}, el("tr", {},
        el("th", { text: "站点" }), el("th", { text: "π" }))),
      el("tbody", {}, ...potRows))));

  const rows = r.edges.map(e => {
    const rcs = e.residual.map(x =>
      `${x.direction === "forward" ? "正向" : "反向"} 容${x.capacity} ` +
      `<span class="${rcClass(x.reduced_cost)}">c*=${x.reduced_cost}</span>`);
    return el("tr", {},
      el("td", { class: "mono", text: e.id }),
      el("td", { text: `${e.from} → ${e.to}` }),
      el("td", { class: "mono", text: `${e.flow} ∈ [${e.lo}, ${e.hi}]` }),
      el("td", { class: "mono", text: e.cost }),
      el("td", { class: "mono", text: e.cost_contribution }),
      el("td", { html: rcs.join("<br>") || "无正残量弧" }));
  });
  resultBody.appendChild(el("h3", { text: "逐边流量与残量约化成本（复算证据）" }));
  resultBody.appendChild(el("div", { class: "table-wrap" },
    el("table", { class: "result" },
      el("thead", {}, el("tr", {},
        el("th", { text: "管路" }), el("th", { text: "方向" }),
        el("th", { text: "流量（界）" }), el("th", { text: "单位成本" }),
        el("th", { text: "费用贡献" }), el("th", { text: "正残量弧约化成本" }))),
      el("tbody", {}, ...rows))));

  const nrows = r.node_check.map(n => el("tr", {},
    el("td", { text: n.station }),
    el("td", { class: "mono", text: n.inflow }),
    el("td", { class: "mono", text: n.outflow }),
    el("td", { class: "mono", text: n.net_out }),
    el("td", { class: "mono", text: n.balance }),
    el("td", { class: n.ok ? "ok mono" : "bad mono", text: n.ok ? "✓" : "✗" })));
  resultBody.appendChild(el("details", { open: "" },
    el("summary", { text: "逐站净供需复算（流出−流入 = balance？）" }),
    el("div", { class: "table-wrap" },
      el("table", { class: "result" },
        el("thead", {}, el("tr", {},
          el("th", { text: "站点" }), el("th", { text: "流入" }),
          el("th", { text: "流出" }),
          el("th", { text: "净流出" }), el("th", { text: "应等于" }),
          el("th", { text: "" }))),
        el("tbody", {}, ...nrows)))));
}

function renderInfeasible(r) {
  resultBody.appendChild(el("p", {}, el("b", { class: "bad", text: "无可行流。" }),
    el("span", { text: r.note })));
  const rows = r.unmet_demand.map(u => el("tr", {},
    el("td", { text: u.station }),
    el("td", { class: "mono", text: u.required }),
    el("td", { class: "mono bad", text: u.unsatisfied })));
  resultBody.appendChild(el("h3", { text: "未满足需求（超源出边残余）" }));
  resultBody.appendChild(el("div", { class: "table-wrap" },
    el("table", { class: "result" },
      el("thead", {}, el("tr", {},
        el("th", { text: "节点" }), el("th", { text: "需要量" }),
        el("th", { text: "未满足量" }))),
      el("tbody", {}, ...rows))));
  resultBody.appendChild(el("h3", { text: "残量可达站点（失败 cut 证据）" }));
  resultBody.appendChild(el("p", { class: "mono", text:
    r.reachable_from_source.join(", ") || "（空）" }));
}

function renderInvalid(e) {
  resultBody.appendChild(el("p", {},
    el("b", { class: "bad", text: "输入非法：" }),
    el("span", { class: "mono", text: ` ${e.loc} — ${e.error}` })));
}

async function submit() {
  let payload;
  try {
    payload = buildPayload();
  } catch (e) {
    showResult("invalid", { error: e.message, loc: e.loc });
    return;
  }
  requestPreview.textContent = JSON.stringify(payload, null, 2);
  const auditId = document.querySelector("#auditId").value.trim();
  try {
    const headers = { "Content-Type": "application/json" };
    if (auditId) headers["X-Audit-Id"] = auditId;
    const resp = await fetch("/api/solve", {
      method: "POST", headers, body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (resp.status === 400) {
      showResult("invalid", { error: data.error, loc: data.loc });
      return;
    }
    if (resp.status === 409) {
      showResult("invalid", {
        error: `${data.error}（已存指纹 ${
          (data.conflict?.payload_fingerprint || "").slice(0, 12)}…）`,
        loc: "X-Audit-Id",
      });
      return;
    }
    const r = data.result || data;
    showResult(r.status, r, data);
  } catch (e) {
    showResult("invalid", { error: `请求失败: ${e}`, loc: "$" });
  }
}

function showResult(kind, r, envelope) {
  resultCard.classList.remove("hidden");
  resultBody.innerHTML = "";
  statusBanner.innerHTML = "";
  if (kind === "optimal") {
    statusBanner.appendChild(el("div", { class: "banner optimal",
      text: `✓ 已取得最小费用可行流${envelope?.replayed ? "（审计重放原记录）" : ""}` }));
    renderOptimal(r);
  } else if (kind === "infeasible") {
    statusBanner.appendChild(el("div", { class: "banner infeasible",
      text: "✗ 网络无可行流（下列证据可复算定位瓶颈）" }));
    renderInfeasible(r);
  } else {
    statusBanner.appendChild(el("div", { class: "banner invalid",
      text: "⚠ 输入校验失败" }));
    renderInvalid(r);
  }
}

// ------------------------------------------------------------ 初始化
document.querySelector("#addStation").onclick = () => addStationRow();
document.querySelector("#addPipe").onclick = () => addPipeRow();
document.querySelector("#submitBtn").onclick = submit;
document.querySelector("#clearAll").onclick = () => {
  stationBody.innerHTML = "";
  pipeBody.innerHTML = "";
  renderCounter();
};

const SAMPLE = {
  stations: [
    { id: "S1", balance: 6 }, { id: "S2", balance: 4 },
    { id: "V1", balance: 0 }, { id: "V2", balance: 0 },
    { id: "D1", balance: -3 }, { id: "D2", balance: -7 },
  ],
  pipes: [
    { id: "p1", from: "S1", to: "V1", lo: 1, hi: 8, cost: 2 },
    { id: "p2", from: "S2", to: "V1", lo: 0, hi: 5, cost: 4 },
    { id: "p3", from: "S2", to: "V2", lo: 1, hi: 6, cost: 1 },
    { id: "p4", from: "V1", to: "D1", lo: 0, hi: 4, cost: 0 },
    { id: "p5", from: "V1", to: "D2", lo: 0, hi: 9, cost: 3 },
    { id: "p6", from: "V2", to: "D2", lo: 0, hi: 7, cost: 2 },
    { id: "p7", from: "D1", to: "D2", lo: 0, hi: 3, cost: -1 },
  ],
};
document.querySelector("#loadSample").onclick = () => {
  stationBody.innerHTML = "";
  pipeBody.innerHTML = "";
  for (const s of SAMPLE.stations) addStationRow(s.id, String(s.balance));
  for (const p of SAMPLE.pipes) addPipeRow(p);
};
for (const s of SAMPLE.stations) addStationRow(s.id, String(s.balance));
for (const p of SAMPLE.pipes) addPipeRow(p);
renderCounter();

// ============================================================
// 冻结嵌入核验：不可拆分批次 · 完整简单路径整数分配
// ============================================================
const MAX_BATCHES = 6;
const batchBody = document.querySelector("#batchTable tbody");
const batchCounter = document.querySelector("#batchCounter");
const embedPreview = document.querySelector("#embedPreview");
const embedStatusBanner = document.querySelector("#embedStatusBanner");
const embedBody = document.querySelector("#embedBody");
const embedAuditInput = document.querySelector("#embedAuditId");
const groupInput = document.querySelector("#groupId");
const FORM_KEY = "embedFormV1";

function renderBatchCounter() {
  batchCounter.textContent = `批次 ${batchBody.rows.length}/${MAX_BATCHES}`;
}

function addBatchRow(b = {}) {
  if (batchBody.rows.length >= MAX_BATCHES) return;
  const tr = el("tr");
  const idIn = el("input", { type: "text", maxlength: "32", value: b.id || "" });
  const fromSel = el("select");
  const toSel = el("select");
  const volIn = el("input", { type: "text", inputmode: "integer",
                              value: b.volume ?? "" });
  const avoidIn = el("input", { type: "text",
    value: b.avoid || b.avoidStr || "",
    placeholder: "如 p2,p5" });
  avoidIn.style.width = "200px";
  const refresh = () => {
    for (const [sel, cur] of [[fromSel, b.from], [toSel, b.to]]) {
      const old = sel.value || cur;
      sel.innerHTML = "";
      sel.appendChild(el("option", { value: "", text: "选择…" }));
      for (const sid of stationIds())
        sel.appendChild(el("option", { value: sid, text: sid }));
      if (old && stationIds().includes(old)) sel.value = old;
    }
  };
  stationBody.addEventListener("change", refresh);
  stationBody.addEventListener("input", refresh);
  tr.appendChild(el("td", {}, idIn));
  tr.appendChild(el("td", {}, fromSel));
  tr.appendChild(el("td", {}, toSel));
  tr.appendChild(el("td", {}, volIn));
  tr.appendChild(el("td", {}, avoidIn));
  tr.appendChild(el("td", {}, el("button", {
    type: "button", text: "删除",
    onclick: () => { tr.remove(); renderBatchCounter(); saveEmbedForm(); },
  })));
  batchBody.appendChild(tr);
  refresh();
  renderBatchCounter();
}

function collectForm() {
  const auditId = embedAuditInput.value.trim();
  const groupId = groupInput.value.trim();
  const batches = [];
  const bids = new Set();
  [...batchBody.rows].forEach((row, i) => {
    const inputs = row.querySelectorAll("input");
    const selects = row.querySelectorAll("select");
    const bid = inputs[0].value.trim();
    if (!bid) throw { loc: `batches[${i}].id`, message: "批次标识不能为空" };
    if (bids.has(bid)) throw { loc: `batches[${i}].id`,
                               message: `批次标识重复: ${bid}` };
    bids.add(bid);
    if (!selects[0].value) throw { loc: `batches[${i}].from`, message: "请选择来源站" };
    if (!selects[1].value) throw { loc: `batches[${i}].to`, message: "请选择目标站" };
    if (selects[0].value === selects[1].value)
      throw { loc: `batches[${i}]`, message: "来源站目标站不能相同" };
    const volume = safeInt(inputs[1].value, `batches[${i}].volume`);
    if (volume <= 0) throw { loc: `batches[${i}].volume`,
                             message: "体积必须为正整数" };
    const avoid = inputs[2].value.split(",").map(s => s.trim())
      .filter(Boolean);
    batches.push({ id: bid, from: selects[0].value, to: selects[1].value,
                   volume, avoid });
  });
  if (!batches.length) throw { loc: "batches", message: "至少添加一批" };
  return { auditId, groupId, batches };
}

function saveEmbedForm() {
  try {
    const prev = JSON.parse(localStorage.getItem(FORM_KEY) || "{}");
    // 仅当编组标识未变时保留“已确认”标记，便于刷新后重新核对
    const keepConfirmed = prev.groupId === groupInput.value.trim()
      ? prev.confirmedGroupId : undefined;
    const form = {
      auditId: embedAuditInput.value.trim(),
      groupId: groupInput.value.trim(),
      confirmedGroupId: keepConfirmed,
      batches: [...batchBody.rows].map(row => {
        const inputs = row.querySelectorAll("input");
        const selects = row.querySelectorAll("select");
        return { id: inputs[0].value, from: selects[0].value,
                 to: selects[1].value, volume: inputs[1].value,
                 avoid: inputs[2].value };
      }),
    };
    localStorage.setItem(FORM_KEY, JSON.stringify(form));
  } catch (e) { /* 存储不可用时静默 */ }
}

// ------------------------------------------------------------ 结果渲染
function renderEmbedded(r) {
  embedBody.appendChild(el("div", { class: "kv" },
    el("div", { class: "box" },
      el("div", { class: "label", text: "完整搜索节点数（穷尽回溯）" }),
      el("div", { class: "value", text: r.search?.nodes ?? "-" })),
    el("div", { class: "box" },
      el("div", { class: "label", text: "来源冻结审计" }),
      el("div", { class: "value mono", style: "font-size:15px",
                  text: r.frozen_audit_id }))));

  const brows = r.batches.map(b => el("tr", {},
    el("td", { class: "mono", text: b.id }),
    el("td", { text: `${b.from} → ${b.to}` }),
    el("td", { class: "mono", text: b.volume }),
    el("td", { class: "mono", text: (b.avoid || []).join(", ") || "—" }),
    el("td", { class: "mono", text: b.path.stations.join(" → ") }),
    el("td", { class: "mono", text: b.path.pipes.join(" → ") })));
  embedBody.appendChild(el("h3", { text: "按批次规范路径（不可拆分）" }));
  embedBody.appendChild(el("div", { class: "table-wrap" },
    el("table", { class: "result" },
      el("thead", {}, el("tr", {},
        el("th", { text: "批次" }), el("th", { text: "方向" }),
        el("th", { text: "体积" }), el("th", { text: "禁经" }),
        el("th", { text: "站点路径" }), el("th", { text: "管路路径" }))),
      el("tbody", {}, ...brows))));

  const prows = r.pipe_usage.map(p => el("tr", {},
    el("td", { class: "mono", text: p.pipe }),
    el("td", { text: `${p.from} → ${p.to}` }),
    el("td", { class: "mono", text: p.frozen_flow }),
    el("td", { class: "mono", text: p.used }),
    el("td", { class: p.remaining === 0 ? "bad mono" : "ok mono",
               text: p.remaining }),
    el("td", { class: "mono",
      text: p.by.map(x => `${x.batch}:${x.amount}`).join(", ") || "—" })));
  embedBody.appendChild(el("h3", { text: "逐管占用与剩余通量" }));
  embedBody.appendChild(el("div", { class: "table-wrap" },
    el("table", { class: "result" },
      el("thead", {}, el("tr", {},
        el("th", { text: "管路" }), el("th", { text: "方向" }),
        el("th", { text: "冻结流量" }), el("th", { text: "批次占用合计" }),
        el("th", { text: "剩余通量" }), el("th", { text: "占用明细" }))),
      el("tbody", {}, ...prows))));
}

function renderCannotEmbed(r) {
  embedBody.appendChild(el("p", { class: "hint" }, el("span", { text: r.note })));
  embedBody.appendChild(el("p", {},
    el("b", { text: "最先耗尽搜索层：" }),
    el("span", { class: "mono", text: String(r.critical_layer) })));

  const urows = (r.unplaced_batches || []).map(u => el("tr", {},
    el("td", { class: "mono", text: u.id }),
    el("td", { class: "mono", text: u.search_layer }),
    el("td", { class: "mono", text: u.candidate_simple_paths }),
    el("td", { class: "mono bad", text: u.paths_still_fitting_snapshot })));
  embedBody.appendChild(el("h3", { text: "未安置批次（自最深耗尽层起）" }));
  embedBody.appendChild(el("div", { class: "table-wrap" },
    el("table", { class: "result" },
      el("thead", {}, el("tr", {},
        el("th", { text: "批次" }), el("th", { text: "搜索层" }),
        el("th", { text: "候选简单路径数" }),
        el("th", { text: "快照下仍可容纳的路径数" }))),
      el("tbody", {}, ...urows))));

  const srows = (r.saturated_pipes || []).map(p => el("tr", {},
    el("td", { class: "mono", text: p.pipe }),
    el("td", { text: `${p.from} → ${p.to}` }),
    el("td", { class: "mono", text: p.frozen_flow }),
    el("td", { class: "mono bad", text: p.used }),
    el("td", { class: "mono bad", text: "0" })));
  embedBody.appendChild(el("h3", {
    text: (r.saturated_pipes || []).length
      ? "已占满管路（占用 = 冻结流量）" : "已占满管路（无）" }));
  if (srows.length) embedBody.appendChild(el("div", { class: "table-wrap" },
    el("table", { class: "result" },
      el("thead", {}, el("tr", {},
        el("th", { text: "管路" }), el("th", { text: "方向" }),
        el("th", { text: "冻结流量" }), el("th", { text: "占用" }),
        el("th", { text: "剩余" }))),
      el("tbody", {}, ...srows))));

  const prows = (r.pipe_usage || []).map(p => el("tr", {},
    el("td", { class: "mono", text: p.pipe }),
    el("td", { text: `${p.from} → ${p.to}` }),
    el("td", { class: "mono", text: p.frozen_flow }),
    el("td", { class: "mono", text: p.used }),
    el("td", { class: p.remaining === 0 ? "bad mono" : "mono",
               text: p.remaining })));
  embedBody.appendChild(el("details", { open: "" },
    el("summary", { text: "各管剩余容量（耗尽快照）" }),
    el("div", { class: "table-wrap" },
      el("table", { class: "result" },
        el("thead", {}, el("tr", {},
          el("th", { text: "管路" }), el("th", { text: "方向" }),
          el("th", { text: "冻结流量" }), el("th", { text: "占用" }),
          el("th", { text: "剩余容量" }))),
        el("tbody", {}, ...prows)))));
}

function showEmbed(kind, r, envelope) {
  embedBody.innerHTML = "";
  embedStatusBanner.innerHTML = "";
  if (kind === "embedded") {
    embedStatusBanner.appendChild(el("div",
      { class: "banner optimal",
        text: `✓ 全部批次可同时嵌入冻结流量`
          + (envelope?.replayed ? "（编组原结论重放）" : "") }));
    renderEmbedded(r);
  } else if (kind === "cannot_embed") {
    embedStatusBanner.appendChild(el("div",
      { class: "banner infeasible",
        text: "✗ 无法同时嵌入（完整穷尽分配后的稳定结论"
          + (envelope?.replayed ? "· 原结论重放" : "") + "）" }));
    renderCannotEmbed(r);
  } else {
    embedStatusBanner.appendChild(el("div", { class: "banner invalid",
      text: "⚠ 核验请求被拒绝" }));
    embedBody.appendChild(el("p", {},
      el("b", { class: "bad", text: "原因：" }),
      el("span", { class: "mono",
        text: ` ${r.loc ? r.loc + " — " : ""}${r.error}` })));
  }
}

async function submitEmbed() {
  let form;
  try {
    form = collectForm();
  } catch (e) {
    showEmbed("invalid", { error: e.message, loc: e.loc });
    return;
  }
  if (!form.auditId) {
    showEmbed("invalid", { error: "请填写已冻结 optimal 的来源审计标识",
                           loc: "audit_id" });
    return;
  }
  if (!form.groupId) {
    showEmbed("invalid", { error: "请填写稳定编组标识", loc: "group_id" });
    return;
  }
  const body = { audit_id: form.auditId, group_id: form.groupId,
                 batches: form.batches };
  embedPreview.textContent = JSON.stringify(body, null, 2);
  saveEmbedForm();
  try {
    const resp = await fetch("/api/embed", {
      method: "POST",
      headers: { "Content-Type": "application/json",
                 "X-Audit-Id": form.auditId, "X-Group-Id": form.groupId },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (resp.status === 400 || resp.status === 404 || resp.status === 409) {
      showEmbed("invalid", { error: data.error, loc: data.loc });
      return;
    }
    // 200（embedded / cannot_embed 均已落盘）：标记为可在刷新后核对的编组
    try {
      const f = JSON.parse(localStorage.getItem(FORM_KEY) || "{}");
      f.confirmedGroupId = form.groupId;
      localStorage.setItem(FORM_KEY, JSON.stringify(f));
    } catch (e) { /* ignore */ }
    showEmbed(data.result.status, data.result, data);
  } catch (e) {
    showEmbed("invalid", { error: `请求失败: ${e}`, loc: "$" });
  }
}

async function lookupGroup() {
  const groupId = groupInput.value.trim();
  if (!groupId) {
    showEmbed("invalid", { error: "请填写要查询的稳定编组标识", loc: "group_id" });
    return;
  }
  saveEmbedForm();
  try {
    const resp = await fetch(`/api/groups/${encodeURIComponent(groupId)}`);
    const data = await resp.json();
    if (resp.status !== 200) {
      showEmbed("invalid", { error: data.error || "编组不存在", loc: "group_id" });
      return;
    }
    if (embedAuditInput.value.trim() === "")
      embedAuditInput.value = data.audit_id || "";
    showEmbed(data.result.status, data.result,
              { replayed: true });
  } catch (e) {
    showEmbed("invalid", { error: `请求失败: ${e}`, loc: "$" });
  }
}

document.querySelector("#addBatch").onclick = () => { addBatchRow(); saveEmbedForm(); };
document.querySelector("#embedSubmitBtn").onclick = submitEmbed;
document.querySelector("#groupLookupBtn").onclick = lookupGroup;
for (const node of [embedAuditInput, groupInput])
  node.addEventListener("change", saveEmbedForm);
// 批次行内的任何编辑也即时留存，刷新/重开后表单与结论一致
batchBody.addEventListener("input", saveEmbedForm);
batchBody.addEventListener("change", saveEmbedForm);

const EMBED_SAMPLE = [
  { id: "B1", from: "S1", to: "D2", volume: 2, avoid: "p5" },
  { id: "B2", from: "S2", to: "D1", volume: 1, avoid: "" },
];
document.querySelector("#embedSampleBtn").onclick = () => {
  batchBody.innerHTML = "";
  for (const b of EMBED_SAMPLE) addBatchRow(b);
  if (!embedAuditInput.value.trim()) embedAuditInput.value = "run-embed-base";
  if (!groupInput.value.trim()) groupInput.value = "group-sample-1";
  renderBatchCounter();
  saveEmbedForm();
};

// 刷新 / 重新打开：恢复表单并从服务端核对同一编组结论
(function restoreEmbedForm() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(FORM_KEY) || "null"); }
  catch (e) { saved = null; }
  if (saved && Array.isArray(saved.batches) && saved.batches.length) {
    embedAuditInput.value = saved.auditId || "";
    groupInput.value = saved.groupId || "";
    batchBody.innerHTML = "";
    for (const b of saved.batches) addBatchRow(b);
    renderBatchCounter();
    // 仅在该编组确曾被服务端接受落盘时，刷新/重开后自动核对同一结论
    if (saved.groupId && saved.confirmedGroupId === saved.groupId)
      lookupGroup();
  } else {
    for (const b of EMBED_SAMPLE) addBatchRow(b);
    renderBatchCounter();
  }
})();
