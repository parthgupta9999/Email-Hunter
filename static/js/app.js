(() => {
  const ATTACH_PATTERN = /\battach\w*\b/i;
  const TOKEN_PERSON = "{person name}";
  const TOKEN_COMPANY = "{company name}";

  const state = {
    placeholders: [],
    placeholderFields: null,
    recipients: [],
    approved: new Set(),
    rejected: new Set(),
    gmailAddress: "",
    gmailVerified: false,
    mailProvider: "gmail",
    hasAttachment: false,
    attachmentName: null,
    animating: false,
    regenerating: false,
    regenJobId: null,
    regenPoll: null,
    regenRejectAfterCancel: false,
    campaignPoll: null,
    editingRecipientEmail: null,
    composeMode: null,
    aiProvider: "groq",
    multiAgent: false,
    aiResumeLoaded: false,
    aiGenPoll: null,
    aiCancelling: false,
    aiPipelinePhase: "idle",
    aiScrapeTotal: 0,
    aiWriteTotal: 0,
    partialReview: false,
    reviewNotice: "",
    uploadAnalysis: null,
    loadingModalMode: "ai",
    fillCompaniesPoll: null,
    fillCancelling: false,
    dailyLimitAcknowledged: false,
    aiPartialStopData: null,
    aiLastProgressKey: "",
    aiLastProgressAt: 0,
  };

  const RING_CIRCUMFERENCE = 326.73;

  const $ = (sel) => document.querySelector(sel);

  function injectIcons() {
    document.querySelectorAll("[data-icon]").forEach((el) => {
      const icon = ICONS[el.dataset.icon];
      if (icon) el.innerHTML = icon;
    });
  }

  function formatToastText(text) {
    const parts = String(text).split(/(https?:\/\/\S+)/g);
    return parts
      .map((part) => {
        if (!/^https?:\/\//.test(part)) return escapeHtml(part);
        const trailingMatch = part.match(/^(https?:\/\/\S+?)([.,;:!?)]+)$/);
        if (trailingMatch) {
          const [, url, suffix] = trailingMatch;
          return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(url)}</a>${escapeHtml(suffix)}`;
        }
        return `<a href="${escapeHtml(part)}" target="_blank" rel="noopener">${escapeHtml(part)}</a>`;
      })
      .join("");
  }

  function toast(el, text, type = "success") {
    el.innerHTML = formatToastText(text);
    el.className = `toast ${type}`;
    el.classList.remove("hidden");
  }

  function hideToast(el) {
    el.classList.add("hidden");
    el.textContent = "";
  }

  const DISCLAIMER_PHRASE = "I AGREE";
  const DISCLAIMER_ALLOWED = new Set(["I", "A", "G", "R", "E", " "]);

  function resetDisclaimerAgree() {
    const input = $("#disclaimer-agree-input");
    const btn = $("#disclaimer-accept");
    if (input) input.value = "";
    if (btn) btn.disabled = true;
  }

  function syncDisclaimerAccept() {
    const input = $("#disclaimer-agree-input");
    const btn = $("#disclaimer-accept");
    if (!input || !btn) return;
    btn.disabled = input.value !== DISCLAIMER_PHRASE;
  }

  function onDisclaimerAgreeInput() {
    const input = $("#disclaimer-agree-input");
    if (!input) return;
    const filtered = input.value
      .toUpperCase()
      .split("")
      .filter((ch) => DISCLAIMER_ALLOWED.has(ch))
      .join("");
    if (filtered !== input.value) input.value = filtered;
    syncDisclaimerAccept();
  }

  function showDisclaimer(email) {
    $("#disclaimer-email").textContent = email;
    const modal = $("#disclaimer-modal");
    modal.classList.remove("hidden");
    injectIcons();
    resetDisclaimerAgree();
    $("#disclaimer-agree-input")?.focus();
  }

  function hideDisclaimer() {
    $("#disclaimer-modal").classList.add("hidden");
  }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function escapeRegex(s) {
    return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function formatEmailBodyHtml(text) {
    return escapeHtml(String(text || "")).replace(/\n/g, "<br>");
  }

  function highlightSubstitutions(text, recipient) {
    const isAi = state.composeMode === "ai" || recipient?.ai_generated;
    const values = [];
    const person = String(recipient?.person_name || "").trim();
    if (person) {
      values.push(person);
      const first = person.split(/\s+/)[0]?.replace(/[,.]$/, "");
      if (first && first.length >= 2 && first !== person) values.push(first);
    }
    if (!isAi) {
      const company = String(recipient?.company_name || "").trim();
      if (company) values.push(company);
    }

    values.sort((a, b) => b.length - a.length);

    if (!values.length) return formatEmailBodyHtml(text);

    let html = formatEmailBodyHtml(text);
    for (const val of values) {
      const escaped = escapeHtml(val);
      html = html.replace(
        new RegExp(escapeRegex(escaped), "g"),
        `<span class="ph-highlight">${escaped}</span>`
      );
    }
    return html;
  }

  function mentionsAttachment() {
    return ATTACH_PATTERN.test(`${$("#subject").value} ${$("#body").value}`);
  }

  function checkAttachReminder() {
    const show = mentionsAttachment() && !state.hasAttachment;
    $("#attach-reminder").classList.toggle("hidden", !show);
  }

  function updateAttachZone() {
    const zone = $("#attach-zone");
    const title = $("#attach-zone-title");
    const meta = $("#attach-zone-meta");

    if (state.hasAttachment) {
      zone.classList.add("has-file");
      title.textContent = state.attachmentName;
      meta.textContent = "Click to replace file";
      $("#remove-attachment").classList.remove("hidden");
    } else {
      zone.classList.remove("has-file");
      title.textContent = "Add attachment (optional)";
      meta.textContent = 'Only needed if you write "attach", "attached", "attachment", etc.';
      $("#remove-attachment").classList.add("hidden");
    }
    checkAttachReminder();
  }

  function updateWizardLock() {
    document.querySelectorAll(".wizard-step").forEach((btn) => {
      const step = Number(btn.dataset.step);
      const locked = step > 1 && !state.gmailVerified;
      btn.classList.toggle("locked", locked);
      btn.disabled = locked;
      btn.setAttribute("aria-disabled", locked ? "true" : "false");
    });
  }

  function requireGmailVerified() {
    if (state.gmailVerified) return true;
    goToStep(1);
    toast($("#mail-status"), "Connect and verify your email account to continue.", "error");
    return false;
  }

  function handleAuthRequired(res, data) {
    if (res.status !== 401 || (data?.code !== "mail_required" && data?.code !== "gmail_required")) return false;
    state.gmailVerified = false;
    updateWizardLock();
    updateUserSidebar();
    goToStep(1);
    toast($("#mail-status"), data.error || "Connect your email account first to continue.", "error");
    return true;
  }

  function setSendStepVisible(visible) {
    const sendStep = document.querySelector(".wizard-step-send");
    const wizard = $("#steps");
    if (!sendStep || !wizard) return;
    sendStep.classList.toggle("hidden", !visible);
    wizard.classList.toggle("has-send-step", visible);
    if (!visible) sendStep.classList.remove("done", "active");
  }

  function markSendingWizardComplete() {
    const step5 = document.querySelector('.wizard-step[data-step="5"]');
    if (!step5) return;
    step5.classList.remove("active");
    step5.classList.add("done");
  }

  function resetCampaignPage() {
    document.querySelector(".campaign-page")?.classList.remove("is-complete");
    $("#campaign-done-actions")?.classList.add("hidden");
    $("#ring-sublabel").textContent = "Sending";
    $("#ring-percent").textContent = "0%";
    $("#campaign-title").textContent = "Sending your emails…";
    $("#campaign-subtitle").textContent = "Approved emails are sent in the background.";
    $("#campaign-last").textContent = "";
    $("#stat-sent").textContent = "0";
    $("#stat-queued").textContent = "0";
    $("#stat-failed").textContent = "0";
    const ring = $("#ring-fill");
    if (ring) ring.style.strokeDashoffset = String(RING_CIRCUMFERENCE);
  }

  function goToStep(n) {
    if (n > 1 && !state.gmailVerified) {
      n = 1;
    }

    document.querySelectorAll(".stage").forEach((p) => p.classList.remove("active"));
    document.querySelectorAll(".wizard-step").forEach((s) => s.classList.remove("active", "done"));
    document.getElementById(`panel-${n}`).classList.add("active");
    document.querySelector(`.wizard-step[data-step="${n}"]`).classList.add("active");
    for (let i = 1; i < n; i++) {
      document.querySelector(`.wizard-step[data-step="${i}"]`).classList.add("done");
    }
    if (n === 3) {
      initComposeStep();
      checkAttachReminder();
    }
    if (n === 4) loadReview();
    if (n === 5) {
      setSendStepVisible(true);
      startCampaignPolling();
      refreshCampaignUI();
    }
    updateWizardLock();
    updateSidebarNav(n);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  document.querySelectorAll(".wizard-step").forEach((btn) => {
    btn.addEventListener("click", () => {
      const step = Number(btn.dataset.step);
      if (step > 1 && !state.gmailVerified) {
        requireGmailVerified();
        return;
      }
      goToStep(step);
    });
  });

  async function fetchStatus() {
    return (await fetch("/api/status")).json();
  }

  function renderPlaceholderPanel(fields) {
    state.placeholderFields = fields;
    const panel = $("#placeholder-panel");
    if (!fields) {
      panel.innerHTML = `<p id="placeholder-empty" class="aside-empty">Upload contacts to configure placeholders.</p>`;
      return;
    }

    const block = (key, token, field) => {
      if (!field?.has_column) return "";

      if (field.has_empty) {
        const inputId = key === "person_name" ? "fallback-person" : "fallback-company";
        const placeholder = key === "person_name" ? "e.g. there, Hiring Manager" : "e.g. your company, the team";
        const stored = key === "person_name" ? state.fallbackPerson : state.fallbackCompany;
        const n = field.empty_count || 0;
        return `
          <div class="pf-block pf-fallback">
            <span class="pf-badge pf-badge-warn">${n} empty cell${n === 1 ? "" : "s"}</span>
            <code>${token}</code>
            <p class="pf-desc">Column <strong>${escapeHtml(field.column)}</strong> has blank rows in your sheet.</p>
            <label for="${inputId}">Use this when the cell is empty</label>
            <input type="text" id="${inputId}" placeholder="${placeholder}" value="${escapeHtml(stored || "")}">
          </div>`;
      }

      return `
        <div class="pf-block pf-sheet">
          <span class="pf-badge">From sheet</span>
          <code>${token}</code>
          <p class="pf-desc">Column: <strong>${escapeHtml(field.column)}</strong> · all rows filled</p>
        </div>`;
    };

    const blocks = [
      block("person_name", TOKEN_PERSON, fields.person_name),
      block("company_name", TOKEN_COMPANY, fields.company_name),
    ].filter(Boolean);

    panel.innerHTML = blocks.length
      ? blocks.join("")
      : `<p class="aside-empty">No name or company columns found. Add those columns to your spreadsheet to use <code>${TOKEN_PERSON}</code> or <code>${TOKEN_COMPANY}</code>.</p>`;
  }

  function getFallbacks() {
    if (!state.placeholderFields) return {};
    const f = {};
    if (state.placeholderFields.person_name.has_empty) {
      f.person_name = ($("#fallback-person")?.value ?? state.fallbackPerson ?? "");
    }
    if (state.placeholderFields.company_name.has_empty) {
      f.company_name = ($("#fallback-company")?.value ?? state.fallbackCompany ?? "");
    }
    return f;
  }

  function payloadWithTemplate() {
    return {
      subject: $("#subject").value,
      body: $("#body").value,
      fallbacks: getFallbacks(),
    };
  }

  function getRecipientByEmail(email) {
    return state.recipients.find((r) => r.email === email);
  }

  function recipientIsUndeliverable(recipient) {
    if (!recipient) return false;
    if (recipient.generation_failed) return true;
    const subject = (recipient.subject || "").trim().toLowerCase();
    const body = (recipient.body || "").trim().toLowerCase();
    if (subject === "(generation failed)" || subject === "generation failed") return true;
    if (body.startsWith("could not generate")) return true;
    return false;
  }

  function payloadForRecipient(recipient) {
    const base = {
      fallbacks: getFallbacks(),
      email: recipient.email,
      generation_failed: Boolean(recipient.generation_failed),
    };
    if (recipient.customized) {
      return {
        ...base,
        subject: recipient.subject,
        body: recipient.body,
        customized: true,
      };
    }
    return { ...payloadWithTemplate(), email: recipient.email };
  }

  function usesToken(text, token) {
    return text.includes(token);
  }

  function validateFallbacks(subject, body) {
    if (!state.placeholderFields) return null;
    const f = getFallbacks();
    const text = subject + body;
    if (usesToken(text, TOKEN_PERSON) && state.placeholderFields.person_name.has_empty && !f.person_name.trim()) {
      const n = state.placeholderFields.person_name.empty_count;
      return `Set a value for empty name cells (${n} row${n === 1 ? "" : "s"}) in the sidebar, or remove ${TOKEN_PERSON} from your template.`;
    }
    if (usesToken(text, TOKEN_COMPANY) && state.placeholderFields.company_name.has_empty && !f.company_name.trim()) {
      const n = state.placeholderFields.company_name.empty_count;
      return `Set a value for empty company cells (${n} row${n === 1 ? "" : "s"}) in the sidebar, or remove ${TOKEN_COMPANY} from your template.`;
    }
    if (usesToken(text, TOKEN_PERSON) && !state.placeholderFields.person_name.has_column) {
      return `${TOKEN_PERSON} requires a name column in your spreadsheet.`;
    }
    if (usesToken(text, TOKEN_COMPANY) && !state.placeholderFields.company_name.has_column) {
      return `${TOKEN_COMPANY} requires a company column in your spreadsheet.`;
    }
    return null;
  }

  function renderPlaceholders(placeholders, fields) {
    renderPlaceholderPanel(fields || state.placeholderFields);
  }

  function formatDisplayName(email) {
    if (!email) return "User";
    const local = email.split("@")[0] || "User";
    return local
      .replace(/[._+-]+/g, " ")
      .split(" ")
      .filter(Boolean)
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
      .join(" ") || "User";
  }

  function updateUserSidebar() {
    const sidebar = $("#user-sidebar");
    const show = state.gmailVerified && !!state.gmailAddress;

    sidebar.classList.toggle("hidden", !show);
    if (!show) return;

    const name = formatDisplayName(state.gmailAddress);
    $("#sidebar-username").textContent = name;
    $("#sidebar-email").textContent = state.gmailAddress;
    $("#sidebar-avatar").textContent = (name[0] || "U").toUpperCase();
    injectIcons();
  }

  function updateSidebarNav(step) {
    document.querySelectorAll(".user-nav-item").forEach((btn) => {
      btn.classList.toggle("active", Number(btn.dataset.step) === step);
    });
  }

  const PREVIEW_ROWS = 10;

  function showAnalysisPanel(mode) {
    const aside = $("#import-analysis-aside");
    aside.classList.remove("is-loading", "is-ready", "is-idle");
    if (mode) aside.classList.add(mode);
    if (mode === "is-loading" || mode === "is-ready") {
      aside.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }

  function setAnalysisBadge(stateKey) {
    const badge = $("#analysis-badge");
    badge.className = "analysis-badge";
    if (stateKey === "ready") {
      badge.textContent = "Complete";
      badge.classList.add("ready");
    } else if (stateKey === "loading") {
      badge.textContent = "Analyzing";
      badge.classList.add("loading");
    } else {
      badge.textContent = "Pending";
    }
  }

  function setUploadContinueVisible(visible) {
    $("#upload-continue").classList.toggle("hidden", !visible);
  }

  function renderUploadAnalysis(data) {
    const panel = $("#upload-analysis-panel");
    const c = data.detected_columns;
    const rows = data.rows || [];
    const rowCount = data.row_count ?? data.selected_count ?? rows.length;

    setAnalysisBadge("ready");
    showAnalysisPanel("is-ready");

    const colRow = (label, fieldKey, cols) => {
      const col = cols[fieldKey];
      const meta = data.placeholder_fields?.[fieldKey];
      if (!col) {
        return `<li><span class="col-label">${label}</span><span><span class="badge badge-no">No column</span></span></li>`;
      }
      const emptyNote = meta?.has_empty
        ? ` <span class="col-empty-note">${meta.empty_count} empty</span>`
        : "";
      return `
        <li>
          <span class="col-label">${label}</span>
          <span>
            <span class="badge badge-ok">Found</span>
            <span class="col-value">${escapeHtml(col)}</span>${emptyNote}
          </span>
        </li>`;
    };

    panel.innerHTML = `
      <div class="analysis-stat-row">
        <div class="analysis-stat">
          <span class="analysis-stat-value">${data.selected_count}</span>
          <span class="analysis-stat-label">Contacts ready</span>
        </div>
        <div class="analysis-stat">
          <span class="analysis-stat-value">${data.total_valid_emails ?? data.selected_count}</span>
          <span class="analysis-stat-label">Valid emails</span>
        </div>
      </div>

      ${data.truncated ? `<div class="analysis-alert">Batch capped at ${window.APP_CONFIG.maxRecipients} contacts.</div>` : ""}

      ${(() => {
        const fill = data.company_fill || {};
        const fillStats = data.company_fill_stats || {};
        const showFill = fill.needs_fill && fill.inferrable_count > 0;
        const showDownload = data.company_names_filled || (fillStats.filled || 0) > 0;
        let html = "";
        if (showFill) {
          const missingNote = fill.missing_column
            ? "No company column detected."
            : `${fill.empty_count} contact${fill.empty_count === 1 ? "" : "s"} missing a company name.`;
          html += `
      <div class="analysis-fill-callout">
        <p class="analysis-fill-callout-title">Company names missing</p>
        <p class="field-hint">${missingNote} We can infer ${fill.inferrable_count} from work email domains (rows that already have a company are left unchanged).</p>
        <button type="button" class="btn btn-brand" id="fill-companies-btn">Fill company names from emails</button>
      </div>`;
        } else if (fill.needs_fill && fill.empty_count && !fill.inferrable_count) {
          html += `
      <div class="analysis-alert">${fill.empty_count} contact${fill.empty_count === 1 ? "" : "s"} missing company names, but none use a work email domain we can infer from (e.g. Gmail).</div>`;
        }
        if (showDownload) {
          html += `
      <div class="analysis-download-actions">
        <p class="field-hint">${fillStats.filled ? `${fillStats.filled} company name${fillStats.filled === 1 ? "" : "s"} added.` : "Download your spreadsheet with the current company names."}${fillStats.skipped ? ` ${fillStats.skipped} skipped.` : ""}</p>
        <div class="analysis-download-buttons">
          <button type="button" class="btn btn-soft btn-sm" id="download-sheet-xlsx">Download .xlsx</button>
          <button type="button" class="btn btn-soft btn-sm" id="download-sheet-csv">Download .csv</button>
        </div>
      </div>`;
        }
        return html;
      })()}

      <p class="analysis-section-title">Column mapping</p>
      <ul class="analysis-columns">
        ${colRow("Email", "email", c)}
        ${colRow("Name", "person_name", c)}
        ${colRow("Company", "company_name", c)}
        ${colRow("Company about", "company_about", c)}
      </ul>

      ${data.sheet_about_count ? `<div class="analysis-alert analysis-alert-info">${data.sheet_about_count} contact${data.sheet_about_count === 1 ? "" : "s"} include company about text — web crawling will be skipped for those.</div>` : ""}

      ${rows.length ? `
        <p class="analysis-section-title">Preview</p>
        <div class="analysis-table-wrap">
          <table class="analysis-table">
            <thead><tr><th>Email</th><th>Name</th><th>Company</th></tr></thead>
            <tbody>
              ${rows.slice(0, PREVIEW_ROWS).map((r) => `
                <tr>
                  <td title="${escapeHtml(r.email)}">${escapeHtml(r.email)}</td>
                  <td title="${escapeHtml(r.person_name || "—")}">${escapeHtml(r.person_name || "—")}</td>
                  <td title="${escapeHtml(r.company_name || "—")}">${escapeHtml(r.company_name || "—")}</td>
                </tr>`).join("")}
            </tbody>
          </table>
        </div>
        ${rowCount > PREVIEW_ROWS ? `<p class="analysis-more">+ ${rowCount - PREVIEW_ROWS} more in batch</p>` : ""}
      ` : ""}`;

    setUploadContinueVisible(true);
    state.uploadAnalysis = { ...data, rows: data.rows || [] };
    injectIcons();
  }

  function restoreUploadAnalysis(summary) {
    if (!summary) return;
    renderUploadAnalysis({
      detected_columns: summary.detected_columns,
      selected_count: summary.selected_count,
      total_valid_emails: summary.total_valid_emails,
      truncated: summary.truncated,
      rows: summary.rows || [],
      row_count: summary.row_count ?? summary.selected_count,
      placeholder_fields: summary.placeholder_fields,
      sheet_about_count: summary.sheet_about_count,
      company_fill: summary.company_fill,
      company_names_filled: summary.company_names_filled,
      company_fill_stats: summary.company_fill_stats,
    });
  }

  function getSenderInitial() {
    return (state.gmailAddress[0] || "Y").toUpperCase();
  }

  function formatGmailDate() {
    return new Date().toLocaleString(undefined, {
      month: "short", day: "numeric", year: "numeric",
      hour: "numeric", minute: "2-digit",
    });
  }

  function buildInboxCard(r) {
    const sender = state.gmailAddress || "you@gmail.com";
    const senderName = sender.split("@")[0].replace(/[._]/g, " ");
    const failed = recipientIsUndeliverable(r);
    const failedBanner = failed
      ? `<div class="gmail-failed-banner">Drafting failed for this email. Use <strong>Regenerate</strong> or <strong>Edit</strong> before approving — failed drafts cannot be sent.</div>`
      : "";
    const files = state.hasAttachment
      ? `<div class="gmail-open-files">
          <div class="gmail-files-label">${ICONS.paperclip} One attachment</div>
          <div class="gmail-file-card">
            <div class="gmail-file-icon">${ICONS.file}</div>
            <div class="gmail-file-meta">
              <span class="gmail-file-name">${escapeHtml(state.attachmentName)}</span>
              <span class="gmail-file-type">PDF / Document</span>
            </div>
          </div>
        </div>`
      : "";

    return `
      <div class="gmail-open">
        <div class="gmail-open-toolbar">
          <button type="button" class="gmail-ico-btn" aria-label="Back">${ICONS.back}</button>
          <div class="gmail-open-toolbar-actions">
            <button type="button" class="gmail-ico-btn" aria-label="Archive">${ICONS.archive}</button>
            <button type="button" class="gmail-ico-btn" aria-label="Delete">${ICONS.trash}</button>
            <button type="button" class="gmail-ico-btn" aria-label="Label">${ICONS.label}</button>
            <button type="button" class="gmail-ico-btn" aria-label="More">${ICONS.more}</button>
          </div>
        </div>

        <div class="gmail-open-subject-wrap">
          <h2 class="gmail-open-subject">${highlightSubstitutions(r.subject, r)}</h2>
          ${failed ? '<span class="gmail-open-tag gmail-failed-tag">Generation failed</span>' : ""}
          ${r.customized && !failed ? '<span class="gmail-open-tag gmail-custom-tag">Customized</span>' : ""}
          <span class="gmail-open-tag">Inbox</span>
        </div>

        ${failedBanner}

        <div class="gmail-open-sender">
          <div class="gmail-open-sender-left">
            <div class="gmail-avatar">${getSenderInitial()}</div>
            <div class="gmail-sender-block">
              <div class="gmail-sender-line">
                <span class="gmail-sender-name">${escapeHtml(senderName)}</span>
                <span class="gmail-sender-addr">&lt;${escapeHtml(sender)}&gt;</span>
              </div>
              <div class="gmail-recipient-line">
                to <span class="gmail-recipient">${escapeHtml(r.email)}</span>
                <span class="gmail-caret">▾</span>
              </div>
            </div>
          </div>
          <div class="gmail-open-sender-right">
            <span class="gmail-open-date">${formatGmailDate()}</span>
            <div class="gmail-open-sender-icons">
              <button type="button" class="gmail-ico-btn sm" aria-label="Star">${ICONS.star}</button>
              <button type="button" class="gmail-ico-btn sm" aria-label="Reply">${ICONS.reply}</button>
              <button type="button" class="gmail-ico-btn sm" aria-label="More">${ICONS.more}</button>
            </div>
          </div>
        </div>

        <div class="gmail-open-body">${highlightSubstitutions(r.body, r)}</div>
        ${files}

        <div class="gmail-open-reply-bar">
          <button type="button" class="gmail-reply-btn">${ICONS.reply} Reply</button>
          <button type="button" class="gmail-reply-btn">${ICONS.forward} Forward</button>
        </div>
      </div>`;
  }

  function pendingRecipients() {
    return state.recipients.filter(
      (r) => !state.approved.has(r.email) && !state.rejected.has(r.email)
    );
  }

  function updateReviewActions() {
    const pending = pendingRecipients();
    const current = pending[0];
    const undeliverable = recipientIsUndeliverable(current);
    const approveBtn = $("#btn-approve");
    if (approveBtn) {
      approveBtn.disabled = undeliverable || state.animating || state.regenerating || !pending.length;
      approveBtn.title = undeliverable
        ? "This draft failed to generate. Regenerate or edit it before approving."
        : "";
    }
    const sendablePending = pending.filter((r) => !recipientIsUndeliverable(r));
    const approveAllBtn = $("#approve-all-btn");
    if (approveAllBtn) {
      approveAllBtn.disabled = sendablePending.length === 0;
      approveAllBtn.title =
        pending.length && sendablePending.length === 0
          ? "No sendable drafts in the queue — regenerate or skip failed emails."
          : "";
    }
  }

  function updateReviewStats() {
    const total = state.recipients.length;
    const approved = state.approved.size;
    const rejected = state.rejected.size;
    const pending = total - approved - rejected;

    $("#approved-count").textContent = approved;
    $("#rejected-count").textContent = rejected;
    $("#pending-count").textContent = pending;
    $("#review-counter").textContent = `${approved + rejected} / ${total}`;
    $("#review-progress-fill").style.width = total ? `${((approved + rejected) / total) * 100}%` : "0%";
    $("#approve-all-btn").disabled = pending === 0;
    $("#approve-all-btn").classList.toggle("hidden", pending === 0);
    updateReviewActions();
  }

  function showReviewComplete() {
    $("#gmail-card").classList.add("hidden");
    $("#review-actions").classList.add("hidden");
    $("#approve-all-btn").classList.add("hidden");
    $("#review-done").classList.remove("hidden");

    const n = state.approved.size;
    if (n === 0) {
      $("#review-done-text").textContent = state.composeMode === "ai"
        ? "No emails approved. Go back to review or regenerate drafts."
        : "No emails approved. Go back to edit your template.";
      return;
    }

    $("#review-done-text").textContent = `Sending ${n} approved email${n === 1 ? "" : "s"}…`;
    setSendStepVisible(true);
    goToStep(5);
  }

  async function approveAndSend(recipient) {
    const r = typeof recipient === "string" ? getRecipientByEmail(recipient) : recipient;
    if (!r) return false;
    if (recipientIsUndeliverable(r)) {
      toast($("#review-errors"), "This email failed to generate. Regenerate or edit it before sending.", "error");
      state.approved.delete(r.email);
      return false;
    }
    const res = await fetch("/api/campaign/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payloadForRecipient(r)),
    });
    const data = await res.json();
    if (handleAuthRequired(res, data)) return false;
    if (!data.ok) {
      toast($("#review-errors"), data.errors?.join(" ") || data.error, "error");
      state.approved.delete(r.email);
      return false;
    }
    setSendStepVisible(true);
    $("#review-send-badge").classList.remove("hidden");
    startCampaignPolling();
    updateCampaignUI(data);
    return true;
  }

  function updateCampaignUI(snap) {
    if (!snap) return;
    const total = snap.approved || 0;
    const done = snap.completed || 0;
    const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;

    const ring = $("#ring-fill");
    if (ring) {
      ring.style.strokeDashoffset = String(RING_CIRCUMFERENCE * (1 - done / Math.max(total, 1)));
    }
    $("#ring-percent").textContent = `${pct}%`;
    $("#stat-sent").textContent = snap.sent ?? 0;
    $("#stat-queued").textContent = (snap.queued ?? 0) + (snap.in_flight ?? 0);
    $("#stat-failed").textContent = snap.failed ?? 0;

    const inProgress = snap.in_flight > 0 || snap.queued > 0;
    const isDone = snap.status === "done" || (total > 0 && done >= total && !inProgress);

    if (isDone) {
      $("#ring-sublabel").textContent = "Complete";
      $("#campaign-title").textContent = snap.failed > 0
        ? "Campaign finished"
        : "Congratulations! All emails sent.";
      $("#campaign-subtitle").textContent = snap.failed > 0
        ? `${snap.sent} delivered · ${snap.failed} failed`
        : `Successfully delivered ${snap.sent} email${snap.sent === 1 ? "" : "s"}.`;
      document.querySelector(".campaign-page")?.classList.add("is-complete");
      $("#campaign-done-actions")?.classList.remove("hidden");
      $("#campaign-last").textContent = "";
      markSendingWizardComplete();
      $("#review-send-badge")?.classList.add("done");
      $("#review-send-badge-text").textContent = `${snap.sent} sent`;
      stopCampaignPolling();
      refreshQuota();
    } else {
      $("#ring-sublabel").textContent = snap.in_flight > 0 ? "Sending" : "Queued";
      $("#campaign-title").textContent = "Sending your emails…";
      $("#campaign-subtitle").textContent = `${done} of ${total} complete · ${snap.in_flight} in progress`;
      document.querySelector(".campaign-page")?.classList.remove("is-complete");
      $("#campaign-done-actions")?.classList.add("hidden");
      if (snap.last_email) {
        $("#campaign-last").textContent = `Last: ${snap.last_email}`;
      }
      $("#review-send-badge-text").textContent = `${snap.sent} sent · ${snap.queued + snap.in_flight} queued`;
    }
  }

  async function refreshCampaignUI() {
    const res = await fetch("/api/campaign/status");
    const data = await res.json();
    if (data.ok && data.approved > 0) updateCampaignUI(data);
  }

  function startCampaignPolling() {
    if (state.campaignPoll) return;
    state.campaignPoll = setInterval(async () => {
      const res = await fetch("/api/campaign/status");
      const data = await res.json();
      if (data.ok) updateCampaignUI(data);
    }, 800);
  }

  function stopCampaignPolling() {
    if (state.campaignPoll) {
      clearInterval(state.campaignPoll);
      state.campaignPoll = null;
    }
  }

  function updateRegenerateButton(recipient) {
    const btn = $("#btn-regenerate");
    if (!btn) return;
    const show = state.composeMode === "ai";
    btn.classList.toggle("hidden", !show);
    if (!show) return;
    btn.disabled = state.regenerating;
    if (recipient?.generation_failed) {
      btn.classList.add("decision-regenerate--urgent");
    } else {
      btn.classList.remove("decision-regenerate--urgent");
    }
  }

  function setRegenerateOverlay(visible, label = "Rewriting…") {
    const stage = $("#card-stage");
    const overlay = $("#regen-overlay");
    const labelEl = $("#regen-label");
    const statusEl = $("#regen-status");
    const cancelBtn = $("#regen-cancel");
    if (stage) stage.classList.toggle("is-regenerating", visible);
    if (overlay) overlay.classList.toggle("hidden", !visible);
    if (labelEl) labelEl.textContent = label;
    if (statusEl && !visible) statusEl.textContent = "";
    if (cancelBtn) cancelBtn.disabled = false;
  }

  function stopRegenPoll() {
    if (state.regenPoll) {
      clearInterval(state.regenPoll);
      state.regenPoll = null;
    }
  }

  function finishRegenerateUi() {
    stopRegenPoll();
    state.regenerating = false;
    state.regenJobId = null;
    state.regenRejectAfterCancel = false;
    setRegenerateOverlay(false);
    const pending = pendingRecipients();
    updateRegenerateButton(pending[0]);
  }

  async function cancelRegenerate() {
    if (!state.regenJobId) return;
    const cancelBtn = $("#regen-cancel");
    if (cancelBtn) cancelBtn.disabled = true;
    const statusEl = $("#regen-status");
    if (statusEl) statusEl.textContent = "Cancelling…";
    try {
      await fetch("/api/ai/regenerate/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: state.regenJobId }),
      });
    } catch {
      /* polling will pick up cancelled/error */
    }
  }

  async function pollRegenerateStatus() {
    if (!state.regenJobId) return;
    const res = await fetch(`/api/ai/regenerate/status?job_id=${encodeURIComponent(state.regenJobId)}`);
    const data = await res.json();
    if (!data.ok) return;

    const statusEl = $("#regen-status");
    if (statusEl && data.status_note) statusEl.textContent = data.status_note;

    if (data.status === "running") return;

    const pending = pendingRecipients();
    const recipient = pending[0];
    const rejectAfter = state.regenRejectAfterCancel;

    if (data.status === "done" && recipient && !rejectAfter) {
      Object.assign(recipient, data.recipient);
      finishRegenerateUi();
      showCurrentCard();
      return;
    }

    if (data.status === "cancelled" && rejectAfter && recipient) {
      finishRegenerateUi();
      animateDecision("reject", recipient.email, showCurrentCard);
      return;
    }

    finishRegenerateUi();
    if (data.status === "error" && !rejectAfter) {
      const msg = data.error || "Could not regenerate email.";
      toast($("#review-errors"), msg, "error");
    }
    if (data.status === "done" && rejectAfter) {
      /* user moved on while regen finished — ignore result */
    }
  }

  function startRegenPoll() {
    stopRegenPoll();
    state.regenPoll = setInterval(pollRegenerateStatus, 1000);
    pollRegenerateStatus();
  }

  async function regenerateCurrentEmail() {
    const pending = pendingRecipients();
    if (!pending.length || state.regenerating || state.animating) return;

    const recipient = pending[0];
    state.regenerating = true;
    state.regenRejectAfterCancel = false;
    updateRegenerateButton(recipient);
    setRegenerateOverlay(true);
    hideToast($("#review-errors"));

    try {
      const res = await fetch("/api/ai/regenerate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: recipient.email }),
      });
      const data = await res.json();
      if (handleAuthRequired(res, data)) {
        finishRegenerateUi();
        return;
      }
      if (!data.ok) {
        finishRegenerateUi();
        toast($("#review-errors"), data.error || "Could not start regenerate.", "error");
        return;
      }
      state.regenJobId = data.job_id;
      startRegenPoll();
    } catch {
      finishRegenerateUi();
      toast($("#review-errors"), "Could not start regenerate.", "error");
    }
  }

  function rejectDuringRegenerate() {
    const pending = pendingRecipients();
    if (!pending.length || state.animating) return;
    if (!state.regenerating) {
      animateDecision("reject", pending[0].email, showCurrentCard);
      return;
    }
    state.regenRejectAfterCancel = true;
    setRegenerateOverlay(true, "Cancelling rewrite…");
    cancelRegenerate();
  }

  function showCurrentCard() {
    const pending = pendingRecipients();
    const card = $("#gmail-card");
    const stage = $("#card-stage");

    if (!pending.length) {
      updateReviewStats();
      showReviewComplete();
      return;
    }

    $("#review-done").classList.add("hidden");
    $("#review-actions").classList.remove("hidden");

    const current = pending[0];
    card.className = "inbox-card";
    card.innerHTML = buildInboxCard(current);
    card.classList.remove("hidden");
    injectIcons();
    stage.querySelectorAll(".approve-flash").forEach((f) => f.remove());
    updateRegenerateButton(current);
    updateReviewStats();
  }

  function animateDecision(type, email, callback) {
    if (state.animating) return;
    state.animating = true;

    const card = $("#gmail-card");
    const stage = $("#card-stage");

    if (type === "approve") {
      const recipient = getRecipientByEmail(email);
      if (recipientIsUndeliverable(recipient)) {
        state.animating = false;
        toast($("#review-errors"), "This email failed to generate. Regenerate or edit it before sending.", "error");
        callback();
        return;
      }
      const flash = document.createElement("div");
      flash.className = "approve-flash";
      flash.innerHTML = `${ICONS.check} Approved & sending`;
      stage.appendChild(flash);
      card.classList.add("slide-approve");
      state.approved.add(email);
      if (recipient) approveAndSend(recipient);
    } else {
      card.classList.add("slide-reject");
      state.rejected.add(email);
    }

    setTimeout(() => {
      state.animating = false;
      card.classList.remove("slide-approve", "slide-reject");
      callback();
    }, 500);
  }

  function collectCustomOverrides() {
    const overrides = {};
    for (const r of state.recipients) {
      if (r.customized) {
        overrides[r.email] = {
          subject: r.subject,
          body: r.body,
          customized: true,
        };
      }
    }
    return overrides;
  }

  function applyCustomOverrides(recipients, overrides) {
    return recipients.map((r) => {
      const o = overrides[r.email];
      return o ? { ...r, ...o } : r;
    });
  }

  function showEditChoiceModal(recipient) {
    state.editingRecipientEmail = recipient.email;
    if (state.composeMode === "ai") {
      showEmailEditModal(recipient);
      return;
    }
    $("#edit-choice-email").textContent = recipient.email;
    $("#edit-choice-modal").classList.remove("hidden");
    injectIcons();
  }

  function hideEditChoiceModal() {
    $("#edit-choice-modal").classList.add("hidden");
    state.editingRecipientEmail = null;
  }

  function showEmailEditModal(recipient) {
    state.editingRecipientEmail = recipient.email;
    $("#email-edit-recipient").textContent = recipient.email;
    $("#email-edit-subject").value = recipient.subject;
    $("#email-edit-body").value = recipient.body;
    hideToast($("#email-edit-error"));
    $("#email-edit-modal").classList.remove("hidden");
    $("#email-edit-subject").focus();
  }

  function hideEmailEditModal() {
    $("#email-edit-modal").classList.add("hidden");
    state.editingRecipientEmail = null;
  }

  function saveEmailEdit() {
    const email = state.editingRecipientEmail;
    const recipient = email ? getRecipientByEmail(email) : null;
    if (!recipient) return;

    const subject = $("#email-edit-subject").value.trim();
    const body = $("#email-edit-body").value.trim();
    const errEl = $("#email-edit-error");

    if (!subject || !body) {
      toast(errEl, "Subject and message are required.", "error");
      return;
    }

    recipient.subject = subject;
    recipient.body = body;
    recipient.customized = true;
    recipient.generation_failed = false;
    hideEmailEditModal();
    showCurrentCard();
  }

  async function loadReview() {
    hideToast($("#review-errors"));
    stopCampaignPolling();
    stopAiGenPoll();
    if (state.regenJobId) await cancelRegenerate();
    finishRegenerateUi();
    await fetch("/api/campaign/reset", { method: "POST" });
    $("#review-send-badge").classList.add("hidden");
    setSendStepVisible(false);
    resetCampaignPage();

    $("#review-loading").classList.remove("hidden");
    $("#gmail-card").classList.add("hidden");
    $("#review-done").classList.add("hidden");

    const overrides = collectCustomOverrides();
    const payload = state.composeMode === "ai"
      ? { use_ai_drafts: true }
      : payloadWithTemplate();

    const res = await fetch("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    $("#review-loading").classList.add("hidden");

    if (handleAuthRequired(res, data)) return;

    if (!data.ok) {
      toast($("#review-errors"), data.errors?.join(" ") || data.error, "error");
      return;
    }

    state.recipients = applyCustomOverrides(data.recipients, overrides);
    state.approved = new Set();
    state.rejected = new Set();
    state.partialReview = !!data.partial_review;

    const banner = $("#review-partial-banner");
    if (banner) {
      if (state.reviewNotice) {
        banner.textContent = state.reviewNotice;
        banner.classList.remove("hidden");
        banner.classList.add("toast-error");
      } else if (data.quota_exhausted && data.quota_message) {
        banner.textContent = data.quota_message;
        banner.classList.remove("hidden");
        banner.classList.add("toast-error");
      } else if (state.partialReview && data.generated_ok != null) {
        const count = data.generated_ok;
        const remaining = data.remaining_count ?? 0;
        banner.textContent = remaining > 0
          ? `${count} email${count === 1 ? "" : "s"} generated · ${remaining} remaining — download below to resume later.`
          : count === 1
            ? "Showing 1 generated email from this run."
            : `Showing ${count} generated emails from this run.`;
        banner.classList.remove("hidden", "toast-error");
        if (data.quota_exhausted) banner.classList.add("toast-error");
      } else {
        banner.classList.add("hidden");
        banner.textContent = "";
      }
    }
    showReviewRemainingActions(data);
    state.reviewNotice = "";

    showCurrentCard();
  }

  async function validateCompose() {
    const errEl = $("#template-errors");
    hideToast(errEl);

    const fbErr = validateFallbacks($("#subject").value, $("#body").value);
    if (fbErr) {
      toast(errEl, fbErr, "error");
      return false;
    }

    const res = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payloadWithTemplate()),
    });
    const data = await res.json();

    if (handleAuthRequired(res, data)) return false;

    if (!data.ok) {
      toast(errEl, data.errors.join(" "), "error");
      return false;
    }

    if (mentionsAttachment() && !state.hasAttachment) {
      toast(errEl, "Upload a file or remove words like attach, attached, attachment.", "error");
      $("#attach-reminder").classList.remove("hidden");
      return false;
    }

    return true;
  }

  async function uploadAttachment(file) {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch("/api/attachment", { method: "POST", body: fd });
    const data = await res.json();
    if (handleAuthRequired(res, data)) return;
    if (!data.ok) {
      alert(data.error);
      return;
    }
    state.hasAttachment = true;
    state.attachmentName = data.filename;
    markSessionActive();
    updateAttachZone();
  }

  async function removeAttachment() {
    await fetch("/api/attachment", { method: "DELETE" });
    state.hasAttachment = false;
    state.attachmentName = null;
    $("#attachment-file").value = "";
    updateAttachZone();
  }

  document.querySelectorAll(".user-nav-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      const step = Number(btn.dataset.step);
      if (step > 1 && !state.gmailVerified) {
        requireGmailVerified();
        return;
      }
      goToStep(step);
    });
  });

  // Sheet dropzone — label opens file picker natively; no extra click handler
  const sheetZone = $("#sheet-dropzone");
  const sheetInput = $("#sheet-file");

  sheetZone.addEventListener("dragover", (e) => { e.preventDefault(); sheetZone.classList.add("dragover"); });
  sheetZone.addEventListener("dragleave", () => sheetZone.classList.remove("dragover"));
  sheetZone.addEventListener("drop", (e) => {
    e.preventDefault();
    sheetZone.classList.remove("dragover");
    if (e.dataTransfer.files[0]) {
      sheetInput.files = e.dataTransfer.files;
      onSheetFilePicked(e.dataTransfer.files[0]);
    }
  });
  sheetInput.addEventListener("change", () => {
    if (sheetInput.files[0]) onSheetFilePicked(sheetInput.files[0]);
  });

  $("#upload-form").addEventListener("submit", (e) => {
    e.preventDefault();
    if (sheetInput.files[0]) analyzeSpreadsheetFile(sheetInput.files[0]);
  });

  $("#upload-continue").addEventListener("click", () => showComposeChoiceModal());

  // Attachment zone — label opens file picker natively
  const attachZone = $("#attach-zone");
  const attachInput = $("#attachment-file");

  attachInput.addEventListener("change", () => {
    if (attachInput.files[0]) uploadAttachment(attachInput.files[0]);
  });
  $("#remove-attachment").addEventListener("click", (e) => {
    e.stopPropagation();
    removeAttachment();
  });

  $("#subject").addEventListener("input", checkAttachReminder);
  $("#body").addEventListener("input", checkAttachReminder);

  const APP_PASSWORD_HINT = " · requires 2FA";

  const PROVIDER_UI = {
    gmail: {
      placeholder: "you@gmail.com",
      passwordUrl: "https://myaccount.google.com/apppasswords",
    },
    outlook: {
      placeholder: "you@outlook.com",
      passwordUrl: "https://account.microsoft.com/security",
    },
  };

  function passwordHintHtml(url) {
    return `<a href="${url}" target="_blank" rel="noopener">Generate app password</a>${APP_PASSWORD_HINT}`;
  }

  function setMailProvider(provider) {
    state.mailProvider = provider === "outlook" ? "outlook" : "gmail";
    document.querySelectorAll(".provider-tab").forEach((tab) => {
      const active = tab.dataset.provider === state.mailProvider;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    $("#connect-info-gmail")?.classList.toggle("hidden", state.mailProvider !== "gmail");
    $("#connect-info-outlook")?.classList.toggle("hidden", state.mailProvider !== "outlook");
    const ui = PROVIDER_UI[state.mailProvider];
    if ($("#mail-address")) $("#mail-address").placeholder = ui.placeholder;
    if ($("#mail-password-hint")) $("#mail-password-hint").innerHTML = passwordHintHtml(ui.passwordUrl);
  }

  function setMailConnectLoading(visible, label) {
    const panel = $("#mail-connect-loading");
    const submitBtn = $("#mail-submit-btn");
    const testBtn = $("#test-mail");
    const addressInput = $("#mail-address");
    const passwordInput = $("#mail-password");
    if (!panel) return;

    if (visible) {
      if (label && $("#mail-connect-loading-label")) {
        $("#mail-connect-loading-label").textContent = label;
      }
      panel.classList.remove("hidden");
      submitBtn?.classList.add("is-loading");
      testBtn?.classList.add("is-loading");
      if (submitBtn) submitBtn.disabled = true;
      if (testBtn) testBtn.disabled = true;
      if (addressInput) addressInput.disabled = true;
      if (passwordInput) passwordInput.disabled = true;
    } else {
      panel.classList.add("hidden");
      submitBtn?.classList.remove("is-loading");
      testBtn?.classList.remove("is-loading");
      if (submitBtn) submitBtn.disabled = false;
      if (testBtn) testBtn.disabled = false;
      if (addressInput) addressInput.disabled = false;
      if (passwordInput) passwordInput.disabled = false;
    }
  }

  function setGeminiKeyLoading(visible) {
    const panel = $("#llm-key-loading");
    const btn = $("#llm-key-continue");
    const cancel = $("#llm-key-cancel");
    const input = $("#llm-api-key");
    const secondaryInput = $("#llm-api-key-secondary");
    const multiAgentToggle = $("#llm-multi-agent");
    if (!panel) return;

    panel.classList.toggle("hidden", !visible);
    if (btn) {
      btn.disabled = visible;
      btn.classList.toggle("is-loading", visible);
      btn.textContent = visible ? "Checking…" : "Continue";
    }
    if (cancel) cancel.disabled = visible;
    if (input) input.disabled = visible;
    if (secondaryInput) secondaryInput.disabled = visible;
    if (multiAgentToggle) multiAgentToggle.disabled = visible;
    if (visible) hideToast($("#llm-key-error"));
  }

  function updateMultiAgentUi() {
    const enabled = !!$("#llm-multi-agent")?.checked;
    state.multiAgent = enabled;
    const wrap = $("#llm-secondary-key-wrap");
    wrap?.classList.toggle("hidden", !enabled);
    if (!enabled && $("#llm-api-key-secondary")) {
      $("#llm-api-key-secondary").value = "";
    }
  }

  const LLM_PROVIDER_COPY = {
    groq: {
      label: "Groq API key",
      placeholder: "gsk_…",
      hintHtml: '<a href="https://console.groq.com/keys" target="_blank" rel="noopener">Create a key in Groq Console</a>',
      freeNote:
        'No credit card required. Limits are per model. See <a href="https://console.groq.com/settings/limits" target="_blank" rel="noopener">your quota in Groq Console</a>.',
    },
    gemini: {
      label: "Gemini API key",
      placeholder: "AIza…",
      hintHtml: '<a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener">Create a key in Google AI Studio</a>',
      freeNote:
        "No credit card required. Many free projects are capped at about 20 requests per day, per model. Failed retries still count toward the limit.",
    },
  };

  function setAiProvider(provider) {
    state.aiProvider = provider === "groq" ? "groq" : "gemini";
    document.querySelectorAll("[data-llm-provider]").forEach((tab) => {
      const active = tab.dataset.llmProvider === state.aiProvider;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    const copy = LLM_PROVIDER_COPY[state.aiProvider];
    const label = $("#llm-api-key-label");
    const input = $("#llm-api-key");
    const hint = $("#llm-api-key-hint");
    const note = $("#llm-key-free-note");
    if (label) label.textContent = copy.label;
    if (input) input.placeholder = copy.placeholder;
    if (hint) hint.innerHTML = copy.hintHtml;
    if (note) note.innerHTML = copy.freeNote;
    updateComposeProviderLabel();
  }

  function updateComposeProviderLabel() {
    const el = $("#compose-ai-provider-label");
    if (!el) return;
    const name = state.aiProvider === "groq" ? "Groq" : "Gemini";
    const mode = state.multiAgent ? " · multi-agent orchestration" : "";
    el.textContent = `Connected via ${name}${mode}`;
  }

  function mailConnectPayload() {
    return {
      provider: state.mailProvider,
      email_address: $("#mail-address").value,
      app_password: $("#mail-password").value,
    };
  }

  document.querySelectorAll(".provider-tab").forEach((tab) => {
    tab.addEventListener("click", () => setMailProvider(tab.dataset.provider));
  });

  $("#mail-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const statusEl = $("#mail-status");
    hideToast(statusEl);

    const providerLabel = state.mailProvider === "outlook" ? "Outlook" : "Gmail";
    setMailConnectLoading(true, `Verifying ${providerLabel}…`);

    try {
      const res = await fetch("/api/mail", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(mailConnectPayload()),
      });
      const data = await res.json();
      if (data.ok) {
        state.gmailAddress = $("#mail-address").value.trim();
        state.gmailVerified = true;
        state.mailProvider = data.provider || state.mailProvider;
        markSessionActive();
        updateWizardLock();
        updateUserSidebar();
        toast(statusEl, data.message, "success");
        showDisclaimer(state.gmailAddress);
      } else {
        toast(statusEl, data.error, "error");
      }
    } finally {
      setMailConnectLoading(false);
    }
  });

  $("#test-mail").addEventListener("click", async () => {
    const statusEl = $("#mail-status");
    hideToast(statusEl);

    const providerLabel = state.mailProvider === "outlook" ? "Outlook" : "Gmail";
    setMailConnectLoading(true, `Testing ${providerLabel}…`);

    try {
      const res = await fetch("/api/mail/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(mailConnectPayload()),
      });
      const data = await res.json();
      toast(statusEl, data.message, data.ok ? "success" : "error");
    } finally {
      setMailConnectLoading(false);
    }
  });

  $("#disclaimer-agree-input")?.addEventListener("input", onDisclaimerAgreeInput);
  $("#disclaimer-agree-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && $("#disclaimer-agree-input")?.value === DISCLAIMER_PHRASE) {
      e.preventDefault();
      $("#disclaimer-accept")?.click();
    }
  });
  $("#disclaimer-agree-input")?.addEventListener("paste", (e) => {
    e.preventDefault();
    const pasted = (e.clipboardData?.getData("text") || "")
      .toUpperCase()
      .split("")
      .filter((ch) => DISCLAIMER_ALLOWED.has(ch))
      .join("");
    const input = $("#disclaimer-agree-input");
    if (input) {
      input.value = pasted;
      syncDisclaimerAccept();
    }
  });

  $("#disclaimer-accept").addEventListener("click", () => {
    if ($("#disclaimer-agree-input")?.value !== DISCLAIMER_PHRASE) return;
    hideDisclaimer();
    goToStep(2);
  });

  $("#disclaimer-cancel").addEventListener("click", hideDisclaimer);

  $("#disclaimer-modal").addEventListener("click", (e) => {
    if (e.target.id === "disclaimer-modal") hideDisclaimer();
  });

  async function analyzeSpreadsheetFile(file) {
    if (!file) return;
    if (!requireGmailVerified()) return;

    setAnalysisBadge("loading");
    showAnalysisPanel("is-loading");
    setUploadContinueVisible(false);
    $("#upload-analysis-panel").innerHTML = `
      <div class="analysis-loading">
        <span class="analysis-loading-spinner" aria-hidden="true"></span>
        <p>Analyzing <strong>${escapeHtml(file.name)}</strong>…</p>
      </div>`;

    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch("/api/upload", { method: "POST", body: fd });
    const data = await res.json();
    if (handleAuthRequired(res, data)) return;
    if (!data.ok) {
      setAnalysisBadge("pending");
      showAnalysisPanel("is-idle");
      setUploadContinueVisible(false);
      $("#upload-analysis-panel").innerHTML = `<p class="aside-empty">Analysis failed. Check your file and try again.</p>`;
      alert(data.error);
      return;
    }

    state.placeholders = data.placeholders;
    renderPlaceholderPanel(data.placeholder_fields);
    renderUploadAnalysis(data);
    markSessionActive();
  }

  function onSheetFilePicked(file) {
    if (!file) return;
    $("#sheet-filename").textContent = file.name;
    $("#sheet-filename").classList.remove("hidden");
    sheetZone.classList.add("has-file");
    analyzeSpreadsheetFile(file);
  }

  function showComposeChoiceModal() {
    const modal = $("#compose-choice-modal");
    modal.classList.remove("hidden");
    injectIcons();
    $("#compose-choice-manual").focus();
  }

  function hideComposeChoiceModal() {
    $("#compose-choice-modal").classList.add("hidden");
  }

  function showAiKeyModal() {
    const modal = $("#llm-key-modal");
    modal.classList.remove("hidden");
    injectIcons();
    hideToast($("#llm-key-error"));
    setGeminiKeyLoading(false);
    setAiProvider(state.aiProvider || "groq");
    const multiToggle = $("#llm-multi-agent");
    if (multiToggle) multiToggle.checked = !!state.multiAgent;
    updateMultiAgentUi();
    $("#llm-api-key").focus();
  }

  function hideAiKeyModal() {
    $("#llm-key-modal").classList.add("hidden");
  }

  function resetAiLoadingCancelButton() {
    state.aiCancelling = false;
    const btn = $("#ai-loading-cancel");
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Cancel";
    }
  }

  function showAiLoadingModal() {
    state.loadingModalMode = "ai";
    resetAiLoadingCancelButton();
    hideAiPartialStop();
    state.aiLastProgressKey = "";
    state.aiLastProgressAt = 0;
    const modal = $("#ai-loading-modal");
    modal.classList.remove("hidden");
    setAiLoadingPhase("scraping");
  }

  function showFillCompaniesModal(total) {
    state.loadingModalMode = "fill-companies";
    state.fillCancelling = false;
    resetAiLoadingCancelButton();
    const modal = $("#ai-loading-modal");
    modal.classList.remove("hidden");
    setAiLoadingPhase("scraping");
    const title = $("#ai-loading-title");
    const subtitle = $("#ai-loading-subtitle");
    if (title) title.textContent = "Finding company names";
    if (subtitle) subtitle.textContent = "Reading work email domains in your spreadsheet…";
    updateFillCompaniesProgress({ total: total || 0, completed: 0, status_note: "Starting…" });
  }

  function hideAiLoadingModal() {
    $("#ai-loading-modal").classList.add("hidden");
  }

  function setAiLoadingPhase(phase) {
    state.aiPipelinePhase = phase;
    const scrapePanel = $("#ai-loader-gather");
    const writePanel = $("#ai-loader-write");
    const title = $("#ai-loading-title");
    const subtitle = $("#ai-loading-subtitle");

    scrapePanel?.classList.toggle("active", phase === "scraping");
    writePanel?.classList.toggle("active", phase === "writing" || phase === "done");

    if (state.loadingModalMode === "fill-companies") {
      if (title) title.textContent = "Finding company names";
      if (subtitle) subtitle.textContent = "Reading work email domains in your spreadsheet…";
      return;
    }

    if (phase === "scraping") {
      title.textContent = "Gathering info about companies";
      subtitle.textContent = "Looking up each company from your spreadsheet…";
    } else {
      title.textContent = "Writing drafts";
      subtitle.textContent = "Preparing a separate email for each contact…";
    }
  }

  function updateFillCompaniesProgress(data) {
    setAiLoadingPhase("scraping");
    const total = data.total || 0;
    const done = data.completed || 0;
    const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
    const fillEl = $("#ai-loading-fill");
    if (fillEl) fillEl.style.width = `${pct}%`;
    const counterEl = $("#ai-loading-counter");
    if (counterEl) counterEl.textContent = total ? `${done} / ${total}` : "Starting…";
    const detail = $("#ai-loading-detail");
    if (!detail) return;
    if (data.error) {
      detail.textContent = data.error;
      return;
    }
    if (data.status_note) {
      detail.textContent = data.status_note;
      return;
    }
    if (data.current) {
      detail.textContent = `Checking ${data.current}…`;
      return;
    }
    detail.textContent = "Inferring company names from email domains…";
  }

  function stopFillCompaniesPoll() {
    if (state.fillCompaniesPoll) {
      clearInterval(state.fillCompaniesPoll);
      state.fillCompaniesPoll = null;
    }
  }

  async function finishFillCompanies(result, failed = false) {
    stopFillCompaniesPoll();
    resetAiLoadingCancelButton();
    state.loadingModalMode = "ai";
    state.fillCancelling = false;
    hideAiLoadingModal();

    if (failed || !result) {
      if (failed) alert("Company fill stopped or could not finish. Try again.");
      return;
    }

    try {
      const freshRes = await fetch("/api/upload/data");
      const fresh = await freshRes.json();
      if (fresh.ok) {
        if (fresh.placeholders) state.placeholders = fresh.placeholders;
        if (fresh.placeholder_fields) renderPlaceholderPanel(fresh.placeholder_fields);
        renderUploadAnalysis(fresh);
        showFillCompaniesResultModal({
          ...result,
          skipped: result.skipped || [],
          filled: result.filled ?? fresh.company_fill_stats?.filled ?? 0,
        });
        return;
      }
    } catch (err) {
      console.error("upload refresh failed", err);
    }

    try {
      if (result.placeholders) state.placeholders = result.placeholders;
      if (result.placeholder_fields) renderPlaceholderPanel(result.placeholder_fields);
      renderUploadAnalysis({
        ...(state.uploadAnalysis || {}),
        ...result,
        row_count: (result.rows || []).length || state.uploadAnalysis?.selected_count,
      });
    } catch (err) {
      console.error("fill companies UI update failed", err);
    }

    showFillCompaniesResultModal(result);
  }

  function showFillCompaniesResultModal(result) {
    const skipped = result?.skipped || [];
    const filled = result.filled ?? result.company_fill_stats?.filled ?? 0;
    const processed = result.processed ?? result.company_fill_stats?.processed ?? 0;

    if (!skipped.length) {
      if (processed > 0 && filled === 0) {
        alert("I cannot determine company names with full confidence for any of the remaining contacts, so they were skipped.");
      }
      return;
    }

    const modal = $("#fill-companies-result-modal");
    const summary = $("#fill-result-summary");
    const list = $("#fill-result-skipped");
    if (!modal || !summary || !list) return;

    summary.textContent = filled
      ? `Added ${filled} company name${filled === 1 ? "" : "s"}. For these contacts, I cannot determine with full confidence so skipped:`
      : "For these contacts, I cannot determine with full confidence so skipped:";

    list.innerHTML = skipped
      .map(
        (item) =>
          `<li><span class="fill-skipped-email">${escapeHtml(item.email || "Unknown email")}</span><span class="fill-skipped-msg">${escapeHtml(item.message || "I cannot determine with full confidence so skipped.")}</span></li>`
      )
      .join("");

    modal.classList.remove("hidden");
    injectIcons();
  }

  function hideFillCompaniesResultModal() {
    $("#fill-companies-result-modal")?.classList.add("hidden");
  }

  async function pollFillCompanies() {
    let res;
    let data;
    try {
      res = await fetch("/api/upload/fill-companies/status");
      data = await res.json();
    } catch {
      finishFillCompanies(null, true);
      return;
    }

    if (handleAuthRequired(res, data)) {
      stopFillCompaniesPoll();
      hideAiLoadingModal();
      state.loadingModalMode = "ai";
      return;
    }

    if (!data.ok) {
      finishFillCompanies(null, true);
      return;
    }

    updateFillCompaniesProgress(data);

    if (data.status === "done" && data.result) {
      await finishFillCompanies(data.result);
      return;
    }

    if (data.status === "cancelled") {
      finishFillCompanies(null, false);
      return;
    }

    if (data.status === "error") {
      finishFillCompanies(null, true);
      if (data.error) alert(data.error);
    }
  }

  function startFillCompaniesPoll() {
    stopFillCompaniesPoll();
    state.fillCompaniesPoll = setInterval(pollFillCompanies, 800);
    pollFillCompanies();
  }

  async function startFillCompanies() {
    if (!requireGmailVerified()) return;

    const res = await fetch("/api/upload/fill-companies", { method: "POST" });
    const data = await res.json();
    if (handleAuthRequired(res, data)) return;
    if (!data.ok) {
      alert(data.error || "Could not start company fill.");
      return;
    }

    showFillCompaniesModal(data.total || 0);
    startFillCompaniesPoll();
  }

  async function cancelFillCompanies() {
    if (state.fillCancelling) return;
    state.fillCancelling = true;
    const btn = $("#ai-loading-cancel");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Cancelling…";
    }
    try {
      await fetch("/api/upload/fill-companies/cancel", { method: "POST" });
    } catch {
      resetAiLoadingCancelButton();
      state.fillCancelling = false;
    }
  }

  async function downloadRemainingSheet(format) {
    const fmt = format === "csv" ? "csv" : "xlsx";
    try {
      const res = await fetch(`/api/upload/download-remaining?format=${encodeURIComponent(fmt)}`);
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        alert(data.error || "Download failed.");
        return;
      }
      const blob = await res.blob();
      let filename = fmt === "csv" ? "contacts-remaining.csv" : "contacts-remaining.xlsx";
      const disposition = res.headers.get("Content-Disposition") || "";
      const match = /filename\*?=(?:UTF-8''|")?([^";]+)/i.exec(disposition);
      if (match) filename = decodeURIComponent(match[1].replace(/"/g, ""));
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.style.display = "none";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch {
      alert("Download failed.");
    }
  }

  async function downloadUpdatedSheet(format) {
    const fmt = format || "xlsx";
    try {
      const res = await fetch(`/api/upload/download?format=${encodeURIComponent(fmt)}`);
      const contentType = res.headers.get("content-type") || "";
      if (!res.ok) {
        if (contentType.includes("application/json")) {
          const data = await res.json();
          alert(data.error || "Download failed.");
        } else {
          alert("Download failed.");
        }
        return;
      }
      const blob = await res.blob();
      let filename = fmt === "csv" ? "contacts-with-companies.csv" : "contacts-with-companies.xlsx";
      const disposition = res.headers.get("Content-Disposition") || "";
      const match = /filename\*?=(?:UTF-8''|")?([^";]+)/i.exec(disposition);
      if (match) filename = decodeURIComponent(match[1].replace(/"/g, ""));
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.style.display = "none";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch {
      alert("Download failed.");
    }
  }

  function updateAiLoadingProgress(data) {
    const phase = data.phase || state.aiPipelinePhase || "scraping";
    if (phase === "writing" || phase === "done") setAiLoadingPhase("writing");

    const isGathering = phase === "scraping";
    const total = isGathering
      ? (data.scrape_total || state.aiScrapeTotal || 0)
      : (data.total || state.aiWriteTotal || 0);
    const done = isGathering ? (data.scrape_completed || 0) : (data.completed || 0);
    const generatedOk = data.generated_ok ?? data.drafts_ready ?? 0;
    const partialStop =
      data.status === "cancelled" ||
      data.quota_exhausted ||
      data.agent_abort ||
      (data.status === "done" && data.quota_exhausted);
    const counterEl = $("#ai-loading-counter");

    let pct;
    if (isGathering) {
      pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
      if (counterEl) counterEl.textContent = total ? `${done} / ${total}` : "Starting…";
    } else if (partialStop || (data.status === "done" && generatedOk < total)) {
      pct = 100;
      if (counterEl) {
        counterEl.textContent =
          generatedOk === 1 ? "1 email generated" : `${generatedOk} emails generated`;
      }
    } else {
      pct = total ? Math.min(100, Math.round((generatedOk / total) * 100)) : 0;
      if (counterEl) {
        counterEl.textContent = total
          ? `${generatedOk} / ${total} generated`
          : generatedOk
            ? `${generatedOk} generated`
            : "Starting…";
      }
    }

    $("#ai-loading-fill").style.width = `${pct}%`;

    const detail = $("#ai-loading-detail");
    if (data.quota_exhausted || data.groq_daily_blocked) {
      detail.textContent =
        data.quota_message ||
        data.groq_block_message ||
        "Groq daily limit reached — try again after your quota resets (usually within 24 hours).";
      return;
    }
    if (data.status_note && /daily quota is expired|groq daily/i.test(data.status_note)) {
      detail.textContent = data.status_note;
      return;
    }
    if (data.agent_abort && data.error) {
      detail.textContent = data.error;
      return;
    }
    if (data.error) {
      detail.textContent = data.error;
      return;
    }
    if (data.status_note) {
      detail.textContent = data.status_note;
      return;
    }
    if (isGathering && data.current_company) {
      detail.textContent = `Researching ${data.current_company}…`;
      return;
    }
    if (!isGathering && data.current) {
      detail.textContent = `Writing for ${data.current}…`;
      return;
    }
    if (!isGathering) {
      detail.textContent = "First draft can take up to a minute…";
    }
  }

  async function proceedToComposeManual() {
    hideComposeChoiceModal();
    await setComposeMode("manual");
    goToStep(3);
  }

  async function proceedToComposeAI() {
    hideComposeChoiceModal();
    showAiKeyModal();
  }

  async function confirmAiKeyAndContinue() {
    const statusEl = $("#llm-key-error");
    const rawKey = $("#llm-api-key").value.trim();
    const secondaryKey = ($("#llm-api-key-secondary")?.value || "").trim();
    const multiAgent = !!$("#llm-multi-agent")?.checked;
    const provider = state.aiProvider || "groq";
    const providerLabel = provider === "groq" ? "Groq" : "Gemini";
    hideToast(statusEl);

    if (!rawKey) {
      const keyCheck = await fetch(`/api/ai/llm-key?provider=${encodeURIComponent(provider)}`);
      const keyStatus = await keyCheck.json();
      if (!keyStatus.has_key) {
        toast(statusEl, `Enter your ${providerLabel} API key to continue.`, "error");
        return;
      }
    }

    setGeminiKeyLoading(true);

    try {
      const payload = { provider, multi_agent: multiAgent };
      if (rawKey) payload.api_key = rawKey;
      if (multiAgent && secondaryKey) payload.api_key_secondary = secondaryKey;

      const res = await fetch("/api/ai/llm-key/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();

      if (handleAuthRequired(res, data)) return;
      if (!data.ok) {
        toast(statusEl, data.error || "Invalid API key.", "error");
        return;
      }

      state.aiProvider = data.provider || provider;
      state.multiAgent = !!data.multi_agent;
      updateComposeProviderLabel();
      hideAiKeyModal();
      await setComposeMode("ai");
      goToStep(3);
    } finally {
      setGeminiKeyLoading(false);
    }
  }

  async function setComposeMode(mode) {
    state.composeMode = mode;
    await fetch("/api/ai/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    if (mode === "ai") showComposeAI();
    else showComposeManual();
  }

  function showComposeManual() {
    $("#compose-manual-panel")?.classList.remove("hidden");
    $("#compose-ai-panel")?.classList.add("hidden");
    $("#compose-intro-text").textContent = "Write once, personalize for every contact. Attach a file only if you need it.";
  }

  function showComposeAI() {
    $("#compose-manual-panel")?.classList.add("hidden");
    $("#compose-ai-panel")?.classList.remove("hidden");
    $("#compose-intro-text").textContent = "Add your resume and generate one draft per contact.";
    updateAiGenerateButton();
    injectIcons();
  }

  function stopAiGenPoll() {
    if (state.aiGenPoll) {
      clearInterval(state.aiGenPoll);
      state.aiGenPoll = null;
    }
  }

  async function cancelAiGeneration() {
    if (state.aiCancelling) return;
    state.aiCancelling = true;
    const btn = $("#ai-loading-cancel");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Cancelling…";
    }
    const detail = $("#ai-loading-detail");
    if (detail) detail.textContent = "Cancelling…";

    try {
      await fetch("/api/ai/generate/cancel", { method: "POST" });
    } catch {
      resetAiLoadingCancelButton();
    }
  }

  function shouldShowAiPartialStop(data) {
    const generatedOk = data.generated_ok ?? data.drafts_ready ?? 0;
    const total = data.total || state.aiWriteTotal || 0;
    if (data.quota_exhausted || data.groq_daily_blocked) return true;
    if (data.status === "error" && generatedOk > 0) return true;
    if (data.status === "cancelled" && generatedOk > 0) return true;
    if (data.status === "done" && total > 0 && generatedOk < total) return true;
    return false;
  }

  function showAiPartialStop(data) {
    stopAiGenPoll();
    resetAiLoadingCancelButton();
    $("#ai-generate-btn").disabled = false;
    state.aiPartialStopData = data;

    const generatedOk = data.generated_ok ?? data.drafts_ready ?? 0;
    const remaining = data.remaining_count ?? Math.max((data.total || 0) - generatedOk, 0);
    let text =
      data.quota_message ||
      data.error ||
      data.status_note ||
      data.groq_block_message ||
      "";

    if (!text && data.quota_exhausted) {
      text = `Groq daily limit reached after ${generatedOk} email${generatedOk === 1 ? "" : "s"}. Your quota resets on a rolling 24-hour window — try again tomorrow.`;
    } else if (!text) {
      text = `Generation stopped — ${generatedOk} email${generatedOk === 1 ? "" : "s"} ready to review.`;
    }
    if (remaining > 0) {
      text += ` ${remaining} contact${remaining === 1 ? "" : "s"} still need drafts — download below to resume later.`;
    }

    $("#ai-loading-partial-msg").textContent = text;
    $("#ai-loading-partial")?.classList.remove("hidden");
    $("#ai-loading-cancel")?.classList.add("hidden");
    updateAiLoadingProgress(data);
  }

  function hideAiPartialStop() {
    $("#ai-loading-partial")?.classList.add("hidden");
    $("#ai-loading-cancel")?.classList.remove("hidden");
    state.aiPartialStopData = null;
  }

  function showReviewRemainingActions(data) {
    const panel = $("#review-remaining-actions");
    const remaining = data?.remaining_count ?? 0;
    if (!panel || remaining <= 0) {
      panel?.classList.add("hidden");
      return;
    }
    const text = data.quota_exhausted
      ? `${remaining} contact${remaining === 1 ? "" : "s"} still need AI drafts. Download and re-upload tomorrow when your Groq quota resets.`
      : `${remaining} contact${remaining === 1 ? "" : "s"} were not generated. Download the spreadsheet to resume later.`;
    $("#review-remaining-text").textContent = text;
    panel.classList.remove("hidden");
  }

  function handleAiGenerationStopped(data) {
    hideAiPartialStop();
    stopAiGenPoll();
    resetAiLoadingCancelButton();
    $("#ai-generate-btn").disabled = false;
    hideAiLoadingModal();
    return data;
  }

  async function finishAiGenerationToReview(data, notice) {
    handleAiGenerationStopped(data);
    state.reviewNotice = notice || "";
    await loadReview();
    goToStep(4);
  }

  async function pollAiGeneration() {
    const res = await fetch("/api/ai/generate/status");
    const data = await res.json();
    if (!data.ok) return;
    updateAiLoadingProgress(data);

    if (data.status === "running") {
      const progressKey = `${data.phase}:${data.generated_ok}:${data.completed}:${data.scrape_completed}`;
      if (state.aiLastProgressKey === progressKey) {
        if (!state.aiLastProgressAt) state.aiLastProgressAt = Date.now();
        else if (Date.now() - state.aiLastProgressAt > 180000) {
          const detail = $("#ai-loading-detail");
          if (detail && !data.quota_exhausted) {
            detail.textContent =
              "Still waiting on Groq — large prompts can take up to a minute. If this stays stuck, cancel and download remaining contacts.";
          }
        }
      } else {
        state.aiLastProgressKey = progressKey;
        state.aiLastProgressAt = Date.now();
      }
    }

    if (shouldShowAiPartialStop(data)) {
      showAiPartialStop(data);
      return;
    }

    const generatedOk = data.generated_ok ?? data.drafts_ready ?? 0;

    if (data.status === "done") {
      await finishAiGenerationToReview(data, "");
    } else if (data.status === "cancelled") {
      if (generatedOk > 0) {
        const label = generatedOk === 1 ? "1 email" : `${generatedOk} emails`;
        await finishAiGenerationToReview(
          data,
          `Generation cancelled. ${label} ready to review.`
        );
      } else {
        handleAiGenerationStopped(data);
        toast($("#ai-generate-status"), "Generation cancelled.", "error");
      }
    } else if (data.status === "error") {
      if (generatedOk > 0) {
        await finishAiGenerationToReview(
          data,
          data.error || "Generation stopped early. Review the emails generated so far."
        );
      } else {
        handleAiGenerationStopped(data);
        toast($("#ai-generate-status"), data.error || "Generation failed.", "error");
      }
    }
  }

  function startAiGenPoll() {
    stopAiGenPoll();
    state.aiGenPoll = setInterval(pollAiGeneration, 1500);
    pollAiGeneration();
  }

  function updateAiGenerateButton() {
    const btn = $("#ai-generate-btn");
    if (btn) btn.disabled = !state.aiResumeLoaded;
  }

  function markResumeUploaded(filename) {
    $("#resume-zone-title").textContent = filename || "Resume uploaded";
    $("#resume-zone")?.classList.add("has-file");
    state.aiResumeLoaded = true;
    updateAiGenerateButton();
  }

  async function initComposeStep() {
    const res = await fetch("/api/ai/context");
    const data = await res.json();
    if (!data.ok) return;

    state.composeMode = data.compose_mode || null;
    state.aiResumeLoaded = !!data.resume_text;

    if (data.resume_filename) {
      markResumeUploaded(data.resume_filename);
    }
    if (data.portfolio_url && $("#portfolio-url")) {
      $("#portfolio-url").value = data.portfolio_url;
    }

    if (data.ai_provider) {
      state.aiProvider = data.ai_provider === "groq" ? "groq" : "gemini";
      updateComposeProviderLabel();
    }
    if (typeof data.multi_agent === "boolean") {
      state.multiAgent = data.multi_agent;
      updateComposeProviderLabel();
    }

    if (state.composeMode === "manual") showComposeManual();
    else if (state.composeMode === "ai") showComposeAI();
    else showComposeChoiceModal();
  }

  document.querySelectorAll("[data-llm-provider]").forEach((tab) => {
    tab.addEventListener("click", () => setAiProvider(tab.dataset.llmProvider));
  });

  $("#llm-multi-agent")?.addEventListener("change", updateMultiAgentUi);

  $("#compose-choice-manual")?.addEventListener("click", proceedToComposeManual);
  $("#compose-choice-ai")?.addEventListener("click", proceedToComposeAI);
  $("#compose-choice-cancel")?.addEventListener("click", hideComposeChoiceModal);

  $("#llm-key-cancel")?.addEventListener("click", () => {
    hideAiKeyModal();
    showComposeChoiceModal();
  });
  $("#llm-key-continue")?.addEventListener("click", confirmAiKeyAndContinue);

  $("#compose-manual-back")?.addEventListener("click", () => goToStep(2));

  $("#compose-ai-back")?.addEventListener("click", () => goToStep(2));

  $("#ai-loading-cancel")?.addEventListener("click", () => {
    if (state.loadingModalMode === "fill-companies") {
      cancelFillCompanies();
      return;
    }
    cancelAiGeneration();
  });

  $("#upload-analysis-panel")?.addEventListener("click", (e) => {
    const target = e.target;
    if (!(target instanceof HTMLElement)) return;
    if (target.id === "fill-companies-btn") startFillCompanies();
    if (target.id === "download-sheet-xlsx") downloadUpdatedSheet("xlsx");
    if (target.id === "download-sheet-csv") downloadUpdatedSheet("csv");
  });

  $("#ai-download-remaining-xlsx")?.addEventListener("click", () => downloadRemainingSheet("xlsx"));
  $("#ai-download-remaining-csv")?.addEventListener("click", () => downloadRemainingSheet("csv"));
  $("#review-download-remaining-xlsx")?.addEventListener("click", () => downloadRemainingSheet("xlsx"));
  $("#review-download-remaining-csv")?.addEventListener("click", () => downloadRemainingSheet("csv"));

  $("#ai-continue-review")?.addEventListener("click", async () => {
    const data = state.aiPartialStopData;
    if (!data) return;
    const notice =
      data.quota_message ||
      (data.quota_exhausted
        ? "Groq daily limit reached. Review generated emails, then resume tomorrow with the remaining spreadsheet."
        : "Partial generation — review what's ready, then continue later with the remaining contacts.");
    await finishAiGenerationToReview(data, notice);
  });

  $("#fill-result-close")?.addEventListener("click", hideFillCompaniesResultModal);
  $("#fill-companies-result-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "fill-companies-result-modal") hideFillCompaniesResultModal();
  });

  $("#resume-file")?.addEventListener("change", async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const statusEl = $("#resume-status");
    hideToast(statusEl);
    toast(statusEl, "Parsing resume…", "success");

    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch("/api/ai/resume", { method: "POST", body: fd });
    const data = await res.json();
    if (handleAuthRequired(res, data)) return;
    if (!data.ok) {
      toast(statusEl, data.error || "Could not parse resume.", "error");
      return;
    }

    markResumeUploaded(data.resume_filename);
    if (data.has_attachment !== undefined) {
      state.hasAttachment = !!data.has_attachment;
      state.attachmentName = data.attachment_name || null;
      updateAttachZone();
    }
    toast(statusEl, "Resume uploaded and attached to emails.", "success");
  });

  $("#ai-generate-btn")?.addEventListener("click", async () => {
    const statusEl = $("#ai-generate-status");
    const btn = $("#ai-generate-btn");
    hideToast(statusEl);

    if (!state.aiResumeLoaded) {
      toast(statusEl, "Upload your resume first.", "error");
      return;
    }

    const portfolioUrl = ($("#portfolio-url")?.value || "").trim();
    await fetch("/api/ai/portfolio", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ portfolio_url: portfolioUrl }),
    });

    btn.disabled = true;
    showAiLoadingModal();

    const res = await fetch("/api/ai/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        portfolio_url: portfolioUrl,
        provider: state.aiProvider || "groq",
        multi_agent: !!state.multiAgent,
      }),
    });
    const data = await res.json();
    if (handleAuthRequired(res, data)) {
      btn.disabled = false;
      hideAiLoadingModal();
      return;
    }
    if (!data.ok) {
      hideAiLoadingModal();
      btn.disabled = false;
      if (data.code === "groq_daily_exhausted") {
        const hours = data.hours_until_retry || 24;
        toast(
          statusEl,
          `${data.error} Retry in about ${hours} hour${hours === 1 ? "" : "s"}.`,
          "error"
        );
        if (data.can_download_remaining) {
          await downloadRemainingSheet("xlsx");
        }
      } else {
        toast(statusEl, data.error || "Could not start generation.", "error");
      }
      return;
    }

    state.aiLastProgressKey = "";
    state.aiLastProgressAt = 0;
    hideAiPartialStop();

    state.aiScrapeTotal = data.scrape_total || 0;
    state.aiWriteTotal = data.total || 0;
    updateAiLoadingProgress({
      phase: "scraping",
      scrape_total: state.aiScrapeTotal,
      scrape_completed: 0,
    });
    startAiGenPoll();
  });

  $("#preview-btn").addEventListener("click", async () => {
    if (!(await validateCompose())) return;
    const res = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payloadWithTemplate()),
    });
    const data = await res.json();
    const el = $("#preview-result");
    el.classList.remove("hidden");
    el.innerHTML = `<p style="font-weight:700;margin-bottom:0.5rem">Sample previews</p>` +
      data.samples.map((s) => `
        <div class="preview-item">
          <div style="font-size:0.78rem;color:var(--muted)">To ${escapeHtml(s.email)}</div>
          <div style="font-weight:700;margin:0.2rem 0">${highlightSubstitutions(s.subject, s)}</div>
          <pre class="preview-body">${highlightSubstitutions(s.body, s)}</pre>
        </div>`).join("");
  });

  $("#to-review-btn").addEventListener("click", async () => {
    if (!requireGmailVerified()) return;
    if (!(await validateCompose())) return;
    goToStep(4);
  });

  $("#btn-reject").addEventListener("click", () => {
    const pending = pendingRecipients();
    if (!pending.length || state.animating) return;
    if (state.regenerating) {
      rejectDuringRegenerate();
      return;
    }
    animateDecision("reject", pending[0].email, showCurrentCard);
  });

  $("#btn-edit").addEventListener("click", () => {
    const pending = pendingRecipients();
    if (!pending.length || state.animating || state.regenerating) return;
    showEditChoiceModal(pending[0]);
  });

  $("#btn-regenerate")?.addEventListener("click", regenerateCurrentEmail);
  $("#regen-cancel")?.addEventListener("click", cancelRegenerate);

  $("#edit-this-email").addEventListener("click", () => {
    const email = state.editingRecipientEmail;
    const recipient = email ? getRecipientByEmail(email) : null;
    hideEditChoiceModal();
    if (recipient) showEmailEditModal(recipient);
  });

  $("#edit-template").addEventListener("click", () => {
    hideEditChoiceModal();
    goToStep(3);
  });

  $("#edit-choice-cancel").addEventListener("click", hideEditChoiceModal);
  $("#edit-choice-modal").addEventListener("click", (e) => {
    if (e.target.id === "edit-choice-modal") hideEditChoiceModal();
  });

  $("#email-edit-save").addEventListener("click", saveEmailEdit);
  $("#email-edit-cancel").addEventListener("click", hideEmailEditModal);
  $("#email-edit-modal").addEventListener("click", (e) => {
    if (e.target.id === "email-edit-modal") hideEmailEditModal();
  });

  $("#btn-approve").addEventListener("click", async () => {
    const pending = pendingRecipients();
    if (!pending.length || state.animating || state.regenerating) return;
    if (recipientIsUndeliverable(pending[0])) {
      toast($("#review-errors"), "This email failed to generate. Regenerate or edit it before sending.", "error");
      return;
    }
    if (!(await ensureDailyLimitAllowed(1))) return;
    animateDecision("approve", pending[0].email, showCurrentCard);
  });

  $("#approve-all-btn").addEventListener("click", async () => {
    const pending = pendingRecipients();
    if (!pending.length || state.animating) return;
    const sendable = pending.filter((r) => !recipientIsUndeliverable(r));
    const skipped = pending.length - sendable.length;
    if (!sendable.length) {
      toast($("#review-errors"), "No sendable drafts left — regenerate or skip failed emails.", "error");
      return;
    }
    const n = sendable.length;
    if (!(await ensureDailyLimitAllowed(n))) return;
    const skipNote = skipped
      ? `\n\n${skipped} failed draft${skipped === 1 ? "" : "s"} will be skipped.`
      : "";
    const ok = confirm(
      `Approve and send ${n} email${n === 1 ? "" : "s"}?${skipNote}\n\nEach will be sent immediately in the background.`
    );
    if (!ok) return;
    for (const r of sendable) {
      state.approved.add(r.email);
      await approveAndSend(r);
    }
    for (const r of pending) {
      if (recipientIsUndeliverable(r) && !state.approved.has(r.email)) {
        state.rejected.add(r.email);
      }
    }
    updateReviewStats();
    showReviewComplete();
  });

  $("#campaign-new-btn").addEventListener("click", async () => {
    await fetch("/api/session/reset", { method: "POST" });
    stopCampaignPolling();
    resetUiToFresh();
  });

  $("#campaign-compose-btn").addEventListener("click", () => goToStep(3));

  async function ensureDailyLimitAllowed(extraCount = 1) {
    if (state.dailyLimitAcknowledged) return true;

    const status = await fetchStatus();
    const limit = status.recommended_daily_limit ?? status.daily_limit ?? APP_CONFIG.dailyLimit;
    const sent = status.sent_today ?? 0;
    if (sent + extraCount <= limit) return true;

    const overBy = sent + extraCount - limit;
    const msg = sent >= limit
      ? `You've sent ${sent} emails today (recommended: ${limit}/day for deliverability).\n\nSend ${extraCount === 1 ? "this one" : `${extraCount} more`} anyway?`
      : `This will bring you to ${sent + extraCount}/${limit} emails today (${overBy} over the recommended daily limit).\n\nContinue anyway?`;

    if (confirm(msg)) {
      state.dailyLimitAcknowledged = true;
      return true;
    }
    return false;
  }

  async function refreshQuota() {
    const status = await fetchStatus();
    const limit = status.recommended_daily_limit ?? status.daily_limit ?? APP_CONFIG.dailyLimit;
    const sent = status.sent_today ?? 0;
    const banner = $("#daily-banner");
    const over = status.over_recommended_daily_limit || sent >= limit;

    if (over) {
      banner.textContent = `${sent}/${limit} sent today (over recommended limit)`;
      banner.classList.add("is-over-limit");
    } else {
      banner.textContent = `${sent}/${limit} sent from this address · ${status.remaining_today} left today (recommended)`;
      banner.classList.remove("is-over-limit");
    }
  }

  function deriveFields(cols, rows = []) {
    if (!cols) return null;
    const emptyCount = (key) => rows.filter((r) => !String(r[key] || "").trim()).length;
    const meta = (key, token, col) => {
      const hasColumn = !!col;
      const empty = hasColumn ? emptyCount(key) : 0;
      return {
        token,
        has_column: hasColumn,
        column: col,
        has_empty: empty > 0,
        empty_count: empty,
      };
    };
    return {
      person_name: meta("person_name", TOKEN_PERSON, cols.person_name),
      company_name: meta("company_name", TOKEN_COMPANY, cols.company_name),
    };
  }

  function markSessionActive() {
    window.__ehSessionActive = true;
  }

  function isPageReload() {
    const nav = performance.getEntriesByType("navigation")[0];
    return nav?.type === "reload";
  }

  function hasWorkflowData(status) {
    return !!(status?.gmail_verified || status?.has_upload || status?.has_attachment);
  }

  function resetUiToFresh() {
    state.gmailVerified = false;
    state.hasAttachment = false;
    state.attachmentName = null;
    state.placeholderFields = null;
    state.placeholders = [];
    state.recipients = [];
    state.approved = new Set();
    state.rejected = new Set();

    state.mailProvider = "gmail";
    $("#mail-password").value = "";
    $("#subject").value = "";
    $("#body").value = "";
    if (sheetInput) sheetInput.value = "";
    $("#sheet-filename")?.classList.add("hidden");
    sheetZone?.classList.remove("has-file");
    $("#attachment-file").value = "";

    renderPlaceholderPanel(null);
    setAnalysisBadge("pending");
    showAnalysisPanel("is-idle");
    setUploadContinueVisible(false);
    $("#upload-analysis-panel").innerHTML =
      `<p class="aside-empty">Upload and analyze a file to see column mapping and preview rows.</p>`;

    $("#preview-result")?.classList.add("hidden");
    hideToast($("#mail-status"));
    setMailProvider("gmail");
    hideToast($("#template-errors"));
    hideToast($("#review-errors"));

    updateAttachZone();
    updateWizardLock();
    updateUserSidebar();
    stopCampaignPolling();
    $("#review-send-badge")?.classList.add("hidden");
    setSendStepVisible(false);
    resetCampaignPage();
    stopAiGenPoll();
    state.composeMode = null;
    state.aiResumeLoaded = false;
    goToStep(1);
    window.__ehSessionActive = false;
  }

  window.addEventListener("beforeunload", (e) => {
    if (!window.__ehSessionActive) return;
    e.preventDefault();
    e.returnValue = "Reloading will erase your session and start fresh.";
    return e.returnValue;
  });

  injectIcons();
  setMailProvider("gmail");
  setAiProvider("groq");

  (async () => {
    if (isPageReload()) {
      await fetch("/api/session/reset", { method: "POST" });
      resetUiToFresh();
    }

    const status = await fetchStatus();

    if (status.gmail_address || status.email_address) {
      const addr = status.email_address || status.gmail_address;
      $("#mail-address").value = addr;
      state.gmailAddress = addr;
    }
    if (status.mail_provider) {
      setMailProvider(status.mail_provider);
    }

    if (!isPageReload()) {
      state.gmailVerified = !!status.gmail_verified;
      updateWizardLock();
      updateUserSidebar();

      if (status.upload_summary) {
        state.placeholders = status.upload_summary.placeholders;
        renderPlaceholderPanel(
          status.upload_summary.placeholder_fields
            || deriveFields(status.upload_summary.detected_columns, status.upload_summary.rows || [])
        );
        restoreUploadAnalysis(status.upload_summary);
      }
      if (status.has_attachment) {
        state.hasAttachment = true;
        state.attachmentName = status.attachment_name;
        updateAttachZone();
      }

      if (status.ai_context?.ai_provider) {
        setAiProvider(status.ai_context.ai_provider);
      }

      if (hasWorkflowData(status)) markSessionActive();

      if (status.gmail_verified && status.has_upload) goToStep(3);
      else if (status.gmail_verified) goToStep(2);
      else goToStep(1);
    }

    refreshQuota();
    checkAttachReminder();
  })();
})();
