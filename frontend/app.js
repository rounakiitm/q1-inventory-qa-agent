/* Inventory Intelligence — app logic (independent of other projects) */
// Always same-origin (FastAPI serves /app). Avoid localhost vs 127.0.0.1 mismatch.
const API = "";
const $ = (s) => document.querySelector(s);
const LS_SID = "q1_sid";
const LS_MODEL = "q1_model";
const LS_TOKEN = "q1_sb_token";
const LS_REFRESH = "q1_sb_refresh";
const LS_EMAIL = "q1_sb_email";
let SID = localStorage.getItem(LS_SID) || null;
let totalTokens = 0;
let SELECTED_MODEL = localStorage.getItem(LS_MODEL) || "";
let LIBRARY = [];          // all uploaded docs
let SESSION_DOCS = [];     // docs attached to current session
let PREVIEW_ID = null;     // which library doc is shown on Dataset page
let AUTH_REQUIRED = false;
let ACCESS_TOKEN = localStorage.getItem(LS_TOKEN) || "";
let AUTH_READY = false;
let chatAbort = null;       // AbortController for in-flight SSE
let chatStreaming = false;

/* ---------- routing ---------- */
const pages = ["chat", "data", "notebook", "about"];
function route() {
  const page = (location.hash.replace("#/", "") || "chat");
  pages.forEach((p) => {
    document.getElementById("page-" + p).classList.toggle("active", p === page);
    document.querySelector(`[data-page="${p}"]`).classList.toggle("active", p === page);
  });
  if (page === "data") loadData();
  if (page === "notebook") loadNotebook();
  if (page === "chat") refreshSessionDocs();
}
window.addEventListener("hashchange", route);

/* ---------- auth ---------- */
function authHeaders(extra = {}) {
  const h = { ...extra };
  if (ACCESS_TOKEN) h["Authorization"] = `Bearer ${ACCESS_TOKEN}`;
  return h;
}

function setSessionTokens(access, refresh, email) {
  ACCESS_TOKEN = access || "";
  if (access) localStorage.setItem(LS_TOKEN, access);
  else localStorage.removeItem(LS_TOKEN);
  if (refresh) localStorage.setItem(LS_REFRESH, refresh);
  else localStorage.removeItem(LS_REFRESH);
  if (email) localStorage.setItem(LS_EMAIL, email);
  const signedIn = AUTH_REQUIRED && !!ACCESS_TOKEN;
  const row = $("#authUserRow");
  if (row) {
    row.hidden = !signedIn;
    $("#authEmailLabel").textContent = email || localStorage.getItem(LS_EMAIL) || "";
  }
  const topOut = $("#authSignOutTop");
  if (topOut) topOut.hidden = !signedIn;
}

function showAuthGate(show) {
  const gate = $("#authGate");
  gate.hidden = !show;
  // belt-and-suspenders: some CSS `display:` rules override [hidden]
  gate.style.display = show ? "grid" : "none";
  $("#appShell").style.display = show ? "none" : "";
}

