const dashboardState = {
  charts: {
    timeline: null,
    breakdown: null,
    ratings: null,
  },
};

function renderHomeStatus(statusData) {
  const pendingEl = document.getElementById("home-outbox-pending");
  if (!pendingEl) {
    return;
  }
  const inProgressEl = document.getElementById("home-outbox-progress");
  const nextOutboxEl = document.getElementById("home-next-outbox");
  const nextImportEl = document.getElementById("home-next-import");
  const queueEl = document.getElementById("home-import-queue");
  const metadataEl = document.getElementById("home-metadata-pending");

  if (!statusData) {
    [pendingEl, inProgressEl, nextOutboxEl, nextImportEl, queueEl, metadataEl].forEach(
      (el) => {
        if (el) {
          el.textContent = "—";
        }
      }
    );
    return;
  }

  const outbox = statusData.outbox || {};
  const counts = outbox.counts || {};
  const pending = (counts.pending || 0) + (counts.failed_retryable || 0);
  const inProgress = counts.in_progress || 0;
  const imports = statusData.imports || {};
  const quick = imports.quick || null;
  const importAll = imports.import_all || null;
  const queueOrder = Array.isArray(imports.queue_order) ? imports.queue_order : [];
  const isActive = (state) =>
    state && (state.status === "pending" || state.status === "in_progress");
  let activeQueue = [];
  if (isActive(importAll) && Array.isArray(importAll.queue) && importAll.queue.length) {
    activeQueue = importAll.queue;
  } else if (isActive(quick) && Array.isArray(quick.queue) && quick.queue.length) {
    activeQueue = quick.queue;
  } else if (queueOrder.length) {
    activeQueue = queueOrder;
  }
  const queue = activeQueue.length
    ? activeQueue.map((entry) => formatProvider(entry)).join(" → ")
    : "Idle";
  const metadataCounts = statusData.metadata ? statusData.metadata.counts : {};
  const metadataPending = metadataCounts && metadataCounts.pending ? metadataCounts.pending : 0;

  if (pendingEl) {
    pendingEl.textContent = String(pending);
  }
  if (inProgressEl) {
    inProgressEl.textContent = String(inProgress);
  }
  if (nextOutboxEl) {
    nextOutboxEl.textContent = formatRelativeTime(outbox.next_run_at);
    nextOutboxEl.title = formatMetadataDate(outbox.next_run_at);
  }
  if (nextImportEl) {
    nextImportEl.textContent = formatRelativeTime(quick ? quick.next_run_at : null);
    nextImportEl.title = formatMetadataDate(quick ? quick.next_run_at : null);
  }
  if (queueEl) {
    queueEl.textContent = queue;
  }
  if (metadataEl) {
    metadataEl.textContent = String(metadataPending);
  }
}

async function loadUpNext() {
  const section = document.getElementById("up-next-section");
  const container = document.getElementById("up-next-list");
  if (!section || !container) {
    return;
  }
  try {
    const data = await requestJSON("/api/dashboard/up-next?limit=12");
    const items = data && data.items ? data.items : [];
    renderUpNext(items);
  } catch (error) {
    console.error("Failed to load up next", error);
  }
}

