/** Tailwind config for uEye. Compiled to web/tailwind.css at Docker build time
 *  by the standalone CLI (no CDN at runtime). Keep the color tokens here in sync
 *  with the CSS variables in web/style.css. */
module.exports = {
  content: ['./web/**/*.{html,js}'],
  theme: {
    extend: {
      colors: {
        bg: '#0b0f14', panel: '#121926', line: '#1f2a3a', fg: '#e8eef5',
        muted: '#7d8da0', accent: '#2dd4bf', ok: '#34d399', off: '#5b6b7d',
      },
      maxWidth: { screen2k: '1700px' },
    },
  },
};
