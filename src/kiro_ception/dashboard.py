"""HTML dashboard served by the engine at GET /."""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kiro Ception</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #1a1a2e; color: #e0e0e0; padding: 2rem; line-height: 1.5; }
  h1 { color: #64ffda; margin-bottom: 0.5rem; }
  h2 { color: #80cbc4; margin: 1.5rem 0 0.5rem; font-size: 1.1rem; }
  .subtitle { color: #888; margin-bottom: 2rem; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }
  @media (max-width: 768px) { .grid { grid-template-columns: 1fr; } }
  .card { background: #16213e; border-radius: 8px; padding: 1.2rem; }
  .card h2 { margin-top: 0; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  td { padding: 0.3rem 0.5rem; border-bottom: 1px solid #1a1a2e; }
  td:first-child { color: #80cbc4; white-space: nowrap; width: 40%; }
  .status-badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
                  font-size: 0.8rem; font-weight: 600; }
  .status-idle { background: #2e7d32; color: #c8e6c9; }
  .status-indexing { background: #e65100; color: #ffe0b2; }
  .status-starting { background: #1565c0; color: #bbdefb; }
  .status-error { background: #b71c1c; color: #ffcdd2; }
  .loading { color: #888; font-style: italic; }
  #error { color: #ef5350; margin: 1rem 0; }
  .refresh { color: #64ffda; cursor: pointer; font-size: 0.85rem; float: right; }
  .refresh:hover { text-decoration: underline; }
  .countdown-ring { display: inline-block; vertical-align: middle; margin-left: 0.5rem; }
  .countdown-ring svg { width: 18px; height: 18px; transform: rotate(-90deg); }
  .countdown-ring circle { fill: none; stroke: #64ffda; stroke-width: 2.5;
    stroke-dasharray: 44; stroke-dashoffset: 0;
    transition: none; }
  .countdown-ring.active circle {
    animation: countdown 10s linear forwards; }
  @keyframes countdown {
    from { stroke-dashoffset: 0; }
    to { stroke-dashoffset: 44; }
  }
</style>
</head>
<body>
<h1>Kiro Ception</h1>
<p class="subtitle">Status Dashboard</p>
<div id="error"></div>
<div class="grid">
  <div class="card">
    <span class="refresh" onclick="load()">refresh<span class="countdown-ring" id="ring"><svg viewBox="0 0 18 18"><circle cx="9" cy="9" r="7"/></svg></span></span>
    <h2>Indexing Status</h2>
    <div id="status"><p class="loading">Loading...</p></div>
  </div>
  <div class="card">
    <h2>Configuration</h2>
    <div id="config"><p class="loading">Loading...</p></div>
  </div>
</div>
<script>
function esc(s) { return String(s).replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function badge(state) {
  const cls = state === 'idle' ? 'status-idle' : state === 'indexing' ? 'status-indexing'
    : state === 'starting' ? 'status-starting' : 'status-error';
  return `<span class="status-badge ${cls}">${esc(state)}</span>`;
}
function row(k, v) { return `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`; }
function renderStatus(d) {
  let h = '<table>';
  h += `<tr><td>State</td><td>${badge(d.state)}</td></tr>`;
  h += row('Progress', d.progress_percent + '%');
  h += row('Sessions (total)', d.sessions_total);
  h += row('Sessions (processed)', d.sessions_processed);
  h += row('Sessions (unchanged)', d.sessions_unchanged);
  h += row('Messages embedded', d.messages_embedded);
  h += row('Messages cached', d.messages_cached);
  h += row('Errors', d.errors);
  h += row('Rate', d.rate_msg_per_sec + ' msg/s');
  h += row('Elapsed', Math.round(d.elapsed_seconds) + 's');
  h += row('Uptime', Math.round(d.uptime_seconds) + 's');
  h += row('Memory used', (d.memory_used_mb || 0) + ' MB' + (d.memory_used_percent ? ' (' + d.memory_used_percent + '%)' : ''));
  if (d.memory_limit_mb) h += row('Memory limit', d.memory_limit_mb + ' MB');
  h += row('Embedding count', d.embedding_count);
  h += row('DB size', (d.db_size_mb || 0) + ' MB');
  h += row('Schema version', d.schema_version || '?');
  h += row('FTS enabled', d.fts_enabled ? 'Yes' : 'No');
  h += row('Search ready', d.search_ready ? 'Yes' : 'No');
  h += row('Search messages', d.search_message_count);
  if (d.newest_message_at) {
    const msgDate = new Date(d.newest_message_at.replace(' ', 'T'));
    const ageMs = Date.now() - msgDate.getTime();
    const ageDays = ageMs / (1000 * 60 * 60 * 24);
    let label = d.newest_message_at;
    if (ageDays >= 4) {
      label = `<span style="color:#ef5350;font-weight:600" title="Indexing likely stalled — no new messages in ${Math.floor(ageDays)} days">⚠️ ${d.newest_message_at} (${Math.floor(ageDays)}d ago)</span>`;
    } else if (ageDays >= 1) {
      label = `<span style="color:#ffa726" title="No new messages in ${Math.floor(ageDays)} day(s) — indexing may be stalled or Kiro unused">⚡ ${d.newest_message_at} (${Math.floor(ageDays)}d ago)</span>`;
    }
    h += `<tr><td>Newest message</td><td>${label}</td></tr>`;
  }
  if (d.last_error) h += row('Last error', d.last_error);
  if (d.last_completed_at) h += row('Last completed', d.last_completed_at);
  h += '</table>';
  document.getElementById('status').innerHTML = h;
}
function renderConfig(d) {
  let h = '<table>';
  h += row('Version', d.version?.kiro_ception || '?');
  h += row('Python', d.version?.python || '?');
  h += row('Platform', d.version?.platform || '?');
  h += row('Role', d.instance?.role || '?');
  h += row('PID', d.instance?.pid || '?');
  h += row('Port', d.instance?.port || d.server?.engine_port || '?');
  h += row('Backend', d.embedding?.backend || '?');
  h += row('Model', d.embedding?.model || '?');
  h += row('Dimensions', d.embedding?.dimensions || 'auto');
  h += row('Embeddings', d.cache?.embedding_count || 0);
  h += row('Messages', d.cache?.message_count || 0);
  h += row('Sessions indexed', d.cache?.indexed_sessions || 0);
  h += row('Memory limit', d.memory?.effective_limit_mb + ' MB' || '?');
  h += row('Rescan interval', d.indexing?.rescan_interval_minutes + ' min');
  h += row('Heartbeat interval', (d.server?.heartbeat_interval_seconds || 30) + 's');
  h += row('Peers enabled', d.peers?.enabled ? 'Yes' : 'No');
  if (d.peers?.enabled) h += row('Peer nodes', d.peers.nodes?.join(', ') || 'none');
  h += '</table>';
  document.getElementById('config').innerHTML = h;
}
async function load() {
  try {
    const [statusRes, configRes] = await Promise.all([
      fetch('/status'), fetch('/config')
    ]);
    renderStatus(await statusRes.json());
    renderConfig(await configRes.json());
    document.getElementById('error').textContent = '';
  } catch(e) {
    document.getElementById('error').textContent = 'Failed to load: ' + e.message;
  }
  // Restart countdown animation
  const ring = document.getElementById('ring');
  ring.classList.remove('active');
  void ring.offsetWidth; // Force reflow to restart animation
  ring.classList.add('active');
}
load();
setInterval(load, 10000);
</script>
</body>
</html>"""
