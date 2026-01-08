const ACTIVITY_PAGE_SIZE = 10;
const activityState = {
  status: null,
  jobs: [],
  events: [],
  lastRefresh: null,
  timer: null,
  eventsVisible: ACTIVITY_PAGE_SIZE,
  jobsVisible: ACTIVITY_PAGE_SIZE,
  filters: {
    status: "all",
    provider: "all",
    search: "",
  },
};

function renderStatCard(label, value, title) {
  const card = document.createElement("div");
  card.className = "activity-stat";
  const labelEl = document.createElement("span");
  labelEl.textContent = label;
  const valueEl = document.createElement("strong");
  valueEl.textContent = value;
  if (title) {
    valueEl.title = title;
  }
  card.appendChild(labelEl);
  card.appendChild(valueEl);
  return card;
}

function renderActivitySummary(statusData) {
  const container = document.getElementById("activity-summary");
  if (!container) {
    return;
  }
  container.innerHTML = "";
  if (!statusData) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No status data yet.";
    container.appendChild(empty);
    return;
  }
  const outbox = statusData.outbox || {};
  const counts = outbox.counts || {};
  const metadata = statusData.metadata || {};
  const metadataCounts = metadata.counts || {};
  const pending = (counts.pending || 0) + (counts.failed_retryable || 0);
  const inProgress = counts.in_progress || 0;
  const nextOutbox = outbox.next_run_at || null;
  const quickImport = statusData.imports ? statusData.imports.quick : null;
  const nextImport = quickImport ? quickImport.next_run_at : null;
  const queue =
    quickImport && Array.isArray(quickImport.queue) && quickImport.queue.length
      ? quickImport.queue.map((entry) => formatProvider(entry)).join(" → ")
      : "Idle";

  const stats = [
    {
      label: "Outbox pending",
      value: String(pending),
    },
    {
      label: "Outbox in progress",
      value: String(inProgress),
    },
    {
      label: "Next outbox run",
      value: formatRelativeTime(nextOutbox),
      title: formatMetadataDate(nextOutbox),
    },
    {
      label: "Next import",
      value: formatRelativeTime(nextImport),
      title: formatMetadataDate(nextImport),
    },
    {
      label: "Import queue",
      value: queue,
    },
    {
      label: "Metadata pending",
      value: String(metadataCounts.pending || 0),
    },
  ];

  stats.forEach((stat) => {
    container.appendChild(renderStatCard(stat.label, stat.value, stat.title));
  });
}

function renderScheduleGroup(title, rows) {
  const group = document.createElement("div");
  group.className = "schedule-group";
  const header = document.createElement("h3");
  header.textContent = title;
  group.appendChild(header);
  rows.forEach((row) => {
    group.appendChild(row);
  });
  return group;
}

function createScheduleActionButton(label, options = {}) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `btn ${options.variant === "primary" ? "btn-primary" : "btn-secondary"} btn-sm`;
  button.textContent = label;
  if (options.disabled) {
    button.disabled = true;
  }
  if (typeof options.onClick === "function") {
    button.addEventListener("click", options.onClick);
  }
  return button;
}

async function requestQuickImportNow(button) {
  if (button) {
    button.disabled = true;
  }
  setMessage("activity-import-message", "Requesting import...");
  try {
    const response = await requestJSON("/api/integrations/import/quick", {
      method: "POST",
    });
    const providers = response && response.providers ? response.providers : [];
    const label = providers.length
      ? `Quick import queued: ${providers.join(", ")}.`
      : "Quick import requested.";
    setMessage("activity-import-message", label);
    await loadActivity(true);
  } catch (error) {
    setMessage("activity-import-message", error.message, true);
  } finally {
    if (button) {
      button.disabled = false;
    }
  }
}

async function requestImportAllNow(button) {
  const confirmed = window.confirm(
    "Start import all? This can take a while and will re-sync your full history."
  );
  if (!confirmed) {
    return;
  }
  if (button) {
    button.disabled = true;
  }
  setMessage("activity-import-message", "Requesting import...");
  try {
    const response = await requestJSON("/api/integrations/import/all", {
      method: "POST",
    });
    const providers = response && response.providers ? response.providers : [];
    const label = providers.length
      ? `Import queued: ${providers.join(", ")}.`
      : "Import requested.";
    setMessage("activity-import-message", label);
    await loadActivity(true);
  } catch (error) {
    setMessage("activity-import-message", error.message, true);
  } finally {
    if (button) {
      button.disabled = false;
    }
  }
}

