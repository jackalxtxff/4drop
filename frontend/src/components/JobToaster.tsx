import { useEffect, useRef, useState } from "react";

import { api, type SyncJob } from "../api";

// Человекочитаемые названия фоновых скриптов (kind в SyncJob).
const KIND_LABEL: Record<string, string> = {
  catalog: "Обновление каталога",
  stocks: "Обновление цен и остатков",
  push: "Отправка на площадки",
  cards: "Создание карточек",
  cards_update: "Обновление карточек",
  auto_cards: "Авто-создание карточек",
};

type ToastKind = "info" | "success" | "error";

interface Toast {
  id: number;
  kind: ToastKind;
  title: string;
  text?: string;
}

const TOAST_STYLE: Record<ToastKind, string> = {
  info: "border-blue-500 bg-white dark:bg-slate-900",
  success: "border-emerald-500 bg-white dark:bg-slate-900",
  error: "border-red-500 bg-white dark:bg-slate-900",
};

const ICON: Record<ToastKind, string> = { info: "▶", success: "✓", error: "✕" };
const ICON_COLOR: Record<ToastKind, string> = {
  info: "text-blue-600 dark:text-blue-400",
  success: "text-emerald-600 dark:text-emerald-400",
  error: "text-red-600 dark:text-red-400",
};

/** Глобальные уведомления о фоновых скриптах: всплывают справа сверху при запуске
 *  любой задачи (с любой страницы и по расписанию) и при её завершении.
 *
 *  Работает опросом списка задач: сравниваем с виденными ранее и всплываем на новых
 *  и на смене статуса. Первый опрос заполняет «виденные» молча — чтобы старые задачи
 *  при открытии не сыпали уведомлениями. */
export function JobToaster({ supplierId }: { supplierId: number | undefined }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const seen = useRef<Map<number, string>>(new Map());
  const inited = useRef(false);
  const counter = useRef(0);

  const remove = (id: number) => setToasts((ts) => ts.filter((t) => t.id !== id));

  const add = (t: Omit<Toast, "id">) => {
    const id = ++counter.current;
    setToasts((ts) => [{ ...t, id }, ...ts].slice(0, 5));
    setTimeout(() => remove(id), 6000);
  };

  useEffect(() => {
    if (!supplierId) return;
    seen.current = new Map();
    inited.current = false;
    let alive = true;

    const poll = async () => {
      let jobs: SyncJob[];
      try {
        jobs = await api.get<SyncJob[]>(`/suppliers/${supplierId}/products/jobs`);
      } catch {
        return; // сеть моргнула — молча ждём следующий опрос
      }
      if (!alive) return;

      // Первый опрос — только запомнить текущие задачи, без уведомлений.
      if (!inited.current) {
        for (const j of jobs) seen.current.set(j.id, j.status);
        inited.current = true;
        return;
      }

      // Задачи приходят от новых к старым — идём от старых к новым, чтобы порядок
      // уведомлений был естественным.
      for (const j of [...jobs].reverse()) {
        const prev = seen.current.get(j.id);
        const label = KIND_LABEL[j.kind] ?? j.kind;
        if (prev === undefined) {
          if (j.status === "queued" || j.status === "running") {
            add({ kind: "info", title: `${label} — запущено` });
          } else if (j.status === "done") {
            add({ kind: "success", title: `${label} — готово`, text: j.message ?? undefined });
          } else if (j.status === "failed") {
            add({ kind: "error", title: `${label} — ошибка`, text: j.message ?? undefined });
          }
        } else if (prev !== j.status) {
          if (j.status === "done") {
            add({ kind: "success", title: `${label} — готово`, text: j.message ?? undefined });
          } else if (j.status === "failed") {
            add({ kind: "error", title: `${label} — ошибка`, text: j.message ?? undefined });
          }
        }
        seen.current.set(j.id, j.status);
      }
    };

    void poll();
    const timer = setInterval(poll, 3000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [supplierId]);

  if (toasts.length === 0) return null;

  return (
    <div className="pointer-events-none fixed right-4 top-4 z-[100] flex w-80 flex-col gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`pointer-events-auto rounded-lg border-l-4 px-4 py-3 shadow-lg ring-1 ring-slate-200 dark:ring-slate-700 ${TOAST_STYLE[t.kind]}`}
        >
          <div className="flex items-start gap-2">
            <span className={`text-sm font-bold ${ICON_COLOR[t.kind]}`}>{ICON[t.kind]}</span>
            <div className="min-w-0 flex-1">
              <div className="text-sm font-medium text-slate-900 dark:text-slate-100">
                {t.title}
              </div>
              {t.text && (
                <div className="mt-0.5 line-clamp-3 text-xs text-slate-500 dark:text-slate-400">
                  {t.text}
                </div>
              )}
            </div>
            <button
              onClick={() => remove(t.id)}
              className="shrink-0 text-slate-400 hover:text-slate-700 dark:hover:text-slate-200"
              aria-label="Закрыть"
            >
              ×
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