async function authRequest(path, body) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 25000);
  try {
    const r = await fetch(API + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const detail = data.detail;
      const msg = (typeof detail === "string" ? detail : null)
        || data.error_description || data.msg || data.error || r.statusText
        || "auth failed";
      throw new Error(msg);
    }
    return data;
  } catch (err) {
    if (err.name === "AbortError") throw new Error("Sign-in timed out — check the API is running");
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

async function signIn(email, password) {
  if (!AUTH_READY) throw new Error("Auth not ready yet — wait a moment and retry");
  const data = await authRequest("/auth/login", { email, password });
  if (!data.access_token) throw new Error("No access token returned");
  setSessionTokens(data.access_token, data.refresh_token, email);
  showAuthGate(false);
  await bootApp();
}

async function signUp(email, password) {
  if (!AUTH_READY) throw new Error("Auth not ready yet — wait a moment and retry");
  const data = await authRequest("/auth/signup", { email, password });
  if (data.access_token) {
    setSessionTokens(data.access_token, data.refresh_token, email);
    showAuthGate(false);
    await bootApp();
    return;
  }
  $("#authMsg").className = "uploadmsg okmsg";
  $("#authMsg").textContent = "Account created — check your email to confirm, then sign in.";
}

function signOut() {
  setSessionTokens("", "", "");
  localStorage.removeItem(LS_SID);
  SID = null;
  if (AUTH_REQUIRED) showAuthGate(true);
}

$("#authForm").onsubmit = async (e) => {
  e.preventDefault();
  const email = $("#authEmail").value.trim();
  const password = $("#authPassword").value;
  const msg = $("#authMsg");
  msg.className = "uploadmsg muted";
  msg.textContent = "Signing in…";
  $("#authSignIn").disabled = true;
  try {
    await signIn(email, password);
    msg.textContent = "";
  } catch (err) {
    msg.className = "uploadmsg errmsg";
    msg.textContent = err.message;
  }
  $("#authSignIn").disabled = false;
};
$("#authSignUp").onclick = async () => {
  const email = $("#authEmail").value.trim();
  const password = $("#authPassword").value;
  const msg = $("#authMsg");
  if (!email || password.length < 6) {
    msg.className = "uploadmsg errmsg";
    msg.textContent = "Enter email and a password (6+ chars).";
    return;
  }
  msg.className = "uploadmsg muted";
  msg.textContent = "Creating account…";
  try {
    await signUp(email, password);
  } catch (err) {
    msg.className = "uploadmsg errmsg";
    msg.textContent = err.message;
  }
};
$("#authSignOut").onclick = signOut;
$("#authSignOutTop").onclick = signOut;

/* ---------- api ---------- */
async function api(path, opts = {}) {
  const headers = authHeaders(opts.headers || {});
  const r = await fetch(API + path, { ...opts, headers });
  if (r.status === 401 && AUTH_REQUIRED) {
    signOut();
    throw new Error("session expired — sign in again");
  }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

function setSession(sid) {
  SID = sid;
  localStorage.setItem(LS_SID, sid);
  $("#nbDownload").href = "#";
  $("#sessionHint").textContent = `session ${sid}`;
  highlightActiveSession();
}

$("#nbDownload").onclick = async (e) => {
  e.preventDefault();
  if (!SID) return;
  try {
    const r = await fetch(`${API}/sessions/${SID}/notebook`, { headers: authHeaders() });
    if (!r.ok) throw new Error(r.statusText);
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `session_${SID}.ipynb`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    alert("Notebook export failed: " + err.message);
  }
};

function highlightActiveSession() {
  document.querySelectorAll(".sessionitem").forEach((el) => {
    el.classList.toggle("active", el.dataset.sid === SID);
  });
}

async function refreshSessionList() {
  const box = $("#sessionList");
  try {
    const sessions = await api("/sessions?limit=40");
    if (!sessions.length) {
      box.innerHTML = `<div class="muted">No sessions yet</div>`;
      return;
    }
    box.innerHTML = sessions.map((s) => {
      const when = (s.last_activity || s.created_at || "").replace("T", " ").slice(0, 16);
      return `<button type="button" class="sessionitem" data-sid="${esc(s.session_id)}" title="${esc(s.session_id)}">
        <span class="sprev">${esc(s.preview || "New session")}</span>
        <span class="smeta">${esc(when)} · ${s.n_messages} msg</span>
      </button>`;
    }).join("");
    box.querySelectorAll(".sessionitem").forEach((btn) => {
      btn.onclick = () => openSession(btn.dataset.sid);
    });
    highlightActiveSession();
  } catch (err) {
    box.innerHTML = `<div class="muted">⚠ ${esc(err.message)}</div>`;
  }
}

async function newSession() {
  const info = await api("/sessions", { method: "POST" });
  setSession(info.session_id);
  totalTokens = 0;
  SESSION_DOCS = [];
  $("#usageTotals").textContent = "";
  $("#chat").innerHTML = "";
  // Auto-attach every library workbook so chat works immediately; user can detach.
  try {
    if (!LIBRARY.length) await loadLibrary();
    if (LIBRARY.length) {
      await api(`/sessions/${info.session_id}/documents`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ doc_ids: LIBRARY.map((d) => d.id) }),
      });
    }
  } catch (_) {}
  await refreshSessionList();
  await refreshSessionDocs();
  const names = SESSION_DOCS.map((d) => `<code>@${esc(d.name)}</code>`).join(", ");
  addMsg(
    "bot",
    names
      ? `👋 Ready. Attached: ${names}. Ask a question, or type <b>@</b> to focus a document. Detach any you don't want used.`
      : `👋 No workbooks in the library yet — upload one on the <b>Dataset</b> page, then come back.`
  );
}
$("#newSession").onclick = newSession;

async function openSession(sid) {
  if (!sid) return;
  try {
    const msgs = await api(`/sessions/${sid}/messages`);
    setSession(sid);
    totalTokens = 0;
    $("#usageTotals").textContent = "";
    $("#chat").innerHTML = "";
    if (!msgs.length) {
      addMsg("bot", "👋 Empty session — ask a question about the inventory.");
    } else {
      for (const m of msgs) {
        if (m.role === "user") addMsg("user", esc(m.content));
        else {
          const bot = addMsg("bot", "");
          const body = document.createElement("div");
          body.className = "answerbody";
          body.innerHTML = md(m.content);
          bot.appendChild(body);
          if (m.meta?.incomplete) attachContinue(bot, m.meta.incomplete_reason);
        }
      }
    }
    location.hash = "#/chat";
    await refreshSessionList();
    await refreshSessionDocs();
  } catch (err) {
    // Stale localStorage id — start fresh.
    localStorage.removeItem(LS_SID);
    await newSession();
  }
}

