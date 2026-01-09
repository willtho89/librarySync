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
  const form = document.getElementById("quick-import-form");
  if (!form) {
    return;
  }
  try {
    const statusData = await requestJSON("/api/status");
    settingsState.status = statusData;
    applyQuickImportControls(statusData);
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
    ]);
  } catch (error) {
    console.error("settings load failed", error);
  }
};
