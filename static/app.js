// ═══════════════════════════════════════════════════════════════════════════
// DMARC Pipeline — joyful, interactive dashboard
// Disney's animation principles: squash & stretch, anticipation, staging
// ═══════════════════════════════════════════════════════════════════════════

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtDate(ts) {
  if (!ts) return '–';
  const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
  return d.toISOString().slice(0, 10);
}

function fmtCount(n) {
  if (n == null) return '0';
  return n.toLocaleString();
}

function fmtPct(n) {
  if (n == null) return '0%';
  return n.toFixed(1) + '%';
}

// Count-up animation with expo-out easing
function animateCount(el, target, duration = 1200) {
  if (!el) return;
  const start = performance.now();
  function tick(now) {
    const t = Math.min((now - start) / duration, 1);
    const eased = t === 1 ? 1 : 1 - Math.pow(2, -10 * t);
    el.textContent = fmtCount(Math.round(target * eased));
    if (t < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

// ── Scroll-triggered reveals ──────────────────────────────────────────────────

function initScrollReveals() {
  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.style.opacity = '1';
        entry.target.style.transform = 'translateY(0)';
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.1, rootMargin: '0px 0px -50px 0px' });

  $$('.reveal').forEach((el, i) => {
    el.style.opacity = '0';
    el.style.transform = 'translateY(24px)';
    el.style.transition = `opacity 0.7s cubic-bezier(0.16, 1, 0.3, 1) ${i * 0.08}s, transform 0.7s cubic-bezier(0.16, 1, 0.3, 1) ${i * 0.08}s`;
    observer.observe(el);
  });
}

// ── Stats ─────────────────────────────────────────────────────────────────────

async function loadStats() {
  try {
    const stats = await fetch('/api/stats').then(r => r.json());
    animateCount($('#stat-reports'), stats.total_reports || 0);
    animateCount($('#stat-records'), stats.total_records || 0);
    animateCount($('#stat-messages'), stats.total_messages || 0);
    animateCount($('#stat-pass'), stats.pass_count || 0);
    animateCount($('#stat-fail'), stats.fail_count || 0);

    animateCount($('#hero-reports'), stats.total_reports || 0);
    animateCount($('#hero-messages'), stats.total_messages || 0);
    animateCount($('#hero-ips'), stats.total_records || 0);
  } catch (e) {
    console.error('Failed to load stats:', e);
  }
}

// ── Reports table ─────────────────────────────────────────────────────────────

async function loadReports() {
  try {
    const reports = await fetch('/api/reports').then(r => r.json());
    const tbody = $('#reports-table');
    tbody.innerHTML = '';
    $('#reports-count').textContent = `${reports.length} total`;

    reports.forEach((r, i) => {
      const tr = document.createElement('tr');
      tr.style.cursor = 'pointer';
      tr.style.animation = `fadeInUp 0.5s cubic-bezier(0.16, 1, 0.3, 1) ${i * 0.06}s both`;
      tr.innerHTML = `
        <td>${r.id}</td>
        <td>${r.domain || '–'}</td>
        <td>${r.org_name || '–'}</td>
        <td>${fmtDate(r.date_begin)} → ${fmtDate(r.date_end)}</td>
        <td>${r.record_count}</td>
        <td><span class="badge pass">${r.pass_count}</span></td>
        <td><span class="badge fail">${r.fail_count}</span></td>
        <td><a href="/file/${r.id}" class="details-link">Details →</a></td>
      `;
      tbody.appendChild(tr);
    });
  } catch (e) {
    console.error('Failed to load reports:', e);
  }
}

// ── Records table ─────────────────────────────────────────────────────────────

async function loadRecords(reportId) {
  const section = $('#records-section');
  section.hidden = false;
  $('#records-report-id').textContent = reportId;

  try {
    const filter = $('#result-filter').value;
    const qs = filter ? `?result=${filter}` : '';
    const records = await fetch(`/api/reports/${reportId}/records${qs}`).then(r => r.json());

    const tbody = $('#records-table');
    tbody.innerHTML = '';

    records.forEach((rec, i) => {
      const tr = document.createElement('tr');
      tr.style.animation = `fadeInUp 0.4s cubic-bezier(0.16, 1, 0.3, 1) ${i * 0.04}s both`;
      const dkimClass = rec.dkim_aligned ? 'pass' : 'fail';
      const spfClass = rec.spf_aligned ? 'pass' : 'fail';
      tr.innerHTML = `
        <td>${rec.source_ip || '–'}</td>
        <td>${fmtCount(rec.count)}</td>
        <td>${rec.header_from || '–'}</td>
        <td>${rec.disposition || '–'}</td>
        <td><span class="badge ${dkimClass}">${rec.dkim_result || '–'}</span></td>
        <td><span class="badge ${spfClass}">${rec.spf_result || '–'}</span></td>
      `;
      tbody.appendChild(tr);
    });

    section.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    console.error('Failed to load records:', e);
  }
}

// ── Upload / drop zone (delightful interactions) ─────────────────────────────

const dropZone = $('#drop-zone');
const fileInput = $('#file-input');
const fileName = $('#file-name');
const uploadBtn = $('#upload-btn');
const statusEl = $('#upload-status');

function setFile(file) {
  if (!file) return;
  fileName.textContent = `${file.name} (${(file.size / 1024).toFixed(1)} KB)`;
  uploadBtn.disabled = false;
  statusEl.textContent = '';
  statusEl.className = 'status';
}

fileInput.addEventListener('change', () => setFile(fileInput.files[0]));

dropZone.addEventListener('click', (e) => {
  if (e.target.tagName !== 'LABEL' && e.target.tagName !== 'INPUT') {
    fileInput.click();
  }
});

['dragenter', 'dragover'].forEach((evt) => {
  dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropZone.classList.add('drag-over');
  });
});

