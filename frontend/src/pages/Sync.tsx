import { useCallback, useEffect, useState } from "react";

import { api, type SyncJob, type SyncSettings } from "../api";
import { useSupplier } from "../components/Layout";
import { FormulaField } from "../components/FormulaField";
import { CopyChip } from "../components/CopyChip";

/** Пресеты формул нашей цены продажи. */
const PRICE_PRESETS = [
  { label: "Наценка 25%", formula: "round_to(purchase * 1.25, 10)" },
  { label: "Наценка + комиссия 17%", formula: "round_to(purchase * 1.2 / (1 - 0.17), 10)" },
  { label: "Наценка + логистика", formula: "round_to(purchase * 1.2 + 250, 10)" },
  { label: "Не ниже розницы 4tochki", formula: "max(purchase * 1.2, rrp)" },
];

/** Пресеты формул цены до скидки — от нашей цены. */
const BEFORE_PRESETS = (v: string) => [
  { label: "Без скидки", formula: v },
  { label: "+40% к нашей", formula: `round_to(${v} * 1.4, 100)` },
  { label: "+70% к нашей", formula: `round_to(${v} * 1.7, 100)` },
  { label: "Фикс +3000 ₽", formula: `round_to(${v} + 3000, 100)` },
];

/** Пресеты интервалов. 0 = выключено. */
const INTERVALS: { value: number; label: string }[] = [
  { value: 0, label: "Выключено" },
  { value: 5, label: "5 минут" },
  { value: 10, label: "10 минут" },
  { value: 15, label: "15 минут" },
  { value: 30, label: "30 минут" },
  { value: 60, label: "1 час" },
  { value: 180, label: "3 часа" },
  { value: 360, label: "6 часов" },
  { value: 720, label: "12 часов" },
  { value: 1440, label: "1 раз в сутки" },
  { value: 4320, label: "1 раз в 3 дня" },
  { value: 10080, label: "1 раз в неделю" },
];

const KIND_LABEL: Record<string, string> = {
  catalog: "Каталог",
  stocks: "Цены и остатки",
  push: "Отправка на площадки",
  cards: "Создание карточек",
};

const STATUS_LABEL: Record<string, string> = {
  queued: "в очереди",
  running: "выполняется",
  done: "готово",
  failed: "ошибка",
};

const STATUS_STYLE: Record<string, string> = {
  queued: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400",
  running: "bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
  done: "bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
  failed: "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300",
};

function Row({
  title,
  hint,
  value,
  onChange,
  onRun,
  running,
  warning,
}: {
  title: string;
  hint: string;
  value: number;
  onChange: (v: number) => void;
  onRun: () => void;
  running: boolean;
  warning?: string;
}) {
  return (
    <div className="flex flex-wrap items-start gap-4 border-b border-slate-100 py-4 last:border-0 dark:border-slate-800">
      <div className="min-w-56 grow">
        <p className="font-medium">{title}</p>
        <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">{hint}</p>
        {warning && (
          <p className="mt-1.5 text-sm text-amber-700 dark:text-amber-400">{warning}</p>
        )}
      </div>

      <select
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="mt-1 w-44 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-800"
      >
        {INTERVALS.map((i) => (
          <option key={i.value} value={i.value}>
            {i.label}
          </option>
        ))}
      </select>

      <button
        onClick={onRun}
        disabled={running}
        className="mt-1 rounded-md border border-slate-300 px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-800"
      >
        {running ? "Выполняется…" : "Запустить сейчас"}
      </button>
    </div>
  );
}

