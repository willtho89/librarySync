const addonState = {
  config: null,
  catalogs: [],
  customCatalogs: [],
  selectedCatalogId: null,
  items: [],
  candidates: [],
  lookupId: null,
  lookupTimer: null,
  installLinks: {
    manifestUrl: "",
    installUrl: "",
  },
};

const BUILTIN_CATALOG_DETAILS = {
  watchlist_movies: {
    title: "Watchlist Movies",
    description: "Movies queued in your watchlist.",
  },
  watchlist_shows: {
    title: "Watchlist Shows",
    description: "TV and anime watchlist entries.",
  },
  watchlist_anime: {
    title: "Watchlist Anime",
    description: "Anime-only watchlist slice.",
  },
  in_progress_shows: {
    title: "In Progress",
    description: "Shows with released episodes left to watch.",
  },
};

const STATUS_OPTIONS = [
  { value: "added", label: "Added" },
  { value: "in_progress", label: "In progress" },
  { value: "not_released", label: "Not released" },
  { value: "watched", label: "Watched" },
];

const ORDER_OPTIONS = [
  { value: "date_added", label: "Date added" },
  { value: "release_date", label: "Release date" },
  { value: "last_watched", label: "Last watched" },
  { value: "episodes_left", label: "Episodes left" },
  { value: "progress", label: "Progress" },
  { value: "last_episode_air_date", label: "Last episode air date" },
  { value: "next_episode_air_date", label: "Next episode air date" },
  { value: "random", label: "Random" },
];

const CUSTOM_ORDER_OPTIONS = [
  { value: "manual", label: "Manual" },
  ...ORDER_OPTIONS,
];

function setAddonMessage(message, isError = false) {
  setMessage("stremio-addon-message", message, isError);
}

function setControlsMessage(message, isError = false) {
  setMessage("stremio-addon-controls-message", message, isError);
}

function updateInstallLinks(payload) {
  if (!payload) {
    return;
  }
  if (payload.manifest_url) {
    addonState.installLinks.manifestUrl = payload.manifest_url;
  }
  if (payload.install_url) {
    addonState.installLinks.installUrl = payload.install_url;
  }
}

function renderInstallSection() {
  const manifestInput = document.getElementById("stremio-manifest-url");
  const installLink = document.getElementById("stremio-install-link");
  const manifestCopy = document.getElementById("stremio-manifest-copy");
  const installCopy = document.getElementById("stremio-install-copy");
  const hint = document.getElementById("stremio-install-hint");

  const manifestUrl = addonState.installLinks.manifestUrl || "";
  const installUrl = addonState.installLinks.installUrl || "";
  if (manifestInput) {
    manifestInput.value = manifestUrl;
  }
  if (manifestCopy) {
    manifestCopy.disabled = !manifestUrl;
  }
  if (installLink) {
    installLink.href = installUrl || "#";
    installLink.dataset.disabled = installUrl ? "false" : "true";
  }
  if (installCopy) {
    installCopy.disabled = !installUrl;
  }
  if (hint) {
    hint.textContent = manifestUrl
      ? "Keep this URL somewhere safe for reinstalling."
      : "Save your settings to generate the install link.";
  }
}

function renderControlSection() {
  const enabledToggle = document.getElementById("stremio-addon-enabled");
  if (enabledToggle) {
    enabledToggle.checked = !!(addonState.config && addonState.config.is_enabled);
  }
}

function buildSelect(options, selectedValue) {
  const select = document.createElement("select");
  select.className = "select";
  options.forEach((option) => {
    const opt = document.createElement("option");
    opt.value = option.value;
    opt.textContent = option.label;
    if (option.value === selectedValue) {
      opt.selected = true;
    }
    select.appendChild(opt);
  });
  return select;
}

function normalizeStatuses(catalog) {
  const filters = catalog && catalog.filters ? catalog.filters : {};
  const statuses = Array.isArray(filters.statuses) ? filters.statuses : null;
  if (Array.isArray(statuses)) {
    return statuses;
  }
  return ["added", "in_progress", "not_released"];
}

function normalizeOrdering(catalog) {
  const ordering = catalog && catalog.ordering ? catalog.ordering : {};
  const orderBy = ordering.order_by || "date_added";
  const orderDir = ordering.order_dir || "desc";
  return { orderBy, orderDir };
}

