/* Watch State settings use the authenticated API, never the credential-bearing addon URL. */
(() => {
  const api = "/api/stremio-addon/watch-state";
  let mapping = null;
  let ratings = [];
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
  const date = (value) => value ? new Date(value).toLocaleString() : "None yet";
  const row = (text, action = "") => `<div class="rounded-2xl border border-line/60 p-3 flex flex-wrap items-center justify-between gap-3"><span class="text-sm text-ink">${text}</span>${action}</div>`;
  const button = (action, id, label) => `<button type="button" class="btn btn-secondary btn-sm" data-state-action="${action}" data-state-id="${escape(id)}">${label}</button>`;
  const render = (id, rows, empty) => {
    document.getElementById(id).innerHTML = rows.join("") || `<p class="helper-text">${empty}</p>`;
  };
  const send = (path, method = "POST", body) => requestJSON(`${api}${path}`, {
    method, ...(body ? {body: JSON.stringify(body)} : {}),
  });

  async function refresh() {
    const state = await requestJSON(`${api}/status`);
    document.getElementById("watch-state-summary").textContent =
      `Last event: ${date(state.last_received_at)}. Last AIOStreams pull: ${date(state.last_pulled_at)}.`;
    render("watch-state-resume", state.resume.map((item) => {
      const minutes = Math.floor(item.positionMs / 60000);
      const percent = item.progressPercent === undefined ? "duration unknown" : `${item.progressPercent}%`;
      return row(`${escape(item.metaId)}${item.episode == null ? "" : `, episode ${item.episode}`} at ${minutes} min (${percent})`,
        button("clear-resume", item.id, "Remove resume point"));
    }), "No paused or unfinished playback.");
    render("watch-state-next-up", state.next_up.map((item) => row(
      `${escape(item.metaId)}, season ${item.season}, episode ${item.episode}`,
    )), "No next episode to show. A show may be dropped, fully watched, unreleased or missing episode metadata.");
    ratings = state.ratings;
    render("watch-state-ratings", ratings.map((item, index) => {
      let scope = "";
      if (item.videoId) scope = ` / ${escape(item.videoId)}`;
      else if (item.season != null) scope = ` / season ${item.season}`;
      return row(`${escape(item.metaId)}${scope}: ${item.rating}/10`,
        button("edit-rating", index, "Edit") + button("clear-rating", index, "Clear rating"));
    }), "No ratings.");
    render("watch-state-viewers", state.viewers.map((item) => row(
      `${escape(item.viewer)}: ${escape(item.status)}`, button("revoke-viewer", item.id, "Revoke"),
    )), "No viewer bindings.");
    render("watch-state-events", state.events.map((event) => {
      const outcomes = (event.outcomes || []).filter((item) => item.status === "unresolved");
      let actions = ["unresolved", "failed"].includes(event.status) ? button("retry-event", event.id, "Retry") : "";
      outcomes.forEach((item) => {
        actions += `<button type="button" class="btn btn-secondary btn-sm" data-state-action="map-event" data-state-id="${escape(event.id)}" data-video-id="${escape(item.videoId)}">Map ${escape(item.videoId)}</button>`;
      });
      return row(`${escape(event.event)} on ${escape(event.metaId)}: ${escape(event.status)}${event.viewer ? ` (${escape(event.viewer)})` : ""}, ${event.duplicates} duplicate(s)<br><span class="text-xs text-muted">${escape(date(event.received_at))}${event.error ? `: ${escape(event.error)}` : ""}</span>`, actions);
    }), "No events received. Enable Watch State, save, and refresh the addon in AIOStreams.");
    render("watch-state-deliveries", state.deliveries.map((item) => row(
      `${escape(item.provider)} / ${escape(item.operation)}: ${escape(item.status)}${item.error ? `<br><span class="text-xs text-muted">${escape(item.error)}</span>` : ""}`,
    )), "No delivery jobs.");
  }

  async function run(action) {
    try {
      await action();
      await refresh();
      setMessage("watch-state-message", "Saved.", false);
    } catch (error) {
      setMessage("watch-state-message", error.message || "Unable to update Watch State.", true);
    }
  }

  function ratingBody(item, clear = false) {
    const episode = Boolean(item.videoId && item.videoId !== item.metaId);
    let scope = item.type === "movie" ? "movie" : "series";
    if (episode) scope = "episode";
    else if (item.season != null) scope = "season";
    return {
      id: "manual", at: Math.floor(Date.now() / 1000), event: clear ? "unrated" : "rated",
      scope,
      metaId: item.metaId, videoId: episode ? item.videoId : null,
      season: item.season ?? null, episode: item.episode ?? null, rating: clear ? null : item.rating,
    };
  }

  window.initializeWatchStatePage = async () => {
    document.getElementById("watch-state-refresh").addEventListener("click", () => run(refresh));
    document.getElementById("watch-state-panel").addEventListener("click", async (event) => {
      const target = event.target.closest("[data-state-action]");
      if (!target) return;
      const id = target.dataset.stateId;
      const action = target.dataset.stateAction;
      if (action === "map-event") {
        mapping = {receipt: id, video_id: target.dataset.videoId};
        document.getElementById("watch-state-mapping-panel").hidden = false;
        document.querySelector("#watch-state-mapping-search input").focus();
        return;
      }
      if (action === "edit-rating") {
        const body = ratingBody(ratings[Number(id)]);
        const form = document.getElementById("watch-state-rating-form");
        for (const name of ["scope", "metaId", "videoId", "season", "episode", "rating"]) {
          form.elements[name].value = body[name] ?? "";
        }
        form.elements.rating.focus();
        return;
      }
      await run(async () => {
        target.disabled = true;
        try {
          if (action === "clear-resume") await send(`/resume/${encodeURIComponent(id)}`, "DELETE");
          else if (action === "revoke-viewer") await send(`/viewers/${encodeURIComponent(id)}`, "DELETE");
          else if (action === "retry-event") await send(`/events/${encodeURIComponent(id)}/retry`);
          else if (action === "clear-rating") {
            const item = ratings[Number(id)];
            await send(`/ratings/${item.type === "movie" ? "movie" : "series"}`, "POST", ratingBody(item, true));
          }
        } finally { target.disabled = false; }
      });
    });
    document.getElementById("watch-state-rating-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const fields = new FormData(event.target);
      const scope = fields.get("scope");
      const body = {
        id: "manual", at: Math.floor(Date.now() / 1000), event: "rated", scope,
        metaId: fields.get("metaId").trim(), rating: Number(fields.get("rating")),
        season: fields.get("season") === "" ? null : Number(fields.get("season")),
        episode: fields.get("episode") === "" ? null : Number(fields.get("episode")),
        videoId: scope === "episode" ? fields.get("videoId").trim() : null,
      };
      run(() => send(`/ratings/${scope === "movie" ? "movie" : "series"}`, "POST", body));
    });
    document.getElementById("watch-state-invite-form").addEventListener("submit", (event) => {
      event.preventDefault();
      run(async () => {
        const result = await send("/viewers", "POST", {viewer: new FormData(event.target).get("viewer")});
        document.getElementById("watch-state-invitation-field").hidden = false;
        document.getElementById("watch-state-invitation").value = result.invitation;
      });
    });
    document.getElementById("watch-state-accept-form").addEventListener("submit", (event) => {
      event.preventDefault();
      run(async () => {
        await send("/viewers/accept", "POST", {invitation: new FormData(event.target).get("invitation").trim()});
        event.target.reset();
      });
    });
    document.getElementById("watch-state-mapping-search").addEventListener("submit", (event) => {
      event.preventDefault();
      run(async () => {
        const query = new FormData(event.target).get("query");
        const result = await requestJSON(`/api/metadata/lookup/local?query=${encodeURIComponent(query)}`);
        const select = document.querySelector("#watch-state-mapping-form select");
        select.innerHTML = result.candidates.filter((item) => item.media_type !== "movie").map((item) =>
          `<option value="${escape(item.id)}">${escape(item.title)}${item.year ? ` (${item.year})` : ""}</option>`,
        ).join("");
        if (!select.options.length) throw new Error("No show found. Add or refresh the title's metadata, then search again.");
      });
    });
    document.getElementById("watch-state-mapping-form").addEventListener("submit", (event) => {
      event.preventDefault();
      run(async () => {
        if (!mapping) throw new Error("Choose an unresolved episode first.");
        const fields = new FormData(event.target);
        await send(`/events/${encodeURIComponent(mapping.receipt)}/resolve`, "POST", {
          video_id: mapping.video_id, media_item_id: fields.get("media_item_id"),
          season: Number(fields.get("season")), episode: Number(fields.get("episode")),
        });
        document.getElementById("watch-state-mapping-panel").hidden = true;
        mapping = null;
      });
    });
    await run(refresh);
  };
})();
