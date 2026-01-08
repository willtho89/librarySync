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

window.librarysyncPageInit = async ({ user }) => {
  if (!user) {
    return;
  }
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
};