function renderBuiltInCatalogs() {
  const container = document.getElementById("stremio-addon-catalogs");
  if (!container) {
    return;
  }
  container.innerHTML = "";
  if (!addonState.catalogs.length) {
    container.textContent = "No catalogs configured yet.";
    return;
  }

  const fragment = document.createDocumentFragment();
  addonState.catalogs.forEach((catalog) => {
    const details = BUILTIN_CATALOG_DETAILS[catalog.id] || {};
    const titleText = catalog.name || details.title || catalog.id;
    const descriptionText = details.description || "Catalog settings.";
    const statuses = normalizeStatuses(catalog);
    const ordering = normalizeOrdering(catalog);

    const card = document.createElement("div");
    card.className = "rounded-2xl border border-line/60 bg-surface/80 p-5";
    card.dataset.catalogId = catalog.id;

    const header = document.createElement("div");
    header.className = "flex flex-wrap items-center justify-between gap-3";
    const info = document.createElement("div");
    const title = document.createElement("h3");
    title.className = "font-display text-base font-semibold text-ink";
    title.textContent = titleText;
    const desc = document.createElement("p");
    desc.className = "text-xs text-muted";
    desc.textContent = descriptionText;
    info.appendChild(title);
    info.appendChild(desc);

    const toggle = document.createElement("label");
    toggle.className = "inline-control";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = catalog.enabled !== false;
    checkbox.dataset.catalogEnabled = "true";
    const toggleLabel = document.createElement("span");
    toggleLabel.textContent = "Enabled";
    toggle.appendChild(checkbox);
    toggle.appendChild(toggleLabel);

    header.appendChild(info);
    header.appendChild(toggle);

    const body = document.createElement("div");
    body.className = "mt-4 grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]";

    const statusBlock = document.createElement("div");
    const statusLabel = document.createElement("p");
    statusLabel.className = "text-xs font-semibold uppercase tracking-[0.2em] text-muted";
    statusLabel.textContent = "Statuses";
    const statusGroup = document.createElement("div");
    statusGroup.className = "mt-2 flex flex-wrap gap-3";
    STATUS_OPTIONS.forEach((option) => {
      const label = document.createElement("label");
      label.className = "inline-control";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = option.value;
      input.dataset.catalogStatus = "true";
      input.checked = statuses.includes(option.value);
      const span = document.createElement("span");
      span.textContent = option.label;
      label.appendChild(input);
      label.appendChild(span);
      statusGroup.appendChild(label);
    });
    statusBlock.appendChild(statusLabel);
    statusBlock.appendChild(statusGroup);

    const orderingBlock = document.createElement("div");
    orderingBlock.className = "space-y-3";
    const orderByField = document.createElement("label");
    orderByField.className = "field";
    const orderByLabel = document.createElement("span");
    orderByLabel.textContent = "Order by";
    const orderBySelect = buildSelect(ORDER_OPTIONS, ordering.orderBy);
    orderBySelect.dataset.catalogOrderBy = "true";
    orderByField.appendChild(orderByLabel);
    orderByField.appendChild(orderBySelect);

    const orderDirField = document.createElement("label");
    orderDirField.className = "field";
    const orderDirLabel = document.createElement("span");
    orderDirLabel.textContent = "Direction";
    const orderDirSelect = buildSelect(
      [
        { value: "desc", label: "Descending" },
        { value: "asc", label: "Ascending" },
      ],
      ordering.orderDir
    );
    orderDirSelect.dataset.catalogOrderDir = "true";
    orderDirField.appendChild(orderDirLabel);
    orderDirField.appendChild(orderDirSelect);

    orderingBlock.appendChild(orderByField);
    orderingBlock.appendChild(orderDirField);

    body.appendChild(statusBlock);
    body.appendChild(orderingBlock);

    const footer = document.createElement("div");
    footer.className = "mt-4 flex flex-wrap items-center justify-between gap-3";
    const idLabel = document.createElement("span");
    idLabel.className = "text-xs text-muted";
    idLabel.textContent = `Catalog ID: ${catalog.id}`;
    const saveButton = document.createElement("button");
    saveButton.type = "button";
    saveButton.className = "btn btn-secondary btn-sm";
    saveButton.dataset.catalogSave = catalog.id;
    saveButton.textContent = "Save changes";
    footer.appendChild(idLabel);
    footer.appendChild(saveButton);

    card.appendChild(header);
    card.appendChild(body);
    card.appendChild(footer);
    fragment.appendChild(card);
  });
  container.appendChild(fragment);
}

