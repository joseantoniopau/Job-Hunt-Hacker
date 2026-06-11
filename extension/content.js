/* Job Hunt Hacker — autofill content script.
 *
 * Injected on demand by the popup (chrome.scripting.executeScript on the
 * active tab — activeTab + scripting permissions, user-initiated only).
 * Receives the fill payload from the popup via runtime messaging, detects
 * application-form fields with heuristics, and renders a floating
 * brutalist panel with per-field FILL buttons.
 *
 * HARD GUARANTEES:
 *   - NEVER clicks submit, never calls form.submit(), never touches
 *     buttons or [type=submit] elements in any way.
 *   - Makes ZERO network calls. All data arrives in the message payload
 *     (the popup talks to 127.0.0.1:8731; this script talks to nobody).
 *   - Only writes a field value when the user clicks FILL / FILL ALL.
 */
(function () {
  "use strict";

  if (window.__JHH_AUTOFILL_LOADED__) return; // double-injection guard
  window.__JHH_AUTOFILL_LOADED__ = true;

  var xt = (typeof browser !== "undefined" && browser) ||
           (typeof chrome !== "undefined" && chrome) || null;

  var PANEL_ID = "jhh-autofill-panel";
  var MONO = '"SF Mono", Menlo, Consolas, "Courier New", monospace';

  // ---------------------------------------------------------------------
  // field detection heuristics
  // ---------------------------------------------------------------------

  var SKIP_INPUT_TYPES = {
    hidden: 1, submit: 1, button: 1, reset: 1, image: 1, file: 1,
    checkbox: 1, radio: 1, password: 1, range: 1, color: 1, date: 1,
    time: 1, "datetime-local": 1, month: 1, week: 1, number: 1
  };

  // Ordered: first matching definition wins for an element. `d` is the
  // lowercased descriptor (name/id/placeholder/aria-label/autocomplete/
  // label text); `el` is the element itself.
  var FIELD_DEFS = [
    { key: "email", label: "EMAIL",
      match: function (el, d) { return el.type === "email" || /\be?[-_ ]?mail\b/.test(d); } },
    { key: "phone", label: "PHONE",
      match: function (el, d) { return el.type === "tel" || /\b(phone|mobile|tel)\b/.test(d); } },
    { key: "linkedin", label: "LINKEDIN",
      match: function (el, d) { return /linked[-_ ]?in/.test(d); } },
    { key: "github", label: "GITHUB",
      match: function (el, d) { return /git[-_ ]?hub/.test(d); } },
    { key: "portfolio", label: "WEBSITE / PORTFOLIO",
      match: function (el, d) {
        if (/linked[-_ ]?in|git[-_ ]?hub/.test(d)) return false;
        return /\b(portfolio|website|web[-_ ]?site|personal[-_ ]?site|home[-_ ]?page|url)\b/.test(d) ||
               el.getAttribute("autocomplete") === "url";
      } },
    { key: "first_name", label: "FIRST NAME",
      match: function (el, d) { return /\b(first[-_ ]?name|given[-_ ]?name|fname)\b/.test(d); } },
    { key: "last_name", label: "LAST NAME",
      match: function (el, d) { return /\b(last[-_ ]?name|family[-_ ]?name|surname|lname)\b/.test(d); } },
    { key: "full_name", label: "FULL NAME",
      match: function (el, d) {
        if (/\b(user[-_ ]?name|company|file|middle|nick)\b/.test(d)) return false;
        return /\b(full[-_ ]?name|your[-_ ]?name|legal[-_ ]?name|name)\b/.test(d);
      } },
    { key: "location", label: "LOCATION",
      match: function (el, d) { return /\b(location|city|address|address[-_ ]?level)\b/.test(d); } },
    { key: "cover_letter", label: "COVER LETTER",
      match: function (el, d) {
        if (el.tagName !== "TEXTAREA") return false;
        return /\b(cover[-_ ]?letter|motivation|letter)\b/.test(d);
      } },
    { key: "why_company", label: "WHY THIS COMPANY",
      match: function (el, d) {
        if (el.tagName !== "TEXTAREA") return false;
        return /\bwhy\b.{0,60}\b(company|us|here|join|interested|apply|role)\b/.test(d) ||
               /\b(why[-_ ]?company|why[-_ ]?us|interest[-_ ]?in)\b/.test(d);
      } },
    { key: "resume_text", label: "RESUME TEXT",
      match: function (el, d) {
        if (el.tagName !== "TEXTAREA") return false;
        return /\b(resume|cv|curriculum)\b/.test(d);
      } },
    { key: "experience_summary", label: "EXPERIENCE SUMMARY",
      match: function (el, d) {
        if (el.tagName !== "TEXTAREA") return false;
        return /\b(experience|summary|about[-_ ]?(you|yourself|me)|background|bio)\b/.test(d);
      } }
  ];

  function descriptorFor(el) {
    var bits = [
      el.name, el.id, el.placeholder,
      el.getAttribute("aria-label"),
      el.getAttribute("aria-labelledby"),
      el.getAttribute("autocomplete"),
      el.getAttribute("data-qa"),
      el.getAttribute("data-testid")
    ];
    try {
      if (el.id && window.CSS && CSS.escape) {
        var lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (lab) bits.push(lab.textContent);
      }
      var wrap = el.closest && el.closest("label");
      if (wrap) bits.push(wrap.textContent);
      if (!wrap && el.parentElement) {
        var near = el.parentElement.querySelector("label");
        if (near) bits.push(near.textContent);
      }
    } catch (e) { /* descriptor enrichment is best-effort */ }
    return bits.filter(Boolean).join(" ")
      .toLowerCase().replace(/\s+/g, " ").slice(0, 400);
  }

  function isFillable(el) {
    if (el.disabled || el.readOnly) return false;
    if (el.tagName === "INPUT" && SKIP_INPUT_TYPES[(el.type || "text").toLowerCase()]) return false;
    var panel = document.getElementById(PANEL_ID);
    if (panel && panel.contains(el)) return false;
    var rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    return true;
  }

  function detectFields() {
    var found = [];
    var taken = {}; // key -> true (one element per category, first wins)
    var els = document.querySelectorAll("input, textarea");
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (!isFillable(el)) continue;
      var d = descriptorFor(el);
      for (var j = 0; j < FIELD_DEFS.length; j++) {
        var def = FIELD_DEFS[j];
        if (taken[def.key]) continue;
        var hit = false;
        try { hit = def.match(el, d); } catch (e) { hit = false; }
        if (hit) {
          taken[def.key] = true;
          found.push({ key: def.key, label: def.label, el: el });
          break;
        }
      }
    }
    return found;
  }

  // ---------------------------------------------------------------------
  // value mapping + filling
  // ---------------------------------------------------------------------

  function buildValues(data) {
    data = data || {};
    var p = data.profile || {};
    var a = data.answers || {};
    var name = (p.name || "").trim();
    var nameParts = name ? name.split(/\s+/) : [];
    return {
      full_name: name,
      first_name: nameParts[0] || "",
      last_name: nameParts.slice(1).join(" "),
      email: p.email || "",
      phone: p.phone || "",
      location: p.location || "",
      linkedin: p.linkedin_url || "",
      github: p.github_url || "",
      portfolio: p.portfolio_url || "",
      cover_letter: data.cover_letter_text || "",
      resume_text: data.resume_text || "",
      why_company: a.why_company || "",
      experience_summary: a.experience_summary || ""
    };
  }

  function setNativeValue(el, value) {
    // Use the prototype's value setter so framework-controlled inputs
    // (React et al.) register the change, then fire input + change.
    var proto = el.tagName === "TEXTAREA"
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    var desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function copyText(text, done) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done(true); },
        function () { done(legacyCopy(text)); });
    } else {
      done(legacyCopy(text));
    }
  }

  function legacyCopy(text) {
    try {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch (e) { return false; }
  }

  // ---------------------------------------------------------------------
  // panel UI (brutalist: monospace, 2px black border, hard shadow)
  // ---------------------------------------------------------------------

  function styleButton(btn, primary) {
    btn.style.cssText =
      "font-family:" + MONO + ";font-size:11px;text-transform:uppercase;" +
      "letter-spacing:0.04em;cursor:pointer;border:2px solid #000;" +
      "padding:4px 8px;border-radius:0;line-height:1.2;" +
      (primary ? "background:#000;color:#fff;" : "background:#fff;color:#000;");
  }

  function flash(btn, text) {
    var prev = btn.textContent;
    btn.textContent = text;
    setTimeout(function () { btn.textContent = prev; }, 1400);
  }

  function removePanel() {
    var old = document.getElementById(PANEL_ID);
    if (old && old.parentNode) old.parentNode.removeChild(old);
  }

  function showPanel(data) {
    removePanel();
    var values = buildValues(data);
    var fields = detectFields();

    var panel = document.createElement("div");
    panel.id = PANEL_ID;
    panel.style.cssText =
      "position:fixed;top:16px;right:16px;z-index:2147483647;width:340px;" +
      "max-height:80vh;overflow-y:auto;background:#fff;color:#000;" +
      "border:2px solid #000;box-shadow:6px 6px 0 #000;" +
      "font-family:" + MONO + ";font-size:12px;line-height:1.45;" +
      "text-align:left;padding:0;";

    // header
    var header = document.createElement("div");
    header.style.cssText =
      "display:flex;justify-content:space-between;align-items:center;" +
      "border-bottom:2px solid #000;padding:8px 10px;background:#000;color:#fff;";
    var title = document.createElement("span");
    title.textContent = "JHH AUTOFILL";
    title.style.cssText = "font-weight:700;letter-spacing:0.08em;";
    var close = document.createElement("button");
    close.textContent = "X";
    close.setAttribute("aria-label", "Close panel");
    close.style.cssText =
      "font-family:" + MONO + ";background:#fff;color:#000;border:2px solid #fff;" +
      "cursor:pointer;font-size:11px;padding:1px 7px;border-radius:0;";
    close.addEventListener("click", removePanel);
    header.appendChild(title);
    header.appendChild(close);
    panel.appendChild(header);

    // matched-job line
    var info = document.createElement("div");
    info.style.cssText = "padding:8px 10px;border-bottom:2px solid #000;";
    if (data && data.job) {
      info.textContent = "JOB: " + (data.job.title || "?") + " @ " +
        (data.job.company || "?") + "  [matched by " + (data.job.matched_by || "?") + "]";
    } else {
      info.textContent = "JOB: no vault match for this page — using profile + base resume.";
    }
    panel.appendChild(info);

    // field rows
    var body = document.createElement("div");
    body.style.cssText = "padding:8px 10px;";
    if (fields.length === 0) {
      var none = document.createElement("div");
      none.textContent = "No fillable form fields detected on this page.";
      body.appendChild(none);
    }

    var fillables = [];
    fields.forEach(function (f) {
      var value = values[f.key] || "";
      var row = document.createElement("div");
      row.style.cssText =
        "display:flex;justify-content:space-between;align-items:center;" +
        "gap:8px;margin:0 0 8px;border:2px solid #000;padding:5px 7px;";
      var left = document.createElement("div");
      left.style.cssText = "min-width:0;flex:1;";
      var lab = document.createElement("div");
      lab.textContent = f.label;
      lab.style.cssText = "font-weight:700;font-size:10px;letter-spacing:0.06em;";
      var preview = document.createElement("div");
      preview.textContent = value
        ? (value.length > 60 ? value.slice(0, 60) + "…" : value)
        : "(no data in vault)";
      preview.style.cssText =
        "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" +
        "font-size:10px;color:" + (value ? "#000" : "#777") + ";";
      left.appendChild(lab);
      left.appendChild(preview);
      var btn = document.createElement("button");
      btn.textContent = "FILL";
      styleButton(btn, false);
      if (!value) {
        btn.disabled = true;
        btn.style.opacity = "0.4";
        btn.style.cursor = "not-allowed";
      } else {
        var entry = { el: f.el, value: value, btn: btn };
        fillables.push(entry);
        btn.addEventListener("click", function () {
          setNativeValue(entry.el, entry.value);
          flash(btn, "FILLED");
        });
      }
      row.appendChild(left);
      row.appendChild(btn);
      body.appendChild(row);
    });
    panel.appendChild(body);

    // action bar
    var actions = document.createElement("div");
    actions.style.cssText =
      "display:flex;flex-wrap:wrap;gap:6px;padding:8px 10px;border-top:2px solid #000;";

    var fillAll = document.createElement("button");
    fillAll.textContent = "FILL ALL";
    styleButton(fillAll, true);
    if (fillables.length === 0) {
      fillAll.disabled = true;
      fillAll.style.opacity = "0.4";
      fillAll.style.cursor = "not-allowed";
    }
    fillAll.addEventListener("click", function () {
      fillables.forEach(function (entry) {
        setNativeValue(entry.el, entry.value);
        flash(entry.btn, "FILLED");
      });
      flash(fillAll, "FILLED " + fillables.length);
    });
    actions.appendChild(fillAll);

    var copyResume = document.createElement("button");
    copyResume.textContent = "COPY RESUME TEXT";
    styleButton(copyResume, false);
    var resumeText = (data && data.resume_text) || "";
    if (!resumeText) {
      copyResume.disabled = true;
      copyResume.style.opacity = "0.4";
      copyResume.style.cursor = "not-allowed";
    }
    copyResume.addEventListener("click", function () {
      copyText(resumeText, function (ok) { flash(copyResume, ok ? "COPIED" : "COPY FAILED"); });
    });
    actions.appendChild(copyResume);

    var copyLetter = document.createElement("button");
    copyLetter.textContent = "COPY COVER LETTER";
    styleButton(copyLetter, false);
    var letterText = (data && data.cover_letter_text) || "";
    if (!letterText) {
      copyLetter.disabled = true;
      copyLetter.style.opacity = "0.4";
      copyLetter.style.cursor = "not-allowed";
    }
    copyLetter.addEventListener("click", function () {
      copyText(letterText, function (ok) { flash(copyLetter, ok ? "COPIED" : "COPY FAILED"); });
    });
    actions.appendChild(copyLetter);

    var rescan = document.createElement("button");
    rescan.textContent = "RESCAN PAGE";
    styleButton(rescan, false);
    rescan.addEventListener("click", function () { showPanel(data); });
    actions.appendChild(rescan);

    panel.appendChild(actions);

    // footer — the contract, in writing, on every page
    var foot = document.createElement("div");
    foot.style.cssText =
      "padding:7px 10px;border-top:2px solid #000;font-size:10px;color:#333;";
    foot.textContent =
      "Fills only on your click. NEVER auto-submits — review every field, then submit yourself.";
    panel.appendChild(foot);

    (document.body || document.documentElement).appendChild(panel);
    return fields.length;
  }

  // ---------------------------------------------------------------------
  // messaging — popup sends the payload after fetching it from 127.0.0.1
  // ---------------------------------------------------------------------

  if (xt && xt.runtime && xt.runtime.onMessage) {
    xt.runtime.onMessage.addListener(function (msg, _sender, sendResponse) {
      if (msg && msg.type === "JHH_SHOW_PANEL") {
        var count = 0;
        try { count = showPanel(msg.payload || {}); } catch (e) {
          sendResponse({ ok: false, error: String(e && e.message || e) });
          return false;
        }
        sendResponse({ ok: true, fields_detected: count });
      } else if (msg && msg.type === "JHH_REMOVE_PANEL") {
        removePanel();
        sendResponse({ ok: true });
      } else if (msg && msg.type === "JHH_PING") {
        sendResponse({ ok: true, loaded: true });
      }
      return false; // responses above are synchronous
    });
  }
})();
