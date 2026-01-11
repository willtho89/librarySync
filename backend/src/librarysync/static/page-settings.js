const ACTIVITY_PAGE_SIZE = 10;
const activityState = {
  jobs: [],
  events: [],
  lastRefresh: null,
  eventsVisible: ACTIVITY_PAGE_SIZE,
  jobsVisible: ACTIVITY_PAGE_SIZE,
  filters: {
    status: "all",
    provider: "all",
    search: "",
  },
};

const settingsState = {
  hasAnyImports: false,
  status: null,
  importQueue: [],
  watchlistSources: [],
};
const DEFAULT_IMPORT_QUEUE_ORDER = [
  "trakt",
  "letterboxd",
  "simkl",
  "anilist",
  "stremio",
  "aiostreams",
];
const WATCHLIST_PROVIDER_LABELS = {
  trakt: "Trakt",
  letterboxd: "Letterboxd",
};

function applyQuickImportControls(statusData) {
  const form = document.getElementById("quick-import-form");
  if (!form) {
    return;
  }
  const select = form.querySelector("select[name='quick_import_interval']");
  const lastEl = document.getElementById("quick-import-last");
  const importNow = document.getElementById("quick-import-now");
  const quickImport =
    statusData && statusData.imports ? statusData.imports.quick : null;
  const intervalSeconds = quickImport ? quickImport.interval_seconds : null;
  if (select) {
    select.value =
      intervalSeconds && intervalSeconds > 0 ? String(intervalSeconds) : "";
    select.disabled = !settingsState.hasAnyImports;
  }
  if (importNow) {
    importNow.disabled = !settingsState.hasAnyImports;
  }
  if (!settingsState.hasAnyImports) {
    if (lastEl) {
      lastEl.textContent = "Connect an integration to enable quick import.";
    }
    setMessage("quick-import-message", "");
    return;
  }
  if (lastEl) {
    const lastRun = quickImport ? quickImport.last_run_at : null;
    lastEl.textContent = formatImportTimestamp(lastRun);
  }
}

function setIntegrationStatusBadge(id, connected) {
  const badge = document.getElementById(id);
  if (!badge) {
    return;
  }
  badge.textContent = connected ? "Connected" : "Not connected";
  badge.dataset.state = connected ? "connected" : "disconnected";
}

function formatWatchlistSourceLabel(source) {
  const providerLabel =
    WATCHLIST_PROVIDER_LABELS[source.provider] || source.provider || "Unknown";
  const name = source.name ? source.name.replace(/-/g, " ") : "Watchlist";
  if (name.toLowerCase().includes(providerLabel.toLowerCase())) {
    return name;
  }
  return `${providerLabel}: ${name}`;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

async function loadStatusData() {
  try {
    const [statusData, outboxData, eventsData] = await Promise.all([
      requestJSON("/api/status"),
      requestJSON("/api/outbox?limit=100"),
      requestJSON("/api/activity/events?limit=100"),
    ]);
    settingsState.status = statusData;
    activityState.jobs = outboxData && outboxData.jobs ? outboxData.jobs : [];
    activityState.events =
      eventsData && eventsData.events ? eventsData.events : [];
    activityState.lastRefresh = new Date();
    // Ensure we don't shrink the list on refresh if user expanded it
    activityState.eventsVisible = Math.max(
      activityState.eventsVisible || 0,
      ACTIVITY_PAGE_SIZE
    );
    activityState.jobsVisible = Math.max(
      activityState.jobsVisible || 0,
      ACTIVITY_PAGE_SIZE
    );

    applyQuickImportControls(statusData);
    if (typeof renderMaintenanceSummary === "function") {
      renderMaintenanceSummary(statusData);
    }
    if (typeof renderMaintenanceSchedule === "function") {
      renderMaintenanceSchedule(statusData);
    }
    if (typeof renderOutboxList === "function") {
      updateProviderFilterOptions(activityState.jobs);
      renderOutboxList();
    }
    if (typeof renderEventsList === "function") {
      renderEventsList();
    }
  } catch (error) {
    console.error("status load failed", error);
  }
}

function arraysEqual(left, right) {
  if (!Array.isArray(left) || !Array.isArray(right)) {
    return false;
  }
  if (left.length !== right.length) {
    return false;
  }
  for (let i = 0; i < left.length; i += 1) {
    if (left[i] !== right[i]) {
      return false;
    }
  }
  return true;
}

function readImportQueueOrder(list) {
  if (!list) {
    return [];
  }
  return Array.from(list.querySelectorAll(".import-queue-item"))
    .map((item) => item.dataset.provider)
    .filter(Boolean);
}

function updateImportQueueIndices(list) {
  if (!list) {
    return;
  }
  const items = Array.from(list.querySelectorAll(".import-queue-item"));
  items.forEach((item, index) => {
    const indexEl = item.querySelector("[data-queue-index]");
    if (indexEl) {
      indexEl.textContent = `#${index + 1}`;
    }
  });
}

function buildDefaultImportQueueOrder(queue) {
  const current = Array.isArray(queue) ? queue.map((entry) => String(entry)) : [];
  const ordered = [];
  DEFAULT_IMPORT_QUEUE_ORDER.forEach((provider) => {
    if (current.includes(provider) && !ordered.includes(provider)) {
      ordered.push(provider);
    }
  });
  current.forEach((provider) => {
    if (!ordered.includes(provider)) {
      ordered.push(provider);
    }
  });
  return ordered;
}

function bindImportQueueDrag(list) {
  if (!list || list.dataset.dragBound === "true") {
    return;
  }
  list.dataset.dragBound = "true";
  let draggedItem = null;
  let overItem = null;

  list.addEventListener("dragstart", (event) => {
    const item = event.target.closest(".import-queue-item");
    if (!item) {
      return;
    }
    draggedItem = item;
    item.classList.add("is-dragging");
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", item.dataset.provider || "");
    }
  });

  list.addEventListener("dragover", (event) => {
    if (!draggedItem) {
      return;
    }
    event.preventDefault();
    const target = event.target.closest(".import-queue-item");
    if (!target || target === draggedItem) {
      return;
    }
    const rect = target.getBoundingClientRect();
    const shouldInsertBefore = event.clientY < rect.top + rect.height / 2;
    const referenceNode = shouldInsertBefore ? target : target.nextElementSibling;
    if (referenceNode !== draggedItem) {
      list.insertBefore(draggedItem, referenceNode);
    }
    if (overItem && overItem !== target) {
      overItem.classList.remove("is-over");
    }
    overItem = target;
    overItem.classList.add("is-over");
  });

  list.addEventListener("dragleave", (event) => {
    const target = event.target.closest(".import-queue-item");
    if (target && target === overItem) {
      target.classList.remove("is-over");
      overItem = null;
    }
  });

  list.addEventListener("drop", (event) => {
    if (!draggedItem) {
      return;
    }
    event.preventDefault();
    if (overItem) {
      overItem.classList.remove("is-over");
      overItem = null;
    }
  });

  list.addEventListener("dragend", () => {
    if (!draggedItem) {
      return;
    }
    draggedItem.classList.remove("is-dragging");
    draggedItem = null;
    if (overItem) {
      overItem.classList.remove("is-over");
      overItem = null;
    }
    updateImportQueueIndices(list);
    const order = readImportQueueOrder(list);
    if (!arraysEqual(order, settingsState.importQueue)) {
      void saveImportQueueOrder(order);
    }
  });
}

function renderImportQueue(queue) {
  const list = document.getElementById("import-queue-list");
  const empty = document.getElementById("import-queue-empty");
  if (!list || !empty) {
    return;
  }
  list.innerHTML = "";
  const providers = Array.isArray(queue) ? queue.map((entry) => String(entry)) : [];
  settingsState.importQueue = providers;
  if (!providers.length) {
    list.hidden = true;
    empty.hidden = false;
    return;
  }
  list.hidden = false;
  empty.hidden = true;
  providers.forEach((provider) => {
    const label = formatIntegrationName(provider);
    const item = document.createElement("li");
    item.className = "import-queue-item";
    item.draggable = true;
    item.dataset.provider = provider;

    const main = document.createElement("div");
    main.className = "import-queue-main";

    const handle = document.createElement("button");
    handle.type = "button";
    handle.className = "import-queue-handle";
    handle.draggable = true;
    handle.textContent = "Drag";
    handle.setAttribute("aria-label", `Drag to reorder ${label}`);

    const name = document.createElement("span");
    name.className = "import-queue-name";
    name.textContent = label;

    main.appendChild(handle);
    main.appendChild(name);

    const indexEl = document.createElement("span");
    indexEl.className = "import-queue-index";
    indexEl.dataset.queueIndex = "true";

    item.appendChild(main);
    item.appendChild(indexEl);
    list.appendChild(item);
  });
  updateImportQueueIndices(list);
  bindImportQueueDrag(list);
}

