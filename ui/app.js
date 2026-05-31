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
    async _req(method, path, body, opts = {}) {
      const init = { method, headers: {} };
      if (body !== undefined && !(body instanceof FormData)) {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(body);
      } else if (body instanceof FormData) {
        init.body = body;
      }
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
      }
    },
    get(p, opts)        { return this._req('GET', p, undefined, opts); },
    post(p, b, opts)    { return this._req('POST', p, b, opts); },
    put(p, b, opts)     { return this._req('PUT', p, b, opts); },
    patch(p, b, opts)   { return this._req('PATCH', p, b, opts); },
    del(p, opts)        { return this._req('DELETE', p, undefined, opts); },
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
  const PAGES = ['landing','setup','vault','dashboard','resume','pipeline','inbox','calendar','intel','network','settings'];
  function switchPage(name) {
    if (!PAGES.includes(name)) name = 'landing';
    state.page = name;
    $$('.page').forEach(p => p.classList.toggle('active', p.dataset.page === name));
    $$('.tabs a').forEach(a => a.classList.toggle('active', a.dataset.tab === name));
    window.scrollTo({ top: 0, behavior: 'instant' });
    if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);

    // page-specific lazy loads
    if (name === 'setup')     loadProfile();
    if (name === 'vault')     { loadVault(); loadVaultSummary(); }
    if (name === 'dashboard') { loadJobs(); loadSavedSearches(); }
    if (name === 'resume')    loadResumes();
    if (name === 'pipeline')  loadPipeline();
    if (name === 'inbox')     loadInbox();
    if (name === 'calendar')  { renderAvailGrid(); loadCalendarEvents(); }
    if (name === 'intel')     loadIntel();
    if (name === 'network')   loadNetwork();
    if (name === 'settings')  loadSettings();
  }
  function bindRouting() {
    window.addEventListener('hashchange', () => switchPage(location.hash.replace('#','')));
    $$('.tabs a').forEach(a => a.addEventListener('click', (e) => {
      e.preventDefault();
      switchPage(a.dataset.tab);
    }));
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
      goBtn.disabled = true;
      goBtn.textContent = 'RUNNING…';
      const progress = $('#autopilot-progress');
      const result = $('#autopilot-result');
      progress.classList.remove('hidden');
      result.classList.add('hidden');
      progress.innerHTML = renderAutopilotProgress([
        { name: 'profile_inferred', label: 'Inferring profile', status: 'pending' },
        { name: 'vault_populated', label: 'Populating Career Evidence Vault', status: 'pending' },
        { name: 'search_complete', label: 'Searching every job board', status: 'pending' },
        { name: 'scoring_complete', label: 'Scoring every job vs your evidence', status: 'pending' },
        { name: 'tailoring_complete', label: 'Tailoring resumes for top matches', status: 'pending' },
        { name: 'packets_built', label: 'Building application packets', status: 'pending' },
        { name: 'saved_search_registered', label: 'Scheduling daily re-run', status: 'pending' },
      ]);

      const r = await api.post('/api/autopilot/start', fd, { silent: true });
      goBtn.disabled = false;
      goBtn.textContent = 'START AUTOPILOT';

      if (!r.ok && !r.data) {
        toast('Autopilot failed: ' + (r.error || 'unknown'), 'error');
        return;
      }
      const d = r.data || {};
      progress.innerHTML = renderAutopilotProgress(d.steps || []);
      result.classList.remove('hidden');
      result.innerHTML = renderAutopilotResult(d);
      toast(`Autopilot finished in ${(d.elapsed_ms || 0)/1000}s — ${d.packets?.built || 0} packets ready.`,
            'success');
      // Refresh background pill + nav stats
      await loadAutopilotPill();
      bootStatus();
    });

    loadAutopilotPill();
  }

  function renderAutopilotProgress(steps) {
    const rows = (steps || []).map(s => {
      const icon = s.status === 'ok' ? '✓' :
                   s.status === 'error' ? '×' :
                   '·';
      const cls = s.status === 'ok' ? 'ap-ok' :
                  s.status === 'error' ? 'ap-err' :
                  'ap-pending';
      return `<li class="ap-step ${cls}"><span class="ap-icon">${icon}</span>
                <span class="ap-name">${s.label || s.name}</span>
                <span class="ap-detail">${(s.detail || '').replace(/</g,'&lt;')}</span></li>`;
    }).join('');
    return `<ul class="ap-list">${rows}</ul>`;
  }

  function renderAutopilotResult(d) {
    const parts = [];
    parts.push(`<h4>Done in ${((d.elapsed_ms || 0)/1000).toFixed(1)}s</h4>`);
    parts.push('<div class="ap-kpis">');
    parts.push(`<span class="ap-kpi"><strong>${d.search?.discovered ?? 0}</strong> discovered</span>`);
    parts.push(`<span class="ap-kpi"><strong>${d.search?.inserted ?? 0}</strong> new jobs</span>`);
    parts.push(`<span class="ap-kpi"><strong>${d.scoring?.scored ?? 0}</strong> scored</span>`);
    parts.push(`<span class="ap-kpi"><strong>${d.tailoring?.tailored ?? 0}</strong> tailored</span>`);
    parts.push(`<span class="ap-kpi"><strong>${d.packets?.built ?? 0}</strong> packets</span>`);
    parts.push('</div>');
    const paths = d.packets?.paths || [];
    if (paths.length) {
      parts.push('<h4>Top packets ready for review</h4><ol class="ap-packets">');
      for (const p of paths) {
        parts.push(`<li>
          <strong>${(p.title || '').replace(/</g,'&lt;')}</strong>
          @ ${(p.company || '').replace(/</g,'&lt;')}
          — score ${(Number(p.score || 0) * 100).toFixed(0)}
          <span class="ap-path muted small">${(p.packet_dir || '').replace(/</g,'&lt;')}</span>
        </li>`);
      }
      parts.push('</ol>');
    }
    if (d.saved_search?.created) {
      parts.push(`<p class="muted small">Recurring saved search active: <strong>${(d.saved_search.label || '').replace(/</g,'&lt;')}</strong> — re-runs every ${d.saved_search.frequency_hours || 24}h.</p>`);
    }
    parts.push('<div class="ap-actions">');
    parts.push('<a class="btn btn-secondary" href="#pipeline">OPEN PIPELINE</a>');
    parts.push('<a class="btn btn-ghost" href="#dashboard">OPEN DASHBOARD</a>');
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
    $('#jd-close').addEventListener('click', () => $('#job-detail').classList.add('hidden'));
    $('#jd-tailor').addEventListener('click', () => state.selectedJob && tailorForJob(state.selectedJob.id));
    $('#jd-cover').addEventListener('click', () => state.selectedJob && coverForJob(state.selectedJob.id));
    $('#jd-packet').addEventListener('click', () => state.selectedJob && buildPacket(state.selectedJob.id));
    $('#jd-save').addEventListener('click', () => state.selectedJob && saveToPipeline(state.selectedJob));
    $('#jd-recruiter').addEventListener('click', () => state.selectedJob && recruiterMessageForJob(state.selectedJob.id));
    $('#jd-interview').addEventListener('click', () => state.selectedJob && interviewPrepForJob(state.selectedJob.id));
    $('#jd-rescore').addEventListener('click', () => state.selectedJob && rescoreJob(state.selectedJob.id));
    $('#jd-archive').addEventListener('click', () => state.selectedJob && archiveJob(state.selectedJob.id));
  }
  async function loadJobs() {
    const r = await api.get('/api/jobs?limit=200', { silent: true });
    const jobs = (r.ok && (r.data || [])) || [];
    state.jobs = jobs;
    const body = $('#results-table tbody');
    body.innerHTML = '';
    $('#results-count').textContent = `${jobs.length} stored`;
    if (!jobs.length) {
      body.appendChild(el('tr', {}, el('td', { colspan: 10, class: 'empty', text: 'No jobs yet — run a search.' })));
      return;
    }
    // sort by score desc
    const sorted = jobs.slice().sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
    sorted.forEach((j, i) => {
      const score = j.score ?? null;
      const tr = el('tr', { class: 'clickable', onclick: () => openJobDetail(j) }, [
        el('td', { text: String(i + 1) }),
        el('td', { text: safeText(j.title || '—') }),
        el('td', { text: safeText(j.company || '—') }),
        el('td', { text: safeText(j.location || (j.is_remote ? 'Remote' : '—')) }),
        el('td', {}, score == null ? document.createTextNode('—')
          : el('span', { class: 'score-chip ' + scoreClass(score), text: String(Math.round(score)) })),
        el('td', { text: fmtSalary(j.salary_min, j.salary_max, j.salary_currency || 'USD') }),
        el('td', { text: fmtRel(j.posted_at || j.created_at) }),
        el('td', { text: safeText(j.source || '—') }),
        el('td', {}, renderBadges(j)),
        el('td', {}, j.url ? el('a', { href: j.url, target: '_blank', rel: 'noopener', text: 'open' }) : document.createTextNode('—')),
      ]);
      body.appendChild(tr);
    });
  }
  function renderBadges(j) {
    const wrap = el('span', {}, []);
    const score = j.score;
    if (score != null) {
      wrap.appendChild(el('span', { class: 'badge ' + (score >= 85 ? 'badge-green' : score >= 70 ? 'badge-blue' : 'badge-warn'), text: scoreLabel(score) }));
    }
    if (j.is_remote) wrap.appendChild(el('span', { class: 'badge badge-blue', text: 'REMOTE' }));
    if (j.source && ['linkedin','indeed','glassdoor'].includes(j.source)) {
      wrap.appendChild(el('span', { class: 'badge badge-warn', text: 'GRAY' }));
    } else if (j.source && ['greenhouse','lever','ashby','remotive','wwr','rss','remoteintech'].includes(j.source)) {
      wrap.appendChild(el('span', { class: 'badge badge-green', text: 'LEGAL' }));
    }
    return wrap;
  }
  async function openJobDetail(job) {
    state.selectedJob = job;
    const det = $('#job-detail');
    det.classList.remove('hidden');
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
        col.classList.remove('drag-over');
        const id = e.dataTransfer.getData('text/plain');
        if (!id) return;
        const r = await api.patch('/api/applications/' + id, { status });
        if (r.ok) { toast('Status: ' + status, 'success'); loadPipeline(); }
      });
      kb.appendChild(col);
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
        const card = el('div', {
          class: 'kan-card', draggable: 'true',
          'data-app-id': String(app.id),
          onclick: () => openApplicationModal(app),
        }, [
          el('div', { class: 'kc-title', text: safeText(app.title || ('app#' + app.id)) }),
          el('div', { class: 'kc-meta', text: `${safeText(app.company || '')} · score ${app.score ?? '—'}` }),
        ]);
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
    $('#card-save-btn').onclick = async () => {
      const payload = {
        notes: $('#app-notes').value,
        application_url: $('#app-url').value || null,
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
  function bindSettings() {
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
        const r = await fetch('/api/data/export', { method: 'GET' });
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
        el('td', { text: fmtRel(s.last_run_at) }),
        el('td', { text: s.enabled ? 'yes' : 'no' }),
        el('td', {}, [
          el('button', { class: 'btn btn-ghost small', onclick: () => runSavedSearch(s.id) }, 'RUN NOW'),
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
  // INIT
  // ============================================================
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
    bindSettings();
    refreshWeightTotal();
    bootStatus();

    const start = (location.hash || '#landing').replace('#', '');
    switchPage(PAGES.includes(start) ? start : 'landing');

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
  }

  async function loadNetwork() {
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
    // Network: refer-at lookup
    const rf = $('#refer-form');
    if (rf) {
      rf.addEventListener('submit', async (e) => {
        e.preventDefault();
        const co = rf.elements.namedItem('company').value.trim();
        if (!co) return;
        const r = await api.get('/api/connections/refer/' + encodeURIComponent(co), { silent: true });
        const list = $('#refer-results');
        list.innerHTML = '';
        const arr = (r.ok && (r.data || [])) || [];
        if (!arr.length) {
          list.appendChild(el('li', { class: 'muted', text: `No connections at ${co} (yet).` }));
        } else {
          for (const c of arr) {
            list.appendChild(el('li', {
              text: `${c.name} — ${c.role || ''} at ${c.company || co}${c.contact ? ' · ' + c.contact : ''}`
            }));
          }
        }
      });
    }
    const refCo = $('#connections-refresh');
    if (refCo) refCo.addEventListener('click', loadNetwork);
  }

  document.addEventListener('DOMContentLoaded', init);
})();
