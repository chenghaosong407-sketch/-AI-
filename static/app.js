/* 教师端聊天界面 */
"use strict";

const $ = id => document.getElementById(id);
const state = { me: null, convId: null, files: [], controller: null, busy: false };

// 快捷指令:send=true 直接发送;否则把模板放进输入框
const CHIPS = [
  { label: "菜单", text: "菜单", send: true },
  { label: "答疑", text: "答疑:" },
  { label: "备课", text: "备课:PU 第_级 Unit _ 第_课时,课时__分钟,班级层次:" },
  { label: "分层", text: "分层:年级__,教材及级别__,班级人数__,现象:" },
  { label: "衔接", text: "衔接:Think 第_级 Unit _" },
  { label: "家长话术", text: "家长话术:" },
  { label: "做培训", text: "做培训:主题__,对象__,时长__分钟" },
  { label: "评课", text: "评课:(请点📎上传听课记录或教学设计)" },
  { label: "诊断", text: "诊断:" },
  { label: "体系", text: "体系:" },
  { label: "查一下", text: "查一下:" },
  { label: "提交", text: "提交:课例/做法/问题:" },
  { label: "学习这份", text: "学习这份", attach: true },
  { label: "简版", text: "简版", send: true },
  { label: "展开", text: "展开:" },
];

const isTouch = matchMedia("(pointer: coarse)").matches;

/* ---------------- 登录 ---------------- */
async function boot() {
  try {
    state.me = await api("/api/me");
  } catch (e) {
    if (e.status === 401) return showLogin();
    alert("无法连接服务器:" + e.message);
    return;
  }
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  const { user, app } = state.me;
  document.title = app.title;
  $("whoami").textContent = `${user.display_name}(${user.role === "admin" ? "管理员" : "教师"})`;
  $("adminLink").classList.toggle("hidden", user.role !== "admin");
  $("demoBadge").classList.toggle("hidden", app.llm_ready);
  $("searchBadge").classList.toggle("hidden", !app.search_ready);
  renderChips();
  await loadConversations();
  showWelcome();
  if (user.must_change_password) changePassword(true);
}

function showLogin() {
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
  $("loginUser").focus();
}

$("loginForm").addEventListener("submit", async e => {
  e.preventDefault();
  $("loginErr").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: { username: $("loginUser").value, password: $("loginPass").value } });
    $("loginPass").value = "";
    boot();
  } catch (err) {
    $("loginErr").textContent = err.message;
  }
});

$("logout").onclick = async () => { await api("/api/logout", { method: "POST" }); location.reload(); };

function changePassword(force = false) {
  openModal(`
    <h3>${force ? "首次登录,请修改密码" : "修改密码"}</h3>
    <div style="display:grid;gap:10px">
      <input id="pwOld" type="password" placeholder="原密码">
      <input id="pwNew" type="password" placeholder="新密码(至少6位)">
      <input id="pwNew2" type="password" placeholder="再次输入新密码">
      <div class="err" id="pwErr"></div>
    </div>
    <div class="row">${force ? "" : '<button data-close>取消</button>'}<button class="primary" id="pwSave">保存</button></div>`);
  $("pwSave").onclick = async () => {
    if ($("pwNew").value !== $("pwNew2").value) return ($("pwErr").textContent = "两次输入不一致");
    try {
      await api("/api/me/password", { method: "POST", body: { old_password: $("pwOld").value, new_password: $("pwNew").value } });
      closeModal(); toast("密码已修改");
      state.me.user.must_change_password = 0;
    } catch (e) { $("pwErr").textContent = e.message; }
  };
}
$("changePw").onclick = () => changePassword(false);

/* ---------------- 对话列表 ---------------- */
async function loadConversations() {
  const list = await api("/api/conversations");
  $("convList").innerHTML = list.length ? "" : '<div class="muted" style="padding:10px">还没有对话</div>';
  for (const c of list) {
    const el = document.createElement("div");
    el.className = "conv-item" + (c.id === state.convId ? " active" : "");
    el.innerHTML = `<span title="${esc(c.title)}">${esc(c.title)}</span><button class="del" title="删除">✕</button>`;
    el.onclick = () => openConversation(c.id);
    el.querySelector(".del").onclick = async ev => {
      ev.stopPropagation();
      if (!confirm("删除这个对话?")) return;
      await api(`/api/conversations/${c.id}`, { method: "DELETE" });
      if (state.convId === c.id) newChat();
      loadConversations();
    };
    el.ondblclick = async () => {
      const t = prompt("重命名对话", c.title);
      if (t) { await api(`/api/conversations/${c.id}`, { method: "PATCH", body: { title: t } }); loadConversations(); }
    };
    $("convList").appendChild(el);
  }
}

