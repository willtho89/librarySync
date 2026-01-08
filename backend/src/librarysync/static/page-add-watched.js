const lookupState = {
  id: null,
  timer: null,
  candidates: [],
  cache: new Map(),
  searchTimer: null,
  requestVersion: 0,
  lastQuery: "",
  lastScope: "all",
};
const episodeState = {
  tmdbId: null,
  seasonNumber: null,
};

function clearLookupTimer() {
  if (lookupState.timer) {
    window.clearTimeout(lookupState.timer);
    lookupState.timer = null;
  }
}

function clearLookupSearchTimer() {
  if (lookupState.searchTimer) {
    window.clearTimeout(lookupState.searchTimer);
    lookupState.searchTimer = null;
  }
}

function normalizeLookupQuery(query) {
  return query.trim().replace(/\s+/g, " ").toLowerCase();
}

function getLookupCacheKey(query, scope) {
  return `${normalizeLookupQuery(query)}::${scope}`;
}

function readLookupCache(cacheKey) {
  const entry = lookupState.cache.get(cacheKey);
  if (!entry) {
    return null;
  }
  const ageMs = Date.now() - entry.savedAt;
  return { ...entry, ageMs };
}

function writeLookupCache(cacheKey, candidates) {
  lookupState.cache.set(cacheKey, { candidates, savedAt: Date.now() });
}

function resetLookupUI() {
  const resultsCard = document.getElementById("lookup-results");
  const candidatesEl = document.getElementById("candidate-list");
  if (resultsCard) {
    resultsCard.hidden = true;
  }
  restoreConfirmPanel();
  if (candidatesEl) {
    candidatesEl.innerHTML = "";
  }
  lookupState.id = null;
  lookupState.candidates = [];
  lookupState.lastQuery = "";
  lookupState.lastScope = "all";
  resetEpisodePicker();
  setMessage("lookup-message", "");
  setMessage("confirm-message", "");
}

function getConfirmPanel() {
  return document.getElementById("confirm-panel");
}

function restoreConfirmPanel() {
  const panel = getConfirmPanel();
  const form = document.getElementById("confirm-form");
  if (!panel || !form) {
    return;
  }
  form.appendChild(panel);
  panel.setAttribute("hidden", "hidden");
}

function showConfirmPanel(panel) {
  panel.removeAttribute("hidden");
}

function moveConfirmPanelToCandidate(candidateId) {
  const panel = getConfirmPanel();
  if (!panel) {
    return;
  }
  const candidatesEl = document.getElementById("candidate-list");
  if (!candidatesEl) {
    restoreConfirmPanel();
    return;
  }
  const selectedInput =
    (candidateId &&
      candidatesEl.querySelector(
        `input[name='candidate_id'][value="${candidateId}"]`
      )) ||
    candidatesEl.querySelector("input[name='candidate_id']:checked");
  if (!selectedInput) {
    candidatesEl.appendChild(panel);
    showConfirmPanel(panel);
    return;
  }
  const selectedLabel = selectedInput.closest(".candidate-option");
  if (!selectedLabel) {
    candidatesEl.appendChild(panel);
    showConfirmPanel(panel);
    return;
  }
  selectedLabel.insertAdjacentElement("afterend", panel);
  showConfirmPanel(panel);
}

function resetEpisodePicker() {
  const picker = document.getElementById("episode-picker");
  const seasonSelect = document.getElementById("season-select");
  const episodeSelect = document.getElementById("episode-select");
  if (picker) {
    picker.hidden = true;
  }
  if (seasonSelect) {
    seasonSelect.innerHTML = "";
    seasonSelect.disabled = true;
  }
  if (episodeSelect) {
    episodeSelect.innerHTML = "";
    episodeSelect.disabled = true;
  }
  episodeState.tmdbId = null;
  episodeState.seasonNumber = null;
  setMessage("episode-message", "");
}