function buildQuickImportRow(quick) {
  if (!quick) {
    return null;
  }
  const row = document.createElement("div");
  row.className = "schedule-row";

  const main = document.createElement("div");
  const title = document.createElement("div");
  title.className = "schedule-title";
  title.textContent = "Quick import";
  const meta = document.createElement("div");
  meta.className = "schedule-meta";
  const parts = [];
  if (quick.interval_seconds !== null && quick.interval_seconds !== undefined) {
    parts.push(formatInterval(quick.interval_seconds));
  }
  if (quick.last_run_at) {
    parts.push(`Last run ${formatMetadataDate(quick.last_run_at)}`);
  }
  if (quick.status) {
    parts.push(formatLabel(quick.status));
  }
  meta.textContent = parts.join(" · ");
  main.appendChild(title);
  main.appendChild(meta);

  const time = document.createElement("div");
  time.className = "schedule-time";
  const nextLabel = quick.next_run_at ? formatRelativeTime(quick.next_run_at) : "Not scheduled";
  time.textContent = nextLabel;
  if (quick.next_run_at) {
    const details = document.createElement("small");
    details.textContent = formatMetadataDate(quick.next_run_at);
    time.appendChild(details);
  }
  const actions = document.createElement("div");
  actions.className = "schedule-actions";
  if (quick.status === "in_progress") {
    actions.appendChild(createStatusBadge("Running", "in_progress"));
  }
  if (
    quick.status === "pending" ||
    quick.status === "in_progress"
  ) {
    actions.appendChild(createStatusBadge("Scheduled", "pending"));
  }
  const runButton = createScheduleActionButton("Run now", {
    disabled: quick.status === "pending" || quick.status === "in_progress",
  });
  runButton.addEventListener("click", () => requestQuickImportNow(runButton));
  actions.appendChild(runButton);

  const side = document.createElement("div");
  side.className = "schedule-side";
  side.appendChild(time);
  side.appendChild(actions);

  row.appendChild(main);
  row.appendChild(side);
  return row;
}

function buildImportAllRow(importAll) {
  if (!importAll) {
    return null;
  }
  const row = document.createElement("div");
  row.className = "schedule-row";
  const main = document.createElement("div");
  const title = document.createElement("div");
  title.className = "schedule-title";
  title.textContent = "Import all";
  const meta = document.createElement("div");
  meta.className = "schedule-meta";
  const metaParts = [formatLabel(importAll.status || "idle")];
  if (importAll.started_at) {
    metaParts.push(`Started ${formatMetadataDate(importAll.started_at)}`);
  }
  if (importAll.completed_at) {
    metaParts.push(`Completed ${formatMetadataDate(importAll.completed_at)}`);
  }
  if (importAll.error) {
    metaParts.push(`Error: ${importAll.error}`);
  }
  meta.textContent = metaParts.join(" · ");
  main.appendChild(title);
  main.appendChild(meta);

  const time = document.createElement("div");
  time.className = "schedule-time";
  const statusLabel = formatLabel(importAll.status || "idle");
  time.textContent = statusLabel;
  if (importAll.started_at) {
    const details = document.createElement("small");
    details.textContent = formatMetadataDate(importAll.started_at);
    time.appendChild(details);
  }
  const actions = document.createElement("div");
  actions.className = "schedule-actions";
  if (importAll.status === "in_progress") {
    actions.appendChild(createStatusBadge("Running", "in_progress"));
  }
  if (
    importAll.status === "pending" ||
    importAll.status === "in_progress"
  ) {
    actions.appendChild(createStatusBadge("Scheduled", "pending"));
  }
  const runButton = createScheduleActionButton("Run now", {
    variant: "primary",
    disabled: importAll.status === "pending" || importAll.status === "in_progress",
  });
  runButton.addEventListener("click", () => requestImportAllNow(runButton));
  actions.appendChild(runButton);

  const side = document.createElement("div");
  side.className = "schedule-side";
  side.appendChild(time);
  side.appendChild(actions);

  row.appendChild(main);
  row.appendChild(side);
  return row;
}

function buildJobScheduleRow(job) {
  const row = document.createElement("div");
  row.className = "schedule-row";
  const main = document.createElement("div");
  const title = document.createElement("div");
  title.className = "schedule-title";
  title.textContent = formatLabel(job.name);
  const meta = document.createElement("div");
  meta.className = "schedule-meta";
  const parts = [];
  if (job.last_run_at) {
    parts.push(`Last run ${formatMetadataDate(job.last_run_at)}`);
  }
  if (job.lease_until && new Date(job.lease_until) > new Date()) {
    parts.push(`Leased until ${formatMetadataDate(job.lease_until)}`);
  }
  meta.textContent = parts.join(" · ");
  main.appendChild(title);
  main.appendChild(meta);

  const time = document.createElement("div");
  time.className = "schedule-time";
  const nextLabel = job.next_run_at ? formatRelativeTime(job.next_run_at) : "Not scheduled";
  time.textContent = nextLabel;
  if (job.next_run_at) {
    const details = document.createElement("small");
    details.textContent = formatMetadataDate(job.next_run_at);
    time.appendChild(details);
  }
  if (job.lease_until && new Date(job.lease_until) > new Date()) {
    time.prepend(createStatusBadge("Running", "in_progress"));
  }

  const side = document.createElement("div");
  side.className = "schedule-side";
  side.appendChild(time);

  row.appendChild(main);
  row.appendChild(side);
  return row;
}

