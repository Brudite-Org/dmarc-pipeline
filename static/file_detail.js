// ═══════════════════════════════════════════════════════════════════════════
// DMARC Pipeline — per-file detail view (every parameter + failure diagnosis)
// ═══════════════════════════════════════════════════════════════════════════

const $ = (sel) => document.querySelector(sel);

// Every value here originates from report XML sent by third parties, so it must
// be escaped before it is interpolated into innerHTML.
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function val(v) {
  return v === null || v === undefined || v === '' ? '–' : esc(v);
}

function fmtDate(ts) {
  if (!ts) return '–';
  const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
  if (Number.isNaN(d.getTime())) return '–';
  return d.toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
}

function fmtCount(n) {
  if (n == null) return '0';
  return n.toLocaleString();
}

function fmtPct(n) {
  if (n == null) return '0%';
  return n.toFixed(1) + '%';
}

function plural(n, word) {
  return `${fmtCount(n)} ${word}${n === 1 ? '' : 's'}`;
}

// Build a grid of key-value pairs. Values are pre-escaped HTML.
function detailGrid(items) {
  return `<div class="detail-grid">` + items.map(([key, value, cls = '']) => `
    <div class="detail-item">
      <div class="detail-key">${esc(key)}</div>
      <div class="detail-value ${cls}">${value}</div>
    </div>`).join('') + `</div>`;
}

// ── Result → badge colour ────────────────────────────────────────────────────

function resultClass(result) {
  switch ((result || '').toLowerCase()) {
    case 'pass': return 'pass';
    case 'fail':
    case 'permerror': return 'fail';
    case '': return '';
    default: return 'warn'; // softfail, neutral, none, temperror, policy …
  }
}

function resultBadge(result) {
  return `<span class="badge ${resultClass(result)}">${val(result)}</span>`;
}

function dispositionBadge(d) {
  const cls = d === 'reject' ? 'fail' : d === 'quarantine' ? 'warn' : d ? 'pass' : '';
  return `<span class="badge ${cls}">${val(d)}</span>`;
}

// ── Record classification ────────────────────────────────────────────────────
//   fail  → neither DKIM nor SPF aligned: DMARC fails, policy is applied
//   issue → exactly one of them failed: DMARC still passes but something is mis-set
//   pass  → both aligned

function recordStatus(rec) {
  const d = !!rec.dkim_aligned;
  const s = !!rec.spf_aligned;
  if (!d && !s) return 'fail';
  if (!d || !s) return 'issue';
  return 'pass';
}

// ── Failure diagnosis (derived from the auth_results stored with each record) ─
// RFC 7489 aggregate reports carry no per-message data, so the diagnosis is per
// source IP row: which mechanism failed, for which domain, and the usual causes.

function diagnoseDkim(rec, report) {
  const auth = rec.dkim_auth || [];
  const mode = report.adkim === 's' ? 'strict' : 'relaxed';
  const from = rec.header_from || report.domain || 'the From domain';

  if (rec.dkim_aligned) {
    return { ok: true, text: 'DKIM passed and is aligned with the From domain.', actions: [] };
  }
  if (auth.length === 0) {
    return {
      ok: false,
      text: 'No DKIM signature was found on the message.',
      actions: [`Enable DKIM signing for ${from} on the sending service and publish its public key in DNS.`],
    };
  }
  const passed = auth.filter((a) => (a.result || '').toLowerCase() === 'pass');
  if (passed.length > 0) {
    const doms = [...new Set(passed.map((a) => a.domain).filter(Boolean))].join(', ') || 'another domain';
    return {
      ok: false,
      text: `DKIM verified for ${doms}, but that domain is not aligned with ${from} (${mode} alignment, adkim=${report.adkim || 'r'}). Only signatures by the From domain count for DMARC.`,
      actions: [`Configure the sender to sign with d=${from} (custom/branded DKIM) instead of its own domain.`],
    };
  }
  const detail = auth.map((a) => {
    const sel = a.selector ? `, selector ${a.selector}` : '';
    return `${a.domain || 'unknown domain'}${sel} → ${a.result || 'no result'}`;
  }).join('; ');
  return {
    ok: false,
    text: `DKIM signature could not be verified (${detail}).`,
    actions: [
      'Check the DKIM public key for that selector exists in DNS and matches the key the sender signs with.',
      'If the IP is not one of our services, the message may carry a copied/forged signature or have been altered in transit (forwarder, mailing list, footer added).',
    ],
  };
}

