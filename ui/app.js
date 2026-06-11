/* =========================================================================
   JOB HUNT HACKER — vanilla JS app
   No frameworks. No dependencies. All API calls relative to same origin.
   ========================================================================= */

(() => {
  'use strict';

  // ----- state -----
  const state = {
    page: 'landing',
    profile: null,
    settings: null,
    jobs: [],
    selectedJob: null,
    selectedResume: null,
    resumes: [],
    applications: [],
    availability: {},     // {dayIdx: {hourIdx: true}}
  };

  // ----- api helper -----
  const api = {
    // Count of in-flight requests; drives the global activity bar so the
    // user always sees when the app is talking to the backend.
    _inflight: 0,
    _bump(delta) {
      this._inflight = Math.max(0, this._inflight + delta);
      const bar = document.getElementById('activity-bar');
      if (bar) bar.classList.toggle('active', this._inflight > 0);
    },
    async _req(method, path, body, opts = {}) {
      const init = { method, headers: {} };
      if (body !== undefined && !(body instanceof FormData)) {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(body);
      } else if (body instanceof FormData) {
        init.body = body;
      }
      this._bump(1);
      try {
        const r = await fetch(path, init);
        const ctype = r.headers.get('content-type') || '';
        let data = null;
        if (ctype.includes('application/json')) {
          data = await r.json().catch(() => null);
        } else {
          data = await r.text().catch(() => null);
        }
        if (!r.ok) {
          const msg = (data && (data.detail || data.error || data.message)) || `${r.status} ${r.statusText}`;
          if (!opts.silent) toast(`${method} ${path}: ${msg}`, 'error');
          return { ok: false, status: r.status, error: msg, data };
        }
        return data && typeof data === 'object' && 'ok' in data ? data : { ok: true, data };
      } catch (e) {
        if (!opts.silent) toast(`Network error: ${e.message}`, 'error');
        return { ok: false, error: e.message };
      } finally {
        this._bump(-1);
      }
    },
    get(p, opts)        { return this._req('GET', p, undefined, opts); },
    post(p, b, opts)    { return this._req('POST', p, b, opts); },
    put(p, b, opts)     { return this._req('PUT', p, b, opts); },
    patch(p, b, opts)   { return this._req('PATCH', p, b, opts); },
    del(p, opts)        { return this._req('DELETE', p, undefined, opts); },
    delete(p, opts)     { return this._req('DELETE', p, undefined, opts); },
  };

  // ----- toasts -----
  function toast(msg, kind = '') {
    const host = document.getElementById('toasts');
    if (!host) return;
    const el = document.createElement('div');
    el.className = 'toast ' + (kind || '');
    el.textContent = msg;
    host.appendChild(el);
    setTimeout(() => el.remove(), 4200);
  }

  // ----- DOM helpers -----
  const $  = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  function el(tag, attrs = {}, children = []) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') e.className = v;
      else if (k === 'html') e.innerHTML = v;
      else if (k === 'text') e.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
      else if (v === false || v == null) continue;
      else e.setAttribute(k, v === true ? '' : v);
    }
    for (const c of [].concat(children)) {
      if (c == null) continue;
      e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    }
    return e;
  }
  function fmtDate(ts) {
    if (!ts) return '—';
    const n = typeof ts === 'number' ? ts : parseFloat(ts);
    if (!n || Number.isNaN(n)) return '—';
    const d = new Date(n < 1e12 ? n * 1000 : n);
    return d.toISOString().slice(0, 10);
  }
  function fmtRel(ts) {
    if (!ts) return '—';
    const n = typeof ts === 'number' ? ts : parseFloat(ts);
    if (!n || Number.isNaN(n)) return '—';
    const sec = (Date.now() / 1000) - (n < 1e12 ? n : n / 1000);
    if (sec < 3600) return Math.max(0, Math.round(sec / 60)) + 'm';
    if (sec < 86400) return Math.round(sec / 3600) + 'h';
    if (sec < 86400 * 7) return Math.round(sec / 86400) + 'd';
    if (sec < 86400 * 30) return Math.round(sec / 86400 / 7) + 'w';
    return Math.round(sec / 86400 / 30) + 'mo';
  }
  function fmtSalary(min, max, currency) {
    if (!min && !max) return '—';
    const c = currency && currency !== 'USD' ? ` ${currency}` : '';
    const sign = currency === 'USD' || !currency ? '$' : '';
    const fmt = n => n ? `${sign}${Math.round(n / 1000)}k` : '?';
    if (min && max) return `${fmt(min)}–${fmt(max)}${c}`;
    return `${fmt(min || max)}+${c}`;
  }
  function safeText(s) {
    return (s == null ? '' : String(s));
  }
  function csvToList(s) {
    if (!s) return [];
    return String(s).split(/[,\n]/).map(x => x.trim()).filter(Boolean);
  }
  function listToCsv(arr) {
    return Array.isArray(arr) ? arr.join(', ') : (arr || '');
  }
  function scoreClass(score) {
    if (score == null) return '';
    if (score >= 85) return 'score-strong';
    if (score >= 70) return 'score-ok';
    return 'score-weak';
  }
  function scoreLabel(score) {
    if (score == null) return 'WEAK';
    if (score >= 85) return 'STRONG';
    if (score >= 70) return 'OK';
    return 'WEAK';
  }

  // ----- form serialize -----
  function serializeForm(form) {
    const out = {};
    for (const elx of form.elements) {
      if (!elx.name) continue;
      if (elx.type === 'checkbox') continue;
      if (elx.tagName === 'BUTTON') continue;
      let v = elx.value;
      if (elx.type === 'number') v = v === '' ? null : Number(v);
      out[elx.name] = v;
    }
    // checkbox groups
    for (const grp of form.querySelectorAll('.check-grid[data-name]')) {
      const name = grp.dataset.name;
      out[name] = $$('input[type="checkbox"]:checked', grp).map(c => c.value);
    }
    // single checkboxes by name
    for (const cb of form.querySelectorAll('input[type="checkbox"][name]')) {
      out[cb.name] = !!cb.checked;
    }
    return out;
  }

  // ----- page routing -----
  const PAGES = ['landing','setup','vault','dashboard','resume','pipeline','inbox','calendar','interview','intel','network','offers','settings'];
  function switchPage(name) {
    if (!PAGES.includes(name)) name = 'landing';
    state.page = name;
    $$('.page').forEach(p => p.classList.toggle('active', p.dataset.page === name));
    $$('.tabs a').forEach(a => a.classList.toggle('active', a.dataset.tab === name));
    window.scrollTo({ top: 0, behavior: 'instant' });
    if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
    try { localStorage.setItem('jhh.lastPage', name); } catch (_) {}

    // page-specific lazy loads
    if (name === 'landing')   refreshDemoCta();
    if (name === 'setup')     loadProfile();
    if (name === 'vault')     { loadVault(); loadVaultSummary(); }
    if (name === 'dashboard') { loadJobs(); loadSavedSearches(); }
    if (name === 'resume')    loadResumes();
    if (name === 'pipeline')  loadPipeline();
    if (name === 'inbox')     loadInbox();
    if (name === 'calendar')  { renderAvailGrid(); loadCalendarEvents(); }
    if (name === 'intel')     loadIntel();
    if (name === 'interview') loadInterview();
    if (name === 'network')   loadNetwork();
    if (name === 'offers')    loadOffers();
    if (name === 'settings')  loadSettings();
  }
  function bindRouting() {
    window.addEventListener('hashchange', () => switchPage(location.hash.replace('#','')));
    $$('.tabs a').forEach(a => a.addEventListener('click', (e) => {
      e.preventDefault();
      switchPage(a.dataset.tab);
    }));
  }

  // ----- keyboard shortcuts -----
  // `g` then a letter jumps to a tab; `/` focuses job search; `[`/`]` cycle
  // tabs; `?` shows the cheat-sheet; Esc closes whatever is open. All
  // shortcuts are inert while typing in a field.
  const GOTO_MAP = {
    l: 'landing',  u: 'setup',    v: 'vault',   d: 'dashboard',
    r: 'resume',   p: 'pipeline', i: 'inbox',   c: 'calendar',
    w: 'interview', t: 'intel',   n: 'network', o: 'offers',
    s: 'settings',
  };
  let _gotoArmed = 0;

  function closeTopmost() {
    const openModal = $$('.modal').find(m => !m.classList.contains('hidden'));
    if (openModal) { openModal.classList.add('hidden'); return true; }
    const detail = $('#job-detail');
    if (detail && !detail.classList.contains('hidden')) {
      detail.classList.add('hidden');
      return true;
    }
    return false;
  }

  function toggleShortcutsModal(force) {
    const m = $('#shortcuts-modal');
    if (!m) return;
    const show = force !== undefined ? force : m.classList.contains('hidden');
    m.classList.toggle('hidden', !show);
  }

  function bindKeyboard() {
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { closeTopmost(); return; }
      // Never hijack typing, IME composition, or chorded browser shortcuts.
      const t = e.target;
      if (e.ctrlKey || e.metaKey || e.altKey || e.isComposing) return;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
                t.tagName === 'SELECT' || t.isContentEditable)) return;

      const now = Date.now();
      if (_gotoArmed && now - _gotoArmed < 1200) {
        _gotoArmed = 0;
        const dest = GOTO_MAP[e.key.toLowerCase()];
        if (dest) { e.preventDefault(); switchPage(dest); }
        return;
      }

      if (e.key === 'g') { _gotoArmed = now; return; }
      if (e.key === '?') { e.preventDefault(); toggleShortcutsModal(); return; }
      if (e.key === '/') {
        e.preventDefault();
        switchPage('dashboard');
        const q = $('#search-form input[name="query"]');
        if (q) q.focus();
        return;
      }
      if (e.key === '[' || e.key === ']') {
        e.preventDefault();
        const idx = PAGES.indexOf(state.page);
        const next = e.key === ']'
          ? PAGES[(idx + 1) % PAGES.length]
          : PAGES[(idx - 1 + PAGES.length) % PAGES.length];
        switchPage(next);
      }
    });
    const closeBtn = $('#shortcuts-close');
    if (closeBtn) closeBtn.addEventListener('click', () => toggleShortcutsModal(false));
    const m = $('#shortcuts-modal');
    if (m) m.addEventListener('click', (e) => { if (e.target === m) toggleShortcutsModal(false); });
    const hint = $('#kbd-hint');
    if (hint) hint.addEventListener('click', () => toggleShortcutsModal(true));
  }

  // ----- health + settings (boot) -----
  async function bootStatus() {
    const r = await api.get('/api/health', { silent: true });
    const pill = $('#health-pill');
    if (r.ok) {
      pill.textContent = `OK · v${r.version || '0'}`;
      pill.classList.remove('pill-muted');
      pill.classList.add('pill-green');
      if (r.auto_apply_enabled) {
        $('#compliance-banner').classList.remove('hidden');
      }
      if (r.default_mode) {
        $('#mode-pill').textContent = 'MODE: ' + String(r.default_mode).toUpperCase();
      }
    } else {
      pill.textContent = 'OFFLINE';
      pill.classList.remove('pill-muted');
      pill.classList.add('pill-red');
    }
  }

  // ============================================================
  // AUTOPILOT — one-step "just find me a job" flow
  // ============================================================
  function bindAutopilot() {
    const openBtn = $('#autopilot-open');
    const modal = $('#autopilot-modal');
    if (!openBtn || !modal) return;

    openBtn.addEventListener('click', () => {
      modal.classList.remove('hidden');
      $('#autopilot-file').focus();
    });
    $('#autopilot-cancel').addEventListener('click', () => modal.classList.add('hidden'));
    modal.addEventListener('click', (e) => {
      if (e.target === modal) modal.classList.add('hidden');
      // Any "jump to tab" link inside the result panel should also close the
      // modal so the user actually lands on the tab they clicked.
      const closer = e.target.closest('[data-close-autopilot]');
      if (closer) modal.classList.add('hidden');
    });

    $('#autopilot-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData();
      const fileEl = $('#autopilot-file');
      if (fileEl.files && fileEl.files[0]) fd.append('resume_file', fileEl.files[0]);
      for (const [id, name] of [
        ['#autopilot-linkedin', 'linkedin_text'],
        ['#autopilot-li-url', 'linkedin_url'],
        ['#autopilot-gh-url', 'github_url'],
        ['#autopilot-pf-url', 'portfolio_url'],
        ['#ap-tailor-top', 'tailor_top'],
        ['#ap-packet-top', 'packet_top'],
        ['#ap-search-results', 'search_results'],
        ['#ap-hours-old', 'search_hours_old'],
        ['#ap-recur', 'daily_recurrence'],
        ['#ap-sites', 'sites'],
      ]) {
        const v = ($(id).value || '').trim();
        if (v) fd.append(name, v);
      }
      if (!fd.has('resume_file') && !fd.has('linkedin_text') && !fd.has('linkedin_url')) {
        toast('Drop a resume or paste your LinkedIn first.', 'warn');
        return;
      }

      // ---- Capture the four explicit picks the user made (employment,
      // location, remote/country, comp, interview availability) and persist
      // them to the profile right before autopilot runs so the inferred
      // profile MERGES with these instead of overwriting. ----

      const employmentTypes = Array.from(
        document.querySelectorAll('#autopilot-employment-types input[type="checkbox"]:checked')
      ).map(cb => cb.value);

      // Remote preference is now structured: "remote-us", "remote-emea", etc.
      // We split into the raw mode AND a preferred-region string.
      const remoteRaw = $('#autopilot-remote').value || '';
      let remoteMode = '';
      let remoteRegion = '';
      if (remoteRaw.startsWith('remote')) {
        remoteMode = 'remote';
        const suffix = remoteRaw.replace(/^remote-?/, '');
        const REGION_LABEL = {
          'worldwide': 'Remote · Worldwide',
          'us':        'Remote · US Only',
          'canada':    'Remote · Canada',
          'emea':      'Remote · Europe / EMEA',
          'latam':     'Remote · LATAM',
          'apac':      'Remote · APAC',
          'uk':        'Remote · UK',
          '':          'Remote',
        };
        remoteRegion = REGION_LABEL[suffix] || 'Remote';
      } else if (remoteRaw) {
        remoteMode = remoteRaw;     // 'hybrid' | 'onsite'
      }

      const compRange = $('#autopilot-comp-range').value || '';
      const locationPref = ($('#autopilot-location').value || '').trim();
      let minSalary = null, prefSalary = null;
      if (compRange.includes(':')) {
        const [lo, hi] = compRange.split(':').map(n => parseInt(n, 10));
        minSalary = lo;
        prefSalary = hi;
      }

      // Interview availability → encode as a structured JSON object the
      // Calendar tab understands: { tz, window:{start,end}, days:[...] }.
      const availDays = Array.from(
        document.querySelectorAll('#autopilot-avail-days input[type="checkbox"]:checked')
      ).map(cb => cb.value);
      const availWindowKey = $('#autopilot-avail-window').value;
      const WINDOWS = {
        morning:   { start: 9,  end: 12 },
        afternoon: { start: 12, end: 17 },
        evening:   { start: 17, end: 20 },
        business:  { start: 9,  end: 17 },
        anytime:   { start: 9,  end: 20 },
      };
      const availWindow = WINDOWS[availWindowKey] || WINDOWS.afternoon;
      const availTz = $('#autopilot-avail-tz').value || 'America/New_York';
      const interviewAvailability = {
        timezone: availTz,
        days: availDays,
        window: availWindow,
        window_label: availWindowKey,
      };

      const preProfile = {};
      if (employmentTypes.length) preProfile.employment_types = employmentTypes;
      if (remoteMode) preProfile.remote_preference = remoteMode;
      const prefLocs = [];
      if (locationPref) {
        preProfile.location = locationPref;
        prefLocs.push(locationPref);
      }
      if (remoteRegion) prefLocs.push(remoteRegion);
      if (prefLocs.length) preProfile.preferred_locations = prefLocs;
      if (minSalary != null) preProfile.minimum_salary = minSalary;
      if (prefSalary != null) preProfile.preferred_salary = prefSalary;
      preProfile.interview_availability_json = interviewAvailability;

      if (Object.keys(preProfile).length) {
        await api.put('/api/profile', preProfile, { silent: true });
      }

      const goBtn = $('#autopilot-go');
      const cancelBtn = $('#autopilot-cancel');
      const form = $('#autopilot-form');
      const progress = $('#autopilot-progress');
      const result = $('#autopilot-result');

      goBtn.disabled = true;
      goBtn.textContent = 'RUNNING…';
      cancelBtn.textContent = 'CLOSE';
      // Collapse the form during the run so progress is the only thing in view
      form.classList.add('autopilot-running');
      progress.classList.remove('hidden');
      result.classList.add('hidden');
      progress.innerHTML = renderAutopilotProgress([
        { name: 'profile_inferred', label: 'Inferring profile from resume', status: 'pending' },
        { name: 'vault_populated', label: 'Populating Career Evidence Vault', status: 'pending' },
        { name: 'search_complete', label: 'Searching every job board (9 sources)', status: 'pending' },
        { name: 'scoring_complete', label: 'Scoring every job vs your evidence', status: 'pending' },
        { name: 'tailoring_complete', label: 'Tailoring resumes for top matches', status: 'pending' },
        { name: 'packets_built', label: 'Building application packets', status: 'pending' },
        { name: 'saved_search_registered', label: 'Scheduling daily re-run', status: 'pending' },
      ], { running: true });
      // Make sure the user actually sees the progress — scroll it into view
      // inside the modal even on small screens.
      try { progress.scrollIntoView({ block: 'start', behavior: 'smooth' }); } catch (_) {}

      const r = await api.post('/api/autopilot/start', fd, { silent: true });
      goBtn.disabled = false;
      goBtn.textContent = 'START AUTOPILOT';
      form.classList.remove('autopilot-running');

      if (!r.ok && !r.data) {
        toast('Autopilot failed: ' + (r.error || 'unknown'), 'error');
        progress.innerHTML = `<div class="ap-error">Autopilot failed: ${(r.error || 'unknown').replace(/</g,'&lt;')}</div>`;
        return;
      }
      const d = r.data || {};
      progress.innerHTML = renderAutopilotProgress(d.steps || [], { running: false });
      result.classList.remove('hidden');
      result.innerHTML = renderAutopilotResult(d);
      try { result.scrollIntoView({ block: 'start', behavior: 'smooth' }); } catch (_) {}
      toast(`Autopilot finished in ${(d.elapsed_ms || 0)/1000}s — ${d.packets?.built || 0} packets ready.`,
            'success');
      // Refresh background pill + nav stats
      await loadAutopilotPill();
      bootStatus();
      // Autopilot's profile-inference step may have produced a proposal
      // that's waiting on a human-review decision. Refresh the pill list
      // so the user notices it from the Setup page.
      refreshProposalsPills();
    });

    loadAutopilotPill();
  }

  function renderAutopilotProgress(steps, opts) {
    const running = !!(opts && opts.running);
    const allOk = !running && (steps || []).length && (steps || []).every(s => s.status === 'ok');
    const anyErr = !running && (steps || []).some(s => s.status === 'error');
    const rows = (steps || []).map(s => {
      const icon = s.status === 'ok' ? '✓' :
                   s.status === 'error' ? '×' :
                   running ? '◐' : '·';
      const cls = s.status === 'ok' ? 'ap-ok' :
                  s.status === 'error' ? 'ap-err' :
                  running ? 'ap-running' : 'ap-pending';
      return `<li class="ap-step ${cls}"><span class="ap-icon">${icon}</span>
                <span class="ap-name">${s.label || s.name}</span>
                <span class="ap-detail">${(s.detail || '').replace(/</g,'&lt;')}</span></li>`;
    }).join('');
    let header = '';
    if (running) {
      header = `<div class="ap-status-banner ap-status-running">
        <strong>AUTOPILOT RUNNING…</strong>
        <span class="muted small">Synchronous run — typically 5–30s. Don't close this window.</span>
      </div>`;
    } else if (allOk) {
      header = `<div class="ap-status-banner ap-status-ok">
        <strong>AUTOPILOT COMPLETE</strong>
        <span class="muted small">All steps finished — see results below.</span>
      </div>`;
    } else if (anyErr) {
      header = `<div class="ap-status-banner ap-status-err">
        <strong>AUTOPILOT FINISHED WITH ERRORS</strong>
        <span class="muted small">See per-step details below.</span>
      </div>`;
    }
    return `${header}<ul class="ap-list">${rows}</ul>`;
  }

  function renderAutopilotResult(d) {
    const parts = [];
    parts.push(`<h4 class="ap-result-h">DONE IN ${((d.elapsed_ms || 0)/1000).toFixed(1)}S</h4>`);
    parts.push('<div class="ap-kpis">');
    parts.push(`<span class="ap-kpi"><strong>${d.search?.discovered ?? 0}</strong> discovered</span>`);
    parts.push(`<span class="ap-kpi"><strong>${d.search?.inserted ?? 0}</strong> new jobs</span>`);
    parts.push(`<span class="ap-kpi"><strong>${d.scoring?.scored ?? 0}</strong> scored</span>`);
    parts.push(`<span class="ap-kpi"><strong>${d.tailoring?.tailored ?? 0}</strong> tailored</span>`);
    parts.push(`<span class="ap-kpi"><strong>${d.packets?.built ?? 0}</strong> packets</span>`);
    parts.push('</div>');

    // WHERE TO TRACK — primary handoff. Lives at top of the result panel
    // because the modal stays open and the user otherwise has no obvious
    // next step.
    parts.push(`
      <div class="ap-handoff">
        <h4>TRACK YOUR HUNT</h4>
        <p class="muted small">The popup will stay open so you can review packets here.
          Everything is also wired into the main tabs below — click to jump.</p>
        <div class="ap-handoff-grid">
          <a class="ap-handoff-card" href="#pipeline" data-close-autopilot>
            <strong>PIPELINE</strong>
            <span class="muted small">Kanban of every application Autopilot prepared (status=prepared). Move them through Applied → Interview → Offer.</span>
          </a>
          <a class="ap-handoff-card" href="#dashboard" data-close-autopilot>
            <strong>DASHBOARD</strong>
            <span class="muted small">All ${d.search?.discovered ?? 0} discovered jobs with match scores, salary, source, and one-click TAILOR.</span>
          </a>
          <a class="ap-handoff-card" href="#resume" data-close-autopilot>
            <strong>RESUME LAB</strong>
            <span class="muted small">The ${d.tailoring?.tailored ?? 0} tailored resumes — diff vs. base, ATS score, honesty report per claim.</span>
          </a>
          <a class="ap-handoff-card" href="#vault" data-close-autopilot>
            <strong>EVIDENCE VAULT</strong>
            <span class="muted small">Every claim extracted from your resume, with provenance. Nothing is fabricated — what's here is what gets used.</span>
          </a>
          <a class="ap-handoff-card" href="#intel" data-close-autopilot>
            <strong>INTEL</strong>
            <span class="muted small">Velocity funnel, top skill gaps, salary read, company effectiveness leaderboard.</span>
          </a>
          <a class="ap-handoff-card" href="#inbox" data-close-autopilot>
            <strong>INBOX</strong>
            <span class="muted small">Recruiter replies pulled into one place (after you wire Gmail in Settings).</span>
          </a>
        </div>
      </div>`);

    const paths = d.packets?.paths || [];
    if (paths.length) {
      parts.push('<h4>TOP PACKETS READY FOR REVIEW</h4><ol class="ap-packets">');
      for (const p of paths) {
        parts.push(`<li>
          <strong>${(p.title || '').replace(/</g,'&lt;')}</strong>
          @ ${(p.company || '').replace(/</g,'&lt;')}
          — score ${(Number(p.score || 0) * 100).toFixed(0)}
          <span class="ap-path muted small">${(p.packet_dir || '').replace(/</g,'&lt;')}</span>
        </li>`);
      }
      parts.push('</ol>');
      parts.push('<p class="muted small">Auto-apply is OFF by default. Packets are <strong>prepared</strong> in your pipeline; you click APPLY when you\'re ready.</p>');
    }
    if (d.saved_search?.created) {
      parts.push(`<p class="muted small">Recurring saved search active: <strong>${(d.saved_search.label || '').replace(/</g,'&lt;')}</strong> — re-runs every ${d.saved_search.frequency_hours || 24}h.</p>`);
    }
    parts.push('<div class="ap-actions">');
    parts.push('<a class="btn btn-primary" href="#pipeline" data-close-autopilot>OPEN PIPELINE</a>');
    parts.push('<a class="btn btn-ghost" href="#dashboard" data-close-autopilot>OPEN DASHBOARD</a>');
    parts.push('</div>');
    return parts.join('');
  }

  async function loadAutopilotPill() {
    const r = await api.get('/api/autopilot/status', { silent: true });
    const pill = $('#autopilot-pill');
    if (!pill) return;
    if (r.ok && r.data?.active) {
      pill.classList.remove('hidden');
      pill.textContent = `AUTOPILOT ON — ${r.data.label} · every ${r.data.frequency_hours}h`;
      pill.classList.add('autopilot-pill-on');
    } else {
      pill.classList.add('hidden');
    }
  }

  // ============================================================
  // PROFILE / SETUP
  // ============================================================
  async function loadProfile() {
    const r = await api.get('/api/profile', { silent: true });
    if (!r.ok) {
      $('#profile-status').textContent = 'No profile loaded yet.';
      return;
    }
    const p = r.data || {};
    state.profile = p;
    const f = $('#profile-form');
    if (!f) return;
    for (const [k, v] of Object.entries(p)) {
      const inp = f.elements.namedItem(k);
      if (!inp) continue;
      if (inp.type === 'checkbox') inp.checked = !!v;
      else if (Array.isArray(v)) inp.value = listToCsv(v);
      else if (v && typeof v === 'object') continue;
      else if (v != null) inp.value = v;
    }
    // checkbox groups
    for (const grp of f.querySelectorAll('.check-grid[data-name]')) {
      const name = grp.dataset.name;
      const arr = Array.isArray(p[name]) ? p[name] : [];
      for (const cb of grp.querySelectorAll('input[type="checkbox"]')) {
        cb.checked = arr.includes(cb.value);
      }
    }
    // restore availability for calendar tab
    if (p.interview_availability_json && typeof p.interview_availability_json === 'object') {
      state.availability = p.interview_availability_json;
    }
    $('#profile-status').textContent = 'Profile loaded.';
  }
  function bindProfileForm() {
    const f = $('#profile-form');
    if (!f) return;
    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      const data = serializeForm(f);
      const listFields = ['target_titles','target_keywords','excluded_keywords',
        'preferred_locations','industries','excluded_industries',
        'preferred_companies','excluded_companies','visa_preferences'];
      for (const k of listFields) {
        if (typeof data[k] === 'string') data[k] = csvToList(data[k]);
      }
      if (data.mode === '') delete data.mode;
      const r = await api.put('/api/profile', data);
      if (r.ok) {
        toast('Profile saved.', 'success');
        $('#profile-status').textContent = 'Saved at ' + new Date().toLocaleTimeString();
      }
    });
    $('#profile-reload').addEventListener('click', loadProfile);
  }

  // ----- Quick Setup: infer profile from resume + LinkedIn -----
  function bindInferForm() {
    const f = $('#infer-form');
    if (!f) return;

    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData();
      const fileEl = $('#infer-file');
      if (fileEl && fileEl.files && fileEl.files[0]) {
        fd.append('resume_file', fileEl.files[0]);
      }
      const linkedinText = $('#infer-linkedin').value.trim();
      if (linkedinText) fd.append('linkedin_text', linkedinText);
      for (const name of ['linkedin_url', 'github_url', 'portfolio_url']) {
        const v = f.elements.namedItem(name).value.trim();
        if (v) fd.append(name, v);
      }
      if (!fd.has('resume_file') && !fd.has('linkedin_text') &&
          !fd.has('linkedin_url') && !fd.has('github_url') && !fd.has('portfolio_url')) {
        toast('Add at least a resume file or LinkedIn text first.', 'warn');
        return;
      }
      $('#infer-status').textContent = 'Parsing…';
      const r = await api.post('/api/profile/infer', fd);
      if (!r.ok) {
        $('#infer-status').textContent = 'Failed to infer.';
        return;
      }
      const inferred = r.inferred_fields || [];
      const meta = r.inferred_meta || {};
      const used = r.sources_used || [];
      applyInferredToProfileForm(r.data || {}, inferred);
      renderInferMeta(used, meta, r.notes || []);
      const fieldCount = inferred.length;
      $('#infer-status').textContent =
        fieldCount > 0
          ? `Inferred ${fieldCount} field${fieldCount === 1 ? '' : 's'}. Review and SAVE PROFILE when ready.`
          : 'No fields could be inferred — try adding more text.';
      toast(fieldCount > 0
        ? `Profile pre-filled (${fieldCount} fields). Review and save.`
        : 'No fields inferred — paste more text.',
        fieldCount > 0 ? 'success' : 'warn');

      // Human review gate: if the deterministic parser and the LLM
      // disagree on any field, surface the side-by-side picker so the
      // user resolves it before anything is committed to the profile.
      maybeOpenProposalGate(r);
      refreshProposalsPills();
    });

    $('#infer-clear').addEventListener('click', () => {
      const pf = $('#profile-form');
      if (!pf) return;
      pf.querySelectorAll('.field-inferred').forEach(el => el.classList.remove('field-inferred'));
      $('#infer-meta').hidden = true;
      $('#infer-meta').innerHTML = '';
      $('#infer-status').textContent = '';
    });
  }

  function applyInferredToProfileForm(data, inferredFields) {
    const f = $('#profile-form');
    if (!f) return;
    const inferredSet = new Set(inferredFields);

    // Standard scalar + list fields
    for (const [k, v] of Object.entries(data)) {
      const inp = f.elements.namedItem(k);
      if (!inp) continue;
      if (Array.isArray(v)) {
        if (v.length) inp.value = listToCsv(v);
      } else if (v && typeof v === 'object') {
        continue;
      } else if (v != null && v !== '') {
        inp.value = v;
      }
      if (inferredSet.has(k)) {
        // Visual hint on the wrapping <label>
        const wrap = inp.closest('label') || inp;
        wrap.classList.add('field-inferred');
      }
    }

    // Checkbox groups (employment_types, seniority_targets)
    for (const grp of f.querySelectorAll('.check-grid[data-name]')) {
      const name = grp.dataset.name;
      const arr = Array.isArray(data[name]) ? data[name] : [];
      if (!arr.length) continue;
      for (const cb of grp.querySelectorAll('input[type="checkbox"]')) {
        if (arr.includes(cb.value)) cb.checked = true;
      }
      if (inferredSet.has(name)) {
        const wrap = grp.closest('label') || grp;
        wrap.classList.add('field-inferred');
      }
    }

    $('#profile-status').textContent =
      'Pre-filled from resume / LinkedIn. Highlighted rows came from inference — review and edit.';
  }

  function renderInferMeta(sourcesUsed, fieldMeta, notes) {
    const host = $('#infer-meta');
    if (!host) return;
    host.innerHTML = '';
    host.hidden = false;

    const sourceLine = sourcesUsed.length
      ? sourcesUsed.map(s => {
          if (s.kind === 'resume')
            return `resume "${s.filename}" (${s.experience_entries} roles, ${s.skills_found} skills)`;
          if (s.kind === 'linkedin')
            return `linkedin (${(s.sections_found || []).join(', ') || 'no sections'})`;
          return s.kind;
        }).join(' · ')
      : 'no sources parsed';

    host.appendChild(el('div', { class: 'kv-row' },
      el('span', { class: 'kv-key', text: 'sources:' }),
      el('span', { class: 'kv-val', text: sourceLine })));

    const fields = Object.keys(fieldMeta);
    if (fields.length) {
      host.appendChild(el('div', { class: 'kv-row' },
        el('span', { class: 'kv-key', text: 'fields filled:' }),
        el('span', { class: 'kv-val', text: fields.join(', ') })));
    }
    if (notes && notes.length) {
      host.appendChild(el('div', { class: 'kv-row' },
        el('span', { class: 'kv-key', text: 'notes:' }),
        el('span', { class: 'kv-val', text: notes.join(' | ') })));
    }
  }

  // ----- evidence upload -----
  function bindEvidence() {
    const dz = $('#dropzone');
    const input = $('#evidence-files');
    if (!dz || !input) return;
    dz.addEventListener('click', () => input.click());
    dz.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
    });
    dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('drag'); });
    dz.addEventListener('dragleave', () => dz.classList.remove('drag'));
    dz.addEventListener('drop', async (e) => {
      e.preventDefault();
      dz.classList.remove('drag');
      await uploadFiles(e.dataTransfer.files);
    });
    input.addEventListener('change', () => uploadFiles(input.files));

    const previewBtn = $('#url-preview-btn');
    if (previewBtn) {
      previewBtn.addEventListener('click', async () => {
        const f = $('#url-form');
        const url = (f.elements.namedItem('url').value || '').trim();
        const sourceType = f.elements.namedItem('source_type').value || '';
        if (!url) { toast('Enter a URL first.', 'warn'); return; }
        const status = $('#url-preview-status');
        const out = $('#url-preview-out');
        status.textContent = 'fetching…';
        out.classList.add('hidden');
        const r = await api.post('/api/urls/preview', { url, source_type: sourceType });
        if (!r.ok) { status.textContent = 'preview failed'; return; }
        const d = r.data || {};
        out.textContent = `URL: ${d.url || url}\nTITLE: ${d.title || ''}\nTYPE: ${d.content_type || ''}\n\n${(d.text || '').slice(0, 2000)}`;
        out.classList.remove('hidden');
        status.textContent = `previewed (${(d.text || '').length} chars)`;
      });
    }

    $('#url-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = serializeForm(e.target);
      const r = await api.post('/api/evidence/url', fd);
      if (r.ok) {
        toast('URL ingested.', 'success');
        // If this looks like a portfolio/about/github URL, also let it
        // contribute to the profile auto-fill.
        if (isSetupVisible() && fd.url) {
          const isGh = /github\.com/i.test(fd.url);
          const isLi = /linkedin\.com/i.test(fd.url);
          const inferFD = new FormData();
          if (isGh) inferFD.append('github_url', fd.url);
          else if (isLi) inferFD.append('linkedin_url', fd.url);
          else inferFD.append('portfolio_url', fd.url);
          await runInferAndApply(inferFD, { silent: true });
        }
        e.target.reset();
      }
    });
    $('#github-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = serializeForm(e.target);
      fd.repo_urls = csvToList(fd.repo_urls);
      const r = await api.post('/api/github/ingest', fd);
      if (r.ok) {
        toast('GitHub ingest scheduled.', 'success');
        if (isSetupVisible() && fd.profile_url) {
          const inferFD = new FormData();
          inferFD.append('github_url', fd.profile_url);
          await runInferAndApply(inferFD, { silent: true });
        }
        e.target.reset();
      }
    });
    $('#linkedin-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = serializeForm(e.target);
      // No dedicated /evidence/linkedin endpoint; route to text or url.
      let r;
      if (fd.text && fd.text.trim()) {
        r = await api.post('/api/evidence/text', {
          title: 'LinkedIn paste',
          text: fd.text,
          source_type: 'linkedin',
        });
      } else if (fd.url) {
        r = await api.post('/api/evidence/url', { url: fd.url, source_type: 'linkedin' });
      } else {
        toast('Provide LinkedIn text or URL.', 'error');
        return;
      }
      if (r.ok) {
        toast('LinkedIn ingested.', 'success');
        // Feed the LinkedIn content into profile inference.
        if (isSetupVisible()) {
          const inferFD = new FormData();
          if (fd.text && fd.text.trim()) inferFD.append('linkedin_text', fd.text);
          if (fd.url) inferFD.append('linkedin_url', fd.url);
          await runInferAndApply(inferFD, { silent: true });
        }
        e.target.reset();
      }
    });
  }

  function isSetupVisible() {
    const setup = document.querySelector('section.page[data-page="setup"]');
    return !!setup && !setup.classList.contains('hidden');
  }

  /** POST FormData to /api/profile/infer and merge the result into the
   *  profile form WITHOUT overwriting fields the user has already filled.
   *  Used by the dropzone + URL/LinkedIn/GitHub ingest paths so any
   *  ingestion event on the Setup page contributes to auto-fill. */
  async function runInferAndApply(fd, { silent = false } = {}) {
    const r = await api.post('/api/profile/infer', fd, { silent: true });
    if (!r.ok) return false;
    const inferred = r.inferred_fields || [];
    if (!inferred.length) return false;
    mergeInferredIntoProfileForm(r.data || {}, inferred);
    renderInferMeta(r.sources_used || [], r.inferred_meta || {}, r.notes || []);
    const status = $('#infer-status');
    if (status) status.textContent =
      `Auto-filled ${inferred.length} more field${inferred.length === 1 ? '' : 's'} from this ingestion. Review and SAVE PROFILE.`;
    if (!silent) {
      toast(`Profile auto-filled (${inferred.length} fields). Review and save.`, 'success');
    }
    return true;
  }

  /** Like applyInferredToProfileForm but ONLY writes fields the user has
   *  not already filled, so successive ingestions accumulate instead of
   *  clobbering each other's contributions. */
  function mergeInferredIntoProfileForm(data, inferredFields) {
    const f = $('#profile-form');
    if (!f) return;
    const inferredSet = new Set(inferredFields);

    const fieldHasValue = (inp) => {
      if (!inp) return false;
      if (inp.type === 'checkbox') return false; // checkboxes handled below
      return (inp.value || '').trim() !== '';
    };

    for (const [k, v] of Object.entries(data)) {
      const inp = f.elements.namedItem(k);
      if (!inp) continue;
      if (fieldHasValue(inp)) continue;
      if (Array.isArray(v)) {
        if (v.length) inp.value = listToCsv(v);
      } else if (v && typeof v === 'object') {
        continue;
      } else if (v != null && v !== '') {
        inp.value = v;
      }
      if (inferredSet.has(k)) {
        const wrap = inp.closest('label') || inp;
        wrap.classList.add('field-inferred');
      }
    }

    for (const grp of f.querySelectorAll('.check-grid[data-name]')) {
      const name = grp.dataset.name;
      const arr = Array.isArray(data[name]) ? data[name] : [];
      if (!arr.length) continue;
      let touched = false;
      for (const cb of grp.querySelectorAll('input[type="checkbox"]')) {
        if (arr.includes(cb.value) && !cb.checked) { cb.checked = true; touched = true; }
      }
      if (touched && inferredSet.has(name)) {
        const wrap = grp.closest('label') || grp;
        wrap.classList.add('field-inferred');
      }
    }
  }

  async function uploadFiles(files) {
    const list = $('#upload-list');
    for (const f of files) {
      const li = el('li', {}, [
        el('span', { text: f.name }),
        el('span', { class: 'muted', text: 'uploading…' }),
      ]);
      list.appendChild(li);
      const fd = new FormData();
      fd.append('file', f);
      const r = await api.post('/api/evidence/upload', fd, { silent: true });
      const status = li.lastChild;
      if (r.ok) status.textContent = 'OK';
      else status.textContent = 'failed (' + (r.error || 'err') + ')';

      // If the dropped file looks like a resume AND we're on the Setup
      // page, run it through the profile inference too so the form fills.
      if (r.ok && isSetupVisible() && looksLikeResume(f.name)) {
        const inferFD = new FormData();
        inferFD.append('resume_file', f);
        const filled = await runInferAndApply(inferFD, { silent: true });
        if (filled) {
          status.textContent = 'OK · profile filled';
        }
      }
    }
  }

  function looksLikeResume(name) {
    const n = (name || '').toLowerCase();
    return /\b(resume|cv|curriculum)\b/.test(n) ||
           /\.(pdf|docx|doc|md|txt)$/.test(n);
  }

  // ============================================================
  // VAULT
  // ============================================================
  async function loadVaultSummary() {
    const r = await api.get('/api/stats', { silent: true });
    if (!r.ok) return;
    const d = r.data || {};
    $$('#vault-kpis [data-k]').forEach(node => {
      const k = node.dataset.k;
      node.textContent = d[k] != null ? d[k] : '—';
    });
  }
  async function loadVault() {
    // sources — vault/summary plus evidence/sources for the full list
    const sR = await api.get('/api/evidence/sources', { silent: true });
    const sources = (sR.ok && (sR.data?.sources || sR.data || [])) || [];
    const sBody = $('#vault-sources tbody');
    sBody.innerHTML = '';
    if (!Array.isArray(sources) || !sources.length) {
      sBody.appendChild(el('tr', {}, el('td', { colspan: 6, class: 'empty', text: 'No sources yet.' })));
    } else {
      for (const s of sources) {
        sBody.appendChild(el('tr', {}, [
          el('td', { text: safeText(s.id || '') }),
          el('td', { text: safeText(s.source_type || s.type || '—') }),
          el('td', { text: safeText(s.title || s.url || '—') }),
          el('td', { text: fmtDate(s.created_at) }),
          el('td', { text: safeText(s.claim_count != null ? s.claim_count : '—') }),
          el('td', {}, [
            el('button', { class: 'btn btn-ghost small', onclick: () => deleteSource(s.id) }, 'DELETE'),
          ]),
        ]));
      }
    }
    // contradictions: only surface after a scan; no GET endpoint, so hide by default
    $('#vault-contradictions').classList.add('hidden');
    await loadVaultClaims();
  }
  async function deleteSource(id) {
    if (!id) return;
    if (!confirm('Delete source ' + id + ' and its claims?')) return;
    const r = await api.del('/api/evidence/sources/' + id);
    if (r.ok) { toast('Deleted.', 'success'); loadVault(); }
  }
  async function loadVaultClaims() {
    const params = new URLSearchParams();
    const t = $('#claim-type').value, v = $('#claim-verified').value, a = $('#claim-allowed').value;
    if (t) params.set('type', t);
    if (v !== '') params.set('verified', v);
    if (a !== '') params.set('allowed_for_resume', a);
    const r = await api.get('/api/vault/claims?' + params.toString(), { silent: true });
    const claims = (r.ok && (r.data?.claims || r.data)) || [];
    const body = $('#vault-claims tbody');
    body.innerHTML = '';
    if (!Array.isArray(claims) || !claims.length) {
      body.appendChild(el('tr', {}, el('td', { colspan: 7, class: 'empty', text: 'No claims.' })));
      return;
    }
    for (const c of claims) {
      const tr = el('tr', {}, [
        el('td', { text: safeText(c.id) }),
        el('td', { text: safeText(c.claim_type || '—') }),
        el('td', {}, [
          el('div', { text: safeText(c.claim_text || c.normalized_claim || '') }),
        ]),
        el('td', { text: safeText(c.confidence != null ? Number(c.confidence).toFixed(2) : '—') }),
        el('td', {}, el('input', {
          type: 'checkbox', checked: !!c.user_verified,
          onchange: (ev) => patchClaim(c.id, { user_verified: ev.target.checked }),
        })),
        el('td', {}, el('input', {
          type: 'checkbox', checked: !!c.allowed_for_resume,
          onchange: (ev) => patchClaim(c.id, { allowed_for_resume: ev.target.checked }),
        })),
        el('td', {}, [
          el('button', { class: 'btn btn-ghost small', onclick: () => editClaim(c) }, 'EDIT'),
        ]),
      ]);
      body.appendChild(tr);
    }
    applyClaimsFilter();
  }
  // Same pattern as the jobs quick filter: client-side narrow over the
  // rendered claim rows (the type/verified selects already filter
  // server-side; this handles free text).
  function applyClaimsFilter() {
    const input = $('#claims-quick-filter');
    const body = $('#vault-claims tbody');
    if (!input || !body) return;
    const needle = (input.value || '').trim().toLowerCase();
    for (const tr of $$('tr', body)) {
      if (tr.querySelector('td.empty')) continue;
      tr.classList.toggle('hidden', !!needle && !tr.textContent.toLowerCase().includes(needle));
    }
  }
  async function patchClaim(id, payload) {
    const r = await api.patch('/api/vault/claims/' + id, payload);
    if (r.ok) toast('Claim updated.', 'success');
  }
  function editClaim(claim) {
    const next = prompt('Edit claim text:', claim.claim_text || claim.normalized_claim || '');
    if (next == null) return;
    patchClaim(claim.id, { claim_text: next });
  }
  function bindVault() {
    $('#vault-refresh').addEventListener('click', loadVault);
    const cf = $('#claims-quick-filter');
    if (cf) cf.addEventListener('input', applyClaimsFilter);
    $('#claim-type').addEventListener('change', loadVaultClaims);
    $('#claim-verified').addEventListener('change', loadVaultClaims);
    $('#claim-allowed').addEventListener('change', loadVaultClaims);
    $('#claims-scan').addEventListener('click', async () => {
      const r = await api.post('/api/vault/contradictions/scan', {});
      if (r.ok) {
        const n = Array.isArray(r.data) ? r.data.length : (r.data?.count ?? 0);
        const banner = $('#vault-contradictions');
        if (n) {
          banner.textContent = `${n} contradiction(s) detected — review claims below.`;
          banner.classList.remove('hidden');
        } else {
          banner.textContent = 'No contradictions found.';
          banner.classList.remove('hidden');
        }
        toast('Contradiction scan done.', 'success');
        loadVault();
      }
    });
  }

  // ============================================================
  // DASHBOARD / SEARCH
  // ============================================================
  function bindSearch() {
    $('#search-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const body = serializeForm(e.target);
      if (!body.query) { toast('Query is required.', 'error'); return; }
      $('#search-status').textContent = 'searching…';
      const r = await api.post('/api/search', body);
      if (r.ok) {
        const d = r.data || {};
        $('#search-status').textContent =
          `${d.discovered ?? 0} found / ${d.inserted ?? 0} new / ${d.scored ?? 0} scored`;
        toast('Search complete.', 'success');
        await loadJobs();
      } else {
        $('#search-status').textContent = 'search failed';
      }
    });
    $('#reload-jobs').addEventListener('click', loadJobs);
    bindLLMRerankUI();
    $('#jd-close').addEventListener('click', () => $('#job-detail').classList.add('hidden'));
    $('#jd-tailor').addEventListener('click', () => state.selectedJob && tailorForJob(state.selectedJob.id));
    $('#jd-cover').addEventListener('click', () => state.selectedJob && coverForJob(state.selectedJob.id));
    $('#jd-packet').addEventListener('click', () => state.selectedJob && buildPacket(state.selectedJob.id));
    $('#jd-save').addEventListener('click', () => state.selectedJob && saveToPipeline(state.selectedJob));
    $('#jd-recruiter').addEventListener('click', () => state.selectedJob && recruiterMessageForJob(state.selectedJob.id));
    $('#jd-interview').addEventListener('click', () => state.selectedJob && interviewPrepForJob(state.selectedJob.id));
    $('#jd-rescore').addEventListener('click', () => state.selectedJob && rescoreJob(state.selectedJob.id));
    $('#jd-archive').addEventListener('click', () => state.selectedJob && archiveJob(state.selectedJob.id));
    $('#jd-fit-good').addEventListener('click', () => state.selectedJob && submitJobFeedback(state.selectedJob.id, 'good_fit'));
    $('#jd-fit-bad').addEventListener('click', () => state.selectedJob && submitJobFeedback(state.selectedJob.id, 'bad_fit'));
  }

  // Fit feedback feeds scorer.load_feedback_adjustments — nudges role-family
  // weighting after >=5 signals. Buttons disable after a vote.
  async function submitJobFeedback(jobId, verdict) {
    const status = $('#jd-fit-status');
    const r = await api.post('/api/effectiveness/job-feedback', { job_id: jobId, verdict });
    if (r.ok) {
      toast(verdict === 'good_fit' ? 'Marked good fit — the scorer will learn from it.'
                                   : 'Marked bad fit — similar roles will rank lower.', 'success');
      const g = $('#jd-fit-good'), b = $('#jd-fit-bad');
      if (g) g.disabled = true;
      if (b) b.disabled = true;
      if (status) status.textContent = verdict === 'good_fit' ? '✓ good fit' : '✓ bad fit';
    }
  }
  // LLM scores side-dict, keyed by job_id, loaded alongside /api/jobs.
  state.llmScores = {};
  // Persisted sort choice: deterministic (default) or llm.
  // The user explicitly opts into LLM ranking — the deterministic score
  // stays authoritative until they flip the toggle.
  function getSortMode() {
    try {
      const v = localStorage.getItem('jhh.dashboard.sortMode');
      return v === 'llm' ? 'llm' : 'deterministic';
    } catch (_) { return 'deterministic'; }
  }
  function setSortMode(mode) {
    try { localStorage.setItem('jhh.dashboard.sortMode', mode); } catch (_) {}
  }

  async function loadJobs() {
    // Fire both requests in parallel — the LLM-scores call is cheap and
    // independent. We merge them into a single render pass.
    const [jobsRes, scoresRes] = await Promise.all([
      api.get('/api/jobs?limit=200', { silent: true }),
      api.get('/api/scoring/llm-scores?limit=500', { silent: true }),
    ]);
    const jobs = (jobsRes.ok && (jobsRes.data || [])) || [];
    state.jobs = jobs;
    const llmScores = {};
    const llmList = (scoresRes.ok && (scoresRes.data || [])) || [];
    for (const s of llmList) llmScores[s.job_id] = s;
    state.llmScores = llmScores;

    renderJobsTable();
    updateSortToggleUI();
    loadReferralFlags();  // async; re-renders to add REFERRAL pills
  }

  // Client-side text filter over the rendered jobs table. Matches against
  // the full row text (title/company/location/source/badges) so the user
  // can narrow 200 results to "remote staff" without another server call.
  function getMinScore() {
    try { return parseInt(localStorage.getItem('jhh.dashboard.minScore') || '0', 10) || 0; }
    catch (_) { return 0; }
  }

  function applyJobsFilter() {
    const input = $('#jobs-quick-filter');
    const body = $('#results-table tbody');
    if (!body) return;
    const needle = ((input && input.value) || '').trim().toLowerCase();
    const minScore = getMinScore();
    const rows = $$('tr[data-job-id]', body);
    let visible = 0;
    for (const tr of rows) {
      const textHit = !needle || tr.textContent.toLowerCase().includes(needle);
      // Unscored rows (no data-score) stay visible — hiding them would
      // mask a scoring failure rather than filter noise.
      const sAttr = tr.getAttribute('data-score');
      const scoreHit = !minScore || sAttr === '' || sAttr === null
        || parseInt(sAttr, 10) >= minScore;
      const hit = textHit && scoreHit;
      tr.classList.toggle('hidden', !hit);
      if (hit) visible++;
    }
    const countEl = $('#results-count');
    if (countEl && rows.length) {
      const llmCount = Object.keys(state.llmScores || {}).length;
      countEl.textContent = needle
        ? `${visible} / ${rows.length} shown · ${llmCount} LLM-scored`
        : `${rows.length} stored · ${llmCount} LLM-scored`;
    }
  }

  function bindJobsFilter() {
    const input = $('#jobs-quick-filter');
    if (input) {
      input.addEventListener('input', applyJobsFilter);
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') { input.value = ''; applyJobsFilter(); input.blur(); e.stopPropagation(); }
      });
    }
    const sel = $('#jobs-min-score');
    if (sel) {
      sel.value = String(getMinScore());
      sel.addEventListener('change', () => {
        try { localStorage.setItem('jhh.dashboard.minScore', sel.value); } catch (_) {}
        applyJobsFilter();
      });
    }
  }

  function renderJobsTable() {
    const body = $('#results-table tbody');
    if (!body) return;
    body.innerHTML = '';
    const jobs = state.jobs || [];
    const llmScores = state.llmScores || {};
    const llmCount = Object.keys(llmScores).length;
    const countEl = $('#results-count');
    if (countEl) {
      countEl.textContent =
        `${jobs.length} stored · ${llmCount} LLM-scored`;
    }
    if (!jobs.length) {
      body.appendChild(el('tr', {}, el('td', { colspan: 13, class: 'empty', text: 'No jobs yet — run a search.' })));
      return;
    }
    const mode = getSortMode();
    const presorted = jobs.slice().sort((a, b) => {
      if (mode === 'llm') {
        // Use semantic_score (0-1) scaled to 0-100 for comparison. Jobs
        // without an LLM score sort to the bottom — that's the honest
        // signal: the model hasn't weighed in yet.
        const sa = llmScores[a.id]?.semantic_score;
        const sb = llmScores[b.id]?.semantic_score;
        const na = sa == null ? -1 : sa * 100;
        const nb = sb == null ? -1 : sb * 100;
        if (nb !== na) return nb - na;
      }
      return (b.score ?? -1) - (a.score ?? -1);
    });
    // Collapse per-location duplicates: boards post the same role once per
    // city. Keep the best-ranked row and badge it with the sibling count;
    // the duplicates stay in the DB (each is a distinct posting) but stop
    // flooding the table.
    const groups = new Map();
    const sorted = [];
    for (const j of presorted) {
      const key = `${(j.title || '').trim().toLowerCase()}|${(j.company || '').trim().toLowerCase()}`;
      if (groups.has(key)) {
        groups.get(key).push(j);
      } else {
        groups.set(key, []);
        sorted.push(j);
      }
    }
    sorted.forEach((j, i) => {
      const key = `${(j.title || '').trim().toLowerCase()}|${(j.company || '').trim().toLowerCase()}`;
      j._siblings = groups.get(key) || [];
      const score = j.score ?? null;
      const llmRow = llmScores[j.id];
      const tr = el('tr', { class: 'clickable', 'data-job-id': j.id,
        'data-score': score == null ? '' : String(Math.round(score)), onclick: (e) => {
        // Don't open the detail panel when the user clicks a row button or checkbox.
        if (e.target.closest('.row-action-btn')) return;
        if (e.target.matches('input[type="checkbox"]')) return;
        openJobDetail(j);
      } }, [
        el('td', {}, el('input', { type: 'checkbox', class: 'job-row-check',
          'data-job-id': j.id,
          onchange: refreshJobsSelectionUI })),
        el('td', { text: String(i + 1) }),
        el('td', {}, [
          document.createTextNode(safeText(j.title || '—')),
          ...(j._siblings && j._siblings.length ? [el('span', {
            class: 'dup-badge',
            title: 'Same role posted for: ' +
              j._siblings.map(s => s.location || 'unknown').join(', '),
            text: `+${j._siblings.length}`,
          })] : []),
        ]),
        el('td', { text: safeText(j.company || '—') }),
        el('td', { text: safeText(j.location || (j.is_remote ? 'Remote' : '—')) }),
        el('td', {}, score == null ? document.createTextNode('—')
          : el('span', { class: 'score-chip ' + scoreClass(score), text: String(Math.round(score)) })),
        el('td', {}, renderLLMScoreCell(j.id, llmRow)),
        el('td', { text: fmtSalary(j.salary_min, j.salary_max, j.salary_currency || 'USD') }),
        el('td', { text: fmtRel(j.posted_at || j.created_at) }),
        el('td', { text: safeText(j.source || '—') }),
        el('td', {}, renderBadges(j)),
        el('td', {}, renderRowActions(j, llmRow)),
        el('td', {}, j.url ? el('a', { href: j.url, target: '_blank', rel: 'noopener', text: 'open' }) : document.createTextNode('—')),
      ]);
      body.appendChild(tr);
    });
    applyJobsFilter();
  }

  function renderLLMScoreCell(jobId, llmRow) {
    if (!llmRow || llmRow.semantic_score == null) {
      return el('span', { class: 'llm-score-chip llm-none', text: '—',
        title: 'No LLM semantic score yet. Click 🔍 to learn more or RESCORE to compute.' });
    }
    const n = Math.round(Number(llmRow.semantic_score) * 100);
    const cls = n >= 75 ? 'llm-high' : n >= 45 ? 'llm-mid' : 'llm-low';
    const action = (llmRow.recommended_action || '').toUpperCase();
    return el('span', {
      class: 'llm-score-chip ' + cls,
      text: String(n),
      title: action ? `LLM ${n}/100 · ${action}` : `LLM ${n}/100`,
    });
  }

  function renderRowActions(j, llmRow) {
    const wrap = el('span', { class: 'row-actions' });
    // 🔍 explain — opens the LLM rerank modal for this job
    const explainBtn = el('button', {
      type: 'button',
      class: 'row-action-btn row-action-explain',
      title: 'View LLM fit summary, strengths, gaps, red flags',
      'aria-label': 'Explain LLM score',
      text: '🔍',
      onclick: (e) => {
        e.stopPropagation();
        openLLMRerankModal(j.id);
      },
    });
    wrap.appendChild(explainBtn);
    // RESCORE — POST /api/scoring/llm-rerank/{id}
    const rescoreBtn = el('button', {
      type: 'button',
      class: 'row-action-btn',
      title: 'Re-run LLM second-pass scoring for this job (10–60s).',
      text: 'RESCORE',
      onclick: async (e) => {
        e.stopPropagation();
        await rescoreOneWithLLM(j.id, rescoreBtn);
      },
    });
    wrap.appendChild(rescoreBtn);
    // SAVE — move into pipeline as a saved application
    const saveBtn = el('button', {
      type: 'button', class: 'row-action-btn', title: 'Save to pipeline',
      text: 'SAVE',
      onclick: async (e) => {
        e.stopPropagation();
        await saveJobToPipeline(j, saveBtn);
      },
    });
    wrap.appendChild(saveBtn);
    // DISMISS — mark dismissed so REFRESH excludes it
    const dismissBtn = el('button', {
      type: 'button', class: 'row-action-btn row-action-dismiss',
      title: 'Dismiss this job — it will be excluded from future REFRESH calls',
      text: 'DISMISS',
      onclick: async (e) => {
        e.stopPropagation();
        await dismissJobInline(j, dismissBtn);
      },
    });
    wrap.appendChild(dismissBtn);
    return wrap;
  }

  // ----- Dashboard selection + bulk + per-row actions -----
  function selectedJobIds() {
    return $$('.job-row-check').filter(c => c.checked).map(c => Number(c.getAttribute('data-job-id')));
  }
  function refreshJobsSelectionUI() {
    const n = selectedJobIds().length;
    const dismissBtn = $('#jobs-dismiss-selected');
    const saveBtn = $('#jobs-save-selected');
    if (dismissBtn) dismissBtn.classList.toggle('hidden', n === 0);
    if (saveBtn) saveBtn.classList.toggle('hidden', n === 0);
    if (dismissBtn) dismissBtn.textContent = `DISMISS SELECTED (${n})`;
    if (saveBtn) saveBtn.textContent = `SAVE SELECTED (${n})`;
  }

  async function dismissJobInline(j, btn) {
    btn.disabled = true; btn.textContent = '…';
    const r = await api.patch('/api/jobs/' + j.id + '/status', { status: 'dismissed' });
    btn.disabled = false; btn.textContent = 'DISMISS';
    if (r.ok) {
      toast(`Dismissed "${j.title || j.id}". REFRESH will exclude it.`, 'success');
      await loadJobs();
    }
  }

  async function saveJobToPipeline(j, btn) {
    btn.disabled = true; btn.textContent = '…';
    const r = await api.post('/api/applications', { job_id: j.id, status: 'saved' });
    btn.disabled = false; btn.textContent = 'SAVE';
    if (r.ok) toast(`Saved "${j.title || j.id}" to Pipeline.`, 'success');
  }

  async function bulkDismissSelected() {
    const ids = selectedJobIds();
    if (!ids.length) return;
    if (!confirm(`Dismiss ${ids.length} job(s)? They will be excluded from future REFRESH calls.`)) return;
    const r = await api.post('/api/jobs/bulk-status', { job_ids: ids, status: 'dismissed' });
    if (r.ok) {
      toast(`Dismissed ${r.data.updated.length}.`, 'success');
      await loadJobs();
      refreshJobsSelectionUI();
    }
  }
  async function bulkSaveSelected() {
    const ids = selectedJobIds();
    if (!ids.length) return;
    let saved = 0;
    for (const id of ids) {
      const j = (state.jobs || []).find(x => x.id === id);
      if (!j) continue;
      const r = await api.post('/api/applications', { job_id: id, status: 'saved' }, { silent: true });
      if (r.ok) saved++;
    }
    toast(`Saved ${saved} of ${ids.length} to Pipeline.`, 'success');
    refreshJobsSelectionUI();
  }

  async function refreshJobsFromSources() {
    const btn = $('#jobs-refresh-btn');
    const status = $('#jobs-refresh-status');
    btn.disabled = true; btn.textContent = 'REFRESHING…';
    if (status) status.textContent = 'searching boards (1–3 min)…';
    const r = await api.post('/api/jobs/refresh', {});
    btn.disabled = false; btn.textContent = 'REFRESH JOBS';
    if (r.ok) {
      const d = r.data || {};
      const msg = `+${d.inserted || 0} new · ${d.excluded_dismissed || 0} excluded (dismissed) · ${d.discovered || 0} discovered`;
      if (status) status.textContent = msg;
      toast(msg, 'success');
      await loadJobs();
    } else {
      if (status) status.textContent = 'failed: ' + (r.error || 'unknown');
    }
  }

  function bindDashboardSelection() {
    const all = $('#jobs-select-all');
    if (all) all.addEventListener('change', () => {
      const v = all.checked;
      $$('.job-row-check').forEach(c => { c.checked = v; });
      refreshJobsSelectionUI();
    });
    const dismissBtn = $('#jobs-dismiss-selected');
    if (dismissBtn) dismissBtn.addEventListener('click', bulkDismissSelected);
    const saveBtn = $('#jobs-save-selected');
    if (saveBtn) saveBtn.addEventListener('click', bulkSaveSelected);
    const refreshBtn = $('#jobs-refresh-btn');
    if (refreshBtn) refreshBtn.addEventListener('click', refreshJobsFromSources);
  }

  function updateSortToggleUI() {
    const mode = getSortMode();
    $$('.sort-toggle-btn').forEach((b) => {
      const m = b.getAttribute('data-sort-mode');
      const active = m === mode;
      b.classList.toggle('is-active', active);
      b.classList.toggle('btn-secondary', active);
      b.classList.toggle('btn-ghost', !active);
    });
  }

  function bindLLMRerankUI() {
    $$('.sort-toggle-btn').forEach((b) => {
      b.addEventListener('click', () => {
        const mode = b.getAttribute('data-sort-mode');
        setSortMode(mode);
        updateSortToggleUI();
        renderJobsTable();
      });
    });
    const topBtn = $('#llm-rerank-top-btn');
    if (topBtn) topBtn.addEventListener('click', rerankTop30WithLLM);
    const closeBtn = $('#llm-rerank-modal-close');
    if (closeBtn) closeBtn.addEventListener('click', () => {
      const m = $('#llm-rerank-modal');
      if (m) m.classList.add('hidden');
    });
    const viewRunBtn = $('#llm-rerank-view-run');
    if (viewRunBtn) viewRunBtn.addEventListener('click', () => {
      const runId = viewRunBtn.getAttribute('data-run-id');
      if (!runId) { toast('No LLM run id on this score yet.', 'error'); return; }
      // Close the rerank modal first so the run modal sits on top cleanly.
      const m = $('#llm-rerank-modal');
      if (m) m.classList.add('hidden');
      openLLMRunModal(Number(runId));
    });
  }

  async function rescoreOneWithLLM(jobId, btn) {
    if (!btn) return;
    const original = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="row-action-spinner" aria-hidden="true"></span>';
    toast('LLM rescoring job ' + jobId + '… (10–60s)');
    try {
      const r = await api.post('/api/scoring/llm-rerank/' + jobId, {}, { silent: true });
      if (r.ok && r.data) {
        const persisted = r.data.data || r.data;
        state.llmScores = state.llmScores || {};
        // Merge fresh score into the side-dict so the chip updates without
        // a full reload. Server returns the persisted row shape.
        if (persisted && persisted.job_id) {
          state.llmScores[persisted.job_id] = {
            ...state.llmScores[persisted.job_id],
            ...persisted,
            strengths: persisted.strengths || [],
            gaps: persisted.gaps || [],
            red_flags: persisted.red_flags || [],
          };
        }
        toast('LLM rescore complete.', 'success');
        renderJobsTable();
      } else {
        const err = r.error || (r.data && (r.data.error || (r.data.data && r.data.data.error))) || 'unknown error';
        toast('LLM rescore failed: ' + err, 'error');
      }
    } catch (e) {
      toast('LLM rescore network error: ' + e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  let _rerankBatchInProgress = false;
  async function rerankTop30WithLLM() {
    if (_rerankBatchInProgress) {
      toast('A batch rerank is already running.', 'error');
      return;
    }
    if (!confirm('Run LLM second-pass scoring on the top 30 deterministic-scored jobs?\n\nThis can take 1–10 minutes (uses your local LLM).')) return;
    _rerankBatchInProgress = true;
    const btn = $('#llm-rerank-top-btn');
    const status = $('#llm-rerank-status');
    const original = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="row-action-spinner" aria-hidden="true"></span> RUNNING…';
    if (status) status.textContent = 'reranking top 30… check the LLM Activity panel for live progress.';
    // Long poll: refresh the score side-dict every 12s so the table updates
    // as jobs finish. The POST blocks until the batch completes — we poll
    // alongside it for live UX.
    const pollInterval = setInterval(async () => {
      try {
        const r = await api.get('/api/scoring/llm-scores?limit=500', { silent: true });
        if (r.ok) {
          const llmScores = {};
          for (const s of (r.data || [])) llmScores[s.job_id] = s;
          state.llmScores = llmScores;
          renderJobsTable();
        }
      } catch (_) {}
    }, 12000);
    try {
      const r = await api.post('/api/scoring/llm-rerank', { top_n: 30 }, { silent: true });
      clearInterval(pollInterval);
      // Always refresh once at the end.
      const sres = await api.get('/api/scoring/llm-scores?limit=500', { silent: true });
      if (sres.ok) {
        const llmScores = {};
        for (const s of (sres.data || [])) llmScores[s.job_id] = s;
        state.llmScores = llmScores;
        renderJobsTable();
      }
      if (r.ok && r.data) {
        const d = r.data.data || r.data;
        toast(`Reranked ${d.reranked || 0} jobs · ${d.errors || 0} errors · ${Math.round((d.elapsed_ms || 0) / 1000)}s.`, 'success');
        if (status) status.textContent = `Last batch: ${d.reranked || 0} reranked, ${d.errors || 0} errors.`;
      } else {
        const dd = (r.data && (r.data.data || r.data)) || {};
        if (dd.skipped_no_provider) {
          toast('No real LLM provider configured. Open Settings → LLM to pin one.', 'error');
        } else {
          toast('Batch rerank failed: ' + (r.error || 'unknown'), 'error');
        }
      }
    } catch (e) {
      clearInterval(pollInterval);
      toast('Batch rerank network error: ' + e.message, 'error');
    } finally {
      _rerankBatchInProgress = false;
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  async function openLLMRerankModal(jobId) {
    const modal = $('#llm-rerank-modal');
    const body = $('#llm-rerank-modal-body');
    const viewRunBtn = $('#llm-rerank-view-run');
    if (!modal || !body) return;
    const titleEl = $('#llm-rerank-modal-title');
    body.innerHTML = '<p class="muted small">Loading LLM score for job ' + jobId + '…</p>';
    if (viewRunBtn) {
      viewRunBtn.removeAttribute('data-run-id');
      viewRunBtn.disabled = true;
    }
    modal.classList.remove('hidden');

    let row = (state.llmScores || {})[jobId] || null;
    if (!row) {
      // No cached LLM score — try the live endpoint with a tight cap so
      // we only walk the table once. If still empty, prompt the user.
      const r = await api.get('/api/scoring/llm-scores?limit=500', { silent: true });
      if (r.ok) {
        for (const s of (r.data || [])) {
          if (s.job_id === jobId) { row = s; break; }
        }
      }
    }
    const job = (state.jobs || []).find((j) => j.id === jobId) || {};
    if (titleEl) {
      titleEl.textContent = `LLM SCORE · ${job.title || 'Job ' + jobId} · ${job.company || ''}`.trim();
    }

    if (!row) {
      body.innerHTML = `
        <p class="muted small">No LLM semantic score for this job yet.</p>
        <p>Click <strong>RESCORE</strong> on the row to run the LLM second-pass scoring now (10–60s with a local 70B model).</p>
      `;
      return;
    }
    const semantic = row.semantic_score == null ? '—' : Math.round(row.semantic_score * 100);
    const det = row.deterministic_score == null ? '—' : Math.round(row.deterministic_score * 100);
    const action = (row.recommended_action || '').toUpperCase();
    const esc = (s) => (s == null ? '' : String(s)).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const list = (arr, emptyLabel) => {
      if (!arr || !arr.length) {
        return `<ul class="llm-modal-list is-empty"><li>${emptyLabel}</li></ul>`;
      }
      return '<ul class="llm-modal-list">' +
        arr.map((s) => `<li>${esc(s)}</li>`).join('') + '</ul>';
    };
    const actionClass = action === 'APPLY' ? 'is-good'
      : (action === 'SKIP' ? 'is-bad' : '');
    body.innerHTML = `
      <div class="llm-modal-kpis">
        <span class="ap-kpi"><strong>LLM ${semantic}/100</strong></span>
        <span class="ap-kpi"><strong>DETERMINISTIC ${det}/100</strong></span>
        <span class="llm-rerank-pill ${actionClass}"><strong>${action || '—'}</strong></span>
      </div>
      <div class="llm-modal-summary">${esc(row.fit_summary || '(no fit summary)')}</div>
      <div class="llm-modal-section">
        <h4>Strengths (evidence-backed)</h4>
        ${list(row.strengths, 'No strengths reported.')}
      </div>
      <div class="llm-modal-section">
        <h4>Gaps</h4>
        ${list(row.gaps, 'No gaps reported.')}
      </div>
      <div class="llm-modal-section">
        <h4>Red flags</h4>
        ${list(row.red_flags, 'None.')}
      </div>
      <p class="muted small" style="margin-top:var(--s-3);">Scored against your verified Career Evidence Vault. No skills/certs are assumed.</p>
    `;
    if (viewRunBtn) {
      if (row.llm_run_id) {
        viewRunBtn.setAttribute('data-run-id', String(row.llm_run_id));
        viewRunBtn.disabled = false;
      } else {
        viewRunBtn.disabled = true;
      }
    }
  }

  function renderBadges(j) {
    const wrap = el('span', {}, []);
    const score = j.score;
    if (score != null) {
      wrap.appendChild(el('span', { class: 'badge ' + (score >= 85 ? 'badge-green' : score >= 70 ? 'badge-blue' : 'badge-warn'), text: scoreLabel(score) }));
    }
    if (j.is_remote) wrap.appendChild(el('span', { class: 'badge badge-blue', text: 'REMOTE' }));
    if (j.posting_changed) wrap.appendChild(el('span', { class: 'badge badge-warn',
      title: 'This posting changed since you saved/applied', text: 'CHANGED' }));
    if (state.referralFlags && state.referralFlags[j.id]) wrap.appendChild(el('span', {
      class: 'badge badge-green', title: 'You have a connection at this company', text: 'REFERRAL' }));
    if (j.source && ['linkedin','indeed','glassdoor'].includes(j.source)) {
      wrap.appendChild(el('span', { class: 'badge badge-warn', text: 'GRAY' }));
    } else if (j.source && ['greenhouse','lever','ashby','remotive','wwr','rss','remoteintech'].includes(j.source)) {
      wrap.appendChild(el('span', { class: 'badge badge-green', text: 'LEGAL' }));
    }
    return wrap;
  }

  // Batch-fetch referral availability for the visible jobs, then re-badge.
  async function loadReferralFlags() {
    const jobs = state.jobs || [];
    const ids = jobs.map(j => j.id).filter(Boolean);
    if (!ids.length) return;
    const r = await api.get('/api/referrals/job-flags?job_ids=' + ids.join(','), { silent: true });
    if (!r.ok || !r.data) return;
    const flags = {};
    let any = false;
    for (const [k, v] of Object.entries(r.data)) { flags[k] = !!v; if (v) any = true; }
    state.referralFlags = flags;
    if (any) renderJobsTable();  // re-render so REFERRAL pills appear
  }
  async function openJobDetail(job) {
    state.selectedJob = job;
    const det = $('#job-detail');
    det.classList.remove('hidden');
    // Reset fit-feedback controls for the newly-opened job.
    const fg = $('#jd-fit-good'), fb = $('#jd-fit-bad'), fs = $('#jd-fit-status');
    if (fg) fg.disabled = false;
    if (fb) fb.disabled = false;
    if (fs) fs.textContent = '';
    $('#jd-title').textContent = `${job.title || '—'} · ${job.company || '—'}`;
    const meta = $('#jd-meta'); meta.innerHTML = '';
    [['LOC', job.location || (job.is_remote ? 'remote' : '—')],
     ['SAL', fmtSalary(job.salary_min, job.salary_max, job.salary_currency || 'USD')],
     ['SRC', job.source || '—'],
     ['POSTED', fmtRel(job.posted_at || job.created_at)],
     ['SCORE', job.score != null ? Math.round(job.score) : '—']]
      .forEach(([k, v]) => meta.appendChild(el('span', { text: `${k}: ${v}` })));

    $('#jd-description').textContent = job.description || job.summary || '(no description text)';

    // keyword matrix
    const kwBody = $('#jd-keywords tbody'); kwBody.innerHTML = '';
    const kws = job.keywords || job.keyword_matrix || [];
    if (!kws.length) {
      kwBody.appendChild(el('tr', {}, el('td', { colspan: 5, class: 'empty', text: 'No keyword analysis yet — try TAILOR RESUME.' })));
    } else {
      for (const k of kws) {
        kwBody.appendChild(el('tr', {}, [
          el('td', { text: safeText(k.keyword || k.text || '') }),
          el('td', { text: safeText(k.category || '—') }),
          el('td', { text: safeText(k.importance || '—') }),
          el('td', { text: safeText(k.support || k.status || '—') }),
          el('td', { text: k.resume_safe ? 'YES' : 'no' }),
        ]));
      }
    }
    // fit + gaps
    const fit = $('#jd-fit'); fit.innerHTML = '';
    (job.fit || []).forEach(f => fit.appendChild(el('li', { text: safeText(f) })));
    if (!fit.children.length) fit.appendChild(el('li', { class: 'muted', text: 'Run TAILOR RESUME to compute fit lines.' }));
    const gaps = $('#jd-gaps'); gaps.innerHTML = '';
    (job.gaps || []).forEach(g => gaps.appendChild(el('li', { text: safeText(g) })));
    if (!gaps.children.length) gaps.appendChild(el('li', { class: 'muted', text: '—' }));
  }
  async function tailorForJob(jobId) {
    toast('Tailoring resume…');
    const r = await api.post('/api/resume/tailor', { job_id: jobId, resume_type: 'job_specific' });
    if (r.ok) { toast('Tailored resume created.', 'success'); switchPage('resume'); }
  }
  async function coverForJob(jobId) {
    toast('Drafting cover letter…');
    const r = await api.post('/api/cover-letter', { job_id: jobId, tone: 'professional' });
    if (r.ok) toast('Cover letter drafted.', 'success');
  }
  async function buildPacket(jobId) {
    const r = await api.post('/api/packet/build', { job_id: jobId });
    if (r.ok) toast('Packet built: ' + (r.data?.packet_dir || r.data?.path || 'ok'), 'success');
  }
  async function saveToPipeline(job) {
    const r = await api.post('/api/applications', { job_id: job.id, status: 'saved' });
    if (r.ok) { toast('Saved to pipeline.', 'success'); }
  }
  async function recruiterMessageForJob(jobId) {
    toast('Drafting recruiter message…');
    const r = await api.post('/api/recruiter/message', { job_id: jobId, channel: 'email' });
    if (!r.ok) return;
    const text = r.data?.text || r.data?.message || '';
    showTextModal('Recruiter message draft', text,
      'Drafts are never sent automatically. Copy into LinkedIn / email.');
  }
  async function interviewPrepForJob(jobId) {
    toast('Generating interview prep…');
    const r = await api.post('/api/interview/prep', { job_id: jobId });
    if (!r.ok) return;
    const d = r.data || {};
    const lines = [];
    if (d.talking_points && d.talking_points.length) {
      lines.push('TALKING POINTS');
      d.talking_points.forEach(p => lines.push('  · ' + (typeof p === 'string' ? p : p.text || JSON.stringify(p))));
      lines.push('');
    }
    if (d.likely_questions && d.likely_questions.length) {
      lines.push('LIKELY QUESTIONS');
      d.likely_questions.forEach(q => lines.push('  · ' + (typeof q === 'string' ? q : q.text || JSON.stringify(q))));
    }
    showTextModal('Interview prep', lines.join('\n') || JSON.stringify(d, null, 2),
      'Generated from your evidence. Edit before the interview.');
  }
  async function rescoreJob(jobId) {
    toast('Rescoring…');
    const r = await api.post('/api/jobs/rescore', { job_ids: [jobId] });
    if (!r.ok) return;
    const scored = (r.data?.scored || []).length;
    toast(`Rescored ${scored} job${scored === 1 ? '' : 's'}.`, 'success');
    // Refresh detail panel
    const fresh = await api.get('/api/jobs/' + jobId, { silent: true });
    if (fresh.ok && fresh.data) openJobDetail(fresh.data);
  }
  async function archiveJob(jobId) {
    if (!confirm('Archive this job? It will be hidden from search results.')) return;
    const r = await api.patch('/api/jobs/' + jobId + '/status', { status: 'archived' });
    if (r.ok) {
      toast('Job archived.', 'success');
      $('#job-detail').classList.add('hidden');
      loadJobs();
    }
  }
  function showTextModal(title, text, hint) {
    const m = $('#card-modal');
    if (!m) { alert(text); return; }
    $('#card-modal-title').textContent = title;
    const body = $('#card-modal-body');
    body.innerHTML = '';
    if (hint) body.appendChild(el('p', { class: 'muted small', text: hint }));
    const ta = el('textarea', { id: 'modal-text', rows: 14 });
    ta.value = text || '';
    body.appendChild(ta);
    m.classList.remove('hidden');
    $('#card-save-btn').textContent = 'COPY';
    $('#card-save-btn').onclick = async () => {
      try { await navigator.clipboard.writeText($('#modal-text').value); toast('Copied.', 'success'); }
      catch { toast('Copy failed — select text manually.', 'error'); }
    };
    $('#card-close-btn').onclick = () => {
      $('#card-save-btn').textContent = 'SAVE';
      m.classList.add('hidden');
    };
  }

  // ============================================================
  // RESUME LAB
  // ============================================================
  async function loadResumes() {
    const r = await api.get('/api/resumes', { silent: true });
    const list = (r.ok && (r.data || [])) || [];
    state.resumes = list;
    const body = $('#resume-list tbody');
    body.innerHTML = '';
    if (!list.length) {
      body.appendChild(el('tr', {}, el('td', { colspan: 5, class: 'empty', text: 'No resumes yet.' })));
    } else {
      for (const res of list) {
        const downloads = ['md', 'txt', 'docx', 'pdf'].map(fmt =>
          el('a', {
            href: '/api/resume/' + res.id + '/download/' + fmt,
            target: '_blank',
            class: 'btn btn-ghost small',
            text: fmt.toUpperCase(),
            onclick: (e) => e.stopPropagation(),
          })
        );
        body.appendChild(el('tr', { class: 'clickable', onclick: () => openResume(res.id) }, [
          el('td', { text: String(res.id) }),
          el('td', { text: safeText(res.resume_type || 'master') }),
          el('td', { text: safeText(res.job_id || '—') }),
          el('td', { text: fmtDate(res.updated_at || res.created_at) }),
          el('td', {}, downloads),
        ]));
      }
    }
    // Cover letters list (parallel surface in Resume Lab)
    loadCoverLetters();
  }
  async function loadCoverLetters() {
    const host = $('#cover-letters-table tbody');
    if (!host) return;
    const r = await api.get('/api/cover-letters', { silent: true });
    const list = (r.ok && (r.data || [])) || [];
    host.innerHTML = '';
    if (!list.length) {
      host.appendChild(el('tr', {}, el('td', { colspan: 4, class: 'empty', text: 'No cover letters yet.' })));
      return;
    }
    for (const cl of list) {
      host.appendChild(el('tr', {}, [
        el('td', { text: String(cl.id) }),
        el('td', { text: safeText(cl.job_id || '—') }),
        el('td', { text: fmtDate(cl.created_at) }),
        el('td', {}, el('a', {
          href: '/api/cover-letter/' + cl.id + '/download',
          target: '_blank',
          class: 'btn btn-ghost small',
          text: 'DOWNLOAD .txt',
        })),
      ]));
    }
  }
  async function openResume(id) {
    const r = await api.get('/api/resumes/' + id, { silent: true });
    if (!r.ok) return;
    const res = r.data || {};
    state.selectedResume = res;
    $('#resume-title').textContent = `Resume #${id} · ${res.resume_type || ''}`;
    $('#resume-md').textContent = res.markdown || res.content_md || res.text || '(no content)';
    // honesty report
    $('#honesty-report').textContent = res.honesty_report
      ? (typeof res.honesty_report === 'string' ? res.honesty_report : JSON.stringify(res.honesty_report, null, 2))
      : 'No honesty report attached.';
    // provenance
    const plist = $('#provenance-list'); plist.innerHTML = '';
    const prov = res.provenance || {};
    Object.entries(prov).forEach(([segment, sources]) => {
      plist.appendChild(el('li', {}, [
        el('strong', { text: segment + ': ' }),
        document.createTextNode(Array.isArray(sources) ? sources.join(', ') : String(sources)),
      ]));
    });
    if (!plist.children.length) plist.appendChild(el('li', { class: 'muted', text: 'No provenance map attached.' }));
    // coverage chart
    renderCoverage(res.coverage || res.keyword_coverage || null);
  }
  function renderCoverage(cov) {
    const host = $('#coverage-chart');
    host.innerHTML = '';
    if (!cov) {
      host.appendChild(el('p', { class: 'muted small', text: 'Tailor a resume to populate coverage.' }));
      return;
    }
    const buckets = [
      ['Supported',    cov.supported    ?? cov.strong  ?? 0, ''],
      ['Transferable', cov.transferable ?? 0,                 't-transferable'],
      ['Weak',         cov.weak         ?? 0,                 't-weak'],
      ['Unsupported',  cov.unsupported  ?? cov.missing ?? 0,  't-missing'],
    ];
    const total = buckets.reduce((s, b) => s + (b[1] || 0), 0) || 1;
    for (const [label, n, cls] of buckets) {
      const pct = Math.round((n / total) * 100);
      host.appendChild(el('div', { class: 'coverage-bar ' + cls }, [
        el('span', { text: label }),
        el('span', { class: 'bar-bg' }, el('span', { class: 'bar-fg', style: `width:${pct}%` })),
        el('span', { text: String(n) }),
      ]));
    }
  }
  function bindResume() {
    $('#resume-new-master').addEventListener('click', async () => {
      // Master resume creation = upload a base file. Trigger file picker.
      const f = document.createElement('input');
      f.type = 'file';
      f.accept = '.pdf,.docx,.md,.txt';
      f.onchange = async () => {
        if (!f.files.length) return;
        const fd = new FormData();
        fd.append('file', f.files[0]);
        fd.append('resume_type', 'master');
        const r = await api.post('/api/resume/upload', fd);
        if (r.ok) { toast('Master resume uploaded.', 'success'); loadResumes(); }
      };
      f.click();
    });
    $$('.resume-panel [data-export]').forEach(btn => {
      btn.addEventListener('click', () => {
        const res = state.selectedResume;
        if (!res) { toast('Select a resume first.', 'error'); return; }
        const fmt = btn.dataset.export;
        window.open(`/api/resume/${res.id}/download/${fmt}`, '_blank');
      });
    });
    $('#resume-print').addEventListener('click', () => window.print());
  }

  // ============================================================
  // PIPELINE
  // ============================================================
  const PIPELINE_STATUSES = ['saved','prepared','applied','replied','interview','offer','rejected'];

  // Track whether the most recent drop landed inside a column. The window-level
  // drop handler reads this to decide whether to treat a drop as a delete.
  const _kanbanDragState = { lastDropOnColumn: false };

  function bindPipelineBoard() {
    const kb = $('#kanban');
    kb.innerHTML = '';
    for (const status of PIPELINE_STATUSES) {
      const col = el('div', { class: 'kanban-col', 'data-status': status }, [
        el('h4', {}, [
          el('span', { text: status.toUpperCase() }),
          el('span', { class: 'count', text: '0' }),
        ]),
        el('div', { class: 'col-body' }),
      ]);
      col.addEventListener('dragover', (e) => { e.preventDefault(); col.classList.add('drag-over'); });
      col.addEventListener('dragleave', () => col.classList.remove('drag-over'));
      col.addEventListener('drop', async (e) => {
        e.preventDefault();
        e.stopPropagation();
        col.classList.remove('drag-over');
        _kanbanDragState.lastDropOnColumn = true;
        const id = e.dataTransfer.getData('text/plain');
        if (!id) return;
        const r = await api.patch('/api/applications/' + id, { status });
        if (r.ok) { toast('Status: ' + status, 'success'); loadPipeline(); }
      });
      kb.appendChild(col);
    }

    // Window-level drag handlers — fired when the user drops a kanban card
    // OUTSIDE any column. We confirm + delete instead of silently snapping
    // the card back. Bound once; re-binding bindPipelineBoard is a no-op.
    if (!window._jhhKanbanDeleteBound) {
      window._jhhKanbanDeleteBound = true;
      // dragover anywhere must call preventDefault for drop to fire at all
      window.addEventListener('dragover', (e) => {
        if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('text/plain')) {
          e.preventDefault();
        }
      });
      window.addEventListener('dragstart', () => { _kanbanDragState.lastDropOnColumn = false; });
      window.addEventListener('drop', async (e) => {
        // Only handle drops outside a column. The column handler set the flag
        // (and stopped propagation) for valid drops.
        if (_kanbanDragState.lastDropOnColumn) return;
        const id = e.dataTransfer && e.dataTransfer.getData('text/plain');
        if (!id) return;
        const app = (state.applications || []).find(a => String(a.id) === String(id));
        if (!app) return;
        e.preventDefault();
        confirmDeleteApplication(app);
      });
    }
  }

  // Reusable yes/no delete confirm modal — works for kanban drag-out AND
  // any other place that wants a destructive confirmation.
  function confirmDeleteApplication(app) {
    const m = $('#confirm-delete-modal');
    if (!m) {
      // Modal node missing — fall back to native confirm to avoid silent failure
      if (confirm(`Delete application "${app.title || 'app#' + app.id}" at ${app.company || ''}? This cannot be undone.`)) {
        deleteApplication(app.id);
      }
      return;
    }
    $('#confirm-delete-title').textContent = `Delete "${app.title || 'application #' + app.id}"?`;
    $('#confirm-delete-detail').textContent =
      `${app.company || 'No company'} · status: ${app.status || '—'}. ` +
      `This removes the application from your pipeline. The underlying job posting stays in the Dashboard.`;
    m.classList.remove('hidden');
    const yes = $('#confirm-delete-yes');
    const no = $('#confirm-delete-no');
    const close = () => m.classList.add('hidden');
    yes.onclick = async () => {
      yes.disabled = true; yes.textContent = 'DELETING…';
      await deleteApplication(app.id);
      yes.disabled = false; yes.textContent = 'DELETE';
      close();
    };
    no.onclick = close;
    m.onclick = (e) => { if (e.target === m) close(); };
  }
  async function deleteApplication(appId) {
    const r = await api.delete('/api/applications/' + appId);
    if (r.ok) {
      toast('Application deleted.', 'success');
      loadPipeline();
    } else {
      toast('Delete failed: ' + (r.error || 'unknown'), 'error');
    }
  }
  async function loadPipeline() {
    if (!$('#kanban').children.length) bindPipelineBoard();
    // Use the server-side board grouping (single round-trip, status buckets)
    const r = await api.get('/api/applications/board', { silent: true });
    const board = (r.ok && r.data) || {};
    // Flatten for state.applications (some callers read this)
    state.applications = Object.values(board).flat();
    for (const col of $$('#kanban .kanban-col')) {
      const body = $('.col-body', col);
      body.innerHTML = '';
      const status = col.getAttribute('data-status');
      const apps = board[status] || [];
      $('.count', col).textContent = String(apps.length);
      for (const app of apps) {
        const showAnalyze = ['interview','offer','negotiating','interviewing'].includes((app.status || status || '').toLowerCase());
        const children = [
          el('div', { class: 'kc-title', text: safeText(app.title || ('app#' + app.id)) }),
          el('div', { class: 'kc-meta', text: `${safeText(app.company || '')} · score ${app.score ?? '—'}` }),
        ];
        if (app.deadline_at) {
          const dlSec = app.deadline_at < 1e12 ? app.deadline_at : app.deadline_at / 1000;
          const hoursLeft = (dlSec - Date.now() / 1000) / 3600;
          const cls = hoursLeft < 0 ? 'kc-deadline overdue' : hoursLeft < 48 ? 'kc-deadline soon' : 'kc-deadline';
          const when = new Date(dlSec * 1000).toISOString().slice(0, 10);
          children.push(el('div', { class: cls,
            text: (hoursLeft < 0 ? '⚠ deadline passed ' : 'deadline ') + when }));
        }
        if (showAnalyze) {
          children.push(el('button', {
            class: 'btn btn-secondary small', type: 'button',
            style: 'margin-top:6px;',
            onclick: (e) => {
              e.stopPropagation();
              window.openOffersForApp && window.openOffersForApp(app.id);
            },
          }, 'ANALYZE OFFER'));
        }
        const card = el('div', {
          class: 'kan-card', draggable: 'true',
          'data-app-id': String(app.id),
          onclick: () => openApplicationModal(app),
        }, children);
        card.addEventListener('dragstart', (e) => e.dataTransfer.setData('text/plain', String(app.id)));
        body.appendChild(card);
      }
    }
  }
  function openApplicationModal(app) {
    const m = $('#card-modal');
    const body = $('#card-modal-body');
    $('#card-modal-title').textContent = `${app.title || 'Application'} · ${app.company || ''}`;
    body.innerHTML = '';
    body.appendChild(el('label', {}, [
      'Notes',
      el('textarea', { id: 'app-notes', rows: 4, text: safeText(app.notes || '') }),
    ]));
    body.appendChild(el('label', {}, [
      'Next follow-up (YYYY-MM-DD)',
      el('input', { id: 'app-followup', type: 'date',
        value: app.next_followup_at ? new Date(app.next_followup_at * (app.next_followup_at < 1e12 ? 1000 : 1)).toISOString().slice(0,10) : '' }),
    ]));
    body.appendChild(el('label', {}, [
      'Application deadline (YYYY-MM-DD)',
      el('input', { id: 'app-deadline', type: 'date',
        value: app.deadline_at ? new Date(app.deadline_at * (app.deadline_at < 1e12 ? 1000 : 1)).toISOString().slice(0,10) : '' }),
    ]));
    body.appendChild(el('label', {}, [
      'Application URL',
      el('input', { id: 'app-url', type: 'url', value: safeText(app.application_url || '') }),
    ]));
    if (app.packet_path) {
      body.appendChild(el('p', { class: 'muted small', text: 'Packet: ' + app.packet_path }));
    }
    m.classList.remove('hidden');
    // Add view-packet button if a packet path was stored on the application.
    if (app.packet_path) {
      const viewBtn = el('button', { class: 'btn btn-secondary small', type: 'button',
        onclick: () => viewPacketManifest(app.id) }, 'VIEW PACKET');
      body.appendChild(viewBtn);
    }
    // Jump to INTERVIEW tab for prep + practice
    body.appendChild(el('button', {
      class: 'btn btn-primary small', type: 'button',
      onclick: () => {
        m.classList.add('hidden');
        state.interviewFocusAppId = app.id;
        switchPage('interview');
      },
    }, 'INTERVIEW PREP'));
    $('#card-save-btn').onclick = async () => {
      const dl = $('#app-deadline').value;
      const payload = {
        notes: $('#app-notes').value,
        application_url: $('#app-url').value || null,
        // epoch seconds, or null to clear
        deadline_at: dl ? Math.round(new Date(dl).getTime() / 1000) : null,
      };
      const r = await api.patch('/api/applications/' + app.id, payload);
      if (!r.ok) return;
      // Use the dedicated followup endpoint when the date changed
      const newF = $('#app-followup').value;
      if (newF) {
        const target = new Date(newF).getTime() / 1000;
        const daysAhead = Math.max(1, Math.round((target - Date.now()/1000) / 86400));
        await api.post('/api/applications/' + app.id + '/followup', { days: daysAhead });
      }
      toast('Application updated.', 'success');
      m.classList.add('hidden');
      loadPipeline();
    };
    $('#card-close-btn').onclick = () => m.classList.add('hidden');
  }
  async function viewPacketManifest(appId) {
    const r = await api.get('/api/packet/' + appId + '/manifest', { silent: true });
    if (!r.ok) { toast('No packet manifest found.', 'warn'); return; }
    const d = r.data || {};
    const files = d.files || [];
    const lines = ['Packet dir: ' + (d.packet_dir || '—'), '', 'FILES:'];
    files.forEach(f => lines.push('  · ' + (f.name || f.filename || String(f))));
    showTextModal('Packet manifest', lines.join('\n'),
      'Open files via /api/packet/' + appId + '/file/<filename>');
  }

  // ============================================================
  // INBOX
  // ============================================================
  async function loadInbox() {
    const r = await api.get('/api/email/events', { silent: true });
    const body = $('#inbox-table tbody');
    const status = $('#inbox-status');
    if (!r.ok) {
      status.textContent = 'Gmail / IMAP not configured. Connect via Settings to enable inbox.';
      status.classList.remove('hidden');
      body.innerHTML = '';
      body.appendChild(el('tr', {}, el('td', { colspan: 6, class: 'empty', text: 'No email source connected.' })));
      return;
    }
    status.classList.add('hidden');
    const evs = (r.data || []);
    body.innerHTML = '';
    if (!evs.length) {
      body.appendChild(el('tr', {}, el('td', { colspan: 6, class: 'empty', text: 'No emails ingested.' })));
      return;
    }
    for (const e of evs) {
      // Skip events already resolved server-side.
      if (e.status === 'replied' || e.status === 'ignored' || e.status === 'actioned') continue;
      const cls = e.classification || 'other';
      const cssClass = cls === 'recruiter' ? 'badge-green' : cls === 'rejection' ? 'badge-red' : cls === 'interview' ? 'badge-blue' : 'badge-muted';
      body.appendChild(el('tr', { 'data-event-id': String(e.id) }, [
        el('td', { text: fmtRel(e.received_at) }),
        el('td', { text: safeText(e.from_address || '—') }),
        el('td', { text: safeText(e.subject || '(no subject)') }),
        el('td', {}, el('span', { class: 'badge ' + cssClass, text: cls.toUpperCase() })),
        el('td', { text: safeText(e.job_id || '—') }),
        el('td', {}, [
          el('button', { class: 'btn btn-ghost small', onclick: () => draftReply(e) }, 'DRAFT REPLY'),
          el('button', { class: 'btn btn-ghost small', onclick: () => markReplied(e) }, 'MARK REPLIED'),
        ]),
      ]));
    }
  }
  async function draftReply(ev) {
    const r = await api.post('/api/email/draft-reply', { event_id: ev.id });
    if (!r.ok) return;
    const draft = r.data?.text || r.data?.draft || '';
    const m = $('#card-modal');
    $('#card-modal-title').textContent = 'Draft reply';
    const body = $('#card-modal-body');
    body.innerHTML = '';
    body.appendChild(el('p', { class: 'muted small', text: 'Drafts are never sent automatically. Copy into your email client.' }));
    body.appendChild(el('textarea', { id: 'draft-text', rows: 12, text: draft }));
    m.classList.remove('hidden');
    $('#card-save-btn').textContent = 'COPY';
    $('#card-save-btn').onclick = async () => {
      const txt = $('#draft-text').value;
      try { await navigator.clipboard.writeText(txt); toast('Copied.', 'success'); }
      catch { toast('Copy failed — select text manually.', 'error'); }
    };
    $('#card-close-btn').onclick = () => {
      $('#card-save-btn').textContent = 'SAVE';
      m.classList.add('hidden');
    };
  }
  async function markReplied(ev) {
    if (!ev || !ev.id) return;
    const r = await api.patch('/api/email/events/' + ev.id, { status: 'replied' });
    if (!r.ok) return;
    toast('Marked replied.', 'success');
    // Remove the row visually
    const rows = $$('#inbox-table tbody tr');
    for (const tr of rows) {
      if (tr.dataset.eventId === String(ev.id)) {
        tr.remove();
        break;
      }
    }
    const tbody = $('#inbox-table tbody');
    if (tbody && !tbody.children.length) {
      tbody.appendChild(el('tr', {}, el('td', { colspan: 6, class: 'empty', text: 'No emails ingested.' })));
    }
  }
  function bindInbox() {
    $('#inbox-refresh').addEventListener('click', loadInbox);
    const sweepBtn = $('#inbox-sweep');
    if (sweepBtn) {
      sweepBtn.addEventListener('click', async () => {
        sweepBtn.disabled = true;
        sweepBtn.textContent = 'SWEEPING…';
        const r = await api.post('/api/email/sweep', {});
        sweepBtn.disabled = false;
        sweepBtn.textContent = 'SWEEP NOW';
        if (r.ok) {
          const d = r.data || {};
          toast(`Sweep done · gmail=${(d.gmail?.processed ?? '–')} · imap=${(d.imap?.processed ?? '–')}`, 'success');
          loadInbox();
        }
      });
    }
    // status banner using /api/email/status
    api.get('/api/email/status', { silent: true }).then(r => {
      const status = $('#inbox-status');
      if (!status || !r.ok) return;
      const d = r.data || {};
      const gmail = d.gmail?.configured ? 'Gmail OK' : 'Gmail not configured';
      const imap = d.imap?.configured ? 'IMAP OK' : 'IMAP not configured';
      status.textContent = `${gmail} · ${imap}`;
      status.classList.remove('hidden');
    });
  }

  // ============================================================
  // CALENDAR
  // ============================================================
  const DAYS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
  const HOURS = ['8','9','10','11','12','13','14','15','16','17','18','19','20'];
  function renderAvailGrid() {
    const g = $('#avail-grid');
    if (!g) return;
    g.innerHTML = '';
    const head = el('tr', {}, [el('th', { text: '' })]);
    HOURS.forEach(h => head.appendChild(el('th', { text: h })));
    g.appendChild(head);
    DAYS.forEach((d, di) => {
      const tr = el('tr', {}, el('td', { class: 'label', text: d }));
      HOURS.forEach((h, hi) => {
        const on = state.availability?.[di]?.[hi];
        const td = el('td', {
          class: on ? 'on' : '',
          'aria-label': `${d} ${h}:00 ${on ? 'available' : 'unavailable'}`,
          onclick: () => {
            state.availability[di] = state.availability[di] || {};
            state.availability[di][hi] = !state.availability[di][hi];
            td.classList.toggle('on');
          },
        });
        tr.appendChild(td);
      });
      g.appendChild(tr);
    });
  }
  function bindCalendar() {
    $('#avail-clear').addEventListener('click', () => { state.availability = {}; renderAvailGrid(); });
    $('#avail-save').addEventListener('click', async () => {
      const r = await api.put('/api/profile', { interview_availability_json: state.availability });
      if (r.ok) toast('Availability saved.', 'success');
    });
    $('#suggest-slots').addEventListener('click', async () => {
      const r = await api.post('/api/calendar/slots', { availability: state.availability });
      const list = $('#slot-list'); list.innerHTML = '';
      const slots = (r.ok && (r.data?.slots || r.data)) || [];
      if (!slots.length) list.appendChild(el('li', { class: 'muted', text: 'No suggestions.' }));
      else slots.forEach(s => list.appendChild(el('li', { text: typeof s === 'string' ? s : JSON.stringify(s) })));
    });
    $('#cal-refresh').addEventListener('click', loadCalendarEvents);
    const createForm = $('#cal-create-form');
    if (createForm) {
      createForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const data = serializeForm(createForm);
        const status = $('#cal-create-status');
        if (!data.title || !data.start || !data.end) {
          if (status) status.textContent = 'title, start, end are required';
          return;
        }
        const startISO = new Date(data.start).toISOString();
        const endISO = new Date(data.end).toISOString();
        const attendees = csvToList(data.attendees || '');
        if (!confirm(`Create event "${data.title}" from ${startISO} to ${endISO}?`)) return;
        const body = {
          title: data.title,
          start: startISO,
          end: endISO,
          attendees,
          description: data.description || '',
          confirmed: true,
        };
        if (data.application_id) body.application_id = Number(data.application_id);
        if (status) status.textContent = 'creating…';
        const r = await api.post('/api/calendar/event', body);
        if (r.ok) {
          const d = r.data || {};
          const ics = d.ics_path ? ` · ICS: ${d.ics_path}` : '';
          if (status) status.textContent = 'created event #' + (d.event_id || '?') + ics;
          toast('Event created.', 'success');
          createForm.reset();
          loadCalendarEvents();
        } else if (status) {
          status.textContent = 'create failed';
        }
      });
    }
  }
  async function loadCalendarEvents() {
    // Status banner via /api/calendar/status (Google OAuth state)
    api.get('/api/calendar/status', { silent: true }).then(sr => {
      const sb = $('#cal-status');
      if (!sb || !sr.ok) return;
      const d = sr.data || {};
      const msg = d.configured ? 'Google Calendar connected.' : 'Google Calendar not connected — events save as .ics files locally.';
      sb.textContent = msg;
      sb.classList.remove('hidden');
    });
    // Fetch the actual events list (the /status endpoint only reports OAuth state).
    const r = await api.get('/api/calendar/events', { silent: true });
    const body = $('#cal-events tbody');
    body.innerHTML = '';
    const evs = (r.ok && (r.data?.events || r.data || [])) || [];
    if (!evs.length) {
      body.appendChild(el('tr', {}, el('td', { colspan: 4, class: 'empty', text: 'None.' })));
      return;
    }
    for (const e of evs) {
      const icsLink = el('a', {
        href: '/api/calendar/' + e.id + '/ics',
        target: '_blank',
        class: 'btn btn-ghost small',
        text: 'DOWNLOAD .ics',
      });
      body.appendChild(el('tr', {}, [
        el('td', { text: fmtDate(e.start_at) }),
        el('td', { text: safeText(e.with || e.attendees || '—') }),
        el('td', { text: safeText(e.application_id || '—') }),
        el('td', {}, icsLink),
      ]));
    }
  }

  // ============================================================
  // SETTINGS
  // ============================================================
  async function loadSettings() {
    const r = await api.get('/api/settings', { silent: true });
    if (!r.ok) return;
    const d = r.data || {};
    state.settings = d;
    // mode
    if (d.default_mode) $('#mode-pill').textContent = 'MODE: ' + String(d.default_mode).toUpperCase();
    if ($('#mode-form')?.elements.mode) $('#mode-form').elements.mode.value = d.default_mode || 'assisted';
    // providers
    const pg = $('#provider-grid'); pg.innerHTML = '';
    const items = [
      ['LLM provider', d.llm?.provider || '—'],
      ['Anthropic', d.llm?.anthropic_configured ? 'configured' : 'missing'],
      ['OpenAI', d.llm?.openai_configured ? 'configured' : 'missing'],
      ['Ollama', d.llm?.ollama_configured ? 'configured' : 'missing'],
      ['Embeddings', d.embeddings?.provider || '—'],
      ['SerpAPI', d.integrations?.serpapi ? 'configured' : 'missing'],
      ['SearchAPI', d.integrations?.searchapi ? 'configured' : 'missing'],
      ['GitHub token', d.integrations?.github ? 'configured' : 'missing'],
      ['Google OAuth', d.integrations?.google_oauth ? 'configured' : 'missing'],
      ['IMAP', d.integrations?.imap ? 'configured' : 'missing'],
    ];
    for (const [label, val] of items) {
      const ok = val === 'configured';
      pg.appendChild(el('div', { class: 'kpi' }, [
        el('span', { class: 'kpi-num', text: ok ? 'OK' : '—' }),
        el('span', { class: 'kpi-label', text: label }),
        el('span', { class: 'badge ' + (ok ? 'badge-green' : 'badge-muted'), text: val }),
      ]));
    }
    // sources
    const body = $('#sources-table tbody'); body.innerHTML = '';
    const sources = d.job_sources || [];
    if (!sources.length) {
      body.appendChild(el('tr', {}, el('td', { colspan: 7, class: 'empty', text: 'No sources registered.' })));
    } else {
      for (const s of sources) {
        const policy = s.policy || {};
        const risk = (policy.risk_level || policy.risk || 'GRAY').toUpperCase();
        const riskCls = risk === 'LEGAL' ? 'badge-green' : risk === 'GRAY' ? 'badge-warn' : 'badge-red';
        const testBtn = el('button', {
          class: 'btn btn-ghost small',
          type: 'button',
          'data-action': 'test-source',
          onclick: () => testSource(s.name),
        }, 'TEST');
        if (!s.healthy) testBtn.disabled = true;
        body.appendChild(el('tr', { 'data-source': s.name }, [
          el('td', { text: safeText(policy.display_name || s.name) }),
          el('td', {}, el('span', { class: 'badge ' + (s.healthy ? 'badge-green' : 'badge-muted'), text: s.healthy ? 'YES' : 'no' })),
          el('td', {}, el('span', { class: 'badge ' + riskCls, text: risk })),
          el('td', { text: policy.apply_automation_allowed ? 'yes' : 'no' }),
          el('td', { text: safeText(policy.note || policy.description || '—') }),
          el('td', {}, testBtn),
          el('td', { 'data-cell': 'status' }, el('span', { class: 'muted small', text: s.healthy ? '—' : 'unhealthy' })),
        ]));
      }
    }
    // auto-apply
    $('#aa-status').textContent = d.auto_apply_enabled ? 'ENABLED' : 'disabled';
    $('#aa-cap').textContent = d.auto_apply_daily_cap ?? '—';
    $('#aa-min').textContent = d.auto_apply_min_score ?? '—';
    $('#compliance-banner').classList.toggle('hidden', !d.auto_apply_enabled);
    // weights — pull from profile if set
    const w = (state.profile && state.profile.scoring_weights_json) || null;
    if (w && typeof w === 'object') {
      for (const inp of $$('#weights-form input[type="range"]')) {
        if (w[inp.name] != null) {
          inp.value = w[inp.name];
          inp.nextElementSibling.textContent = Number(w[inp.name]).toFixed(2);
        }
      }
    }
    refreshWeightTotal();
    loadApiKeys();
    loadSchedulerStatus();
    loadAutoApplyQueue();
  }

  async function loadSchedulerStatus() {
    const r = await api.get('/api/scheduler/status', { silent: true });
    const line = $('#scheduler-status-line');
    const list = $('#scheduler-jobs');
    if (!line || !list) return;
    list.innerHTML = '';
    if (!r.ok) {
      line.textContent = 'Scheduler not running.';
      return;
    }
    const d = r.data || {};
    line.textContent = (d.running ? 'Running' : 'Stopped') +
      ' · ' + (d.jobs?.length ?? 0) + ' jobs registered';
    for (const j of (d.jobs || [])) {
      list.appendChild(el('li', { text: `${j.id || ''} — next run ${j.next_run_time || j.next_run || 'unscheduled'}` }));
    }
    if (!(d.jobs || []).length) {
      list.appendChild(el('li', { class: 'muted', text: 'No scheduled jobs. Saved searches and inbox sweep register on startup.' }));
    }
  }

  async function loadAutoApplyQueue() {
    const tbody = $('#aa-queue-table tbody');
    if (!tbody) return;
    const r = await api.get('/api/auto-apply/queue', { silent: true });
    tbody.innerHTML = '';
    const rows = (r.ok && (r.data || [])) || [];
    if (!rows.length) {
      tbody.appendChild(el('tr', {}, el('td', { colspan: 5, class: 'empty', text: 'No queued auto-apply packets.' })));
      return;
    }
    for (const a of rows) {
      tbody.appendChild(el('tr', {}, [
        el('td', { text: String(a.id) }),
        el('td', { text: safeText(a.title || a.job_title || '—') }),
        el('td', { text: safeText(a.company || a.job_company || '—') }),
        el('td', { text: a.score != null ? String(a.score) : '—' }),
        el('td', { text: safeText(a.packet_path || '—') }),
      ]));
    }
  }

  // ----- API keys -----
  async function loadApiKeys() {
    const r = await api.get('/api/settings/api-keys', { silent: true });
    if (!r.ok) return;
    const keys = r.data?.keys || [];
    const envPath = r.data?.env_path || '.env';
    const pathEl = $('#api-keys-env-path');
    if (pathEl) pathEl.textContent = envPath;

    const groups = { llm: [], jobs: [], google: [], imap: [] };
    for (const k of keys) (groups[k.group] || (groups[k.group] = [])).push(k);

    for (const [grp, rows] of Object.entries(groups)) {
      const host = document.querySelector(`#api-keys-group-${grp} .api-keys-rows`);
      if (!host) continue;
      host.innerHTML = '';
      for (const k of rows) {
        host.appendChild(renderApiKeyRow(k));
      }
    }
  }

  function renderApiKeyRow(k) {
    const wrap = el('label', { class: 'api-key-row', 'data-env': k.env });
    const statusBadge = el('span', {
      class: 'badge api-key-status ' + (k.configured ? 'badge-blue' : 'badge-muted'),
      text: k.configured ? 'set · not tested' : 'not set'
    });
    const head = el('div', { class: 'api-key-head' }, [
      el('span', { class: 'api-key-label', text: k.label }),
      el('span', { class: 'api-key-env', text: k.env }),
      statusBadge,
    ]);
    wrap.appendChild(head);
    if (k.purpose) {
      wrap.appendChild(el('span', { class: 'api-key-purpose muted small', text: k.purpose }));
    }

    let input;
    if (k.kind === 'choice') {
      input = el('select', { name: k.env, 'data-kind': 'choice' });
      for (const opt of (k.choices || [])) {
        const o = el('option', { value: opt, text: opt });
        if (opt === (k.preview || '').trim()) o.selected = true;
        input.appendChild(o);
      }
    } else {
      const type = k.kind === 'secret' ? 'password' : (k.kind === 'url' ? 'url' : 'text');
      input = el('input', {
        type, name: k.env, autocomplete: 'off', spellcheck: 'false',
        placeholder: k.kind === 'secret'
          ? (k.configured ? `currently: ${k.preview}` : 'paste key here')
          : (k.preview || ''),
      });
      if (k.kind !== 'secret' && k.preview) input.value = k.preview;
    }
    input.classList.add('api-key-input');
    wrap.appendChild(input);

    const actions = el('div', { class: 'api-key-actions' });
    if (k.kind === 'secret') {
      const reveal = el('button', { type: 'button', class: 'btn btn-ghost small' }, 'SHOW');
      reveal.addEventListener('click', () => {
        const showing = input.type === 'text';
        input.type = showing ? 'password' : 'text';
        reveal.textContent = showing ? 'SHOW' : 'HIDE';
      });
      const clear = el('button', { type: 'button', class: 'btn btn-ghost small' }, 'CLEAR');
      clear.addEventListener('click', () => { input.value = ''; input.dataset.cleared = '1'; });
      actions.appendChild(reveal);
      actions.appendChild(clear);
    }
    const testBtn = el('button', { type: 'button', class: 'btn btn-ghost small' }, 'TEST');
    testBtn.addEventListener('click', () => testApiKey(k.env, input.value.trim()));
    actions.appendChild(testBtn);
    wrap.appendChild(actions);

    const unlocksMsg = el('span', { class: 'api-key-msg muted small' });
    wrap.appendChild(unlocksMsg);

    return wrap;
  }

  async function testApiKey(envName, overrideValue) {
    const row = document.querySelector(`.api-key-row[data-env="${envName}"]`);
    if (!row) return;
    const badge = row.querySelector('.api-key-status');
    const msg = row.querySelector('.api-key-msg');
    badge.className = 'badge api-key-status badge-muted';
    badge.textContent = 'testing…';
    msg.textContent = '';

    const body = { env: envName };
    if (overrideValue) body.value = overrideValue;
    const r = await api.post('/api/settings/api-keys/test', body, { silent: true });
    const d = r.data || {};
    applyApiKeyTestResult(row, d);
  }

  function applyApiKeyTestResult(row, d) {
    const badge = row.querySelector('.api-key-status');
    const msg = row.querySelector('.api-key-msg');
    const ok = !!d.ok;
    badge.className = 'badge api-key-status ' + (ok ? 'badge-green' : 'badge-red');
    badge.textContent = ok ? '✓ working' : (d.status || 'failed');
    let line = d.message || '';
    if (d.latency_ms != null) line += ` · ${d.latency_ms}ms`;
    msg.textContent = line;
  }

  async function testAllApiKeys() {
    const r = await api.post('/api/settings/api-keys/test-all', {}, { silent: true });
    if (!r.ok) return;
    for (const res of (r.data?.results || [])) {
      const row = document.querySelector(`.api-key-row[data-env="${res.env}"]`);
      if (row) applyApiKeyTestResult(row, res);
    }
  }

  function bindApiKeysForm() {
    const f = $('#api-keys-form');
    if (!f) return;
    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      const payload = { keys: {} };
      for (const inp of f.querySelectorAll('.api-key-input')) {
        const env = inp.name;
        const val = (inp.value || '').trim();
        // For secrets, only send if user typed something OR explicitly cleared.
        if (inp.type === 'password' || inp.dataset.kind === 'secret-was') {
          if (val || inp.dataset.cleared === '1') payload.keys[env] = val;
        } else {
          payload.keys[env] = val;
        }
      }
      if (!Object.keys(payload.keys).length) {
        toast('Nothing to save.', 'warn');
        return;
      }
      $('#api-keys-status').textContent = 'Saving…';
      const r = await api.put('/api/settings/api-keys', payload);
      if (r.ok) {
        const d = r.data || {};
        const u = (d.updated || []).length;
        const c = (d.cleared || []).length;
        $('#api-keys-status').textContent =
          `Saved. Testing… updated=${u} cleared=${c}. Written to ${d.env_path || '.env'}.`;
        toast(`API keys saved (${u} set, ${c} cleared). Testing now…`, 'success');
        // Reload settings + api keys to refresh "configured" badges
        await loadSettings();
        // Test everything that has a value so the user sees green/red checks
        await testAllApiKeys();
        $('#api-keys-status').textContent =
          `Saved + tested. Updated=${u} Cleared=${c}.`;
      } else {
        $('#api-keys-status').textContent = 'Save failed.';
      }
    });
    $('#api-keys-reload').addEventListener('click', async () => {
      await loadApiKeys();
      await testAllApiKeys();
    });
  }

  function refreshWeightTotal() {
    let total = 0;
    for (const inp of $$('#weights-form input[type="range"]')) {
      total += Number(inp.value);
      inp.nextElementSibling.textContent = Number(inp.value).toFixed(2);
    }
    const tEl = $('#weights-total');
    tEl.textContent = total.toFixed(2);
    tEl.style.color = Math.abs(total - 1) < 0.001 ? 'var(--positive)' : 'var(--accent)';
  }

  // ============================================================
  // LOCAL LLM detection + pin
  // ============================================================
  function bindLocalLLM() {
    const btn = $('#llm-discover');
    const testBtn = $('#llm-test');
    const tplBtn = $('#llm-use-template');
    if (!btn) return;
    btn.addEventListener('click', discoverLocalLLMs);
    if (testBtn) testBtn.addEventListener('click', testActiveLLM);
    if (tplBtn) tplBtn.addEventListener('click', async () => {
      if (!confirm('Switch to the deterministic template provider? No LLM will be used.')) return;
      const r = await api.post('/api/llm/use-template', {});
      if (r.ok) {
        toast('LLM disabled — using deterministic templates.', 'success');
        loadSettings();
      } else {
        toast('Could not switch: ' + (r.error || 'unknown'), 'error');
      }
    });
    // Wire up result-panel click handler (delegated)
    $('#llm-discover-result').addEventListener('click', async (e) => {
      const useBtn = e.target.closest('[data-use-model]');
      if (!useBtn) return;
      const baseUrl = useBtn.getAttribute('data-base-url');
      const model = useBtn.getAttribute('data-use-model');
      const kind = useBtn.getAttribute('data-kind') || 'ollama';
      useBtn.disabled = true;
      useBtn.textContent = 'PINNING…';
      const r = await api.post('/api/llm/use-local', {
        base_url: baseUrl, model, provider_kind: kind,
      });
      useBtn.disabled = false;
      useBtn.textContent = 'USE THIS MODEL';
      if (r.ok) {
        toast(`Now using ${model} via local ${kind}.`, 'success');
        $('#llm-status').textContent = `ACTIVE: ${kind} · ${model} · ${baseUrl}`;
        loadSettings();
      } else {
        toast('Could not pin model: ' + (r.error || 'unknown'), 'error');
      }
    });
  }

  async function discoverLocalLLMs() {
    const btn = $('#llm-discover');
    const status = $('#llm-status');
    const result = $('#llm-discover-result');
    const guide = $('#llm-install-guide');
    const guideBody = $('#llm-install-body');
    btn.disabled = true; btn.textContent = 'SCANNING…';
    status.textContent = 'Scanning loopback ports (11434, 1234, 8000, 8080)…';
    result.classList.add('hidden');
    guide.classList.add('hidden');

    const r = await api.get('/api/llm/discover', { silent: true });
    btn.disabled = false; btn.textContent = 'DETECT LOCAL LLMS';
    if (!r.ok) {
      status.textContent = 'Discovery failed: ' + (r.error || 'unknown');
      return;
    }
    const d = r.data || {};
    const daemons = d.daemons || [];
    const rec = d.recommended || {};
    const cur = d.current || {};
    const ramGb = d.ram_gb || 0;

    if (!daemons.length) {
      status.textContent = `No local LLM daemon detected. RAM: ${ramGb || '?'} GB. ` +
        `You can still use Anthropic / OpenAI by entering an API key below, ` +
        `or install Ollama (see guide).`;
      guide.classList.remove('hidden');
      if (d.install_guide) guideBody.innerHTML = renderInstallGuide(d.install_guide, rec);
      return;
    }

    status.textContent = `Found ${daemons.length} daemon(s) · ${ramGb} GB RAM · ` +
      `current provider: ${cur.provider || 'auto'} (${cur.model || 'default'})`;
    result.classList.remove('hidden');
    result.innerHTML = renderDiscoveryResult(daemons, rec, cur, ramGb);
  }

  function renderDiscoveryResult(daemons, rec, cur, ramGb) {
    const parts = [];
    if (rec && rec.installed) {
      parts.push(`<div class="llm-rec">
        <strong>RECOMMENDED FOR YOU:</strong> ${rec.name}
        <div class="muted small">${rec.reason || ''}</div>
        <button class="btn btn-primary small" type="button"
                data-use-model="${rec.name}"
                data-base-url="${rec.base_url || daemons[0].base_url}"
                data-kind="${rec.type || daemons[0].type}">USE THIS MODEL</button>
      </div>`);
    } else if (rec && !rec.installed) {
      parts.push(`<div class="llm-rec llm-rec-pull">
        <strong>SUGGESTED MODEL TO INSTALL:</strong> ${rec.name}
        <div class="muted small">${rec.reason || ''}</div>
        <pre class="codeblock"><code>ollama pull ${rec.name}</code></pre>
      </div>`);
    }
    for (const daemon of daemons) {
      parts.push(`<div class="llm-daemon">
        <h5>${daemon.type.toUpperCase()} <span class="muted small">${daemon.base_url}</span></h5>
        <div class="llm-model-grid">`);
      for (const m of daemon.models) {
        const isCurrent = (cur.model === m);
        parts.push(`<div class="llm-model ${isCurrent ? 'llm-model-active' : ''}">
          <span class="llm-model-name">${m}</span>
          ${isCurrent
            ? '<span class="ap-kpi" style="font-size:var(--fs-micro);">ACTIVE</span>'
            : `<button class="btn btn-ghost small" type="button"
                  data-use-model="${m}"
                  data-base-url="${daemon.base_url}"
                  data-kind="${daemon.type}">USE</button>`}
        </div>`);
      }
      parts.push('</div></div>');
    }
    return parts.join('');
  }

  function renderInstallGuide(guide, rec) {
    const parts = [`<p class="muted small">OS detected: <strong>${guide.os}</strong></p>`];
    parts.push('<ol class="install-steps">');
    for (const step of (guide.steps || [])) {
      parts.push(`<li>
        <strong>${step.title}</strong>
        ${step.command ? `<pre class="codeblock"><code>${step.command}</code></pre>` : ''}
        ${step.alt_command ? `<p class="muted small">or: ${step.alt_command}</p>` : ''}
        ${step.note ? `<p class="muted small">${step.note}</p>` : ''}
      </li>`);
    }
    parts.push('</ol>');
    if (rec && rec.name) {
      parts.push(`<p class="muted small">For this machine we recommend pulling
        <strong>${rec.name}</strong> (${rec.reason || ''}).</p>`);
    }
    return parts.join('');
  }

  async function testActiveLLM() {
    const btn = $('#llm-test');
    btn.disabled = true; btn.textContent = 'TESTING…';
    const r = await api.post('/api/llm/test', {});
    btn.disabled = false; btn.textContent = 'TEST ACTIVE PROVIDER';
    if (r.ok) {
      const d = r.data || {};
      toast(`OK · ${d.provider} (${d.model}) · ${d.elapsed_ms ?? '?'} ms · "${d.sample || ''}"`, 'success');
    } else {
      toast('LLM test failed: ' + (r.error || 'unknown'), 'error');
    }
  }
  function bindSettings() {
    bindLocalLLM();
    $('#weights-form').addEventListener('input', refreshWeightTotal);
    $('#weights-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const weights = {};
      for (const inp of $$('#weights-form input[type="range"]')) weights[inp.name] = Number(inp.value);
      const total = Object.values(weights).reduce((s, v) => s + v, 0);
      if (Math.abs(total - 1) > 0.001) {
        toast(`Weights must sum to 1.00 (current ${total.toFixed(2)}).`, 'error');
        return;
      }
      const r = await api.put('/api/profile', { scoring_weights_json: weights });
      if (r.ok) toast('Weights saved.', 'success');
    });
    $('#mode-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const mode = e.target.elements.mode.value;
      const r = await api.put('/api/settings/mode', { mode });
      if (r.ok) {
        const finalMode = (r.data && r.data.mode) || mode;
        toast('Mode: ' + finalMode, 'success');
        $('#mode-pill').textContent = 'MODE: ' + String(finalMode).toUpperCase();
      }
    });
    $('#aa-enable').addEventListener('click', () => $('#aa-modal').classList.remove('hidden'));
    $('#aa-cancel-btn').addEventListener('click', () => $('#aa-modal').classList.add('hidden'));
    function updateAaBtn() {
      $('#aa-confirm-btn').disabled = !($('#aa-ack').checked && $('#aa-confirm').value.trim() === 'ENABLE');
    }
    $('#aa-ack').addEventListener('change', updateAaBtn);
    $('#aa-confirm').addEventListener('input', updateAaBtn);
    $('#aa-confirm-btn').addEventListener('click', async () => {
      // /api/auto-apply/resume requires {i_understand: true} (server-side
      // gate matching the modal's typed-confirm). The modal already enforces
      // the user typed ENABLE before this button enables, so we forward the
      // acknowledgement to the server.
      const r = await api.post('/api/auto-apply/resume', { i_understand: true });
      if (r.ok) toast('Auto-apply runner resumed.', 'success');
      else toast('Auto-apply could not be enabled: ' + (r.error || 'unknown error'), 'error');
      $('#aa-modal').classList.add('hidden');
      if (r.ok) $('#compliance-banner').classList.remove('hidden');
      loadSettings();
    });
    $('#aa-disable').addEventListener('click', async () => {
      // DISABLE = turn off runtime flag (different from HALT, which is
      // the emergency kill switch). Use the dedicated endpoint.
      const r = await api.post('/api/auto-apply/disable', {});
      if (r.ok) toast('Auto-apply disabled.', 'success');
      $('#compliance-banner').classList.add('hidden');
      loadSettings();
    });
    $('#aa-halt').addEventListener('click', haltAutoApply);
    $('#halt-auto').addEventListener('click', haltAutoApply);

    // Auto-apply: RUN NOW + queue refresh
    const aaRunBtn = $('#aa-run');
    if (aaRunBtn) {
      aaRunBtn.addEventListener('click', async () => {
        aaRunBtn.disabled = true;
        aaRunBtn.textContent = 'RUNNING…';
        const r = await api.post('/api/auto-apply/run', {});
        aaRunBtn.disabled = false;
        aaRunBtn.textContent = 'RUN NOW';
        if (r.ok) {
          const d = r.data || {};
          toast(`Auto-apply: prepared ${d.prepared ?? 0}, skipped ${(d.skipped || []).length}.`, 'success');
          loadAutoApplyQueue();
        } else {
          toast('Auto-apply refused: ' + (r.error || r.data?.reason || 'unknown'), 'warn');
        }
      });
    }
    const aaQRefresh = $('#aa-queue-refresh');
    if (aaQRefresh) aaQRefresh.addEventListener('click', loadAutoApplyQueue);

    // Scheduler controls
    const schRefresh = $('#scheduler-refresh');
    if (schRefresh) schRefresh.addEventListener('click', loadSchedulerStatus);
    const schSweepNow = $('#scheduler-sweep-now');
    if (schSweepNow) schSweepNow.addEventListener('click', async () => {
      schSweepNow.disabled = true;
      const r = await api.post('/api/scheduler/inbox-sweep', {});
      schSweepNow.disabled = false;
      if (r.ok) toast('Inbox sweep triggered.', 'success');
    });
    const schFollow = $('#scheduler-followups-now');
    if (schFollow) schFollow.addEventListener('click', async () => {
      schFollow.disabled = true;
      const r = await api.post('/api/scheduler/followups', {});
      schFollow.disabled = false;
      if (r.ok) toast(`Followups: ${r.data?.due ?? 0} due, ${r.data?.notified ?? 0} notified.`, 'success');
    });

    $('#export-data').addEventListener('click', async () => {
      const status = $('#data-status');
      if (status) status.textContent = 'preparing export…';
      try {
        const redact = $('#export-redact') && $('#export-redact').checked;
        const r = await fetch('/api/data/export' + (redact ? '?redact_pii=true' : ''), { method: 'GET' });
        if (!r.ok) {
          const txt = await r.text();
          toast('Export failed: ' + (txt || r.status), 'error');
          if (status) status.textContent = 'export failed';
          return;
        }
        const blob = await r.blob();
        const cd = r.headers.get('content-disposition') || '';
        const m = /filename="?([^";]+)"?/.exec(cd);
        const filename = (m && m[1]) || `jhh-export-${Date.now()}.json`;
        const counts = r.headers.get('x-jhh-export-counts');
        const url = URL.createObjectURL(blob);
        const a2 = document.createElement('a');
        a2.href = url; a2.download = filename;
        document.body.appendChild(a2);
        a2.click();
        a2.remove();
        URL.revokeObjectURL(url);
        toast('Export downloaded.', 'success');
        if (status) status.textContent = 'export saved as ' + filename + (counts ? ' · ' + counts : '');
      } catch (e) {
        toast('Export failed: ' + e.message, 'error');
        if (status) status.textContent = 'export failed';
      }
    });

    // Tracker import (Huntr / Teal / generic CSV)
    const trackerInput = $('#tracker-import-file');
    const trackerBtn = $('#tracker-import-btn');
    if (trackerBtn && trackerInput) {
      trackerBtn.addEventListener('click', () => trackerInput.click());
      trackerInput.addEventListener('change', async () => {
        const file = trackerInput.files && trackerInput.files[0];
        if (!file) return;
        const fmt = ($('#tracker-format') || {}).value || 'csv';
        const status = $('#tracker-import-status');
        const out = $('#tracker-import-result');
        if (status) status.textContent = 'importing…';
        if (out) out.innerHTML = '';
        const fd = new FormData();
        fd.append('file', file);
        fd.append('format', fmt);
        const r = await api.post('/api/data/import-tracker', fd);
        trackerInput.value = '';
        if (!r.ok) { if (status) status.textContent = 'import failed'; return; }
        const d = r.data || {};
        if (status) status.textContent = 'done';
        if (out) {
          for (const [k, label] of [['imported_jobs', 'Jobs imported'],
              ['imported_applications', 'Applications imported'],
              ['skipped_duplicates', 'Duplicates skipped']]) {
            out.appendChild(el('div', { class: 'kv-row' }, [
              el('span', { class: 'kv-key', text: label }),
              el('span', { class: 'kv-val', text: String(d[k] ?? 0) }),
            ]));
          }
          const errs = d.errors || [];
          if (errs.length) out.appendChild(el('div', { class: 'kv-row' }, [
            el('span', { class: 'kv-key', text: 'Row errors' }),
            el('span', { class: 'kv-val ss-error', text: errs.slice(0, 5).join('; ') }),
          ]));
        }
        toast(`Imported ${d.imported_jobs ?? 0} jobs, ${d.imported_applications ?? 0} applications.`, 'success');
        loadJobs();
      });
    }

    const importInput = $('#import-data-file');
    $('#import-data').addEventListener('click', () => importInput && importInput.click());
    if (importInput) {
      importInput.addEventListener('change', async () => {
        if (!importInput.files || !importInput.files[0]) return;
        const file = importInput.files[0];
        if (!confirm(`Import "${file.name}" into the live database? Existing rows with matching IDs will be overwritten.`)) {
          importInput.value = '';
          return;
        }
        const status = $('#data-status');
        if (status) status.textContent = 'importing…';
        const fd = new FormData();
        fd.append('file', file);
        const r = await api.post('/api/data/import', fd);
        importInput.value = '';
        if (r.ok) {
          const d = r.data || {};
          const counts = d.imported_counts || {};
          const summary = Object.entries(counts).filter(([, n]) => n > 0)
            .map(([t, n]) => `${t}=${n}`).join(' ');
          toast('Import complete.', 'success');
          if (status) status.textContent = 'imported · ' + (summary || '0 rows') +
            (d.error_count ? ` · ${d.error_count} errors` : '');
          loadSettings();
          if (typeof loadVaultSummary === 'function') loadVaultSummary();
        } else if (status) {
          status.textContent = 'import failed';
        }
      });
    }

    $('#delete-data').addEventListener('click', async () => {
      if (!confirm('DELETE ALL DATA — are you sure?')) return;
      if (!confirm('This cannot be undone. Continue?')) return;
      const status = $('#data-status');
      if (status) status.textContent = 'wiping…';
      const r = await api.del('/api/data?i_understand=ENABLE');
      if (r.ok) {
        toast('All data deleted. Profile reset.', 'success');
        const d = r.data || {};
        const counts = d.counts || {};
        const summary = Object.entries(counts).map(([t, n]) => `${t}=${n}`).join(' ');
        if (status) status.textContent = 'wiped · ' + summary;
        // Reload everything we can
        loadSettings();
        if (typeof loadVaultSummary === 'function') loadVaultSummary();
      } else if (status) {
        status.textContent = 'delete failed';
      }
    });

    // ----- Source-test "TEST ALL" -----
    const testAllBtn = $('#sources-test-all');
    if (testAllBtn) {
      testAllBtn.addEventListener('click', async () => {
        const statusEl = $('#sources-test-status');
        if (statusEl) statusEl.textContent = 'testing all healthy adapters…';
        const sources = (state.settings && state.settings.job_sources) || [];
        const healthy = sources.filter(s => s.healthy);
        let okCount = 0;
        for (const s of healthy) {
          const ok = await testSource(s.name);
          if (ok) okCount += 1;
        }
        if (statusEl) statusEl.textContent =
          `tested ${healthy.length} · ok=${okCount} · failed=${healthy.length - okCount}`;
      });
    }

    // ----- Saved searches -----
    const saveBtn = $('#save-search-btn');
    if (saveBtn) saveBtn.addEventListener('click', saveCurrentSearch);
    const reloadSaved = $('#saved-searches-reload');
    if (reloadSaved) reloadSaved.addEventListener('click', loadSavedSearches);
  }

  // ============================================================
  // Source connector live-test
  // ============================================================
  async function testSource(name) {
    const row = document.querySelector(`#sources-table tr[data-source="${name}"]`);
    const cell = row ? row.querySelector('[data-cell="status"]') : null;
    const btn = row ? row.querySelector('button[data-action="test-source"]') : null;
    if (cell) { cell.innerHTML = ''; cell.appendChild(el('span', { class: 'badge badge-muted', text: 'testing…' })); }
    if (btn) btn.disabled = true;
    const r = await api.post('/api/settings/sources/test/' + encodeURIComponent(name), {}, { silent: true });
    if (btn) btn.disabled = false;
    const d = (r && r.data) || {};
    const ok = !!d.ok;
    if (cell) {
      cell.innerHTML = '';
      cell.appendChild(el('span', {
        class: 'badge ' + (ok ? 'badge-green' : 'badge-red'),
        text: ok ? `${d.records ?? '?'} rec · ${d.latency_ms ?? '?'}ms` : (d.status || 'failed'),
      }));
      if (d.message) {
        cell.appendChild(el('div', { class: 'muted small', text: d.message }));
      }
    }
    return ok;
  }

  // ============================================================
  // Saved searches
  // ============================================================
  async function loadSavedSearches() {
    const tbody = $('#saved-searches-table tbody');
    if (!tbody) return;
    const r = await api.get('/api/scheduler/saved-searches', { silent: true });
    const list = (r.ok && (r.data || [])) || [];
    tbody.innerHTML = '';
    if (!list.length) {
      tbody.appendChild(el('tr', {}, el('td', { colspan: 7, class: 'empty', text: 'None yet.' })));
      return;
    }
    for (const s of list) {
      const q = s.query_json || {};
      const summary = [q.query, q.location, q.is_remote ? 'remote' : null].filter(Boolean).join(' · ');
      tbody.appendChild(el('tr', {}, [
        el('td', { text: String(s.id) }),
        el('td', { text: safeText(s.label || '') }),
        el('td', { text: summary || '(no query)' }),
        el('td', { text: String(s.frequency_hours ?? 24) }),
        el('td', {}, [
          document.createTextNode(fmtRel(s.last_run_at)),
          ...(s.last_error ? [el('div', { class: 'ss-error', title: s.last_error,
            text: '⚠ ' + String(s.last_error).slice(0, 60) })] : []),
        ]),
        el('td', { text: s.enabled ? 'yes' : 'no' }),
        el('td', {}, [
          el('button', { class: 'btn btn-ghost small', onclick: () => runSavedSearch(s.id) }, 'RUN NOW'),
          el('button', { class: 'btn btn-ghost small', onclick: () => dryRunSavedSearch(s.id) }, 'DRY RUN'),
          el('button', { class: 'btn btn-ghost small', onclick: () => toggleSavedSearch(s.id, !s.enabled) },
             s.enabled ? 'PAUSE' : 'RESUME'),
          el('button', { class: 'btn btn-ghost small', onclick: () => deleteSavedSearch(s.id) }, 'DELETE'),
        ]),
      ]));
    }
  }

  async function saveCurrentSearch() {
    const form = $('#search-form');
    if (!form) return;
    const body = serializeForm(form);
    if (!body.query) { toast('Query is required to save a search.', 'error'); return; }
    const labelDefault = body.query + (body.location ? ' / ' + body.location : '');
    const label = prompt('Label for this saved search:', labelDefault);
    if (label == null) return;
    const freqRaw = prompt('Run every how many hours?', '24');
    if (freqRaw == null) return;
    const frequency_hours = Math.max(1, parseInt(freqRaw, 10) || 24);
    const r = await api.post('/api/scheduler/saved-searches', {
      label: label || labelDefault,
      query: body,
      frequency_hours,
      enabled: true,
    });
    if (r.ok) {
      toast('Saved search created.', 'success');
      loadSavedSearches();
    }
  }

  async function runSavedSearch(sid) {
    toast('Running saved search…');
    const r = await api.post('/api/scheduler/run-now/' + sid, {});
    if (r.ok) {
      toast('Saved search done.', 'success');
      loadSavedSearches();
      loadJobs();
    }
  }

  async function dryRunSavedSearch(sid) {
    toast('Dry-running (no jobs saved)…');
    const r = await api.post('/api/scheduler/saved-searches/' + sid + '/dry-run', {});
    if (!r.ok) return;
    const d = r.data || {};
    const top = (d.top || []).map(t => `• ${t.title} — ${t.company}`).join('\n');
    toast(`Dry run: ${d.would_insert} new / ${d.duplicates} dup of ${d.discovered} found.`, 'success');
    alert(`DRY RUN (nothing saved)\n\nWould insert: ${d.would_insert}\nDuplicates: ${d.duplicates}\nDiscovered: ${d.discovered}\n\n${top || '(no sample)'}`);
  }

  async function toggleSavedSearch(sid, enabled) {
    const r = await api.patch('/api/scheduler/saved-searches/' + sid, { enabled });
    if (r.ok) {
      toast(enabled ? 'Saved search resumed.' : 'Saved search paused.', 'success');
      loadSavedSearches();
    }
  }

  async function deleteSavedSearch(sid) {
    if (!confirm('Delete saved search #' + sid + '?')) return;
    const r = await api.del('/api/scheduler/saved-searches/' + sid);
    if (r.ok) { toast('Deleted.', 'success'); loadSavedSearches(); }
  }

  async function haltAutoApply() {
    const r = await api.post('/api/auto-apply/halt', {});
    if (r.ok) { toast('Auto-apply halted.', 'success'); loadSettings(); $('#compliance-banner').classList.add('hidden'); }
  }

  // ============================================================
  // PROFILE PROPOSAL — human review gate for LLM inference
  // ============================================================

  // Field labels used in the modal headings — falls back to the field
  // name if not in the map so we never crash on new schema additions.
  const PROPOSAL_FIELD_LABELS = {
    name: 'Name',
    email: 'Email',
    phone: 'Phone',
    location: 'Location',
    target_titles: 'Target titles',
    target_keywords: 'Target keywords / skills',
    industries: 'Industries',
    years_experience: 'Years of experience',
    seniority_level: 'Seniority level (LLM)',
    seniority_targets: 'Seniority targets',
    key_achievements: 'Key achievements',
    preferred_locations: 'Preferred locations',
    linkedin_url: 'LinkedIn URL',
    github_url: 'GitHub URL',
    portfolio_url: 'Portfolio URL',
    currency: 'Currency',
  };

  // Local state for the open proposal modal.
  const proposalState = {
    proposalId: null,
    differences: {},
    deterministic: {},
    llm: {},
    llmRunId: null,
    pollTimer: null,
  };

  function fieldLabel(name) {
    return PROPOSAL_FIELD_LABELS[name] || name.replace(/_/g, ' ');
  }

  function renderProposalValue(value) {
    if (value == null || value === '') {
      return el('span', { class: 'empty', text: '— null —' });
    }
    if (Array.isArray(value)) {
      if (!value.length) return el('span', { class: 'empty', text: '— empty list —' });
      const ul = el('ul');
      for (const v of value) {
        ul.appendChild(el('li', { text: String(v) }));
      }
      return ul;
    }
    if (typeof value === 'object') {
      return el('code', { text: JSON.stringify(value) });
    }
    return el('span', { text: String(value) });
  }

  function valueIsList(d, l) {
    return Array.isArray(d) || Array.isArray(l);
  }

  function maybeOpenProposalGate(response) {
    if (!response || !response.proposal_id) return false;
    const diffs = response.differences || {};
    const diffKeys = Object.keys(diffs);
    if (!diffKeys.length) return false;
    openProposalModal({
      proposalId: response.proposal_id,
      differences: diffs,
      deterministic: response.deterministic || {},
      llm: response.llm || {},
      llmRunId: response.llm_run_id || null,
    });
    return true;
  }

  function openProposalModal(payload) {
    const modal = document.getElementById('proposal-modal');
    if (!modal) return;
    proposalState.proposalId = payload.proposalId;
    proposalState.differences = payload.differences || {};
    proposalState.deterministic = payload.deterministic || {};
    proposalState.llm = payload.llm || {};
    proposalState.llmRunId = payload.llmRunId || null;

    const diffKeys = Object.keys(proposalState.differences);
    const intro = document.getElementById('proposal-modal-intro');
    if (intro) {
      const n = diffKeys.length;
      intro.textContent = n
        ? `The LLM and the deterministic parser disagree on ${n} field${n === 1 ? '' : 's'}. Pick the value you trust for each, edit manually, or use one of the bulk shortcuts. Nothing is committed to your profile until you click ACCEPT SELECTED.`
        : 'No disagreements found — nothing to review.';
    }

    const body = document.getElementById('proposal-modal-body');
    if (body) {
      body.innerHTML = '';
      for (const field of diffKeys) {
        body.appendChild(renderProposalField(field, proposalState.differences[field]));
      }
    }
    modal.classList.remove('hidden');
  }

  function closeProposalModal() {
    const modal = document.getElementById('proposal-modal');
    if (modal) modal.classList.add('hidden');
    proposalState.proposalId = null;
    proposalState.differences = {};
    proposalState.deterministic = {};
    proposalState.llm = {};
    proposalState.llmRunId = null;
  }

  function renderProposalField(field, diff) {
    const wrap = el('div', { class: 'proposal-field', 'data-field': field });
    const head = el('div', { class: 'proposal-field-head' }, [
      el('h4', { text: fieldLabel(field) }),
    ]);
    if (proposalState.llmRunId) {
      const btn = el('button', {
        class: 'proposal-reasoning-btn',
        type: 'button',
        text: 'VIEW REASONING',
        onclick: () => openLLMRunModal(proposalState.llmRunId),
      });
      head.appendChild(btn);
    }
    wrap.appendChild(head);

    const opts = el('div', { class: 'proposal-field-options' });
    const detVal = diff.deterministic;
    const llmVal = diff.llm;
    const isList = valueIsList(detVal, llmVal);

    opts.appendChild(makeOption(field, 'deterministic', 'DETERMINISTIC', detVal));
    opts.appendChild(makeOption(field, 'llm', 'LLM', llmVal));
    opts.appendChild(makeOption(field, 'manual', 'EDIT MANUALLY', null, { isList, seed: llmVal != null && llmVal !== '' ? llmVal : detVal }));

    wrap.appendChild(opts);
    return wrap;
  }

  function makeOption(field, key, label, value, extra = {}) {
    const row = el('label', { class: 'proposal-opt', 'data-choice': key });
    const radio = el('input', { type: 'radio', name: `prop-${field}`, value: key });
    radio.addEventListener('change', () => {
      const parent = row.parentElement;
      if (parent) {
        for (const sib of parent.querySelectorAll('.proposal-opt')) {
          sib.classList.toggle('selected', sib === row);
        }
      }
    });
    // Default selection: LLM gets first dibs (it's usually the smarter
    // answer); if LLM value is null/absent, fall back to deterministic.
    const llmHasValue = (val) => val != null && !(Array.isArray(val) && !val.length) && val !== '';
    if (key === 'llm' && llmHasValue(proposalState.differences[field].llm)) {
      radio.checked = true;
      row.classList.add('selected');
    } else if (key === 'deterministic'
               && !llmHasValue(proposalState.differences[field].llm)
               && llmHasValue(proposalState.differences[field].deterministic)) {
      radio.checked = true;
      row.classList.add('selected');
    }
    row.appendChild(radio);
    row.appendChild(el('span', { class: 'proposal-opt-label', text: label }));

    const valBox = el('div', { class: 'proposal-opt-value' });
    if (key === 'manual') {
      const isList = !!extra.isList;
      const seed = extra.seed;
      let seedStr = '';
      if (Array.isArray(seed)) seedStr = seed.join(', ');
      else if (seed != null) seedStr = String(seed);
      const input = el(isList ? 'textarea' : 'input', {
        class: 'manual-input',
        rows: isList ? 2 : undefined,
        type: isList ? undefined : 'text',
        placeholder: isList ? 'comma-separated values…' : 'type a value…',
        'data-field': field,
        'data-manual': '1',
      });
      input.value = seedStr;
      input.addEventListener('focus', () => {
        radio.checked = true;
        row.classList.add('selected');
        const parent = row.parentElement;
        if (parent) {
          for (const sib of parent.querySelectorAll('.proposal-opt')) {
            if (sib !== row) sib.classList.remove('selected');
          }
        }
      });
      valBox.appendChild(input);
    } else {
      valBox.appendChild(renderProposalValue(value));
    }
    row.appendChild(valBox);
    return row;
  }

  function collectProposalChoices() {
    const body = document.getElementById('proposal-modal-body');
    if (!body) return {};
    const accepted = {};
    for (const fieldEl of body.querySelectorAll('.proposal-field')) {
      const field = fieldEl.getAttribute('data-field');
      const sel = fieldEl.querySelector(`input[name="prop-${field}"]:checked`);
      if (!sel) continue;
      const choice = sel.value;
      if (choice === 'deterministic' || choice === 'llm') {
        accepted[field] = choice;
      } else if (choice === 'manual') {
        const input = fieldEl.querySelector('.manual-input');
        if (!input) continue;
        let v = (input.value || '').trim();
        const det = proposalState.differences[field].deterministic;
        const llm = proposalState.differences[field].llm;
        const isList = Array.isArray(det) || Array.isArray(llm);
        if (isList) {
          accepted[field] = v ? v.split(',').map(s => s.trim()).filter(Boolean) : [];
        } else {
          accepted[field] = v;
        }
      }
    }
    return accepted;
  }

  async function submitProposalAcceptance(choicesOverride) {
    const pid = proposalState.proposalId;
    if (!pid) return;
    const accepted = choicesOverride || collectProposalChoices();
    if (!Object.keys(accepted).length) {
      toast('Select a value for at least one field, or use a bulk shortcut.', 'warn');
      return;
    }
    const r = await api.post(`/api/profile/proposals/${pid}/accept`,
      { accepted_fields: accepted });
    if (!r.ok) return;
    const data = r.data || {};
    toast(`Applied ${(data.applied_fields || []).length} field${(data.applied_fields || []).length === 1 ? '' : 's'} to your profile.`,
          'success');
    closeProposalModal();
    await loadProfile();
    refreshProposalsPills();
  }

  async function rejectProposal() {
    const pid = proposalState.proposalId;
    if (!pid) { closeProposalModal(); return; }
    const r = await api.post(`/api/profile/proposals/${pid}/reject`, {});
    if (r.ok) {
      toast('Proposal rejected — your profile is unchanged.', 'success');
    }
    closeProposalModal();
    refreshProposalsPills();
  }

  function bulkAccept(source) {
    const accepted = {};
    for (const field of Object.keys(proposalState.differences)) {
      accepted[field] = source;
    }
    submitProposalAcceptance(accepted);
  }

  function bindProfileProposalGate() {
    const modal = document.getElementById('proposal-modal');
    if (!modal) return;
    const closeBtn = document.getElementById('proposal-close');
    if (closeBtn) closeBtn.addEventListener('click', closeProposalModal);
    const accBtn = document.getElementById('proposal-accept-selected');
    if (accBtn) accBtn.addEventListener('click', () => submitProposalAcceptance());
    const llmBtn = document.getElementById('proposal-use-all-llm');
    if (llmBtn) llmBtn.addEventListener('click', () => bulkAccept('llm'));
    const detBtn = document.getElementById('proposal-use-all-det');
    if (detBtn) detBtn.addEventListener('click', () => bulkAccept('deterministic'));
    const rejBtn = document.getElementById('proposal-reject');
    if (rejBtn) rejBtn.addEventListener('click', rejectProposal);

    // Click on the backdrop closes (clicking the card does not bubble up
    // because of how flexbox + the form-actions sticky element work)
    modal.addEventListener('click', (e) => {
      if (e.target === modal) closeProposalModal();
    });

    // Sidebar pills + initial load
    refreshProposalsPills();
    startProposalsPoll();
  }

  async function refreshProposalsPills() {
    const wrap = document.getElementById('proposals-pills');
    const list = document.getElementById('proposals-pills-list');
    if (!wrap || !list) return;
    const r = await api.get('/api/profile/proposals?status=pending&limit=20', { silent: true });
    if (!r.ok) { wrap.hidden = true; return; }
    const items = (r.data && r.data.proposals) || [];
    if (!items.length) { wrap.hidden = true; list.innerHTML = ''; return; }
    wrap.hidden = false;
    list.innerHTML = '';
    for (const p of items) {
      const li = el('li');
      const btn = el('button', {
        class: 'proposal-pill',
        type: 'button',
        title: `Proposal #${p.id} from ${p.source} — ${new Date(p.created_at * 1000).toLocaleString()}`,
        text: `#${p.id} · ${p.source} · ${fmtRel(p.created_at)}`,
        onclick: () => openProposalById(p.id),
      });
      li.appendChild(btn);
      list.appendChild(li);
    }
  }

  async function openProposalById(pid) {
    const r = await api.get(`/api/profile/proposals/${pid}`, { silent: true });
    if (!r.ok) {
      toast(`Could not load proposal #${pid}.`, 'error');
      return;
    }
    const d = r.data || {};
    openProposalModal({
      proposalId: d.id,
      differences: d.differences || {},
      deterministic: d.deterministic || {},
      llm: d.llm || {},
      llmRunId: d.llm_run_id || null,
    });
  }

  function startProposalsPoll() {
    if (proposalState.pollTimer) return;
    // 10s cadence — matches the spec; doesn't pile up if tab is hidden
    // because fetch from a hidden tab still resolves but at low priority.
    proposalState.pollTimer = setInterval(() => {
      if (document.hidden) return;
      const setup = document.querySelector('section.page[data-page="setup"]');
      if (!setup || setup.classList.contains('hidden')) return;
      refreshProposalsPills();
    }, 10000);
  }

  // ============================================================
  // INIT
  // ============================================================
  // ============================================================
  // NOTIFICATIONS (v0.5) — topbar bell + dropdown, 60s poll
  // ============================================================
  let _notifPollHandle = null;

  async function refreshNotifications() {
    const r = await api.get('/api/notifications?unread_only=false', { silent: true });
    if (!r.ok) return;
    const items = r.data || [];
    const unread = r.unread_count != null ? r.unread_count : items.filter(n => !n.read).length;
    const countEl = $('#notif-count');
    const toggle = $('#notif-toggle');
    if (countEl) {
      countEl.textContent = String(unread);
      countEl.classList.toggle('hidden', !unread);
    }
    if (toggle) toggle.classList.toggle('pill-warn', unread > 0);
    const list = $('#notif-list');
    if (!list) return;
    list.innerHTML = '';
    if (!items.length) {
      list.appendChild(el('li', { class: 'notif-empty', text: 'No notifications.' }));
      return;
    }
    for (const n of items.slice(0, 30)) {
      const li = el('li', {
        class: 'notif-item' + (n.read ? '' : ' notif-unread'),
        role: 'menuitem', tabindex: '0',
        onclick: () => markNotificationRead(n.id),
      }, [
        el('div', { class: 'notif-title', text: safeText(n.title || '(untitled)') }),
        n.body ? el('div', { class: 'notif-body', text: safeText(n.body) }) : null,
        el('div', { class: 'notif-time muted small', text: fmtRel(n.ts) }),
      ].filter(Boolean));
      li.addEventListener('keydown', (e) => { if (e.key === 'Enter') markNotificationRead(n.id); });
      list.appendChild(li);
    }
  }

  async function markNotificationRead(id) {
    const r = await api.post('/api/notifications/' + id + '/read', {}, { silent: true });
    if (r.ok) refreshNotifications();
  }

  function bindNotifications() {
    const toggle = $('#notif-toggle');
    const dropdown = $('#notif-dropdown');
    if (!toggle || !dropdown) return;
    toggle.addEventListener('click', (e) => {
      e.stopPropagation();
      const open = dropdown.classList.toggle('hidden');
      toggle.setAttribute('aria-expanded', String(!open));
      if (!open) refreshNotifications();
    });
    document.addEventListener('click', (e) => {
      if (!dropdown.contains(e.target) && e.target !== toggle) {
        dropdown.classList.add('hidden');
        toggle.setAttribute('aria-expanded', 'false');
      }
    });
  }

  function startNotificationsPoll() {
    refreshNotifications();
    if (_notifPollHandle) clearInterval(_notifPollHandle);
    _notifPollHandle = setInterval(refreshNotifications, 60000);
  }

  // ============================================================
  // DEMO MODE (v0.5) — landing onboarding
  // ============================================================
  async function refreshDemoCta() {
    const cta = $('#demo-mode-cta');
    if (!cta) return;
    const status = await api.get('/api/vault/demo-status', { silent: true });
    const active = !!(status.ok && status.data && status.data.active);
    let vaultEmpty = true;
    if (!active) {
      const sum = await api.get('/api/vault/summary', { silent: true });
      if (sum.ok && sum.data) {
        const d = sum.data;
        vaultEmpty = !((d.claims_total || 0) > 0 || (d.sources_total || 0) > 0);
      }
    }
    // Show the CTA only when demo is active (offer removal) or vault is empty.
    cta.classList.toggle('hidden', !(active || vaultEmpty));
    const seedBtn = $('#demo-seed-btn');
    const removeBtn = $('#demo-remove-btn');
    const note = $('#demo-cta-note');
    if (seedBtn) seedBtn.classList.toggle('hidden', active);
    if (removeBtn) removeBtn.classList.toggle('hidden', !active);
    if (note) note.textContent = active
      ? 'Demo data is loaded. Remove it whenever you add your own evidence.'
      : 'Seeds a fictional profile + jobs so you can explore before adding your own. Fully reversible.';
  }

  function bindDemoMode() {
    const seedBtn = $('#demo-seed-btn');
    const removeBtn = $('#demo-remove-btn');
    if (seedBtn) seedBtn.addEventListener('click', async () => {
      seedBtn.disabled = true; seedBtn.textContent = 'SEEDING…';
      const r = await api.post('/api/vault/demo-seed', { confirm: true });
      seedBtn.disabled = false; seedBtn.textContent = 'TRY WITH DEMO DATA';
      if (r.ok) { toast('Demo data loaded.', 'success'); refreshDemoCta(); }
    });
    if (removeBtn) removeBtn.addEventListener('click', async () => {
      if (!confirm('Remove all demo data?')) return;
      removeBtn.disabled = true;
      const r = await api.del('/api/vault/demo-seed');
      removeBtn.disabled = false;
      if (r.ok) { toast('Demo data removed.', 'success'); refreshDemoCta(); }
    });
  }

  function init() {
    bindRouting();
    bindProfileForm();
    bindInferForm();
    bindEvidence();
    bindApiKeysForm();
    bindAutopilot();
    bindVault();
    bindSearch();
    bindResume();
    bindPipelineBoard();
    bindInbox();
    bindCalendar();
    bindIntelNetwork();
    bindOffers();
    bindInterview();
    bindSettings();
    bindProfileProposalGate();
    bindCareerSnapshot();
    bindBaseResume();
    bindDashboardSelection();
    bindJobsFilter();
    bindUrlIngestStatus();
    refreshWeightTotal();
    bootStatus();
    bindLLMActivity();
    startLLMActivityPoll();
    bindVaultQuickUpdate();
    bindSetupQuickIngestLinks();
    bindNotifications();
    startNotificationsPoll();
    bindDemoMode();

    // Explicit hash wins; otherwise resume wherever the user left off.
    let start = (location.hash || '').replace('#', '');
    if (!start) {
      try { start = localStorage.getItem('jhh.lastPage') || ''; } catch (_) {}
    }
    switchPage(PAGES.includes(start) ? start : 'landing');
    bindKeyboard();

    // populate availability grid even when calendar tab not yet visited
    renderAvailGrid();
  }
  // ============================================================
  // INTEL + NETWORK pages (v0.3 — headhunter-grade surfaces)
  // ============================================================
  async function loadIntel() {
    // Velocity funnel
    const fr = await api.get('/api/velocity/funnel', { silent: true });
    const f = (fr.ok && (fr.data || {})) || {};
    const fhost = $('#velocity-funnel');
    if (fhost) {
      fhost.innerHTML = '';
      const fields = [
        ['Prepared', f.prepared ?? 0], ['Applied', f.applied ?? 0],
        ['Replied', f.replied ?? 0], ['Screened', f.screened ?? 0],
        ['Interview', f.interviewed ?? f.interview ?? 0],
        ['Offered', f.offered ?? 0], ['Rejected', f.rejected ?? 0],
        ['Ghosted', f.ghosted ?? 0],
        ['Reply rate', ((f.reply_rate ?? 0) * 100).toFixed(0) + '%'],
        ['Interview rate', ((f.interview_rate ?? 0) * 100).toFixed(0) + '%'],
        ['Offer rate', ((f.offer_rate ?? 0) * 100).toFixed(0) + '%'],
      ];
      for (const [k, v] of fields) {
        fhost.appendChild(el('div', { class: 'kv-row' }, [
          el('span', { class: 'kv-key', text: k }),
          el('span', { class: 'kv-val', text: String(v) }),
        ]));
      }
    }
    // Bottleneck
    const br = await api.get('/api/velocity/bottleneck', { silent: true });
    if ($('#velocity-bottleneck')) {
      const d = (br.ok && br.data) || {};
      $('#velocity-bottleneck').textContent =
        d.diagnosis || d.summary || d.message || 'Not enough data yet — apply to ≥5 jobs first.';
    }
    // Gaps
    const gr = await api.get('/api/gaps/top?days=30&limit=10', { silent: true });
    const glist = $('#intel-gaps');
    if (glist) {
      glist.innerHTML = '';
      const arr = (gr.ok && (gr.data?.gaps || gr.data || [])) || [];
      if (!arr.length) {
        glist.appendChild(el('li', { class: 'muted', text: 'No gaps tracked yet.' }));
      } else {
        for (const g of arr) {
          glist.appendChild(el('li', {
            text: `${g.keyword} — ${g.mentions} job${g.mentions === 1 ? '' : 's'}`,
          }));
        }
      }
    }
    // Effectiveness
    const er = await api.get('/api/effectiveness/leaderboard?min_sent=1', { silent: true });
    const tb = $('#intel-effectiveness tbody');
    if (tb) {
      tb.innerHTML = '';
      const rows = (er.ok && (er.data || [])) || [];
      if (!rows.length) {
        tb.appendChild(el('tr', {}, el('td', { colspan: 5, class: 'empty',
          text: 'No effectiveness data yet — mark applications as replied/interview/offer in Pipeline.' })));
      } else {
        for (const r of rows) {
          tb.appendChild(el('tr', {}, [
            el('td', { text: `#${r.resume_id ?? '—'}` }),
            el('td', { text: String(r.sent ?? 0) }),
            el('td', { text: ((r.reply_rate ?? 0) * 100).toFixed(0) + '%' }),
            el('td', { text: ((r.interview_rate ?? 0) * 100).toFixed(0) + '%' }),
            el('td', { text: ((r.offer_rate ?? 0) * 100).toFixed(0) + '%' }),
          ]));
        }
      }
    }
    // Companies
    const cr = await api.get('/api/companies', { silent: true });
    const ctb = $('#intel-companies tbody');
    if (ctb) {
      ctb.innerHTML = '';
      const arr = (cr.ok && (cr.data || [])) || [];
      if (!arr.length) {
        ctb.appendChild(el('tr', {}, el('td', { colspan: 4, class: 'empty', text: 'No companies tracked yet.' })));
      } else {
        for (const c of arr.slice(0, 25)) {
          ctb.appendChild(el('tr', {}, [
            el('td', { text: safeText(c.company || c.name || '—') }),
            el('td', { text: String(c.jobs_seen ?? c.count ?? 0) }),
            el('td', { text: String(c.applications ?? 0) }),
            el('td', { text: safeText((c.outcomes || []).join(', ') || '—') }),
          ]));
        }
      }
    }
    // A/B by resume style
    const abr = await api.get('/api/effectiveness/ab', { silent: true });
    const abtb = $('#intel-ab tbody');
    if (abtb) {
      abtb.innerHTML = '';
      const styles = (abr.ok && abr.data && abr.data.styles) || [];
      if (!styles.length) {
        abtb.appendChild(el('tr', {}, el('td', { colspan: 8, class: 'empty',
          text: 'No A/B data yet — tailor in different styles and mark outcomes in Pipeline.' })));
      } else {
        const pct = (v) => ((v ?? 0) * 100).toFixed(0) + '%';
        for (const s of styles) {
          const tr = el('tr', { class: s.insufficient_data ? 'row-muted' : '',
            title: s.caveat || '' }, [
            el('td', {}, [
              document.createTextNode(safeText(s.style || '—')),
              ...(s.insufficient_data ? [el('span', { class: 'dup-badge', text: 'n<5' })] : []),
            ]),
            el('td', { text: String(s.sent ?? 0) }),
            el('td', { text: String(s.replied ?? 0) }),
            el('td', { text: String(s.interviewed ?? 0) }),
            el('td', { text: String(s.offered ?? 0) }),
            el('td', { text: pct(s.reply_rate) }),
            el('td', { text: pct(s.interview_rate) }),
            el('td', { text: pct(s.offer_rate) }),
          ]);
          abtb.appendChild(tr);
        }
      }
    }
  }

  async function loadNetwork() {
    loadReferralQuickPicks();
    const r = await api.get('/api/connections', { silent: true });
    const tb = $('#connections-table tbody');
    if (!tb) return;
    tb.innerHTML = '';
    const rows = (r.ok && (r.data || [])) || [];
    if (!rows.length) {
      tb.appendChild(el('tr', {}, el('td', { colspan: 6, class: 'empty', text: 'No connections yet — add one in the form above.' })));
      return;
    }
    for (const c of rows) {
      const delBtn = el('button', { class: 'btn btn-ghost small', type: 'button',
        onclick: async () => {
          if (!confirm(`Delete ${c.name}?`)) return;
          await api.del('/api/connections/' + c.id);
          loadNetwork();
        }
      }, 'DEL');
      tb.appendChild(el('tr', {}, [
        el('td', { text: safeText(c.name || '—') }),
        el('td', { text: safeText(c.company || '—') }),
        el('td', { text: safeText(c.role || '—') }),
        el('td', { text: safeText(c.relationship || '—') }),
        el('td', { text: safeText(c.contact || '—') }),
        el('td', {}, delBtn),
      ]));
    }
  }

  function bindIntelNetwork() {
    // Intel: salary form + refresh
    const sf = $('#intel-salary-form');
    if (sf) {
      $('#intel-salary-go').addEventListener('click', async (e) => {
        e.preventDefault();
        const role = sf.elements.namedItem('role').value.trim();
        if (!role) { toast('Enter a role first.', 'warn'); return; }
        const loc = sf.elements.namedItem('location').value.trim();
        const cur = sf.elements.namedItem('currency').value;
        $('#intel-salary-status').textContent = 'Loading…';
        const r = await api.get(`/api/salary/market?role=${encodeURIComponent(role)}` +
          (loc ? `&location=${encodeURIComponent(loc)}` : '') +
          `&currency=${cur}`, { silent: true });
        const out = $('#intel-salary-out');
        out.innerHTML = '';
        if (!r.ok) {
          $('#intel-salary-status').textContent = 'No data.';
          return;
        }
        const d = r.data || {};
        $('#intel-salary-status').textContent = `n=${d.count ?? 0}`;
        const fmt = (n) => n ? '$' + Number(n).toLocaleString() : '—';
        for (const [k, v] of [
          ['Count', d.count ?? 0], ['Currency', d.currency || cur],
          ['p25', fmt(d.p25)], ['Median', fmt(d.median)], ['p75', fmt(d.p75)], ['p90', fmt(d.p90)],
        ]) {
          out.appendChild(el('div', { class: 'kv-row' }, [
            el('span', { class: 'kv-key', text: k }),
            el('span', { class: 'kv-val', text: String(v) }),
          ]));
        }
      });
    }
    const refV = $('#intel-refresh-velocity');
    if (refV) refV.addEventListener('click', loadIntel);

    // Network: add connection form
    const cf = $('#connection-form');
    if (cf) {
      cf.addEventListener('submit', async (e) => {
        e.preventDefault();
        const data = serializeForm(cf);
        const r = await api.post('/api/connections', data);
        if (r.ok) {
          toast('Connection added.', 'success');
          cf.reset();
          loadNetwork();
        }
      });
    }
    // Network: refer-at lookup (v0.5 — ranked referral finder)
    const rf = $('#refer-form');
    if (rf) {
      rf.addEventListener('submit', async (e) => {
        e.preventDefault();
        const co = rf.elements.namedItem('company').value.trim();
        if (!co) return;
        await runReferralLookup(co);
      });
    }
    const refCo = $('#connections-refresh');
    if (refCo) refCo.addEventListener('click', loadNetwork);
  }

  const _MATCH_PILL = { current: 'badge-green', past: 'badge-blue', fuzzy: 'badge-warn', mention: 'badge-muted' };

  async function runReferralLookup(company) {
    const list = $('#refer-results');
    if (!list) return;
    list.innerHTML = '';
    const r = await api.get('/api/referrals?company=' + encodeURIComponent(company), { silent: true });
    const arr = (r.ok && (r.data || [])) || [];
    if (!arr.length) {
      list.appendChild(el('li', { class: 'muted', text: `No connections at ${company} (yet).` }));
      return;
    }
    for (const item of arr) {
      const c = item.connection || item;
      const kind = item.match_kind || 'fuzzy';
      const li = el('li', { class: 'refer-item' }, [
        el('div', { class: 'refer-line' }, [
          el('span', { class: 'badge ' + (_MATCH_PILL[kind] || 'badge-muted'), text: kind.toUpperCase() }),
          el('strong', { text: safeText(c.name || '—') }),
          el('span', { class: 'muted small', text: [c.role, item.matched_company || c.company].filter(Boolean).join(' · ') }),
          c.last_contacted_at ? el('span', { class: 'muted small', text: 'last contact ' + fmtRel(c.last_contacted_at) }) : null,
        ].filter(Boolean)),
      ]);
      if (item.suggested_message) {
        const ta = el('textarea', { class: 'refer-msg', readonly: 'readonly', rows: '3' });
        ta.value = item.suggested_message;
        const copyBtn = el('button', { class: 'btn btn-ghost small', type: 'button',
          onclick: () => { ta.select(); navigator.clipboard && navigator.clipboard.writeText(ta.value); toast('Message copied.', 'success'); } }, 'COPY');
        li.appendChild(el('div', { class: 'refer-msg-row' }, [ta, copyBtn]));
      }
      list.appendChild(li);
    }
  }

  async function loadReferralQuickPicks() {
    const host = $('#refer-companies');
    if (!host) return;
    const r = await api.get('/api/referrals/companies-with-connections', { silent: true });
    host.innerHTML = '';
    const arr = (r.ok && (r.data || [])) || [];
    if (!arr.length) { host.classList.add('hidden'); return; }
    host.classList.remove('hidden');
    host.appendChild(el('span', { class: 'muted small', text: 'Companies with a connection: ' }));
    for (const c of arr.slice(0, 12)) {
      const name = c.company || c.name;
      if (!name) continue;
      host.appendChild(el('button', {
        class: 'chip', type: 'button',
        title: `${c.connection_count || 1} connection(s)`,
        onclick: () => {
          const inp = $('#refer-form') && $('#refer-form').elements.namedItem('company');
          if (inp) inp.value = name;
          runReferralLookup(name);
        },
      }, `${name}${c.connection_count ? ' (' + c.connection_count + ')' : ''}`));
    }
  }

  // ============================================================
  // OFFERS — LLM offer analysis + comparison
  // ============================================================
  const offerState = {
    list: [],
    selected: new Set(),
  };

  function bindOffers() {
    const form = $('#offer-form');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      await submitOfferAnalysis();
    });
    const refresh = $('#offers-refresh');
    if (refresh) refresh.addEventListener('click', () => loadOffersList());
    const compareBtn = $('#offers-compare-btn');
    if (compareBtn) compareBtn.addEventListener('click', () => runCompareSelected());
    const closeBtn = $('#offer-detail-close');
    if (closeBtn) closeBtn.addEventListener('click', () => {
      const card = $('#offer-detail-card');
      if (card) card.classList.add('hidden');
    });
    const closeCmpBtn = $('#offer-compare-close');
    if (closeCmpBtn) closeCmpBtn.addEventListener('click', () => {
      const card = $('#offer-compare-card');
      if (card) card.classList.add('hidden');
    });
  }

  async function loadOffers() {
    await loadOfferAppOptions();
    await loadOffersList();
    // If pipeline asked to pre-fill, apply now.
    if (state._offerPrefill) {
      const sel = $('#offer-app-select');
      if (sel) sel.value = String(state._offerPrefill);
      state._offerPrefill = null;
    }
  }

  async function loadOfferAppOptions() {
    const sel = $('#offer-app-select');
    if (!sel) return;
    const r = await api.get('/api/applications/board', { silent: true });
    const board = (r.ok && r.data) || {};
    const apps = Object.values(board).flat();
    sel.innerHTML = '';
    sel.appendChild(el('option', { value: '' }, 'No application — analyze offer in isolation'));
    for (const a of apps) {
      const label = `#${a.id} · ${a.title || 'application'} @ ${a.company || ''} [${a.status || '?'}]`;
      sel.appendChild(el('option', { value: String(a.id) }, label));
    }
  }

  async function loadOffersList() {
    const host = $('#offers-list');
    if (!host) return;
    host.innerHTML = '<p class="muted small">Loading…</p>';
    const r = await api.get('/api/offers?limit=50', { silent: true });
    if (!r.ok) {
      host.innerHTML = `<p class="ap-error">Could not load: ${r.error || 'unknown'}</p>`;
      return;
    }
    const rows = (r.data || []);
    offerState.list = rows;
    offerState.selected = new Set();
    updateCompareBtnState();
    if (!rows.length) {
      host.innerHTML = '<p class="muted">No analyses yet. Paste an offer above to get started.</p>';
      return;
    }
    host.innerHTML = '';
    for (const a of rows) {
      host.appendChild(renderOfferCard(a));
    }
  }

  function updateCompareBtnState() {
    const btn = $('#offers-compare-btn');
    if (!btn) return;
    btn.disabled = offerState.selected.size < 2;
  }

  function renderOfferCard(a) {
    const recColor = recommendationPillClass(a.recommendation);
    const score = a.total_score != null ? Math.round(Number(a.total_score)) : '—';
    const company = safeText(a.company || '(no application)');
    const title = safeText(a.title || 'Offer');
    const when = fmtRel(a.created_at);
    const card = el('div', { class: 'card offer-card', style: 'padding:var(--s-3);' });
    const head = el('div', { class: 'kpi-row', style: 'justify-content:space-between;align-items:flex-start;gap:var(--s-3);flex-wrap:wrap;' });
    const left = el('div', { class: 'stack', style: 'flex:1;min-width:280px;' });
    const titleLine = el('div', {}, [
      el('label', { style: 'display:inline-flex;align-items:center;gap:6px;cursor:pointer;' }, [
        el('input', {
          type: 'checkbox',
          'data-offer-id': String(a.id),
          onchange: (e) => {
            if (e.target.checked) offerState.selected.add(a.id);
            else offerState.selected.delete(a.id);
            updateCompareBtnState();
          },
        }),
        el('strong', { text: `${company} · ${title}` }),
      ]),
    ]);
    left.appendChild(titleLine);
    left.appendChild(el('div', { class: 'muted small', text: `${when} ago · analysis #${a.id}` }));
    const kpis = el('div', { class: 'kpi-row', style: 'gap:var(--s-3);' }, [
      el('span', { class: 'ap-kpi' }, [el('strong', { text: `SCORE ${score}` })]),
      el('span', { class: 'pill ' + recColor, text: (a.recommendation || 'negotiate').toUpperCase().replace('_', ' ') }),
    ]);
    left.appendChild(kpis);
    const actions = el('div', { class: 'form-actions', style: 'margin:0;flex-wrap:wrap;' }, [
      el('button', { class: 'btn btn-secondary small', type: 'button', onclick: () => openOfferDetail(a.id) }, 'VIEW'),
      el('button', { class: 'btn btn-ghost small', type: 'button', onclick: () => deleteOffer(a.id) }, 'DELETE'),
    ]);
    if (a.llm_run_id) {
      actions.appendChild(
        el('button', { class: 'btn btn-ghost small', type: 'button', onclick: () => openLLMRunModal(a.llm_run_id) }, 'VIEW LLM REASONING')
      );
    }
    head.appendChild(left);
    head.appendChild(actions);
    card.appendChild(head);
    return card;
  }

  function recommendationPillClass(rec) {
    const r = (rec || '').toLowerCase();
    if (r === 'accept') return 'pill-green';
    if (r === 'walk' || r === 'counter_hard') return 'pill-red';
    if (r === 'negotiate') return 'pill-warn';
    return 'pill-muted';
  }

  async function deleteOffer(id) {
    if (!confirm('Delete this offer analysis?')) return;
    try {
      const r = await fetch('/api/offers/' + id, { method: 'DELETE' });
      if (r.ok) {
        toast('Deleted.', 'success');
      } else {
        toast('Delete not supported on server — analysis remains.', 'warn');
      }
    } catch (e) {
      toast('Delete failed: ' + e.message, 'error');
    }
    loadOffersList();
  }

  async function submitOfferAnalysis() {
    const sel = $('#offer-app-select');
    const txt = $('#offer-text');
    const btn = $('#offer-analyze-btn');
    const status = $('#offer-status');
    if (!txt || !(txt.value || '').trim()) {
      toast('Paste the offer text first.', 'warn');
      return;
    }
    const body = { offer_text: txt.value.trim() };
    const appId = (sel && sel.value) ? Number(sel.value) : null;
    if (appId) body.application_id = appId;

    if (btn) btn.disabled = true;
    if (status) status.textContent = 'Analyzing with LLM — this may take 30–90s on a local model…';
    const r = await api.post('/api/offers/analyze', body);
    if (btn) btn.disabled = false;
    if (status) status.textContent = '';
    if (!r.ok) return;
    toast('Analysis complete.', 'success');
    txt.value = '';
    await loadOffersList();
    if (r.data && r.data.id) openOfferDetail(r.data.id, r.data);
  }

  async function openOfferDetail(id, prefetched) {
    const card = $('#offer-detail-card');
    const body = $('#offer-detail-body');
    const ttl = $('#offer-detail-title');
    if (!card || !body) return;
    card.classList.remove('hidden');
    body.innerHTML = '<p class="muted small">Loading…</p>';

    let data = prefetched;
    if (!data) {
      // We don't have a single-by-id endpoint; pull from list state, or hit by app.
      const found = (offerState.list || []).find(x => Number(x.id) === Number(id));
      if (found) data = found;
    }
    if (!data) {
      // Fallback — refresh and try again.
      await loadOffersList();
      data = (offerState.list || []).find(x => Number(x.id) === Number(id));
    }
    if (!data) {
      body.innerHTML = '<p class="ap-error">Could not load analysis.</p>';
      return;
    }
    if (ttl) ttl.textContent = `${data.company || '(no application)'} · ${data.title || 'Offer'} — Analysis #${data.id}`;
    body.innerHTML = '';
    body.appendChild(renderOfferDetail(data));
    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function renderOfferDetail(a) {
    const wrap = el('div', { class: 'stack' });

    // Recommendation + score banner
    const score = a.total_score != null ? Math.round(Number(a.total_score)) : '—';
    wrap.appendChild(el('div', { class: 'kpi-row', style: 'gap:var(--s-4);flex-wrap:wrap;align-items:center;' }, [
      el('div', { class: 'ap-kpi', style: 'font-size:var(--s-5);' }, [
        el('strong', { text: 'TOTAL SCORE: ' + score + ' / 100' }),
      ]),
      el('span', { class: 'pill ' + recommendationPillClass(a.recommendation), style: 'font-size:14px;' },
        'RECOMMENDATION: ' + ((a.recommendation || 'negotiate').toUpperCase().replace('_', ' '))),
      a.llm_run_id ? el('button', { class: 'btn btn-ghost small', type: 'button', onclick: () => openLLMRunModal(a.llm_run_id) }, 'VIEW LLM REASONING') : null,
    ].filter(Boolean)));

    // COMPONENTS
    const comp = a.components || {};
    const compCard = el('section', { class: 'card' }, [
      el('header', { class: 'card-head' }, el('h3', { text: 'COMPENSATION COMPONENTS' })),
      renderComponentsGrid(comp),
    ]);
    wrap.appendChild(compCard);

    // MARKET COMPARISON
    const mkt = a.market_comparison || {};
    wrap.appendChild(el('section', { class: 'card' }, [
      el('header', { class: 'card-head' }, el('h3', { text: 'MARKET COMPARISON' })),
      renderMarketBlock(mkt),
    ]));

    // COUNTER SCRIPT
    const counters = a.counter_script || [];
    wrap.appendChild(el('section', { class: 'card' }, [
      el('header', { class: 'card-head' }, [
        el('h3', { text: 'COUNTER SCRIPT — 3 ANGLES' }),
        el('span', { class: 'muted small', text: 'click to expand · text is copyable' }),
      ]),
      renderCounters(counters),
    ]));

    // RED FLAGS
    const flags = a.red_flags || [];
    wrap.appendChild(el('section', { class: 'card' }, [
      el('header', { class: 'card-head' }, el('h3', { text: 'RED FLAGS' })),
      renderRedFlags(flags),
    ]));

    // EQUITY
    const eq = a.equity_analysis || {};
    wrap.appendChild(el('section', { class: 'card' }, [
      el('header', { class: 'card-head' }, el('h3', { text: 'EQUITY ANALYSIS' })),
      renderEquity(eq),
    ]));

    return wrap;
  }

  function renderComponentsGrid(comp) {
    const fields = [
      ['Base salary', comp.base_salary],
      ['Bonus', comp.bonus],
      ['Equity', comp.equity],
      ['Sign-on', comp.sign_on],
      ['Total comp (est.)', comp.total_compensation_estimate],
    ];
    const grid = el('div', { class: 'three-col' });
    for (const [label, v] of fields) {
      const valTxt = (v && v.value_text) || 'unknown';
      const conf = (v && v.confidence) || 'unknown';
      grid.appendChild(el('div', { class: 'kv-row', style: 'flex-direction:column;align-items:flex-start;gap:4px;' }, [
        el('span', { class: 'kv-key small muted', text: label.toUpperCase() }),
        el('span', { class: 'kv-val', style: 'font-family:var(--mono);font-size:13px;', text: safeText(String(valTxt)) }),
        el('span', { class: 'pill ' + confPillClass(conf), style: 'font-size:10px;', text: conf }),
      ]));
    }
    const benefits = (comp.benefits || []);
    if (benefits.length) {
      const list = el('ul', { class: 'bullets' });
      for (const b of benefits) list.appendChild(el('li', { text: safeText(String(b)) }));
      grid.appendChild(el('div', { style: 'grid-column:1 / -1;' }, [
        el('span', { class: 'kv-key small muted', text: 'BENEFITS' }),
        list,
      ]));
    }
    return grid;
  }

  function confPillClass(conf) {
    const c = (conf || '').toLowerCase();
    if (c === 'stated') return 'pill-green';
    if (c === 'estimated') return 'pill-warn';
    return 'pill-muted';
  }

  function renderMarketBlock(mkt) {
    const wrap = el('div', { class: 'stack' });
    const pct = safeText(String(mkt.percentile_estimate || 'unknown'));
    wrap.appendChild(el('p', {}, [el('strong', { text: 'Percentile estimate: ' }), pct]));
    const lo = numOrNull(mkt.market_low);
    const mid = numOrNull(mkt.market_mid);
    const hi = numOrNull(mkt.market_high);
    if (lo != null || mid != null || hi != null) {
      const bar = el('div', { style: 'display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:12px;' }, [
        el('span', { text: lo != null ? '$' + lo.toLocaleString() : '—' }),
        el('div', { style: 'flex:1;height:14px;background:var(--card-2);border:2px solid var(--ink);position:relative;' }, [
          mid != null && lo != null && hi != null && hi > lo
            ? el('div', {
                style: `position:absolute;top:-4px;width:4px;height:22px;background:var(--accent);left:${Math.max(0, Math.min(100, ((mid - lo) / (hi - lo)) * 100))}%;`,
              }) : null,
        ].filter(Boolean)),
        el('span', { text: hi != null ? '$' + hi.toLocaleString() : '—' }),
      ]);
      wrap.appendChild(bar);
      if (mid != null) wrap.appendChild(el('p', { class: 'muted small', text: 'Market midpoint: $' + mid.toLocaleString() }));
    }
    const factors = mkt.leverage_factors || [];
    if (factors.length) {
      wrap.appendChild(el('p', {}, el('strong', { text: 'Leverage factors:' })));
      const ul = el('ul', { class: 'bullets' });
      for (const f of factors) ul.appendChild(el('li', { text: safeText(String(f)) }));
      wrap.appendChild(ul);
    }
    return wrap;
  }

  function numOrNull(v) {
    if (v == null) return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  function renderCounters(counters) {
    const wrap = el('div', { class: 'stack' });
    if (!counters.length) {
      wrap.appendChild(el('p', { class: 'muted', text: 'No counter angles returned.' }));
      return wrap;
    }
    counters.forEach((c, i) => {
      const det = el('details', { class: 'card', style: 'padding:var(--s-3);', open: i === 0 });
      det.appendChild(el('summary', { style: 'cursor:pointer;font-weight:700;' },
        `ANGLE ${i + 1}: ${safeText(String(c.angle_name || 'unnamed'))}`));
      const pitch = (c.pitch || '').toString();
      const ask = (c.suggested_ask || '').toString();
      const ev = (c.evidence_basis || []).join(', ');
      det.appendChild(el('p', { class: 'small muted', text: 'Suggested ask: ' + safeText(ask) }));
      det.appendChild(el('pre', { class: 'codeblock', style: 'white-space:pre-wrap;' }, el('code', { text: pitch })));
      const copyBtn = el('button', { class: 'btn btn-ghost small', type: 'button',
        onclick: () => {
          navigator.clipboard.writeText(pitch).then(() => toast('Copied pitch to clipboard.', 'success'),
            () => toast('Could not copy.', 'warn'));
        }
      }, 'COPY PITCH');
      const actions = el('div', { class: 'form-actions', style: 'margin:0;' }, [copyBtn]);
      det.appendChild(actions);
      if (ev) det.appendChild(el('p', { class: 'muted small', text: 'Evidence basis: claim ' + safeText(ev) }));
      wrap.appendChild(det);
    });
    return wrap;
  }

  function renderRedFlags(flags) {
    if (!flags.length) {
      return el('p', { class: 'muted', text: 'No red flags identified.' });
    }
    const ul = el('ul', { class: 'stack', style: 'list-style:none;padding:0;' });
    for (const f of flags) {
      const sev = (f.severity || 'medium').toLowerCase();
      const pill = sev === 'high' ? 'pill-red' : (sev === 'low' ? 'pill-muted' : 'pill-warn');
      ul.appendChild(el('li', { class: 'card', style: 'padding:var(--s-3);' }, [
        el('div', { class: 'kpi-row', style: 'gap:var(--s-3);align-items:center;flex-wrap:wrap;' }, [
          el('span', { class: 'pill ' + pill, text: sev.toUpperCase() }),
          el('strong', { text: safeText(String(f.flag || '')) }),
        ]),
        el('p', { class: 'small', text: safeText(String(f.explanation || '')) }),
      ]));
    }
    return ul;
  }

  function renderEquity(eq) {
    const fields = [
      ['Strike price', eq.strike_price],
      ['Vesting cliff', eq.vesting_cliff],
      ['Vesting schedule', eq.vesting_schedule],
      ['FDV stated', eq.fdv_stated],
      ['Dilution risk', eq.dilution_risk],
    ];
    const dl = el('dl', { style: 'display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;font-family:var(--mono);font-size:13px;' });
    for (const [k, v] of fields) {
      dl.appendChild(el('dt', { class: 'muted small', text: k.toUpperCase() }));
      dl.appendChild(el('dd', { style: 'margin:0;', text: safeText(String(v || 'unknown')) }));
    }
    const wrap = el('div', { class: 'stack' });
    wrap.appendChild(dl);
    if (eq.notes) wrap.appendChild(el('p', { class: 'small', text: safeText(String(eq.notes)) }));
    return wrap;
  }

  async function runCompareSelected() {
    const ids = Array.from(offerState.selected);
    if (ids.length < 2) {
      toast('Select at least 2 analyses to compare.', 'warn');
      return;
    }
    const btn = $('#offers-compare-btn');
    if (btn) btn.disabled = true;
    const r = await api.post('/api/offers/compare', { analysis_ids: ids });
    if (btn) btn.disabled = false;
    if (!r.ok) return;
    renderCompareCard(r.data || {});
  }

  function renderCompareCard(data) {
    const card = $('#offer-compare-card');
    const body = $('#offer-compare-body');
    if (!card || !body) return;
    card.classList.remove('hidden');
    body.innerHTML = '';
    const cmp = data.comparison || {};
    const scorecard = cmp.scorecard || [];
    if (scorecard.length) {
      const tbl = el('table', { class: 'data-table' }, [
        el('thead', {}, el('tr', {}, [
          el('th', { text: 'ID' }),
          el('th', { text: 'COMPANY' }),
          el('th', { text: 'TITLE' }),
          el('th', { text: 'COMP' }),
          el('th', { text: 'GROWTH' }),
          el('th', { text: 'RISK' }),
          el('th', { text: 'FIT' }),
          el('th', { text: 'OVERALL' }),
          el('th', { text: 'HEADLINE' }),
        ])),
        el('tbody', {}, scorecard.map(row => el('tr', {}, [
          el('td', { text: '#' + (row.analysis_id ?? '?') }),
          el('td', { text: safeText(String(row.company || '')) }),
          el('td', { text: safeText(String(row.title || '')) }),
          el('td', { text: String(row.comp_score ?? '—') }),
          el('td', { text: String(row.growth_score ?? '—') }),
          el('td', { text: String(row.risk_score ?? '—') }),
          el('td', { text: String(row.fit_score ?? '—') }),
          el('td', {}, el('strong', { text: String(row.overall ?? '—') })),
          el('td', { text: safeText(String(row.headline || '')) }),
        ]))),
      ]);
      body.appendChild(el('div', { class: 'table-wrap' }, tbl));
    }
    const regret = cmp.regret_minimization || {};
    if (regret.reasoning || regret.least_regret_12mo) {
      body.appendChild(el('h4', { text: 'REGRET MINIMIZATION (12 months out)' }));
      if (regret.least_regret_12mo != null) {
        body.appendChild(el('p', {}, [el('strong', { text: 'Least-regret offer: #' + regret.least_regret_12mo })]));
      }
      if (regret.reasoning) body.appendChild(el('p', { text: safeText(String(regret.reasoning)) }));
    }
    if (cmp.recommendation) {
      body.appendChild(el('p', {}, [el('strong', { text: 'Recommendation: ' }), safeText(String(cmp.recommendation))]));
    }
    if (cmp.reasoning) {
      body.appendChild(el('p', { class: 'small', text: safeText(String(cmp.reasoning)) }));
    }
    if (data.llm_run_id) {
      body.appendChild(el('div', { class: 'form-actions', style: 'margin-top:var(--s-3);' }, [
        el('button', { class: 'btn btn-ghost small', type: 'button', onclick: () => openLLMRunModal(data.llm_run_id) }, 'VIEW LLM REASONING'),
      ]));
    }
    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  // Public helper so the pipeline can pre-fill the offers tab.
  window.openOffersForApp = function (appId) {
    state._offerPrefill = appId;
    switchPage('offers');
  };

  // ============================================================
  // LLM ACTIVITY PANEL — live view of what the LLM is doing
  // ============================================================
  const LLM_STAGE_LABELS = {
    'llm_test': 'Provider test',
    'profile_inference': 'Profile inference',
    'profile_proposal': 'Profile proposal',
    'vault_reingest': 'Vault re-ingest',
    'evidence_extraction': 'Evidence extraction',
    'llm_rerank': 'Job scoring rerank',
    'resume_tailor': 'Resume tailoring',
    'cover_letter': 'Cover letter',
    'interview_prep': 'Interview prep',
    'interview_practice': 'Interview practice',
    'offer_analysis': 'Offer analysis',
  };
  let _llmPollHandle = null;
  let _llmLastSeenId = 0;

  function bindLLMActivity() {
    const toggle = document.getElementById('llm-activity-toggle');
    const panel = document.getElementById('llm-activity-panel');
    const closer = document.getElementById('llm-activity-close');
    if (!toggle || !panel) return;
    toggle.addEventListener('click', () => {
      const open = panel.classList.toggle('hidden');
      toggle.setAttribute('aria-expanded', String(!open));
      if (!open) refreshLLMActivity({ resetSeen: false, force: true });
    });
    if (closer) closer.addEventListener('click', () => {
      panel.classList.add('hidden');
      toggle.setAttribute('aria-expanded', 'false');
    });
    // Delegated click on a list item opens the run-detail modal
    const list = document.getElementById('llm-activity-list');
    if (list) list.addEventListener('click', (e) => {
      const item = e.target.closest('[data-run-id]');
      if (!item) return;
      openLLMRunModal(Number(item.getAttribute('data-run-id')));
    });
    const modalClose = document.getElementById('llm-run-modal-close');
    if (modalClose) modalClose.addEventListener('click', () => {
      document.getElementById('llm-run-modal').classList.add('hidden');
    });
    const modal = document.getElementById('llm-run-modal');
    if (modal) modal.addEventListener('click', (e) => {
      if (e.target === modal) modal.classList.add('hidden');
    });
  }

  function startLLMActivityPoll() {
    refreshLLMActivity({ resetSeen: true, force: true });
    if (_llmPollHandle) clearInterval(_llmPollHandle);
    _llmPollHandle = setInterval(() => refreshLLMActivity({ resetSeen: false, force: false }), 2000);
  }

  async function refreshLLMActivity({ resetSeen, force } = {}) {
    const r = await api.get('/api/llm/runs?limit=30', { silent: true });
    if (!r.ok) return;
    const d = r.data || {};
    const runs = d.runs || [];
    const active = d.active || 0;

    // Update topbar pill
    const pill = document.getElementById('llm-activity-toggle');
    const dot = pill ? pill.querySelector('.llm-act-dot') : null;
    const label = document.getElementById('llm-activity-label');
    if (label) {
      if (active > 0) label.textContent = `LLM RUNNING · ${active}`;
      else if (runs.length) label.textContent = `LLM ${runs[0].status.toUpperCase()}`;
      else label.textContent = 'LLM IDLE';
    }
    if (pill) {
      pill.classList.toggle('pill-llm-active', active > 0);
      pill.classList.toggle('pill-llm-error', !active && runs[0] && runs[0].status === 'error');
    }
    if (dot) dot.classList.toggle('pulse', active > 0);

    // Toast on newly-finished runs (when panel is closed) so the user sees
    // success/failure even if they don't open the panel.
    const panelOpen = !document.getElementById('llm-activity-panel').classList.contains('hidden');
    if (resetSeen) {
      _llmLastSeenId = runs.length ? runs[0].id : 0;
    } else if (!panelOpen) {
      for (const run of runs) {
        if (run.id <= _llmLastSeenId) break;
        if (run.status === 'error') {
          toast(`LLM ${stageLabel(run.stage)} failed: ${(run.error || '').slice(0, 80)}`, 'error');
        }
      }
      if (runs.length) _llmLastSeenId = Math.max(_llmLastSeenId, runs[0].id);
    } else {
      if (runs.length) _llmLastSeenId = Math.max(_llmLastSeenId, runs[0].id);
    }

    // Update summary line
    const summary = document.getElementById('llm-act-summary');
    if (summary) summary.textContent = active ? `${active} running · last ${runs.length} shown` : `${runs.length} recent runs`;

    // Re-render list if open OR forced
    if (!panelOpen && !force) return;
    const list = document.getElementById('llm-activity-list');
    if (!list) return;
    list.innerHTML = '';
    if (!runs.length) {
      list.appendChild(el('li', { class: 'muted small', text: 'No LLM runs yet. Trigger Autopilot or any LLM-powered action to see live activity here.' }));
      return;
    }
    for (const r of runs) {
      const li = document.createElement('li');
      li.className = `llm-act-item llm-act-${r.status}`;
      li.setAttribute('data-run-id', r.id);
      li.setAttribute('tabindex', '0');
      const ts = new Date(r.ts * 1000);
      const when = ts.toLocaleTimeString();
      const elapsed = r.elapsed_ms != null
        ? `${(r.elapsed_ms / 1000).toFixed(1)}s`
        : (r.status === 'running' ? '…' : '?');
      li.innerHTML = `
        <span class="llm-act-stage">${stageLabel(r.stage)}</span>
        <span class="llm-act-meta muted small">${r.provider || ''}${r.model ? ' · ' + r.model.split(':').slice(0,2).join(':') : ''}</span>
        <span class="llm-act-time muted small">${when}</span>
        <span class="llm-act-status llm-act-status-${r.status}">${r.status.toUpperCase()}</span>
        <span class="llm-act-elapsed muted small">${elapsed}</span>
      `;
      list.appendChild(li);
    }
  }

  function stageLabel(stage) {
    return LLM_STAGE_LABELS[stage] || stage || 'LLM call';
  }

  async function openLLMRunModal(runId) {
    const modal = document.getElementById('llm-run-modal');
    const body = document.getElementById('llm-run-detail');
    if (!modal || !body) return;
    body.innerHTML = `<p class="muted small">Loading run #${runId}…</p>`;
    modal.classList.remove('hidden');
    const r = await api.get(`/api/llm/runs/${runId}`, { silent: true });
    if (!r.ok) {
      body.innerHTML = `<p class="ap-error">Could not load run: ${r.error || 'unknown'}</p>`;
      return;
    }
    const d = r.data || {};
    const status = (d.status || '').toUpperCase();
    const elapsed = d.elapsed_ms != null ? `${(d.elapsed_ms / 1000).toFixed(2)}s` : '?';
    const esc = (s) => (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
    body.innerHTML = `
      <div class="ap-kpis">
        <span class="ap-kpi"><strong>${stageLabel(d.stage)}</strong></span>
        <span class="ap-kpi"><strong>${d.provider || ''}</strong></span>
        <span class="ap-kpi"><strong>${(d.model || '').split(':').slice(0,2).join(':')}</strong></span>
        <span class="ap-kpi"><strong>${elapsed}</strong></span>
        <span class="ap-kpi llm-act-status-${d.status}"><strong>${status}</strong></span>
      </div>
      ${d.error ? `<div class="ap-error">${esc(d.error)}</div>` : ''}
      <h4>SYSTEM PROMPT</h4>
      <pre class="codeblock"><code>${esc(d.system_text)}</code></pre>
      <h4>USER PROMPT</h4>
      <pre class="codeblock"><code>${esc(d.user_text)}</code></pre>
      <h4>OUTPUT</h4>
      <pre class="codeblock"><code>${esc(d.output_text)}</code></pre>
      <p class="muted small">Run #${d.id} · ${new Date(d.ts * 1000).toLocaleString()}</p>
    `;
  }

  // ============================================================
  // VAULT QUICK-UPDATE (always-on widget)
  // ============================================================
  // Pulls the SETUP page's saved URL values into the modal's URL inputs so
  // the user sees what's currently on file before pushing a change.
  async function loadVaultUpdateState() {
    const r = await api.get('/api/profile', { silent: true });
    const p = (r.ok && r.data) || {};
    const li = document.getElementById('vu-linkedin');
    const gh = document.getElementById('vu-github');
    const pf = document.getElementById('vu-portfolio');
    if (li) li.value = p.linkedin_url || '';
    if (gh) gh.value = p.github_url || '';
    if (pf) pf.value = p.portfolio_url || '';
    // Render the existing-sources list with claim counts + per-row buttons
    const s = await api.get('/api/vault/sources-with-claim-counts', { silent: true });
    const host = document.getElementById('vu-sources-list');
    if (!host) return;
    const sources = (s.ok && s.data && s.data.sources) || [];
    if (!sources.length) {
      host.innerHTML = '<p class="muted small">No evidence sources yet — add a URL or paste above.</p>';
      return;
    }
    host.innerHTML = '';
    for (const src of sources) {
      const row = el('div', { class: 'vu-source-row', 'data-source-id': src.id });
      const left = el('div', { class: 'vu-source-meta' }, [
        el('div', { class: 'vu-source-title', text: src.title || src.filename || src.url || `Source #${src.id}` }),
        el('div', { class: 'muted small' }, [
          el('span', { text: `#${src.id} · ` }),
          el('strong', { text: (src.source_type || '—').toUpperCase() }),
          el('span', { text: ` · ${src.claim_count != null ? src.claim_count : 0} claim${src.claim_count === 1 ? '' : 's'}` }),
        ]),
      ]);
      const right = el('div', { class: 'vu-source-actions' }, [
        el('span', { class: 'vu-source-status muted small', text: '' }),
        el('button', {
          class: 'btn btn-ghost small', type: 'button',
          onclick: () => reingestOneSourceUI(src.id, row),
        }, 'RE-INGEST'),
      ]);
      row.appendChild(left);
      row.appendChild(right);
      host.appendChild(row);
    }
  }

  async function reingestOneSourceUI(sourceId, rowEl) {
    const btn = rowEl && rowEl.querySelector('button');
    const status = rowEl && rowEl.querySelector('.vu-source-status');
    if (btn) { btn.disabled = true; btn.textContent = 'RUNNING…'; }
    if (status) status.textContent = 'LLM running…';
    const r = await api.post(`/api/vault/sources/${sourceId}/reingest`, {}, { silent: true });
    if (btn) { btn.disabled = false; btn.textContent = 'RE-INGEST'; }
    if (!r.ok) {
      if (status) status.textContent = `Failed: ${r.error || 'unknown'}`;
      toast(`Re-ingest #${sourceId}: ${r.error || 'failed'}`, 'error');
      return;
    }
    const d = (r.data && r.data.data) || r.data || {};
    const oldN = d.claims_old_count ?? 0;
    const newN = d.claims_inserted ?? 0;
    const dropped = d.claims_dropped_unverified ?? 0;
    if (status) {
      status.innerHTML = `${oldN} &rarr; ${newN} claims · ${dropped} dropped${
        d.llm_run_id ? ` · <a href="#" data-llm-run="${d.llm_run_id}">VIEW LLM REASONING</a>` : ''
      }`;
      const link = status.querySelector('[data-llm-run]');
      if (link) link.addEventListener('click', (ev) => {
        ev.preventDefault();
        openLLMRunModal(Number(link.getAttribute('data-llm-run')));
      });
    }
    toast(`Source #${sourceId}: ${oldN} → ${newN} claims (${dropped} dropped)`, 'success');
    // Refresh the vault page if it's mounted
    if (typeof loadVault === 'function') { try { loadVault(); } catch (_) {} }
  }

  async function reingestAllSourcesUI() {
    const btn = document.getElementById('vu-reingest-all');
    if (btn) { btn.disabled = true; btn.textContent = 'RUNNING…'; }
    const host = document.getElementById('vu-sources-list');
    const rows = host ? Array.from(host.querySelectorAll('.vu-source-row')) : [];
    for (const row of rows) {
      const status = row.querySelector('.vu-source-status');
      if (status) status.textContent = 'queued';
    }
    const r = await api.post('/api/vault/reingest', {}, { silent: true });
    if (btn) { btn.disabled = false; btn.textContent = 'RE-INGEST ALL'; }
    if (!r.ok) {
      toast(`Re-ingest ALL failed: ${r.error || 'unknown'}`, 'error');
      return;
    }
    const d = (r.data && r.data.data) || r.data || {};
    const totals = d.totals || {};
    toast(`Re-ingest: ${totals.claims_inserted || 0} inserted, ${totals.claims_dropped_unverified || 0} dropped, ${totals.errors || 0} errors`, 'success');
    await loadVaultUpdateState();
    if (typeof loadVault === 'function') { try { loadVault(); } catch (_) {} }
  }

  async function quickUpdateUI(payload, statusEl) {
    if (statusEl) statusEl.textContent = 'LLM running…';
    const r = await api.post('/api/vault/quick-update', payload, { silent: true });
    if (!r.ok) {
      if (statusEl) statusEl.textContent = `Failed: ${r.error || 'unknown'}`;
      toast(`Quick-update: ${r.error || 'failed'}`, 'error');
      return null;
    }
    const d = (r.data && r.data.data) || r.data || {};
    const touched = d.touched || [];
    const parts = [];
    for (const t of touched) {
      if (!t.ok) {
        parts.push(`${(t.kind || '').toUpperCase()}: ${t.error || 'failed'}`);
        continue;
      }
      const seg = `${(t.kind || '').toUpperCase()} #${t.source_id}: ${t.claims_inserted ?? 0} claims` +
                  (t.claims_dropped_unverified ? `, ${t.claims_dropped_unverified} dropped` : '');
      parts.push(t.llm_run_id
        ? `${seg} <a href="#" data-llm-run="${t.llm_run_id}">VIEW LLM REASONING</a>`
        : seg);
    }
    if (statusEl) {
      statusEl.innerHTML = parts.join(' &middot; ') || 'No-op.';
      statusEl.querySelectorAll('[data-llm-run]').forEach(a => {
        a.addEventListener('click', (ev) => {
          ev.preventDefault();
          openLLMRunModal(Number(a.getAttribute('data-llm-run')));
        });
      });
    }
    toast('Vault updated.', 'success');
    await loadVaultUpdateState();
    if (typeof loadVault === 'function') { try { loadVault(); } catch (_) {} }
    if (typeof loadVaultSummary === 'function') { try { loadVaultSummary(); } catch (_) {} }
    return d;
  }

  async function ingestResumeUI() {
    const fileEl = document.getElementById('vu-resume-file');
    const status = document.getElementById('vu-resume-status');
    if (!fileEl || !fileEl.files || !fileEl.files[0]) {
      if (status) status.textContent = 'Pick a resume file first.';
      return;
    }
    if (status) status.textContent = 'Uploading + extracting…';
    const fd = new FormData();
    fd.append('file', fileEl.files[0]);
    fd.append('source_type', 'resume');
    const r = await api.post('/api/evidence/upload', fd, { silent: true });
    if (!r.ok) {
      if (status) status.textContent = `Failed: ${r.error || 'unknown'}`;
      toast(`Resume upload: ${r.error || 'failed'}`, 'error');
      return;
    }
    const sourceId = r.data && (r.data.source_id ?? r.data.data?.source_id);
    if (status) status.textContent = `Source #${sourceId} ingested · running LLM re-ingest…`;
    // Now upgrade with the strict LLM extractor so claims have source_span verification.
    const r2 = await api.post(`/api/vault/sources/${sourceId}/reingest`, {}, { silent: true });
    if (!r2.ok) {
      if (status) status.textContent = `Ingested but LLM upgrade failed: ${r2.error || 'unknown'}`;
      toast('Resume ingested (deterministic claims only).', 'warn');
      return;
    }
    const d = (r2.data && r2.data.data) || r2.data || {};
    if (status) {
      status.innerHTML = `Source #${sourceId} &rarr; ${d.claims_inserted ?? 0} verified claims` +
        (d.claims_dropped_unverified ? `, ${d.claims_dropped_unverified} dropped` : '') +
        (d.llm_run_id ? ` <a href="#" data-llm-run="${d.llm_run_id}">VIEW LLM REASONING</a>` : '');
      const link = status.querySelector('[data-llm-run]');
      if (link) link.addEventListener('click', (ev) => {
        ev.preventDefault();
        openLLMRunModal(Number(link.getAttribute('data-llm-run')));
      });
    }
    toast(`Resume ingested with LLM-verified claims.`, 'success');
    await loadVaultUpdateState();
    if (typeof loadVault === 'function') { try { loadVault(); } catch (_) {} }
  }

  function bindVaultQuickUpdate() {
    const toggle = document.getElementById('vault-update-toggle');
    const modal = document.getElementById('vault-update-modal');
    const closer = document.getElementById('vault-update-close');
    if (!toggle || !modal) return;
    toggle.addEventListener('click', async () => {
      modal.classList.remove('hidden');
      toggle.setAttribute('aria-expanded', 'true');
      await loadVaultUpdateState();
    });
    if (closer) closer.addEventListener('click', () => {
      modal.classList.add('hidden');
      toggle.setAttribute('aria-expanded', 'false');
    });
    modal.addEventListener('click', (e) => {
      if (e.target === modal) {
        modal.classList.add('hidden');
        toggle.setAttribute('aria-expanded', 'false');
      }
    });

    // Per-URL save buttons
    const saveLI = document.getElementById('vu-save-li');
    const saveGH = document.getElementById('vu-save-gh');
    const savePF = document.getElementById('vu-save-pf');
    const urlStatus = document.getElementById('vu-url-status');
    if (saveLI) saveLI.addEventListener('click', () => {
      const v = (document.getElementById('vu-linkedin').value || '').trim();
      if (!v) { toast('Enter a LinkedIn URL first.', 'warn'); return; }
      quickUpdateUI({ linkedin_url: v }, urlStatus);
    });
    if (saveGH) saveGH.addEventListener('click', () => {
      const v = (document.getElementById('vu-github').value || '').trim();
      if (!v) { toast('Enter a GitHub URL first.', 'warn'); return; }
      quickUpdateUI({ github_url: v }, urlStatus);
    });
    if (savePF) savePF.addEventListener('click', () => {
      const v = (document.getElementById('vu-portfolio').value || '').trim();
      if (!v) { toast('Enter a portfolio URL first.', 'warn'); return; }
      quickUpdateUI({ portfolio_url: v }, urlStatus);
    });

    // Paste
    const pasteBtn = document.getElementById('vu-paste-ingest');
    const pasteStatus = document.getElementById('vu-paste-status');
    if (pasteBtn) pasteBtn.addEventListener('click', () => {
      const text = (document.getElementById('vu-paste-text').value || '').trim();
      const label = (document.getElementById('vu-paste-label').value || '').trim();
      if (!text) { toast('Paste some text first.', 'warn'); return; }
      quickUpdateUI({ paste_text: text, paste_label: label || null }, pasteStatus);
    });

    // Resume upload
    const resumeBtn = document.getElementById('vu-resume-ingest');
    if (resumeBtn) resumeBtn.addEventListener('click', ingestResumeUI);

    // Re-ingest all
    const allBtn = document.getElementById('vu-reingest-all');
    if (allBtn) allBtn.addEventListener('click', reingestAllSourcesUI);
  }

  // SETUP page integration: add "INGEST" link next to LinkedIn/GitHub/Portfolio
  // URL inputs that triggers quick-update when the value differs from saved.
  function bindSetupQuickIngestLinks() {
    const form = document.getElementById('profile-form');
    if (!form) return;
    const fields = [
      ['linkedin_url', 'linkedin'],
      ['github_url', 'github'],
      ['portfolio_url', 'portfolio'],
    ];
    for (const [name, kind] of fields) {
      const input = form.elements.namedItem(name);
      if (!input) continue;
      // Build an INGEST button + tiny status, append into the input's label
      const wrap = input.closest('label') || input.parentElement;
      if (!wrap) continue;
      const link = el('button', {
        type: 'button',
        class: 'btn btn-ghost small setup-ingest-link',
        title: 'Fetch the URL now and re-extract claims with the LLM',
      }, 'INGEST NOW');
      const status = el('span', { class: 'muted small setup-ingest-status' });
      link.addEventListener('click', async () => {
        const v = (input.value || '').trim();
        if (!v) { toast(`Enter a ${kind} URL first.`, 'warn'); return; }
        link.disabled = true; link.textContent = 'INGESTING…';
        status.textContent = '';
        const payload = {};
        payload[`${kind}_url`] = v;
        const d = await quickUpdateUI(payload, status);
        link.disabled = false; link.textContent = 'INGEST NOW';
      });
      wrap.appendChild(link);
      wrap.appendChild(status);
    }
  }

  // ============================================================
  // INTERVIEW PREP + PRACTICE MODE
  // ============================================================
  async function loadInterview() {
    const host = $('#iv-app-list');
    if (!host) return;
    host.innerHTML = '<p class="muted">Loading eligible applications…</p>';
    const r = await api.get('/api/interview/eligible', { silent: true });
    if (!r.ok) {
      host.innerHTML = '<p class="ap-error">Could not load eligible applications: ' + (r.error || 'unknown') + '</p>';
      return;
    }
    const apps = (r.data || []);
    host.innerHTML = '';
    if (!apps.length) {
      host.appendChild(el('p', { class: 'muted',
        text: 'No applications in the prep funnel yet. Move at least one app to prepared/applied/interview in PIPELINE.' }));
      return;
    }
    for (const app of apps) {
      const hasPacket = !!app.packet_id;
      const statusPill = el('span', { class: 'iv-pill iv-pill-active',
        text: String(app.status || '').toUpperCase() });
      const packetPill = el('span', {
        class: 'iv-pill ' + (hasPacket ? 'iv-pill-active' : 'iv-pill-warn'),
        text: hasPacket ? 'PACKET READY' : 'NO PACKET',
      });
      const left = el('div', {}, [
        el('div', { class: 'iv-q',
          text: (app.job_title || ('application #' + app.application_id)) + ' · ' + (app.job_company || '—') }),
        el('div', { class: 'iv-app-meta' }, [
          statusPill, packetPill,
          el('span', { text: 'application #' + app.application_id }),
          el('span', { class: 'muted',
            text: '  · ' + (app.practice_count || 0) + ' practice session(s)' }),
        ]),
      ]);
      const actions = el('div', { class: 'iv-app-actions' }, [
        hasPacket
          ? el('button', { class: 'btn btn-secondary small', type: 'button',
              onclick: () => viewPacket(app.application_id) }, 'VIEW PACKET')
          : el('button', { class: 'btn btn-primary small', type: 'button',
              onclick: (e) => generatePrep(app.application_id, e.target) }, 'GENERATE PREP PACKET'),
        el('button', { class: 'btn btn-positive small', type: 'button',
          onclick: () => startPractice(app.application_id) }, 'START PRACTICE'),
      ]);
      host.appendChild(el('div', { class: 'iv-app-card' }, [left, actions]));
    }
    // If the user clicked INTERVIEW PREP from the pipeline modal, auto-open
    // that application's packet.
    if (state.interviewFocusAppId) {
      const target = state.interviewFocusAppId;
      state.interviewFocusAppId = null;
      // Defer so the eligible list renders first
      setTimeout(() => viewPacket(target).catch(() => {}), 50);
    }
  }

  async function generatePrep(appId, btn) {
    const banner = $('#interview-banner');
    if (banner) {
      banner.classList.remove('hidden');
      banner.classList.remove('banner-warn');
      banner.classList.add('banner-muted');
      banner.textContent = 'Generating prep packet… this calls your local LLM and can take 30-90s.';
    }
    if (btn) { btn.disabled = true; btn.textContent = 'GENERATING…'; }
    const r = await api.post('/api/interview/prep/' + appId, {});
    if (btn) { btn.disabled = false; btn.textContent = 'GENERATE PREP PACKET'; }
    if (!r.ok) {
      if (banner) {
        banner.classList.remove('banner-muted');
        banner.classList.add('banner-warn');
        banner.textContent = 'Prep packet failed: ' + (r.error || 'unknown');
      }
      return;
    }
    if (banner) banner.textContent = 'Packet ready. All references cite Vault claims.';
    toast('Prep packet generated.', 'success');
    await loadInterview();
    await renderPacket(r.data, r.llm_run_id);
  }

  async function viewPacket(appId) {
    const r = await api.get('/api/interview/prep/' + appId, { silent: true });
    if (!r.ok) {
      toast('No packet yet — click GENERATE PREP PACKET.', 'warn');
      return;
    }
    await renderPacket(r.data, r.data && r.data.llm_run_id);
  }

  async function renderPacket(packet, llmRunId) {
    const card = $('#iv-packet-card');
    const body = $('#iv-packet-body');
    if (!card || !body) return;
    card.style.display = '';
    $('#iv-packet-title').textContent = 'Prep packet · application #' + packet.application_id +
      ' · created ' + fmtRel(packet.created_at) + ' ago';
    body.innerHTML = '<p class="muted">Loading claim references…</p>';

    const claimMap = _ivBuildClaimIndex(packet);
    await _ivFetchClaimTexts(claimMap);

    body.innerHTML = '';
    // Company brief
    body.appendChild(el('div', { class: 'iv-section' }, [
      el('h4', { text: 'Company brief (from JD only)' }),
      el('p', { text: packet.company_brief || '(empty)' }),
    ]));

    // LLM run link
    if (llmRunId || packet.llm_run_id) {
      const rid = llmRunId || packet.llm_run_id;
      body.appendChild(el('button', {
        class: 'iv-llm-link',
        onclick: () => openLLMRunModal(Number(rid)),
        text: 'VIEW LLM REASONING #' + rid,
      }));
    }

    const renderQList = (title, items, keyClaim, metaKey) => {
      const list = el('ul', { class: 'iv-qlist' });
      (items || []).forEach(q => {
        const li = el('li', {});
        const qLine = el('div', {}, [
          el('span', { class: 'iv-q', text: q.question || '(missing question)' }),
        ]);
        if (keyClaim && q[keyClaim] != null) {
          const pill = _ivClaimPill(q[keyClaim], claimMap);
          if (pill) qLine.appendChild(pill);
        }
        li.appendChild(qLine);
        if (metaKey && q[metaKey]) {
          li.appendChild(el('div', { class: 'iv-q-meta',
            text: (metaKey === 'target_competency' ? 'competency: ' :
                   metaKey === 'skill_or_tool' ? 'skill/tool: ' :
                   metaKey === 'judgement_axis' ? 'axis: ' : '') + q[metaKey] }));
        }
        list.appendChild(li);
      });
      return el('div', { class: 'iv-section' }, [
        el('h4', { text: title }), list,
      ]);
    };

    body.appendChild(renderQList(
      'Behavioral questions (' + (packet.behavioral_questions_json || []).length + ')',
      packet.behavioral_questions_json, 'suggested_claim_id', 'target_competency'));
    body.appendChild(renderQList(
      'Technical questions (' + (packet.technical_questions_json || []).length + ')',
      packet.technical_questions_json, 'suggested_claim_id', 'skill_or_tool'));
    body.appendChild(renderQList(
      'Scenario questions (' + (packet.scenario_questions_json || []).length + ')',
      packet.scenario_questions_json, null, 'judgement_axis'));

    // STAR skeletons
    const skSection = el('div', { class: 'iv-section' }, [el('h4', { text: 'STAR skeletons' })]);
    (packet.star_skeletons_json || []).forEach(sk => {
      const div = el('div', { class: 'iv-skeleton' });
      div.appendChild(el('div', { class: 'iv-q' }, [
        sk.behavioral_question || '(no question)',
        _ivClaimPill(sk.situation_from_claim_id, claimMap) || el('span', { class: 'muted small', text: ' (no claim)' }),
      ]));
      const star = sk.draft_star || {};
      div.appendChild(el('div', { class: 'iv-star-row' }, [
        el('span', { text: 'S' }), el('span', { text: star.situation || '—' }),
      ]));
      div.appendChild(el('div', { class: 'iv-star-row' }, [
        el('span', { text: 'T' }), el('span', { text: star.task || '—' }),
      ]));
      div.appendChild(el('div', { class: 'iv-star-row' }, [
        el('span', { text: 'A' }), el('span', { text: star.action || '—' }),
      ]));
      div.appendChild(el('div', { class: 'iv-star-row' }, [
        el('span', { text: 'R' }), el('span', { text: star.result || '—' }),
      ]));
      if (sk.situation_from_claim_id != null && claimMap[sk.situation_from_claim_id]) {
        div.appendChild(el('div', { class: 'iv-claim-source',
          text: 'Source claim #' + sk.situation_from_claim_id + ': ' + claimMap[sk.situation_from_claim_id] }));
      }
      skSection.appendChild(div);
    });
    body.appendChild(skSection);

    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  async function startPractice(appId) {
    // Pre-load + open the modal so the user sees a status while we generate
    // a packet if needed.
    const modal = $('#practice-modal');
    const body = $('#practice-body');
    if (!modal || !body) return;
    body.innerHTML = '<p class="muted">Starting practice session… this may generate a fresh packet if none exists.</p>';
    $('#practice-progress').textContent = '—';
    modal.classList.remove('hidden');

    const r = await api.post('/api/interview/practice/' + appId + '/start',
      { question_count: 5 });
    if (!r.ok) {
      body.innerHTML = '<p class="ap-error">Could not start practice: ' + (r.error || 'unknown') + '</p>';
      return;
    }
    const d = r.data || {};
    _practice.session_id = d.session_id;
    _practice.application_id = appId;
    _practice.packet_id = d.packet_id;
    _practice.total = d.total_questions;
    _practice.answered = 0;
    _practice.currentTurn = d.first_question;
    _practice.claimIndex = {};
    // Eagerly fetch vault claims so the feedback pills can show text
    const cr = await api.get('/api/vault/claims', { silent: true });
    if (cr.ok) {
      (cr.data || []).forEach(c => {
        if (c && c.id != null) _practice.claimIndex[c.id] = c.claim_text || '';
      });
    }
    renderPracticeTurn();
  }

  function renderPracticeTurn() {
    const body = $('#practice-body');
    const prog = $('#practice-progress');
    const turn = _practice.currentTurn;
    if (!turn) {
      // Done — render summary
      renderPracticeFinal();
      return;
    }
    prog.textContent = 'Q ' + ((turn.turn_index ?? 0) + 1) + ' of ' + _practice.total;
    body.innerHTML = '';
    body.appendChild(el('div', { class: 'practice-q' }, [
      el('div', { class: 'practice-q-type', text: turn.question_type || 'general' }),
      el('div', { text: turn.question_text || '(missing question)' }),
    ]));
    body.appendChild(el('label', {}, [
      'YOUR ANSWER',
      el('textarea', {
        id: 'practice-answer-text', class: 'practice-answer-area',
        placeholder: 'Walk through your answer. STAR structure recommended.\nSituation — Task — Action — Result.',
        rows: 8,
      }),
    ]));
    body.appendChild(el('div', { class: 'form-actions' }, [
      el('button', { class: 'btn btn-primary', type: 'button',
        id: 'practice-submit-btn',
        onclick: submitPracticeAnswer }, 'SUBMIT ANSWER'),
      el('button', { class: 'btn btn-ghost', type: 'button',
        onclick: () => { $('#practice-modal').classList.add('hidden'); loadInterview(); } }, 'SAVE & EXIT'),
    ]));
  }

  async function submitPracticeAnswer() {
    const ta = $('#practice-answer-text');
    const btn = $('#practice-submit-btn');
    if (!ta) return;
    const txt = (ta.value || '').trim();
    if (!txt) { toast('Type an answer first — even one sentence.', 'warn'); return; }
    btn.disabled = true; btn.textContent = 'GRADING…';
    const r = await api.post('/api/interview/practice/' + _practice.session_id + '/answer', {
      turn_index: _practice.currentTurn.turn_index,
      user_answer: txt,
    });
    btn.disabled = false; btn.textContent = 'SUBMIT ANSWER';
    if (!r.ok) {
      toast('Grading failed: ' + (r.error || 'unknown'), 'error');
      return;
    }
    const d = r.data || {};
    _practice.answered += 1;
    renderPracticeFeedback(d);
    // Save next turn ref for advance
    _practice._next = d.next_question;
  }

  function renderPracticeFeedback(d) {
    const body = $('#practice-body');
    if (!body) return;
    const f = d.feedback || {};
    const turn = d.turn || {};
    body.innerHTML = '';
    $('#practice-progress').textContent =
      'Q ' + ((turn.turn_index ?? 0) + 1) + ' of ' + _practice.total + ' · graded';

    body.appendChild(el('div', { class: 'practice-q' }, [
      el('div', { class: 'practice-q-type', text: turn.question_type || 'general' }),
      el('div', { text: turn.question_text || '' }),
    ]));
    body.appendChild(el('div', { class: 'iv-section' }, [
      el('h4', { text: 'Your answer' }),
      el('pre', { class: 'codeblock', text: turn.user_answer || '' }),
    ]));

    const fb = el('div', { class: 'practice-feedback' });
    fb.appendChild(el('div', { class: 'practice-score' }, [
      el('span', { class: 'practice-score-val', text: String(f.score ?? '—') }),
      el('span', { class: 'practice-score-meta', text: '/ 10' }),
    ]));
    if ((f.strengths || []).length) {
      fb.appendChild(el('h5', { text: 'Strengths' }));
      const ul = el('ul', {});
      f.strengths.forEach(s => ul.appendChild(el('li', { text: s })));
      fb.appendChild(ul);
    }
    if ((f.improvements || []).length) {
      fb.appendChild(el('h5', { text: 'Improvements' }));
      const ul = el('ul', {});
      f.improvements.forEach(s => ul.appendChild(el('li', { text: s })));
      fb.appendChild(ul);
    }
    if ((f.evidence_used || []).length) {
      fb.appendChild(el('h5', { text: 'Evidence used (cited Vault claims)' }));
      const pillRow = el('div', {});
      f.evidence_used.forEach(cid => {
        const txt = _practice.claimIndex[cid] || '';
        pillRow.appendChild(el('span', {
          class: 'practice-evidence-pill',
          title: txt ? 'claim #' + cid + ': ' + txt : 'claim #' + cid,
          onclick: () => showTextModal('Vault claim #' + cid, txt || '(text not loaded)'),
          text: '[claim #' + cid + ']',
        }));
      });
      fb.appendChild(pillRow);
    }
    if ((f.unverified_claims || []).length) {
      fb.appendChild(el('h5', { text: 'Unverified claims (NOT in your Vault — flag honestly)' }));
      const wrap = el('div', {});
      f.unverified_claims.forEach(s => {
        wrap.appendChild(el('span', { class: 'practice-unverified-pill', text: s }));
      });
      fb.appendChild(wrap);
    }
    if (f.rewrite_suggestion) {
      fb.appendChild(el('h5', { text: 'STAR rewrite suggestion' }));
      fb.appendChild(el('pre', { class: 'codeblock', text: f.rewrite_suggestion }));
    }
    if (d.llm_run_id) {
      fb.appendChild(el('button', {
        class: 'iv-llm-link', type: 'button',
        onclick: () => openLLMRunModal(Number(d.llm_run_id)),
        text: 'VIEW LLM REASONING #' + d.llm_run_id,
      }));
    }
    body.appendChild(fb);

    const isLast = !d.next_question;
    body.appendChild(el('div', { class: 'form-actions' }, [
      el('button', { class: 'btn btn-primary', type: 'button',
        onclick: () => {
          if (isLast) {
            renderPracticeFinal();
          } else {
            _practice.currentTurn = d.next_question;
            renderPracticeTurn();
          }
        },
        text: isLast ? 'FINISH' : 'NEXT QUESTION' }),
      el('button', { class: 'btn btn-ghost', type: 'button',
        onclick: () => { $('#practice-modal').classList.add('hidden'); loadInterview(); } }, 'EXIT'),
    ]));
  }

  async function renderPracticeFinal() {
    const body = $('#practice-body');
    if (!body) return;
    const r = await api.get('/api/interview/practice/session/' + _practice.session_id, { silent: true });
    const d = (r.ok && r.data) || {};
    const session = d.session || {};
    const turns = d.turns || [];
    body.innerHTML = '';
    body.appendChild(el('div', { class: 'practice-final' }, [
      el('h4', { text: 'Mock interview complete' }),
      el('div', { class: 'practice-final-score',
        text: session.avg_score == null ? '—' : String(session.avg_score) }),
      el('p', { class: 'muted', text: 'Average score across ' + (turns.length) + ' answered turn(s).' }),
    ]));
    // Per-turn recap
    turns.forEach(t => {
      const row = el('div', { class: 'iv-skeleton' }, [
        el('div', { class: 'iv-q', text: 'Q' + ((t.turn_index ?? 0) + 1) + ' · ' + (t.question_text || '') }),
        el('div', { class: 'iv-q-meta', text: 'score ' + (t.score == null ? '—' : t.score) +
          ' · type ' + (t.question_type || 'general') }),
      ]);
      if (t.user_answer) {
        row.appendChild(el('pre', { class: 'codeblock', text: t.user_answer }));
      }
      if (t.llm_run_id) {
        row.appendChild(el('button', { class: 'iv-llm-link', type: 'button',
          onclick: () => openLLMRunModal(Number(t.llm_run_id)),
          text: 'VIEW LLM REASONING #' + t.llm_run_id }));
      }
      body.appendChild(row);
    });
    body.appendChild(el('div', { class: 'form-actions' }, [
      el('button', { class: 'btn btn-primary', type: 'button',
        onclick: () => { $('#practice-modal').classList.add('hidden'); loadInterview(); },
        text: 'DONE' }),
    ]));
  }

  function bindInterview() {
    const btn = $('#iv-refresh');
    if (btn) btn.addEventListener('click', loadInterview);
    const close = $('#iv-packet-close');
    if (close) close.addEventListener('click', () => {
      $('#iv-packet-card').style.display = 'none';
    });
    const pclose = $('#practice-close-btn');
    if (pclose) pclose.addEventListener('click', () => {
      $('#practice-modal').classList.add('hidden');
      loadInterview();
    });
  }

  // ============================================================
  // CAREER SNAPSHOT — landing-page LLM narrative
  // ============================================================
  function bindCareerSnapshot() {
    const card = document.getElementById('career-snapshot-card');
    if (!card) return;
    const genBtn = document.getElementById('snapshot-generate-btn');
    const viewBtn = document.getElementById('snapshot-view-reasoning-btn');
    if (genBtn) genBtn.addEventListener('click', generateCareerSnapshot);
    if (viewBtn) viewBtn.addEventListener('click', () => {
      const rid = viewBtn.getAttribute('data-run-id');
      if (rid) openLLMRunModal(Number(rid));
    });
    loadCareerSnapshot();
  }

  async function loadCareerSnapshot() {
    const r = await api.get('/api/profile/snapshot', { silent: true });
    if (r.ok && r.data) renderCareerSnapshot(r.data);
  }

  async function generateCareerSnapshot() {
    const btn = document.getElementById('snapshot-generate-btn');
    btn.disabled = true; btn.textContent = 'GENERATING (1–3 min)…';
    const r = await api.post('/api/profile/snapshot', {});
    btn.disabled = false; btn.textContent = 'REGENERATE SNAPSHOT';
    if (!r.ok) {
      toast('Snapshot failed: ' + (r.error || r.detail || 'unknown'), 'error');
      return;
    }
    renderCareerSnapshot(r.data);
    toast(`Snapshot ready (${r.data.generated_by || 'llm'})`, 'success');
  }

  function renderCareerSnapshot(snap) {
    const body = document.getElementById('snapshot-body');
    const empty = document.getElementById('snapshot-empty');
    const viewBtn = document.getElementById('snapshot-view-reasoning-btn');
    const genBtn = document.getElementById('snapshot-generate-btn');
    if (!body) return;
    if (genBtn) genBtn.textContent = 'REGENERATE SNAPSHOT';
    if (empty) empty.classList.add('hidden');
    body.classList.remove('hidden');
    const esc = (s) => (s || '').toString().replace(/&/g,'&amp;').replace(/</g,'&lt;');
    const basic = snap.basic_info || {};
    const stagePill = `<span class="career-stage-pill stage-${esc(snap.career_stage || 'unclear')}">${esc((snap.career_stage || 'unclear').toUpperCase())}</span>`;
    const recs = (snap.job_recommendations || []).map((rec, i) => `
      <li>
        <strong>${esc(rec.title)}</strong>
        ${rec.keywords && rec.keywords.length ? `<span class="muted small">— ${esc(rec.keywords.join(', '))}</span>` : ''}
        <div class="muted small">${esc(rec.rationale || '')}</div>
        <button type="button" class="btn btn-ghost small" data-snap-search="${esc(rec.title)}">SEARCH JOBS FOR THIS →</button>
      </li>`).join('');
    const nextSteps = (snap.next_steps || []).map(ns => `
      <li><strong>${esc(ns.move)}</strong>
          <div class="muted small">${esc(ns.rationale || '')}</div></li>`).join('');
    const strengths = (snap.strengths || []).map(s => `<span class="ap-kpi" style="font-size:var(--fs-micro);">${esc(s)}</span>`).join(' ');

    body.innerHTML = `
      <div class="snapshot-header">
        <h4>${esc(basic.name || 'You')} ${stagePill}</h4>
        <p class="muted small">${esc(basic.current_role || '')}${basic.location ? ' · ' + esc(basic.location) : ''}</p>
      </div>
      <p class="snapshot-narrative">${esc(snap.narrative || '')}</p>
      <details class="snapshot-section" open>
        <summary><strong>WHAT YOU DO</strong></summary>
        <p>${esc(snap.what_they_do || '')}</p>
      </details>
      <details class="snapshot-section">
        <summary><strong>CAREER STAGE</strong> ${stagePill}</summary>
        <p>${esc(snap.career_stage_reasoning || '')}</p>
      </details>
      ${strengths ? `<details class="snapshot-section" open>
        <summary><strong>STRENGTHS</strong></summary>
        <div class="snapshot-strengths">${strengths}</div>
      </details>` : ''}
      ${nextSteps ? `<details class="snapshot-section" open>
        <summary><strong>NEXT CAREER MOVES</strong></summary>
        <ul class="snapshot-list">${nextSteps}</ul>
      </details>` : ''}
      ${recs ? `<details class="snapshot-section" open>
        <summary><strong>JOB RECOMMENDATIONS</strong></summary>
        <ul class="snapshot-list">${recs}</ul>
        <p class="muted small">Click a recommendation to search jobs for it — results land in the DASHBOARD where you can save or dismiss them.</p>
      </details>` : ''}`;
    // Wire job-recommendation search buttons
    body.querySelectorAll('[data-snap-search]').forEach(b => {
      b.addEventListener('click', async () => {
        const q = b.getAttribute('data-snap-search');
        if (!q) return;
        b.disabled = true; b.textContent = 'SEARCHING…';
        const r = await api.post('/api/search', { query: q, limit: 25 });
        b.disabled = false; b.textContent = 'SEARCH JOBS FOR THIS →';
        if (r.ok) {
          toast(`Found ${r.data?.discovered || 0} jobs for "${q}". Opening Dashboard.`, 'success');
          switchPage('dashboard');
        }
      });
    });
    if (viewBtn) {
      if (snap.llm_run_id) {
        viewBtn.classList.remove('hidden');
        viewBtn.setAttribute('data-run-id', String(snap.llm_run_id));
      } else {
        viewBtn.classList.add('hidden');
      }
    }
  }

  // ============================================================
  // BASE RESUME — landing-page view
  // ============================================================
  function bindBaseResume() {
    const card = document.getElementById('base-resume-card');
    if (!card) return;
    const genBtn = document.getElementById('base-resume-generate-btn');
    const viewBtn = document.getElementById('base-resume-view-reasoning-btn');
    if (genBtn) genBtn.addEventListener('click', generateBaseResume);
    if (viewBtn) viewBtn.addEventListener('click', () => {
      const rid = viewBtn.getAttribute('data-run-id');
      if (rid) openLLMRunModal(Number(rid));
    });
    loadBaseResume();
  }
  async function loadBaseResume() {
    const r = await api.get('/api/resume/base', { silent: true });
    if (r.ok && r.data) renderBaseResume(r.data);
  }
  async function generateBaseResume() {
    const btn = document.getElementById('base-resume-generate-btn');
    btn.disabled = true; btn.textContent = 'BUILDING (2–6 min)…';
    const r = await api.post('/api/resume/base/generate', {});
    btn.disabled = false; btn.textContent = 'REGENERATE BASE RESUME';
    if (!r.ok) {
      toast('Base resume failed: ' + (r.error || r.detail || 'unknown'), 'error');
      return;
    }
    renderBaseResume(r.data);
    toast(`Base resume ready (${r.data.generated_by || 'llm'})`, 'success');
  }
  function renderBaseResume(data) {
    const body = document.getElementById('base-resume-body');
    const empty = document.getElementById('base-resume-empty');
    const viewBtn = document.getElementById('base-resume-view-reasoning-btn');
    const genBtn = document.getElementById('base-resume-generate-btn');
    if (!body) return;
    if (genBtn) genBtn.textContent = 'REGENERATE BASE RESUME';
    if (empty) empty.classList.add('hidden');
    body.classList.remove('hidden');
    const md = data.markdown || '';
    // Render markdown minimally — bold + headings + bullets. We keep it
    // small so the user sees a clean preview without a full md parser.
    const html = renderSimpleMarkdown(md);
    const notes = (data.honesty_notes || []).map(n => `<li>${escapeHtml(n)}</li>`).join('');
    body.innerHTML = `
      <div class="base-resume-actions">
        <button class="btn btn-ghost small" type="button" id="base-resume-copy">COPY MARKDOWN</button>
      </div>
      <article class="base-resume-md">${html}</article>
      ${notes ? `<details class="snapshot-section" open>
        <summary><strong>HONESTY NOTES</strong></summary>
        <ul>${notes}</ul></details>` : ''}
    `;
    const copyBtn = document.getElementById('base-resume-copy');
    if (copyBtn) copyBtn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(md);
        toast('Markdown copied.', 'success');
      } catch (e) {
        toast('Copy failed: ' + e.message, 'error');
      }
    });
    if (viewBtn) {
      if (data.llm_run_id) {
        viewBtn.classList.remove('hidden');
        viewBtn.setAttribute('data-run-id', String(data.llm_run_id));
      } else {
        viewBtn.classList.add('hidden');
      }
    }
  }

  function escapeHtml(s) {
    return (s || '').toString().replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }
  function renderSimpleMarkdown(md) {
    if (!md) return '';
    const lines = md.split('\n');
    const out = [];
    let inList = false;
    const closeList = () => { if (inList) { out.push('</ul>'); inList = false; } };
    for (const raw of lines) {
      const line = raw.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      let m;
      if ((m = line.match(/^# (.+)$/))) { closeList(); out.push(`<h2>${m[1]}</h2>`); continue; }
      if ((m = line.match(/^## (.+)$/))) { closeList(); out.push(`<h3>${m[1]}</h3>`); continue; }
      if ((m = line.match(/^### (.+)$/))) { closeList(); out.push(`<h4>${m[1]}</h4>`); continue; }
      if ((m = line.match(/^- (.+)$/))) {
        if (!inList) { out.push('<ul>'); inList = true; }
        let content = m[1];
        content = content.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
        content = content.replace(/_([^_]+)_/g, '<em>$1</em>');
        out.push(`<li>${content}</li>`);
        continue;
      }
      closeList();
      if (!line.trim()) { out.push(''); continue; }
      let content = line;
      content = content.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
      content = content.replace(/_([^_]+)_/g, '<em>$1</em>');
      out.push(`<p>${content}</p>`);
    }
    closeList();
    return out.join('\n');
  }

  // ============================================================
  // URL INGEST STATUS — surfaces robots-blocked LinkedIn paste UI
  // ============================================================
  function bindUrlIngestStatus() {
    const card = document.getElementById('url-ingest-card');
    if (!card) return;
    const refresh = document.getElementById('url-ingest-refresh-btn');
    if (refresh) refresh.addEventListener('click', loadUrlIngestStatus);
    const ingest = document.getElementById('linkedin-paste-ingest');
    if (ingest) ingest.addEventListener('click', ingestLinkedInPaste);
    loadUrlIngestStatus();
  }

  async function loadUrlIngestStatus() {
    const r = await api.get('/api/profile/url-ingest-status', { silent: true });
    if (!r.ok) return;
    const data = r.data || {};
    const card = document.getElementById('url-ingest-card');
    const list = document.getElementById('url-ingest-list');
    const pasteBlock = document.getElementById('linkedin-paste-block');
    if (!card || !list) return;

    // Show card only when at least one URL is configured
    const anyUrl = Object.values(data).some(v => v && v.url);
    card.classList.toggle('hidden', !anyUrl);

    list.innerHTML = '';
    const labels = {
      linkedin_url: 'LINKEDIN',
      github_url: 'GITHUB',
      portfolio_url: 'PORTFOLIO',
    };
    let needsLinkedInPaste = false;
    for (const [field, label] of Object.entries(labels)) {
      const row = data[field];
      if (!row || !row.url) continue;
      const status = row.status || 'unknown';
      const cls = row.ingested ? 'url-row-ok'
        : status === 'blocked_by_robots' ? 'url-row-blocked'
        : 'url-row-pending';
      const statusBadge = row.ingested
        ? `<span class="ap-kpi" style="background:var(--positive-soft);">OK · ${row.char_count} chars</span>`
        : status === 'blocked_by_robots'
        ? `<span class="ap-kpi" style="background:var(--accent-soft);color:var(--accent);">BLOCKED · robots.txt</span>`
        : `<span class="ap-kpi" style="background:var(--card-2);">NOT FETCHED</span>`;
      const li = document.createElement('li');
      li.className = 'url-row ' + cls;
      li.innerHTML = `
        <span class="url-row-label">${label}</span>
        <a href="${row.url}" target="_blank" rel="noopener" class="url-row-url">${row.url}</a>
        ${statusBadge}
        ${row.remediation ? `<span class="muted small url-row-hint">${row.remediation}</span>` : ''}
      `;
      list.appendChild(li);
      if (field === 'linkedin_url' && !row.ingested) needsLinkedInPaste = true;
    }
    if (pasteBlock) pasteBlock.classList.toggle('hidden', !needsLinkedInPaste);
  }

  async function ingestLinkedInPaste() {
    const textarea = document.getElementById('linkedin-paste-text');
    const btn = document.getElementById('linkedin-paste-ingest');
    const status = document.getElementById('linkedin-paste-status');
    if (!textarea || !btn) return;
    const text = (textarea.value || '').trim();
    if (text.length < 100) {
      toast('Paste at least 100 characters of your LinkedIn profile.', 'error');
      return;
    }
    btn.disabled = true; btn.textContent = 'INGESTING + EXTRACTING…';
    if (status) status.textContent = 'Saving paste + running LLM extractor (1–5 min on 70B)…';
    const r = await api.post('/api/vault/quick-update', {
      paste_text: text,
      paste_label: 'LinkedIn profile (pasted)',
      paste_source_type: 'linkedin',
    });
    btn.disabled = false; btn.textContent = 'INGEST + EXTRACT WITH LLM';
    if (!r.ok) {
      if (status) status.textContent = 'Failed: ' + (r.error || 'unknown');
      return;
    }
    const d = r.data || {};
    const claims = d.claims_inserted ?? d.claims ?? 0;
    const dropped = d.claims_dropped_unverified ?? 0;
    if (status) status.textContent = `Inserted ${claims} verified claim(s); dropped ${dropped} unverifiable.`;
    toast(`LinkedIn ingested · ${claims} claims · ${dropped} dropped`, 'success');
    textarea.value = '';
    await loadUrlIngestStatus();
    // Re-render the snapshot + base resume with the new claims if they exist
    await loadCareerSnapshot();
    await loadBaseResume();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