async function loadImportQueue() {
  const list = document.getElementById("import-queue-list");
  if (!list) {
    return;
  }
  try {
    const data = await requestJSON("/api/integrations/import/queue");
    const queue = data && Array.isArray(data.queue) ? data.queue : [];
    renderImportQueue(queue);
    setMessage("import-queue-message", "");
  } catch (error) {
    console.error("import queue load failed", error);
    setMessage("import-queue-message", error.message, true);
  }
}

async function saveImportQueueOrder(order) {
  const previousOrder = settingsState.importQueue.slice();
  setMessage("import-queue-message", "Saving order...");
  try {
    const response = await requestJSON("/api/integrations/import/queue", {
      method: "POST",
      body: JSON.stringify({ order }),
    });
    const queue =
      response && Array.isArray(response.queue) ? response.queue : order;
    settingsState.importQueue = queue.map((entry) => String(entry));
    setMessage("import-queue-message", "Import queue updated.");
    renderImportQueue(settingsState.importQueue);
  } catch (error) {
    settingsState.importQueue = previousOrder;
    renderImportQueue(previousOrder);
    setMessage("import-queue-message", error.message, true);
  }
}

async function handleImportQueueRestore() {
  const order = buildDefaultImportQueueOrder(settingsState.importQueue);
  if (!order.length) {
    setMessage(
      "import-queue-message",
      "Connect an integration to create an import queue.",
      true
    );
    return;
  }
  if (arraysEqual(order, settingsState.importQueue)) {
    setMessage("import-queue-message", "Import queue already matches the default.");
    return;
  }
  await saveImportQueueOrder(order);
}

async function loadIntegrations() {
  const data = await requestJSON("/api/integrations");
  const integrations = data && data.integrations ? data.integrations : [];
  const letterboxdForm = document.getElementById("letterboxd-form");
  const letterboxd = integrations.find((item) => item.provider === "letterboxd");
  if (letterboxdForm) {
    const apiBaseInput = letterboxdForm.querySelector(
      "input[name='api_base_url']"
    );
    if (
      letterboxd &&
      letterboxd.config &&
      letterboxd.config.api_base_url &&
      apiBaseInput
    ) {
      apiBaseInput.value = letterboxd.config.api_base_url;
    }
    const letterboxdMessage = document.getElementById("letterboxd-message");
    const letterboxdDisconnect = document.getElementById("letterboxd-disconnect");
    const letterboxdConnected = isIntegrationConnected(letterboxd);
    setIntegrationStatusBadge("letterboxd-status", letterboxdConnected);
    if (letterboxdConnected) {
      if (letterboxdMessage && !letterboxdMessage.textContent) {
        setMessage("letterboxd-message", "Credentials are stored securely.");
      }
      if (letterboxdDisconnect) {
        letterboxdDisconnect.hidden = false;
      }
    } else {
      setMessage("letterboxd-message", "");
      if (letterboxdDisconnect) {
        letterboxdDisconnect.hidden = true;
      }
    }
  }
  const trakt = integrations.find((item) => item.provider === "trakt");
  const traktMessage = document.getElementById("trakt-message");
  const traktConnect = document.getElementById("trakt-connect");
  const traktDisconnect = document.getElementById("trakt-disconnect");
  const traktConnected = isIntegrationConnected(trakt);
  setIntegrationStatusBadge("trakt-status", traktConnected);
  if (traktConnected) {
    const username =
      trakt.config && trakt.config.trakt_username
        ? trakt.config.trakt_username
        : null;
    const label = username
      ? `Connected as ${username}.`
      : "Trakt connection is active.";
    setMessage("trakt-message", label);
    if (traktConnect) {
      traktConnect.hidden = true;
    }
    if (traktDisconnect) {
      traktDisconnect.hidden = false;
    }
  } else {
    setMessage("trakt-message", "");
    if (traktConnect) {
      traktConnect.hidden = false;
    }
    if (traktDisconnect) {
      traktDisconnect.hidden = true;
    }
  }
  const simkl = integrations.find((item) => item.provider === "simkl");
  const simklMessage = document.getElementById("simkl-message");
  const simklConnect = document.getElementById("simkl-connect");
  const simklDisconnect = document.getElementById("simkl-disconnect");
  const simklConnected = isIntegrationConnected(simkl);
  setIntegrationStatusBadge("simkl-status", simklConnected);
  if (simklConnected) {
    const username =
      simkl.config && simkl.config.simkl_username ? simkl.config.simkl_username : null;
    const label = username
      ? `Connected as ${username}.`
      : "SIMKL connection is active.";
    setMessage("simkl-message", label);
    if (simklConnect) {
      simklConnect.hidden = true;
    }
    if (simklDisconnect) {
      simklDisconnect.hidden = false;
    }
  } else {
    setMessage("simkl-message", "");
    if (simklConnect) {
      simklConnect.hidden = false;
    }
    if (simklDisconnect) {
      simklDisconnect.hidden = true;
    }
  }

  const anilist = integrations.find((item) => item.provider === "anilist");
  const anilistMessage = document.getElementById("anilist-message");
  const anilistConnect = document.getElementById("anilist-connect");
  const anilistDisconnect = document.getElementById("anilist-disconnect");
  const anilistConnected = isIntegrationConnected(anilist);
  setIntegrationStatusBadge("anilist-status", anilistConnected);
  if (anilistConnected) {
    const username =
      anilist.config && anilist.config.anilist_username ? anilist.config.anilist_username : null;
    const label = username
      ? `Connected as ${username}.`
      : "AniList connection is active.";
    setMessage("anilist-message", label);
    if (anilistConnect) {
      anilistConnect.hidden = true;
    }
    if (anilistDisconnect) {
      anilistDisconnect.hidden = false;
    }
  } else {
    setMessage("anilist-message", "");
    if (anilistConnect) {
      anilistConnect.hidden = false;
    }
    if (anilistDisconnect) {
      anilistDisconnect.hidden = true;
    }
  }

  const stremio = integrations.find((item) => item.provider === "stremio");
  const stremioForm = document.getElementById("stremio-form");
  if (stremioForm) {
    const apiBaseInput = stremioForm.querySelector("input[name='api_base_url']");
    if (stremio && stremio.config && stremio.config.api_base_url && apiBaseInput) {
      apiBaseInput.value = stremio.config.api_base_url;
    }
  }
  const stremioMessage = document.getElementById("stremio-message");
  const stremioDisconnect = document.getElementById("stremio-disconnect");
  const stremioConnected = isIntegrationConnected(stremio);
  setIntegrationStatusBadge("stremio-status", stremioConnected);
  if (stremioConnected) {
    const name =
      stremio.config && stremio.config.stremio_name ? stremio.config.stremio_name : null;
    const email =
      stremio.config && stremio.config.stremio_email ? stremio.config.stremio_email : null;
    const label = name || email ? `Connected as ${name || email}.` : "Stremio is connected.";
    setMessage("stremio-message", label);
    if (stremioDisconnect) {
      stremioDisconnect.hidden = false;
    }
  } else {
    setMessage("stremio-message", "");
    if (stremioDisconnect) {
      stremioDisconnect.hidden = true;
    }
  }

  const aiostreams = integrations.find((item) => item.provider === "aiostreams");
  const aiostreamsForm = document.getElementById("aiostreams-form");
  if (aiostreamsForm) {
    const apiBaseInput = aiostreamsForm.querySelector("input[name='api_base_url']");
    const usernameInput = aiostreamsForm.querySelector("input[name='username']");
    if (aiostreams && aiostreams.config && aiostreams.config.api_base_url && apiBaseInput) {
      apiBaseInput.value = aiostreams.config.api_base_url;
    }
    if (aiostreams && aiostreams.config && aiostreams.config.username && usernameInput) {
      usernameInput.value = aiostreams.config.username;
    }
  }
  const aiostreamsMessage = document.getElementById("aiostreams-message");
  const aiostreamsDisconnect = document.getElementById("aiostreams-disconnect");
  const aiostreamsConnected = isIntegrationConnected(aiostreams);
  setIntegrationStatusBadge("aiostreams-status", aiostreamsConnected);
  if (aiostreamsConnected) {
    const username =
      aiostreams && aiostreams.config && aiostreams.config.username
        ? aiostreams.config.username
        : null;
    const label = username
      ? `Connected as ${username}.`
      : "AIOStreams Proxy is connected.";
    setMessage("aiostreams-message", label);
    if (aiostreamsDisconnect) {
      aiostreamsDisconnect.hidden = false;
    }
  } else {
    setMessage("aiostreams-message", "");
    if (aiostreamsDisconnect) {
      aiostreamsDisconnect.hidden = true;
    }
  }
  settingsState.hasAnyImports = integrations.some((item) => item.has_secrets);
  const importAllButton = document.getElementById("import-all-button");
  if (importAllButton) {
    importAllButton.disabled = !settingsState.hasAnyImports;
  }
  applyQuickImportControls(settingsState.status);
  await loadWatchlistSources();
  await loadImportQueue();
}

