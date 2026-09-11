import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{js,ts,jsx,tsx}", "./components/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#111827",
        panel: "#f7f8fa",
        line: "#d9dee7",
        mint: "#0f8b6f",
        coral: "#bd4b43",
        amber: "#b87912"
      }
    }
  },
  plugins: []
};

export default config;
