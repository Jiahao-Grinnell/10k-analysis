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
      if (active) {
        const card = document.getElementById("run-" + active.run_id);
        if (card) {
          const set = (field, value) => { const el = card.querySelector('[data-field="' + field + '"]'); if (el && value != null) el.textContent = value; };
          set("completed", active.completed != null ? active.completed.toLocaleString() : null);
          set("chunks_done", active.chunk ? active.chunk.chunks_done : null);
          set("state", active.state);
        }
      }
    } catch (error) {
      if (clock) clock.textContent = "monitor: cannot reach /api/status";
    }
    setTimeout(tick, mode === "active" ? 4000 : 15000);
  }
  tick();
})();