function renderCustomCatalogs() {
  const container = document.getElementById("custom-catalog-list");
  if (!container) {
    return;
  }
  container.innerHTML = "";
  if (!addonState.customCatalogs.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No custom catalogs yet. Create one above.";
    container.appendChild(empty);
    return;
  }

  const fragment = document.createDocumentFragment();
  addonState.customCatalogs.forEach((catalog) => {
    const card = document.createElement("div");
    card.className = "rounded-2xl border border-line/60 bg-surface/80 p-5";
    if (catalog.id === addonState.selectedCatalogId) {
      card.classList.add("border-primary/50", "shadow-glow");
    }
    card.dataset.customCatalogId = catalog.id;

    const header = document.createElement("div");
    header.className = "flex flex-wrap items-start justify-between gap-3";
    const info = document.createElement("div");
    const title = document.createElement("h3");
    title.className = "font-display text-base font-semibold text-ink";
    title.textContent = catalog.name;
    const slug = document.createElement("p");
    slug.className = "text-xs text-muted";
    slug.textContent = `Slug: ${catalog.slug}`;
    info.appendChild(title);
    info.appendChild(slug);

    const actions = document.createElement("div");
    actions.className = "flex flex-wrap items-center gap-2";
    const manageButton = document.createElement("button");
    manageButton.type = "button";
    manageButton.className = "btn btn-secondary btn-sm";
    manageButton.dataset.customCatalogManage = catalog.id;
    manageButton.textContent =
      catalog.id === addonState.selectedCatalogId ? "Managing" : "Manage items";
    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "btn btn-ghost btn-sm";
    deleteButton.dataset.customCatalogDelete = catalog.id;
    deleteButton.textContent = "Delete";
    actions.appendChild(manageButton);
    actions.appendChild(deleteButton);

    header.appendChild(info);
    header.appendChild(actions);

    const body = document.createElement("div");
    body.className = "mt-4 grid gap-4 md:grid-cols-3";

    const nameField = document.createElement("label");
    nameField.className = "field md:col-span-3";
    const nameLabel = document.createElement("span");
    nameLabel.textContent = "Name";
    const nameInput = document.createElement("input");
    nameInput.className = "input";
    nameInput.type = "text";
    nameInput.value = catalog.name;
    nameInput.dataset.customCatalogName = "true";
    nameField.appendChild(nameLabel);
    nameField.appendChild(nameInput);

    const typeField = document.createElement("label");
    typeField.className = "field";
    const typeLabel = document.createElement("span");
    typeLabel.textContent = "Type";
    const typeSelect = buildSelect(
      [
        { value: "movie", label: "Movie" },
        { value: "tv", label: "TV" },
        { value: "anime", label: "Anime" },
      ],
      catalog.media_type
    );
    typeSelect.dataset.customCatalogType = "true";
    typeField.appendChild(typeLabel);
    typeField.appendChild(typeSelect);

    const orderField = document.createElement("label");
    orderField.className = "field";
    const orderLabel = document.createElement("span");
    orderLabel.textContent = "Order";
    const orderSelect = buildSelect(CUSTOM_ORDER_OPTIONS, catalog.order_by || "manual");
    orderSelect.dataset.customCatalogOrderBy = "true";
    orderField.appendChild(orderLabel);
    orderField.appendChild(orderSelect);

    const dirField = document.createElement("label");
    dirField.className = "field";
    const dirLabel = document.createElement("span");
    dirLabel.textContent = "Direction";
    const dirSelect = buildSelect(
      [
        { value: "asc", label: "Ascending" },
        { value: "desc", label: "Descending" },
      ],
      catalog.order_dir || "asc"
    );
    dirSelect.dataset.customCatalogOrderDir = "true";
    dirField.appendChild(dirLabel);
    dirField.appendChild(dirSelect);

    body.appendChild(nameField);
    body.appendChild(typeField);
    body.appendChild(orderField);
    body.appendChild(dirField);

    const footer = document.createElement("div");
    footer.className = "mt-4 flex flex-wrap items-center justify-between gap-3";
    const updated = document.createElement("span");
    updated.className = "text-xs text-muted";
    const updatedAt = catalog.updated_at
      ? `Updated ${formatMetadataDate(catalog.updated_at)}`
      : "Updated —";
    updated.textContent = updatedAt;
    const saveButton = document.createElement("button");
    saveButton.type = "button";
    saveButton.className = "btn btn-secondary btn-sm";
    saveButton.dataset.customCatalogUpdate = catalog.id;
    saveButton.textContent = "Save changes";

    footer.appendChild(updated);
    footer.appendChild(saveButton);

    card.appendChild(header);
    card.appendChild(body);
    card.appendChild(footer);

    fragment.appendChild(card);
  });
  container.appendChild(fragment);

  if (addonState.selectedCatalogId) {
    renderCustomCatalogItemsPanel();
  }
}