['dragleave', 'drop'].forEach((evt) => {
  dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
  });
});

dropZone.addEventListener('drop', (e) => {
  const file = e.dataTransfer.files[0];
  if (file) {
    const dt = new DataTransfer();
    dt.items.add(file);
    fileInput.files = dt.files;
    setFile(file);
  }
});

$('#upload-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const file = fileInput.files[0];
  if (!file) return;

  const fd = new FormData();
  fd.append('file', file);

  try {
    statusEl.textContent = 'Analyzing...';
    statusEl.className = 'status';
    uploadBtn.disabled = true;
    const result = await fetch('/api/upload', { method: 'POST', body: fd }).then(r => r.json());
    statusEl.textContent = `Done — ${result.status}`;
    statusEl.className = 'status success';
    fileInput.value = '';
    fileName.textContent = '';
    renderUploadResults(result);
  } catch (err) {
    statusEl.textContent = `Error: ${err.message}`;
    statusEl.className = 'status error';
  } finally {
    uploadBtn.disabled = fileInput.files.length === 0;
    await Promise.all([loadStats(), loadReports()]);
  }
});

// ── Render upload results ─────────────────────────────────────────────────────

function renderUploadResults(result) {
  const card = $('#upload-results-card');
  const container = $('#upload-results');
  card.hidden = false;
  $('#upload-results-filename').textContent = result.filename;

  let html = '';

  // Extracted files
  if (result.extracted_files?.length > 0) {
    html += `<div style="margin-bottom: var(--sp-5);">
      <div style="font-family: var(--font-mono); font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-muted); margin-bottom: var(--sp-2);">
        Extracted files (${result.extracted_files.length})
      </div>
      <div style="display: flex; flex-wrap: wrap; gap: var(--sp-2);">`;
    for (const f of result.extracted_files) {
      const failed = result.failed_files?.includes(f);
      html += `<span class="file-chip ${failed ? 'failed' : ''}">${f}</span>`;
    }
    html += `</div></div>`;
  }

  // Collective analysis
  if (result.collective_analysis) {
    const c = result.collective_analysis;
    const o = c.overall;
    const a = c.alignment;
    html += `<div style="background: linear-gradient(135deg, var(--accent-dim), transparent); border: 1px solid rgba(232, 163, 61, 0.2); border-radius: var(--r-lg); padding: var(--sp-5); margin-bottom: var(--sp-5);">
      <div style="font-family: var(--font-mono); font-size: 13px; font-weight: 600; color: var(--accent); margin-bottom: var(--sp-3);">
        Combined health: ${o.health_score}/100 (${o.health_label})
      </div>
      <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(100px, 1fr)); gap: var(--sp-3);">
        <div><div style="font-family: var(--font-mono); font-size: 24px; font-weight: 700;">${fmtCount(o.total_reports)}</div><div style="font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em;">Reports</div></div>
        <div><div style="font-family: var(--font-mono); font-size: 24px; font-weight: 700;">${fmtCount(o.total_records)}</div><div style="font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em;">Records</div></div>
        <div><div style="font-family: var(--font-mono); font-size: 24px; font-weight: 700;">${fmtCount(o.total_messages)}</div><div style="font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em;">Messages</div></div>
        <div><div style="font-family: var(--font-mono); font-size: 24px; font-weight: 700; color: var(--accent);">${fmtPct(a.overall_pass_rate)}</div><div style="font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em;">Pass rate</div></div>
      </div>
    </div>`;
  }

  // Per-file analysis
  if (result.per_file_analysis?.length > 0) {
    html += `<div>
      <div style="font-family: var(--font-mono); font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-muted); margin-bottom: var(--sp-3);">Per-file analysis</div>
      <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: var(--sp-3);">`;
    for (const file of result.per_file_analysis) {
      const a = file.analysis;
      const o = a.overall;
      const al = a.alignment;
      html += `<div class="analysis-card">
        <div class="analysis-card-header">
          <span style="font-family: var(--font-mono); font-size: 12px;">${file.xml_filename}</span>
          <span class="badge ${o.health_score >= 70 ? 'pass' : o.health_score >= 40 ? 'warn' : 'fail'}">${o.health_score}/100</span>
        </div>
        <div class="analysis-card-body">
          <div class="analysis-metric"><span>Org</span><span>${file.org_name || '–'}</span></div>
          <div class="analysis-metric"><span>Domain</span><span>${file.domain || '–'}</span></div>
          <div class="analysis-metric"><span>Messages</span><span>${fmtCount(o.total_messages)}</span></div>
          <div class="analysis-metric"><span>DKIM</span><span style="color: var(--success);">${fmtPct(al.dkim_pass_rate)}</span></div>
          <div class="analysis-metric"><span>SPF</span><span style="color: var(--${al.spf_pass_rate > 50 ? 'success' : 'danger'});">${fmtPct(al.spf_pass_rate)}</span></div>
        </div>
        <div style="padding: 0 var(--sp-3) var(--sp-3);">
          <a href="/file/${file.report_id}" class="btn" style="font-size: 11px; padding: 6px 12px; display: inline-block; text-decoration: none;">View full details →</a>
        </div>
      </div>`;
    }
    html += `</div></div>`;
  }

  container.innerHTML = html;
  card.scrollIntoView({ behavior: 'smooth', block: 'start' });

  // Trigger animations on new content
  $$('.analysis-card').forEach((el, i) => {
    el.style.animation = `fadeInUp 0.5s cubic-bezier(0.16, 1, 0.3, 1) ${i * 0.1}s both`;
  });
}

