# librarySync Frontend Refresh Plan (Tailwind + PWA, Local Assets + Dark Mode)

## Goals and Constraints
- Use Tailwind CSS built locally and committed (no external CDN CSS/JS).
- Serve fonts locally (no external font CDNs).
- Keep plain JS only; no frameworks or bundlers.
- Mobile-first and fully responsive.
- PWA compatible (manifest, service worker, installable).
- Modern, fast UI with a cohesive, reusable design language.
- Friendly URLs for pages (e.g., `/login` not `/static/login.html`).
- Palette should match the logo (blue/teal/steel-gray) and include a dark mode.

## Current Pages (Static)
- `/` -> `index.html`
- `/static/login.html`
- `/static/add-watched.html`
- `/static/history.html`
- `/static/integrations.html`
- `/static/activity.html`
- `/static/settings.html`

## Routing and URL Plan
- Add FastAPI routes to serve each HTML page at clean URLs:
  - `/login`, `/add-watched`, `/history`, `/integrations`, `/activity`, `/settings`
- Keep `/static` for assets only (JS, CSS, icons).
- Update all in-app links and redirects in `app.js` to use clean URLs.
- Update `site.webmanifest` with `start_url` and `scope` to `/`.

## Tailwind Integration (Local Build)
- Add a minimal Tailwind build step and commit output CSS:
  - `frontend/tailwind.config.js`
  - `frontend/input.css`
  - Build to `backend/src/librarysync/static/styles.css`
- Define a small, shared Tailwind config:
  - Colors aligned to the logo palette (blue/teal/steel-gray).
  - Typography: self-hosted fonts (e.g., `Space Grotesk` for headings, `IBM Plex Sans` for body).
  - Shadow + radius tokens for cards, modals, and buttons.
- Keep additional custom CSS minimal and colocated in `input.css`.
- No external CSS/JS loaded at runtime.

## Reusable Layout Components
- Use server-side templates (Jinja2) with a shared base layout:
  - `base.html` includes header/footer and common meta tags.
  - Per-page templates provide only main content.
  - More common for server-rendered apps and less prone to client-side breakage.
- Keep component class names consistent across pages (buttons, cards, badges).

## Color System and Dark Mode
- Define CSS variables for light and dark themes in `input.css`.
- Brand palette (from `logo_w_text.png`, approximate hex):
  - Primary blue (Sync text): `#0d78d3`
  - Accent blue (icon highlight): `#1490e4`
  - Teal (icon): `#0b8a9b`
  - Steel gray (library text + arrows): `#8b98a1`
  - Deep slate (dark backgrounds): `#0f1b24`
  - Soft white (light backgrounds): `#f6f8fa`
- Light theme: soft white background, steel-gray text, blue/teal accents.
- Dark theme: deep slate background, muted gray text, bright blue/teal accents.
- Use `@media (prefers-color-scheme: dark)` for default dark mode.
- Optional manual toggle stored in `localStorage` to override system preference.
- Set `meta name="theme-color"` for light/dark variants with media queries.

## PWA Plan
- Add `service-worker.js` to cache:
  - Core HTML pages, `/static/app.js`, `/static/styles.css`, icons, manifest.
  - Use a simple stale-while-revalidate strategy.
- Add an offline fallback page for network loss.
- Update `site.webmanifest`:
  - `start_url: "/"`, `scope: "/"`, `display: "standalone"`
  - `theme_color` and `background_color` aligned with new palette.
- Add `meta name="theme-color"` to every page.
- Register service worker in `app.js` once on DOM load.

## Performance Plan
- Prefer lightweight markup and avoid large inline scripts.
- Prebuild Tailwind CSS and keep only the utilities in use.
- Use `defer` for JS, `loading="lazy"` for images.
- Self-host fonts in `static/fonts` and serve WOFF2 only.
- For large lists (history/activity), keep pagination and add skeleton loaders.
- Avoid extra API calls; cache user info in memory during a session.

## Page-by-Page Plan

### Home (`/`)
- Hero section with quick description and CTA buttons.
- Quick actions card: Add Watched, History, Integrations.
- Status cards: last sync, outbox count, import queue state.
- Guest view: show benefits + login/register CTA.

### Login (`/login`)
- Split layout: left branding/benefits, right auth forms.
- Improve form spacing, input focus states, and inline error text.
- Hide registration block when disabled by config (already handled in JS).
- Add "Forgot password" placeholder (non-functional for now).

### Add Watched (`/add-watched`)
- Search form as top hero card.
- Results in a responsive grid with posters and metadata chips.
- Episode picker in a side panel or drawer.
- Rating selector upgraded to star UI (still posts numeric rating).
- Confirm panel with summary before submission.

### History (`/history`)
- Filters in a sticky toolbar (search, type, source, paging).
- List rows show poster, title, year, provider badges, rating.
- Bulk actions in a bottom action bar when selections exist.
- Metadata modal restyled as slide-over with clear close action.

### Integrations (`/integrations`)
- Grouped sections: Imports, Providers, Metadata.
- Each provider card has status badge, connect/disconnect, last import.
- Quick Import and Import All as prominent primary actions.
- Security note under providers explaining where secrets live.

### Activity (`/activity`)
- Summary row with counts: pending, failed, retrying, last run.
- Schedule list as timeline cards with next run times.
- Filters grouped and compact on mobile.
- Sync activity list uses status pills and provider icons.

### Settings (`/settings`)
- Split into "Search" and "Metadata Providers" cards.
- Provider cards use consistent toggles + save button placement.
- Inline helper text for API keys and language fields.

## Implementation Steps (High-Level)
1. Add clean URL routes in backend and update `app.js` redirects/links.
2. Convert static HTML to Jinja2 templates with a shared base layout.
3. Add Tailwind local build config, self-hosted fonts, and generate `styles.css`.
4. Replace page markup with Tailwind-based layout + reusable components.
5. Update manifest and add service worker for PWA behavior.
6. Verify dark mode, mobile responsiveness, and performance with real data.
