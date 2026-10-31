const $ = (id) => document.getElementById(id);
const input = $("messageInput"), sendButton = $("sendButton"), messagesEl = $("messages");
const MAX = 3000, MODEL = "qwen2.5:1.5b";
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
let history = [], busy = false, toastTimer;

const esc = (v) => String(v).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = (t) => esc(t).replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>").replace(/\n/g, "<br>");
const timeNow = () => new Intl.DateTimeFormat(undefined, {hour:"numeric", minute:"2-digit"}).format(new Date());
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function toast(msg) {
  const el = $("toast"); el.textContent = msg; el.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

/* ---------- ambient interactions ---------- */
addEventListener("pointermove", e => {
  const r = document.documentElement.style;
  r.setProperty("--mx", e.clientX + "px"); r.setProperty("--my", e.clientY + "px");
}, {passive:true});

function bindTilt(el) {
  el.addEventListener("pointermove", e => {
    const b = el.getBoundingClientRect(), x = (e.clientX - b.left) / b.width, y = (e.clientY - b.top) / b.height;
    el.style.setProperty("--cx", x * 100 + "%"); el.style.setProperty("--cy", y * 100 + "%");
    if (!reduceMotion) el.style.transform = `translateY(-4px) rotateX(${(0.5 - y) * 8}deg) rotateY(${(x - 0.5) * 10}deg)`;
  });
  el.addEventListener("pointerleave", () => el.style.transform = "");
}
document.querySelectorAll(".tilt").forEach(bindTilt);

document.addEventListener("pointerdown", e => {
  const btn = e.target.closest(".ripple"); if (!btn || btn.disabled) return;
  const b = btn.getBoundingClientRect(), s = Math.max(b.width, b.height), w = document.createElement("span");
  w.className = "ripple-wave";
  Object.assign(w.style, {width: s + "px", height: s + "px", left: e.clientX - b.left - s / 2 + "px", top: e.clientY - b.top - s / 2 + "px"});
  btn.appendChild(w); setTimeout(() => w.remove(), 650);
});

function countUp(el, to) {
  if (reduceMotion || isNaN(to)) { el.textContent = to; return; }
  const from = parseInt(el.textContent) || 0, t0 = performance.now();
  const step = (t) => {
    const p = Math.min((t - t0) / 900, 1);
    el.textContent = Math.round(from + (to - from) * (1 - Math.pow(1 - p, 3)));
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/* ---------- messages ---------- */
function scrollLatest(smooth = true) {
  messagesEl.lastElementChild?.scrollIntoView({behavior: smooth && !reduceMotion ? "smooth" : "auto", block: "end"});
}

function buildMessage(role, kind) {
  const row = document.createElement("article");
  row.className = `message ${role}${kind === "emergency" ? " urgent" : ""}`;
  row.innerHTML = `<div class="message-avatar">${role === "assistant" ? "✳" : "Y"}</div><div class="message-body"><div class="message-meta">${role === "assistant" ? "AURA · AI Health Assistant" : "You"}${kind === "emergency" ? " · Urgent" : ""}</div><div class="message-bubble"></div></div>`;
  messagesEl.appendChild(row);
  return row;
}

function finishMessage(row, role, content, sources, elapsed) {
  const body = row.querySelector(".message-body");
  if (role === "assistant" && sources?.length) {
    body.insertAdjacentHTML("beforeend", `<details class="sources"><summary>PDF knowledge sources · ${sources.length} page(s) matched</summary>${sources.map(s => `<p><b>${esc(s.dataset)}</b><br>PDF page ${esc(s.page)} · relevance ${esc(s.score)}</p>`).join("")}</details>`);
  }
  body.insertAdjacentHTML("beforeend", `<div class="msg-foot"><span class="message-time">${timeNow()}${elapsed !== null && elapsed !== undefined ? ` · ${esc(elapsed)}s` : ""}</span><button class="copy-btn" type="button">Copy</button></div>`);
  body.querySelector(".copy-btn").addEventListener("click", async (e) => {
    try { await navigator.clipboard.writeText(content); e.target.textContent = "Copied"; toast("Copied to clipboard"); }
    catch { toast("Couldn't copy"); }
    setTimeout(() => e.target.textContent = "Copy", 1500);
  });
}

async function renderMessage(role, content, sources = [], kind = "answer", elapsed = null, stream = false) {
  const row = buildMessage(role, kind), bubble = row.querySelector(".message-bubble");
  if (stream && !reduceMotion) {
    bubble.classList.add("streaming");
    const words = content.split(/(\s+)/), chunk = Math.max(1, Math.ceil(words.length / 140));
    let shown = "";
    for (let i = 0; i < words.length; i += chunk) {
      shown += words.slice(i, i + chunk).join("");
      bubble.innerHTML = fmt(shown); scrollLatest(false);
      await sleep(18);
    }
    bubble.classList.remove("streaming");
  }
  bubble.innerHTML = fmt(content);
  finishMessage(row, role, content, sources, elapsed);
  scrollLatest();
}

function setChatMode() {
  const w = $("welcome");
  if (w.style.display !== "none") {
    w.animate([{opacity:1, transform:"none"}, {opacity:0, transform:"translateY(-14px)"}], {duration:260, easing:"ease-in"}).onfinish = () => w.style.display = "none";
  }
  $("chatSection").classList.add("visible");
}

function showTyping() {
  const el = document.createElement("article");
  el.className = "message assistant"; el.id = "typing";
  el.innerHTML = '<div class="message-avatar">✳</div><div class="message-body"><div class="message-meta">AURA is thinking…</div><div class="message-bubble"><div class="typing"><span></span><span></span><span></span></div></div></div>';
  messagesEl.appendChild(el); scrollLatest();
}
const removeTyping = () => $("typing")?.remove();

function progress(state) {
  const p = $("progressBar");
  p.classList.remove("on", "done");
  if (state === "start") { void p.offsetWidth; p.classList.add("on"); }
  if (state === "end") p.classList.add("done");
}

/* ---------- send flow ---------- */
async function submitMessage(value) {
  const message = value.trim();
  if (!message || busy) return;
  if (message.length > MAX) { toast("Shorten your message to 3,000 characters or fewer."); return; }
  busy = true; sendButton.disabled = true; input.value = ""; updateInputUI();
  setChatMode(); await renderMessage("user", message);
  const prior = history.slice(-8); history.push({role:"user", content:message});
  showTyping(); progress("start");
  try {
    const response = await fetch("/api/chat", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({message, history:prior, use_knowledge_base:true})});
    const data = await response.json(); removeTyping();
    if (!response.ok) throw new Error(data.detail || "Something went wrong.");
    progress("end");
    await renderMessage("assistant", data.answer, data.sources || [], data.kind, data.elapsed_seconds ?? null, true);
    history.push({role:"assistant", content:data.answer});
  } catch (error) {
    removeTyping(); progress("end");
    await renderMessage("assistant", `I couldn't complete that request.\n\n${error.message}\n\nCheck that the Ollama service is running and that ${MODEL} is installed.`);
    toast("Connection issue");
  } finally {
    busy = false; sendButton.disabled = !input.value.trim(); input.focus();
  }
}

function updateInputUI() {
  input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 130) + "px";
  const n = input.value.length, ring = $("ringFill");
  $("charCount").textContent = `${n} / ${MAX}`;
  ring.style.strokeDashoffset = 94.2 * (1 - Math.min(n / MAX, 1));
  ring.classList.toggle("warn", n > MAX * 0.9);
  document.querySelector(".ring").classList.toggle("show", n > 0);
  sendButton.disabled = busy || !n || !input.value.trim();
}

function clearConversation() {
  history = []; messagesEl.innerHTML = ""; removeTyping(); progress("end");
  const w = $("welcome"); w.style.display = "block";
  w.animate([{opacity:0, transform:"translateY(14px)"}, {opacity:1, transform:"none"}], {duration:500, easing:"cubic-bezier(.22,1,.36,1)"});
  $("chatSection").classList.remove("visible");
  input.value = ""; updateInputUI(); closeMenu(); scrollTo({top:0, behavior:"smooth"});
  toast("Conversation cleared");
}

/* ---------- events ---------- */
sendButton.addEventListener("click", () => submitMessage(input.value));
input.addEventListener("input", updateInputUI);
input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (!input.value.trim()) { const c = $("composer"); c.classList.remove("shake"); void c.offsetWidth; c.classList.add("shake"); return; }
    submitMessage(input.value);
  }
});
["newChat", "clearChat", "clearTop"].forEach(id => $(id).addEventListener("click", clearConversation));
document.querySelectorAll("[data-prompt]").forEach(b => b.addEventListener("click", () => { input.value = b.dataset.prompt; updateInputUI(); submitMessage(input.value); }));

