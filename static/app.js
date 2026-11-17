const layout = document.getElementById("layout");
const historyEl = document.getElementById("history");
const messagesEl = document.getElementById("messages");
const emptyEl = document.getElementById("empty");
const titleEl = document.getElementById("chatTitle");
const totalsEl = document.getElementById("tokenTotals");
const form = document.getElementById("form");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");

const quotaEl = document.getElementById("quota");
let quota = { used: +quotaEl.dataset.used, limit: +quotaEl.dataset.limit };

function renderQuota() {
  const left = Math.max(quota.limit - quota.used, 0);
  quotaEl.textContent = `${quota.used} / ${quota.limit} free messages used`;
  quotaEl.classList.toggle("low", left > 0 && left <= 3);
  quotaEl.classList.toggle("out", left === 0);
  const out = left === 0;
  input.disabled = out;
  sendBtn.disabled = out || busy;
  input.placeholder = out
    ? "Free message limit reached"
    : "Message the assistant…";
  document.getElementById("limitNotice").hidden = !out;
}

let conversations = [];
let currentId = null; // null => unsaved "New chat"
let busy = false;

/* ---------------- helpers ---------------- */
async function api(url, options = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (res.status === 401) {
    window.location.href = "/login"; // session expired
    throw new Error("Please log in again.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `Request failed (${res.status})`);
    err.quota = data.quota;
    throw err;
  }
  return data;
}

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Minimal safe formatting: code blocks, inline code, bold.
function format(text) {
  let html = escapeHtml(text);
  html = html.replace(/```(?:\w+)?\n?([\s\S]*?)```/g, (_, c) => `<pre><code>${c}</code></pre>`);
  html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  return html;
}

function groupLabel(iso) {
  const d = new Date(iso);
  const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
  const days = Math.round((startOfDay(new Date()) - startOfDay(d)) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days <= 7) return "Previous 7 days";
  if (days <= 30) return "Previous 30 days";
  return "Older";
}

const ICON_EDIT =
  '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
const ICON_TRASH =
  '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="m19 6-1 14H6L5 6"/></svg>';

/* ---------------- sidebar ---------------- */
function renderHistory() {
  historyEl.innerHTML = "";
  if (!conversations.length) {
    const p = document.createElement("div");
    p.className = "history-empty";
    p.textContent = "No conversations yet";
    historyEl.appendChild(p);
    return;
  }
  let lastGroup = null;
  for (const c of conversations) {
    const group = groupLabel(c.updated_at);
    if (group !== lastGroup) {
      const label = document.createElement("div");
      label.className = "group-label";
      label.textContent = group;
      historyEl.appendChild(label);
      lastGroup = group;
    }
    historyEl.appendChild(buildItem(c));
  }
}

function buildItem(c) {
  const item = document.createElement("div");
  item.className = "item" + (c.id === currentId ? " active" : "");
  item.dataset.id = c.id;

  const title = document.createElement("div");
  title.className = "title";
  title.textContent = c.title;
  title.title = c.title;

  const actions = document.createElement("div");
  actions.className = "actions";
  const edit = document.createElement("button");
  edit.className = "act";
  edit.title = "Rename";
  edit.innerHTML = ICON_EDIT;
  const del = document.createElement("button");
  del.className = "act del";
  del.title = "Delete";
  del.innerHTML = ICON_TRASH;
  actions.append(edit, del);
  item.append(title, actions);

  item.addEventListener("click", () => openConversation(c.id));
  edit.addEventListener("click", (e) => { e.stopPropagation(); startRename(item, c); });
  del.addEventListener("click", (e) => { e.stopPropagation(); removeConversation(c); });
  return item;
}

function startRename(item, c) {
  const title = item.querySelector(".title");
  const field = document.createElement("input");
  field.className = "rename";
  field.value = c.title;
  field.maxLength = 100;
  item.replaceChild(field, title);
  item.querySelector(".actions").style.display = "none";
  field.focus();
  field.select();

  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const value = field.value.trim();
    if (save && value && value !== c.title) {
      try {
        const r = await api(`/api/conversations/${c.id}`, {
          method: "PATCH",
          body: JSON.stringify({ title: value }),
        });
        c.title = r.title;
        if (c.id === currentId) titleEl.textContent = c.title;
      } catch (err) { alert(err.message); }
    }
    renderHistory();
  };
  field.addEventListener("click", (e) => e.stopPropagation());
  field.addEventListener("keydown", (e) => {
    if (e.key === "Enter") finish(true);
    if (e.key === "Escape") finish(false);
  });
  field.addEventListener("blur", () => finish(true));
}