function parseCookieString(cookieHeader) {
  const cookies = {};
  if (!cookieHeader) {
    return cookies;
  }
  cookieHeader.split(";").forEach((part) => {
    const trimmed = part.trim();
    if (!trimmed) {
      return;
    }
    const idx = trimmed.indexOf("=");
    if (idx === -1) {
      return;
    }
    const name = trimmed.slice(0, idx).trim();
    const value = trimmed.slice(idx + 1).trim();
    if (name && value) {
      cookies[name] = value;
    }
  });
  return cookies;
}

function extractCookieHeaders(command) {
  const cookies = {};
  const headerRegex = /(?:-H|--header)\s+(['"])(Cookie:\s*[^'"]+)\1/gi;
  let match = null;
  while ((match = headerRegex.exec(command)) !== null) {
    const headerValue = match[2];
    const cookiePart = headerValue.replace(/^Cookie:\s*/i, "");
    Object.assign(cookies, parseCookieString(cookiePart));
  }
  const cookieRegex = /(?:-b|--cookie)\s+(['"])([^'"]+)\1/gi;
  while ((match = cookieRegex.exec(command)) !== null) {
    const cookieValue = match[2];
    if (cookieValue.startsWith("@")) {
      continue;
    }
    Object.assign(cookies, parseCookieString(cookieValue));
  }
  const inlineCookieRegex = /Cookie:\s*([^'"\n\r]+)/gi;
  while ((match = inlineCookieRegex.exec(command)) !== null) {
    Object.assign(cookies, parseCookieString(match[1]));
  }
  return cookies;
}

function extractDataSegments(command) {
  const segments = [];
  const dataRegex =
    /(?:-d|--data(?:-raw|-urlencode)?|--raw)\s+(['"])(.*?)\1/gi;
  let match = null;
  while ((match = dataRegex.exec(command)) !== null) {
    segments.push(match[2]);
  }
  const dataBareRegex =
    /(?:-d|--data(?:-raw|-urlencode)?|--raw)\s+([^\s]+)/gi;
  while ((match = dataBareRegex.exec(command)) !== null) {
    const value = match[1];
    if (!value.startsWith("-")) {
      segments.push(value);
    }
  }
  return segments;
}

function parseKeyValues(segment) {
  const values = {};
  if (!segment) {
    return values;
  }
  const parts = segment.split("&");
  parts.forEach((part) => {
    const idx = part.indexOf("=");
    if (idx === -1) {
      return;
    }
    const rawKey = part.slice(0, idx);
    const rawValue = part.slice(idx + 1);
    const key = decodeURIComponent(rawKey.replace(/\+/g, " ")).trim();
    const value = decodeURIComponent(rawValue.replace(/\+/g, " ")).trim();
    if (key && value && values[key] === undefined) {
      values[key] = value;
    }
  });
  return values;
}

function extractUrlParams(command) {
  const values = {};
  const urlMatch = command.match(/https?:\/\/[^\s'"]+/i);
  if (!urlMatch) {
    return values;
  }
  try {
    const url = new URL(urlMatch[0]);
    url.searchParams.forEach((value, key) => {
      if (value && values[key] === undefined) {
        values[key] = value;
      }
    });
  } catch (error) {
    return values;
  }
  return values;
}

function extractCredential(command, key) {
  const segments = extractDataSegments(command);
  for (const segment of segments) {
    const trimmed = segment.trim();
    if (
      (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
      (trimmed.startsWith("[") && trimmed.endsWith("]"))
    ) {
      try {
        const parsed = JSON.parse(trimmed);
        if (parsed && typeof parsed === "object" && parsed[key]) {
          return String(parsed[key]);
        }
      } catch (error) {
        // ignore json errors
      }
    }
    const params = parseKeyValues(trimmed);
    if (params[key]) {
      return String(params[key]);
    }
  }
  const urlParams = extractUrlParams(command);
  if (urlParams[key]) {
    return String(urlParams[key]);
  }
  const keyRegex = new RegExp(
    `${key}\\s*(?:==|=|:=)\\s*('[^']*'|"[^"]*"|[^\\s&]+)`,
    "i"
  );
  const match = command.match(keyRegex);
  if (match) {
    return match[1].replace(/^['"]|['"]$/g, "");
  }
  return null;
}

function parseLetterboxdCommand(command) {
  const cookies = extractCookieHeaders(command);
  return {
    client_id: extractCredential(command, "client_id"),
    client_secret: extractCredential(command, "client_secret"),
    refresh_token: extractCredential(command, "refresh_token"),
    cookies: Object.keys(cookies).length ? cookies : null,
  };
}

function handleLetterboxdParse() {
  const commandInput = document.getElementById("letterboxd-command");
  if (!commandInput) {
    return;
  }
  const command = commandInput.value || "";
  if (!command.trim()) {
    setMessage("letterboxd-message", "Paste a curl/httpie command first.", true);
    return;
  }
  const parsed = parseLetterboxdCommand(command);
  const form = document.getElementById("letterboxd-form");
  if (!form) {
    return;
  }
  const fields = [];
  if (parsed.client_id) {
    const input = form.querySelector("input[name='client_id']");
    if (input) {
      input.value = parsed.client_id;
      fields.push("client_id");
    }
  }
  if (parsed.client_secret) {
    const input = form.querySelector("input[name='client_secret']");
    if (input) {
      input.value = parsed.client_secret;
      fields.push("client_secret");
    }
  }
  if (parsed.refresh_token) {
    const input = form.querySelector("input[name='refresh_token']");
    if (input) {
      input.value = parsed.refresh_token;
      fields.push("refresh_token");
    }
  }
  if (parsed.cookies) {
    const input = form.querySelector("textarea[name='cookies']");
    if (input) {
      input.value = JSON.stringify(parsed.cookies, null, 2);
      fields.push("cookies");
    }
  }
  if (!fields.length) {
    setMessage("letterboxd-message", "No Letterboxd fields found.", true);
  } else {
    setMessage(
      "letterboxd-message",
      `Parsed: ${fields.join(", ")}. Review and save.`
    );
  }
}

function renderWatchlistSources(sources) {
  const list = document.getElementById("watchlist-sources-list");
  const empty = document.getElementById("watchlist-sources-empty");
  if (!list) {
    return;
  }
  if (!Array.isArray(sources) || !sources.length) {
    list.innerHTML = "";
    if (empty) {
      empty.hidden = false;
    }
    return;
  }
  if (empty) {
    empty.hidden = true;
  }
  list.innerHTML = sources
    .map((source) => {
      const label = escapeHtml(formatWatchlistSourceLabel(source));
      const urlLine = source.url ? escapeHtml(source.url) : "Personal watchlist";
      const provider = escapeHtml(source.provider || "unknown");
      const checked = source.is_enabled ? "checked" : "";
      const disableLabel = source.is_enabled ? "Enabled" : "Disabled";
      const syncDisabled = source.is_enabled ? "" : "disabled";
      const syncButton = `
        <button class="btn btn-ghost" type="button" data-action="sync" data-id="${escapeHtml(
          source.id
        )}" ${syncDisabled}>Sync now</button>
      `;
      const deleteButton = source.is_deletable
        ? `<button class="btn btn-ghost" type="button" data-action="delete" data-id="${escapeHtml(
            source.id
          )}">Remove</button>`
        : "";
      return `
        <li class="card card-muted">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p class="section-title">${label}</p>
              <p class="helper-text">${provider} · ${urlLine}</p>
            </div>
            <div class="flex items-center gap-2">
              <label class="inline-control">
                <input type="checkbox" data-action="toggle" data-id="${escapeHtml(
                  source.id
                )}" ${checked} />
                <span>${disableLabel}</span>
              </label>
              ${syncButton}
              ${deleteButton}
            </div>
          </div>
        </li>
      `;
    })
    .join("");
}

async function loadWatchlistSources() {
  try {
    const data = await requestJSON("/api/watchlist/sources");
    const sources = data && Array.isArray(data.sources) ? data.sources : [];
    settingsState.watchlistSources = sources;
    renderWatchlistSources(sources);
  } catch (error) {
    console.error("watchlist sources load failed", error);
  }
}

async function handleWatchlistSourceAdd(data) {
  setMessage("watchlist-source-message", "");
  const url = data.get("url");
  if (!url) {
    setMessage("watchlist-source-message", "Enter a watchlist URL.", true);
    return;
  }
  try {
    const response = await requestJSON("/api/watchlist/sources", {
      method: "POST",
      body: JSON.stringify({ url }),
    });
    const imported =
      response && typeof response.imported === "number" ? response.imported : null;
    if (response && response.sync_error) {
      setMessage(
        "watchlist-source-message",
        `Watchlist added, but sync failed: ${response.sync_error}`,
        true
      );
    } else if (imported !== null) {
      setMessage(
        "watchlist-source-message",
        `Watchlist added. Imported ${imported} items.`
      );
    } else {
      setMessage("watchlist-source-message", "Watchlist added.");
    }
    await loadWatchlistSources();
  } catch (error) {
    setMessage("watchlist-source-message", error.message, true);
  }
}

async function handleWatchlistSourceToggle(sourceId, isEnabled) {
  setMessage("watchlist-sources-message", "");
  try {
    await requestJSON(`/api/watchlist/sources/${sourceId}`, {
      method: "PATCH",
      body: JSON.stringify({ is_enabled: isEnabled }),
    });
    await loadWatchlistSources();
  } catch (error) {
    setMessage("watchlist-sources-message", error.message, true);
  }
}

async function handleWatchlistSourceDelete(sourceId) {
  const confirmed = window.confirm("Remove this watchlist source?");
  if (!confirmed) {
    return;
  }
  setMessage("watchlist-sources-message", "");
  try {
    await requestJSON(`/api/watchlist/sources/${sourceId}`, {
      method: "DELETE",
    });
    await loadWatchlistSources();
  } catch (error) {
    setMessage("watchlist-sources-message", error.message, true);
  }
}

async function handleWatchlistSourceSync(sourceId) {
  setMessage("watchlist-sources-message", "");
  try {
    const response = await requestJSON(
      `/api/watchlist/sources/${sourceId}/sync`,
      {
        method: "POST",
      }
    );
    const imported =
      response && typeof response.imported === "number" ? response.imported : 0;
    setMessage(
      "watchlist-sources-message",
      `Watchlist synced. Imported ${imported} items.`
    );
    await loadWatchlistSources();
  } catch (error) {
    setMessage("watchlist-sources-message", error.message, true);
  }
}

function bindWatchlistSourceActions() {
  const list = document.getElementById("watchlist-sources-list");
  if (!list) {
    return;
  }
  list.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const action = target.dataset.action;
    const sourceId = target.dataset.id;
    if (!action || !sourceId) {
      return;
    }
    if (action === "delete") {
      handleWatchlistSourceDelete(sourceId);
    }
    if (action === "sync") {
      handleWatchlistSourceSync(sourceId);
    }
  });
  list.addEventListener("change", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) {
      return;
    }
    if (target.dataset.action !== "toggle") {
      return;
    }
    const sourceId = target.dataset.id;
    if (!sourceId) {
      return;
    }
    handleWatchlistSourceToggle(sourceId, target.checked);
  });
}

async function handleLetterboxdSave(data, form) {
  setMessage("letterboxd-message", "");
  const apiBaseUrl = (data.get("api_base_url") || "").trim();
  const clientId = (data.get("client_id") || "").trim();
  const clientSecret = (data.get("client_secret") || "").trim();
  const refreshToken = (data.get("refresh_token") || "").trim();
  const cookiesRaw = (data.get("cookies") || "").trim();
  const payload = {};
  if (apiBaseUrl) {
    payload.api_base_url = apiBaseUrl;
  }
  if (clientId) {
    payload.client_id = clientId;
  }
  if (clientSecret) {
    payload.client_secret = clientSecret;
  }
  if (refreshToken) {
    payload.refresh_token = refreshToken;
  }
  if (cookiesRaw) {
    try {
      const parsed = JSON.parse(cookiesRaw);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("Cookies JSON must be an object.");
      }
      payload.cookies = parsed;
    } catch (error) {
      setMessage("letterboxd-message", "Invalid cookies JSON.", true);
      return;
    }
  }
  if (!Object.keys(payload).length) {
    setMessage("letterboxd-message", "Enter credentials to save.", true);
    return;
  }
  try {
    await requestJSON("/api/integrations/letterboxd", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const secretInputs = ["client_id", "client_secret", "refresh_token"];
    secretInputs.forEach((name) => {
      const input = form.querySelector(`input[name='${name}']`);
      if (input) {
        input.value = "";
      }
    });
    const cookiesInput = form.querySelector("textarea[name='cookies']");
    if (cookiesInput) {
      cookiesInput.value = "";
    }
    setMessage("letterboxd-message", "Saved.");
    await loadIntegrations();
  } catch (error) {
    setMessage("letterboxd-message", error.message, true);
  }
}

async function handleLetterboxdTest() {
  setMessage("letterboxd-message", "");
  try {
    await requestJSON("/api/integrations/letterboxd/test", { method: "POST" });
    setMessage("letterboxd-message", "Connection test succeeded.");
    await loadIntegrations();
  } catch (error) {
    setMessage("letterboxd-message", error.message, true);
  }
}

async function handleLetterboxdDisconnect() {
  setMessage("letterboxd-message", "");
  try {
    await requestJSON("/api/integrations/letterboxd/disconnect", { method: "POST" });
    setMessage("letterboxd-message", "Disconnected.");
    await loadIntegrations();
  } catch (error) {
    setMessage("letterboxd-message", error.message, true);
  }
}

function handleTraktConnect() {
  window.location.href = "/api/integrations/trakt/start";
}

async function handleTraktDisconnect() {
  setMessage("trakt-message", "");
  try {
    await requestJSON("/api/integrations/trakt/disconnect", { method: "POST" });
    setMessage("trakt-message", "Disconnected.");
    await loadIntegrations();
  } catch (error) {
    setMessage("trakt-message", error.message, true);
  }
}

function handleSimklConnect() {
  window.location.href = "/api/integrations/simkl/start";
}

async function handleSimklDisconnect() {
  setMessage("simkl-message", "");
  try {
    await requestJSON("/api/integrations/simkl/disconnect", { method: "POST" });
    setMessage("simkl-message", "Disconnected.");
    await loadIntegrations();
  } catch (error) {
    setMessage("simkl-message", error.message, true);
  }
}

function handleAniListConnect() {
  window.location.href = "/api/integrations/anilist/start";
}

async function handleAniListDisconnect() {
  setMessage("anilist-message", "");
  try {
    await requestJSON("/api/integrations/anilist/disconnect", { method: "POST" });
    setMessage("anilist-message", "Disconnected.");
    await loadIntegrations();
  } catch (error) {
    setMessage("anilist-message", error.message, true);
  }
}

async function handleStremioConnect(data, form) {
  setMessage("stremio-message", "");
  const email = (data.get("email") || "").trim();
  const password = (data.get("password") || "").trim();
  const apiBaseUrl = (data.get("api_base_url") || "").trim();
  if (!email || !password) {
    setMessage("stremio-message", "Enter your Stremio email and password.", true);
    return;
  }
  const payload = {
    email,
    password,
  };
  if (apiBaseUrl) {
    payload.api_base_url = apiBaseUrl;
  }
  try {
    await requestJSON("/api/integrations/stremio/login", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const input = form.querySelector("input[name='password']");
    if (input) {
      input.value = "";
    }
    setMessage("stremio-message", "Connected.");
    await loadIntegrations();
  } catch (error) {
    setMessage("stremio-message", error.message, true);
  }
}

async function handleStremioDisconnect() {
  setMessage("stremio-message", "");
  try {
    await requestJSON("/api/integrations/stremio/disconnect", { method: "POST" });
    setMessage("stremio-message", "Disconnected.");
    await loadIntegrations();
  } catch (error) {
    setMessage("stremio-message", error.message, true);
  }
}

async function handleAIOStreamsSave(data, form) {
  setMessage("aiostreams-message", "");
  const apiBaseUrl = (data.get("api_base_url") || "").trim();
  const username = (data.get("username") || "").trim();
  const auth = (data.get("auth") || "").trim();
  if (!apiBaseUrl || !auth) {
    setMessage("aiostreams-message", "Enter base URL and auth.", true);
    return;
  }
  const payload = { api_base_url: apiBaseUrl, auth };
  if (username) {
    payload.username = username;
  }
  try {
    await requestJSON("/api/integrations/aiostreams", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const input = form ? form.querySelector("input[name='auth']") : null;
    if (input) {
      input.value = "";
    }
    setMessage("aiostreams-message", "Saved.");
    await loadIntegrations();
  } catch (error) {
    setMessage("aiostreams-message", error.message, true);
  }
}

async function handleAIOStreamsTest() {
  setMessage("aiostreams-message", "");
  try {
    await requestJSON("/api/integrations/aiostreams/test", { method: "POST" });
    setMessage("aiostreams-message", "Connection test succeeded.");
    await loadIntegrations();
  } catch (error) {
    setMessage("aiostreams-message", error.message, true);
  }
}

async function handleAIOStreamsDisconnect() {
  setMessage("aiostreams-message", "");
  try {
    await requestJSON("/api/integrations/aiostreams/disconnect", { method: "POST" });
    setMessage("aiostreams-message", "Disconnected.");
    await loadIntegrations();
  } catch (error) {
    setMessage("aiostreams-message", error.message, true);
  }
}

async function handleQuickImportScheduleSave(data) {
  setMessage("quick-import-message", "");
  const rawValue = data.get("quick_import_interval");
  const intervalSeconds = parseIntervalSeconds(rawValue);
  if (intervalSeconds === null && rawValue !== "" && rawValue !== null) {
    setMessage("quick-import-message", "Choose a valid interval.", true);
    return;
  }
  try {
    await requestJSON("/api/integrations/import/quick/schedule", {
      method: "POST",
      body: JSON.stringify({ interval_seconds: intervalSeconds }),
    });
    setMessage("quick-import-message", "Schedule saved.");
    await loadStatusData();
  } catch (error) {
    setMessage("quick-import-message", error.message, true);
  }
}

async function handleQuickImportNow(options = {}) {
  const opts = options instanceof Event ? {} : options;
  const messageId = opts.messageId || "quick-import-message";
  setMessage(messageId, "Requesting import...");
  try {
    const response = await requestJSON("/api/integrations/import/quick", {
      method: "POST",
    });
    const providers = response && response.providers ? response.providers : [];
    const label = providers.length
      ? `Quick import queued: ${providers.join(", ")}.`
      : "Quick import requested.";
    setMessage(messageId, label);
    await loadStatusData();
  } catch (error) {
    setMessage(messageId, error.message, true);
  }
}

async function handleImportAll(options = {}) {
  const opts = options instanceof Event ? {} : options;
  const buttonId = opts.buttonId || "import-all-button";
  const messageId = opts.messageId || "import-all-message";
  const confirmMessage =
    opts.confirmMessage ||
    "Start import all? This can take a while and will re-sync your full history.";
  if (opts.requireConfirm !== false) {
    const confirmed = window.confirm(confirmMessage);
    if (!confirmed) {
      return;
    }
  }
  const button = document.getElementById(buttonId);
  if (button) {
    button.disabled = true;
  }
  setMessage(messageId, "Requesting import...");
  try {
    const response = await requestJSON("/api/integrations/import/all", {
      method: "POST",
    });
    const providers = response && response.providers ? response.providers : [];
    const label = providers.length
      ? `Import queued: ${providers.join(", ")}.`
      : "Import requested.";
    setMessage(messageId, label);
    await loadStatusData();
  } catch (error) {
    setMessage(messageId, error.message, true);
  } finally {
    if (button) {
      button.disabled = false;
    }
  }
}

async function loadSettings() {
  const form = document.getElementById("settings-form");
  if (!form) {
    return;
  }
  const data = await requestJSON("/api/settings");
  const includeAdultInput = form.querySelector(
    "input[name='include_adult_in_search']"
  );
  if (includeAdultInput) {
    includeAdultInput.checked = !!data.include_adult_in_search;
  }
}

async function handleSettingsSave(data) {
  setMessage("settings-message", "");
  const includeAdult = data.get("include_adult_in_search") === "on";
  const payload = {
    include_adult_in_search: includeAdult,
  };
  try {
    const updated = await requestJSON("/api/settings", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setMessage("settings-message", "Saved.");
    if (updated) {
      await loadSettings();
    }
  } catch (error) {
    setMessage("settings-message", error.message, true);
  }
}

async function loadMetadataProviders() {
  const tmdbForm = document.getElementById("tmdb-form");
  const tvdbForm = document.getElementById("tvdb-form");
  const tvmazeForm = document.getElementById("tvmaze-form");
  const imdbForm = document.getElementById("imdb-form");
  const kitsuForm = document.getElementById("kitsu-form");
  const myanimelistForm = document.getElementById("myanimelist-form");
  const anilistProviderForm = document.getElementById("anilist-provider-form");
  if (
    !tmdbForm &&
    !tvdbForm &&
    !kitsuForm &&
    !tvmazeForm &&
    !imdbForm &&
    !myanimelistForm &&
    !anilistProviderForm
  ) {
    return;
  }
  const data = await requestJSON("/api/metadata/providers");
  const providers = data && data.providers ? data.providers : [];
  const tmdb = providers.find((item) => item.provider === "tmdb") || {
    enabled: false,
    config: {},
  };
  const tvdb = providers.find((item) => item.provider === "tvdb") || {
    enabled: false,
    config: {},
  };
  const tvmaze = providers.find((item) => item.provider === "tvmaze") || {
    enabled: false,
    config: {},
  };
  const imdb = providers.find((item) => item.provider === "imdb") || {
    enabled: false,
    config: {},
  };
  const kitsu = providers.find((item) => item.provider === "kitsu") || {
    enabled: false,
    config: {},
  };
  const myanimelist = providers.find((item) => item.provider === "myanimelist") || {
    enabled: false,
    config: {},
  };
  const anilist = providers.find((item) => item.provider === "anilist") || {
    enabled: false,
    config: {},
  };

  if (tmdbForm) {
    const enabledInput = tmdbForm.querySelector("input[name='enabled']");
    if (enabledInput) {
      enabledInput.checked = !!tmdb.enabled;
    }
    const languageInput = tmdbForm.querySelector("input[name='language']");
    const regionInput = tmdbForm.querySelector("input[name='region']");
    if (languageInput) {
      languageInput.value =
        tmdb.config && tmdb.config.language ? tmdb.config.language : "";
    }
    if (regionInput) {
      regionInput.value = tmdb.config && tmdb.config.region ? tmdb.config.region : "";
    }
    if (tmdb.has_credentials) {
      setMessage("tmdb-message", "API key is stored securely.");
    } else {
      setMessage("tmdb-message", "");
    }
  }

  if (tvdbForm) {
    const enabledInput = tvdbForm.querySelector("input[name='enabled']");
    if (enabledInput) {
      enabledInput.checked = !!tvdb.enabled;
    }
    const languageInput = tvdbForm.querySelector("input[name='language']");
    if (languageInput) {
      languageInput.value =
        tvdb.config && tvdb.config.language ? tvdb.config.language : "";
    }
    if (tvdb.has_credentials) {
      setMessage("tvdb-message", "Credentials are stored securely.");
    } else {
      setMessage("tvdb-message", "");
    }
  }

  if (tvmazeForm) {
    const enabledInput = tvmazeForm.querySelector("input[name='enabled']");
    if (enabledInput) {
      enabledInput.checked = !!tvmaze.enabled;
    }
    if (tvmaze.enabled) {
      setMessage("tvmaze-message", "No API key required.");
    } else {
      setMessage("tvmaze-message", "");
    }
  }

  if (imdbForm) {
    const enabledInput = imdbForm.querySelector("input[name='enabled']");
    if (enabledInput) {
      enabledInput.checked = !!imdb.enabled;
    }
    if (imdb.enabled) {
      setMessage("imdb-message", "No API key required.");
    } else {
      setMessage("imdb-message", "");
    }
  }

  if (kitsuForm) {
    const enabledInput = kitsuForm.querySelector("input[name='enabled']");
    if (enabledInput) {
      enabledInput.checked = !!kitsu.enabled;
    }
    const languageInput = kitsuForm.querySelector("input[name='language']");
    if (languageInput) {
      languageInput.value =
        kitsu.config && kitsu.config.language ? kitsu.config.language : "";
    }
    if (kitsu.enabled) {
      setMessage("kitsu-message", "No API key required.");
    } else {
      setMessage("kitsu-message", "");
    }
  }

  if (myanimelistForm) {
    const enabledInput = myanimelistForm.querySelector("input[name='enabled']");
    if (enabledInput) {
      enabledInput.checked = !!myanimelist.enabled;
    }
    if (myanimelist.enabled) {
      setMessage("myanimelist-message", "No API key required.");
    } else {
      setMessage("myanimelist-message", "");
    }
  }

  if (anilistProviderForm) {
    const enabledInput = anilistProviderForm.querySelector("input[name='enabled']");
    if (enabledInput) {
      enabledInput.checked = !!anilist.enabled;
    }
    if (anilist.enabled) {
      setMessage("anilist-provider-message", "No API key required.");
    } else {
      setMessage("anilist-provider-message", "");
    }
  }
}

async function handleTmdbSave(data, form) {
  setMessage("tmdb-message", "");
  const enabled = data.get("enabled") === "on";
  const apiKeyRaw = data.get("api_key");
  const language = (data.get("language") || "").trim();
  const region = (data.get("region") || "").trim();
  const payload = {
    enabled,
    language: language || null,
    region: region || null,
  };
  if (apiKeyRaw !== null && apiKeyRaw !== undefined) {
    const apiKey = apiKeyRaw.trim();
    if (apiKey) {
      payload.api_key = apiKey;
    }
  }
  try {
    await requestJSON("/api/metadata/providers/tmdb", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const apiKeyInput = form.querySelector("input[name='api_key']");
    if (apiKeyInput) {
      apiKeyInput.value = "";
    }
    setMessage("tmdb-message", "Saved.");
    await loadMetadataProviders();
  } catch (error) {
    setMessage("tmdb-message", error.message, true);
  }
}

async function handleTvdbSave(data, form) {
  setMessage("tvdb-message", "");
  const enabled = data.get("enabled") === "on";
  const apiKeyRaw = data.get("api_key");
  const pinRaw = data.get("pin");
  const language = (data.get("language") || "").trim();
  const payload = {
    enabled,
    language: language || null,
  };
  if (apiKeyRaw !== null && apiKeyRaw !== undefined) {
    const apiKey = apiKeyRaw.trim();
    if (apiKey) {
      payload.api_key = apiKey;
    }
  }
  if (pinRaw !== null && pinRaw !== undefined) {
    const pin = pinRaw.trim();
    if (pin) {
      payload.pin = pin;
    }
  }
  try {
    await requestJSON("/api/metadata/providers/tvdb", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const apiKeyInput = form.querySelector("input[name='api_key']");
    if (apiKeyInput) {
      apiKeyInput.value = "";
    }
    const pinInput = form.querySelector("input[name='pin']");
    if (pinInput) {
      pinInput.value = "";
    }
    setMessage("tvdb-message", "Saved.");
    await loadMetadataProviders();
  } catch (error) {
    setMessage("tvdb-message", error.message, true);
  }
}

async function handleKitsuSave(data) {
  setMessage("kitsu-message", "");
  const enabled = data.get("enabled") === "on";
  const language = (data.get("language") || "").trim();
  const payload = {
    enabled,
    language: language || null,
  };
  try {
    await requestJSON("/api/metadata/providers/kitsu", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setMessage("kitsu-message", "Saved.");
    await loadMetadataProviders();
  } catch (error) {
    setMessage("kitsu-message", error.message, true);
  }
}

async function handleTvmazeSave(data) {
  setMessage("tvmaze-message", "");
  const enabled = data.get("enabled") === "on";
  const payload = { enabled };
  try {
    await requestJSON("/api/metadata/providers/tvmaze", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setMessage("tvmaze-message", "Saved.");
    await loadMetadataProviders();
  } catch (error) {
    setMessage("tvmaze-message", error.message, true);
  }
}

async function handleImdbSave(data) {
  setMessage("imdb-message", "");
  const enabled = data.get("enabled") === "on";
  const payload = { enabled };
  try {
    await requestJSON("/api/metadata/providers/imdb", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setMessage("imdb-message", "Saved.");
    await loadMetadataProviders();
  } catch (error) {
    setMessage("imdb-message", error.message, true);
  }
}

async function handleMyAnimeListSave(data) {
  setMessage("myanimelist-message", "");
  const enabled = data.get("enabled") === "on";
  const payload = { enabled };
  try {
    await requestJSON("/api/metadata/providers/myanimelist", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setMessage("myanimelist-message", "Saved.");
    await loadMetadataProviders();
  } catch (error) {
    setMessage("myanimelist-message", error.message, true);
  }
}

async function handleAniListProviderSave(data) {
  setMessage("anilist-provider-message", "");
  const enabled = data.get("enabled") === "on";
  const payload = { enabled };
  try {
    await requestJSON("/api/metadata/providers/anilist", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setMessage("anilist-provider-message", "Saved.");
    await loadMetadataProviders();
  } catch (error) {
    setMessage("anilist-provider-message", error.message, true);
  }
}

async function handleImportHistoryReset(data) {
  setMessage("import-history-reset-message", "");
  const provider = data.get("provider");
  if (!provider) {
    setMessage("import-history-reset-message", "Choose a provider.", true);
    return;
  }
  const include_blacklisted = data.get("include_blacklisted") === "on";
  const payload = { provider, include_blacklisted };
  try {
    const result = await requestJSON("/api/history/import-history/clear", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const deleted = result.deleted ?? 0;
    setMessage(
      "import-history-reset-message",
      `Cleared ${deleted} import events for ${provider}.`
    );
  } catch (error) {
    setMessage("import-history-reset-message", error.message, true);
  }
}

function bindHistoryClear() {
  const openButton = document.getElementById("history-clear-open");
  const modal = document.getElementById("history-clear-modal");
  if (!openButton || !modal) {
    return;
  }
  const form = document.getElementById("history-clear-form");
  const input = document.getElementById("history-clear-input");
  const confirmButton = document.getElementById("history-clear-confirm");
  const modalMessageId = "history-clear-modal-message";
  const statusMessageId = "history-clear-message";

  const updateConfirmState = () => {
    if (!input || !confirmButton) {
      return false;
    }
    const value = input.value.trim().toLowerCase();
    const isValid = value === "delete";
    confirmButton.disabled = !isValid;
    return isValid;
  };

  const closeModal = () => {
    modal.setAttribute("hidden", "");
    if (form) {
      form.reset();
    }
    if (confirmButton) {
      confirmButton.disabled = true;
    }
    setMessage(modalMessageId, "");
  };

  openButton.addEventListener("click", () => {
    modal.removeAttribute("hidden");
    setMessage(statusMessageId, "");
    setMessage(modalMessageId, "");
    if (confirmButton) {
      confirmButton.disabled = true;
    }
    if (input) {
      input.value = "";
      input.focus();
    }
  });

  modal.querySelectorAll("[data-modal-close]").forEach((button) => {
    button.addEventListener("click", closeModal);
  });

  if (input) {
    input.addEventListener("input", () => {
      updateConfirmState();
    });
  }

  if (form) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const isValid = updateConfirmState();
      if (!isValid) {
        setMessage(modalMessageId, "Type delete to confirm.", true);
        return;
      }
      if (confirmButton) {
        confirmButton.disabled = true;
      }
      setMessage(modalMessageId, "Clearing history...");
      try {
        await requestJSON("/api/history/items", { method: "DELETE" });
        setMessage(statusMessageId, "History cleared.");
        if (typeof window.librarysyncLoadHistory === "function") {
          await window.librarysyncLoadHistory();
        }
        closeModal();
      } catch (error) {
        setMessage(modalMessageId, error.message, true);
        if (confirmButton) {
          confirmButton.disabled = false;
        }
      }
    });
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !modal.hasAttribute("hidden")) {
      closeModal();
    }
  });
}

// Activity Logic

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
  "cookie",
  "cookies",
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

function bindActivityFilters() {
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

// Maintenance Functions

function renderStatCard(label, value, title, options = {}) {
  const card = document.createElement("div");
  card.className = "activity-stat";
  const labelEl = document.createElement("span");
  labelEl.textContent = label;
  if (options.linkHref && options.linkLabel) {
    labelEl.appendChild(document.createTextNode(" · "));
    const link = document.createElement("a");
    link.href = options.linkHref;
    link.className = "link-muted";
    link.textContent = options.linkLabel;
    if (options.linkTitle) {
      link.title = options.linkTitle;
    }
    labelEl.appendChild(link);
  }
  const valueEl = document.createElement("strong");
  valueEl.textContent = value;
  if (title) {
    valueEl.title = title;
  }
  card.appendChild(labelEl);
  card.appendChild(valueEl);
  return card;
}

function renderMaintenanceSummary(statusData) {
  const container = document.getElementById("maintenance-summary");
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
  const imports = statusData.imports || {};
  const quickImport = imports.quick || null;
  const importAll = imports.import_all || null;
  const queueOrder = Array.isArray(imports.queue_order) ? imports.queue_order : [];
  const nextImport = quickImport ? quickImport.next_run_at : null;
  const isActive = (state) =>
    state && (state.status === "pending" || state.status === "in_progress");
  let activeQueue = [];
  if (isActive(importAll) && Array.isArray(importAll.queue) && importAll.queue.length) {
    activeQueue = importAll.queue;
  } else if (
    isActive(quickImport) &&
    Array.isArray(quickImport.queue) &&
    quickImport.queue.length
  ) {
    activeQueue = quickImport.queue;
  } else if (queueOrder.length) {
    activeQueue = queueOrder;
  }
  const queue = activeQueue.length
    ? activeQueue.map((entry) => formatProvider(entry)).join(" → ")
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
      label: "Import queue order",
      value: queue,
      link: {
        linkHref: "#imports", // Intra-page link to imports tab? Or just remove link if same page.
        // Since we are on settings page, clicking this should switch tab.
        // We can handle this or just leave it text.
        // Let's make it switch tab if possible, or just remove link for now to simplify.
        // Actually, let's keep it simple.
        // linkLabel: "Change",
      },
    },
    {
      label: "Metadata pending",
      value: String(metadataCounts.pending || 0),
    },
  ];

  stats.forEach((stat) => {
    container.appendChild(
      renderStatCard(stat.label, stat.value, stat.title, stat.link)
    );
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

async function requestMaintenanceQuickImportNow(button) {
  if (button) {
    button.disabled = true;
  }
  setMessage("maintenance-import-message", "Requesting import...");
  try {
    const response = await requestJSON("/api/integrations/import/quick", {
      method: "POST",
    });
    const providers = response && response.providers ? response.providers : [];
    const label = providers.length
      ? `Quick import queued: ${providers.join(", ")}.`
      : "Quick import requested.";
    setMessage("maintenance-import-message", label);
    await loadStatusData();
  } catch (error) {
    setMessage("maintenance-import-message", error.message, true);
  } finally {
    if (button) {
      button.disabled = false;
    }
  }
}

async function requestMaintenanceImportAllNow(button) {
  const confirmed = window.confirm(
    "Start import all? This can take a while and will re-sync your full history."
  );
  if (!confirmed) {
    return;
  }
  if (button) {
    button.disabled = true;
  }
  setMessage("maintenance-import-message", "Requesting import...");
  try {
    const response = await requestJSON("/api/integrations/import/all", {
      method: "POST",
    });
    const providers = response && response.providers ? response.providers : [];
    const label = providers.length
      ? `Import queued: ${providers.join(", ")}.`
      : "Import requested.";
    setMessage("maintenance-import-message", label);
    await loadStatusData();
  } catch (error) {
    setMessage("maintenance-import-message", error.message, true);
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
  runButton.addEventListener("click", () => requestMaintenanceQuickImportNow(runButton));
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
  runButton.addEventListener("click", () => requestMaintenanceImportAllNow(runButton));
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

function renderMaintenanceSchedule(statusData) {
  const container = document.getElementById("maintenance-schedule");
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

function bindMaintenanceControls() {
  const refreshButton = document.getElementById("maintenance-refresh");
  if (refreshButton) {
    refreshButton.addEventListener("click", () => loadStatusData());
  }
}

function startMaintenanceAutoRefresh() {
  if (settingsState.timer) {
    return;
  }
  settingsState.timer = window.setInterval(() => {
    loadStatusData();
  }, 30000);
}

// Blacklist Logic

const blacklistState = {
  lookupId: null,
  timer: null,
  candidates: [],
  entries: [],
};

function clearBlacklistLookupTimer() {
  if (blacklistState.timer) {
    window.clearTimeout(blacklistState.timer);
    blacklistState.timer = null;
  }
}

function resetBlacklistLookupUI() {
  const resultsCard = document.getElementById("blacklist-results");
  const candidatesEl = document.getElementById("blacklist-candidates");
  if (resultsCard) {
    resultsCard.hidden = true;
  }
  if (candidatesEl) {
    candidatesEl.innerHTML = "";
  }
  blacklistState.lookupId = null;
  blacklistState.candidates = [];
  setMessage("blacklist-lookup-message", "");
}

async function handleBlacklistLookupSubmit(data) {
  resetBlacklistLookupUI();
  clearBlacklistLookupTimer();
  const query = (data.get("query") || "").trim();
  if (!query) {
    setMessage("blacklist-lookup-message", "Enter a TV show name or ID to search.", true);
    return;
  }
  try {
    setMessage("blacklist-lookup-message", "Searching...");
    const response = await requestJSON("/api/metadata/lookup", {
      method: "POST",
      body: JSON.stringify({ query, search_scope: "tv" }),
    });
    blacklistState.lookupId = response.lookup_id;
    await pollBlacklistLookupStatus(response.lookup_id);
  } catch (error) {
    setMessage("blacklist-lookup-message", error.message, true);
  }
}

async function pollBlacklistLookupStatus(lookupId) {
  try {
    const data = await requestJSON(`/api/metadata/lookup/${lookupId}`);
    if (data.status === "completed") {
      renderBlacklistCandidates(data.candidates || []);
      return;
    }
    if (data.status === "failed") {
      setMessage("blacklist-lookup-message", data.error || "Lookup failed.", true);
      return;
    }
    blacklistState.timer = window.setTimeout(() => pollBlacklistLookupStatus(lookupId), 1500);
  } catch (error) {
    setMessage("blacklist-lookup-message", error.message, true);
  }
}

function normalizeLookupId(value) {
  if (value === null || value === undefined) {
    return "";
  }
  return String(value).trim();
}

function normalizeImdbId(value) {
  return normalizeLookupId(value).toLowerCase();
}

function candidateMatchesBlacklist(candidate, entry) {
  if (!candidate || !entry) {
    return false;
  }
  if (
    candidate.provider &&
    candidate.provider_item_id &&
    candidate.provider === entry.provider &&
    candidate.provider_item_id === entry.provider_item_id
  ) {
    return true;
  }
  const pairs = [
    ["imdb_id", normalizeImdbId],
    ["tmdb_id", normalizeLookupId],
    ["tvdb_id", normalizeLookupId],
    ["tvmaze_id", normalizeLookupId],
  ];
  return pairs.some(([key, normalizer]) => {
    const candidateValue = normalizer(candidate[key]);
    const entryValue = normalizer(entry[key]);
    return Boolean(candidateValue && entryValue && candidateValue === entryValue);
  });
}

function renderBlacklistCandidates(candidates) {
  const resultsCard = document.getElementById("blacklist-results");
  const candidatesEl = document.getElementById("blacklist-candidates");
  if (!resultsCard || !candidatesEl) {
    return;
  }
  const tvCandidates = (candidates || []).filter((candidate) => candidate.media_type === "tv");
  blacklistState.candidates = tvCandidates;
  candidatesEl.innerHTML = "";
  if (!tvCandidates.length) {
    candidatesEl.textContent = "No TV matches found.";
  } else {
    tvCandidates.forEach((candidate) => {
      const row = document.createElement("div");
      row.className = "candidate-option";

      const action = document.createElement("button");
      action.type = "button";
      action.className = "btn btn-secondary btn-sm";
      action.dataset.blacklistAdd = candidate.id;
      const alreadyAdded = blacklistState.entries.some((entry) =>
        candidateMatchesBlacklist(candidate, entry),
      );
      action.textContent = alreadyAdded ? "Blacklisted" : "Add";
      action.disabled = alreadyAdded;

      const poster = document.createElement("img");
      poster.className = "candidate-poster";
      if (candidate.poster_url) {
        poster.src = candidate.poster_url;
        poster.alt = `${candidate.title} poster`;
        poster.loading = "lazy";
      } else {
        poster.alt = "";
      }

      const meta = document.createElement("div");
      meta.className = "candidate-meta";
      const title = document.createElement("h3");
      title.textContent = candidate.title || "Unknown title";
      const detail = document.createElement("p");
      const year = candidate.year ? candidate.year : "Year unknown";
      const providerLabel = formatProviderLabel(candidate.provider) || "Provider";
      detail.textContent = `${year} · ${providerLabel}`;
      meta.appendChild(title);
      meta.appendChild(detail);

      row.appendChild(action);
      row.appendChild(poster);
      row.appendChild(meta);
      candidatesEl.appendChild(row);
    });
  }
  resultsCard.hidden = false;
  setMessage("blacklist-lookup-message", "");
}

function getBlacklistCandidate(candidateId) {
  return blacklistState.candidates.find((entry) => entry.id === candidateId) || null;
}

function buildBlacklistPayload(candidate) {
  return {
    provider: candidate.provider,
    provider_item_id: candidate.provider_item_id,
    media_type: "tv",
    title: candidate.title,
    year: candidate.year || null,
    poster_url: candidate.poster_url || null,
    imdb_id: candidate.imdb_id || null,
    tmdb_id: candidate.tmdb_id || null,
    tvdb_id: candidate.tvdb_id || null,
    tvmaze_id: candidate.tvmaze_id || null,
  };
}

async function handleBlacklistAdd(candidateId) {
  setMessage("blacklist-message", "");
  const candidate = getBlacklistCandidate(candidateId);
  if (!candidate) {
    setMessage("blacklist-message", "Select a show to blacklist.", true);
    return;
  }
  if (candidate.media_type !== "tv") {
    setMessage("blacklist-message", "Only TV shows can be blacklisted right now.", true);
    return;
  }
  try {
    await requestJSON("/api/blacklist", {
      method: "POST",
      body: JSON.stringify(buildBlacklistPayload(candidate)),
    });
    setMessage("blacklist-message", `${candidate.title} added to the blacklist.`);
    await loadBlacklist();
    renderBlacklistCandidates(blacklistState.candidates);
  } catch (error) {
    setMessage("blacklist-message", error.message, true);
  }
}

function formatBlacklistIds(entry) {
  const ids = [];
  if (entry.imdb_id) {
    ids.push(`IMDb ${entry.imdb_id}`);
  }
  if (entry.tmdb_id) {
    ids.push(`TMDB ${entry.tmdb_id}`);
  }
  if (entry.tvdb_id) {
    ids.push(`TVDB ${entry.tvdb_id}`);
  }
  if (entry.tvmaze_id) {
    ids.push(`TVMaze ${entry.tvmaze_id}`);
  }
  return ids.join(" · ");
}

function renderBlacklistEntries() {
  const container = document.getElementById("blacklist-entries");
  if (!container) {
    return;
  }
  container.innerHTML = "";
  if (!blacklistState.entries.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No shows are blacklisted yet.";
    container.appendChild(empty);
    return;
  }
  blacklistState.entries.forEach((entry) => {
    const row = document.createElement("div");
    row.className = "candidate-option";

    const action = document.createElement("button");
    action.type = "button";
    action.className = "btn btn-ghost btn-sm";
    action.dataset.blacklistRemove = entry.id;
    action.textContent = "Remove";

    const poster = document.createElement("img");
    poster.className = "candidate-poster";
    if (entry.poster_url) {
      poster.src = entry.poster_url;
      poster.alt = `${entry.title} poster`;
      poster.loading = "lazy";
    } else {
      poster.alt = "";
    }

    const meta = document.createElement("div");
    meta.className = "candidate-meta";
    const title = document.createElement("h3");
    const yearLabel = entry.year ? ` (${entry.year})` : "";
    title.textContent = `${entry.title}${yearLabel}`;
    const detail = document.createElement("p");
    const addedLabel = entry.created_at ? ` · Added ${formatMetadataDate(entry.created_at)}` : "";
    const providerLabel = formatProviderLabel(entry.provider) || "Provider";
    detail.textContent = `${providerLabel}${addedLabel}`;
    const ids = document.createElement("p");
    const idText = formatBlacklistIds(entry);
    ids.textContent = idText || "IDs pending from lookup";
    meta.appendChild(title);
    meta.appendChild(detail);
    meta.appendChild(ids);

    row.appendChild(action);
    row.appendChild(poster);
    row.appendChild(meta);
    container.appendChild(row);
  });
}

async function loadBlacklist() {
  const container = document.getElementById("blacklist-entries");
  if (!container) {
    return;
  }
  container.textContent = "Loading...";
  try {
    const data = await requestJSON("/api/blacklist");
    blacklistState.entries = data && Array.isArray(data.items) ? data.items : [];
    renderBlacklistEntries();
    if (blacklistState.candidates.length) {
      renderBlacklistCandidates(blacklistState.candidates);
    }
  } catch (error) {
    container.innerHTML = "";
    setMessage("blacklist-message", error.message, true);
  }
}

async function handleBlacklistRemove(blacklistId) {
  setMessage("blacklist-message", "");
  try {
    await requestJSON(`/api/blacklist/${blacklistId}`, { method: "DELETE" });
    setMessage("blacklist-message", "Blacklist entry removed.");
    await loadBlacklist();
    renderBlacklistCandidates(blacklistState.candidates);
  } catch (error) {
    setMessage("blacklist-message", error.message, true);
  }
}

async function initBlacklist() {
  bindForm("blacklist-lookup-form", handleBlacklistLookupSubmit);

  const blacklistCandidates = document.getElementById("blacklist-candidates");
  if (blacklistCandidates) {
    blacklistCandidates.addEventListener("click", (event) => {
      const button = event.target.closest("[data-blacklist-add]");
      if (!button) {
        return;
      }
      const candidateId = button.dataset.blacklistAdd;
      if (candidateId) {
        handleBlacklistAdd(candidateId);
      }
    });
  }

  const blacklistEntries = document.getElementById("blacklist-entries");
  if (blacklistEntries) {
    blacklistEntries.addEventListener("click", (event) => {
      const button = event.target.closest("[data-blacklist-remove]");
      if (!button) {
        return;
      }
      const blacklistId = button.dataset.blacklistRemove;
      if (blacklistId) {
        handleBlacklistRemove(blacklistId);
      }
    });
  }

  await loadBlacklist();
}

window.librarysyncPageInit = async ({ user }) => {
  if (!user) {
    return;
  }
  bindForm("letterboxd-form", handleLetterboxdSave);
  bindForm("watchlist-source-form", handleWatchlistSourceAdd);
  bindForm("quick-import-form", handleQuickImportScheduleSave);
  bindForm("stremio-form", handleStremioConnect);
  bindForm("aiostreams-form", handleAIOStreamsSave);
  bindForm("settings-form", handleSettingsSave);
  bindForm("tmdb-form", handleTmdbSave);
  bindForm("tvdb-form", handleTvdbSave);
  bindForm("tvmaze-form", handleTvmazeSave);
  bindForm("imdb-form", handleImdbSave);
  bindForm("kitsu-form", handleKitsuSave);
  bindForm("myanimelist-form", handleMyAnimeListSave);
  bindForm("anilist-provider-form", handleAniListProviderSave);
  bindForm("import-history-reset-form", handleImportHistoryReset);
  bindHistoryClear();
  bindWatchlistSourceActions();
  bindMaintenanceControls();
  bindActivityFilters();

  const letterboxdTest = document.getElementById("letterboxd-test");
  if (letterboxdTest) {
    letterboxdTest.addEventListener("click", handleLetterboxdTest);
  }
  const importAllButton = document.getElementById("import-all-button");
  if (importAllButton) {
    importAllButton.addEventListener("click", handleImportAll);
  }
  const importQueueRestore = document.getElementById("import-queue-restore");
  if (importQueueRestore) {
    importQueueRestore.addEventListener("click", handleImportQueueRestore);
  }
  const quickImportNow = document.getElementById("quick-import-now");
  if (quickImportNow) {
    quickImportNow.addEventListener("click", handleQuickImportNow);
  }
  const letterboxdParse = document.getElementById("letterboxd-parse");
  if (letterboxdParse) {
    letterboxdParse.addEventListener("click", handleLetterboxdParse);
  }
  const letterboxdDisconnect = document.getElementById("letterboxd-disconnect");
  if (letterboxdDisconnect) {
    letterboxdDisconnect.addEventListener("click", handleLetterboxdDisconnect);
  }
  const traktConnect = document.getElementById("trakt-connect");
  if (traktConnect) {
    traktConnect.addEventListener("click", handleTraktConnect);
  }
  const traktDisconnect = document.getElementById("trakt-disconnect");
  if (traktDisconnect) {
    traktDisconnect.addEventListener("click", handleTraktDisconnect);
  }
  const simklConnect = document.getElementById("simkl-connect");
  if (simklConnect) {
    simklConnect.addEventListener("click", handleSimklConnect);
  }
  const simklDisconnect = document.getElementById("simkl-disconnect");
  if (simklDisconnect) {
    simklDisconnect.addEventListener("click", handleSimklDisconnect);
  }
  const anilistConnect = document.getElementById("anilist-connect");
  if (anilistConnect) {
    anilistConnect.addEventListener("click", handleAniListConnect);
  }
  const anilistDisconnect = document.getElementById("anilist-disconnect");
  if (anilistDisconnect) {
    anilistDisconnect.addEventListener("click", handleAniListDisconnect);
  }
  const stremioDisconnect = document.getElementById("stremio-disconnect");
  if (stremioDisconnect) {
    stremioDisconnect.addEventListener("click", handleStremioDisconnect);
  }
  const aiostreamsTest = document.getElementById("aiostreams-test");
  if (aiostreamsTest) {
    aiostreamsTest.addEventListener("click", handleAIOStreamsTest);
  }
  const aiostreamsDisconnect = document.getElementById("aiostreams-disconnect");
  if (aiostreamsDisconnect) {
    aiostreamsDisconnect.addEventListener("click", handleAIOStreamsDisconnect);
  }

  try {
    await Promise.all([
      loadIntegrations(),
      loadSettings(),
      loadMetadataProviders(),
      loadStatusData(),
      initBlacklist(),
    ]);
    startMaintenanceAutoRefresh();
  } catch (error) {
    console.error("settings load failed", error);
  }
};
