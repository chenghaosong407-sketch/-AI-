/* 管理后台 */
"use strict";

const $ = id => document.getElementById(id);
const TIER_NAME = { 1: "L1机构标准", 2: "L2官方依据", 3: "L3课例参考", 4: "L4参考资料" };
const tierTag = t => `<span class="tag t${t}">${TIER_NAME[t] || t}</span>`;

function guard(fn) {
  return async (...args) => {
    try { await fn(...args); }
    catch (e) {
      if (e.status === 401) location.href = "/";
      else if (e.status === 403) { document.body.innerHTML = '<div class="admin"><p>仅管理员可访问。<a href="/">返回</a></p></div>'; }
      else alert(e.message);
    }
  };
}

/* ---------- 选项卡 ---------- */
const loaders = {};
document.querySelectorAll("#tabs button").forEach(b => b.onclick = () => {
  document.querySelectorAll("#tabs button").forEach(x => x.classList.toggle("active", x === b));
  document.querySelectorAll("section[id^=tab-]").forEach(s => s.classList.toggle("hidden", s.id !== "tab-" + b.dataset.tab));
  loaders[b.dataset.tab] && loaders[b.dataset.tab]();
});

/* ---------- 概览 ---------- */
loaders.overview = guard(async () => {
  const [stats, kb] = await Promise.all([api("/api/admin/stats"), api("/api/admin/kb/stats")]);
  const c = stats.config;
  $("configBox").innerHTML = `
    模型:${esc(c.model)}(${esc(c.llm_base_url)})<br>
    向量模型:${esc(c.embed_model)}<br>
    联网搜索:${esc(c.search)}<br>
    扣子插件接口:${c.plugin_api ? "已开启" : "未开启"}<br>
    <span class="muted">修改配置请编辑 .env 后重启程序。</span>`;
  $("tierStats").innerHTML = kb.tiers.map(t => `<div class="stat">${tierTag(t.tier)}<b>${t.docs}</b>份 / ${t.chunks}段</div>`).join("") +
    (kb.missing_embeddings && kb.embed_ready ? `<div class="muted" style="grid-column:1/-1">有 ${kb.missing_embeddings} 段缺少向量,可到"知识库"点"补全向量"。</div>` : "");
  const cmds = Object.entries(stats.by_command);
  $("cmdStats").innerHTML = cmds.length ? cmds.map(([k, v]) => `${esc(k)}|${v}`).join("<br>") : '<span class="muted">暂无数据</span>';
  $("userStats").innerHTML = stats.per_user.map(u => `${esc(u.display_name)}(${esc(u.username)})|近7天 ${u.last7 || 0}|近30天 ${u.last30 || 0}`).join("<br>");
  $("pendingCount").textContent = stats.pending_submissions ? `(${stats.pending_submissions})` : "";
  $("fbStats").textContent = `回答反馈:👍 ${stats.feedback.up} · 👎 ${stats.feedback.down}`;
});

/* ---------- 知识库 ---------- */
$("upBtn").onclick = guard(async () => {
  const files = $("upFiles").files;
  if (!files.length) return alert("请选择文件");
  const form = new FormData();
  form.append("tier", $("upTier").value);
  form.append("note", $("upNote").value);
  Array.from(files).forEach(f => form.append("files", f));
  $("upBtn").disabled = true;
  $("upResult").innerHTML = '<span class="muted">正在解析、切分并向量化,大文件需要一点时间……</span>';
  try {
    const res = await api("/api/admin/documents", { method: "POST", body: form });
    $("upResult").innerHTML = res.results.map(r => r.ok
      ? `<div class="result-ok">✔ ${esc(r.filename)} → ${TIER_NAME[r.tier]},${r.chunks} 段${r.warning ? `<br><span class="result-err">${esc(r.warning)}</span>` : ""}</div>`
      : `<div class="result-err">✘ ${esc(r.filename)}:${esc(r.error)}</div>`).join("");
    $("upFiles").value = "";
    loadDocs();
  } finally { $("upBtn").disabled = false; }
});

$("testBtn").onclick = guard(async () => {
  const q = $("testQ").value.trim();
  if (!q) return;
  const rows = await api("/api/admin/kb/test", { method: "POST", body: { query: q } });
  $("testResult").innerHTML = rows.length ? rows.map(r => `
    <div class="item"><div class="item-head">${tierTag(r.tier)}<span class="name">${esc(r.filename)}</span><span class="muted">排序分 ${r.score}</span></div>
    <pre>${esc(r.content)}</pre></div>`).join("") : '<div class="muted">没有检索到。说明知识库缺这类资料,或资料里的说法与问题差别太大。</div>';
});

