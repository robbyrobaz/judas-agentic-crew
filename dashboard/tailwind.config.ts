import type { Config } from "tailwindcss";

export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        background: "#f5efe2",
        foreground: "#16140f",
        panel: "#fff9ec",
        line: "#d2c3a3",
        accent: "#174c3c",
        sand: "#d8a35d",
        berry: "#8d3f4e",
      },
      boxShadow: {
        panel: "0 24px 80px rgba(34, 25, 7, 0.12)",
      },
      fontFamily: {
        display: ["Georgia", "serif"],
        body: ["ui-sans-serif", "system-ui", "sans-serif"],
      },
      backgroundImage: {
        haze: "radial-gradient(circle at top left, rgba(216,163,93,0.35), transparent 40%), radial-gradient(circle at bottom right, rgba(23,76,60,0.25), transparent 30%)",
      },
    },
  },
  plugins: [],
} satisfies Config;