const SPF_MEANING = {
  fail: (ip, dom) => `${ip} is not authorised by the SPF record of ${dom} (hard fail).`,
  softfail: (ip, dom) => `${ip} is not authorised by the SPF record of ${dom} (soft fail, ~all).`,
  neutral: (ip, dom) => `The SPF record of ${dom} makes no statement about ${ip} (neutral, ?all).`,
  none: (ip, dom) => `${dom} publishes no SPF record.`,
  permerror: (ip, dom) => `The SPF record of ${dom} is invalid (syntax error or more than 10 DNS lookups).`,
  temperror: (ip, dom) => `A temporary DNS error stopped the SPF check for ${dom}; it may not repeat.`,
};

function diagnoseSpf(rec, report) {
  const auth = rec.spf_auth || [];
  const mode = report.aspf === 's' ? 'strict' : 'relaxed';
  const from = rec.header_from || report.domain || 'the From domain';
  const ip = rec.source_ip || 'The sending IP';

  if (rec.spf_aligned) {
    return { ok: true, text: 'SPF passed and is aligned with the From domain.', actions: [] };
  }
  if (auth.length === 0) {
    return { ok: false, text: 'No SPF result was reported.', actions: [] };
  }
  const passed = auth.filter((a) => (a.result || '').toLowerCase() === 'pass');
  if (passed.length > 0) {
    const doms = [...new Set(passed.map((a) => a.domain).filter(Boolean))].join(', ') || 'another domain';
    return {
      ok: false,
      text: `SPF passed for ${doms} (the envelope/bounce domain), but it is not aligned with ${from} (${mode} alignment, aspf=${report.aspf || 'r'}).`,
      actions: [`Make the sender use a return-path / MAIL FROM on ${from} (custom bounce domain).`],
    };
  }
  const lines = auth.map((a) => {
    const r = (a.result || '').toLowerCase();
    const dom = a.domain || rec.envelope_from || from;
    const base = (SPF_MEANING[r] || (() => `SPF result "${a.result || 'none'}" for ${dom}.`))(ip, dom);
    return a.scope ? `${base} [scope: ${a.scope}]` : base;
  });
  const actions = [];
  if (auth.some((a) => ['fail', 'softfail', 'neutral'].includes((a.result || '').toLowerCase()))) {
    actions.push(`If ${ip} is a service we use, add it (include:/ip4:/ip6:) to the SPF record of the domain above. If we don't recognise it, treat it as spoofing.`);
  }
  if (auth.some((a) => (a.result || '').toLowerCase() === 'permerror')) {
    actions.push('Flatten the SPF record or remove unused includes to get under the 10-lookup limit.');
  }
  return { ok: false, text: lines.join(' '), actions };
}

function diagnose(rec, report) {
  const status = recordStatus(rec);
  const dkim = diagnoseDkim(rec, report);
  const spf = diagnoseSpf(rec, report);
  const policy = report.p || 'none';

  let effect;
  if (status === 'fail') {
    effect = rec.disposition && rec.disposition !== 'none'
      ? `DMARC failed. The receiver applied "${rec.disposition}" (published policy p=${policy}).`
      : `DMARC failed. The receiver still delivered it (disposition "none"; published policy p=${policy}).`;
  } else if (status === 'issue') {
    effect = `DMARC passed because the other mechanism is aligned, but ${dkim.ok ? 'SPF' : 'DKIM'} is mis-set and could become a delivery risk if that other mechanism breaks.`;
  } else {
    effect = 'DMARC passed.';
  }
  return { status, dkim, spf, effect };
}

// ── Failures card ────────────────────────────────────────────────────────────