const loadDocs = guard(async () => {
  const params = new URLSearchParams({ q: $("docQ").value });
  if ($("docTier").value) params.set("tier", $("docTier").value);
  const docs = await api("/api/admin/documents?" + params);
  $("docList").innerHTML = docs.length ? "" : '<div class="muted">还没有资料</div>';
  for (const d of docs) {
    const el = document.createElement("div");
    el.className = "item";
    el.innerHTML = `
      <div class="item-head">
        ${tierTag(d.tier)}<span class="name">${esc(d.filename)}</span>
        <span class="muted">${d.chunk_count}段 · ${d.chars}字 · ${esc(d.uploader || "")} · ${esc(d.created_at.slice(0, 10))}</span>
      </div>
      ${d.note ? `<div class="muted">备注:${esc(d.note)}</div>` : ""}
      <div class="form-row" style="margin-top:6px">
        <select data-act="tier">${[1, 2, 3, 4].map(t => `<option value="${t}" ${t === d.tier ? "selected" : ""}>改为 ${TIER_NAME[t]}</option>`).join("")}</select>
        <button data-act="view">查看分段</button>
        <button data-act="del" class="danger">删除</button>
      </div>
      <div data-box></div>`;
    el.querySelector("[data-act=tier]").onchange = guard(async e => {
      await api(`/api/admin/documents/${d.id}`, { method: "PATCH", body: { tier: Number(e.target.value) } });
      loadDocs();
    });
    el.querySelector("[data-act=view]").onclick = guard(async () => {
      const box = el.querySelector("[data-box]");
      if (box.innerHTML) { box.innerHTML = ""; return; }
      const chunks = await api(`/api/admin/documents/${d.id}/chunks`);
      box.innerHTML = chunks.map(c => `<pre>#${c.seq + 1}${c.has_vector ? "" : "(无向量)"}\n${esc(c.content)}</pre>`).join("");
    });
    el.querySelector("[data-act=del]").onclick = guard(async () => {
      if (!confirm(`删除《${d.filename}》?`)) return;
      await api(`/api/admin/documents/${d.id}`, { method: "DELETE" });
      loadDocs();
    });
    $("docList").appendChild(el);
  }
});
$("docSearch").onclick = loadDocs;
$("docTier").onchange = loadDocs;
function reembed(btn, onlyMissing, question) {
  return guard(async () => {
    if (!confirm(question)) return;
    btn.disabled = true;
    try {
      const r = await api("/api/admin/reembed", { method: "POST", body: { only_missing: onlyMissing } });
      alert(`完成,更新 ${r.updated} 段`);
    } finally { btn.disabled = false; }
  });
}
$("reembed").onclick = reembed($("reembed"), true, "为缺少向量的分段补全向量?资料多时需要几分钟。");
$("reembedAll").onclick = reembed($("reembedAll"), false, "更换向量模型后才需要全部重建。确定为所有分段重新生成向量?资料多时需要较长时间。");
loaders.kb = loadDocs;

/* ---------- 入库卡 ---------- */
const loadSubs = guard(async () => {
  const status = $("subStatus").value;
  const rows = await api("/api/admin/submissions?status=" + status);
  $("subList").innerHTML = rows.length ? "" : '<div class="muted">没有记录</div>';
  for (const s of rows) {
    const el = document.createElement("div");
    el.className = "item";
    el.innerHTML = `
      <div class="item-head"><span class="name">#${s.id} ${esc(s.submitter || "")}</span><span class="muted">${esc(s.created_at)}</span>
        ${s.target_tier ? tierTag(s.target_tier) : ""}</div>
      ${status === "pending" ? `<textarea rows="12" style="margin-top:6px">${esc(s.content)}</textarea>` : `<pre>${esc(s.content)}</pre>`}
      ${s.review_note ? `<div class="muted">审核意见:${esc(s.review_note)}</div>` : ""}
      ${status === "pending" ? `
      <div class="form-row" style="margin-top:6px">
        <select data-tier>
          <option value="3">通过 → L3 课例库</option>
          <option value="1">通过 → L1 机构标准库</option>
          <option value="4">通过 → L4 参考资料库</option>
        </select>
        <input data-note placeholder="审核意见(可选)" style="flex:1;min-width:160px">
        <button class="primary" data-ok>通过入库</button>
        <button class="danger" data-no>退回</button>
      </div>` : ""}`;
    if (status === "pending") {
      el.querySelector("[data-ok]").onclick = guard(async () => {
        await api(`/api/admin/submissions/${s.id}/approve`, { method: "POST", body: {
          tier: Number(el.querySelector("[data-tier]").value),
          content: el.querySelector("textarea").value,
          note: el.querySelector("[data-note]").value,
        } });
        loadSubs(); loaders.overview();
      });
      el.querySelector("[data-no]").onclick = guard(async () => {
        const note = el.querySelector("[data-note]").value || prompt("退回原因(会显示给提交人)") || "";
        await api(`/api/admin/submissions/${s.id}/reject`, { method: "POST", body: { note } });
        loadSubs(); loaders.overview();
      });
    }
    $("subList").appendChild(el);
  }
});
$("subStatus").onchange = loadSubs;
loaders.subs = loadSubs;

