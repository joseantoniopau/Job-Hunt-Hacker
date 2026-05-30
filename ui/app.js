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
  const PAGES = ['landing','setup','vault','dashboard','resume','pipeline','inbox','calendar','settings'];
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
    if (name === 'dashboard') loadJobs();
    if (name === 'resume')    loadResumes();
    if (name === 'pipeline')  loadPipeline();
    if (name === 'inbox')     loadInbox();
    if (name === 'calendar')  { renderAvailGrid(); loadCalendarEvents(); }
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

    $('#url-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = serializeForm(e.target);
      const r = await api.post('/api/evidence/url', fd);
      if (r.ok) { toast('URL ingested.', 'success'); e.target.reset(); }
    });
    $('#github-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = serializeForm(e.target);
      fd.repo_urls = csvToList(fd.repo_urls);
      const r = await api.post('/api/github/ingest', fd);
      if (r.ok) { toast('GitHub ingest scheduled.', 'success'); e.target.reset(); }
    });
    $('#linkedin-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = serializeForm(e.target);
      const r = await api.post('/api/evidence/linkedin', fd);
      if (r.ok) { toast('LinkedIn text ingested.', 'success'); e.target.reset(); }
    });
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
    }
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
    // sources
    const sR = await api.get('/api/vault/summary', { silent: true });
    const sources = (sR.ok && sR.data && (sR.data.sources || sR.data)) || [];
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
    // contradictions
    const cR = await api.get('/api/vault/contradictions', { silent: true });
    const banner = $('#vault-contradictions');
    if (cR.ok && Array.isArray(cR.data) && cR.data.length) {
      banner.classList.remove('hidden');
      banner.textContent = `${cR.data.length} contradiction(s) detected — review claims below.`;
    } else {
      banner.classList.add('hidden');
    }
    await loadVaultClaims();
  }
  async function deleteSource(id) {
    if (!id) return;
    if (!confirm('Delete source ' + id + ' and its claims?')) return;
    const r = await api.del('/api/vault/sources/' + id);
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
      if (r.ok) { toast('Contradiction scan done.', 'success'); loadVault(); }
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
    if (r.ok) toast('Packet built: ' + (r.data?.path || 'ok'), 'success');
  }
  async function saveToPipeline(job) {
    const r = await api.post('/api/applications', { job_id: job.id, status: 'saved' });
    if (r.ok) { toast('Saved to pipeline.', 'success'); }
  }

  // ============================================================
  // RESUME LAB
  // ============================================================
  async function loadResumes() {
    const r = await api.get('/api/resume', { silent: true });
    const list = (r.ok && (r.data || [])) || [];
    state.resumes = list;
    const body = $('#resume-list tbody');
    body.innerHTML = '';
    if (!list.length) {
      body.appendChild(el('tr', {}, el('td', { colspan: 5, class: 'empty', text: 'No resumes yet.' })));
      return;
    }
    for (const res of list) {
      body.appendChild(el('tr', { class: 'clickable', onclick: () => openResume(res.id) }, [
        el('td', { text: String(res.id) }),
        el('td', { text: safeText(res.resume_type || 'master') }),
        el('td', { text: safeText(res.job_id || '—') }),
        el('td', { text: fmtDate(res.updated_at || res.created_at) }),
        el('td', {}, [
          el('button', { class: 'btn btn-ghost small', onclick: (e) => { e.stopPropagation(); openResume(res.id); } }, 'OPEN'),
        ]),
      ]));
    }
  }
  async function openResume(id) {
    const r = await api.get('/api/resume/' + id, { silent: true });
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
      const r = await api.post('/api/resume', { resume_type: 'master' });
      if (r.ok) { toast('Master resume created.', 'success'); loadResumes(); }
    });
    $$('.resume-panel [data-export]').forEach(btn => {
      btn.addEventListener('click', () => {
        const res = state.selectedResume;
        if (!res) { toast('Select a resume first.', 'error'); return; }
        const fmt = btn.dataset.export;
        window.open(`/api/resume/${res.id}/export?format=${fmt}`, '_blank');
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
    const r = await api.get('/api/applications', { silent: true });
    const apps = (r.ok && (r.data || [])) || [];
    state.applications = apps;
    for (const col of $$('#kanban .kanban-col')) {
      const body = $('.col-body', col);
      body.innerHTML = '';
      $('.count', col).textContent = '0';
    }
    for (const app of apps) {
      const col = $(`#kanban .kanban-col[data-status="${app.status || 'saved'}"]`);
      if (!col) continue;
      const body = $('.col-body', col);
      const cnt = $('.count', col);
      cnt.textContent = String(parseInt(cnt.textContent, 10) + 1);
      const card = el('div', {
        class: 'kan-card', draggable: 'true',
        onclick: () => openApplicationModal(app),
      }, [
        el('div', { class: 'kc-title', text: safeText(app.title || ('app#' + app.id)) }),
        el('div', { class: 'kc-meta', text: `${safeText(app.company || '')} · score ${app.score ?? '—'}` }),
      ]);
      card.addEventListener('dragstart', (e) => e.dataTransfer.setData('text/plain', String(app.id)));
      body.appendChild(card);
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
    $('#card-save-btn').onclick = async () => {
      const payload = {
        notes: $('#app-notes').value,
        application_url: $('#app-url').value || null,
      };
      const f = $('#app-followup').value;
      if (f) payload.next_followup_at = new Date(f).getTime() / 1000;
      const r = await api.patch('/api/applications/' + app.id, payload);
      if (r.ok) { toast('Application updated.', 'success'); m.classList.add('hidden'); loadPipeline(); }
    };
    $('#card-close-btn').onclick = () => m.classList.add('hidden');
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
      const cls = e.classification || 'other';
      const cssClass = cls === 'recruiter' ? 'badge-green' : cls === 'rejection' ? 'badge-red' : cls === 'interview' ? 'badge-blue' : 'badge-muted';
      body.appendChild(el('tr', {}, [
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
    const r = await api.post('/api/email/draft', { event_id: ev.id });
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
    const r = await api.patch('/api/email/events/' + ev.id, { status: 'replied' });
    if (r.ok) { toast('Marked replied.', 'success'); loadInbox(); }
  }
  function bindInbox() {
    $('#inbox-refresh').addEventListener('click', loadInbox);
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
  }
  async function loadCalendarEvents() {
    const r = await api.get('/api/calendar/events', { silent: true });
    const body = $('#cal-events tbody');
    body.innerHTML = '';
    const evs = (r.ok && (r.data || [])) || [];
    if (!evs.length) {
      body.appendChild(el('tr', {}, el('td', { colspan: 4, class: 'empty', text: 'None.' })));
      return;
    }
    for (const e of evs) {
      body.appendChild(el('tr', {}, [
        el('td', { text: fmtDate(e.start_at) }),
        el('td', { text: safeText(e.with || e.attendees || '—') }),
        el('td', { text: safeText(e.job_id || '—') }),
        el('td', { text: safeText(e.notes || '') }),
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
      body.appendChild(el('tr', {}, el('td', { colspan: 5, class: 'empty', text: 'No sources registered.' })));
    } else {
      for (const s of sources) {
        const policy = s.policy || {};
        const risk = (policy.risk_level || policy.risk || 'GRAY').toUpperCase();
        const riskCls = risk === 'LEGAL' ? 'badge-green' : risk === 'GRAY' ? 'badge-warn' : 'badge-red';
        body.appendChild(el('tr', {}, [
          el('td', { text: safeText(policy.display_name || s.name) }),
          el('td', {}, el('span', { class: 'badge ' + (s.healthy ? 'badge-green' : 'badge-muted'), text: s.healthy ? 'YES' : 'no' })),
          el('td', {}, el('span', { class: 'badge ' + riskCls, text: risk })),
          el('td', { text: policy.apply_automation_allowed ? 'yes' : 'no' }),
          el('td', { text: safeText(policy.note || policy.description || '—') }),
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
      if (r.ok) { toast('Mode: ' + mode, 'success'); $('#mode-pill').textContent = 'MODE: ' + mode.toUpperCase(); }
    });
    $('#aa-enable').addEventListener('click', () => $('#aa-modal').classList.remove('hidden'));
    $('#aa-cancel-btn').addEventListener('click', () => $('#aa-modal').classList.add('hidden'));
    function updateAaBtn() {
      $('#aa-confirm-btn').disabled = !($('#aa-ack').checked && $('#aa-confirm').value.trim() === 'ENABLE');
    }
    $('#aa-ack').addEventListener('change', updateAaBtn);
    $('#aa-confirm').addEventListener('input', updateAaBtn);
    $('#aa-confirm-btn').addEventListener('click', async () => {
      const r = await api.post('/api/auto-apply/enable', { acknowledged: true });
      if (r.ok) {
        toast('Auto-apply enabled.', 'success');
        $('#aa-modal').classList.add('hidden');
        $('#compliance-banner').classList.remove('hidden');
        loadSettings();
      }
    });
    $('#aa-disable').addEventListener('click', async () => {
      const r = await api.post('/api/auto-apply/disable', {});
      if (r.ok) { toast('Auto-apply disabled.', 'success'); loadSettings(); $('#compliance-banner').classList.add('hidden'); }
    });
    $('#aa-halt').addEventListener('click', haltAutoApply);
    $('#halt-auto').addEventListener('click', haltAutoApply);

    $('#export-data').addEventListener('click', () => window.open('/api/data/export', '_blank'));
    $('#import-data').addEventListener('click', async () => {
      const f = document.createElement('input');
      f.type = 'file';
      f.accept = '.json,.zip';
      f.onchange = async () => {
        if (!f.files.length) return;
        const fd = new FormData();
        fd.append('file', f.files[0]);
        const r = await api.post('/api/data/import', fd);
        if (r.ok) toast('Imported.', 'success');
      };
      f.click();
    });
    $('#delete-data').addEventListener('click', async () => {
      if (!confirm('DELETE ALL DATA — are you sure?')) return;
      if (!confirm('This cannot be undone. Continue?')) return;
      const r = await api.del('/api/data');
      if (r.ok) { toast('All data deleted.', 'success'); loadSettings(); }
    });
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
    bindEvidence();
    bindVault();
    bindSearch();
    bindResume();
    bindPipelineBoard();
    bindInbox();
    bindCalendar();
    bindSettings();
    refreshWeightTotal();
    bootStatus();

    const start = (location.hash || '#landing').replace('#', '');
    switchPage(PAGES.includes(start) ? start : 'landing');

    // populate availability grid even when calendar tab not yet visited
    renderAvailGrid();
  }
  document.addEventListener('DOMContentLoaded', init);
})();