function failureCard(rec, idx, report) {
  const dx = diagnose(rec, report);
  const actions = [...dx.dkim.actions, ...dx.spf.actions];
  const ipLink = rec.source_ip
    ? ` <a class="ip-link" href="https://ipinfo.io/${encodeURIComponent(rec.source_ip)}" target="_blank" rel="noopener noreferrer">look up IP ↗</a>`
    : '';

  const mech = (label, ok, text, result) => `
    <div class="why-row">
      <div class="why-label">${label} ${resultBadge(result)}</div>
      <div class="why-text ${ok ? 'ok' : 'bad'}">${esc(text)}</div>
    </div>`;

  return `
    <article class="failure-card ${dx.status}">
      <div class="failure-head">
        <div>
          <div class="failure-title mono">${val(rec.source_ip)}</div>
          <div class="failure-sub">Record ${idx + 1} · ${plural(rec.count || 0, 'message')} · to ${val(rec.envelope_to)}${ipLink}</div>
        </div>
        <div class="failure-badges">
          <span class="badge ${dx.status === 'fail' ? 'fail' : 'warn'}">${dx.status === 'fail' ? 'DMARC FAIL' : 'PARTIAL'}</span>
          ${dispositionBadge(rec.disposition)}
        </div>
      </div>
      <div class="why">
        ${mech('DKIM', dx.dkim.ok, dx.dkim.text, rec.dkim_result)}
        ${mech('SPF', dx.spf.ok, dx.spf.text, rec.spf_result)}
        <div class="why-row">
          <div class="why-label">Impact</div>
          <div class="why-text">${esc(dx.effect)}</div>
        </div>
        ${actions.length ? `
        <div class="why-row">
          <div class="why-label">What to do</div>
          <ul class="why-actions">${actions.map((a) => `<li>${esc(a)}</li>`).join('')}</ul>
        </div>` : ''}
      </div>
      <div class="failure-note">Aggregate reports don't include the individual email (no subject or recipient address). To find the exact message, search the sending system's logs for ${val(rec.envelope_to)} between ${esc(fmtDate(report.date_begin))} and ${esc(fmtDate(report.date_end))}.</div>
    </article>`;
}

function renderFailures(records, report) {
  const withIdx = records.map((rec, i) => [rec, i]);
  const fails = withIdx.filter(([r]) => recordStatus(r) === 'fail');
  const issues = withIdx.filter(([r]) => recordStatus(r) === 'issue');
  const failMsgs = fails.reduce((n, [r]) => n + (r.count || 0), 0);

  const badge = $('#failures-badge');
  const box = $('#failures-container');

  if (fails.length === 0 && issues.length === 0) {
    badge.textContent = 'none';
    badge.className = 'badge pass';
    box.innerHTML = '<div class="empty-ok">All records passed DKIM and SPF alignment. Nothing to fix.</div>';
    return;
  }

  badge.textContent = `${plural(failMsgs, 'failed message')} · ${plural(fails.length, 'source')}`;
  badge.className = `badge ${fails.length ? 'fail' : 'warn'}`;

  let html = '';
  if (fails.length) {
    html += `<h3 class="failures-heading">DMARC failures (${fails.length})</h3>`;
    html += fails.map(([r, i]) => failureCard(r, i, report)).join('');
  }
  if (issues.length) {
    html += `<h3 class="failures-heading">Partial issues — DMARC passed (${issues.length})</h3>`;
    html += issues.map(([r, i]) => failureCard(r, i, report)).join('');
  }
  box.innerHTML = html;
}

// ── Summary ──────────────────────────────────────────────────────────────────

function renderSummary(records, report, analysis) {
  const total = records.reduce((n, r) => n + (r.count || 0), 0);
  const failMsgs = records.filter((r) => recordStatus(r) === 'fail').reduce((n, r) => n + (r.count || 0), 0);
  const passMsgs = total - failMsgs;
  const ips = new Set(records.map((r) => r.source_ip).filter(Boolean)).size;
  const o = analysis.overall;

  const hb = $('#detail-health-badge');
  hb.textContent = `${o.health_score}/100 ${o.health_label}`;
  hb.className = `badge ${o.health_score >= 70 ? 'pass' : o.health_score >= 40 ? 'warn' : 'fail'}`;

  const big = (key, value, color) =>
    [key, `<span style="${color ? `color: var(--${color});` : ''}">${value}</span>`, 'large'];

  $('#summary-grid').innerHTML = detailGrid([
    big('Domain', val(report.domain)),
    big('Total messages', fmtCount(total)),
    big('DMARC pass', fmtCount(passMsgs), 'success'),
    big('DMARC fail', fmtCount(failMsgs), failMsgs ? 'danger' : ''),
    big('Pass rate (by messages)', total ? fmtPct((passMsgs / total) * 100) : '–', 'accent'),
    big('Records', fmtCount(records.length)),
    big('Source IPs', fmtCount(ips)),
    big('Policy', val(report.p)),
  ]);
}

