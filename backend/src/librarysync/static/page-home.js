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
  const quick = statusData.imports ? statusData.imports.quick : null;
  const queue =
    quick && Array.isArray(quick.queue) && quick.queue.length
      ? quick.queue.map((entry) => formatProvider(entry)).join(" → ")
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
  await Promise.all([loadHomeStatus(), loadDashboardStats()]);
};