function renderCustomCatalogItemsPanel() {
  const panel = document.getElementById("custom-catalog-items-panel");
  if (!panel) {
    return;
  }
  const catalog = addonState.customCatalogs.find(
    (entry) => entry.id === addonState.selectedCatalogId
  );
  if (!catalog) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  const subtitle = document.getElementById("custom-catalog-items-subtitle");
  if (subtitle) {
    subtitle.textContent = `${catalog.name} · ${catalog.media_type.toUpperCase()} · ${catalog.slug}`;
  }
  const orderHint = document.getElementById("custom-catalog-items-order-hint");
  if (orderHint) {
    orderHint.textContent =
      catalog.order_by === "manual"
        ? "Manual ordering active."
        : "Manual ordering stored for later.";
  }
}

function resetCustomLookupUI() {
  const results = document.getElementById("custom-catalog-items-results");
  const candidates = document.getElementById("custom-catalog-items-candidates");
  if (results) {
    results.hidden = true;
  }
  if (candidates) {
    candidates.innerHTML = "";
  }
  addonState.candidates = [];
  addonState.lookupId = null;
  setMessage("custom-catalog-items-message", "");
}

function clearCustomLookupTimer() {
  if (addonState.lookupTimer) {
    window.clearTimeout(addonState.lookupTimer);
    addonState.lookupTimer = null;
  }
}

function catalogSearchScope(catalog) {
  if (!catalog) {
    return "all";
  }
  if (catalog.media_type === "anime") {
    return "anime";
  }
  if (catalog.media_type === "tv") {
    return "tv";
  }
  return "movie";
}

function candidateMatchesItem(candidate, item) {
  if (!candidate || !item) {
    return false;
  }
  const pairs = [
    ["imdb_id"],
    ["tmdb_id"],
    ["tvdb_id"],
    ["tvmaze_id"],
    ["kitsu_id"],
    ["myanimelist_id"],
    ["anilist_id"],
  ];
  return pairs.some(([key]) => {
    const left = candidate[key];
    const right = item[key];
    return left && right && String(left) === String(right);
  });
}

function renderCustomCandidates(candidates) {
  const results = document.getElementById("custom-catalog-items-results");
  const container = document.getElementById("custom-catalog-items-candidates");
  const catalog = addonState.customCatalogs.find(
    (entry) => entry.id === addonState.selectedCatalogId
  );
  if (!results || !container || !catalog) {
    return;
  }
  container.innerHTML = "";
  const matching = (candidates || []).filter(
    (candidate) => candidate.media_type === catalog.media_type
  );
  addonState.candidates = matching;

  if (!matching.length) {
    container.textContent = "No matches found for this catalog.";
  } else {
    matching.forEach((candidate) => {
      const row = document.createElement("div");
      row.className = "candidate-option";

      const action = document.createElement("button");
      action.type = "button";
      action.className = "btn btn-secondary btn-sm";
      action.dataset.customItemAdd = candidate.id;
      const alreadyAdded = addonState.items.some((item) => candidateMatchesItem(candidate, item));
      action.textContent = alreadyAdded ? "Added" : "Add";
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
      detail.textContent = `${year} · ${candidate.provider.toUpperCase()}`;
      meta.appendChild(title);
      meta.appendChild(detail);

      row.appendChild(action);
      row.appendChild(poster);
      row.appendChild(meta);
      container.appendChild(row);
    });
  }
  results.hidden = false;
  setMessage("custom-catalog-items-message", "");
}