function renderSchedule(statusData) {
  const container = document.getElementById("activity-schedule");
  if (!container) {
    return;
  }
  container.innerHTML = "";
  if (!statusData) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No schedules yet.";
    container.appendChild(empty);
    return;
  }
  const imports = statusData.imports || {};
  const scheduledJobs = statusData.scheduled_jobs || [];

  const rows = [];
  const importRows = [];
  const quickRow = buildQuickImportRow(imports.quick);
  if (quickRow) {
    importRows.push(quickRow);
  }
  const importAllRow = buildImportAllRow(imports.import_all);
  if (importAllRow) {
    importRows.push(importAllRow);
  }
  if (importRows.length) {
    rows.push(renderScheduleGroup("Imports", importRows));
  }
  if (scheduledJobs.length) {
    rows.push(
      renderScheduleGroup(
        "Maintenance",
        scheduledJobs.map((job) => buildJobScheduleRow(job))
      )
    );
  }
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No schedules yet.";
    container.appendChild(empty);
    return;
  }
  rows.forEach((row) => {
    container.appendChild(row);
  });
}

function buildDetailRow(label, value) {
  const row = document.createElement("div");
  const safeValue =
    value === null || value === undefined || value === "" ? "Unknown" : String(value);
  row.textContent = `${label}: ${safeValue}`;
  return row;
}

const SENSITIVE_PAYLOAD_KEYS = new Set([
  "access_token",
  "refresh_token",
  "client_secret",
  "password",
  "api_key",
  "apikey",
  "token",
]);

function maskSensitivePayload(payload) {
  if (!payload || typeof payload !== "object") {
    return payload;
  }
  if (Array.isArray(payload)) {
    return payload.map((value) => maskSensitivePayload(value));
  }
  const masked = {};
  Object.entries(payload).forEach(([key, value]) => {
    if (SENSITIVE_PAYLOAD_KEYS.has(key.toLowerCase())) {
      masked[key] = "REDACTED";
    } else {
      masked[key] = maskSensitivePayload(value);
    }
  });
  return masked;
}

function formatItemCount(count) {
  const value = Number(count) || 0;
  return `${value} item${value === 1 ? "" : "s"}`;
}

function formatItemCountShell(count) {
  const value = Number(count) || 0;
  return `${value} Item${value === 1 ? "" : "s"}`;
}

function isImportHistoryEvent(event) {
  return Boolean(event && event.event_category === "import");
}

function formatImportType(value) {
  const normalized = value ? String(value).toLowerCase() : "";
  if (normalized === "quick_import") {
    return "Quick Import";
  }
  if (normalized === "import_all") {
    return "Import All";
  }
  return "Import";
}

function importBadgeStatus(status) {
  const normalized = status ? String(status) : "";
  if (normalized === "completed") {
    return "succeeded";
  }
  if (normalized === "failed") {
    return "failed";
  }
  if (normalized === "in_progress") {
    return "in_progress";
  }
  if (normalized === "pending") {
    return "pending";
  }
  return normalized || "pending";
}

function countImportedByProvider(events, since, until = null) {
  if (!since) {
    return {};
  }
  const sinceDate = new Date(since);
  if (Number.isNaN(sinceDate.valueOf())) {
    return {};
  }
  const untilDate = until ? new Date(until) : null;
  if (untilDate && Number.isNaN(untilDate.valueOf())) {
    return {};
  }
  const counts = {};
  events.forEach((event) => {
    if (!event || !event.event_type || !event.event_type.endsWith("_imported")) {
      return;
    }
    if (!event.source_provider || !(event.created_at || event.occurred_at)) {
      return;
    }
    const occurred = new Date(event.created_at || event.occurred_at);
    if (
      Number.isNaN(occurred.valueOf()) ||
      occurred < sinceDate ||
      (untilDate && occurred > untilDate)
    ) {
      return;
    }
    const provider = event.source_provider;
    counts[provider] = (counts[provider] || 0) + 1;
  });
  return counts;
}

function buildMergeSummary(merge) {
  if (!merge) {
    return null;
  }
  if (merge.error) {
    return {
      detail: "Merge needs attention",
      error: merge.error,
    };
  }
  if (merge.required_at && !merge.completed_at) {
    return {
      detail: "Merging duplicates",
      error: null,
    };
  }
  if (merge.completed_at) {
    return {
      detail: "Merge complete",
      error: null,
    };
  }
  return null;
}