/* ---------- documents ---------- */
async function loadLibrary() {
  LIBRARY = await api("/documents");
  renderLibrary();
  fillAttachSelect();
  if (!PREVIEW_ID && LIBRARY.length) PREVIEW_ID = LIBRARY[0].id;
}

function fillAttachSelect() {
  const sel = $("#attachSelect");
  const attached = new Set(SESSION_DOCS.map((d) => d.id));
  const available = LIBRARY.filter((d) => !attached.has(d.id));
  sel.innerHTML = `<option value="">+ Attach document…</option>` +
    available.map((d) => `<option value="${esc(d.id)}">@${esc(d.name)} · ${d.rows}×${d.columns}</option>`).join("");
}

function renderLibrary() {
  const box = $("#docLibrary");
  if (!LIBRARY.length) {
    box.innerHTML = `<div class="muted">No workbooks yet — upload one above.</div>`;
    return;
  }
  box.innerHTML = LIBRARY.map((d) => `
    <div class="doccard ${d.id === PREVIEW_ID ? "active" : ""}" data-id="${esc(d.id)}">
      <div class="doccard-main">
        <div class="doccard-title">@${esc(d.name)}</div>
        <div class="muted">${esc(d.filename)} · ${d.rows} rows · ${d.columns} cols</div>
      </div>
      <div class="doccard-actions">
        <button type="button" class="ghost sm previewbtn">Preview</button>
        <button type="button" class="ghost sm attachbtn">Attach</button>
        <button type="button" class="ghost sm delbtn">Delete</button>
      </div>
    </div>`).join("");
  box.querySelectorAll(".doccard").forEach((card) => {
    const id = card.dataset.id;
    card.querySelector(".previewbtn").onclick = () => { PREVIEW_ID = id; loadData(); renderLibrary(); };
    card.querySelector(".attachbtn").onclick = () => attachDoc(id);
    card.querySelector(".delbtn").onclick = () => deleteDoc(id);
  });
}

async function attachDoc(docId) {
  if (!SID || !docId) return;
  await api(`/sessions/${SID}/documents`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ doc_id: docId }),
  });
  await refreshSessionDocs();
}

async function detachDoc(docId) {
  if (!SID || !docId) return;
  await api(`/sessions/${SID}/documents/${docId}`, { method: "DELETE" });
  await refreshSessionDocs();
}

async function deleteDoc(docId) {
  if (!confirm("Remove this workbook from the library?")) return;
  await api(`/documents/${docId}`, { method: "DELETE" });
  if (PREVIEW_ID === docId) PREVIEW_ID = null;
  await loadLibrary();
  await refreshSessionDocs();
  await loadData();
}

async function refreshSessionDocs() {
  if (!SID) {
    SESSION_DOCS = [];
    $("#sessionDocs").innerHTML = `<span class="muted">No session</span>`;
    fillAttachSelect();
    return;
  }
  try {
    const data = await api(`/sessions/${SID}/documents`);
    SESSION_DOCS = data.documents || [];
  } catch (_) {
    SESSION_DOCS = [];
  }
  const box = $("#sessionDocs");
  if (!SESSION_DOCS.length) {
    box.innerHTML = `<span class="muted">None yet — attach from the library or type @Name</span>`;
  } else {
    box.innerHTML = SESSION_DOCS.map((d) =>
      `<span class="docchip" data-name="@${esc(d.name)}" title="${esc(d.filename)}">@${esc(d.name)}
        <button type="button" class="x" data-id="${esc(d.id)}" title="Detach">×</button></span>`
    ).join("");
    box.querySelectorAll(".x").forEach((btn) => {
      btn.onclick = (e) => { e.stopPropagation(); detachDoc(btn.dataset.id); };
    });
    box.querySelectorAll(".docchip").forEach((chip) => {
      chip.onclick = () => {
        const name = chip.dataset.name;
        const input = $("#askInput");
        input.value = (input.value + (input.value && !input.value.endsWith(" ") ? " " : "") + name + " ");
        input.focus();
      };
    });
  }
  fillAttachSelect();
}
$("#attachSelect").onchange = async () => {
  const id = $("#attachSelect").value;
  if (id) await attachDoc(id);
  $("#attachSelect").value = "";
};

