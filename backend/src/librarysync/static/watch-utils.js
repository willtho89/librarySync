function formatProviderLabel(value) {
  if (!value) {
    return "";
  }
  if (Array.isArray(value)) {
    const labels = value.map((entry) => formatProviderLabel(entry)).filter(Boolean);
    return labels.join(" + ");
  }
  const normalized = value.toLowerCase();
  if (normalized === "tmdb") {
    return "TMDB";
  }
  if (normalized === "tvdb") {
    return "TVDB";
  }
  if (normalized === "tvmaze") {
    return "TVMaze";
  }
  if (normalized === "imdb") {
    return "IMDb";
  }
  if (normalized === "kitsu") {
    return "Kitsu";
  }
  if (normalized === "myanimelist") {
    return "MyAnimeList";
  }
  if (normalized === "local") {
    return "";
  }
  return normalized.toUpperCase();
}

function formatMediaType(value) {
  if (value === "tv") {
    return "TV";
  }
  if (value === "anime") {
    return "Anime";
  }
  return "Movie";
}

function formatSeasonEpisode(seasonNumber, episodeNumber) {
  if (seasonNumber === null || seasonNumber === undefined) {
    return "";
  }
  if (episodeNumber === null || episodeNumber === undefined) {
    return "";
  }
  const pad = (value) => String(value).padStart(2, "0");
  return `S${pad(seasonNumber)}E${pad(episodeNumber)}`;
}

function formatRating(value) {
  if (value === null || value === undefined) {
    return "";
  }
  const rating = Number(value);
  if (!Number.isFinite(rating)) {
    return "";
  }
  return rating === 1 ? "1 star" : `${rating} stars`;
}

function parseRatingValue(value) {
  if (value === null || value === undefined) {
    return null;
  }
  const raw = String(value).trim();
  if (!raw) {
    return null;
  }
  const rating = Number(raw);
  if (!Number.isFinite(rating)) {
    return null;
  }
  return rating;
}

function formatDateTimeInput(value) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) {
    return "";
  }
  const pad = (num) => String(num).padStart(2, "0");
  const year = date.getFullYear();
  const month = pad(date.getMonth() + 1);
  const day = pad(date.getDate());
  const hours = pad(date.getHours());
  const minutes = pad(date.getMinutes());
  return `${year}-${month}-${day}T${hours}:${minutes}`;
}

function parseDateTimeInput(value) {
  if (!value) {
    return null;
  }
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) {
    return null;
  }
  return date.toISOString();
}

function formatMetadataValue(value) {
  if (!value) {
    return "—";
  }
  return String(value);
}

function formatMetadataDate(value) {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) {
    return "—";
  }
  return date.toLocaleString();
}

function normalizeExternalIds(item) {
  const metadataIds = item && item.metadata && item.metadata.ids ? item.metadata.ids : {};
  return {
    imdb_id: metadataIds.imdb_id || item.imdb_id,
    tmdb_id: metadataIds.tmdb_id || item.tmdb_id,
    tvdb_id: metadataIds.tvdb_id || item.tvdb_id,
    tvmaze_id: metadataIds.tvmaze_id || item.tvmaze_id,
    kitsu_id: metadataIds.kitsu_id || item.kitsu_id,
    myanimelist_id: metadataIds.myanimelist_id || item.myanimelist_id,
    anilist_id: metadataIds.anilist_id || item.anilist_id,
  };
}

function buildTraktUrl(item, ids) {
  if (ids.imdb_id) {
    return `https://trakt.tv/search/imdb?query=${encodeURIComponent(ids.imdb_id)}`;
  }
  if (ids.tmdb_id) {
    return `https://trakt.tv/search/tmdb?query=${encodeURIComponent(ids.tmdb_id)}`;
  }
  if (ids.tvdb_id) {
    return `https://trakt.tv/search/tvdb?query=${encodeURIComponent(ids.tvdb_id)}`;
  }
  if (item && item.title) {
    return `https://trakt.tv/search?query=${encodeURIComponent(item.title)}`;
  }
  return "";
}