function renderUpNext(items) {
  const section = document.getElementById("up-next-section");
  const container = document.getElementById("up-next-list");
  if (!section || !container) {
    return;
  }
  if (!items.length) {
    section.hidden = true;
    container.innerHTML = "";
    return;
  }
  section.hidden = false;
  container.innerHTML = "";
  items.forEach((item) => {
    const nextEpisode = item.next_episode || {};
    const episodeLabel = formatSeasonEpisode(
      nextEpisode.season_number,
      nextEpisode.episode_number,
    );

    // Row container
    const row = document.createElement("div");
    row.className = "up-next-row";

    // Thumbnail
    const thumb = document.createElement("img");
    thumb.className = "up-next-thumb";
    if (item.poster_url) {
      thumb.src = item.poster_url;
      thumb.alt = `${item.title} poster`;
      thumb.loading = "lazy";
    } else {
      thumb.alt = "";
    }

    // Body
    const body = document.createElement("div");
    body.className = "up-next-body";

    // Title row: show name + episode code + new badge
    const titleRow = document.createElement("div");
    titleRow.className = "up-next-title-row";

    const titleEl = document.createElement("span");
    titleEl.className = "up-next-title";
    titleEl.textContent = item.title;
    titleRow.appendChild(titleEl);

    if (episodeLabel) {
      const epEl = document.createElement("span");
      epEl.className = "up-next-episode";
      epEl.textContent = episodeLabel;
      titleRow.appendChild(epEl);
    }

    if (item.is_new_release) {
      const badge = document.createElement("span");
      badge.className = "up-next-new-badge";
      badge.textContent = "New";
      titleRow.appendChild(badge);
    }

    // Meta line: episode title · aired date
    const metaParts = [];
    if (nextEpisode.title) {
      metaParts.push(nextEpisode.title);
    }
    if (nextEpisode.air_date) {
      metaParts.push(`Aired ${formatReleaseDate(nextEpisode.air_date)}`);
    } else if (item.year && !episodeLabel) {
      metaParts.push(String(item.year));
    }

    const metaEl = document.createElement("p");
    metaEl.className = "up-next-meta";
    metaEl.textContent = metaParts.join(" · ");

    body.appendChild(titleRow);
    body.appendChild(metaEl);

    // Action button — compact, icon+text on desktop, icon-only hint on mobile
    const action = document.createElement("div");
    action.className = "up-next-action";

    const markButton = document.createElement("button");
    markButton.type = "button";
    markButton.className = "btn btn-primary btn-xs";
    markButton.setAttribute(
      "aria-label",
      episodeLabel ? `Mark ${item.title} ${episodeLabel} as watched` : `Mark ${item.title} as watched`,
    );
    markButton.setAttribute("title", episodeLabel ? `Mark ${episodeLabel} watched` : "Mark watched");

    // SVG check icon
    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("width", "12");
    svg.setAttribute("height", "12");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2.5");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    const polyline = document.createElementNS(svgNS, "polyline");
    polyline.setAttribute("points", "20 6 9 17 4 12");
    svg.appendChild(polyline);

    const btnLabel = document.createElement("span");
    btnLabel.textContent = "Watched";

    markButton.appendChild(svg);
    markButton.appendChild(btnLabel);

    markButton.addEventListener("click", async () => {
      markButton.disabled = true;
      try {
        const data = await requestJSON(
          `/api/history/shows/${item.media_item_id}/mark-next-episode`,
          { method: "POST" },
        );
        const added = data && data.added_episode ? data.added_episode : episodeLabel;
        showToast(`Marked ${added || "episode"} as watched.`);
        await Promise.all([loadUpNext(), loadDashboardStats()]);
      } catch (error) {
        showToast(error.message, true);
        markButton.disabled = false;
      }
    });
    action.appendChild(markButton);

    row.appendChild(thumb);
    row.appendChild(body);
    row.appendChild(action);
    container.appendChild(row);
  });
}

async function loadHomeStatus() {
  const statusCard = document.getElementById("home-status");
  if (!statusCard) {
    return;
  }
  try {
    const data = await requestJSON("/api/status");
    renderHomeStatus(data);
  } catch (error) {
    console.error("Failed to load home status", error);
  }
}

async function loadDashboardStats() {
  const statsElements = [
    document.getElementById("dashboard-movies-count"),
    document.getElementById("dashboard-shows-count"),
    document.getElementById("dashboard-episodes-count"),
    document.getElementById("dashboard-avg-rating"),
  ];

  const chartElements = [
    document.getElementById("activity-timeline-chart"),
    document.getElementById("content-breakdown-chart"),
    document.getElementById("rating-distribution-chart"),
  ];

  if (!statsElements.some((el) => el) && !chartElements.some((el) => el)) {
    return;
  }

  try {
    const data = await requestJSON("/api/dashboard/stats");
    renderDashboardStats(data);
    renderDashboardCharts(data);
  } catch (error) {
    console.error("Failed to load dashboard stats", error);
    if (error.status === 403) {
      const dashboardSections = document.querySelectorAll("[data-dashboard-section]");
      dashboardSections.forEach((section) => {
        section.style.display = "none";
      });
    }
  }
}