function buildImportEventMetrics(event, events) {
  const label = formatImportType(event.event_type);
  const status = event.import_status || (event.raw ? event.raw.status : null);
  const queue = Array.isArray(event.import_queue)
    ? event.import_queue
    : event.raw && Array.isArray(event.raw.queue)
      ? event.raw.queue
      : [];
  const startAtRaw =
    (event.raw && (event.raw.started_at || event.raw.requested_at)) || event.occurred_at;
  const endAtRaw =
    (event.raw && (event.raw.completed_at || event.raw.started_at)) || event.occurred_at;
  const counts = countImportedByProvider(events, startAtRaw, endAtRaw);
  const merge =
    event.import_merge || (event.raw ? {
      required_at: event.raw.merge_required_at,
      completed_at: event.raw.merge_completed_at,
      error: event.raw.merge_error,
    } : null);
  const mergeSummary = merge ? buildMergeSummary(merge) : null;
  const startAt = startAtRaw ? new Date(startAtRaw) : null;
  const endAt = endAtRaw ? new Date(endAtRaw) : null;
  return { label, status, queue, counts, mergeSummary, startAt, endAt };
}

function buildImportEventItems(event, events, metrics) {
  const startAt = metrics.startAt;
  const endAt = metrics.endAt;
  if (!startAt || !endAt || Number.isNaN(startAt.valueOf()) || Number.isNaN(endAt.valueOf())) {
    return [];
  }
  const providers = metrics.queue.map((provider) => String(provider));
  const providerSet = new Set(providers);
  const filterProviders = providerSet.size > 0;
  const matches = events.filter((entry) => {
    if (!entry || entry.event_category === "import") {
      return false;
    }
    if (!entry.event_type || !entry.event_type.endsWith("_imported")) {
      return false;
    }
    const provider = entry.source_provider;
    if (!provider) {
      return false;
    }
    if (filterProviders && !providerSet.has(String(provider))) {
      return false;
    }
    const timestamp = entry.created_at || entry.occurred_at;
    if (!timestamp) {
      return false;
    }
    const occurred = new Date(timestamp);
    if (Number.isNaN(occurred.valueOf())) {
      return false;
    }
    return occurred >= startAt && occurred <= endAt;
  });
  return matches.sort((a, b) => {
    const aTime = new Date(a.created_at || a.occurred_at || 0).getTime();
    const bTime = new Date(b.created_at || b.occurred_at || 0).getTime();
    return bTime - aTime;
  });
}

function buildImportEventShellLines(event, events) {
  const metrics = buildImportEventMetrics(event, events);
  const providerStats = metrics.queue.map((provider) => {
    const providerLabel = formatProvider(provider);
    const count = metrics.counts[provider] || 0;
    let verb = "Synced";
    if (metrics.status === "pending") {
      verb = "Queued";
    } else if (metrics.status === "in_progress") {
      verb = "Syncing";
    } else if (metrics.status === "failed") {
      verb = "Failed";
    }
    return { verb, providerLabel, count };
  });

  const lines = [];
  lines.push(`${metrics.label}:`);
  if (providerStats.length) {
    let maxPrefix = 0;
    providerStats.forEach((stat) => {
      const prefix = `${stat.verb} ${stat.providerLabel}...`;
      maxPrefix = Math.max(maxPrefix, prefix.length);
    });
    providerStats.forEach((stat) => {
      const prefix = `${stat.verb} ${stat.providerLabel}...`;
      const padded = prefix.padEnd(maxPrefix + 2, " ");
      lines.push(`${padded}${formatItemCountShell(stat.count)}`);
    });
  } else {
    lines.push("No providers.");
  }
  if (metrics.mergeSummary && metrics.mergeSummary.detail) {
    lines.push(`${metrics.mergeSummary.detail}...`);
  }
  if (metrics.mergeSummary && metrics.mergeSummary.error) {
    lines.push(`Merge error: ${metrics.mergeSummary.error}`);
  }
  if (event.import_error) {
    lines.push(`Import error: ${event.import_error}`);
  }
  return lines;
}

function buildOutboxSearchText(job) {
  const parts = [];
  if (job.target_provider) {
    parts.push(job.target_provider);
  }
  if (job.source_provider) {
    parts.push(job.source_provider);
  }
  if (job.job_type) {
    parts.push(job.job_type);
  }
  if (job.status) {
    parts.push(job.status);
  }
  if (job.item) {
    if (job.item.title) {
      parts.push(job.item.title);
    }
    if (job.item.episode_title) {
      parts.push(job.item.episode_title);
    }
    if (job.item.year) {
      parts.push(String(job.item.year));
    }
  }
  if (job.payload) {
    try {
      parts.push(JSON.stringify(job.payload));
    } catch (error) {
      parts.push(String(job.payload));
    }
  }
  return parts.join(" ").toLowerCase();
}