async function pollCustomLookupStatus(lookupId) {
  try {
    const data = await requestJSON(`/api/metadata/lookup/${lookupId}`);
    if (data.status === "completed") {
      renderCustomCandidates(data.candidates || []);
      return;
    }
    if (data.status === "failed") {
      setMessage("custom-catalog-items-message", data.error || "Lookup failed.", true);
      return;
    }
    addonState.lookupTimer = window.setTimeout(() => pollCustomLookupStatus(lookupId), 1500);
  } catch (error) {
    setMessage("custom-catalog-items-message", error.message, true);
  }
}

async function handleCustomLookupSubmit(data) {
  resetCustomLookupUI();
  clearCustomLookupTimer();
  const query = (data.get("query") || "").trim();
  if (!query) {
    setMessage("custom-catalog-items-message", "Enter a title or ID to search.", true);
    return;
  }
  const catalog = addonState.customCatalogs.find(
    (entry) => entry.id === addonState.selectedCatalogId
  );
  try {
    setMessage("custom-catalog-items-message", "Searching...");
    const response = await requestJSON("/api/metadata/lookup", {
      method: "POST",
      body: JSON.stringify({ query, search_scope: catalogSearchScope(catalog) }),
    });
    addonState.lookupId = response.lookup_id;
    await pollCustomLookupStatus(response.lookup_id);
  } catch (error) {
    setMessage("custom-catalog-items-message", error.message, true);
  }
}

function getCustomCandidate(candidateId) {
  return addonState.candidates.find((candidate) => candidate.id === candidateId) || null;
}

function buildCustomItemPayload(candidate) {
  return {
    media_type: candidate.media_type,
    title: candidate.title,
    year: candidate.year || null,
    poster_url: candidate.poster_url || null,
    imdb_id: candidate.imdb_id || null,
    tmdb_id: candidate.tmdb_id || null,
    tvdb_id: candidate.tvdb_id || null,
    tvmaze_id: candidate.tvmaze_id || null,
    kitsu_id: candidate.kitsu_id || null,
    myanimelist_id: candidate.myanimelist_id || null,
    anilist_id: candidate.anilist_id || null,
  };
}

async function handleCustomItemAdd(candidateId) {
  const catalogId = addonState.selectedCatalogId;
  if (!catalogId) {
    return;
  }
  const candidate = getCustomCandidate(candidateId);
  if (!candidate) {
    setMessage("custom-catalog-items-message", "Select an item to add.", true);
    return;
  }
  try {
    await requestJSON(`/api/stremio-addon/custom-catalogs/${catalogId}/items`, {
      method: "POST",
      body: JSON.stringify(buildCustomItemPayload(candidate)),
    });
    setMessage(
      "custom-catalog-items-message",
      `${candidate.title || "Item"} added to catalog.`
    );
    await loadCustomCatalogItems();
    renderCustomCandidates(addonState.candidates);
  } catch (error) {
    setMessage("custom-catalog-items-message", error.message, true);
  }
}

function renderCustomItems() {
  const container = document.getElementById("custom-catalog-items-list");
  if (!container) {
    return;
  }
  container.innerHTML = "";
  if (!addonState.items.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No items yet. Add a few above.";
    container.appendChild(empty);
    return;
  }

  const catalog = addonState.customCatalogs.find(
    (entry) => entry.id === addonState.selectedCatalogId
  );
  const allowManual = catalog ? catalog.order_by === "manual" : false;

  addonState.items.forEach((item, index) => {
    const row = document.createElement("div");
    row.className = "candidate-option";

    const actions = document.createElement("div");
    actions.className = "flex flex-col gap-2";
    const upButton = document.createElement("button");
    upButton.type = "button";
    upButton.className = "btn btn-ghost btn-xs";
    upButton.dataset.customItemMove = "up";
    upButton.dataset.mediaItemId = item.media_item_id;
    upButton.textContent = "Up";
    upButton.disabled = index === 0 || !allowManual;

    const downButton = document.createElement("button");
    downButton.type = "button";
    downButton.className = "btn btn-ghost btn-xs";
    downButton.dataset.customItemMove = "down";
    downButton.dataset.mediaItemId = item.media_item_id;
    downButton.textContent = "Down";
    downButton.disabled = index === addonState.items.length - 1 || !allowManual;

    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "btn btn-ghost btn-xs";
    removeButton.dataset.customItemRemove = item.media_item_id;
    removeButton.textContent = "Remove";

    actions.appendChild(upButton);
    actions.appendChild(downButton);
    actions.appendChild(removeButton);

    const poster = document.createElement("img");
    poster.className = "candidate-poster";
    if (item.poster_url) {
      poster.src = item.poster_url;
      poster.alt = `${item.title} poster`;
      poster.loading = "lazy";
    } else {
      poster.alt = "";
    }

    const meta = document.createElement("div");
    meta.className = "candidate-meta";
    const title = document.createElement("h3");
    title.textContent = item.title || "Unknown title";
    const detail = document.createElement("p");
    const year = item.year ? ` (${item.year})` : "";
    detail.textContent = `${item.media_type.toUpperCase()}${year}`;
    meta.appendChild(title);
    meta.appendChild(detail);

    row.appendChild(actions);
    row.appendChild(poster);
    row.appendChild(meta);
    container.appendChild(row);
  });
}