async function openConversation(id) {
  if (state.busy) return;
  state.convId = id;
  $("sidebar").classList.remove("open");
  const data = await api(`/api/conversations/${id}`);
  $("convTitle").textContent = data.conversation.title;
  $("messages").innerHTML = "";
  for (const m of data.messages) {
    if (m.role === "user") addUserMsg(m.content, m.attachments);
    else renderAssistant(addAssistantMsg(), m.content, m.sources, m.searches, m.id, m.rating);
  }
  scrollBottom(true);
  loadConversations();
}

function newChat() {
  if (state.busy) return;
  state.convId = null;
  $("convTitle").textContent = "新对话";
  showWelcome();
  loadConversations();
  $("sidebar").classList.remove("open");
  $("input").focus();
}
$("newChat").onclick = newChat;
$("menuBtn").onclick = () => $("sidebar").classList.toggle("open");

function showWelcome() {
  if (state.convId) return;
  const app = state.me.app;
  $("messages").innerHTML = `
    <div class="welcome">
      <h2>${esc(app.title)}</h2>
      <p class="muted">PU / Think 备课、分层、应试衔接、评课、师训、家长话术。<br>不会用就点下方"菜单"。</p>
      ${app.llm_ready ? "" : '<div class="notice">当前为演示模式:未配置大模型,只会返回知识库检索结果。请管理员按 README 配置 .env。</div>'}
      ${app.daily_limit ? `<p class="muted">今日已提问 ${app.today_count}/${app.daily_limit} 次</p>` : ""}
    </div>`;
}

/* ---------------- 消息渲染 ---------------- */
function scrollBottom(force = false) {
  const box = $("messages");
  if (force || box.scrollHeight - box.scrollTop - box.clientHeight < 160) box.scrollTop = box.scrollHeight;
}

function addUserMsg(text, attachments = []) {
  const welcome = $("messages").querySelector(".welcome");
  if (welcome) welcome.remove();
  const el = document.createElement("div");
  el.className = "msg user";
  el.innerHTML = `<div class="bubble">${esc(text)}${attachments.length ? `<div class="attach-chip">📎 ${attachments.map(esc).join("、")}</div>` : ""}</div>`;
  $("messages").appendChild(el);
  scrollBottom(true);
}

function addAssistantMsg() {
  const el = document.createElement("div");
  el.className = "msg assistant";
  el.innerHTML = `<div class="status hidden"></div><div class="bubble cursor"></div><div class="meta-line hidden"></div>`;
  $("messages").appendChild(el);
  return el;
}

function looksLikeCard(text) {
  return /建议文件名[:：]/.test(text) && /状态[:：]\s*待审核/.test(text);
}

function extractCard(text) {
  const start = text.search(/建议文件名[:：]/);
  if (start < 0) return text;
  const tail = text.slice(start);
  const end = tail.search(/审核意见[:：][^\n]*/);
  if (end < 0) return tail.trim();
  const line = tail.slice(end).split("\n")[0];
  return tail.slice(0, end + line.length).trim();
}

function docTitle(text) {
  // 教案首行"课题|教材及级别|课时|层次"、培训首行"培训名称|对象|…":取前两项作标题
  const lines = text.split("\n").map(l => l.replace(/\*\*/g, "").trim()).filter(Boolean);
  const head = lines.find(l => /^(课题|培训名称)/.test(l)) || lines.find(l => /^#{1,3}\s+/.test(l)) || lines[0] || "";
  const parts = head.replace(/^#+\s*/, "").split("|")
    .map(x => x.replace(/^(课题|培训名称|教材及级别|对象)\s*[:：]?\s*/, "").trim())
    .filter(Boolean);
  return parts.slice(0, 2).join(" ").slice(0, 40) || $("convTitle").textContent;
}

async function downloadDocx(text) {
  try {
    const resp = await fetch("/api/export/docx", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: text, title: docTitle(text) }),
    });
    if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || resp.statusText);
    const blob = await resp.blob();
    const cd = resp.headers.get("content-disposition") || "";
    const m = cd.match(/filename\*=UTF-8''([^;]+)/);
    const name = m ? decodeURIComponent(m[1]) : "教研助手.docx";
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    toast("已生成 Word:" + name);
  } catch (e) {
    toast("下载失败:" + e.message + "(微信内置浏览器不支持下载,请用手机浏览器打开)");
  }
}