function scheduleLookup(query, searchScope) {
  clearLookupSearchTimer();
  const normalized = normalizeLookupQuery(query);
  if (!normalized) {
    resetLookupUI();
    return;
  }
  if (
    normalized === normalizeLookupQuery(lookupState.lastQuery) &&
    searchScope === lookupState.lastScope
  ) {
    return;
  }
  if (normalized.length < 3) {
    setMessage("lookup-message", "Keep typing to search.", false);
    return;
  }
  lookupState.searchTimer = window.setTimeout(() => {
    startLookup(query, searchScope, { force: false });
  }, 350);
}

function bindLookupAutoSearch() {
  const form = document.getElementById("lookup-form");
  if (!form) {
    return;
  }
  const queryInput = form.querySelector("input[name='query']");
  const scopeSelect = form.querySelector("select[name='search_scope']");
  if (!queryInput || !scopeSelect) {
    return;
  }
  queryInput.addEventListener("input", () => {
    scheduleLookup(queryInput.value, scopeSelect.value);
  });
  scopeSelect.addEventListener("change", () => {
    if (!queryInput.value.trim()) {
      return;
    }
    scheduleLookup(queryInput.value, scopeSelect.value);
  });
}

async function startLookup(query, searchScope, options = {}) {
  const { force = false } = options;
  const cacheKey = getLookupCacheKey(query, searchScope);
  const cached = readLookupCache(cacheKey);
  const cacheFresh = cached && cached.ageMs < 2 * 60 * 1000;

  clearLookupTimer();
  clearLookupSearchTimer();
  lookupState.lastQuery = query;
  lookupState.lastScope = searchScope;

  if (cached) {
    renderCandidates(cached.candidates || []);
    if (cacheFresh && !force) {
      setMessage("lookup-message", "");
      return;
    }
  } else {
    resetLookupUI();
  }

  try {
    setMessage("lookup-message", cached ? "Refreshing results..." : "Searching...");
    const requestVersion = lookupState.requestVersion + 1;
    lookupState.requestVersion = requestVersion;
    const response = await requestJSON("/api/metadata/lookup", {
      method: "POST",
      body: JSON.stringify({ query, search_scope: searchScope }),
    });
    lookupState.id = response.lookup_id;
    await pollLookupStatus(response.lookup_id, requestVersion, cacheKey);
  } catch (error) {
    setMessage("lookup-message", error.message, true);
  }
}

async function handleLookupSubmit(data) {
  const query = (data.get("query") || "").trim();
  const scopeRaw = (data.get("search_scope") || "all").toLowerCase();
  const searchScope = ["all", "movie", "tv", "anime"].includes(scopeRaw)
    ? scopeRaw
    : "all";
  if (!query) {
    setMessage("lookup-message", "Enter a title or ID to search.", true);
    return;
  }
  await startLookup(query, searchScope, { force: true });
}

async function pollLookupStatus(lookupId, requestVersion, cacheKey) {
  try {
    const data = await requestJSON(`/api/metadata/lookup/${lookupId}`);
    if (lookupState.requestVersion !== requestVersion || lookupState.id !== lookupId) {
      return;
    }
    const candidates = data.candidates || [];
    if (data.status === "completed") {
      renderCandidates(candidates);
      writeLookupCache(cacheKey, candidates);
      return;
    }
    if (data.status === "failed") {
      setMessage("lookup-message", data.error || "Lookup failed.", true);
      return;
    }
    if (candidates.length) {
      renderCandidates(candidates);
      writeLookupCache(cacheKey, candidates);
    }
    lookupState.timer = window.setTimeout(
      () => pollLookupStatus(lookupId, requestVersion, cacheKey),
      1200,
    );
  } catch (error) {
    setMessage("lookup-message", error.message, true);
  }
}

