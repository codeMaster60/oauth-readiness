// Small, dependency-free client.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

async function startLookup(query, force = false) {
  const msg = $("#lookup-msg");
  try {
    const r = await fetch("/api/generate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, force }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Request failed");
    location.href = d.existing || d.watch;
  } catch (e) {
    if (msg) msg.textContent = e.message; else alert(e.message);
  }
}

$("#lookup")?.addEventListener("submit", (e) => { e.preventDefault(); startLookup($("#q").value); });
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-try]");
  if (t) { e.preventDefault(); startLookup(t.dataset.try, !!t.dataset.force); }
  const c = e.target.closest("[data-copy]");
  if (c) { navigator.clipboard.writeText(c.dataset.copy); c.textContent = "Copied"; }
  const l = e.target.closest("[data-copy-link]");
  if (l) { navigator.clipboard.writeText(location.href.split("#")[0]); l.textContent = "Link copied"; }
  if (e.target.closest("[data-print]")) window.print();
});

$("#filter")?.addEventListener("input", (e) => {
  const q = e.target.value.toLowerCase();
  $$("#catalog tbody tr").forEach((tr) => { tr.hidden = !tr.dataset.name.includes(q); });
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
  const steps = {}, started = Date.now();
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
      li("#log", `🛰️ <b>Probe</b> <code>${esc(ev.url)}</code> — ${esc(ev.observed)}`);
    } else if (ev.type === "log") {
      li("#log", esc(ev.text));
    } else if (ev.type === "done") {
      const r = $("#result"); r.hidden = false;
      r.innerHTML = `<b>Guide ready.</b> <a class="btn" href="/guides/${ev.slug}">Open the guide →</a>`;
      setTimeout(() => (location.href = `/guides/${ev.slug}`), 1500);
    } else if (ev.type === "error") {
      const r = $("#result"); r.hidden = false; r.classList.add("err");
      r.innerHTML = `<b>The agent stopped:</b> ${esc(ev.message)} <a href="/">Back</a>`;
    } else if (ev.type === "end") {
      es.close(); clearInterval(tick);
      Object.values(steps).forEach((el) => { if (el.className === "st-running") el.className = "st-warn"; });
    }
  };
  es.onerror = () => { /* browser auto-reconnects; server replays from the start */ };
}
