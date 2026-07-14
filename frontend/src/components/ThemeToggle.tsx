import { useEffect, useState } from "react";

export type Theme = "system" | "light" | "dark";

const KEY = "4drop.theme";
const media = () => window.matchMedia("(prefers-color-scheme: dark)");

function systemTheme(): "light" | "dark" {
  return media().matches ? "dark" : "light";
}

function apply(theme: Theme) {
  const resolved = theme === "system" ? systemTheme() : theme;
  document.documentElement.classList.toggle("dark", resolved === "dark");
}

/** Применяем тему до первого рендера, иначе на перезагрузке моргает светлым. */
export function initTheme() {
  apply((localStorage.getItem(KEY) as Theme) ?? "system");
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(
    () => (localStorage.getItem(KEY) as Theme) ?? "system",
  );

  useEffect(() => {
    apply(theme);
    localStorage.setItem(KEY, theme);
  }, [theme]);

  // В режиме «системная» следим за сменой темы устройства на лету.
  useEffect(() => {
    if (theme !== "system") return;
    const m = media();
    const onChange = () => apply("system");
    m.addEventListener("change", onChange);
    return () => m.removeEventListener("change", onChange);
  }, [theme]);

  // Цикл: системная → противоположная системной → системная по значению → снова системная.
  // Порядок зависит от темы устройства: при светлой системе первый клик даёт тёмную,
  // при тёмной — светлую. Иначе первый клик выглядел бы как «ничего не произошло».
  const cycle = () => {
    const sys = systemTheme();
    const opposite: Theme = sys === "dark" ? "light" : "dark";
    const order: Theme[] = ["system", opposite, sys];
    setTheme(order[(order.indexOf(theme) + 1) % order.length]);
  };

  const label =
    theme === "system" ? "Тема: системная" : theme === "dark" ? "Тема: тёмная" : "Тема: светлая";

  return (
    <button
      onClick={cycle}
      title={`${label} — нажмите, чтобы переключить`}
      aria-label={label}
      className="rounded-md border border-slate-300 p-1.5 text-slate-600 transition hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
    >
      {theme === "system" ? (
        // монитор
        <svg className="h-4 w-4" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
          <rect x="2.5" y="3.5" width="15" height="10" rx="1.5" />
          <path d="M7 17h6M10 13.5V17" strokeLinecap="round" />
        </svg>
      ) : theme === "dark" ? (
        // луна
        <svg className="h-4 w-4" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
          <path
            d="M16.5 11.5A6.5 6.5 0 018.5 3.5a6.5 6.5 0 108 8z"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      ) : (
        // солнце
        <svg className="h-4 w-4" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
          <circle cx="10" cy="10" r="3.5" />
          <path
            d="M10 2v1.5M10 16.5V18M18 10h-1.5M3.5 10H2M15.7 4.3l-1 1M5.3 14.7l-1 1M15.7 15.7l-1-1M5.3 5.3l-1-1"
            strokeLinecap="round"
          />
        </svg>
      )}
    </button>
  );
}