function feedbackButtons(meta, msgId, rating) {
  const up = document.createElement("button");
  const down = document.createElement("button");
  up.textContent = "👍"; down.textContent = "👎";
  up.title = "有用"; down.title = "没用,告诉教研负责人哪里不对";
  const paint = r => {
    up.classList.toggle("primary", r === 1);
    down.classList.toggle("primary", r === -1);
  };
  let current = rating || 0;
  paint(current);
  const send = async (r, comment = "") => {
    try {
      await api(`/api/messages/${msgId}/feedback`, { method: "POST", body: { rating: r, comment } });
      current = r; paint(r);
      if (r === -1) toast("已反馈,教研负责人会在后台看到");
    } catch (e) { toast(e.message); }
  };
  up.onclick = () => send(current === 1 ? 0 : 1);
  down.onclick = () => {
    if (current === -1) return send(0);
    const comment = prompt("哪里不对?(可选,例如:页码是编的 / 没有分层 / 不符合常州考情)", "");
    if (comment === null) return;
    send(-1, comment);
  };
  meta.appendChild(up); meta.appendChild(down);
}

function renderAssistant(el, text, sources = [], searches = [], msgId = null, rating = 0) {
  const bubble = el.querySelector(".bubble");
  bubble.classList.remove("cursor");
  bubble.innerHTML = renderMarkdown(text);
  el.querySelector(".status").classList.add("hidden");
  const meta = el.querySelector(".meta-line");
  meta.innerHTML = "";
  const seen = new Set();
  const chips = (sources || []).filter(s => !seen.has(s.filename) && seen.add(s.filename));
  if (chips.length) {
    meta.insertAdjacentHTML("beforeend", `<span>知识库:</span>` +
      chips.map(s => `<span class="source-chip">${esc(s.tag)}${esc(s.filename)}</span>`).join(""));
  }
  if (searches && searches.length) {
    meta.insertAdjacentHTML("beforeend", `<span>联网:${searches.map(s => `${esc(s.query)}(${s.count}条)`).join(";")}</span>`);
  }
  const copyBtn = document.createElement("button");
  copyBtn.textContent = "复制";
  copyBtn.onclick = () => copyText(text);
  meta.appendChild(copyBtn);
  if (text.length > 200) {
    const wordBtn = document.createElement("button");
    wordBtn.textContent = "下载Word";
    wordBtn.onclick = () => downloadDocx(text);
    meta.appendChild(wordBtn);
  }
  if (msgId) feedbackButtons(meta, msgId, rating);
  if (looksLikeCard(text)) {
    const subBtn = document.createElement("button");
    subBtn.textContent = "提交入库卡给教研负责人";
    subBtn.onclick = () => submitCard(extractCard(text));
    meta.appendChild(subBtn);
  }
  meta.classList.remove("hidden");
}

function submitCard(content) {
  openModal(`
    <h3>提交入库卡</h3>
    <p class="muted">提交后由教研负责人在管理后台审核,通过后才会进入知识库。可先修改内容。</p>
    <textarea id="cardText">${esc(content)}</textarea>
    <div class="err" id="cardErr"></div>
    <div class="row"><button data-close>取消</button><button class="primary" id="cardSend">提交</button></div>`);
  $("cardSend").onclick = async () => {
    try {
      await api("/api/submissions", { method: "POST", body: { content: $("cardText").value, conversation_id: state.convId } });
      closeModal(); toast("已提交,等待审核");
    } catch (e) { $("cardErr").textContent = e.message; }
  };
}

$("mySubs").onclick = async () => {
  const rows = await api("/api/submissions/mine");
  const label = { pending: "待审核", approved: "已入库", rejected: "未通过" };
  openModal(`<h3>我的入库卡</h3>` + (rows.length ? rows.map(r => `
    <div class="item" style="margin-bottom:8px">
      <div class="item-head"><span class="tag">${label[r.status] || r.status}</span><span class="muted">${esc(r.created_at)}</span></div>
      <div style="font-size:13px;margin-top:6px;white-space:pre-wrap">${esc(r.preview)}…</div>
      ${r.review_note ? `<div class="muted">审核意见:${esc(r.review_note)}</div>` : ""}
    </div>`).join("") : '<p class="muted">还没有提交过。对话中生成入库卡后,点卡片下方的"提交入库卡"。</p>') +
    `<div class="row"><button data-close>关闭</button></div>`);
};

