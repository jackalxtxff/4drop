import { useCallback, useEffect, useRef, useState } from "react";

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
  cards_update: "Обновление карточек",
  auto_cards: "Авто-создание карточек",
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
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Журнал задач: пагинация + фильтры. Держим отдельно от настроек.
  const [jobs, setJobs] = useState<SyncJob[]>([]);
  const [jobsTotal, setJobsTotal] = useState(0);
  const [jobKind, setJobKind] = useState("");
  const [jobStatus, setJobStatus] = useState("");
  const [jobLimit, setJobLimit] = useState(20);
  const [jobsLoading, setJobsLoading] = useState(false);

  // Активные задачи (в очереди/выполняются) — для состояния кнопок «Запустить»,
  // НЕзависимо от фильтров журнала: иначе фильтр по типу сломал бы индикацию.
  const [activeKinds, setActiveKinds] = useState<Set<string>>(new Set());

  // Автоподгрузка журнала при прокрутке — как в таблице товаров.
  const sentinelRef = useRef<HTMLDivElement>(null);

  const refreshActive = useCallback(async () => {
    if (!supplierId) return;
    const page = await api.get<import("../api").SyncJobPage>(
      `/suppliers/${supplierId}/sync/jobs?limit=30`,
    );
    setActiveKinds(
      new Set(
        page.items
          .filter((j) => j.status === "running" || j.status === "queued")
          .map((j) => j.kind),
      ),
    );
  }, [supplierId]);

  const jobsQuery = useCallback(
    (offset: number) => {
      const p = new URLSearchParams();
      if (jobKind) p.set("kind", jobKind);
      if (jobStatus) p.set("job_status", jobStatus);
      p.set("limit", String(jobLimit));
      p.set("offset", String(offset));
      return p.toString();
    },
    [jobKind, jobStatus, jobLimit],
  );

  // Актуальная длина списка для offset автоподгрузки — через ref, чтобы loadJobs
  // не пересоздавался на каждую догрузку (иначе IntersectionObserver перезапускается).
  const jobsCountRef = useRef(0);
  useEffect(() => {
    jobsCountRef.current = jobs.length;
  }, [jobs]);

  const loadJobs = useCallback(
    async (append: boolean) => {
      if (!supplierId) return;
      setJobsLoading(true);
      try {
        const offset = append ? jobsCountRef.current : 0;
        const page = await api.get<import("../api").SyncJobPage>(
          `/suppliers/${supplierId}/sync/jobs?${jobsQuery(offset)}`,
        );
        setJobsTotal(page.total);
        setJobs((prev) => {
          if (!append) return page.items;
          // Дедуп по id: список живой (планировщик добавляет задачи сверху),
          // offset сдвигается, и одна и та же задача может прийти дважды — иначе
          // React ругается на дубли ключей.
          const seen = new Set(prev.map((j) => j.id));
          return [...prev, ...page.items.filter((j) => !seen.has(j.id))];
        });
      } finally {
        setJobsLoading(false);
      }
    },
    [supplierId, jobsQuery],
  );

  const load = useCallback(async () => {
    if (!supplierId) return;
    const s = await api.get<SyncSettings>(`/suppliers/${supplierId}/sync/settings`);
    setSettings(s);
    setDraft(s);
  }, [supplierId]);

  useEffect(() => {
    void load();
    void refreshActive();
  }, [load, refreshActive]);

  // Первая страница журнала и перезагрузка при смене фильтров.
  useEffect(() => {
    void loadJobs(false);
  }, [loadJobs]);

  // Пока что-то выполняется, обновляем журнал и индикатор кнопок.
  useEffect(() => {
    if (activeKinds.size === 0) return;
    const t = setInterval(() => {
      void loadJobs(false);
      void refreshActive();
    }, 3000);
    return () => clearInterval(t);
  }, [activeKinds, loadJobs, refreshActive]);

  // Догружаем следующую порцию журнала, когда «сентинел» под таблицей попадает
  // в область видимости — бесконечная прокрутка вместо кнопки «показать ещё».
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && !jobsLoading && jobs.length < jobsTotal) {
          void loadJobs(true);
        }
      },
      { rootMargin: "200px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [jobsLoading, jobs.length, jobsTotal, loadJobs]);

  const dirty =
    draft && settings && JSON.stringify(draft) !== JSON.stringify(settings);

  // Пустой или «неартикульный» префикс бэкенд отклонит — не даём сохранить весь набор
  // настроек из-за одного поля.
  const prefixValid = !!draft && /^[A-Za-z0-9_-]+$/.test(draft.vendor_prefix);

  const save = async () => {
    if (!draft || !supplierId) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await api.put<SyncSettings>(`/suppliers/${supplierId}/sync/settings`, {
        catalog_interval_minutes: draft.catalog_interval_minutes,
        stocks_interval_minutes: draft.stocks_interval_minutes,
        push_interval_minutes: draft.push_interval_minutes,
        orders_interval_minutes: draft.orders_interval_minutes,
        orders_auto_supplier: draft.orders_auto_supplier,
        cards_update_interval_minutes: draft.cards_update_interval_minutes,
        auto_mode: draft.auto_mode,
        auto_cards_interval_minutes: draft.auto_cards_interval_minutes,
        auto_cards_batch_limit: draft.auto_cards_batch_limit,
        missing_strategy: draft.missing_strategy,
        stock_buffer: draft.stock_buffer,
        vendor_prefix: draft.vendor_prefix,
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
      // Сразу перечитываем: новая задача появляется «в очереди», кнопка меняет вид
      // и включается поллинг прогресса. Без этого до F5 ничего не менялось.
      await Promise.all([loadJobs(false), refreshActive()]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось запустить");
    }
  };

  // Индикатор кнопок — по активным задачам, а не по (возможно отфильтрованному) журналу.
  const isRunning = (kind: string) => activeKinds.has(kind);

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

        <Row
          title="Проверка заказов"
          hint="Забирает новые сборочные задания FBS с площадок, сопоставляет с товаром и определяет склад-источник по привязке. Вебхуков по заказам у WB нет, поэтому только опрос — а у FBS жёсткий дедлайн сборки, так что чаще лучше."
          value={draft.orders_interval_minutes}
          onChange={(v) => setDraft({ ...draft, orders_interval_minutes: v })}
          onRun={() => void run("orders")}
          running={isRunning("orders")}
          warning={
            draft.orders_interval_minutes === 0
              ? "Выключено. Новые заказы не подтянутся сами — только по кнопке на странице «Заказы»."
              : undefined
          }
        />

        <label className="flex items-start gap-2 px-1 pb-2 text-sm">
          <input
            type="checkbox"
            checked={draft.orders_auto_supplier}
            onChange={(e) => setDraft({ ...draft, orders_auto_supplier: e.target.checked })}
            className="mt-0.5 accent-slate-900"
          />
          <span>
            Сразу оформлять заказ у поставщика
            <span className="block text-xs text-slate-500 dark:text-slate-400">
              Найденный заказ автоматически уходит в 4tochki (тестовый контур,
              CreateOrder is_test) с адреса того FBS-склада, куда он пришёл. Без галочки
              заказ оформляется вручную кнопкой в таблице.
            </span>
          </span>
        </label>

        <Row
          title="Обновление карточек"
          hint="Досылает на площадку изменившиеся атрибуты (характеристики, название, картинки). Отправляет только то, что реально изменилось в 4tochki — каждое обновление проходит модерацию, поэтому редко."
          value={draft.cards_update_interval_minutes}
          onChange={(v) => setDraft({ ...draft, cards_update_interval_minutes: v })}
          onRun={() => void run("cards_update")}
          running={isRunning("cards_update")}
        />
      </section>

      <section className="rounded-xl border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-900">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="font-semibold tracking-tight">Автоматический режим</h2>
            <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">
              Товар, появившийся <b>в наличии</b> и ещё не заведённый на Wildberries,
              система сама создаст карточкой. Весь каталог с нулями не заливается —
              только то, что реально есть на складе. Заблокированные товары не трогаются.
            </p>
          </div>

          {/* Тумблер авто-режима. */}
          <button
            type="button"
            onClick={() => setDraft({ ...draft, auto_mode: !draft.auto_mode })}
            className={`relative mt-1 h-6 w-11 shrink-0 rounded-full transition ${
              draft.auto_mode ? "bg-slate-900 dark:bg-slate-100" : "bg-slate-300 dark:bg-slate-700"
            }`}
            aria-pressed={draft.auto_mode}
            aria-label="Автоматический режим"
          >
            <span
              className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition dark:bg-slate-900 ${
                draft.auto_mode ? "left-[1.375rem]" : "left-0.5"
              }`}
            />
          </button>
        </div>

        {draft.auto_mode && (
          <div className="mt-4 flex flex-wrap items-end gap-4 border-t border-slate-100 pt-4 dark:border-slate-800">
            <div>
              <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
                Проверять новинки
              </label>
              <select
                value={draft.auto_cards_interval_minutes}
                onChange={(e) =>
                  setDraft({ ...draft, auto_cards_interval_minutes: Number(e.target.value) })
                }
                className="mt-1 w-40 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-800"
              >
                {INTERVALS.filter((i) => i.value > 0).map((i) => (
                  <option key={i.value} value={i.value}>
                    {i.label}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
                Карточек за один проход
              </label>
              <input
                type="number"
                min={1}
                max={1000}
                value={draft.auto_cards_batch_limit}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    auto_cards_batch_limit: Math.max(1, Number(e.target.value) || 1),
                  })
                }
                className="mt-1 w-28 rounded-md border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
              />
            </div>

            <p className="pb-2 text-xs text-slate-500 dark:text-slate-400">
              Лимит бережёт от rate limit WB и завала модерации: за проход создаётся
              не больше указанного, остальное — в следующий.
            </p>

            <button
              onClick={() => void run("auto_cards")}
              disabled={isRunning("auto_cards")}
              className="ml-auto rounded-md border border-slate-300 px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-800"
            >
              {isRunning("auto_cards") ? "Выполняется…" : "Запустить сейчас"}
            </button>
          </div>
        )}
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

        {/* items-end: над инпутом есть лейбл, и по центру серая подсказка вставала бы
            выше поля. По нижнему краю они выравниваются. */}
        <div className="mt-4 flex flex-wrap items-end gap-4">
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

      <section className="rounded-xl border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-900">
        <h2 className="font-semibold tracking-tight">Префикс артикулов</h2>
        <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">
          С этого префикса начинается артикул (vendorCode) карточек, которые создаёт
          система. По нему она отличает свои карточки от товаров, которые вы завели в
          кабинете сами, — и не меняет чужие цены и остатки. Латиница, цифры, дефис и
          подчёркивание, до 16 символов.
        </p>

        {/* items-end: над инпутом есть лейбл, и по центру серая подсказка вставала бы
            выше поля. По нижнему краю они выравниваются. */}
        <div className="mt-4 flex flex-wrap items-end gap-4">
          <div>
            <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
              Префикс
            </label>
            <input
              value={draft.vendor_prefix}
              onChange={(e) => setDraft({ ...draft, vendor_prefix: e.target.value.trim() })}
              maxLength={16}
              placeholder="4D-"
              className="mt-1 w-32 rounded-md border border-slate-300 px-3 py-2 font-mono text-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
            />
          </div>

          {/* Показываем готовый артикул на живом примере: иначе неочевидно, что дефис
              (или его отсутствие) — часть префикса, а не разделитель от нас. */}
          <div className="rounded-lg bg-slate-50 px-4 py-2.5 text-sm text-slate-600 dark:bg-slate-800/60 dark:text-slate-400">
            {!/^[A-Za-z0-9_-]+$/.test(draft.vendor_prefix) ? (
              <span className="text-red-600 dark:text-red-400">
                {draft.vendor_prefix
                  ? "Допустимы только латиница, цифры, дефис и подчёркивание."
                  : "Префикс не может быть пустым: без него система не отличит свои карточки от чужих."}
              </span>
            ) : (
              <>
                Артикул товара 3480030506 будет{" "}
                <b className="font-mono text-slate-900 dark:text-slate-100">
                  {draft.vendor_prefix}3480030506
                </b>
              </>
            )}
          </div>
        </div>

        {draft.vendor_prefix !== settings?.vendor_prefix && (
          <p className="mt-3 rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-950 dark:text-amber-300">
            Новый префикс получат только карточки, созданные после сохранения. У уже
            созданных артикул на маркетплейсе не меняется — WB не даёт его переписать. Они
            останутся «своими»: прежний префикс запоминается, и система продолжит их
            узнавать, а не заведёт дубли.
          </p>
        )}

        {(settings?.vendor_prefix_history?.length ?? 0) > 0 && (
          <p className="mt-3 text-xs text-slate-400">
            Прежние префиксы, которые тоже считаются нашими:{" "}
            <span className="font-mono">{settings?.vendor_prefix_history.join(", ")}</span>
          </p>
        )}
      </section>

      <div className="flex items-center gap-3">
        <button
          onClick={() => void save()}
          disabled={!dirty || saving || !prefixValid}
          className="rounded-md bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-40 dark:bg-slate-100 dark:text-slate-900"
        >
          {saving ? "Сохраняем…" : "Сохранить настройки"}
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
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="font-semibold tracking-tight">
            Журнал задач{" "}
            <span className="font-normal text-slate-400">({jobsTotal})</span>
          </h2>

          <div className="flex flex-wrap items-center gap-2">
            <select
              value={jobKind}
              onChange={(e) => setJobKind(e.target.value)}
              className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-800"
            >
              <option value="">Все задачи</option>
              {Object.entries(KIND_LABEL).map(([k, label]) => (
                <option key={k} value={k}>
                  {label}
                </option>
              ))}
            </select>

            <select
              value={jobStatus}
              onChange={(e) => setJobStatus(e.target.value)}
              className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-800"
            >
              <option value="">Любой статус</option>
              {Object.entries(STATUS_LABEL).map(([k, label]) => (
                <option key={k} value={k}>
                  {label}
                </option>
              ))}
            </select>

            <select
              value={jobLimit}
              onChange={(e) => setJobLimit(Number(e.target.value))}
              title="Сколько показывать"
              className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-800"
            >
              {[10, 20, 50, 100].map((n) => (
                <option key={n} value={n}>
                  по {n}
                </option>
              ))}
            </select>
          </div>
        </div>

        {jobs.length === 0 ? (
          <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">
            {jobKind || jobStatus ? "Под фильтры ничего не подошло." : "Задач ещё не было."}
          </p>
        ) : (
          <>
            <div className="mt-4 overflow-x-auto">
              <table className="w-full min-w-[720px] text-sm">
                <thead>
                  <tr className="border-b border-slate-200 text-left text-xs font-medium text-slate-500 dark:border-slate-800 dark:text-slate-400">
                    <th className="pb-2 pr-4">Задача</th>
                    <th className="pb-2 pr-4">Статус</th>
                    <th className="pb-2 pr-4">Запуск</th>
                    <th className="pb-2 pr-4">Длительность</th>
                    <th className="pb-2 pr-4">Прогресс</th>
                    <th className="pb-2">Результат</th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.map((j) => {
                    const dur =
                      j.finished_at != null
                        ? Math.round(
                            (new Date(j.finished_at).getTime() -
                              new Date(j.started_at).getTime()) /
                              1000,
                          )
                        : null;
                    return (
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
                        <td className="py-2 pr-4 whitespace-nowrap tabular-nums text-slate-500 dark:text-slate-400">
                          {dur != null ? `${dur} с` : "—"}
                        </td>
                        <td className="py-2 pr-4 tabular-nums text-slate-500 dark:text-slate-400">
                          {j.total > 0 ? `${j.processed} / ${j.total}` : "—"}
                        </td>
                        <td className="py-2 text-slate-500 dark:text-slate-400">
                          {j.message ?? "—"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {/* Сентинел автоподгрузки: пока он виден и есть ещё записи — грузим. */}
            {jobs.length < jobsTotal && (
              <div ref={sentinelRef} className="mt-4 text-center text-sm text-slate-400">
                {jobsLoading ? "Загружаем…" : `Ещё ${jobsTotal - jobs.length}`}
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}
