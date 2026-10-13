// Auto-refresh: while a run is active, poll /api/status every 4 s, update the
// live numbers in place, and reload the page when the run state changes.
(function () {
  const mode = document.body.dataset.autorefresh;
  const clock = document.getElementById("clock");
  if (!mode) return;
  let lastRun = null;
  async function tick() {
    try {
      const response = await fetch("/api/status", { cache: "no-store" });
      const status = await response.json();
      const active = status.active;
      if (clock) clock.textContent = "updated " + new Date().toLocaleTimeString();
      const runId = active ? active.run_id : null;
      if ((mode === "idle" && runId) || (mode === "active" && lastRun !== null && runId !== lastRun)) {
        location.reload();
        return;
      }
      lastRun = runId;
      if (active && active.chunk) {
        document.querySelectorAll('[data-field="chunks_done"]').forEach(el => { el.textContent = active.chunk.chunks_done; });
      }
      // a step finishing changes the filing counts: reload now and then to refresh them
      if (active && (window.__ticks = (window.__ticks || 0) + 1) % 5 === 0) { location.reload(); return; }
    } catch (error) {
      if (clock) clock.textContent = "monitor: cannot reach /api/status";
    }
    setTimeout(tick, mode === "active" ? 4000 : 15000);
  }
  tick();
})();
