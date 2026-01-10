const watchlistState = {
  page: 1,
  pageSize: 50,
  total: 0,
  filters: {
    status: "all",
    mediaType: "all",
    source: "all",
    search: "",
    watchedDisplay: "overlay", // hide, overlay, show
  },
  searchTimer: null,
};

const watchlistSelectionState = {
  items: [],
  selectedIds: new Set(),
};

const WATCHLIST_PROVIDER_LABELS = {
  trakt: "Trakt",
  letterboxd: "Letterboxd",
  manual: "Manual",
};

const WATCHLIST_STATUS_LABELS = {
  added: "Added",
  in_progress: "In progress",
  watched: "Watched",
  not_released: "Not released yet",
  active: "Added",
  waiting: "Watched",
};

function formatWatchlistSourceLabel(source) {
  if (!source) return "Unknown";
  const provider =
    WATCHLIST_PROVIDER_LABELS[source.provider] || source.provider || "Unknown";
  if (source.provider === "manual") return provider;
  if (source.name) {
    const name = source.name.replace(/-/g, " ");
    return `${provider}: ${name}`;
  }
  return `${provider} watchlist`;
}

function formatWatchlistSources(sources) {
  if (!Array.isArray(sources) || sources.length === 0) return [];
  return sources.map((source) => formatWatchlistSourceLabel(source)).filter(Boolean);
}

function watchlistHasActiveFilters() {
  return (
    (watchlistState.filters.search && watchlistState.filters.search.trim()) ||
    watchlistState.filters.status !== "all" ||
    watchlistState.filters.mediaType !== "all" ||
    watchlistState.filters.source !== "all"
  );
}

function buildWatchlistQueryParams() {
  const params = new URLSearchParams();
  params.set("limit", String(watchlistState.pageSize));
  params.set("offset", String((watchlistState.page - 1) * watchlistState.pageSize));
  
  // Logic for Status + Watched Display
  if (watchlistState.filters.status === "all") {
      // All means Added + In Progress + Not Released + (Watched depending on display)
      // Include legacy statuses to backfill existing items.
      const statuses = ["added", "in_progress", "not_released", "active", "waiting"];
      if (watchlistState.filters.watchedDisplay !== "hide") {
          statuses.push("watched");
      }
      params.set("status", statuses.join(","));
  } else {
      // Specific status selected
      params.set("status", watchlistState.filters.status);
  }

  if (watchlistState.filters.mediaType !== "all") {
    params.set("media_type", watchlistState.filters.mediaType);
  }

  if (watchlistState.filters.source !== "all") {
    params.set("source", watchlistState.filters.source);
  }

  if (watchlistState.filters.search && watchlistState.filters.search.trim()) {
    params.set("search", watchlistState.filters.search.trim());
  }

  return params;
}

function getWatchlistTotalPages(total) {
  const value = Math.ceil(total / watchlistState.pageSize);
  return Math.max(1, value);
}

function updateWatchlistPagination() {
  const info = document.getElementById("watchlist-page-info");
  const prev = document.getElementById("watchlist-page-prev");
  const next = document.getElementById("watchlist-page-next");
  const clearFilters = document.getElementById("watchlist-filters-clear");
  const total = watchlistState.total;
  const totalPages = getWatchlistTotalPages(total);
  const label = total
    ? `Page ${watchlistState.page} of ${totalPages} · ${total} items`
    : watchlistHasActiveFilters()
      ? "No matches for your filters."
      : "No items found.";
  if (info) {
    info.textContent = label;
  }
  if (prev) {
    prev.disabled = watchlistState.page <= 1;
  }
  if (next) {
    next.disabled = watchlistState.page >= totalPages;
  }
  if (clearFilters) {
    clearFilters.disabled = !watchlistHasActiveFilters();
  }
}

function updateWatchlistBulkControls() {
  const selectAll = document.getElementById("watchlist-select-all");
  const deleteButton = document.getElementById("watchlist-delete-selected");
  const bulkBar = document.querySelector("[data-bulk-bar]");
  const total = watchlistSelectionState.items.length;
  const selectedCount = watchlistSelectionState.selectedIds.size;

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
      ? `Remove selected (${selectedCount})`
      : "Remove selected";
  }

  if (bulkBar) {
    if (selectedCount > 0) {
      bulkBar.classList.add("is-visible");
    } else {
      bulkBar.classList.remove("is-visible");
    }
  }
}

function resetWatchlistSelection(items) {
  watchlistSelectionState.items = items;
  watchlistSelectionState.selectedIds.clear();
  updateWatchlistBulkControls();
}