// ── Records list ─────────────────────────────────────────────────────────────

function authEntryRows(rows) {
  return rows.map(([key, value]) => `
    <div class="auth-entry-row"><span class="auth-entry-key">${esc(key)}</span><span>${value}</span></div>`).join('');
}

function recordCard(rec, i, report) {
  const status = recordStatus(rec);
  const el = document.createElement('details');
  el.className = `record-card ${status}`;
  el.dataset.status = status;
  if (status !== 'pass') el.open = true;

  const dkimAuth = rec.dkim_auth || [];
  const spfAuth = rec.spf_auth || [];

  const dkimHtml = dkimAuth.length ? `
    <div class="record-subsection">
      <div class="record-subsection-title">DKIM authentication results (${dkimAuth.length})</div>
      ${dkimAuth.map((dk) => `<div class="auth-entry">${authEntryRows([
        ['Domain', val(dk.domain)],
        ['Result', resultBadge(dk.result)],
        ['Selector', val(dk.selector)],
      ])}</div>`).join('')}
    </div>` : '';

  const spfHtml = spfAuth.length ? `
    <div class="record-subsection">
      <div class="record-subsection-title">SPF authentication results (${spfAuth.length})</div>
      ${spfAuth.map((sp) => `<div class="auth-entry">${authEntryRows([
        ['Domain', val(sp.domain)],
        ['Result', resultBadge(sp.result)],
        ['Scope', val(sp.scope)],
      ])}</div>`).join('')}
    </div>` : '';

  const item = (k, v, cls = '') => `
    <div class="detail-item"><div class="detail-key">${k}</div><div class="detail-value ${cls}">${v}</div></div>`;
  const yesNo = (b) => `<span class="badge ${b ? 'pass' : 'fail'}">${b ? 'YES' : 'NO'}</span>`;

  const statusBadge = status === 'fail'
    ? '<span class="badge fail">DMARC FAIL</span>'
    : status === 'issue'
      ? '<span class="badge warn">PARTIAL</span>'
      : '<span class="badge pass">PASS</span>';

  el.innerHTML = `
    <summary class="record-summary">
      <span class="record-card-title">Record ${i + 1}</span>
      <span class="record-summary-ip mono">${val(rec.source_ip)}</span>
      <span class="record-summary-meta">${plural(rec.count || 0, 'msg')} · ${val(rec.envelope_to)}</span>
      <span class="record-summary-badges">
        <span class="mini-label">DKIM</span>${resultBadge(rec.dkim_result)}
        <span class="mini-label">SPF</span>${resultBadge(rec.spf_result)}
        ${statusBadge}
      </span>
    </summary>
    <div class="record-body">
      <div class="record-grid">
        ${item('Source IP', val(rec.source_ip), 'mono')}
        ${item('Count', fmtCount(rec.count), 'large')}
        ${item('Header from', val(rec.header_from))}
        ${item('Envelope from', val(rec.envelope_from))}
        ${item('Envelope to', val(rec.envelope_to))}
        ${item('Disposition', dispositionBadge(rec.disposition))}
        ${item('DKIM aligned', yesNo(rec.dkim_aligned))}
        ${item('SPF aligned', yesNo(rec.spf_aligned))}
        ${item('DKIM result', resultBadge(rec.dkim_result))}
        ${item('SPF result', resultBadge(rec.spf_result))}
        ${item('DKIM domain', val(rec.dkim_domain))}
        ${item('SPF domain', val(rec.spf_domain))}
      </div>
      ${dkimHtml}
      ${spfHtml}
    </div>`;
  return el;
}

function renderRecords(records, report) {
  const container = $('#records-container');
  container.innerHTML = '';
  if (records.length === 0) {
    container.innerHTML = '<div class="empty-ok">This report contains no records.</div>';
    return;
  }
  records.forEach((rec, i) => container.appendChild(recordCard(rec, i, report)));
}

function applyFilter(filter) {
  document.querySelectorAll('.records-filter .filter-btn').forEach((b) =>
    b.classList.toggle('active', b.dataset.filter === filter));
  document.querySelectorAll('#records-container .record-card').forEach((c) => {
    const s = c.dataset.status;
    const show = filter === 'all' || s === filter || (filter === 'issue' && s !== 'pass');
    c.hidden = !show;
  });
}