function jobMatchesFilters(job) {
  const statusFilter = activityState.filters.status;
  const providerFilter = activityState.filters.provider;
  const searchFilter = activityState.filters.search.toLowerCase();
  if (statusFilter !== "all" && job.status !== statusFilter) {
    return false;
  }
  if (providerFilter !== "all" && job.target_provider !== providerFilter) {
    return false;
  }
  if (searchFilter) {
    const haystack = buildOutboxSearchText(job);
    if (!haystack.includes(searchFilter)) {
      return false;
    }
  }
  return true;
}

function applyOutboxGroupFilters(groups) {
  const filtered = groups
    .map((group) => {
      const filteredJobs = group.jobs.filter((job) => jobMatchesFilters(job));
      if (!filteredJobs.length) {
        return null;
      }
      const updatedAt = filteredJobs.reduce((latest, job) => {
        const timeValue = job.updated_at || job.created_at;
        if (!timeValue) {
          return latest;
        }
        if (!latest) {
          return timeValue;
        }
        return new Date(timeValue) > new Date(latest) ? timeValue : latest;
      }, null);
      return {
        ...group,
        jobs: filteredJobs,
        updated_at: updatedAt,
      };
    })
    .filter(Boolean);
  return filtered.sort((a, b) => {
    const aTime = a.updated_at ? new Date(a.updated_at).getTime() : 0;
    const bTime = b.updated_at ? new Date(b.updated_at).getTime() : 0;
    return bTime - aTime;
  });
}

function buildOutboxGroups(jobs) {
  const groups = new Map();
  jobs.forEach((job) => {
    const groupKey = job.watched_item_id ? `watched:${job.watched_item_id}` : `job:${job.id}`;
    const group = groups.get(groupKey) || {
      key: groupKey,
      item: null,
      source_provider: null,
      jobs: [],
      updated_at: null,
    };
    if (!group.item && job.item) {
      group.item = job.item;
    }
    if (!group.source_provider && job.source_provider) {
      group.source_provider = job.source_provider;
    }
    group.jobs.push(job);
    const timeValue = job.updated_at || job.created_at;
    if (!group.updated_at || (timeValue && new Date(timeValue) > new Date(group.updated_at))) {
      group.updated_at = timeValue;
    }
    groups.set(groupKey, group);
  });
  return Array.from(groups.values()).sort((a, b) => {
    const aTime = a.updated_at ? new Date(a.updated_at).getTime() : 0;
    const bTime = b.updated_at ? new Date(b.updated_at).getTime() : 0;
    return bTime - aTime;
  });
}

function formatSyncJobType(jobType) {
  const labels = {
    push_watched: "Watched",
    push_rating: "Rating",
    update_history: "History update",
    remove_history: "History removal",
    update_log_entry: "History update",
    delete_log_entry: "History removal",
    remove_watched: "Remove watched",
    new_item_added: "New item",
  };
  return labels[jobType] || formatLabel(jobType);
}

function formatActivityTitle(item) {
  if (!item) {
    return "Unknown item";
  }
  const title = item.title || "Unknown title";
  const yearLabel = item.year ? ` (${item.year})` : "";
  const seasonLabel = formatSeasonEpisode(item.season_number, item.episode_number);
  if (seasonLabel) {
    const episodeTitle = item.episode_title ? ` - ${item.episode_title}` : "";
    return `${title}${yearLabel} ${seasonLabel}${episodeTitle}`;
  }
  return `${title}${yearLabel}`;
}

function createStatusBadge(label, status) {
  const badge = document.createElement("span");
  badge.className = `status-badge ${statusBadgeClass(status)}`;
  badge.textContent = label;
  return badge;
}