function closeWatchlistMenus() {
  document.querySelectorAll("[data-menu-panel].is-open").forEach((panel) => {
    panel.classList.remove("is-open");
  });
  document.querySelectorAll("[data-menu-button]").forEach((button) => {
    button.setAttribute("aria-expanded", "false");
  });
}

async function loadWatchlist() {
  const container = document.getElementById("watchlist-list");
  if (!container) {
    return;
  }
  bindWatchlistUi();
  container.textContent = "Loading...";
  const params = buildWatchlistQueryParams();
  let data = null;
  try {
    data = await requestJSON(`/api/watchlist/items?${params.toString()}`);
  } catch (error) {
    container.textContent = "Unable to load watchlist.";
    return;
  }
  const items = data && data.items ? data.items : [];
  const total = data && typeof data.total === "number" ? data.total : items.length;
  watchlistState.total = total;
  
  const totalPages = getWatchlistTotalPages(total);
  if (watchlistState.page > totalPages && totalPages > 0) {
    watchlistState.page = totalPages;
    await loadWatchlist();
    return;
  }
  
  updateWatchlistPagination();
  resetWatchlistSelection(items);
  
  if (!items.length) {
    container.textContent = watchlistHasActiveFilters()
      ? "No watchlist items match your filters."
      : "No watchlist items.";
    return;
  }
  
  container.innerHTML = "";
  items.forEach((item) => {
    const card = document.createElement("div");
    card.className = "history-card"; // Reuse history card style
    
    const selectWrap = document.createElement("label");
    selectWrap.className = "history-select";
    const selectInput = document.createElement("input");
    selectInput.type = "checkbox";
    selectInput.value = item.id;
    selectInput.setAttribute("aria-label", `Select ${item.title}`);
    selectInput.setAttribute("data-watchlist-select", "true");
    selectInput.addEventListener("change", () => {
      if (selectInput.checked) {
        watchlistSelectionState.selectedIds.add(item.id);
      } else {
        watchlistSelectionState.selectedIds.delete(item.id);
      }
      updateWatchlistBulkControls();
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
    const year = item.year ? item.year : "Year unknown";
    const mediaType = formatMediaType(item.media_type);
    const statusLabel = WATCHLIST_STATUS_LABELS[item.status] || item.status;
    const detailParts = [year, mediaType, `Status: ${statusLabel}`];
    
    if (item.release_date) {
        detailParts.push(`Release: ${item.release_date}`);
    }
    if (item.first_air_date) {
        detailParts.push(`First Air: ${item.first_air_date}`);
    }
    
    // Add badges for status
    let badgeClass = "badge-neutral";
    if (item.status === "added") badgeClass = "badge-success";
    if (item.status === "in_progress") badgeClass = "badge-warning";
    if (item.status === "not_released") badgeClass = "badge-neutral";
    if (item.status === "watched" || item.status === "waiting") {
        badgeClass = "badge-neutral";
        if (watchlistState.filters.watchedDisplay === "overlay") {
            card.classList.add("is-watched"); // For grey overlay
        }
    }
    
    // Progress for shows
    if (item.progress && item.media_type === "tv") {
        const { watched, total } = item.progress;
        if (total > 0) {
            const pct = Math.round((watched / total) * 100);
            detailParts.push(`Progress: ${watched}/${total} (${pct}%)`);
            if (watched === 0) {
                detailParts.push("Unwatched");
            } else if (watched < total) {
                detailParts.push("Partial");
            } else {
                detailParts.push("Caught Up");
            }
        }
    } else if (item.media_type === "movie") {
        if (item.status === "added") detailParts.push("Unwatched");
        if (item.status === "watched") detailParts.push("Watched");
    }

    const sourceLabels = formatWatchlistSources(item.sources);
    if (sourceLabels.length) {
        detailParts.push(`Sources: ${sourceLabels.join(", ")}`);
    }
    
    detail.textContent = detailParts.join(" · ");
    
    const header = document.createElement("div");
    header.className = "history-header";
    
    const actions = document.createElement("div");
    actions.className = "history-actions";

    const pill = document.createElement("span");
    pill.className = "watchlist-pill";
    pill.textContent = item.media_type === "tv" ? "Caught up" : "Watched";

    // --- Menu ---
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

    // Add "Mark watched" button
    let showMarkWatched = true;
    let markWatchedLabel = "Mark watched";
    
    if (item.media_type === "tv") {
        if (item.status === "watched" || item.status === "waiting") {
            showMarkWatched = false; // Already fully watched
        } else if (item.progress && item.progress.watched >= item.progress.total && item.progress.total > 0) {
            showMarkWatched = false;
        } else {
            markWatchedLabel = "Mark next episode watched";
        }
    } else {
        // Movies
        if (item.status === "watched") {
            markWatchedLabel = "Mark rewatched";
        }
    }

    if (showMarkWatched) {
        const markWatchedButton = document.createElement("button");
        markWatchedButton.type = "button";
        markWatchedButton.textContent = markWatchedLabel;
        markWatchedButton.setAttribute("role", "menuitem");
        markWatchedButton.addEventListener("click", async () => {
            closeWatchlistMenus();
            try {
                const res = await requestJSON(`/api/watchlist/items/${item.id}/mark-watched`, {
                    method: "POST",
                });
                let msg = "Marked as watched.";
                if (res.added_episode) {
                    msg = `Marked ${res.added_episode} as watched.`;
                }
                // Show a toast or just reload
                // Using alert for now or simple reload
                // Ideally a toast, but we have setMessage on history page, here we have no message container?
                // Actually base template might not have one globally.
                // We'll just reload the list to reflect status changes.
                await loadWatchlist();
            } catch (e) {
                alert(e.message);
            }
        });
        menuPanel.appendChild(markWatchedButton);
    }

    const metadataButton = document.createElement("button");
    metadataButton.type = "button";
    metadataButton.textContent = "View metadata";
    metadataButton.setAttribute("role", "menuitem");
    metadataButton.addEventListener("click", () => {
        closeWatchlistMenus();
        // Construct metadata object for openMetadataModal
        const modalItem = {
            id: item.id,
            title: item.title,
            season_number: null,
            episode_number: null,
            imdb_id: item.imdb_id,
            tmdb_id: item.tmdb_id,
            tvdb_id: item.tvdb_id,
            metadata: {
                media_item_id: item.media_item_id,
                ids: {
                    imdb_id: item.imdb_id,
                    tmdb_id: item.tmdb_id,
                    tvdb_id: item.tvdb_id,
                },
                watched_created_at: item.created_at,
                media_created_at: null, // Not available
                media_updated_at: null, // Not available
                first_sync_at: null,
                last_sync_at: null
            }
        };
        openMetadataModal(modalItem);
    });

    const externalLinks = buildExternalMenuLinks(item);

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "danger-button";
    deleteButton.textContent = "Remove";
    deleteButton.setAttribute("role", "menuitem");
    deleteButton.addEventListener("click", async () => {
        closeWatchlistMenus();
        if (!confirm(`Remove "${item.title}" from watchlist?`)) return;
        try {
            await requestJSON(`/api/watchlist/items/${item.id}`, { method: "DELETE" });
            await loadWatchlist();
        } catch(e) {
            alert(e.message);
        }
    });

    menuPanel.appendChild(metadataButton);
    externalLinks.forEach((link) => menuPanel.appendChild(link));
    menuPanel.appendChild(deleteButton);
    
    if (card.classList.contains("is-watched")) {
      actions.appendChild(pill);
    }
    actions.appendChild(menuButton);
    actions.appendChild(menuPanel);

    menuButton.addEventListener("click", (event) => {
      event.stopPropagation();
      const isOpen = menuPanel.classList.contains("is-open");
      closeWatchlistMenus();
      if (!isOpen) {
        menuPanel.classList.add("is-open");
        menuButton.setAttribute("aria-expanded", "true");
      }
    });

    // --- End Menu ---
    
    header.appendChild(title);
    header.appendChild(actions);
    meta.appendChild(header);
    meta.appendChild(detail);
    
    card.appendChild(selectWrap);
    card.appendChild(poster);
    card.appendChild(meta);
    container.appendChild(card);
  });
}

let watchlistUiBound = false;

function bindWatchlistUi() {
    if (watchlistUiBound) {
        return;
    }

    const modal = document.getElementById("metadata-modal");
    if (modal) {
        modal.querySelectorAll("[data-modal-close]").forEach((button) => {
            button.addEventListener("click", closeMetadataModal);
        });
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                closeMetadataModal();
            }
        });
    }

    const searchInput = document.getElementById("watchlist-search");
    if (searchInput) {
        searchInput.value = watchlistState.filters.search;
        searchInput.addEventListener("input", () => {
            watchlistState.filters.search = searchInput.value || "";
            watchlistState.page = 1;
            if (watchlistState.searchTimer) {
                window.clearTimeout(watchlistState.searchTimer);
            }
            watchlistState.searchTimer = window.setTimeout(() => {
                loadWatchlist();
            }, 250);
        });
    }

    const statusSelect = document.getElementById("watchlist-status-filter");
    if (statusSelect) {
        statusSelect.value = watchlistState.filters.status;
        statusSelect.addEventListener("change", () => {
            watchlistState.filters.status = statusSelect.value;
            watchlistState.page = 1;
            loadWatchlist();
        });
    }
    
    const sourceSelect = document.getElementById("watchlist-source-filter");
    if (sourceSelect) {
        sourceSelect.value = watchlistState.filters.source;
        sourceSelect.addEventListener("change", () => {
            watchlistState.filters.source = sourceSelect.value || "all";
            watchlistState.page = 1;
            loadWatchlist();
        });
    }

    const watchedDisplaySelect = document.getElementById("watchlist-watched-display");
    if (watchedDisplaySelect) {
        watchedDisplaySelect.value = watchlistState.filters.watchedDisplay;
        watchedDisplaySelect.addEventListener("change", () => {
            watchlistState.filters.watchedDisplay = watchedDisplaySelect.value;
            watchlistState.page = 1;
            loadWatchlist();
        });
    }
    
    const typeSelect = document.getElementById("watchlist-type-filter");
    if (typeSelect) {
        typeSelect.value = watchlistState.filters.mediaType;
        typeSelect.addEventListener("change", () => {
            watchlistState.filters.mediaType = typeSelect.value;
            watchlistState.page = 1;
            loadWatchlist();
        });
    }
    
    const pageSizeSelect = document.getElementById("watchlist-page-size");
    if (pageSizeSelect) {
        pageSizeSelect.value = String(watchlistState.pageSize);
        pageSizeSelect.addEventListener("change", () => {
            watchlistState.pageSize = Number(pageSizeSelect.value);
            watchlistState.page = 1;
            loadWatchlist();
        });
    }
    
    const prevButton = document.getElementById("watchlist-page-prev");
    if (prevButton) {
        prevButton.addEventListener("click", () => {
            if (watchlistState.page > 1) {
                watchlistState.page--;
                loadWatchlist();
            }
        });
    }
    
    const nextButton = document.getElementById("watchlist-page-next");
    if (nextButton) {
        nextButton.addEventListener("click", () => {
            const totalPages = getWatchlistTotalPages(watchlistState.total);
            if (watchlistState.page < totalPages) {
                watchlistState.page++;
                loadWatchlist();
            }
        });
    }

    const clearFilters = document.getElementById("watchlist-filters-clear");
    if (clearFilters) {
        clearFilters.addEventListener("click", () => {
            watchlistState.filters.search = "";
            watchlistState.filters.status = "all";
            watchlistState.filters.source = "all";
            watchlistState.filters.mediaType = "all";
            watchlistState.page = 1;

            if (searchInput) searchInput.value = "";
            if (statusSelect) statusSelect.value = "all";
            if (sourceSelect) sourceSelect.value = "all";
            if (typeSelect) typeSelect.value = "all";
            // Don't reset watchedDisplay as it's more of a view preference?
            // History clears everything. Let's clear everything but maybe watchedDisplay?
            // Actually history clears "Type" and "Source".
            
            loadWatchlist();
        });
    }

    const selectAll = document.getElementById("watchlist-select-all");
    if (selectAll) {
        selectAll.addEventListener("change", () => {
            const shouldSelect = selectAll.checked;
            watchlistSelectionState.selectedIds.clear();
            document.querySelectorAll("input[data-watchlist-select]").forEach((input) => {
                input.checked = shouldSelect;
                if (shouldSelect) {
                    watchlistSelectionState.selectedIds.add(input.value);
                }
            });
            updateWatchlistBulkControls();
        });
    }

    const deleteSelected = document.getElementById("watchlist-delete-selected");
    if (deleteSelected) {
        deleteSelected.addEventListener("click", async () => {
            const selectedIds = Array.from(watchlistSelectionState.selectedIds);
            if (!selectedIds.length) return;
            
            if (!confirm(`Remove ${selectedIds.length} items from watchlist?`)) return;
            
            try {
                await Promise.all(selectedIds.map(id => 
                    requestJSON(`/api/watchlist/items/${id}`, { method: "DELETE" })
                ));
                await loadWatchlist();
            } catch (error) {
                alert("Some items failed to delete: " + error.message);
                await loadWatchlist();
            }
        });
    }

    document.addEventListener("click", (event) => {
        if (event.target && event.target.closest(".history-actions")) {
            return;
        }
        closeWatchlistMenus();
    });

    watchlistUiBound = true;
}

window.librarysyncPageInit = async ({ user }) => {
  if (!user) {
    return;
  }
  // No need to load integrations for sync buttons as watchlist doesn't have them yet
  await loadWatchlist();
};
