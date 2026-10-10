/* 公共工具:接口请求、转义、轻量 Markdown 渲染、弹窗 */
"use strict";

async function api(path, options = {}) {
  const opts = { credentials: "same-origin", ...options };
  if (opts.body && !(opts.body instanceof FormData) && typeof opts.body !== "string") {
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, opts);
  if (resp.status === 401) {
    const err = new Error("请先登录");
    err.status = 401;
    throw err;
  }
  const type = resp.headers.get("content-type") || "";
  const data = type.includes("application/json") ? await resp.json() : await resp.text();
  if (!resp.ok) {
    const msg = (data && data.detail) ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : resp.statusText;
    const err = new Error(msg);
    err.status = resp.status;
    throw err;
  }
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function inlineMd(s) {
  // s 已转义
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>")
    .replace(/(https?:\/\/[^\s<)）]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
}

/* 只支持教研回答里常见的格式:标题、列表、加粗、代码、引用、分隔线。先整体转义,保证安全。 */
function renderMarkdown(src) {
  const lines = String(src || "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let para = [];
  let list = null; // {type, items}
  let inCode = false, code = [];

  const flushPara = () => {
    if (para.length) { out.push("<p>" + para.map(l => inlineMd(esc(l))).join("<br>") + "</p>"); para = []; }
  };
  const flushList = () => {
    if (list) {
      out.push(`<${list.type}>` + list.items.map(i => `<li>${inlineMd(esc(i))}</li>`).join("") + `</${list.type}>`);
      list = null;
    }
  };

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    if (/^```/.test(line.trim())) {
      if (inCode) { out.push("<pre><code>" + esc(code.join("\n")) + "</code></pre>"); code = []; inCode = false; }
      else { flushPara(); flushList(); inCode = true; }
      continue;
    }
    if (inCode) { code.push(raw); continue; }
    if (!line.trim()) { flushPara(); flushList(); continue; }
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      flushPara(); flushList();
      const level = Math.min(m[1].length + 1, 4);
      out.push(`<h${level}>${inlineMd(esc(m[2]))}</h${level}>`);
    } else if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flushPara(); flushList(); out.push("<hr>");
    } else if ((m = line.match(/^\s*[-*•]\s+(.*)$/))) {
      flushPara();
      if (!list || list.type !== "ul") { flushList(); list = { type: "ul", items: [] }; }
      list.items.push(m[1]);
    } else if ((m = line.match(/^\s*\d+[.)、]\s+(.*)$/))) {
      flushPara();
      if (!list || list.type !== "ol") { flushList(); list = { type: "ol", items: [] }; }
      list.items.push(m[1]);
    } else if ((m = line.match(/^>\s?(.*)$/))) {
      flushPara(); flushList(); out.push(`<blockquote>${inlineMd(esc(m[1]))}</blockquote>`);
    } else {
      flushList(); para.push(line);
    }
  }
  if (inCode) out.push("<pre><code>" + esc(code.join("\n")) + "</code></pre>");
  flushPara(); flushList();
  return out.join("");
}

function openModal(html) {
  document.getElementById("modalBody").innerHTML = html;
  document.getElementById("modal").classList.remove("hidden");
}
function closeModal() {
  document.getElementById("modal").classList.add("hidden");
}
document.addEventListener("click", e => {
  if (e.target.id === "modal") closeModal();
  if (e.target.matches("[data-close]")) closeModal();
});

function toast(msg) {
  const el = document.createElement("div");
  el.textContent = msg;
  el.style.cssText = "position:fixed;left:50%;bottom:90px;transform:translateX(-50%);background:rgba(0,0,0,.8);color:#fff;padding:8px 14px;border-radius:8px;z-index:99;font-size:14px;max-width:90vw";
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2200);
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast("已复制"); }
  catch {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove(); toast("已复制");
  }
}