const openMenu = () => { $("sidebar").classList.add("open"); $("scrim").classList.add("show"); };
const closeMenu = () => { $("sidebar").classList.remove("open"); $("scrim").classList.remove("show"); };
$("mobileMenu").addEventListener("click", () => $("sidebar").classList.contains("open") ? closeMenu() : openMenu());
$("scrim").addEventListener("click", closeMenu);

document.addEventListener("keydown", e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); clearConversation(); input.focus(); }
  if (e.key === "Escape") closeMenu();
});

addEventListener("scroll", () => {
  const far = document.documentElement.scrollHeight - innerHeight - scrollY > 320;
  $("scrollDown").classList.toggle("show", far && $("chatSection").classList.contains("visible"));
}, {passive:true});
$("scrollDown").addEventListener("click", () => scrollTo({top:document.documentElement.scrollHeight, behavior:"smooth"}));

/* ---------- status ---------- */
async function loadStatus() {
  try {
    const response = await fetch("/api/status"), data = await response.json();
    const docs = data.knowledge_base.documents || {};
    countUp($("faqCount"), docs["Health FAQ"]?.pages ?? 0);
    countUp($("vaxCount"), docs["Vaccination"]?.pages ?? 0);
    countUp($("outbreakCount"), docs["Outbreak"]?.pages ?? 0);
    const m = data.ollama;
    $("modelStatus").textContent = m.connected ? (m.model_available ? "Connected · ready" : "Connected · model not installed") : "Ollama not connected";
    $("modelStatus").style.color = m.model_available ? "#7edfc4" : "#e6b77b";
  } catch { $("modelStatus").textContent = "API server unavailable"; $("modelStatus").style.color = "#e6b77b"; }
}
updateInputUI(); loadStatus(); setInterval(loadStatus, 30000);