$('#result-filter').addEventListener('change', () => {
  const section = $('#records-section');
  if (section.hidden) return;
  const id = Number($('#records-report-id').textContent);
  loadRecords(id);
});

// ── Magnetic button effect ────────────────────────────────────────────────────

$$('.btn').forEach(btn => {
  btn.addEventListener('mousemove', (e) => {
    const rect = btn.getBoundingClientRect();
    const x = e.clientX - rect.left - rect.width / 2;
    const y = e.clientY - rect.top - rect.height / 2;
    btn.style.transform = `translate(${x * 0.1}px, ${y * 0.1}px) translateY(-2px)`;
  });

  btn.addEventListener('mouseleave', () => {
    btn.style.transform = '';
  });
});

// ── Connected accounts (Gmail + Outlook) ──────────────────────────────────────

async function loadAccounts() {
  const container = $('#accounts-list');

  try {
    const [gmail, outlook] = await Promise.all([
      fetch('/oauth/accounts').then(r => r.json()).catch(() => []),
      fetch('/outlook/accounts').then(r => r.json()).catch(() => []),
    ]);

    const all = [
      ...gmail.map(a => ({ ...a, type: 'gmail' })),
      ...outlook.map(a => ({ ...a, type: 'outlook' })),
    ];

    if (all.length === 0) {
      container.innerHTML = `
        <div style="text-align: center; padding: var(--sp-6);">
          <p style="color: var(--text-muted); margin-bottom: var(--sp-3);">
            Connect your email to automatically ingest DMARC reports as they arrive.
          </p>
          <div style="display: flex; gap: var(--sp-2); justify-content: center;">
            <a href="/oauth/start" class="btn">+ Connect Gmail</a>
            <a href="/outlook/start" class="btn">+ Connect Outlook</a>
          </div>
        </div>`;
      return;
    }

    let html = '<div style="display: grid; gap: var(--sp-2);">';
    for (const acc of all) {
      const icon = acc.type === 'gmail' ? 'G' : 'M';
      const label = acc.type === 'gmail' ? 'Gmail' : 'Outlook';
      const syncFn = acc.type === 'gmail' ? 'syncAccount' : 'syncOutlookAccount';
      const backfillFn = acc.type === 'gmail' ? 'backfillAccount' : 'backfillOutlookAccount';
      const disconnectFn = acc.type === 'gmail' ? 'disconnectAccount' : 'disconnectOutlookAccount';
      html += `
        <div style="display: flex; align-items: center; justify-content: space-between; padding: var(--sp-3); background: var(--bg-elevated); border: 1px solid var(--border); border-radius: var(--r-md);">
          <div style="display: flex; align-items: center; gap: var(--sp-3);">
            <div style="width: 32px; height: 32px; background: var(--surface-3); border-radius: 50%; display: grid; place-items: center; font-size: 14px; font-weight: 600;">${icon}</div>
            <div>
              <div style="font-family: var(--font-mono); font-size: 13px; font-weight: 500;">${acc.email}</div>
              <div style="font-size: 11px; color: var(--text-muted);">${label} · ${acc.last_sync ? 'Last sync: ' + fmtDate(acc.last_sync) : 'Never synced'}</div>
            </div>
          </div>
          <div style="display: flex; gap: var(--sp-2);">
            <button class="btn" style="font-size: 11px; padding: 4px 10px;" onclick="${syncFn}(${acc.id})">Sync</button>
            <button class="btn btn-ghost" style="font-size: 11px; padding: 4px 10px;" onclick="${backfillFn}(${acc.id})" title="Scan past 10 days">Backfill</button>
            <button class="btn btn-ghost" style="font-size: 11px; padding: 4px 10px;" onclick="${disconnectFn}(${acc.id})">✕</button>
          </div>
        </div>`;
    }
    html += '</div>';
    container.innerHTML = html;
  } catch (e) {
    container.innerHTML = `
      <div style="text-align: center; padding: var(--sp-6);">
        <p style="color: var(--text-muted); margin-bottom: var(--sp-3);">
          Connect your email to automatically ingest DMARC reports as they arrive.
        </p>
        <div style="display: flex; gap: var(--sp-2); justify-content: center;">
          <a href="/oauth/start" class="btn">+ Connect Gmail</a>
          <a href="/outlook/start" class="btn">+ Connect Outlook</a>
        </div>
      </div>`;
  }
}

