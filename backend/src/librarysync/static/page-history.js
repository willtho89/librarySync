const historyState = {
  page: 1,
  pageSize: 50,
  total: 0,
  filters: {
    search: "",
    mediaType: "all",
    source: "all",
  },
  searchTimer: null,
};

let historyUiBound = false;
const historySelectionState = {
  items: [],
  selectedIds: new Set(),
};

function historyHasActiveFilters() {
  return (
    (historyState.filters.search && historyState.filters.search.trim()) ||
    historyState.filters.mediaType !== "all" ||
    historyState.filters.source !== "all"
  );
}

function buildHistoryQueryParams() {
  const params = new URLSearchParams();
  params.set("limit", String(historyState.pageSize));
  params.set("offset", String((historyState.page - 1) * historyState.pageSize));
  if (historyState.filters.search && historyState.filters.search.trim()) {
    params.set("search", historyState.filters.search.trim());
  }
  if (historyState.filters.mediaType !== "all") {
    params.set("media_type", historyState.filters.mediaType);
  }
  if (historyState.filters.source !== "all") {
    params.set("source", historyState.filters.source);
  }
  return params;
}

function getHistoryTotalPages(total) {
  const value = Math.ceil(total / historyState.pageSize);
  return Math.max(1, value);
}

function updateHistoryPagination() {
  const info = document.getElementById("history-page-info");
  const prev = document.getElementById("history-page-prev");
  const next = document.getElementById("history-page-next");
  const clearFilters = document.getElementById("history-filters-clear");
  const total = historyState.total;
  const totalPages = getHistoryTotalPages(total);
  const label = total
    ? `Page ${historyState.page} of ${totalPages} · ${total} items`
    : historyHasActiveFilters()
      ? "No matches for your filters."
      : "No history items yet.";
  if (info) {
    info.textContent = label;
  }
  if (prev) {
    prev.disabled = historyState.page <= 1;
  }
  if (next) {
    next.disabled = historyState.page >= totalPages;
  }
  if (clearFilters) {
    clearFilters.disabled = !historyHasActiveFilters();
  }
}

function updateHistoryBulkControls() {
  const selectAll = document.getElementById("history-select-all");
  const deleteButton = document.getElementById("history-delete-selected");
  const bulkBar = document.querySelector("[data-bulk-bar]");
  const total = historySelectionState.items.length;
  const selectedCount = historySelectionState.selectedIds.size;

  if (selectAll) {
    selectAll.disabled = total === 0;
    if (total === 0 || selectedCount === 0) {
      selectAll.checked = false;
      selectAll.indeterminate = false;
    } else if (selectedCount === total) {
      selectAll.checked = true;
      selectAll.indeterminate = false;
    } else {
      selectAll.checked = false;
      selectAll.indeterminate = true;
    }
  }

  if (deleteButton) {
    deleteButton.disabled = selectedCount === 0;
    deleteButton.textContent = selectedCount
      ? `Delete selected (${selectedCount})`
      : "Delete selected";
  }

  if (bulkBar) {
    if (selectedCount > 0) {
      bulkBar.classList.add("is-visible");
    } else {
      bulkBar.classList.remove("is-visible");
    }
  }

  updateHistorySyncControls();
}

function updateHistorySyncControls() {
  const container = document.getElementById("history-sync-actions");
  const syncAllToggle = document.getElementById("history-sync-all");
  if (!container) {
    return;
  }
  const hasItems = historySelectionState.items.length > 0;
  const totalItems = historyState.total || 0;
  const allowSync = hasItems || totalItems > 0;
  if (syncAllToggle) {
    syncAllToggle.disabled = totalItems === 0;
  }
  container.querySelectorAll("button[data-history-sync]").forEach((button) => {
    button.disabled = !allowSync;
  });
}

function resetHistorySelection(items) {
  historySelectionState.items = items;
  historySelectionState.selectedIds.clear();
  updateHistoryBulkControls();
}