function wireToolbar() {
  document.querySelectorAll('.records-filter .filter-btn').forEach((b) =>
    b.addEventListener('click', () => applyFilter(b.dataset.filter)));
  const setAll = (open) => document.querySelectorAll('#records-container .record-card')
    .forEach((c) => { c.open = open; });
  $('#expand-all').addEventListener('click', () => setAll(true));
  $('#collapse-all').addEventListener('click', () => setAll(false));
}

// ── Load + render ────────────────────────────────────────────────────────────

async function loadDetail() {
  // Extract report ID from URL path: /file/123
  const pathParts = window.location.pathname.split('/');
  const reportId = pathParts[pathParts.length - 1];
  if (!reportId || reportId === 'file') {
    $('#metadata-grid').innerHTML = '<p style="color: var(--danger);">No report ID specified.</p>';
    return;
  }

  let res;
  try {
    res = await fetch(`/api/reports/${encodeURIComponent(reportId)}/detail`);
  } catch (err) {
    $('#metadata-grid').innerHTML = '<p style="color: var(--danger);">Could not reach the server.</p>';
    return;
  }
  if (!res.ok) {
    $('#metadata-grid').innerHTML = '<p style="color: var(--danger);">Report not found.</p>';
    return;
  }

  renderDetail(await res.json());
}

function renderDetail(data) {
  const r = data.report;
  const records = data.records || [];
  const analysis = data.analysis;

  // ── Breadcrumb + header ─────────────────────────────────────────────────
  $('#detail-filename').textContent = r.xml_filename;
  $('#detail-org').textContent = r.org_name || 'Unknown';
  $('#detail-record-count').textContent = `${records.length} record${records.length !== 1 ? 's' : ''}`;

  renderSummary(records, r, analysis);
  renderFailures(records, r);

  // ── Metadata grid ───────────────────────────────────────────────────────
  $('#metadata-grid').innerHTML = detailGrid([
    ['Organization', val(r.org_name)],
    ['Organization email', val(r.org_email)],
    ['Report ID', val(r.report_id), 'mono'],
    ['Date range (begin)', esc(fmtDate(r.date_begin))],
    ['Date range (end)', esc(fmtDate(r.date_end))],
    ['Domain', val(r.domain)],
    ['XML filename', val(r.xml_filename), 'mono'],
    ['Archive filename', val(r.archive_filename), 'mono'],
    ['Ingested at', esc(fmtDate(r.created_at))],
  ]);

  // ── Policy grid ─────────────────────────────────────────────────────────
  $('#policy-grid').innerHTML = detailGrid([
    ['DKIM alignment mode (adkim)', val(r.adkim)],
    ['SPF alignment mode (aspf)', val(r.aspf)],
    ['Policy (p)', val(r.p)],
    ['Subdomain policy (sp)', val(r.sp)],
    ['Percentage applied (pct)', r.pct != null ? esc(r.pct) + '%' : '–'],
  ]);

  // ── Alignment summary (record counts, as computed by the analysis engine) ─
  const a = analysis.alignment;
  const stat = (key, value, color) =>
    [key, `<span style="${color ? `color: var(--${color});` : ''}">${value}</span>`, 'large'];
  $('#alignment-summary').innerHTML = detailGrid([
    stat('DKIM pass', fmtCount(a.dkim_pass), 'success'),
    stat('DKIM fail', fmtCount(a.dkim_fail), 'danger'),
    stat('DKIM pass rate', fmtPct(a.dkim_pass_rate)),
    stat('SPF pass', fmtCount(a.spf_pass), 'success'),
    stat('SPF fail', fmtCount(a.spf_fail), 'danger'),
    stat('SPF pass rate', fmtPct(a.spf_pass_rate)),
    stat('Both pass', fmtCount(a.both_pass)),
    stat('Both fail', fmtCount(a.both_fail), 'danger'),
    stat('Either pass (DMARC compliant)', fmtCount(a.either_pass), 'accent'),
    stat('Overall pass rate', fmtPct(a.overall_pass_rate), 'accent'),
  ]);

  renderRecords(records, r);
}

// ── Init ──────────────────────────────────────────────────────────────────────

wireToolbar();
loadDetail();
