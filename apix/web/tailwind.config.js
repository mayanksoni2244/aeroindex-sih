/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        // Provenance is encoded by colour in several places at once (chart
        // dots, the badge, the coverage bars). Naming the colours after what
        // they mean rather than what they look like keeps those in step.
        live: '#059669',       // emerald-600
        // Tier-1.5 needs its own swatch. Sharing the `live` green would claim a
        // rate-capped source is as well-covered as an airline-direct one;
        // sharing the `simulated` amber would call a real observed fare made
        // up. Teal reads as adjacent to green, which is the honest relation.
        liveLimited: '#0d9488', // teal-600
        simulated: '#d97706',  // amber-600
        imputed: '#7c3aed',    // violet-600
        surge: '#dc2626',      // red-600
      },
      fontFamily: {
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
    },
  },
  plugins: [],
}