/* @mention autocomplete */
function hideMentions() { $("#mentionMenu").hidden = true; $("#mentionMenu").innerHTML = ""; }
function showMentions(filter) {
  const q = (filter || "").toLowerCase();
  const hits = LIBRARY.filter((d) => !q || d.name.toLowerCase().includes(q) || d.filename.toLowerCase().includes(q));
  const menu = $("#mentionMenu");
  if (!hits.length) { hideMentions(); return; }
  menu.hidden = false;
  menu.innerHTML = hits.slice(0, 8).map((d) =>
    `<button type="button" class="mentionitem" data-name="${esc(d.name)}" data-id="${esc(d.id)}">
      <b>@${esc(d.name)}</b><span class="muted">${esc(d.filename)}</span>
     </button>`
  ).join("");
  menu.querySelectorAll(".mentionitem").forEach((btn) => {
    btn.onclick = async () => {
      const input = $("#askInput");
      const v = input.value;
      const at = v.lastIndexOf("@");
      input.value = v.slice(0, at) + "@" + btn.dataset.name + " ";
      hideMentions();
      // ensure attached for this session
      if (!SESSION_DOCS.some((d) => d.id === btn.dataset.id)) await attachDoc(btn.dataset.id);
      input.focus();
    };
  });
}
$("#askInput").addEventListener("input", () => {
  const v = $("#askInput").value;
  const m = v.match(/(?:^|\s)@([A-Za-z0-9_-]*)$/);
  if (m) showMentions(m[1]);
  else hideMentions();
});
$("#askInput").addEventListener("keydown", (e) => {
  if (e.key === "Escape") hideMentions();
});

async function loadModels() {
  const sel = $("#modelSelect");
  try {
    const data = await api("/models");
    const models = data.models || [];
    const ids = new Set(models.map((m) => m.id));
    // Drop retired models stuck in localStorage (e.g. gemini-2.5-flash-lite).
    let preferred = SELECTED_MODEL;
    if (!preferred || !ids.has(preferred)) preferred = data.default || models[0]?.id;
    sel.innerHTML = models.map((m) =>
      `<option value="${esc(m.id)}">${esc(m.id)}${m.tier === "lite" ? " · cheapest" : ""}</option>`
    ).join("");
    if (preferred && ids.has(preferred)) sel.value = preferred;
    else if (data.default && ids.has(data.default)) sel.value = data.default;
    SELECTED_MODEL = sel.value;
    localStorage.setItem(LS_MODEL, SELECTED_MODEL);
    $("#modelBadge").textContent = SELECTED_MODEL;
  } catch (_) {
    sel.innerHTML = `<option value="gemini-3.1-flash-lite">gemini-3.1-flash-lite</option>`;
    SELECTED_MODEL = sel.value;
    localStorage.setItem(LS_MODEL, SELECTED_MODEL);
  }
}
$("#modelSelect").onchange = () => {
  SELECTED_MODEL = $("#modelSelect").value;
  localStorage.setItem(LS_MODEL, SELECTED_MODEL);
  $("#modelBadge").textContent = SELECTED_MODEL;
};

/* ---------- chat ---------- */
function addMsg(kind, html) {
  const div = document.createElement("div");
  div.className = "msg " + kind;
  div.innerHTML = html;
  $("#chat").appendChild(div);
  $("#chat").scrollTop = $("#chat").scrollHeight;
  return div;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}
