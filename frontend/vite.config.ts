import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    host: true,
    // Разрешаем внешний домен: за reverse-proxy Vite иначе отвечает «Blocked request».
    allowedHosts: [".vplink.app"],
    // Фронтенд ходит на относительный /api, а Vite проксирует его на бэкенд внутри
    // docker-сети. Так интерфейс работает и с localhost, и с внешнего адреса сервера:
    // браузеру не нужно самому достучаться до порта бэкенда.
    proxy: {
      "/api": {
        target: "http://backend:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