function bindHistoryUi() {
  if (historyUiBound) {
    return;
  }
  const modal = document.getElementById("metadata-modal");
  if (!modal) {
    return;
  }
  modal.querySelectorAll("[data-modal-close]").forEach((button) => {
    button.addEventListener("click", closeMetadataModal);
  });
  document.addEventListener("click", (event) => {
    if (event.target && event.target.closest(".history-actions")) {
      return;
    }
    closeHistoryMenus();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeHistoryMenus();
      closeMetadataModal();
    }
  });
  const selectAll = document.getElementById("history-select-all");
  if (selectAll) {
    selectAll.addEventListener("change", () => {
      const shouldSelect = selectAll.checked;
      historySelectionState.selectedIds.clear();
      document.querySelectorAll("input[data-history-select]").forEach((input) => {
        input.checked = shouldSelect;
        if (shouldSelect) {
          historySelectionState.selectedIds.add(input.value);
        }
      });
      updateHistoryBulkControls();
    });
  }
  const searchInput = document.getElementById("history-search");
  if (searchInput) {
    searchInput.value = historyState.filters.search;
    searchInput.addEventListener("input", () => {
      historyState.filters.search = searchInput.value || "";
      historyState.page = 1;
      if (historyState.searchTimer) {
        window.clearTimeout(historyState.searchTimer);
      }
      historyState.searchTimer = window.setTimeout(() => {
        loadHistory();
      }, 250);
    });
  }
  const typeSelect = document.getElementById("history-type-filter");
  if (typeSelect) {
    typeSelect.value = historyState.filters.mediaType;
    typeSelect.addEventListener("change", () => {
      historyState.filters.mediaType = typeSelect.value || "all";
      historyState.page = 1;
      loadHistory();
    });
  }
  const sourceSelect = document.getElementById("history-source-filter");
  if (sourceSelect) {
    sourceSelect.value = historyState.filters.source;
    sourceSelect.addEventListener("change", () => {
      historyState.filters.source = sourceSelect.value || "all";
      historyState.page = 1;
      loadHistory();
    });
  }
  const pageSizeSelect = document.getElementById("history-page-size");
  if (pageSizeSelect) {
    pageSizeSelect.value = String(historyState.pageSize);
    pageSizeSelect.addEventListener("change", () => {
      const parsed = Number(pageSizeSelect.value);
      if (!Number.isFinite(parsed) || parsed <= 0) {
        return;
      }
      historyState.pageSize = Math.trunc(parsed);
      historyState.page = 1;
      loadHistory();
    });
  }
  const prevButton = document.getElementById("history-page-prev");
  if (prevButton) {
    prevButton.addEventListener("click", () => {
      if (historyState.page <= 1) {
        return;
      }
      historyState.page -= 1;
      loadHistory();
    });
  }
  const nextButton = document.getElementById("history-page-next");
  if (nextButton) {
    nextButton.addEventListener("click", () => {
      const totalPages = getHistoryTotalPages(historyState.total);
      if (historyState.page >= totalPages) {
        return;
      }
      historyState.page += 1;
      loadHistory();
    });
  }
  const clearFilters = document.getElementById("history-filters-clear");
  if (clearFilters) {
    clearFilters.addEventListener("click", () => {
      historyState.filters.search = "";
      historyState.filters.mediaType = "all";
      historyState.filters.source = "all";
      historyState.page = 1;
      if (searchInput) {
        searchInput.value = "";
      }
      if (typeSelect) {
        typeSelect.value = "all";
      }
      if (sourceSelect) {
        sourceSelect.value = "all";
      }
      loadHistory();
    });
  }
  const deleteSelectedButton = document.getElementById("history-delete-selected");
  if (deleteSelectedButton) {
    deleteSelectedButton.addEventListener("click", async () => {
      const selectedIds = Array.from(historySelectionState.selectedIds);
      if (!selectedIds.length) {
        return;
      }
      const deleteIntegrationsToggle = document.getElementById(
        "history-delete-integrations"
      );
      const deleteIntegrations =
        deleteIntegrationsToggle && deleteIntegrationsToggle.checked;
      const confirmed = window.confirm(
        deleteIntegrations
          ? `Delete ${selectedIds.length} selected items from history and ` +
              "connected integrations? This cannot be undone."
          : `Delete ${selectedIds.length} selected items from history?`
      );
      if (!confirmed) {
        return;
      }
      try {
        setMessage("history-message", "Deleting selected...");
        const data = await requestJSON("/api/history/items/bulk-delete", {
          method: "POST",
          body: JSON.stringify({
            watched_ids: selectedIds,
            delete_integrations: deleteIntegrations,
          }),
        });
        const deletedCount = data && typeof data.deleted === "number" ? data.deleted : 0;
        setMessage("history-message", `Deleted ${deletedCount} entries.`);
        await loadHistory();
      } catch (error) {
        setMessage("history-message", error.message, true);
      }
    });
  }
  historyUiBound = true;
}