/* ---------- 回答反馈 ---------- */
const loadFeedback = guard(async () => {
  const rows = await api("/api/admin/feedback?rating=" + $("fbRating").value);
  $("fbList").innerHTML = rows.length ? "" : '<div class="muted">暂无反馈</div>';
  for (const r of rows) {
    const el = document.createElement("div");
    el.className = "item";
    el.innerHTML = `
      <div class="item-head"><span class="name">${esc(r.question || "(无问题)")}</span>
        <span class="muted">${esc(r.display_name)} · ${esc(r.created_at.slice(0, 16).replace("T", " "))}</span></div>
      ${r.comment ? `<div style="margin-top:6px">反馈:<b>${esc(r.comment)}</b></div>` : ""}
      <details style="margin-top:6px"><summary class="muted">查看回答</summary><pre>${esc(r.answer)}</pre></details>`;
    $("fbList").appendChild(el);
  }
});
$("fbRating").onchange = loadFeedback;
loaders.feedback = loadFeedback;

/* ---------- 知识缺口 ---------- */
loaders.gaps = guard(async () => {
  const rows = await api("/api/admin/gaps");
  $("gapList").innerHTML = rows.length ? rows.map(g => `
    <div class="item"><div class="item-head"><span class="name">${esc(g.gap)}</span>
    <span class="tag">${g.count} 次</span><span class="muted">${esc(g.last.slice(0, 10))} · ${g.users.map(esc).join("、")}</span></div></div>`).join("")
    : '<div class="muted">暂无。助手在回答末尾写"知识缺口:…"时会出现在这里。</div>';
});

/* ---------- 账号 ---------- */
const loadUsers = guard(async () => {
  const users = await api("/api/admin/users");
  $("userList").innerHTML = "";
  for (const u of users) {
    const el = document.createElement("div");
    el.className = "item";
    el.innerHTML = `
      <div class="item-head">
        <span class="name">${esc(u.display_name)}(${esc(u.username)})</span>
        <span class="tag">${u.role === "admin" ? "管理员" : "教师"}</span>
        ${u.active ? "" : '<span class="tag t4">已停用</span>'}
        <span class="muted">累计提问 ${u.questions}</span>
      </div>
      <div class="form-row" style="margin-top:6px">
        <button data-act="pw">重置密码</button>
        <button data-act="role">${u.role === "admin" ? "设为教师" : "设为管理员"}</button>
        <button data-act="active" class="${u.active ? "danger" : ""}">${u.active ? "停用" : "启用"}</button>
      </div>`;
    const patch = body => guard(async () => { await api(`/api/admin/users/${u.id}`, { method: "PATCH", body }); loadUsers(); })();
    el.querySelector("[data-act=pw]").onclick = () => {
      const pw = prompt(`给 ${u.display_name} 设置新密码(至少6位),对方下次登录需修改`);
      if (pw) patch({ reset_password: pw });
    };
    el.querySelector("[data-act=role]").onclick = () => patch({ role: u.role === "admin" ? "teacher" : "admin" });
    el.querySelector("[data-act=active]").onclick = () => patch({ active: !u.active });
    $("userList").appendChild(el);
  }
});
$("createUsers").onclick = guard(async () => {
  const res = await api("/api/admin/users", { method: "POST", body: { lines: $("newUsers").value } });
  $("userResult").innerHTML =
    (res.created.length ? `<div class="result-ok">已创建:${res.created.map(esc).join("、")}</div>` : "") +
    res.errors.map(e => `<div class="result-err">${esc(e)}</div>`).join("");
  if (res.created.length) $("newUsers").value = "";
  loadUsers();
});
loaders.users = loadUsers;

loaders.overview();
