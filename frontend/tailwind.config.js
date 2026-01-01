module.exports = {
  content: [
    "../backend/src/librarysync/templates/**/*.html",
    "../backend/src/librarysync/static/app.js",
  ],
  theme: {
    extend: {
      colors: {
        base: "rgb(var(--color-base) / <alpha-value>)",
        surface: "rgb(var(--color-surface) / <alpha-value>)",
        elevated: "rgb(var(--color-elevated) / <alpha-value>)",
        line: "rgb(var(--color-line) / <alpha-value>)",
        ink: "rgb(var(--color-ink) / <alpha-value>)",
        muted: "rgb(var(--color-muted) / <alpha-value>)",
        primary: "rgb(var(--color-primary) / <alpha-value>)",
        accent: "rgb(var(--color-accent) / <alpha-value>)",
        teal: "rgb(var(--color-teal) / <alpha-value>)",
        steel: "rgb(var(--color-steel) / <alpha-value>)",
        slate: "rgb(var(--color-slate) / <alpha-value>)",
      },
      fontFamily: {
        display: ["Space Grotesk", "IBM Plex Sans", "system-ui", "sans-serif"],
        body: ["IBM Plex Sans", "system-ui", "sans-serif"],
      },
      boxShadow: {
        card: "0 18px 40px -24px rgba(15, 27, 36, 0.45)",
        glow: "0 0 0 1px rgba(13, 120, 211, 0.12), 0 12px 30px -24px rgba(13, 120, 211, 0.6)",
      },
      borderRadius: {
        xl: "1rem",
        "2xl": "1.5rem",
      },
    },
  },
  plugins: [],
};