function renderOutboxList() {
  const container = document.getElementById("sync-activity");
  if (!container) {
    return;
  }
  container.innerHTML = "";
  const allGroups = buildOutboxGroups(activityState.jobs);
  const groups = applyOutboxGroupFilters(allGroups);
  const visibleCount = Math.max(activityState.jobsVisible || 0, ACTIVITY_PAGE_SIZE);
  const visibleGroups = groups.slice(0, visibleCount);

  const showMore = document.getElementById("sync-show-more");
  if (showMore) {
    showMore.hidden = groups.length <= visibleCount;
  }

  if (!groups.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No sync jobs match your filters.";
    container.appendChild(empty);
    return;
  }
  visibleGroups.forEach((group) => {
    const details = document.createElement("details");
    details.className = "activity-row";
    const summary = document.createElement("summary");

    const main = document.createElement("div");
    main.className = "activity-main";
    const title = document.createElement("div");
    title.className = "activity-title";
    title.textContent = group.item ? formatActivityTitle(group.item) : "Sync item";
    const meta = document.createElement("div");
    meta.className = "activity-meta";
    const metaParts = [];
    if (group.source_provider) {
      metaParts.push(`From ${formatProvider(group.source_provider)}`);
    }
    const jobTypes = Array.from(
      new Set(group.jobs.map((entry) => formatSyncJobType(entry.job_type)).filter(Boolean))
    );
    if (jobTypes.length) {
      metaParts.push(jobTypes.join(" + "));
    }
    meta.textContent = metaParts.join(" · ");
    main.appendChild(title);
    main.appendChild(meta);

    const status = document.createElement("div");
    status.className = "activity-status";
    const badges = document.createElement("div");
    badges.className = "activity-badges";
    const sortedJobs = [...group.jobs].sort((a, b) =>
      String(a.target_provider || "").localeCompare(String(b.target_provider || ""))
    );
    sortedJobs.forEach((job) => {
      const badge = createStatusBadge(formatProvider(job.target_provider), job.status);
      badge.title = formatLabel(job.status);
      badges.appendChild(badge);
    });
    status.appendChild(badges);
    const time = document.createElement("span");
    const timeValue = group.updated_at;
    time.textContent = formatRelativeTime(timeValue);
    time.title = formatMetadataDate(timeValue);
    status.appendChild(time);

    summary.appendChild(main);
    summary.appendChild(status);
    details.appendChild(summary);

    const detail = document.createElement("div");
    detail.className = "activity-detail";
    sortedJobs.forEach((job) => {
      const card = document.createElement("div");
      card.className = "activity-subdetail";
      const header = document.createElement("div");
      header.className = "activity-subdetail-header";
      header.textContent = `${formatProvider(job.target_provider)} - ${formatSyncJobType(
        job.job_type
      )}`;
      const metaLine = document.createElement("div");
      metaLine.className = "activity-subdetail-meta";
      const metaParts = [formatLabel(job.status)];
      if (job.attempts !== undefined && job.attempts !== null) {
        metaParts.push(`${job.attempts} attempt${job.attempts === 1 ? "" : "s"}`);
      }
      const updatedAt = job.updated_at || job.created_at;
      if (updatedAt) {
        metaParts.push(formatMetadataDate(updatedAt));
      }
      metaLine.textContent = metaParts.join(" - ");
      card.appendChild(header);
      card.appendChild(metaLine);

      card.appendChild(buildDetailRow("Job ID", job.id));
      card.appendChild(buildDetailRow("Status", formatLabel(job.status)));
      card.appendChild(buildDetailRow("Next run", formatMetadataDate(job.run_after)));
      if (job.last_error) {
        card.appendChild(buildDetailRow("Last error", job.last_error));
      }
      if (job.sync_attempts && job.sync_attempts.length) {
        const attemptLabel = document.createElement("div");
        attemptLabel.className = "activity-subdetail-meta";
        attemptLabel.textContent = "Recent attempts";
        card.appendChild(attemptLabel);
        const attemptList = document.createElement("div");
        attemptList.className = "activity-attempts";
        job.sync_attempts.forEach((attempt) => {
          const attemptRow = document.createElement("div");
          const attemptParts = [
            formatMetadataDate(attempt.attempted_at),
            formatLabel(attempt.status),
          ];
          if (attempt.response_code !== null && attempt.response_code !== undefined) {
            attemptParts.push(`HTTP ${attempt.response_code}`);
          }
          if (attempt.error) {
            attemptParts.push(attempt.error);
          }
          attemptRow.textContent = attemptParts.join(" - ");
          attemptList.appendChild(attemptRow);
        });
        card.appendChild(attemptList);
      }
      if (job.payload && Object.keys(job.payload).length) {
        const payloadLabel = document.createElement("div");
        payloadLabel.className = "activity-subdetail-meta";
        payloadLabel.textContent = "Payload";
        card.appendChild(payloadLabel);
        const pre = document.createElement("pre");
        pre.textContent = JSON.stringify(maskSensitivePayload(job.payload), null, 2);
        card.appendChild(pre);
      }
      detail.appendChild(card);
    });
    details.appendChild(detail);
    container.appendChild(details);
  });
}