function renderCandidates(candidates) {
  const resultsCard = document.getElementById("lookup-results");
  const candidatesEl = document.getElementById("candidate-list");
  if (!resultsCard || !candidatesEl) {
    return;
  }
  const selectedId = getSelectedCandidateId();
  restoreConfirmPanel();
  lookupState.candidates = candidates;
  candidatesEl.innerHTML = "";
  if (!candidates.length) {
    candidatesEl.textContent = "No matches found.";
    restoreConfirmPanel();
    resetEpisodePicker();
  } else {
    candidates.forEach((candidate, index) => {
      const label = document.createElement("label");
      label.className = "candidate-option";
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "candidate_id";
      input.value = candidate.id;
      if (selectedId && candidate.id === selectedId) {
        input.checked = true;
      } else if (!selectedId && index === 0) {
        input.checked = true;
      }

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
      title.textContent = candidate.title;
      const detail = document.createElement("p");
      const year = candidate.year ? candidate.year : "Year unknown";
      const mediaType = formatMediaType(candidate.media_type);
      const detailParts = [`${year}`, mediaType];
      detail.textContent = detailParts.join(" · ");

      meta.appendChild(title);
      meta.appendChild(detail);
      label.appendChild(input);
      label.appendChild(poster);
      label.appendChild(meta);
      candidatesEl.appendChild(label);
    });
    if (selectedId && !getSelectedCandidateId()) {
      const firstInput = candidatesEl.querySelector("input[name='candidate_id']");
      if (firstInput) {
        firstInput.checked = true;
      }
    }
    moveConfirmPanelToCandidate(getSelectedCandidateId());
    handleCandidateSelection();
  }
  resultsCard.hidden = false;
  setMessage("lookup-message", "");
}

function getSelectedCandidate() {
  const candidateId = getSelectedCandidateId();
  if (!candidateId) {
    return null;
  }
  return lookupState.candidates.find((entry) => entry.id === candidateId) || null;
}

function getSelectedCandidateId() {
  const input = document.querySelector("input[name='candidate_id']:checked");
  return input ? input.value : null;
}

async function handleCandidateSelection() {
  const candidate = getSelectedCandidate();
  moveConfirmPanelToCandidate(getSelectedCandidateId());
  if (!candidate || candidate.media_type !== "tv") {
    resetEpisodePicker();
    return;
  }
  const tmdbId = candidate.tmdb_id;
  if (!tmdbId) {
    resetEpisodePicker();
    const picker = document.getElementById("episode-picker");
    if (picker) {
      picker.hidden = false;
    }
    setMessage(
      "episode-message",
      "Episode lookup requires a TMDB-backed result. Try another match.",
      true,
    );
    return;
  }
  await loadSeasons(tmdbId);
}

async function loadSeasons(tmdbId) {
  const picker = document.getElementById("episode-picker");
  const seasonSelect = document.getElementById("season-select");
  const episodeSelect = document.getElementById("episode-select");
  if (!picker || !seasonSelect || !episodeSelect) {
    return;
  }
  picker.hidden = false;
  seasonSelect.disabled = true;
  episodeSelect.disabled = true;
  episodeSelect.innerHTML = "";
  setMessage("episode-message", "Loading seasons...");
  episodeState.tmdbId = tmdbId;
  try {
    const seasons = await requestJSON(`/api/metadata/tv/tmdb/${tmdbId}/seasons`);
    if (episodeState.tmdbId !== tmdbId) {
      return;
    }
    const sorted = (seasons || [])
      .slice()
      .sort((a, b) => a.season_number - b.season_number);
    seasonSelect.innerHTML = "";
    if (!sorted.length) {
      setMessage("episode-message", "No seasons found.", true);
      seasonSelect.disabled = true;
      return;
    }
    sorted.forEach((season) => {
      const option = document.createElement("option");
      option.value = String(season.season_number);
      const label = season.name || `Season ${season.season_number}`;
      const count = season.episode_count ? ` · ${season.episode_count} eps` : "";
      option.textContent = `${label}${count}`;
      seasonSelect.appendChild(option);
    });
    const defaultSeason = sorted[sorted.length - 1];
    seasonSelect.value = String(defaultSeason.season_number);
    seasonSelect.disabled = false;
    episodeState.seasonNumber = defaultSeason.season_number;
    setMessage("episode-message", "");
    await loadEpisodes(tmdbId, defaultSeason.season_number);
  } catch (error) {
    setMessage("episode-message", error.message, true);
    seasonSelect.disabled = true;
    episodeSelect.disabled = true;
  }
}

