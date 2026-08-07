/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  // Preflight is disabled: the ported pages carry their own resets and rely on
  // default browser styling (h1/p/details/buttons), so Tailwind's global reset
  // would drift the faithful look. Utilities/flex/grid are still available.
  corePlugins: { preflight: false },
  theme: { extend: {} },
  plugins: [],
};