async function loadCustomCatalogItems() {
  const catalogId = addonState.selectedCatalogId;
  const container = document.getElementById("custom-catalog-items-list");
  if (!catalogId || !container) {
    return;
  }
  container.textContent = "Loading...";
  try {
    const data = await requestJSON(
      `/api/stremio-addon/custom-catalogs/${catalogId}/items`
    );
    addonState.items = data && Array.isArray(data.items) ? data.items : [];
    renderCustomItems();
    if (addonState.candidates.length) {
      renderCustomCandidates(addonState.candidates);
    }
  } catch (error) {
    container.innerHTML = "";
    setMessage("custom-catalog-items-message", error.message, true);
  }
}

async function handleCustomItemRemove(mediaItemId) {
  const catalogId = addonState.selectedCatalogId;
  if (!catalogId) {
    return;
  }
  setMessage("custom-catalog-items-message", "");
  try {
    await requestJSON(
      `/api/stremio-addon/custom-catalogs/${catalogId}/items/${mediaItemId}`,
      { method: "DELETE" }
    );
    setMessage("custom-catalog-items-message", "Item removed.");
    await loadCustomCatalogItems();
  } catch (error) {
    setMessage("custom-catalog-items-message", error.message, true);
  }
}

async function handleCustomItemMove(mediaItemId, direction) {
  const catalogId = addonState.selectedCatalogId;
  if (!catalogId) {
    return;
  }
  const index = addonState.items.findIndex((item) => item.media_item_id === mediaItemId);
  if (index < 0) {
    return;
  }
  const targetIndex = direction === "up" ? index - 1 : index + 1;
  if (targetIndex < 0 || targetIndex >= addonState.items.length) {
    return;
  }
  const reordered = addonState.items.slice();
  const [moved] = reordered.splice(index, 1);
  reordered.splice(targetIndex, 0, moved);
  try {
    await requestJSON(`/api/stremio-addon/custom-catalogs/${catalogId}/reorder`, {
      method: "POST",
      body: JSON.stringify({
        media_item_ids: reordered.map((item) => item.media_item_id),
      }),
    });
    addonState.items = reordered;
    renderCustomItems();
  } catch (error) {
    setMessage("custom-catalog-items-message", error.message, true);
    await loadCustomCatalogItems();
  }
}