export function SyncPage() {
  const { current } = useSupplier();
  const supplierId = current?.id;

  const [settings, setSettings] = useState<SyncSettings | null>(null);
  const [draft, setDraft] = useState<SyncSettings | null>(null);
  const [jobs, setJobs] = useState<SyncJob[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!supplierId) return;
    const [s, j] = await Promise.all([
      api.get<SyncSettings>(`/suppliers/${supplierId}/sync/settings`),
      api.get<SyncJob[]>(`/suppliers/${supplierId}/sync/jobs`),
    ]);
    setSettings(s);
    setDraft(s);
    setJobs(j);
  }, [supplierId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Пока что-то выполняется, подтягиваем журнал: иначе прогресс замрёт на экране.
  useEffect(() => {
    if (!jobs.some((j) => j.status === "running" || j.status === "queued")) return;
    const t = setInterval(() => void load(), 3000);
    return () => clearInterval(t);
  }, [jobs, load]);

  const dirty =
    draft && settings && JSON.stringify(draft) !== JSON.stringify(settings);

  const save = async () => {
    if (!draft || !supplierId) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await api.put<SyncSettings>(`/suppliers/${supplierId}/sync/settings`, {
        catalog_interval_minutes: draft.catalog_interval_minutes,
        stocks_interval_minutes: draft.stocks_interval_minutes,
        push_interval_minutes: draft.push_interval_minutes,
        missing_strategy: draft.missing_strategy,
        stock_buffer: draft.stock_buffer,
        wb_price_formula: draft.wb_price_formula,
        ozon_price_formula: draft.ozon_price_formula,
        wb_price_before_formula: draft.wb_price_before_formula,
        ozon_price_before_formula: draft.ozon_price_before_formula,
      });
      setSettings(saved);
      setDraft(saved);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сохранить");
    } finally {
      setSaving(false);
    }
  };

  const run = async (kind: string) => {
    if (!supplierId) return;
    setError(null);
    try {
      await api.post(`/suppliers/${supplierId}/sync/run/${kind}`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось запустить");
    }
  };

  const isRunning = (kind: string) =>
    jobs.some((j) => j.kind === kind && (j.status === "running" || j.status === "queued"));

  if (!draft) return <p className="text-sm text-slate-500">Загружаем…</p>;

  const stocksOff = draft.stocks_interval_minutes === 0;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Синхронизация</h1>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          Расписание фоновых обновлений для поставщика «{current?.name}». Задачи
          запускаются по времени последнего запуска, а не по сетке: длинная выгрузка
          не потянет за собой вторую.
        </p>
      </div>

      {error && (
        <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">
          {error}
        </p>
      )}

      <section className="rounded-xl border border-slate-200 bg-white px-6 dark:border-slate-800 dark:bg-slate-900">
        <Row
          title="Обновление каталога"
          hint="Полная выгрузка из 4tochki: карточки, атрибуты, картинки. Тяжёлая, десятки минут."
          value={draft.catalog_interval_minutes}
          onChange={(v) => setDraft({ ...draft, catalog_interval_minutes: v })}
          onRun={() => void run("catalog")}
          running={isRunning("catalog")}
        />

        <Row
          title="Цены и остатки"
          hint="Запрашивает цену и остаток по уже известным CAE. Быстрая — именно она защищает от оверселла."
          value={draft.stocks_interval_minutes}
          onChange={(v) => setDraft({ ...draft, stocks_interval_minutes: v })}
          onRun={() => void run("stocks")}
          running={isRunning("stocks")}
          warning={
            stocksOff
              ? "Выключено. Остаток на маркетплейсе будет расходиться с реальным — риск продать то, чего нет."
              : undefined
          }
        />

        <Row
          title="Отправка на площадки"
          hint="Пуш цен и остатков активных карточек с наценкой и буфером. Wildberries — работает; Ozon — в разработке."
          value={draft.push_interval_minutes}
          onChange={(v) => setDraft({ ...draft, push_interval_minutes: v })}
          onRun={() => void run("push")}
          running={isRunning("push")}
        />
      </section>

      <section className="rounded-xl border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-900">
        <h2 className="font-semibold tracking-tight">Товар пропал из выдачи 4tochki</h2>
        <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">
          Поиск 4tochki отдаёт только позиции с остатком, поэтому «пропал» почти всегда
          значит «кончился», а не «снят с продажи».
        </p>

        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          {[
            {
              value: "zero_stock",
              title: "Обнулить остаток",
              desc: "Карточка и связь с маркетплейсом сохраняются. Товар вернётся на склад — остаток восстановится сам, перезаливать карточку не нужно.",
              recommended: true,
            },
            {
              value: "delete",
              title: "Удалить из каталога",
              desc: "Товар исчезает из базы вместе с маппингом на WB и Ozon. При возврате на склад карточку придётся заводить заново.",
              recommended: false,
            },
          ].map((opt) => {
            const active = draft.missing_strategy === opt.value;
            return (
              <label
                key={opt.value}
                className={`cursor-pointer rounded-lg border p-4 transition ${
                  active
                    ? "border-slate-900 bg-slate-50 dark:border-slate-400 dark:bg-slate-800"
                    : "border-slate-200 hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
                }`}
              >
                <span className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="missing"
                    checked={active}
                    onChange={() =>
                      setDraft({
                        ...draft,
                        missing_strategy: opt.value as SyncSettings["missing_strategy"],
                      })
                    }
                    className="accent-slate-900"
                  />
                  <span className="font-medium">{opt.title}</span>
                  {opt.recommended && (
                    <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-xs text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300">
                      рекомендуется
                    </span>
                  )}
                </span>
                <span className="mt-1.5 block text-sm text-slate-500 dark:text-slate-400">
                  {opt.desc}
                </span>
              </label>
            );
          })}
        </div>
      </section>

      <section className="rounded-xl border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-900">
        <h2 className="font-semibold tracking-tight">Ценообразование</h2>
        <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">
          Формула цены продажи, отдельно по площадкам — у Wildberries и Ozon разные
          комиссии. Считается при создании карточек и при каждом пуше цен.
        </p>

        <div className="mt-3 rounded-lg bg-slate-50 p-3 text-xs text-slate-600 dark:bg-slate-800/60 dark:text-slate-400">
          <p className="font-medium text-slate-700 dark:text-slate-300">
            Переменные <span className="font-normal text-slate-400">— кликните, чтобы скопировать</span>
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <CopyChip value="purchase" hint="закупочная 4tochki" />
            <span className="text-slate-400">закупочная</span>
            <CopyChip value="rrp" hint="розница 4tochki" />
            <span className="text-slate-400">розница</span>
            <CopyChip value="weight" hint="вес, кг" />
            <span className="text-slate-400">вес</span>
            <CopyChip value="wb_price" hint="наша цена — только в формуле до скидки" />
            <CopyChip value="ozon_price" hint="наша цена — только в формуле до скидки" />
            <span className="text-slate-400">наша цена (в формуле до скидки)</span>
          </div>

          <p className="mt-3 font-medium text-slate-700 dark:text-slate-300">Функции</p>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <CopyChip value="round_to(x, 10)" hint="округлить вверх до кратного" kind="func" />
            <CopyChip value="ceil(x)" hint="вверх" kind="func" />
            <CopyChip value="floor(x)" hint="вниз" kind="func" />
            <CopyChip value="round(x)" hint="обычное округление" kind="func" />
            <CopyChip value="min(x, y)" kind="func" />
            <CopyChip value="max(x, y)" kind="func" />
          </div>

          <p className="mt-3 text-slate-500">
            Комиссию площадки удобно вносить делением:{" "}
            <CopyChip value="purchase * 1.2 / (1 - 0.17)" hint="наценка 20% и комиссия 17%" kind="func" />
          </p>
        </div>

        {supplierId && (
          <>
            <p className="mt-4 text-sm font-medium text-slate-700 dark:text-slate-300">
              Наша цена продажи
            </p>
            <div className="mt-2 grid gap-4 lg:grid-cols-2">
              <FormulaField
                supplierId={supplierId}
                label="Wildberries"
                value={draft.wb_price_formula}
                onChange={(v) => setDraft({ ...draft, wb_price_formula: v })}
                commissionHint="комиссия WB обычно 15–25%"
                presets={PRICE_PRESETS}
              />
              <FormulaField
                supplierId={supplierId}
                label="Ozon"
                value={draft.ozon_price_formula}
                onChange={(v) => setDraft({ ...draft, ozon_price_formula: v })}
                commissionHint="комиссия Ozon обычно 15–23%"
                presets={PRICE_PRESETS}
              />
            </div>

            <p className="mt-5 text-sm font-medium text-slate-700 dark:text-slate-300">
              Цена до скидки (зачёркнутая)
            </p>
            <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
              Считается от нашей цены — переменная <code>wb_price</code> /{" "}
              <code>ozon_price</code>. WB принимает не саму цену со скидкой, а процент —
              мы выводим его автоматически (возможна погрешность в 1 ₽ из-за целого
              процента). Ozon принимает обе цены числом.
            </p>
            <div className="mt-2 grid gap-4 lg:grid-cols-2">
              <FormulaField
                supplierId={supplierId}
                label="Wildberries — до скидки"
                value={draft.wb_price_before_formula}
                onChange={(v) => setDraft({ ...draft, wb_price_before_formula: v })}
                commissionHint="выше нашей цены"
                presets={BEFORE_PRESETS("wb_price")}
                priceVar="wb_price"
              />
              <FormulaField
                supplierId={supplierId}
                label="Ozon — до скидки"
                value={draft.ozon_price_before_formula}
                onChange={(v) => setDraft({ ...draft, ozon_price_before_formula: v })}
                commissionHint="выше нашей цены"
                presets={BEFORE_PRESETS("ozon_price")}
                priceVar="ozon_price"
              />
            </div>
          </>
        )}
      </section>

      <section className="rounded-xl border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-900">
        <h2 className="font-semibold tracking-tight">Буфер остатка</h2>
        <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">
          Сколько штук придержать и не выкладывать на маркетплейс. Страхует от
          продажи «последних» единиц, которые могут разобрать другие клиенты 4tochki
          между синхронизациями.
        </p>

        <div className="mt-4 flex flex-wrap items-center gap-4">
          <div>
            <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
              Буфер, шт.
            </label>
            <input
              type="number"
              min={0}
              value={draft.stock_buffer}
              onChange={(e) =>
                setDraft({ ...draft, stock_buffer: Math.max(0, Number(e.target.value) || 0) })
              }
              className="mt-1 w-28 rounded-md border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
            />
          </div>

          {/* Наглядный пример именно с текущим значением буфера — чтобы было видно,
              как оно сработает, до сохранения. */}
          <div className="rounded-lg bg-slate-50 px-4 py-2.5 text-sm text-slate-600 dark:bg-slate-800/60 dark:text-slate-400">
            {draft.stock_buffer === 0 ? (
              <>Буфер выключен: на маркетплейс уходит весь реальный остаток.</>
            ) : (
              <>
                Реальный остаток {draft.stock_buffer + 6} → на маркетплейсе{" "}
                <b className="text-slate-900 dark:text-slate-100">6</b>. Остаток{" "}
                {draft.stock_buffer} или меньше →{" "}
                <b className="text-slate-900 dark:text-slate-100">0</b>.
              </>
            )}
          </div>
        </div>
      </section>

      <div className="flex items-center gap-3">
        <button
          onClick={() => void save()}
          disabled={!dirty || saving}
          className="rounded-md bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-40 dark:bg-slate-100 dark:text-slate-900"
        >
          {saving ? "Сохраняем…" : "Сохранить расписание"}
        </button>
        {dirty && (
          <button
            onClick={() => setDraft(settings)}
            className="text-sm text-slate-500 hover:text-slate-800 dark:hover:text-slate-200"
          >
            Отменить
          </button>
        )}
      </div>

      <section className="rounded-xl border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-900">
        <h2 className="font-semibold tracking-tight">Журнал задач</h2>

        {jobs.length === 0 ? (
          <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">
            Задач ещё не было.
          </p>
        ) : (
          <div className="mt-4 overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead>
                <tr className="border-b border-slate-200 text-left text-xs font-medium text-slate-500 dark:border-slate-800 dark:text-slate-400">
                  <th className="pb-2 pr-4">Задача</th>
                  <th className="pb-2 pr-4">Статус</th>
                  <th className="pb-2 pr-4">Запуск</th>
                  <th className="pb-2 pr-4">Прогресс</th>
                  <th className="pb-2">Результат</th>
                </tr>
              </thead>
              <tbody>
                {jobs.map((j) => (
                  <tr
                    key={j.id}
                    className="border-b border-slate-100 last:border-0 dark:border-slate-800"
                  >
                    <td className="py-2 pr-4 whitespace-nowrap">
                      {KIND_LABEL[j.kind] ?? j.kind}
                    </td>
                    <td className="py-2 pr-4">
                      <span
                        className={`rounded-full px-2 py-0.5 text-xs ${
                          STATUS_STYLE[j.status] ?? STATUS_STYLE.queued
                        }`}
                      >
                        {STATUS_LABEL[j.status] ?? j.status}
                      </span>
                    </td>
                    <td className="py-2 pr-4 whitespace-nowrap text-slate-500 dark:text-slate-400">
                      {new Date(j.started_at).toLocaleString("ru")}
                    </td>
                    <td className="py-2 pr-4 tabular-nums text-slate-500 dark:text-slate-400">
                      {j.total > 0 ? `${j.processed} / ${j.total}` : "—"}
                    </td>
                    <td className="py-2 text-slate-500 dark:text-slate-400">
                      {j.message ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