function md(s) {
  let t = esc(s);
  t = t.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  t = t.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>");
  t = t.replace(/`([^`]+)`/g, "<code>$1</code>");
  t = t.replace(/\n/g, "<br/>");
  return t;
}

function setStreamingUI(on) {
  chatStreaming = on;
  const askBtn = $("#askBtn");
  const stopBtn = $("#stopBtn");
  if (askBtn) {
    askBtn.disabled = on;
    askBtn.hidden = on;
  }
  if (stopBtn) stopBtn.hidden = !on;
  const input = $("#askInput");
  if (input) input.disabled = on;
}

async function streamChat({ question = "", continueFromIncomplete = false, bot } = {}) {
  chatAbort = new AbortController();
  setStreamingUI(true);
  try {
    const resp = await fetch(`${API}/sessions/${SID}/messages`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        question: question || (continueFromIncomplete ? "continue" : ""),
        model: SELECTED_MODEL || undefined,
        continue_from_incomplete: !!continueFromIncomplete,
      }),
      signal: chatAbort.signal,
    });
    if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || resp.statusText);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "", ev = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
        for (const line of chunk.split("\n")) {
          if (line.startsWith("event: ")) ev = line.slice(7);
          else if (line.startsWith("data: ")) handleEvent(ev, JSON.parse(line.slice(6)), bot);
        }
      }
    }
  } catch (err) {
    if (err.name === "AbortError") {
      // Stop was pressed — backend saves incomplete state; Continue appears via answer event if it arrives.
      if (!bot.querySelector(".answerbody") && !bot.querySelector(".continuebtn")) {
        bot.querySelector(".thinking")?.remove();
        const body = document.createElement("div");
        body.className = "answerbody";
        body.innerHTML = `<div class="incomplete-banner">Stopped. Tap Continue to resume from this point.</div>`;
        bot.appendChild(body);
        attachContinue(bot, "stopped");
      }
    } else {
      bot.innerHTML = `<span style="color:var(--red)">⚠ ${esc(err.message)}</span>`;
    }
  } finally {
    chatAbort = null;
    setStreamingUI(false);
  }
}

$("#askForm").onsubmit = async (e) => {
  e.preventDefault();
  const q = $("#askInput").value.trim();
  if (!q || !SID || chatStreaming) return;
  $("#askInput").value = "";
  addMsg("user", esc(q));
  const bot = addMsg("bot", '<span class="thinking muted">thinking…</span>');
  await streamChat({ question: q, bot });
};

$("#stopBtn").onclick = async () => {
  if (!SID || !chatStreaming) return;
  try {
    await fetch(`${API}/sessions/${SID}/cancel`, {
      method: "POST",
      headers: authHeaders(),
    });
  } catch (_) { /* ignore */ }
  if (chatAbort) chatAbort.abort();
};

function attachContinue(bot, reason) {
  bot.querySelector(".continuebtn")?.remove();
  const banner = bot.querySelector(".incomplete-banner");
  if (!banner) {
    const b = document.createElement("div");
    b.className = "incomplete-banner";
    b.textContent =
      reason === "stopped"
        ? "Generation stopped. You can continue from where it left off."
        : "Hit the reasoning-step limit. Continue to keep going from this point.";
    (bot.querySelector(".answerbody") || bot).appendChild(b);
  }
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "continuebtn";
  btn.textContent = "Continue ▸";
  btn.onclick = async () => {
    if (chatStreaming || !SID) return;
    btn.disabled = true;
    btn.textContent = "Continuing…";
    // Brief wait so a just-stopped agent can persist resume state.
    await new Promise((r) => setTimeout(r, 400));
    const next = addMsg("bot", '<span class="thinking muted">continuing…</span>');
    await streamChat({ continueFromIncomplete: true, bot: next });
    btn.remove();
  };
  (bot.querySelector(".answerbody") || bot).appendChild(btn);
}

const TOOL_META = {
  run_dataframe_code: {
    label: "Ran pandas code",
    lang: "python",
    icon: "◆",
    ctx: "Sandbox on session-attached workbooks. Preloaded: <code>df</code> (primary), <code>dfs</code> (all attached), <code>pd</code>, <code>np</code>. No network / filesystem / <code>os</code> / <code>globals</code>. Assign the final value to <code>result</code>.",
  },
  web_search: {
    label: "Web search",
    lang: "search",
    icon: "◎",
    ctx: "Grounded web lookup for definitions / domain context. Results are labelled and not treated as dataset fact.",
  },
  make_chart: {
    label: "Queued chart",
    lang: "plotly",
    icon: "▣",
    ctx: "Builds a Plotly chart from numbers already computed via code; rendered under the answer.",
  },
};

function ensureToolStack(bot) {
  if (bot.querySelector(".thinking")) bot.innerHTML = "";
  let stack = bot.querySelector(".toolstack");
  if (!stack) {
    stack = document.createElement("div");
    stack.className = "toolstack";
    bot.appendChild(stack);
  }
  return stack;
}

function firstLine(s, n = 72) {
  const line = String(s || "").split(/\r?\n/).find((l) => l.trim()) || "";
  return line.length > n ? line.slice(0, n - 1) + "…" : line;
}

function lineCount(s) {
  const t = String(s || "");
  if (!t.trim()) return 0;
  return t.split(/\r?\n/).length;
}

function highlightPy(src) {
  // Escape → park strings/comments → keyword/id wraps → restore parks →
  // number highlight only on plain text (never inside tags / parked slots).
  // IMPORTANT: restore before \b\d+ or placeholder indices become visible
  // "line numbers" and column names like 'Units Sold' vanish.
  let t = esc(src);
  const slots = [];
  const park = (html) => {
    const i = slots.length;
    slots.push(html);
    return `\u0000${i}\u0000`;
  };
  t = t.replace(/(^|\n)([ \t]*#.*)/g, (_, a, b) => a + park(`<span class="cmt">${b}</span>`));
  t = t.replace(/("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')/g, (m) => park(`<span class="str">${m}</span>`));
  t = t.replace(
    /\b(def|return|if|elif|else|for|while|in|not|and|or|True|False|None|import|from|as|with|class|try|except|raise|lambda|pass|break|continue)\b/g,
    '<span class="kw">$1</span>'
  );
  t = t.replace(/\b(df|dfs|pd|np|result)\b/g, '<span class="id">$1</span>');
  t = t.replace(/\u0000(\d+)\u0000/g, (_, i) => slots[Number(i)]);
  t = t.replace(/(<[^>]+>)|([^<]+)/g, (m, tag, text) => {
    if (tag) return tag;
    return text.replace(/\b(\d+\.?\d*)\b/g, '<span class="num">$1</span>');
  });
  return t;
}

function codePanel(label, lang, raw, { highlight = false } = {}) {
  const lines = lineCount(raw);
  const body = highlight ? highlightPy(raw) : esc(raw);
  const wrap = document.createElement("div");
  wrap.className = "toolsec";
  // Collapse by default when taller than a few lines so the user can Expand.
  const collapsible = lines > 6;
  wrap.innerHTML = `
    <div class="toolbar">
      <span class="toollabel">${esc(label)}</span>
      <span class="langpill">${esc(lang)}${lines ? " · " + lines + " line" + (lines === 1 ? "" : "s") : ""}</span>
      ${collapsible ? `<button type="button" class="expandbtn">Expand</button>` : ""}
      <button type="button" class="copybtn">Copy</button>
    </div>
    <pre class="toolcode${collapsible ? " is-collapsed" : ""}">${body}</pre>`;
  const pre = wrap.querySelector(".toolcode");
  pre._raw = raw;
  wrap.querySelector(".copybtn").onclick = async (e) => {
    e.preventDefault();
    e.stopPropagation();
    const btn = e.currentTarget;
    try {
      await navigator.clipboard.writeText(raw);
      btn.textContent = "Copied";
      setTimeout(() => { btn.textContent = "Copy"; }, 1200);
    } catch (_) {
      btn.textContent = "Failed";
      setTimeout(() => { btn.textContent = "Copy"; }, 1200);
    }
  };
  const expandBtn = wrap.querySelector(".expandbtn");
  if (expandBtn) {
    expandBtn.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      const open = pre.classList.toggle("is-expanded");
      pre.classList.toggle("is-collapsed", !open);
      expandBtn.textContent = open ? "Collapse" : "Expand";
      if (open) pre.scrollIntoView({ block: "nearest", behavior: "smooth" });
    };
  }
  return wrap;
}

function handleEvent(ev, d, bot) {
  if (ev === "tool_call") {
    const stack = ensureToolStack(bot);
    const step = stack.querySelectorAll(".toolblock").length + 1;
    const meta = TOOL_META[d.name] || { label: d.name, lang: "tool", icon: "⚙", ctx: "Tool call in the agent loop." };
    const block = document.createElement("details");
    block.className = "toolblock running";
    block.dataset.name = d.name || "";
    const args = d.args || {};
    const code = typeof args.code === "string" ? args.code : "";
    const query = typeof args.query === "string" ? args.query : "";
    const title = args.title || "";
    const preview = firstLine(code || query || title || d.name);

    block.innerHTML = `
      <summary>
        <span class="chev">▸</span>
        <span class="step">#${step}</span>
        <span class="ticon">${meta.icon}</span>
        <span class="tname">${esc(meta.label)}</span>
        <span class="tpill">${esc(meta.lang)}</span>
        <span class="tprev muted">${esc(preview)}</span>
        <span class="tmeta"><span class="spin"></span> running</span>
      </summary>
      <div class="toolbody"></div>`;
    const toolbody = block.querySelector(".toolbody");
    const ctx = document.createElement("div");
    ctx.className = "toolctx";
    ctx.innerHTML = `<span class="ctxbadge">Context</span><span>${meta.ctx}</span>`;
    toolbody.appendChild(ctx);
    if (code) toolbody.appendChild(codePanel("Code executed", "python", code, { highlight: true }));
    else if (query) toolbody.appendChild(codePanel("Search query", "search", query));
    else if (title || args.rationale) toolbody.appendChild(codePanel("Chart plan", "plotly", `${title || "(untitled)"}${args.rationale ? "\n" + args.rationale : ""}`));
    else if (Object.keys(args).length) toolbody.appendChild(codePanel("Arguments", "json", JSON.stringify(args, null, 2)));
    const outWrap = document.createElement("div");
    outWrap.className = "toolsec toolout-wrap";
    outWrap.hidden = true;
    outWrap.innerHTML = `<div class="toolbar"><span class="toollabel">Output</span><span class="langpill outpill">result</span></div><pre class="toolout"></pre>`;
    toolbody.appendChild(outWrap);
    stack.appendChild(block);
  } else if (ev === "tool_result") {
    const blocks = bot.querySelectorAll(".toolblock");
    const block = [...blocks].reverse().find((b) => b.dataset.name === d.name && b.classList.contains("running"))
      || [...blocks].reverse().find((b) => b.dataset.name === d.name)
      || blocks[blocks.length - 1];
    if (!block) return;
    block.classList.remove("running");
    block.classList.add(d.ok ? "ok" : "err");
    const meta = block.querySelector(".tmeta");
    if (meta) meta.innerHTML = `<span class="statusdot"></span>${d.ok ? "ok" : "failed"} · ${d.latency_ms}ms`;
    if (d.summary) {
      const wrap = block.querySelector(".toolout-wrap");
      const out = block.querySelector(".toolout");
      if (wrap && out) { wrap.hidden = false; out.textContent = d.summary; }
    }
    // Auto-expand failures so the user sees the error without clicking.
    if (!d.ok) block.open = true;
  } else if (ev === "answer") {
    renderAnswer(d, bot);
  } else if (ev === "error") {
    bot.innerHTML = `<span style="color:var(--red)">⚠ ${esc(d.message)}</span>`;
  }
}

function renderAnswer(a, bot) {
  // Keep streamed tool blocks; only replace/append the answer body.
  bot.querySelector(".thinking")?.remove();
  bot.querySelector(".continuebtn")?.remove();
  let body = bot.querySelector(".answerbody");
  if (!body) {
    body = document.createElement("div");
    body.className = "answerbody";
    bot.appendChild(body);
  }
  const html = [];
  html.push(md(a.answer).replace(/\[(\d+)\]/g, '<span class="cite">[$1]</span>'));
  if (a.sources?.length) html.push(`<h4>Web sources</h4>` + a.sources.map((s) => `<a href="${esc(s.uri)}" target="_blank" style="color:var(--cyan);font-size:13px">${esc(s.title || s.uri)}</a>`).join("<br/>"));
  body.innerHTML = html.join("");
  (a.charts || []).forEach((ch) => {
    const box = document.createElement("div"); box.className = "chartbox"; body.appendChild(box);
    const spec = ch.spec || {};
    Plotly.newPlot(box, spec.data || [], { title: { text: ch.title, font: { color: "#e6edf7" } }, ...spec.layout, paper_bgcolor: "#0d1526", plot_bgcolor: "#0d1526", font: { color: "#8fa3c0" } }, { responsive: true });
  });
  if (a.incomplete) attachContinue(bot, a.incomplete_reason);
  const usage = a.usage || {};
  const add =
    (usage.prompt_tokens || 0) + (usage.completion_tokens || 0) ||
    usage.total_tokens ||
    0;
  totalTokens += add;
  $("#usageTotals").textContent = `session: ${totalTokens} tokens`;
  refreshSessionList();
  refreshSessionDocs(); // pick up any @-auto-attached docs
}

/* ---------- data page ---------- */
async function loadData() {
  try { await loadLibrary(); } catch (_) {}
  if (!PREVIEW_ID && LIBRARY.length) PREVIEW_ID = LIBRARY[0].id;
  if (!PREVIEW_ID) {
    $("#dataFile").textContent = "no documents";
    $("#dataMeta").innerHTML = "";
    $("#dataTable").innerHTML = `<div class="muted">Upload a workbook to preview it.</div>`;
    $("#dataAudit").textContent = "";
    return;
  }
  $("#dataMeta").innerHTML = `<div class="card"><b>…</b><span>loading</span></div>`;
  $("#dataTable").innerHTML = `<div class="muted">Loading preview…</div>`;
  $("#dataAudit").textContent = "…";
  try {
    const d = await api(`/documents/${PREVIEW_ID}/preview?rows=15`);
    const violations = d.audit?.integrity?.violations ?? 0;
    $("#dataFile").textContent = `@${d.name}`;
    $("#dataMeta").innerHTML = `
      <div class="card"><b>${d.shape[0]}</b><span>rows</span></div>
      <div class="card"><b>${d.shape[1]}</b><span>columns</span></div>
      <div class="card"><b>${violations}</b><span>integrity violations</span></div>`;
    $("#dataAudit").textContent = JSON.stringify(d.audit, null, 2);
    const cols = d.columns || [];
    const rows = d.rows || [];
    $("#dataTable").innerHTML =
      `<table><thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>` +
      rows.map((r) => `<tr>${cols.map((c) => `<td>${esc(r[c] ?? "")}</td>`).join("")}</tr>`).join("") +
      `</tbody></table>` +
      `<div class="muted" style="margin-top:8px">showing ${rows.length} of ${d.total_rows ?? "?"} rows</div>`;
  } catch (err) {
    $("#dataFile").textContent = "error";
    $("#dataMeta").innerHTML = `<div class="card"><b style="color:var(--red)">err</b><span>${esc(err.message)}</span></div>`;
    $("#dataTable").innerHTML = `<div style="color:var(--red)">⚠ ${esc(err.message)}</div>`;
    $("#dataAudit").textContent = err.message;
  }
}

$("#uploadForm").onsubmit = async (e) => {
  e.preventDefault();
  const input = $("#uploadFile");
  const file = input.files?.[0];
  const msg = $("#uploadMsg");
  const btn = $("#uploadBtn");
  if (!file) {
    msg.className = "uploadmsg errmsg";
    msg.textContent = "Choose an .xlsx / .xls file first.";
    return;
  }
  const name = file.name.toLowerCase();
  if (!name.endsWith(".xlsx") && !name.endsWith(".xls")) {
    msg.className = "uploadmsg errmsg";
    msg.textContent = "Only .xlsx / .xls workbooks are supported.";
    return;
  }
  btn.disabled = true;
  msg.className = "uploadmsg muted";
  msg.textContent = `Uploading ${file.name}…`;
  try {
    const fd = new FormData();
    fd.append("file", file, file.name);
    const r = await fetch(`${API}/documents`, { method: "POST", headers: authHeaders(), body: fd });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || r.statusText);
    msg.className = "uploadmsg okmsg";
    msg.textContent = `Added @${body.name} · ${body.rows} rows × ${body.columns} cols (library kept previous docs)`;
    input.value = "";
    PREVIEW_ID = body.id;
    await loadLibrary();
    await loadData();
  } catch (err) {
    msg.className = "uploadmsg errmsg";
    msg.textContent = "Upload failed: " + err.message;
  }
  btn.disabled = false;
};

function cellSource(src) {
  if (Array.isArray(src)) return src.join("");
  return src || "";
}
function cellOutput(outputs) {
  const o = outputs?.[0];
  if (!o) return "";
  if (typeof o.text === "string") return o.text;
  if (Array.isArray(o.text)) return o.text.join("");
  return String(o.text ?? "");
}

/* ---------- notebook page ---------- */
async function loadNotebook() {
  if (!SID) return;
  const box = $("#notebook");
  box.innerHTML = `<div class="muted">Loading notebook…</div>`;
  let toolRuns = [];
  try {
    const r = await fetch(`${API}/sessions/${SID}/notebook`, { headers: authHeaders() });
    if (!r.ok) throw new Error(r.statusText);
    const j = await r.json();
    toolRuns = (j.cells || [])
      .filter((c) => c.cell_type === "code")
      .map((c) => ({ src: cellSource(c.source), out: cellOutput(c.outputs) }));
  } catch (err) {
    box.innerHTML = `<div style="color:var(--red)">⚠ ${esc(err.message)}</div>`;
    return;
  }
  box.innerHTML = toolRuns.length
    ? toolRuns.map((c) => `<div class="nbcell"><pre class="src">${esc(c.src)}</pre><pre class="out">${esc(c.out)}</pre></div>`).join("")
    : '<div class="empty">No code executed yet in this session — ask a question first.</div>';
}

/* ---------- boot ---------- */
async function bootApp() {
  // Parallel: models + library + session list (was a slow waterfall).
  await Promise.all([
    loadModels().catch(() => { $("#modelBadge").textContent = "backend offline"; }),
    loadLibrary().catch(() => {}),
    refreshSessionList().catch(() => {}),
  ]);
  route();
  if (SID) {
    try {
      await openSession(SID);
    } catch (_) {
      await newSession();
    }
  } else {
    await newSession();
  }
}

(async () => {
  try {
    const cfg = await fetch(API + "/auth/config").then((r) => r.json());
    AUTH_REQUIRED = !!cfg.auth_required;
  } catch (_) {
    AUTH_REQUIRED = false;
  }
  AUTH_READY = true;
  if (AUTH_REQUIRED && !ACCESS_TOKEN) {
    showAuthGate(true);
    return;
  }
  if (AUTH_REQUIRED) {
    setSessionTokens(ACCESS_TOKEN, localStorage.getItem(LS_REFRESH), localStorage.getItem(LS_EMAIL));
    try {
      await api("/auth/me");
    } catch (_) {
      setSessionTokens("", "", "");
      showAuthGate(true);
      return;
    }
  }
  showAuthGate(false);
  await bootApp();
})();
