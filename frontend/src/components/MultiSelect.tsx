import { useEffect, useMemo, useRef, useState } from "react";

interface Props {
  label: string;
  options: string[];
  selected: string[];
  onChange: (values: string[]) => void;
  placeholder?: string;
  /** Поиск внутри списка. Для брендов обязателен — их сотни. */
  searchable?: boolean;
  /** Подпись для значения. В API уходит само значение, пользователю показываем
   *  человеческое: сезон приходит кодом «s»/«w»/«u». */
  renderOption?: (value: string) => string;
  className?: string;
}

export function MultiSelect({
  label,
  options,
  selected,
  onChange,
  placeholder = "Все",
  searchable = true,
  renderOption = (v) => v,
  className = "w-56",
}: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const visible = useMemo(() => {
    if (!query.trim()) return options;
    const q = query.toLowerCase();
    return options.filter((o) => renderOption(o).toLowerCase().includes(q));
  }, [options, query, renderOption]);

  const toggle = (value: string) => {
    onChange(
      selected.includes(value)
        ? selected.filter((v) => v !== value)
        : [...selected, value],
    );
  };

  // Показываем сами значения, пока они помещаются, дальше — счётчик:
  // «Bridgestone, Kama» полезнее, чем «выбрано: 2».
  const summary =
    selected.length === 0
      ? placeholder
      : selected.length <= 2
        ? selected.map(renderOption).join(", ")
        : `${selected.length} выбрано`;

  return (
    <div ref={root} className={`relative ${className}`}>
      <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">{label}</label>

      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`mt-1 flex w-full items-center justify-between gap-2 rounded-md border bg-white px-3 py-2 text-left text-sm transition dark:bg-slate-800 ${
          open
            ? "border-slate-900 ring-1 ring-slate-900 dark:border-slate-400 dark:ring-slate-400"
            : "border-slate-300 hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
        }`}
      >
        <span
          className={`truncate ${selected.length ? "text-slate-900 dark:text-slate-100" : "text-slate-400 dark:text-slate-500"}`}
        >
          {summary}
        </span>
        <span className="flex shrink-0 items-center gap-1.5">
          {selected.length > 0 && (
            <span
              role="button"
              tabIndex={0}
              aria-label={`Сбросить ${label.toLowerCase()}`}
              onClick={(e) => {
                e.stopPropagation();
                onChange([]);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.stopPropagation();
                  onChange([]);
                }
              }}
              className="rounded-full px-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700 dark:hover:bg-slate-700 dark:hover:text-slate-200"
            >
              ✕
            </span>
          )}
          <svg
            className={`h-4 w-4 text-slate-400 transition ${open ? "rotate-180" : ""}`}
            viewBox="0 0 20 20"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
          >
            <path d="M6 8l4 4 4-4" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </span>
      </button>

      {open && (
        <div className="absolute z-20 mt-1 w-full overflow-hidden rounded-lg border border-slate-200 bg-white shadow-lg dark:border-slate-700 dark:bg-slate-800">
          {searchable && (
            <div className="border-b border-slate-100 p-2 dark:border-slate-700">
              <input
                autoFocus
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Поиск…"
                className="w-full rounded-md border border-slate-200 px-2 py-1.5 text-sm focus:border-slate-900 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
              />
            </div>
          )}

          <div className="max-h-64 overflow-auto py-1">
            {visible.length === 0 ? (
              <p className="px-3 py-4 text-center text-sm text-slate-400">Ничего не найдено</p>
            ) : (
              visible.map((option) => {
                const checked = selected.includes(option);
                return (
                  <label
                    key={option}
                    className="flex cursor-pointer items-center gap-2.5 px-3 py-1.5 text-sm hover:bg-slate-50 dark:hover:bg-slate-700"
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggle(option)}
                      className="h-4 w-4 shrink-0 rounded border-slate-300 accent-slate-900"
                    />
                    <span className={`truncate ${checked ? "font-medium" : ""}`}>
                      {renderOption(option)}
                    </span>
                  </label>
                );
              })
            )}
          </div>

          {selected.length > 0 && (
            <div className="flex items-center justify-between border-t border-slate-100 px-3 py-2 dark:border-slate-700">
              <span className="text-xs text-slate-500">Выбрано: {selected.length}</span>
              <button
                type="button"
                onClick={() => onChange([])}
                className="text-xs text-slate-500 hover:text-slate-900 dark:hover:text-slate-100"
              >
                Сбросить
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