function renderHistorySyncButtons(integrations) {
  const container = document.getElementById("history-sync-actions");
  const syncAllWrap = document.getElementById("history-sync-all-wrap");
  if (!container) {
    return;
  }
  const note = document.getElementById("history-sync-note");
  container.innerHTML = "";
  const supported = new Set(["letterboxd", "trakt", "simkl", "stremio"]);
  const enabled = (integrations || []).filter(
    (integration) =>
      integration &&
      supported.has(integration.provider) &&
      isIntegrationConnected(integration)
  );
  if (!enabled.length) {
    container.hidden = true;
    if (syncAllWrap) {
      syncAllWrap.hidden = true;
    }
    if (note) {
      note.hidden = true;
    }
    return;
  }
  enabled.forEach((integration) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary-button";
    button.dataset.historySync = integration.provider;
    const label = formatIntegrationName(integration.provider);
    button.textContent = `Sync to ${label}`;
    button.addEventListener("click", () => handleHistorySync(integration.provider));
    container.appendChild(button);
  });
  container.hidden = false;
  if (syncAllWrap) {
    syncAllWrap.hidden = false;
  }
  if (note) {
    note.hidden = false;
  }
  updateHistorySyncControls();
}

async function handleHistorySync(provider) {
  const label = formatIntegrationName(provider);
  const totalItems = historyState.total || 0;
  const pageItems = historySelectionState.items || [];
  if (!pageItems.length && !totalItems) {
    setMessage("history-message", "No history items to sync.", true);
    return;
  }
  const syncAllToggle = document.getElementById("history-sync-all");
  const syncAll = syncAllToggle && syncAllToggle.checked;
  const selectedIds = Array.from(historySelectionState.selectedIds);
  const hasSelection = selectedIds.length > 0;
  const pageIds = pageItems.map((item) => item.id).filter(Boolean);
  const count = syncAll
    ? totalItems
    : hasSelection
      ? selectedIds.length
      : pageIds.length;
  const prompt = syncAll
    ? `Sync all ${count} item${count === 1 ? "" : "s"} to ${label}?`
    : hasSelection
      ? `Sync ${count} selected item${count === 1 ? "" : "s"} to ${label}?`
      : `Sync ${count} item${count === 1 ? "" : "s"} from this page to ${label}?`;
  if (!window.confirm(prompt)) {
    return;
  }
  const payload = { provider };
  if (syncAll) {
    // omit watched_ids to sync the full history
  } else if (hasSelection) {
    payload.watched_ids = selectedIds;
  } else {
    payload.watched_ids = pageIds;
  }
  try {
    setMessage("history-message", "Queueing sync...");
    const data = await requestJSON("/api/history/items/sync", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const requested =
      data && typeof data.requested === "number" ? data.requested : count;
    setMessage(
      "history-message",
      `Sync requested for ${requested} item${requested === 1 ? "" : "s"} to ${label}.`
    );
    await loadHistory();
  } catch (error) {
    setMessage("history-message", error.message, true);
  }
}

function closeHistoryMenus() {
  document.querySelectorAll("[data-menu-panel].is-open").forEach((panel) => {
    panel.classList.remove("is-open");
  });
  document.querySelectorAll("[data-menu-button]").forEach((button) => {
    button.setAttribute("aria-expanded", "false");
  });
}

// formatMetadataValue, formatActivityTitle removed as they are in watch-utils.js now if needed (formatActivityTitle was unused here or not exported?)
// Actually formatActivityTitle was defined in page-history.js but not used in snippet I read?
// Wait, I see formatMetadataValue and renderMetadataSection and openMetadataModal etc being defined here.
// I should remove them.

async function loadHistory() {
  const container = document.getElementById("history-list");
  if (!container) {
    return;
  }
  bindHistoryUi();
  container.textContent = "Loading...";
  const params = buildHistoryQueryParams();
  let data = null;
  try {
    data = await requestJSON(`/api/history/items?${params.toString()}`);
  } catch (error) {
    setMessage("history-message", error.message, true);
    container.textContent = "Unable to load history.";
    return;
  }
  const items = data && data.items ? data.items : [];
  const total = data && typeof data.total === "number" ? data.total : items.length;
  historyState.total = total;
  const totalPages = getHistoryTotalPages(total);
  if (historyState.page > totalPages) {
    historyState.page = totalPages;
    await loadHistory();
    return;
  }
  resetHistorySelection(items);
  updateHistoryPagination();
  if (!items.length) {
    container.textContent = historyHasActiveFilters()
      ? "No watched titles match your filters."
      : "No watched titles yet.";
    return;
  }
  container.innerHTML = "";
  items.forEach((item) => {
    const card = document.createElement("div");
    card.className = "history-card";
    const selectWrap = document.createElement("label");
    selectWrap.className = "history-select";
    const selectInput = document.createElement("input");
    selectInput.type = "checkbox";
    selectInput.value = item.id;
    selectInput.setAttribute("aria-label", `Select ${item.title}`);
    selectInput.setAttribute("data-history-select", "true");
    selectInput.addEventListener("change", () => {
      if (selectInput.checked) {
        historySelectionState.selectedIds.add(item.id);
      } else {
        historySelectionState.selectedIds.delete(item.id);
      }
      updateHistoryBulkControls();
    });
    selectWrap.appendChild(selectInput);
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
    meta.className = "candidate-meta history-meta";
    const title = document.createElement("h3");
    title.textContent = item.title;
    const detail = document.createElement("p");
    const watchedAt = new Date(item.watched_at);
    const watchedLabel = Number.isNaN(watchedAt.valueOf())
      ? ""
      : watchedAt.toLocaleString();
    const year = item.year ? item.year : "Year unknown";
    const mediaType = formatMediaType(item.media_type);
    const hasEpisode =
      item.season_number !== null &&
      item.season_number !== undefined &&
      item.episode_number !== null &&
      item.episode_number !== undefined;
    const detailParts = [year, mediaType];
    if (hasEpisode) {
      const episodeLabel = formatSeasonEpisode(item.season_number, item.episode_number);
      if (episodeLabel) {
        detailParts.push(
          item.episode_title ? `${episodeLabel} · ${item.episode_title}` : episodeLabel,
        );
      } else {
        detailParts.push("Episode unknown");
      }
    }
    if (watchedLabel) {
      detailParts.push(`Watched ${watchedLabel}`);
    }
    if (item.rating !== null && item.rating !== undefined) {
      const ratingLabel = formatRating(item.rating);
      if (ratingLabel) {
        detailParts.push(`Rated ${ratingLabel}`);
      }
    }
    const letterboxdStatus = item.letterboxd_status;
    const letterboxdSucceeded = letterboxdStatus === "succeeded";
    if (letterboxdStatus) {
      const statusLabel = letterboxdStatus.replace(/_/g, " ");
      detailParts.push(`Letterboxd ${statusLabel}`);
    }
    if (!letterboxdSucceeded && item.letterboxd_last_error) {
      const rawError = String(item.letterboxd_last_error);
      const shortError =
        rawError.length > 120 ? `${rawError.slice(0, 120)}...` : rawError;
      detailParts.push(`Letterboxd error: ${shortError}`);
    }
    const traktStatus = item.trakt_status;
    const traktSucceeded = traktStatus === "succeeded";
    if (traktStatus) {
      const statusLabel = traktStatus.replace(/_/g, " ");
      detailParts.push(`Trakt ${statusLabel}`);
    }
    if (!traktSucceeded && item.trakt_last_error) {
      const rawError = String(item.trakt_last_error);
      const shortError =
        rawError.length > 120 ? `${rawError.slice(0, 120)}...` : rawError;
      detailParts.push(`Trakt error: ${shortError}`);
    }
    const simklStatus = item.simkl_status;
    const simklSucceeded = simklStatus === "succeeded";
    if (simklStatus) {
      const statusLabel = simklStatus.replace(/_/g, " ");
      detailParts.push(`SIMKL ${statusLabel}`);
    }
    if (!simklSucceeded && item.simkl_last_error) {
      const rawError = String(item.simkl_last_error);
      const shortError =
        rawError.length > 120 ? `${rawError.slice(0, 120)}...` : rawError;
      detailParts.push(`SIMKL error: ${shortError}`);
    }
    const stremioStatus = item.stremio_status;
    const stremioSucceeded = stremioStatus === "succeeded";
    if (stremioStatus) {
      const statusLabel = stremioStatus.replace(/_/g, " ");
      detailParts.push(`Stremio ${statusLabel}`);
    }
    if (!stremioSucceeded && item.stremio_last_error) {
      const rawError = String(item.stremio_last_error);
      const shortError =
        rawError.length > 120 ? `${rawError.slice(0, 120)}...` : rawError;
      detailParts.push(`Stremio error: ${shortError}`);
    }
    detail.textContent = detailParts.join(" · ");

    const header = document.createElement("div");
    header.className = "history-header";

    const actions = document.createElement("div");
    actions.className = "history-actions";

    const menuButton = document.createElement("button");
    menuButton.type = "button";
    menuButton.className = "menu-button";
    menuButton.setAttribute("aria-haspopup", "true");
    menuButton.setAttribute("aria-expanded", "false");
    menuButton.setAttribute("aria-label", "More actions");
    menuButton.setAttribute("data-menu-button", "true");

    const dotStack = document.createElement("span");
    dotStack.className = "menu-dots";
    for (let i = 0; i < 3; i += 1) {
      const dot = document.createElement("span");
      dot.className = "menu-dot";
      dotStack.appendChild(dot);
    }
    menuButton.appendChild(dotStack);

    const menuPanel = document.createElement("div");
    menuPanel.className = "menu-panel";
    menuPanel.setAttribute("role", "menu");
    menuPanel.setAttribute("data-menu-panel", "true");

    const editButton = document.createElement("button");
    editButton.type = "button";
    editButton.textContent = "Edit watch";
    editButton.setAttribute("role", "menuitem");

    const addToWatchlistButton = document.createElement("button");
    addToWatchlistButton.type = "button";
    addToWatchlistButton.textContent = "Add show to watchlist";
    addToWatchlistButton.setAttribute("role", "menuitem");
    addToWatchlistButton.addEventListener("click", async () => {
      closeHistoryMenus();
      try {
        setMessage("history-message", "Adding to watchlist...");
        await requestJSON("/api/watchlist/items", {
          method: "POST",
          body: JSON.stringify({
            media_type: item.media_type,
            title: item.title,
            year: item.year,
            poster_url: item.poster_url,
            imdb_id: item.imdb_id,
            tmdb_id: item.tmdb_id,
            tvdb_id: item.tvdb_id,
            tvmaze_id: item.tvmaze_id,
            kitsu_id: item.kitsu_id,
            myanimelist_id: item.myanimelist_id,
            anilist_id: item.anilist_id,
          }),
        });
        setMessage("history-message", "Added to watchlist.");
      } catch (error) {
        setMessage("history-message", error.message, true);
      }
    });

    const metadataButton = document.createElement("button");
    metadataButton.type = "button";
    metadataButton.textContent = "View metadata";
    metadataButton.setAttribute("role", "menuitem");

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "danger-button";
    deleteButton.textContent = "Delete";
    deleteButton.setAttribute("role", "menuitem");

    menuPanel.appendChild(editButton);
    if (item.media_type === "tv" || item.media_type === "anime") {
      menuPanel.appendChild(addToWatchlistButton);
    }
    menuPanel.appendChild(metadataButton);
    menuPanel.appendChild(deleteButton);

    actions.appendChild(menuButton);
    actions.appendChild(menuPanel);

    const editRow = document.createElement("div");
    editRow.className = "history-edit";

    const editInput = document.createElement("input");
    editInput.type = "datetime-local";
    editInput.value = formatDateTimeInput(item.watched_at);

    const ratingSelect = buildRatingSelect(item.rating);
    const initialRatingValue =
      item.rating !== null && item.rating !== undefined ? String(item.rating) : "";

    const saveButton = document.createElement("button");
    saveButton.type = "button";
    saveButton.textContent = "Save";

    const cancelButton = document.createElement("button");
    cancelButton.type = "button";
    cancelButton.className = "secondary-button";
    cancelButton.textContent = "Cancel";

    editRow.appendChild(editInput);
    editRow.appendChild(ratingSelect);
    editRow.appendChild(saveButton);
    editRow.appendChild(cancelButton);

    menuButton.addEventListener("click", (event) => {
      event.stopPropagation();
      const isOpen = menuPanel.classList.contains("is-open");
      closeHistoryMenus();
      if (!isOpen) {
        menuPanel.classList.add("is-open");
        menuButton.setAttribute("aria-expanded", "true");
      }
    });

    editButton.addEventListener("click", () => {
      closeHistoryMenus();
      editRow.classList.add("is-visible");
      editInput.focus();
    });

    metadataButton.addEventListener("click", () => {
      closeHistoryMenus();
      openMetadataModal(item);
    });

    cancelButton.addEventListener("click", () => {
      editInput.value = formatDateTimeInput(item.watched_at);
      ratingSelect.value = initialRatingValue;
      editRow.classList.remove("is-visible");
    });

    saveButton.addEventListener("click", async () => {
      setMessage("history-message", "");
      const payload = {};
      const rawValue = editInput.value;
      const initialWatchValue = formatDateTimeInput(item.watched_at);
      if (rawValue !== initialWatchValue) {
        const watchedAt = parseDateTimeInput(rawValue);
        if (rawValue && !watchedAt) {
          setMessage("history-message", "Enter a valid watch date/time.", true);
          return;
        }
        payload.watched_at = watchedAt;
      }
      if (ratingSelect.value !== initialRatingValue) {
        payload.rating = ratingSelect.value
          ? Number(ratingSelect.value)
          : null;
      }
      if (!Object.keys(payload).length) {
        setMessage("history-message", "No changes to save.", true);
        return;
      }
      try {
        setMessage("history-message", "Saving...");
        await requestJSON(`/api/history/items/${item.id}`, {
          method: "PATCH",
          body: JSON.stringify(payload),
        });
        setMessage("history-message", "Update saved.");
        await loadHistory();
      } catch (error) {
        setMessage("history-message", error.message, true);
      }
    });

    deleteButton.addEventListener("click", async () => {
      closeHistoryMenus();
      const confirmed = window.confirm(
        `Delete "${item.title}" from your history?`
      );
      if (!confirmed) {
        return;
      }
      const deleteIntegrations = window.confirm(
        "Also delete this item from all connected integrations? " +
          "Click OK to remove it there too, or Cancel to delete locally only."
      );
      try {
        setMessage("history-message", "Deleting...");
        const url = deleteIntegrations
          ? `/api/history/items/${item.id}?delete_integrations=true`
          : `/api/history/items/${item.id}`;
        await requestJSON(url, {
          method: "DELETE",
        });
        setMessage("history-message", "Entry deleted.");
        await loadHistory();
      } catch (error) {
        setMessage("history-message", error.message, true);
      }
    });

    header.appendChild(title);
    header.appendChild(actions);
    meta.appendChild(header);
    meta.appendChild(detail);
    meta.appendChild(editRow);
    card.appendChild(selectWrap);
    card.appendChild(poster);
    card.appendChild(meta);
    container.appendChild(card);
  });
}

function buildRatingSelect(currentValue) {
  const select = document.createElement("select");
  select.className = "rating-select";
  select.setAttribute("aria-label", "Rating");
  const emptyOption = document.createElement("option");
  emptyOption.value = "";
  emptyOption.textContent = "No rating";
  select.appendChild(emptyOption);
  const options = [0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5];
  options.forEach((value) => {
    const option = document.createElement("option");
    option.value = String(value);
    option.textContent = value === 1 ? "1 star" : `${value} stars`;
    select.appendChild(option);
  });
  if (currentValue !== null && currentValue !== undefined) {
    select.value = String(currentValue);
  }
  return select;
}

async function loadIntegrationsForHistory() {
  try {
    const data = await requestJSON("/api/integrations");
    const integrations = data && data.integrations ? data.integrations : [];
    renderHistorySyncButtons(integrations);
  } catch (error) {
    console.error("failed to load integrations", error);
  }
}

window.librarysyncLoadHistory = loadHistory;

window.librarysyncPageInit = async ({ user }) => {
  if (!user) {
    return;
  }
  await Promise.all([loadHistory(), loadIntegrationsForHistory()]);
};