async function loadEpisodes(tmdbId, seasonNumber) {
  const episodeSelect = document.getElementById("episode-select");
  if (!episodeSelect) {
    return;
  }
  episodeSelect.disabled = true;
  episodeSelect.innerHTML = "";
  setMessage("episode-message", "Loading episodes...");
  episodeState.seasonNumber = seasonNumber;
  try {
    const episodes = await requestJSON(
      `/api/metadata/tv/tmdb/${tmdbId}/seasons/${seasonNumber}/episodes`,
    );
    if (episodeState.tmdbId !== tmdbId || episodeState.seasonNumber !== seasonNumber) {
      return;
    }
    const sorted = (episodes || [])
      .slice()
      .sort((a, b) => a.episode_number - b.episode_number);
    episodeSelect.innerHTML = "";
    if (!sorted.length) {
      setMessage("episode-message", "No episodes found.", true);
      episodeSelect.disabled = true;
      return;
    }
    sorted.forEach((episode) => {
      const option = document.createElement("option");
      option.value = String(episode.episode_number);
      option.dataset.episodeTitle = episode.title || "";
      option.dataset.episodeTmdbId = episode.tmdb_id || "";
      const label = `E${String(episode.episode_number).padStart(2, "0")}`;
      option.textContent = episode.title ? `${label} · ${episode.title}` : label;
      episodeSelect.appendChild(option);
    });
    episodeSelect.disabled = false;
    setMessage("episode-message", "");
  } catch (error) {
    setMessage("episode-message", error.message, true);
    episodeSelect.disabled = true;
  }
}

function getEpisodeSelection() {
  const seasonSelect = document.getElementById("season-select");
  const episodeSelect = document.getElementById("episode-select");
  if (!seasonSelect || !episodeSelect) {
    return null;
  }
  if (seasonSelect.disabled || episodeSelect.disabled) {
    return null;
  }
  const seasonNumber = Number(seasonSelect.value);
  const episodeNumber = Number(episodeSelect.value);
  if (Number.isNaN(seasonNumber) || Number.isNaN(episodeNumber)) {
    return null;
  }
  const selectedOption = episodeSelect.selectedOptions[0];
  return {
    seasonNumber,
    episodeNumber,
    title: selectedOption ? selectedOption.dataset.episodeTitle : "",
    tmdbId: selectedOption ? selectedOption.dataset.episodeTmdbId : "",
  };
}

function clearConfirmInputs() {
  const form = document.getElementById("confirm-form");
  if (!form) {
    return;
  }
  const watchedInput = form.querySelector("input[name='watched_at']");
  if (watchedInput) {
    watchedInput.value = "";
  }
  form.querySelectorAll(".rating-stars input[type='radio']").forEach((input) => {
    input.checked = false;
  });
}