function buildLetterboxdUrl(item, ids) {
  if (!item || item.media_type !== "movie") {
    return "";
  }
  if (ids.tmdb_id) {
    return `https://letterboxd.com/tmdb/${encodeURIComponent(ids.tmdb_id)}/`;
  }
  if (item.title) {
    return `https://letterboxd.com/search/films/${encodeURIComponent(item.title)}/`;
  }
  return "";
}

function buildExternalLinks(item) {
  if (!item) {
    return [];
  }
  const ids = normalizeExternalIds(item);
  const links = [];
  const seen = new Set();
  const addLink = (label, url) => {
    if (!url || seen.has(url)) {
      return;
    }
    seen.add(url);
    links.push({ label, url });
  };
  const mediaType = item.media_type || "movie";

  addLink("Letterboxd", buildLetterboxdUrl(item, ids));
  addLink("Trakt", buildTraktUrl(item, ids));
  if (ids.imdb_id) {
    addLink("IMDb", `https://www.imdb.com/title/${encodeURIComponent(ids.imdb_id)}/`);
  }
  if (ids.tmdb_id) {
    const section = mediaType === "movie" ? "movie" : "tv";
    addLink(
      "TMDB",
      `https://www.themoviedb.org/${section}/${encodeURIComponent(ids.tmdb_id)}`
    );
  }
  if (ids.tvdb_id) {
    const section = mediaType === "movie" ? "movies" : "series";
    addLink(
      "TVDB",
      `https://thetvdb.com/${section}/${encodeURIComponent(ids.tvdb_id)}`
    );
  }
  if (ids.tvmaze_id) {
    addLink("TVMaze", `https://www.tvmaze.com/shows/${encodeURIComponent(ids.tvmaze_id)}`);
  }
  if (ids.kitsu_id && mediaType === "anime") {
    addLink("Kitsu", `https://kitsu.io/anime/${encodeURIComponent(ids.kitsu_id)}`);
  }
  if (ids.myanimelist_id && mediaType === "anime") {
    addLink(
      "MyAnimeList",
      `https://myanimelist.net/anime/${encodeURIComponent(ids.myanimelist_id)}`
    );
  }
  if (ids.anilist_id && mediaType === "anime") {
    addLink("AniList", `https://anilist.co/anime/${encodeURIComponent(ids.anilist_id)}`);
  }
  return links;
}

function buildExternalMenuLinks(item) {
  return buildExternalLinks(item).map((link) => {
    const anchor = document.createElement("a");
    anchor.href = link.url;
    anchor.target = "_blank";
    anchor.rel = "noreferrer noopener";
    anchor.textContent = `View in ${link.label}`;
    anchor.setAttribute("role", "menuitem");
    return anchor;
  });
}

function renderMetadataSection(title, rows) {
  const section = document.createElement("section");
  section.className = "metadata-section";
  const header = document.createElement("h3");
  header.textContent = title;
  section.appendChild(header);
  rows.forEach((row) => {
    const line = document.createElement("div");
    line.className = "metadata-row";
    const label = document.createElement("span");
    label.textContent = row.label;
    const value = document.createElement("span");
    value.textContent = row.value;
    line.appendChild(label);
    line.appendChild(value);
    section.appendChild(line);
  });
  return section;
}