async function handleCustomCatalogCreate(data, form) {
  setMessage("custom-catalog-message", "");
  const payload = {
    name: (data.get("name") || "").trim(),
    media_type: data.get("media_type") || "movie",
    order_by: data.get("order_by") || "manual",
    order_dir: data.get("order_dir") || "asc",
  };
  if (!payload.name) {
    setMessage("custom-catalog-message", "Name is required.", true);
    return;
  }
  try {
    const response = await requestJSON("/api/stremio-addon/custom-catalogs", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    addonState.customCatalogs = [...addonState.customCatalogs, response];
    renderCustomCatalogs();
    if (form) {
      form.reset();
    }
    setMessage("custom-catalog-message", "Catalog created.");
  } catch (error) {
    setMessage("custom-catalog-message", error.message, true);
  }
}

async function handleCustomCatalogUpdate(catalogId, card) {
  setMessage("custom-catalog-message", "");
  const nameInput = card.querySelector("[data-custom-catalog-name]");
  const typeSelect = card.querySelector("[data-custom-catalog-type]");
  const orderSelect = card.querySelector("[data-custom-catalog-order-by]");
  const dirSelect = card.querySelector("[data-custom-catalog-order-dir]");
  const payload = {
    name: nameInput ? nameInput.value.trim() : "",
    media_type: typeSelect ? typeSelect.value : null,
    order_by: orderSelect ? orderSelect.value : null,
    order_dir: dirSelect ? dirSelect.value : null,
  };
  if (!payload.name) {
    setMessage("custom-catalog-message", "Name is required.", true);
    return;
  }
  try {
    const response = await requestJSON(
      `/api/stremio-addon/custom-catalogs/${catalogId}`,
      {
        method: "PATCH",
        body: JSON.stringify(payload),
      }
    );
    addonState.customCatalogs = addonState.customCatalogs.map((entry) =>
      entry.id === catalogId ? response : entry
    );
    renderCustomCatalogs();
    setMessage("custom-catalog-message", "Catalog updated.");
  } catch (error) {
    setMessage("custom-catalog-message", error.message, true);
  }
}

async function handleCustomCatalogDelete(catalogId) {
  setMessage("custom-catalog-message", "");
  const confirmDelete = window.confirm(
    "Delete this catalog? The items will be removed from Stremio."
  );
  if (!confirmDelete) {
    return;
  }
  try {
    await requestJSON(`/api/stremio-addon/custom-catalogs/${catalogId}`, {
      method: "DELETE",
    });
    addonState.customCatalogs = addonState.customCatalogs.filter(
      (entry) => entry.id !== catalogId
    );
    if (addonState.selectedCatalogId === catalogId) {
      addonState.selectedCatalogId = null;
      addonState.items = [];
      resetCustomLookupUI();
      const panel = document.getElementById("custom-catalog-items-panel");
      if (panel) {
        panel.hidden = true;
      }
    }
    renderCustomCatalogs();
    setMessage("custom-catalog-message", "Catalog deleted.");
  } catch (error) {
    setMessage("custom-catalog-message", error.message, true);
  }
}

async function handleEnableSave() {
  setControlsMessage("");
  const enabledToggle = document.getElementById("stremio-addon-enabled");
  if (!enabledToggle) {
    return;
  }
  try {
    const response = await requestJSON("/api/stremio-addon/config", {
      method: "POST",
      body: JSON.stringify({ is_enabled: enabledToggle.checked }),
    });
    if (!addonState.config) {
      addonState.config = {};
    }
    addonState.config.is_enabled = response.is_enabled;
    setControlsMessage("Status saved.");
  } catch (error) {
    setControlsMessage(error.message, true);
  }
}

async function handleCatalogSave(button) {
  const catalogId = button.dataset.catalogSave;
  const card = button.closest("[data-catalog-id]");
  if (!catalogId || !card) {
    return;
  }
  setAddonMessage("");
  const enabledToggle = card.querySelector("[data-catalog-enabled]");
  const statusChecks = Array.from(card.querySelectorAll("[data-catalog-status]"));
  const orderBySelect = card.querySelector("[data-catalog-order-by]");
  const orderDirSelect = card.querySelector("[data-catalog-order-dir]");
  const statuses = statusChecks
    .filter((input) => input.checked)
    .map((input) => input.value);
  const payload = {
    catalogs: [
      {
        id: catalogId,
        enabled: enabledToggle ? enabledToggle.checked : true,
        filters: { statuses },
        ordering: {
          order_by: orderBySelect ? orderBySelect.value : "date_added",
          order_dir: orderDirSelect ? orderDirSelect.value : "desc",
        },
      },
    ],
  };
  try {
    const response = await requestJSON("/api/stremio-addon/config", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    addonState.catalogs = response && Array.isArray(response.catalogs) ? response.catalogs : [];
    renderBuiltInCatalogs();
    setAddonMessage("Catalog updated.");
  } catch (error) {
    setAddonMessage(error.message, true);
  }
}

async function loadAddonConfig() {
  try {
    const data = await requestJSON("/api/stremio-addon/config");
    addonState.config = data;
    addonState.catalogs = Array.isArray(data.catalogs) ? data.catalogs : [];
    addonState.customCatalogs = Array.isArray(data.custom_catalogs) ? data.custom_catalogs : [];
    updateInstallLinks(data);
    renderInstallSection();
    renderControlSection();
    renderBuiltInCatalogs();
    renderCustomCatalogs();
  } catch (error) {
    setAddonMessage(error.message, true);
  }
}

async function copyValue(value, messageId) {
  if (!value) {
    setMessage(messageId, "Nothing to copy.", true);
    return;
  }
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
    } else {
      const temp = document.createElement("textarea");
      temp.value = value;
      temp.setAttribute("readonly", "");
      temp.style.position = "absolute";
      temp.style.left = "-9999px";
      document.body.appendChild(temp);
      temp.select();
      document.execCommand("copy");
      document.body.removeChild(temp);
    }
    setMessage(messageId, "Copied.");
  } catch (error) {
    setMessage(messageId, "Copy failed.", true);
  }
}