/* ---------------- 输入区 ---------------- */
function renderChips() {
  $("chips").innerHTML = "";
  for (const c of CHIPS) {
    const b = document.createElement("button");
    b.className = "chip";
    b.textContent = c.label;
    b.onclick = () => {
      if (c.send) return send(c.text);
      $("input").value = c.text;
      autoGrow();
      $("input").focus();
      if (c.attach) $("fileInput").click();
    };
    $("chips").appendChild(b);
  }
}

function autoGrow() {
  const t = $("input");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 200) + "px";
}
$("input").addEventListener("input", autoGrow);
$("input").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !isTouch && !e.isComposing) {
    e.preventDefault();
    send();
  }
});

$("attachBtn").onclick = () => $("fileInput").click();
$("fileInput").onchange = () => {
  state.files = Array.from($("fileInput").files).slice(0, 5);
  renderFiles();
};
function renderFiles() {
  $("fileList").innerHTML = state.files.map((f, i) => `<span>📎 ${esc(f.name)} <a href="#" data-rm="${i}">✕</a></span>`).join("");
  $("fileList").querySelectorAll("[data-rm]").forEach(a => a.onclick = e => {
    e.preventDefault();
    state.files.splice(Number(a.dataset.rm), 1);
    renderFiles();
  });
}

function setBusy(busy) {
  state.busy = busy;
  $("sendBtn").classList.toggle("hidden", busy);
  $("stopBtn").classList.toggle("hidden", !busy);
}
$("stopBtn").onclick = () => state.controller && state.controller.abort();

async function send(preset) {
  if (state.busy) return;
  const text = (preset ?? $("input").value).trim();
  if (!text && !state.files.length) return;
  const files = state.files;
  addUserMsg(text || "学习这份", files.map(f => f.name));
  $("input").value = ""; autoGrow();
  state.files = []; renderFiles(); $("fileInput").value = "";

  const form = new FormData();
  form.append("text", text);
  if (state.convId) form.append("conversation_id", state.convId);
  files.forEach(f => form.append("files", f));

  const el = addAssistantMsg();
  const bubble = el.querySelector(".bubble");
  const status = el.querySelector(".status");
  let answer = "", sources = [], searches = [], msgId = null;
  setBusy(true);
  state.controller = new AbortController();
  let pending = false;
  const paint = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { bubble.innerHTML = renderMarkdown(answer); pending = false; scrollBottom(); });
  };

  try {
    const resp = await fetch("/api/chat", { method: "POST", body: form, credentials: "same-origin", signal: state.controller.signal });
    if (resp.status === 401) { showLogin(); return; }
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || resp.statusText);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const line = chunk.split("\n").find(l => l.startsWith("data:"));
        if (!line) continue;
        const ev = JSON.parse(line.slice(5));
        if (ev.type === "meta") {
          if (!state.convId) { state.convId = ev.conversation_id; loadConversations(); }
          if (ev.masked) {
            el.insertAdjacentHTML("beforebegin", `<div class="notice">已自动隐去 ${ev.masked} 处手机号/身份证号。学生信息请用代号(如"A同学")。</div>`);
          }
        } else if (ev.type === "sources") {
          sources = ev.items;
        } else if (ev.type === "status") {
          status.textContent = ev.text; status.classList.remove("hidden");
        } else if (ev.type === "delta") {
          status.classList.add("hidden");
          answer += ev.text; paint();
        } else if (ev.type === "error") {
          answer += `\n\n> ⚠️ ${ev.text}`; paint();
        } else if (ev.type === "done") {
          msgId = ev.message_id; searches = ev.searches || [];
        }
      }
    }
  } catch (e) {
    if (e.name === "AbortError") answer += "\n\n> (已停止)";
    else answer += `\n\n> ⚠️ ${e.message}`;
  } finally {
    setBusy(false);
    state.controller = null;
    renderAssistant(el, answer || "(没有生成内容)", sources, searches, msgId);
    scrollBottom();
    loadConversations().then(() => {
      const title = $("convList").querySelector(".conv-item.active span");
      if (title) $("convTitle").textContent = title.textContent;
    }).catch(() => {});
  }
}
$("sendBtn").onclick = () => send();

boot();