function renderDashboardStats(data) {
  const userStats = data.user_stats || {};
  const systemStats = data.system_stats || {};
  const integrationSummary = data.integration_summary || {};

  const movieCount = document.getElementById("dashboard-movies-count");
  if (movieCount) {
    movieCount.textContent = String(userStats.movies_watched || 0);
  }

  const showsCount = document.getElementById("dashboard-shows-count");
  if (showsCount) {
    showsCount.textContent = String(userStats.shows_watched || 0);
  }

  const episodesCount = document.getElementById("dashboard-episodes-count");
  if (episodesCount) {
    episodesCount.textContent = String(userStats.episodes_watched || 0);
  }

  const avgRating = document.getElementById("dashboard-avg-rating");
  if (avgRating) {
    const rating = userStats.avg_rating || 0;
    avgRating.textContent = rating > 0 ? rating.toFixed(1) : "—";
  }

  const systemMediaCount = document.getElementById("system-media-count");
  if (systemMediaCount) {
    systemMediaCount.textContent = String(systemStats.total_media_items || 0);
  }

  const systemEpisodesCount = document.getElementById("system-episodes-count");
  if (systemEpisodesCount) {
    systemEpisodesCount.textContent = String(systemStats.total_episode_items || 0);
  }

  const systemSyncCount = document.getElementById("system-sync-count");
  if (systemSyncCount) {
    systemSyncCount.textContent = String(systemStats.total_sync_events || 0);
  }

  const systemIntegrationsCount = document.getElementById("system-integrations-count");
  if (systemIntegrationsCount) {
    systemIntegrationsCount.textContent = String(integrationSummary.total_integrations || 0);
  }
}