async function removeConversation(c) {
  if (!confirm(`Delete "${c.title}"? This cannot be undone.`)) return;
  try {
    await api(`/api/conversations/${c.id}`, { method: "DELETE" });
  } catch (err) { alert(err.message); return; }
  conversations = conversations.filter((x) => x.id !== c.id);
  if (c.id === currentId) newChat();
  else renderHistory();
}

async function loadConversations() {
  const data = await api("/api/conversations");
  conversations = data.conversations;
  renderHistory();
}

/* ---------------- messages ---------------- */
function clearMessages() {
  messagesEl.querySelectorAll(".msg").forEach((m) => m.remove());
}

const fmtNum = (n) => Number(n).toLocaleString();

function setTotals(t) {
  if (t && t.total > 0) {
    totalsEl.textContent = `${fmtNum(t.total)} tokens in this chat`;
    totalsEl.title = `Input: ${fmtNum(t.input)} · Output: ${fmtNum(t.output)}`;
  } else {
    totalsEl.textContent = "";
    totalsEl.title = "";
  }
}

function addMessage(role, text, { error = false, html = false, tokens = null } = {}) {
  emptyEl.style.display = "none";
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}${error ? " error" : ""}`;
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = role === "user" ? "You" : "AI";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (html) bubble.innerHTML = text;
  else if (role === "assistant" && !error) bubble.innerHTML = format(text);
  else bubble.textContent = text;
  const col = document.createElement("div");
  col.className = "col";
  col.appendChild(bubble);
  if (tokens && tokens.input_tokens != null && tokens.output_tokens != null) {
    const meta = document.createElement("div");
    meta.className = "tokens";
    const total = tokens.input_tokens + tokens.output_tokens;
    meta.textContent =
      `${fmtNum(total)} tokens · ${fmtNum(tokens.input_tokens)} in · ${fmtNum(tokens.output_tokens)} out`;
    meta.title = "Input tokens include the conversation history sent with this prompt";
    col.appendChild(meta);
  }
  wrap.append(avatar, col);
  messagesEl.appendChild(wrap);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return wrap;
}

function newChat() {
  currentId = null;
  clearMessages();
  emptyEl.style.display = "";
  titleEl.textContent = "New chat";
  setTotals(null);
  renderHistory();
  layout.classList.remove("open");
  input.focus();
}

async function openConversation(id) {
  if (busy || id === currentId) { layout.classList.remove("open"); return; }
  try {
    const data = await api(`/api/conversations/${id}`);
    currentId = id;
    clearMessages();
    emptyEl.style.display = data.messages.length ? "none" : "";
    data.messages.forEach((m) =>
      addMessage(m.role, m.content, {
        tokens: { input_tokens: m.input_tokens, output_tokens: m.output_tokens },
      })
    );
    setTotals(data.totals);
    titleEl.textContent = data.conversation.title;
    renderHistory();
    layout.classList.remove("open");
    messagesEl.scrollTop = messagesEl.scrollHeight;
  } catch (err) {
    alert(err.message);
    loadConversations();
  }
}

async function send(text) {
  text = text.trim();
  if (!text || busy || quota.used >= quota.limit) return;

  busy = true;
  sendBtn.disabled = true;
  addMessage("user", text);
  input.value = "";
  autoGrow();

  const typing = addMessage(
    "assistant",
    '<div class="typing"><span></span><span></span><span></span></div>',
    { html: true }
  );

  try {
    const data = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message: text, conversation_id: currentId }),
    });
    typing.remove();
    addMessage("assistant", data.reply, { tokens: data.usage });
    setTotals(data.totals);

    currentId = data.conversation.id;
    titleEl.textContent = data.conversation.title;
    // Move (or add) this conversation to the top of the list.
    conversations = conversations.filter((c) => c.id !== currentId);
    conversations.unshift(data.conversation);
    renderHistory();
    quota = { used: data.quota.used, limit: data.quota.limit };
  } catch (err) {
    typing.remove();
    addMessage("assistant", err.message, { error: true });
    if (err.quota) quota = { used: err.quota.used, limit: err.quota.limit };
  } finally {
    busy = false;
    renderQuota();
    if (!input.disabled) input.focus();
  }
}

function autoGrow() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
}

/* ---------------- events ---------------- */
form.addEventListener("submit", (e) => { e.preventDefault(); send(input.value); });
input.addEventListener("input", autoGrow);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input.value); }
});
document.querySelectorAll(".chip").forEach((c) =>
  c.addEventListener("click", () => send(c.textContent))
);
document.getElementById("newChat").addEventListener("click", () => { if (!busy) newChat(); });
document.getElementById("menu").addEventListener("click", () => layout.classList.toggle("open"));
document.getElementById("backdrop").addEventListener("click", () => layout.classList.remove("open"));

renderQuota();
loadConversations().catch((err) => console.error(err));