function renderEventsList() {
  const container = document.getElementById("events");
  if (!container) {
    return;
  }
  container.innerHTML = "";
  const totalEvents = activityState.events.length;
  const visibleTotal = Math.max(activityState.eventsVisible || 0, ACTIVITY_PAGE_SIZE);
  const visibleEvents = activityState.events.slice(0, visibleTotal);

  const showMore = document.getElementById("events-show-more");
  if (showMore) {
    showMore.hidden = totalEvents <= visibleTotal;
  }

  if (!totalEvents) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No recent events.";
    container.appendChild(empty);
    return;
  }

  visibleEvents.forEach((event) => {
    const isImportEvent = isImportHistoryEvent(event);
    const isBlacklisted = Boolean(
      event && event.event_type && event.event_type.endsWith("_blacklisted"),
    );
    const importStatus = event.import_status || (event.raw ? event.raw.status : null);
    const importQueue = event.import_queue || (event.raw ? event.raw.queue : null);
    const details = document.createElement("details");
    details.className = isImportEvent ? "activity-row activity-import" : "activity-row";
    const summary = document.createElement("summary");

    const main = document.createElement("div");
    main.className = "activity-main";
    const title = document.createElement("div");
    title.className = "activity-title";
    title.textContent = isImportEvent
      ? formatImportType(event.event_type)
      : formatActivityTitle(event.item);
    const meta = document.createElement("div");
    meta.className = "activity-meta";
    const parts = [];
    if (isImportEvent) {
      if (importStatus) {
        parts.push(formatLabel(importStatus));
      }
      if (Array.isArray(importQueue) && importQueue.length) {
        parts.push(importQueue.map((provider) => formatProvider(provider)).join(" → "));
      }
    } else {
      if (event.source_provider) {
        parts.push(formatProvider(event.source_provider));
      }
      if (event.event_type) {
        parts.push(formatLabel(event.event_type));
      }
    }
    meta.textContent = parts.join(" · ");
    main.appendChild(title);
    main.appendChild(meta);

    const status = document.createElement("div");
    status.className = "activity-status";
    if (isImportEvent) {
      status.appendChild(
        createStatusBadge(formatLabel(importStatus) || "Import", importBadgeStatus(importStatus))
      );
    } else {
      const badgeLabel = isBlacklisted ? "Ignored" : "Event";
      const badgeStatus = isBlacklisted ? "blacklisted" : "succeeded";
      status.appendChild(createStatusBadge(badgeLabel, badgeStatus));
    }
    const time = document.createElement("span");
    time.textContent = formatRelativeTime(event.occurred_at);
    time.title = formatMetadataDate(event.occurred_at);
    status.appendChild(time);

    summary.appendChild(main);
    summary.appendChild(status);
    details.appendChild(summary);

    const detail = document.createElement("div");
    detail.className = "activity-detail";
    if (isImportEvent) {
      const metrics = buildImportEventMetrics(event, activityState.events);
      const shellLines = buildImportEventShellLines(event, activityState.events);
      if (shellLines.length) {
        const pre = document.createElement("pre");
        pre.className = "activity-shell";
        pre.textContent = shellLines.join("\n");
        detail.appendChild(pre);
      }
      const importedItems = buildImportEventItems(event, activityState.events, metrics);
      if (importedItems.length) {
        const tree = document.createElement("div");
        tree.className = "activity-import-tree";
        const providerGroups = new Map();
        importedItems.forEach((entry) => {
          const provider = entry.source_provider || "unknown";
          const group = providerGroups.get(provider) || [];
          group.push(entry);
          providerGroups.set(provider, group);
        });
        const sortedProviders = Array.from(providerGroups.keys()).sort((a, b) =>
          formatProvider(a).localeCompare(formatProvider(b))
        );
        const totalShown = importedItems.length;
        const importLimit = 50;
        let shownCount = 0;
        sortedProviders.forEach((provider) => {
          if (shownCount >= importLimit) {
            return;
          }
          const entries = providerGroups.get(provider) || [];
          const remaining = importLimit - shownCount;
          const visibleEntries = entries.slice(0, remaining);
          if (!visibleEntries.length) {
            return;
          }
          shownCount += visibleEntries.length;
          const branch = document.createElement("div");
          branch.className = "activity-import-branch";
          const header = document.createElement("div");
          header.className = "activity-import-branch-title";
          header.textContent = `${formatProvider(provider)} (${entries.length})`;
          branch.appendChild(header);
          const list = document.createElement("ul");
          list.className = "activity-import-list";
          visibleEntries.forEach((entry) => {
            const item = document.createElement("li");
            item.textContent = formatActivityTitle(entry.item);
            list.appendChild(item);
          });
          branch.appendChild(list);
          tree.appendChild(branch);
        });
        detail.appendChild(tree);
        const remainingCount = totalShown - shownCount;
        if (remainingCount > 0) {
          const more = document.createElement("div");
          more.className = "activity-import-more";
          more.textContent = `…and ${formatItemCount(remainingCount)} more not shown`;
          detail.appendChild(more);
        }
      }
      detail.appendChild(buildDetailRow("Import type", formatImportType(event.event_type)));
      if (importStatus) {
        detail.appendChild(buildDetailRow("Status", formatLabel(importStatus)));
      }
      detail.appendChild(buildDetailRow("Occurred", formatMetadataDate(event.occurred_at)));
      detail.appendChild(buildDetailRow("Recorded", formatMetadataDate(event.created_at)));
      if (Array.isArray(importQueue) && importQueue.length) {
        detail.appendChild(
          buildDetailRow(
            "Providers",
            importQueue.map((provider) => formatProvider(provider)).join(" → ")
          )
        );
      }
      if (event.import_error) {
        detail.appendChild(buildDetailRow("Error", event.import_error));
      }
      if (event.raw) {
        const pre = document.createElement("pre");
        pre.textContent = JSON.stringify(event.raw, null, 2);
        detail.appendChild(pre);
      }
    } else {
      detail.appendChild(buildDetailRow("Event type", formatLabel(event.event_type)));
      detail.appendChild(buildDetailRow("Occurred", formatMetadataDate(event.occurred_at)));
      detail.appendChild(buildDetailRow("Recorded", formatMetadataDate(event.created_at)));
      if (event.raw) {
        const pre = document.createElement("pre");
        pre.textContent = JSON.stringify(event.raw, null, 2);
        detail.appendChild(pre);
      }
    }
    details.appendChild(detail);
    container.appendChild(details);
  });
}

