// Small, dependency-free client.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

let busy = false;
async function startLookup(query, force = false) {
  const msg = $("#lookup-msg"), btn = $("#go");
  if (busy) return;
  busy = true;
  const label = btn?.textContent;
  if (btn) { btn.disabled = true; btn.textContent = "Starting…"; }
  if (msg) msg.textContent = "Starting… if the server was asleep this can take up to a minute.";
  try {
    const r = await fetch("/api/generate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, force }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || "Something went wrong. Try again in a moment.");
    if (d.choose) {  // umbrella name like "Google": ask which product
      busy = false;
      if (btn) { btn.disabled = false; btn.textContent = label; }
      const esc = (x) => String(x).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
      const html = `"${esc(query)}" covers a few products with very different review paths. Which one? ` +
        d.choose.map((c) => `<a href="#" data-try="${esc(c)}">${esc(c)}</a>`).join(" · ");
      if (msg) msg.innerHTML = html; else alert(d.choose.join(", "));
      return;
    }
    location.href = d.existing || d.watch;
  } catch (e) {
    busy = false;
    if (btn) { btn.disabled = false; btn.textContent = label; }
    if (msg) msg.textContent = e.message; else alert(e.message);
  }
}

async function copyText(el, text, okLabel) {
  const original = el.dataset.label || el.textContent;
  el.dataset.label = original;
  try {
    await navigator.clipboard.writeText(text);
    el.textContent = okLabel;
  } catch (_) {
    el.textContent = "Copy failed. Select it manually.";
  }
  setTimeout(() => { el.textContent = original; }, 2000);
}

$("#lookup")?.addEventListener("submit", (e) => { e.preventDefault(); startLookup($("#q").value); });
// Some embedded browsers don't submit forms on Enter; make sure it works everywhere.
$("#q")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); startLookup($("#q").value); }
});
// Coming back with the Back button can restore a page whose button still says "Starting…".
window.addEventListener("pageshow", () => {
  busy = false;
  const btn = $("#go");
  if (btn) { btn.disabled = false; btn.textContent = "Get the guide →"; }
});
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-try]");
  if (t) { e.preventDefault(); startLookup(t.dataset.try, !!t.dataset.force); }
  const c = e.target.closest("[data-copy]");
  if (c) copyText(c, c.dataset.copy, "Copied");
  const l = e.target.closest("[data-copy-link]");
  if (l) copyText(l, location.href.split("#")[0], "Link copied");
  if (e.target.closest("[data-print]")) window.print();
  const tf = e.target.closest("#try-filter");
  if (tf) { e.preventDefault(); startLookup($("#filter").value.trim()); }
});

$("#filter")?.addEventListener("input", (e) => {
  const q = e.target.value.trim().toLowerCase();
  let shown = 0;
  $$("#catalog tbody tr[data-name]").forEach((tr) => {
    tr.hidden = !tr.dataset.name.includes(q);
    if (!tr.hidden) shown++;
  });
  const none = $("#no-match");
  if (none) none.hidden = shown > 0 || !q;
});

// Timestamps in the reader's own timezone (the server runs on UTC).
$$("time[data-local], time[data-local-date]").forEach((t) => {
  const v = t.getAttribute("datetime");
  if (!v || !v.includes("T")) return; // plain dates (hand-checked guides) stay as written
  const d = new Date(v);
  if (isNaN(d)) return;
  t.textContent = t.hasAttribute("data-local-date")
    ? d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })
    : d.toLocaleString(undefined, { day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit" });
});

// Per-viewer checklist progress (convenience only; never required).
(function checklist() {
  const g = $(".guide"); if (!g) return;
  const key = "progress:" + g.dataset.slug;
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(key) || "{}"); } catch (_) {}
  const boxes = $$("input[data-step]");
  const update = () => {
    const n = boxes.filter((b) => b.checked).length;
    $("#prog-n").textContent = n; $("#prog-t").textContent = boxes.length;
  };
  boxes.forEach((b) => {
    b.checked = !!saved[b.dataset.step];
    b.closest(".step").classList.toggle("done", b.checked);
    b.addEventListener("change", () => {
      saved[b.dataset.step] = b.checked;
      b.closest(".step").classList.toggle("done", b.checked);
      try { localStorage.setItem(key, JSON.stringify(saved)); } catch (_) {}
      update();
    });
  });
  update();
})();

// Live agent run viewer (Server-Sent Events).
function watchJob(id) {
  const steps = {};
  const started = Number($(".timer")?.dataset.started) || Date.now();
  const tick = setInterval(() => { $("#elapsed").textContent = ((Date.now() - started) / 1000).toFixed(1); }, 100);
  const li = (ul, html, cls = "") => { const x = document.createElement("li"); x.className = cls; x.innerHTML = html; $(ul).appendChild(x); x.scrollIntoView({ block: "nearest" }); return x; };
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const es = new EventSource(`/api/jobs/${id}/events`);
  let next = 0;
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.i < next) return; // replayed after a reconnect
    next = ev.i + 1;
    if (ev.type === "step") {
      let el = steps[ev.id];
      if (!el) el = steps[ev.id] = li("#steps", "");
      el.className = "st-" + ev.status;
      el.innerHTML = `<span class="dot"></span><span>${esc(ev.label)}</span><span class="t">${ev.t}s</span>`;
    } else if (ev.type === "source") {
      li("#sources", `<span class="tag ${ev.official ? "ready" : ""}">${ev.official ? "official" : "3rd-party"}</span> <a href="${esc(ev.url)}" target="_blank" rel="noopener">${esc(ev.title || ev.url)}</a>`);
    } else if (ev.type === "probe") {
      li("#log", `<b>probe</b> <code>${esc(ev.url)}</code> — ${esc(ev.observed)}`);
    } else if (ev.type === "log") {
      li("#log", esc(ev.text));
    } else if (ev.type === "done") {
      const r = $("#result"); r.hidden = false;
      r.innerHTML = `<b>Done.</b> <a class="btn" href="/guides/${ev.slug}">Open the guide →</a>`;
      setTimeout(() => (location.href = `/guides/${ev.slug}`), 1500);
    } else if (ev.type === "error") {
      const r = $("#result"); r.hidden = false; r.classList.add("err");
      const q = $(".gen .q")?.textContent || "";
      r.innerHTML = `<b>The agent got stuck.</b> ${esc(ev.message)} ` +
        `<a href="#" class="btn" data-try="${esc(q)}" data-force="1">Try again</a> <a href="/">Go back</a>`;
    } else if (ev.type === "end") {
      es.close(); clearInterval(tick);
      $("#elapsed").textContent = Number(ev.t || 0).toFixed(1); // how long the run took, not time since
      Object.values(steps).forEach((el) => { if (el.className === "st-running") el.className = "st-warn"; });
    }
  };
  es.onerror = () => { /* browser auto-reconnects; server replays from the start */ };
}