async function handleLookupConfirm(data) {
  setMessage("confirm-message", "");
  if (!lookupState.candidates.length) {
    setMessage("confirm-message", "Search for a title first.", true);
    return;
  }
  const candidateId = data.get("candidate_id");
  if (!candidateId) {
    setMessage("confirm-message", "Select a result to confirm.", true);
    return;
  }
  const candidate = lookupState.candidates.find((entry) => entry.id === candidateId);
  if (!candidate) {
    setMessage("confirm-message", "Selected result is no longer available.", true);
    return;
  }
  const watchedRaw = (data.get("watched_at") || "").trim();
  const payload = buildHistoryPayload(candidate);
  if (!hasHistoryIds(payload)) {
    setMessage("confirm-message", "Selected result is missing external IDs.", true);
    return;
  }
  if (candidate.media_type === "tv") {
    const episodeSelection = getEpisodeSelection();
    if (!episodeSelection) {
      setMessage("confirm-message", "Select a season and episode first.", true);
      return;
    }
    payload.season_number = episodeSelection.seasonNumber;
    payload.episode_number = episodeSelection.episodeNumber;
    if (episodeSelection.title) {
      payload.episode_title = episodeSelection.title;
    }
    if (episodeSelection.tmdbId) {
      payload.episode_tmdb_id = episodeSelection.tmdbId;
    }
  }
  if (watchedRaw) {
    const watchedDate = new Date(watchedRaw);
    if (!Number.isNaN(watchedDate.valueOf())) {
      payload.watched_at = watchedDate.toISOString();
    }
  }
  const ratingValue = parseRatingValue(data.get("rating"));
  if (ratingValue !== null) {
    payload.rating = ratingValue;
  }
  try {
    await requestJSON("/api/history/items", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setMessage("confirm-message", "Watched saved.");
    clearConfirmInputs();
    if (typeof window.librarysyncLoadHistory === "function") {
      await window.librarysyncLoadHistory();
    }
  } catch (error) {
    setMessage("confirm-message", error.message, true);
  }
}

function buildHistoryPayload(candidate) {
  const payload = {
    media_type: candidate.media_type || "movie",
  };
  if (candidate.imdb_id) {
    payload.imdb_id = candidate.imdb_id;
  }
  if (candidate.tmdb_id) {
    payload.tmdb_id = candidate.tmdb_id;
  }
  if (candidate.tvdb_id) {
    payload.tvdb_id = candidate.tvdb_id;
  }
  if (candidate.tvmaze_id) {
    payload.tvmaze_id = candidate.tvmaze_id;
  }
  if (candidate.kitsu_id) {
    payload.kitsu_id = candidate.kitsu_id;
  }
  if (candidate.myanimelist_id) {
    payload.myanimelist_id = candidate.myanimelist_id;
  }
  if (candidate.anilist_id) {
    payload.anilist_id = candidate.anilist_id;
  }
  if (candidate.title) {
    payload.title = candidate.title;
  }
  if (candidate.year) {
    payload.year = candidate.year;
  }
  if (candidate.poster_url) {
    payload.poster_url = candidate.poster_url;
  }
  return payload;
}

function hasHistoryIds(payload) {
  return Boolean(
    payload.imdb_id ||
      payload.tmdb_id ||
      payload.tvdb_id ||
      payload.tvmaze_id ||
      payload.kitsu_id ||
      payload.myanimelist_id ||
      payload.anilist_id,
  );
}

function bindRatingClearControls() {
  document.querySelectorAll("[data-rating-clear]").forEach((button) => {
    button.addEventListener("click", () => {
      const field = button.closest(".field");
      const group = field ? field.querySelector(".rating-stars") : null;
      if (!group) {
        return;
      }
      group.querySelectorAll("input[type='radio']").forEach((input) => {
        input.checked = false;
      });
    });
  });
}

window.librarysyncPageInit = () => {
  bindForm("lookup-form", handleLookupSubmit);
  bindForm("confirm-form", handleLookupConfirm);
  bindRatingClearControls();
  bindLookupAutoSearch();

  const candidateList = document.getElementById("candidate-list");
  if (candidateList) {
    candidateList.addEventListener("change", (event) => {
      if (event.target && event.target.name === "candidate_id") {
        handleCandidateSelection();
      }
    });
  }

  const seasonSelect = document.getElementById("season-select");
  if (seasonSelect) {
    seasonSelect.addEventListener("change", () => {
      const seasonNumber = Number(seasonSelect.value);
      if (Number.isNaN(seasonNumber) || !episodeState.tmdbId) {
        return;
      }
      loadEpisodes(episodeState.tmdbId, seasonNumber);
    });
  }
};