// ── Gmail actions ─────────────────────────────────────────────────────────────

async function syncAccount(id) {
  try {
    await fetch(`/oauth/accounts/${id}/sync`, { method: 'POST' });
    await Promise.all([loadStats(), loadReports(), loadAccounts()]);
  } catch (e) {
    alert('Sync failed: ' + e.message);
  }
}

async function backfillAccount(id) {
  if (!confirm('Scan past 10 days of emails for DMARC reports? This may take a minute.')) return;
  try {
    const res = await fetch(`/oauth/accounts/${id}/sync?backfill=true`, { method: 'POST' });
    const data = await res.json();
    alert(`Backfill complete: ${data.reports_synced} report(s) found`);
    await Promise.all([loadStats(), loadReports(), loadAccounts()]);
  } catch (e) {
    alert('Backfill failed: ' + e.message);
  }
}

async function disconnectAccount(id) {
  if (!confirm('Disconnect this account?')) return;
  try {
    await fetch(`/oauth/accounts/${id}`, { method: 'DELETE' });
    loadAccounts();
  } catch (e) {
    alert('Failed: ' + e.message);
  }
}

// ── Outlook actions ───────────────────────────────────────────────────────────

async function syncOutlookAccount(id) {
  try {
    await fetch(`/outlook/accounts/${id}/sync`, { method: 'POST' });
    await Promise.all([loadStats(), loadReports(), loadAccounts()]);
  } catch (e) {
    alert('Sync failed: ' + e.message);
  }
}

async function backfillOutlookAccount(id) {
  if (!confirm('Scan past 10 days of emails for DMARC reports? This may take a minute.')) return;
  try {
    const res = await fetch(`/outlook/accounts/${id}/sync?backfill=true`, { method: 'POST' });
    const data = await res.json();
    alert(`Backfill complete: ${data.reports_synced} report(s) found`);
    await Promise.all([loadStats(), loadReports(), loadAccounts()]);
  } catch (e) {
    alert('Backfill failed: ' + e.message);
  }
}

async function disconnectOutlookAccount(id) {
  if (!confirm('Disconnect this account?')) return;
  try {
    await fetch(`/outlook/accounts/${id}`, { method: 'DELETE' });
    loadAccounts();
  } catch (e) {
    alert('Failed: ' + e.message);
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────

(async function init() {
  await Promise.all([loadStats(), loadReports(), loadAccounts()]);
  initScrollReveals();
})();
