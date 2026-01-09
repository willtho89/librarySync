const watchlistState = {
  page: 1,
  pageSize: 50,
  total: 0,
  filters: {
    status: "active",
    mediaType: "all",
    watchedDisplay: "overlay", // hide, overlay, show
  },
};

const watchlistSelectionState = {
  items: [],
  selectedIds: new Set(),
};

function buildWatchlistQueryParams() {
  const params = new URLSearchParams();
  params.set("limit", String(watchlistState.pageSize));
  params.set("offset", String((watchlistState.page - 1) * watchlistState.pageSize));
  
  // Logic for Status + Watched Display
  if (watchlistState.filters.status === "all") {
      // All means Active + Waiting + (Watched depending on display)
      // We exclude "removed" by default in "all" unless requested explicitly, but user removed "removed" option.
      const statuses = ["active", "waiting"];
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
  const total = watchlistState.total;
  const totalPages = getWatchlistTotalPages(total);
  const label = total
    ? `Page ${watchlistState.page} of ${totalPages} · ${total} items`
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
    container.textContent = "No watchlist items.";
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
    const statusLabel = item.status.toUpperCase();
    const detailParts = [year, mediaType, `Status: ${statusLabel}`];
    
    if (item.release_date) {
        detailParts.push(`Release: ${item.release_date}`);
    }
    if (item.first_air_date) {
        detailParts.push(`First Air: ${item.first_air_date}`);
    }
    
    // Add badges for status
    let badgeClass = "badge-neutral";
    if (item.status === "active") badgeClass = "badge-success"; // Unwatched / Partial
    if (item.status === "waiting") badgeClass = "badge-warning"; // Caught up
    if (item.status === "watched") {
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
        if (item.status === "active") detailParts.push("Unwatched");
        if (item.status === "watched") detailParts.push("Watched");
    }
    
    detail.textContent = detailParts.join(" · ");
    
    const header = document.createElement("div");
    header.className = "history-header";
    
    const actions = document.createElement("div");
    actions.className = "history-actions";
    
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

    const metadataButton = document.createElement("button");
    metadataButton.type = "button";
    metadataButton.textContent = "View metadata";
    metadataButton.setAttribute("role", "menuitem");
    metadataButton.addEventListener("click", () => {
        closeWatchlistMenus();
        // Since item might not have all fields populated like history items
        // We might need to construct a compatible object or just pass basic metadata if we had it
        // The current item structure is WatchlistItemOut + Media details
        // We'll create a fake "history item" structure for openMetadataModal if needed, or update openMetadataModal
        // Actually page-history.js functions are available globally now if loaded in base.html or here.
        // openMetadataModal expects item.metadata.
        // Our WatchlistItemOut doesn't include 'metadata' structure fully.
        // For now, let's just alert or skip metadata until we update the API to return full metadata object if needed.
        // Wait, 'page-history.js' is loaded in 'watchlist.html' now.
        // But 'openMetadataModal' expects specific structure.
        // Let's implement a simplified metadata view or just show raw data for now?
        // Actually, we can just skip this button for now or implement a basic alert with IDs.
        alert(`External IDs:\nIMDb: ${item.imdb_id || 'N/A'}\nTMDB: ${item.tmdb_id || 'N/A'}\nTVDB: ${item.tvdb_id || 'N/A'}`);
    });

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
    menuPanel.appendChild(deleteButton);
    
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

function bindWatchlistUi() {
    const statusSelect = document.getElementById("watchlist-status-filter");
    const watchedDisplaySelect = document.getElementById("watchlist-watched-display");
    
    function updateControls() {
        if (statusSelect && watchedDisplaySelect) {
            // "Watched items" selector only relevant if status is "All" (or includes watched explicitly)
            // If user selects "Watched" explicitly, we disable the display selector (force show?)
            // Or if user selects "Active", we disable it (none to show).
            if (statusSelect.value === "all") {
                watchedDisplaySelect.disabled = false;
            } else {
                watchedDisplaySelect.disabled = true;
            }
        }
    }

    if (statusSelect) {
        statusSelect.value = watchlistState.filters.status;
        statusSelect.addEventListener("change", () => {
            watchlistState.filters.status = statusSelect.value;
            watchlistState.page = 1;
            updateControls();
            loadWatchlist();
        });
    }
    
    if (watchedDisplaySelect) {
        watchedDisplaySelect.value = watchlistState.filters.watchedDisplay;
        watchedDisplaySelect.addEventListener("change", () => {
            watchlistState.filters.watchedDisplay = watchedDisplaySelect.value;
            watchlistState.page = 1;
            loadWatchlist();
        });
    }
    
    // Init state
    updateControls();
    
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
            
            // We need a bulk delete endpoint really, but for now loop?
            // Actually, best to add bulk delete endpoint.
            // Checklist didn't strictly specify bulk delete endpoint but "DELETE /api/watchlist/items/{id}".
            // We can do parallel requests or add the endpoint. 
            // For robustness, let's just do parallel requests for now as v1 scope is small.
            
            try {
                // Ideally this should be a bulk endpoint
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
}

window.librarysyncPageInit = async ({ user }) => {
  if (!user) {
    return;
  }
  bindWatchlistUi();
  await loadWatchlist();
};