function bindAddonActions() {
  const enableSave = document.getElementById("stremio-addon-enable-save");
  if (enableSave) {
    enableSave.addEventListener("click", handleEnableSave);
  }

  const manifestCopy = document.getElementById("stremio-manifest-copy");
  if (manifestCopy) {
    manifestCopy.addEventListener("click", () =>
      copyValue(addonState.installLinks.manifestUrl, "stremio-install-message")
    );
  }
  const installCopy = document.getElementById("stremio-install-copy");
  if (installCopy) {
    installCopy.addEventListener("click", () =>
      copyValue(addonState.installLinks.installUrl, "stremio-install-message")
    );
  }

  const catalogs = document.getElementById("stremio-addon-catalogs");
  if (catalogs) {
    catalogs.addEventListener("click", (event) => {
      const button = event.target.closest("[data-catalog-save]");
      if (!button) {
        return;
      }
      handleCatalogSave(button);
    });
  }

  const customList = document.getElementById("custom-catalog-list");
  if (customList) {
    customList.addEventListener("click", (event) => {
      const manage = event.target.closest("[data-custom-catalog-manage]");
      if (manage) {
        addonState.selectedCatalogId = manage.dataset.customCatalogManage;
        renderCustomCatalogs();
        resetCustomLookupUI();
        loadCustomCatalogItems();
        return;
      }
      const update = event.target.closest("[data-custom-catalog-update]");
      if (update) {
        const card = update.closest("[data-custom-catalog-id]");
        if (card) {
          handleCustomCatalogUpdate(update.dataset.customCatalogUpdate, card);
        }
        return;
      }
      const remove = event.target.closest("[data-custom-catalog-delete]");
      if (remove) {
        handleCustomCatalogDelete(remove.dataset.customCatalogDelete);
      }
    });
  }

  const closeItems = document.getElementById("custom-catalog-items-close");
  if (closeItems) {
    closeItems.addEventListener("click", () => {
      addonState.selectedCatalogId = null;
      addonState.items = [];
      resetCustomLookupUI();
      const panel = document.getElementById("custom-catalog-items-panel");
      if (panel) {
        panel.hidden = true;
      }
      renderCustomCatalogs();
    });
  }

  const candidates = document.getElementById("custom-catalog-items-candidates");
  if (candidates) {
    candidates.addEventListener("click", (event) => {
      const button = event.target.closest("[data-custom-item-add]");
      if (!button) {
        return;
      }
      handleCustomItemAdd(button.dataset.customItemAdd);
    });
  }

  const itemsList = document.getElementById("custom-catalog-items-list");
  if (itemsList) {
    itemsList.addEventListener("click", (event) => {
      const moveButton = event.target.closest("[data-custom-item-move]");
      if (moveButton) {
        handleCustomItemMove(
          moveButton.dataset.mediaItemId,
          moveButton.dataset.customItemMove
        );
        return;
      }
      const removeButton = event.target.closest("[data-custom-item-remove]");
      if (removeButton) {
        handleCustomItemRemove(removeButton.dataset.customItemRemove);
      }
    });
  }
}

window.librarysyncPageInit = async ({ user }) => {
  if (!user) {
    return;
  }
  bindForm("custom-catalog-form", handleCustomCatalogCreate);
  bindForm("custom-catalog-items-lookup-form", handleCustomLookupSubmit);
  bindAddonActions();
  await loadAddonConfig();
};
