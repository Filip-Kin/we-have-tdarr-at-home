'use strict';

const REFRESH_MS_ACTIVE = 2000;
const REFRESH_MS_IDLE = 8000;

const $ = (id) => document.getElementById(id);

function human(bytes) {
  if (bytes == null) return '?';
  let n = Number(bytes);
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + ' ' + units[i];
}

function fmtEta(seconds) {
  if (!seconds || seconds <= 0) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}

function basename(p) { return p.split('/').pop(); }

function renderActive(job) {
  const el = $('active');
  if (!job) { el.innerHTML = '<p class="empty">Nothing running.</p>'; return; }
  const pct = Math.max(0, Math.min(100, job.progress_pct || 0));
  const eta = fmtEta(job.progress_eta_sec);
  const status = job.status === 'verifying' ? 'verifying output…' : `${pct.toFixed(1)}%`;
  el.innerHTML = `
    <div class="job">
      <div class="job-path">${escapeHtml(job.src_path)}</div>
      <div class="progress-bar"><div style="width:${pct}%"></div></div>
      <div class="job-meta">
        <span><strong>${status}</strong></span>
        <span>fps <strong>${(job.progress_fps||0).toFixed(1)}</strong></span>
        <span>speed <strong>${(job.progress_speed||0).toFixed(2)}x</strong></span>
        <span>ETA <strong>${eta}</strong></span>
        <span>output so far <strong>${human(job.progress_size)}</strong></span>
        <span>source <strong>${job.src_size_human}</strong></span>
        <span>${job.height||'?'}p · ${job.src_codec||'?'} · ${(job.src_bitrate_kbps||0)/1000|0} Mbps</span>
      </div>
    </div>`;
}

function renderPending(jobs) {
  const sec = $('pendingSection');
  if (!jobs.length) { sec.hidden = true; return; }
  sec.hidden = false;
  $('pending').innerHTML = jobs.map(j => {
    const savedPct = j.saved_pct ?? 0;
    return `
      <div class="job">
        <div class="job-path">${escapeHtml(j.src_path)}</div>
        <div class="job-meta">
          <span>source <strong>${j.src_size_human}</strong></span>
          <span>output <strong>${j.dst_size_human||'?'}</strong></span>
          <span>saved <strong class="saved-cell">${j.saved_human||'?'} (${savedPct}%)</strong></span>
          <span>duration <strong>${fmtDuration(j.duration_sec)}</strong></span>
        </div>
        <div style="margin-top:10px">
          <button class="btn btn-primary" onclick="approveJob(${j.id})">Approve &amp; replace</button>
          <button class="btn btn-danger" onclick="discardJob(${j.id})">Discard</button>
        </div>
      </div>`;
  }).join('');
}

function renderQueue(jobs) {
  $('queueCount').textContent = jobs.length;
  $('queue').innerHTML = jobs.length
    ? jobs.map(j => `<div class="job">
        <div class="job-path">${escapeHtml(j.src_path)}</div>
        <div class="job-meta"><span>${j.src_size_human}</span><span>${escapeHtml(j.reason||'')}</span></div>
      </div>`).join('')
    : '<p class="empty">Empty.</p>';
}

function renderDone(jobs) {
  $('doneCount').textContent = jobs.length;
  const empty = $('doneEmpty');
  const table = $('doneTable');
  if (!jobs.length) { empty.hidden = false; table.hidden = true; return; }
  empty.hidden = true; table.hidden = false;
  $('doneBody').innerHTML = jobs.map(j => `
    <tr>
      <td class="path" title="${escapeHtml(j.src_path)}">${escapeHtml(basename(j.src_path))}</td>
      <td>${j.src_size_human}</td>
      <td>${j.dst_size_human||'?'}</td>
      <td class="saved-cell">${j.saved_human||'?'}</td>
      <td>${j.saved_pct??0}%</td>
      <td>${fmtTime(j.approved_at || j.finished_at)}</td>
    </tr>`).join('');
}

function renderFailed(jobs) {
  const sec = $('failedSection');
  $('failedCount').textContent = jobs.length;
  if (!jobs.length) { sec.hidden = true; return; }
  sec.hidden = false;
  $('failedBody').innerHTML = jobs.map(j => `
    <tr>
      <td class="path" title="${escapeHtml(j.src_path)}">${escapeHtml(basename(j.src_path))}</td>
      <td>${j.status}</td>
      <td><span class="error-msg">${escapeHtml(j.error_msg||'')}</span></td>
    </tr>`).join('');
}

function renderConfig(cfg) {
  if (!cfg) return;
  $('config').innerHTML = [
    cfg.allowlist ? `<span class="badge">allowlist <strong>${escapeHtml(cfg.allowlist)}</strong></span>` : '<span class="badge">allowlist <strong>off</strong></span>',
    `<span class="badge">watcher <strong>${cfg.watcher_enabled ? 'on' : 'off'}</strong></span>`,
    `<span class="badge">hold for approval <strong>${cfg.hold_for_approval ? 'yes' : 'no'}</strong></span>`,
    `<span class="badge">keep .original <strong>${cfg.keep_original_backup ? 'yes' : 'no'}</strong></span>`,
    `<span class="badge">x265 <strong>${cfg.preset} / crf ${cfg.crf}</strong></span>`,
  ].join('');
}

function fmtDuration(sec) {
  if (!sec) return '?';
  const h = Math.floor(sec/3600);
  const m = Math.floor((sec%3600)/60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

function escapeHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

async function refresh() {
  try {
    const r = await fetch('/api/state');
    const s = await r.json();
    $('totalSaved').textContent = human(s.total_saved_bytes);
    renderConfig(s.config);
    renderActive(s.active);
    renderPending(s.pending);
    renderQueue(s.queued);
    renderDone(s.done);
    renderFailed(s.failed);
    const isActive = s.active || s.queued.length > 0;
    return isActive ? REFRESH_MS_ACTIVE : REFRESH_MS_IDLE;
  } catch (e) {
    console.error('refresh failed', e);
    return REFRESH_MS_IDLE;
  }
}

async function approveJob(id) {
  if (!confirm('Approve replacement? Original will be renamed to *.original-<timestamp> (kept until you delete it).')) return;
  const r = await fetch(`/api/approve/${id}`, { method: 'POST' });
  if (!r.ok) alert('approve failed: ' + await r.text());
  refresh();
}

async function discardJob(id) {
  if (!confirm('Discard this transcode? Temp file will be deleted; source stays untouched.')) return;
  const r = await fetch(`/api/discard/${id}`, { method: 'POST' });
  if (!r.ok) alert('discard failed: ' + await r.text());
  refresh();
}

async function scanLibrary() {
  $('scanMsg').textContent = 'scanning…';
  const r = await fetch('/api/scan', { method: 'POST' });
  const s = await r.json();
  $('scanMsg').textContent =
    `enqueued ${s.enqueued}, skipped ${s.skipped}, errors ${s.errors}`;
  refresh();
}

$('scanBtn').addEventListener('click', scanLibrary);

(async function loop() {
  while (true) {
    const next = await refresh();
    await new Promise(r => setTimeout(r, next));
  }
})();