function openMetadataModal(item) {
  const modal = document.getElementById("metadata-modal");
  if (!modal || !item) {
    return;
  }
  const title = modal.querySelector("[data-modal-title]");
  const body = modal.querySelector("[data-modal-body]");
  if (!title || !body) {
    return;
  }
  const metadata = item.metadata || {};
  const hasEpisode =
    item.season_number !== null &&
    item.season_number !== undefined &&
    item.episode_number !== null &&
    item.episode_number !== undefined;
  const ids = {
    imdb_id: (metadata.ids && metadata.ids.imdb_id) || item.imdb_id,
    tmdb_id: (metadata.ids && metadata.ids.tmdb_id) || item.tmdb_id,
    tvdb_id: (metadata.ids && metadata.ids.tvdb_id) || item.tvdb_id,
    tvmaze_id: (metadata.ids && metadata.ids.tvmaze_id) || item.tvmaze_id,
    kitsu_id: (metadata.ids && metadata.ids.kitsu_id) || item.kitsu_id,
    myanimelist_id: (metadata.ids && metadata.ids.myanimelist_id) || item.myanimelist_id,
    anilist_id: (metadata.ids && metadata.ids.anilist_id) || item.anilist_id,
  };
  const episodeIds = {
    imdb_id:
      (metadata.episode_ids && metadata.episode_ids.imdb_id) ||
      item.episode_imdb_id,
    tmdb_id:
      (metadata.episode_ids && metadata.episode_ids.tmdb_id) ||
      item.episode_tmdb_id,
    tvdb_id:
      (metadata.episode_ids && metadata.episode_ids.tvdb_id) ||
      item.episode_tvdb_id,
    tvmaze_id:
      (metadata.episode_ids && metadata.episode_ids.tvmaze_id) ||
      item.episode_tvmaze_id,
  };
  title.textContent = `Metadata for ${item.title}`;
  body.innerHTML = "";

  const systemRows = [
    { label: "Watched entry ID", value: formatMetadataValue(item.id) },
    {
      label: "Media item ID",
      value: formatMetadataValue(metadata.media_item_id),
    },
  ];
  if (hasEpisode || metadata.episode_item_id) {
    systemRows.push({
      label: "Episode item ID",
      value: formatMetadataValue(metadata.episode_item_id),
    });
  }
  body.appendChild(renderMetadataSection("Library IDs", systemRows));

  const externalRows = [
    { label: "IMDb", value: formatMetadataValue(ids.imdb_id) },
    { label: "TMDB", value: formatMetadataValue(ids.tmdb_id) },
    { label: "TVDB", value: formatMetadataValue(ids.tvdb_id) },
    { label: "TVMaze", value: formatMetadataValue(ids.tvmaze_id) },
    { label: "Kitsu", value: formatMetadataValue(ids.kitsu_id) },
    { label: "MyAnimeList", value: formatMetadataValue(ids.myanimelist_id) },
    { label: "AniList", value: formatMetadataValue(ids.anilist_id) },
  ];
  body.appendChild(renderMetadataSection("External IDs", externalRows));

  if (hasEpisode) {
    const episodeRows = [
      { label: "Episode IMDb", value: formatMetadataValue(episodeIds.imdb_id) },
      { label: "Episode TMDB", value: formatMetadataValue(episodeIds.tmdb_id) },
      { label: "Episode TVDB", value: formatMetadataValue(episodeIds.tvdb_id) },
      {
        label: "Episode TVMaze",
        value: formatMetadataValue(episodeIds.tvmaze_id),
      },
    ];
    body.appendChild(renderMetadataSection("Episode IDs", episodeRows));
  }

  const timestampRows = [
    {
      label: "Watched entry created",
      value: formatMetadataDate(metadata.watched_created_at),
    },
    {
      label: "Media metadata created",
      value: formatMetadataDate(metadata.media_created_at),
    },
    {
      label: "Media metadata updated",
      value: formatMetadataDate(metadata.media_updated_at),
    },
  ];
  if (metadata.episode_created_at || metadata.episode_updated_at) {
    timestampRows.push(
      {
        label: "Episode metadata created",
        value: formatMetadataDate(metadata.episode_created_at),
      },
      {
        label: "Episode metadata updated",
        value: formatMetadataDate(metadata.episode_updated_at),
      },
    );
  }
  timestampRows.push(
    {
      label: "First sync attempt",
      value: formatMetadataDate(metadata.first_sync_at),
    },
    {
      label: "Last sync update",
      value: formatMetadataDate(metadata.last_sync_at),
    },
  );
  body.appendChild(renderMetadataSection("Timestamps", timestampRows));

  modal.removeAttribute("hidden");
}

function closeMetadataModal() {
  const modal = document.getElementById("metadata-modal");
  if (!modal) {
    return;
  }
  modal.setAttribute("hidden", "");
}