function renderDashboardCharts(data) {
  if (!window.Chart) {
    console.warn("Chart.js not loaded");
    return;
  }

  const userStats = data.user_stats || {};
  const dailyActivity = data.daily_activity || [];
  const ratingDistribution = data.rating_distribution || [];
  const overallDailyActivity = data.overall_daily_activity || [];
  const overallRatingDistribution = data.overall_rating_distribution || [];

  function toggleLegendItem(e, legendItem, legend) {
    const index = legendItem.datasetIndex;
    const chart = legend.chart;
    const meta = chart.getDatasetMeta(index);
    if (meta.hidden === null) {
      meta.hidden = !chart.data.datasets[index].hidden;
    } else {
      meta.hidden = !meta.hidden;
    }
    chart.update();
  }

  const isDark =
    document.documentElement.dataset.theme === "dark" ||
    (document.documentElement.dataset.theme !== "light" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches);

  const colors = {
    primary: isDark ? "rgba(20, 144, 228, 1)" : "rgba(13, 120, 211, 1)",
    primaryAlpha: isDark ? "rgba(20, 144, 228, 0.2)" : "rgba(13, 120, 211, 0.2)",
    accent: isDark ? "rgba(26, 170, 183, 1)" : "rgba(11, 138, 155, 1)",
    accentAlpha: isDark ? "rgba(26, 170, 183, 0.2)" : "rgba(11, 138, 155, 0.2)",
    overall: isDark ? "rgba(169, 182, 195, 1)" : "rgba(100, 116, 139, 1)",
    overallAlpha: isDark ? "rgba(169, 182, 195, 0.2)" : "rgba(100, 116, 139, 0.2)",
    text: isDark ? "rgb(231, 238, 245)" : "rgb(24, 32, 45)",
    muted: isDark ? "rgb(169, 182, 195)" : "rgb(100, 116, 139)",
    grid: isDark ? "rgba(46, 63, 79, 0.3)" : "rgba(214, 223, 230, 0.3)",
  };

  const timelineCanvas = document.getElementById("activity-timeline-chart");
  if (timelineCanvas) {
    if (dashboardState.charts.timeline) {
      dashboardState.charts.timeline.destroy();
    }

    const dateMap = new Map();
    dailyActivity.forEach((d) => {
      dateMap.set(d.date, {
        userMovies: d.movies,
        userEpisodes: d.episodes,
        overallMovies: 0,
        overallEpisodes: 0,
      });
    });
    overallDailyActivity.forEach((d) => {
      const existing = dateMap.get(d.date) || { userMovies: 0, userEpisodes: 0 };
      dateMap.set(d.date, {
        ...existing,
        overallMovies: d.movies,
        overallEpisodes: d.episodes,
      });
    });

    const sortedDates = Array.from(dateMap.keys()).sort();
    const labels = sortedDates;
    const userMoviesData = sortedDates.map((date) => dateMap.get(date).userMovies);
    const userEpisodesData = sortedDates.map((date) => dateMap.get(date).userEpisodes);
    const overallMoviesData = sortedDates.map((date) => dateMap.get(date).overallMovies);
    const overallEpisodesData = sortedDates.map((date) => dateMap.get(date).overallEpisodes);

    const datasets = [
      {
        label: "Your Movies",
        data: userMoviesData,
        borderColor: colors.primary,
        backgroundColor: colors.primaryAlpha,
        tension: 0.3,
        fill: true,
      },
      {
        label: "Your Episodes",
        data: userEpisodesData,
        borderColor: colors.accent,
        backgroundColor: colors.accentAlpha,
        tension: 0.3,
        fill: true,
      },
    ];

    if (overallDailyActivity.length > 0) {
      datasets.push(
        {
          label: "Overall Movies",
          data: overallMoviesData,
          borderColor: colors.overall,
          backgroundColor: colors.overallAlpha,
          tension: 0.3,
          fill: false,
          borderDash: [5, 5],
          hidden: true,
        },
        {
          label: "Overall Episodes",
          data: overallEpisodesData,
          borderColor: colors.muted,
          backgroundColor: colors.overallAlpha,
          tension: 0.3,
          fill: false,
          borderDash: [5, 5],
          hidden: true,
        }
      );
    }

    dashboardState.charts.timeline = new Chart(timelineCanvas, {
      type: "line",
      data: {
        labels: labels,
        datasets: datasets,
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            labels: {
              color: colors.text,
              font: {
                family: "IBM Plex Sans",
              },
            },
            onClick: toggleLegendItem,
          },
          tooltip: {
            callbacks: {
              title: function(context) {
                return context[0].label;
              },
            },
          },
        },
        scales: {
          x: {
            ticks: {
              color: colors.muted,
              maxRotation: 45,
              minRotation: 45,
            },
            grid: {
              color: colors.grid,
            },
          },
          y: {
            beginAtZero: true,
            ticks: {
              color: colors.muted,
              precision: 0,
            },
            grid: {
              color: colors.grid,
            },
          },
        },
      },
    });
  }

  const breakdownCanvas = document.getElementById("content-breakdown-chart");
  if (breakdownCanvas && (userStats.movies_watched || userStats.episodes_watched)) {
    if (dashboardState.charts.breakdown) {
      dashboardState.charts.breakdown.destroy();
    }

    dashboardState.charts.breakdown = new Chart(breakdownCanvas, {
      type: "doughnut",
      data: {
        labels: ["Movies", "Episodes"],
        datasets: [
          {
            data: [userStats.movies_watched || 0, userStats.episodes_watched || 0],
            backgroundColor: [colors.primary, colors.accent],
            borderWidth: 0,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            position: "bottom",
            labels: {
              color: colors.text,
              font: {
                family: "IBM Plex Sans",
              },
              padding: 15,
            },
          },
        },
      },
    });
  }

  const ratingsCanvas = document.getElementById("rating-distribution-chart");
  if (ratingsCanvas && (ratingDistribution.length > 0 || overallRatingDistribution.length > 0)) {
    if (dashboardState.charts.ratings) {
      dashboardState.charts.ratings.destroy();
    }

    const allRatings = [0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5];
    const userRatingCounts = allRatings.map((rating) => {
      const found = ratingDistribution.find((r) => r.rating === rating);
      return found ? found.count : 0;
    });
    const overallRatingCounts = allRatings.map((rating) => {
      const found = overallRatingDistribution.find((r) => r.rating === rating);
      return found ? found.count : 0;
    });

    const datasets = [
      {
        label: "Your Ratings",
        data: userRatingCounts,
        backgroundColor: colors.primary,
        borderRadius: 6,
      },
    ];

    if (overallRatingDistribution.length > 0) {
      datasets.push({
        label: "Overall Ratings",
        data: overallRatingCounts,
        backgroundColor: colors.overall,
        borderRadius: 6,
        hidden: true,
      });
    }

    dashboardState.charts.ratings = new Chart(ratingsCanvas, {
      type: "bar",
      data: {
        labels: allRatings.map((r) => r.toString()),
        datasets: datasets,
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            display: overallRatingDistribution.length > 0,
            labels: {
              color: colors.text,
              font: {
                family: "IBM Plex Sans",
              },
            },
            onClick: toggleLegendItem,
          },
        },
        scales: {
          x: {
            ticks: {
              color: colors.muted,
            },
            grid: {
              display: false,
            },
          },
          y: {
            beginAtZero: true,
            ticks: {
              color: colors.muted,
              precision: 0,
            },
            grid: {
              color: colors.grid,
            },
          },
        },
      },
    });
  }
}

window.librarysyncPageInit = async ({ user }) => {
  if (!user) {
    return;
  }
  await Promise.all([loadHomeStatus(), loadDashboardStats(), loadUpNext()]);
};