function updateProviderFilterOptions(jobs) {
  const select = document.getElementById("activity-provider-filter");
  if (!select) {
    return;
  }
  const current = select.value || "all";
  const providers = Array.from(
    new Set(jobs.map((job) => job.target_provider).filter(Boolean))
  ).sort();
  select.innerHTML = "";
  const allOption = document.createElement("option");
  allOption.value = "all";
  allOption.textContent = "All providers";
  select.appendChild(allOption);
  providers.forEach((provider) => {
    const option = document.createElement("option");
    option.value = provider;
    option.textContent = formatProvider(provider);
    select.appendChild(option);
  });
  select.value = providers.includes(current) ? current : "all";
}

async function loadActivity(silent = false) {
  const summary = document.getElementById("activity-summary");
  const schedule = document.getElementById("activity-schedule");
  const outbox = document.getElementById("sync-activity");
  const events = document.getElementById("events");
  if (!summary && !schedule && !outbox && !events) {
    return;
  }
  if (!silent) {
    if (summary) {
      summary.textContent = "Loading...";
    }
    if (schedule) {
      schedule.textContent = "Loading...";
    }
    if (outbox) {
      outbox.textContent = "Loading...";
    }
    if (events) {
      events.textContent = "Loading...";
    }
  }
  setMessage("activity-message", "");
  try {
    const [statusData, outboxData, eventsData] = await Promise.all([
      requestJSON("/api/status"),
      requestJSON("/api/outbox?limit=100"),
      requestJSON("/api/activity/events?limit=100"),
    ]);
    activityState.status = statusData;
    activityState.jobs = outboxData && outboxData.jobs ? outboxData.jobs : [];
    activityState.events =
      eventsData && eventsData.events ? eventsData.events : [];
    activityState.lastRefresh = new Date();
    activityState.eventsVisible = Math.max(
      activityState.eventsVisible || 0,
      ACTIVITY_PAGE_SIZE
    );
    activityState.jobsVisible = Math.max(
      activityState.jobsVisible || 0,
      ACTIVITY_PAGE_SIZE
    );
    updateProviderFilterOptions(activityState.jobs);
    renderActivitySummary(statusData);
    renderSchedule(statusData);
    renderOutboxList();
    renderEventsList();
  } catch (error) {
    setMessage("activity-message", error.message, true);
  }
}

function bindActivityControls() {
  const searchInput = document.getElementById("activity-search");
  if (searchInput) {
    searchInput.addEventListener("input", () => {
      activityState.filters.search = searchInput.value || "";
      renderOutboxList();
    });
  }
  const statusSelect = document.getElementById("activity-status-filter");
  if (statusSelect) {
    statusSelect.addEventListener("change", () => {
      activityState.filters.status = statusSelect.value || "all";
      renderOutboxList();
    });
  }
  const providerSelect = document.getElementById("activity-provider-filter");
  if (providerSelect) {
    providerSelect.addEventListener("change", () => {
      activityState.filters.provider = providerSelect.value || "all";
      renderOutboxList();
    });
  }
  const refreshButton = document.getElementById("activity-refresh");
  if (refreshButton) {
    refreshButton.addEventListener("click", () => loadActivity());
  }
  const eventsMore = document.getElementById("events-show-more");
  if (eventsMore) {
    eventsMore.addEventListener("click", () => {
      activityState.eventsVisible += ACTIVITY_PAGE_SIZE;
      renderEventsList();
    });
  }
  const syncMore = document.getElementById("sync-show-more");
  if (syncMore) {
    syncMore.addEventListener("click", () => {
      activityState.jobsVisible += ACTIVITY_PAGE_SIZE;
      renderOutboxList();
    });
  }
}

function startActivityAutoRefresh() {
  if (activityState.timer) {
    return;
  }
  activityState.timer = window.setInterval(() => {
    loadActivity(true);
  }, 30000);
}

window.librarysyncPageInit = async ({ user }) => {
  if (!user) {
    return;
  }
  bindActivityControls();
  await loadActivity();
  if (document.getElementById("activity-summary")) {
    startActivityAutoRefresh();
  }
};
