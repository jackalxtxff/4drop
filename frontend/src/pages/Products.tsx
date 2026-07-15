import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";

import {
  api,
  type Credential,
  type Facets,
  type Product,
  type ProductPage,
  type SortField,
  type SyncJob,
} from "../api";
import { useSupplier } from "../components/Layout";
import { MultiSelect } from "../components/MultiSelect";
import { StockTooltip } from "../components/StockTooltip";
import { IntegrateDialog } from "../components/IntegrateDialog";

const PAGE_SIZE = 200;
const ROW_HEIGHT = 56;

// Бейдж площадки: WB фиолетовый, Ozon голубой. Приглушаем, если карточка ещё не
// активна (на модерации / ошибка), чтобы «активен» читался с одного взгляда.
const PLATFORM_BADGE: Record<string, { label: string; active: string; muted: string }> = {
  wb: {
    label: "WB",
    active: "bg-violet-100 text-violet-700 dark:bg-violet-950 dark:text-violet-300",
    muted:
      "bg-violet-50 text-violet-400 ring-1 ring-inset ring-violet-200 dark:bg-violet-950/40 dark:text-violet-500 dark:ring-violet-900",
  },
  ozon: {
    label: "Ozon",
    active: "bg-sky-100 text-sky-700 dark:bg-sky-950 dark:text-sky-300",
    muted:
      "bg-sky-50 text-sky-400 ring-1 ring-inset ring-sky-200 dark:bg-sky-950/40 dark:text-sky-500 dark:ring-sky-900",
  },
};

const LINK_STATUS_LABEL: Record<string, string> = {
  active: "активна",
  pending: "на модерации",
  rejected: "отклонена",
  error: "ошибка",
  none: "нет",
};

const INTEGRATION_FILTER_OPTIONS = [
  { value: "wb", label: "На Wildberries" },
  { value: "ozon", label: "На Ozon" },
  { value: "none", label: "Не интегрирован" },
];

/** Сезон и тип 4tochki отдают кодами. Значения сверены с фактическим каталогом. */
const SEASON_LABEL: Record<string, string> = { s: "Лето", w: "Зима", u: "Всесезонная" };

const TYPE_LABEL: Record<string, string> = {
  car: "Легковая",
  cartruck: "Легкогрузовая",
  vned: "Внедорожная",
  truck: "Грузовая",
  selhoz: "Сельхоз",
  specteh: "Спецтехника",
  loader: "Погрузчик",
  quadbike: "Квадроцикл",
  moto: "Мото",
  logging: "Лесовозная",
};

const season = (c: string | null) => (c === null ? "—" : (SEASON_LABEL[c] ?? c));
const tyreType = (c: string | null) => (c === null ? "—" : (TYPE_LABEL[c] ?? c));

/** Числа приходят как "16.00" — показываем "16". */
const num = (v: string | null) =>
  v === null ? "—" : String(Number(v)).replace(/\.0+$/, "");

const DEFAULT_SORT: SortField = "cae";
const DEFAULT_ORDER: Order = "asc";

type Order = "asc" | "desc";

interface Filters {
  q: string;
  brand: string[];
  season: string[];
  tyre_type: string[];
  constr: string[];
  camera: string[];
  width: string[];
  height: string[];
  diameter: string[];
  integration: string[];
  inStock: boolean;
  priceMin: string;
  priceMax: string;
}

const EMPTY: Filters = {
  q: "",
  brand: [],
  season: [],
  tyre_type: [],
  constr: [],
  camera: [],
  width: [],
  height: [],
  diameter: [],
  integration: [],
  inStock: false,
  priceMin: "",
  priceMax: "",
};

// 15 колонок в ширину экрана не влезают, поэтому таблица скроллится горизонтально;
// заголовок и строки лежат в одном контейнере, чтобы ехать вместе.
const GRID =
  "grid-cols-[40px_110px_minmax(240px,1fr)_120px_64px_64px_72px_60px_110px_100px_64px_56px_80px_96px_124px]";

interface Column {
  title: string;
  sort?: SortField;
  align?: "right";
}

const COLUMNS: Column[] = [
  { title: "CAE", sort: "cae" },
  { title: "Наименование", sort: "name" },
  { title: "Тип", sort: "tyre_type" },
  { title: "Ширина", sort: "width" },
  { title: "Профиль", sort: "height" },
  { title: "Диаметр", sort: "diameter" },
  { title: "Констр.", sort: "constr" },
  { title: "Камера", sort: "camera" },
  { title: "Сезон", sort: "season" },
  { title: "Шум", sort: "noise" },
  { title: "Усил." },
  { title: "Остаток", sort: "total_rest", align: "right" },
  { title: "Закупка, ₽", sort: "min_price", align: "right" },
  { title: "Интеграция", sort: "integration_status" },
];

function buildQuery(f: Filters, sort: SortField, order: Order, page: number): string {
  const p = new URLSearchParams();
  if (f.q) p.set("q", f.q);
  (
    ["brand", "season", "tyre_type", "constr", "camera", "width", "height", "diameter", "integration"] as const
  ).forEach((key) => f[key].forEach((v) => p.append(key, v)));
  if (f.inStock) p.set("in_stock", "true");
  if (f.priceMin) p.set("price_min", f.priceMin);
  if (f.priceMax) p.set("price_max", f.priceMax);
  p.set("sort", sort);
  p.set("order", order);
  p.set("page", String(page));
  p.set("page_size", String(PAGE_SIZE));
  return p.toString();
}

export function ProductsPage() {
  const { current, reload } = useSupplier();
  const supplierId = current?.id;

  const [filters, setFilters] = useState<Filters>(EMPTY);
  const [draft, setDraft] = useState("");
  const [facets, setFacets] = useState<Facets | null>(null);

  const [sort, setSort] = useState<SortField>(DEFAULT_SORT);
  const [order, setOrder] = useState<Order>(DEFAULT_ORDER);

  const [items, setItems] = useState<Product[]>([]);
  const [total, setTotal] = useState(0);
  const [summary, setSummary] = useState({ inStock: 0, rest: 0, buffer: 0 });
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);

  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [job, setJob] = useState<SyncJob | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [confirm, setConfirm] = useState<string[] | null>(null);
  const [integrating, setIntegrating] = useState(false);
  const [creds, setCreds] = useState<Credential[]>([]);

  const [zoom, setZoom] = useState<{ src: string; top: number; left: number } | null>(null);
  const [stockHover, setStockHover] = useState<{
    productId: number;
    totalRest: number;
    marketplaceRest: number;
    top: number;
    left: number;
  } | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const t = setTimeout(() => setFilters((f) => ({ ...f, q: draft })), 300);
    return () => clearTimeout(t);
  }, [draft]);

  const fetchPage = useCallback(
    async (pageNo: number, append: boolean) => {
      if (!supplierId) return;
      setLoading(true);
      setError(null);
      try {
        const data = await api.get<ProductPage>(
          `/suppliers/${supplierId}/products?${buildQuery(filters, sort, order, pageNo)}`,
        );
        setTotal(data.total);
        setSummary({
          inStock: data.in_stock_count,
          rest: data.total_rest,
          buffer: data.stock_buffer,
        });
        setItems((prev) => (append ? [...prev, ...data.items] : data.items));
        setPage(pageNo);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Не удалось загрузить товары");
      } finally {
        setLoading(false);
      }
    },
    [supplierId, filters, sort, order],
  );

  useEffect(() => {
    setSelected(new Set());
    scrollRef.current?.scrollTo({ top: 0 });
    void fetchPage(1, false);
  }, [fetchPage]);

  useEffect(() => {
    if (!supplierId) return;
    void api.get<Facets>(`/suppliers/${supplierId}/products/facets`).then(setFacets);
    void api.get<Credential[]>(`/suppliers/${supplierId}/connections`).then(setCreds);
  }, [supplierId, current?.catalog_synced_at]);

  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
  });

  const virtualRows = virtualizer.getVirtualItems();
  useEffect(() => {
    const last = virtualRows.at(-1);
    if (!last || loading) return;
    if (last.index >= items.length - 20 && items.length < total) {
      void fetchPage(page + 1, true);
    }
  }, [virtualRows, items.length, total, loading, page, fetchPage]);

  /** Клик по заголовку: по возрастанию → по убыванию → сортировка по умолчанию. */
  const onSort = (field: SortField) => {
    if (sort !== field) {
      setSort(field);
      setOrder("asc");
    } else if (order === "asc") {
      setOrder("desc");
    } else {
      setSort(DEFAULT_SORT);
      setOrder(DEFAULT_ORDER);
    }
  };

  const toggle = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const allLoadedSelected = items.length > 0 && items.every((i) => selected.has(i.id));

  const syncCatalog = async () => {
    if (!supplierId) return;
    setError(null);
    try {
      setJob(await api.post<SyncJob>(`/suppliers/${supplierId}/products/sync`));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось запустить синхронизацию");
    }
  };

  const integrate = async (platforms: string[]) => {
    if (!supplierId || selected.size === 0) return;
    setIntegrating(true);
    setError(null);
    try {
      setJob(
        await api.post<SyncJob>(`/suppliers/${supplierId}/products/integrate`, {
          product_ids: [...selected],
          platforms,
        }),
      );
      setConfirm(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось запустить интеграцию");
    } finally {
      setIntegrating(false);
    }
  };

  useEffect(() => {
    if (!job || !supplierId) return;
    if (job.status === "done" || job.status === "failed") return;

    const t = setInterval(async () => {
      const jobs = await api.get<SyncJob[]>(`/suppliers/${supplierId}/products/jobs`);
      const fresh = jobs.find((j) => j.id === job.id);
      if (!fresh) return;
      setJob(fresh);
      if (fresh.status === "done" || fresh.status === "failed") {
        clearInterval(t);
        if (fresh.status === "done") await Promise.all([fetchPage(1, false), reload()]);
      }
    }, 2000);

    return () => clearInterval(t);
  }, [job, supplierId, fetchPage, reload]);

  const syncedAt = useMemo(
    () =>
      current?.catalog_synced_at
        ? new Date(current.catalog_synced_at).toLocaleString("ru")
        : "никогда",
    [current?.catalog_synced_at],
  );

  const activeFilters =
    (["brand", "season", "tyre_type", "constr", "camera", "width", "height", "diameter", "integration"] as const)
      .reduce((n, k) => n + filters[k].length, 0) +
    (filters.inStock ? 1 : 0) +
    (filters.priceMin ? 1 : 0) +
    (filters.priceMax ? 1 : 0) +
    (filters.q ? 1 : 0);

  const cell = "truncate text-slate-600 dark:text-slate-400";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Товары</h1>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            Каталог 4tochki поставщика «{current?.name}». Последняя синхронизация: {syncedAt}.
          </p>
        </div>

        <button
          onClick={() => void syncCatalog()}
          disabled={job?.status === "running" || job?.status === "queued"}
          className="rounded-md border border-slate-300 px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-800"
        >
          Обновить каталог из 4tochki
        </button>
      </div>

      {job && (
        <div
          className={`rounded-lg px-4 py-3 text-sm ${
            job.status === "failed"
              ? "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300"
              : job.status === "done"
                ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300"
                : "bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
          }`}
        >
          <span className="font-medium">
            {job.kind === "catalog" ? "Синхронизация каталога" : "Создание карточек"}:{" "}
            {job.status === "queued" && "в очереди"}
            {job.status === "running" && "выполняется"}
            {job.status === "done" && "готово"}
            {job.status === "failed" && "ошибка"}
          </span>
          {job.total > 0 && job.status === "running" && (
            <span className="ml-2">
              {job.processed} из {job.total}
            </span>
          )}
          {job.message && <span className="ml-2">— {job.message}</span>}
        </div>
      )}

      {error && (
        <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">
          {error}
        </p>
      )}

      <div className="flex flex-wrap items-end gap-3 rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
        <div className="min-w-52 grow">
          <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
            Поиск
          </label>
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="CAE, бренд, модель, наименование"
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-900 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 dark:focus:border-slate-400"
          />
        </div>

        <MultiSelect
          label="Бренд"
          options={facets?.brands ?? []}
          selected={filters.brand}
          onChange={(v) => setFilters({ ...filters, brand: v })}
          placeholder="Все"
          className="w-44"
        />
        <MultiSelect
          label="Тип"
          options={facets?.tyre_types ?? []}
          selected={filters.tyre_type}
          onChange={(v) => setFilters({ ...filters, tyre_type: v })}
          renderOption={tyreType}
          searchable={false}
          placeholder="Любой"
          className="w-40"
        />
        <MultiSelect
          label="Ширина"
          options={facets?.widths ?? []}
          selected={filters.width}
          onChange={(v) => setFilters({ ...filters, width: v })}
          placeholder="Любая"
          className="w-28"
        />
        <MultiSelect
          label="Профиль"
          options={facets?.heights ?? []}
          selected={filters.height}
          onChange={(v) => setFilters({ ...filters, height: v })}
          placeholder="Любой"
          className="w-28"
        />
        <MultiSelect
          label="Диаметр"
          options={facets?.diameters ?? []}
          selected={filters.diameter}
          onChange={(v) => setFilters({ ...filters, diameter: v })}
          placeholder="Любой"
          className="w-28"
        />
        <MultiSelect
          label="Сезон"
          options={facets?.seasons ?? []}
          selected={filters.season}
          onChange={(v) => setFilters({ ...filters, season: v })}
          renderOption={season}
          searchable={false}
          placeholder="Любой"
          className="w-36"
        />
        <MultiSelect
          label="Констр."
          options={facets?.constrs ?? []}
          selected={filters.constr}
          onChange={(v) => setFilters({ ...filters, constr: v })}
          searchable={false}
          placeholder="Любая"
          className="w-24"
        />
        <MultiSelect
          label="Камера"
          options={facets?.cameras ?? []}
          selected={filters.camera}
          onChange={(v) => setFilters({ ...filters, camera: v })}
          placeholder="Любая"
          className="w-36"
        />
        <MultiSelect
          label="Интеграция"
          options={INTEGRATION_FILTER_OPTIONS.map((o) => o.value)}
          selected={filters.integration}
          onChange={(v) => setFilters({ ...filters, integration: v })}
          renderOption={(v) =>
            INTEGRATION_FILTER_OPTIONS.find((o) => o.value === v)?.label ?? v
          }
          searchable={false}
          placeholder="Любая"
          className="w-44"
        />

        <div>
          <label className="block text-xs font-medium text-slate-500 dark:text-slate-400">
            Цена, ₽
          </label>
          <div className="mt-1 flex gap-1">
            <input
              value={filters.priceMin}
              onChange={(e) => setFilters({ ...filters, priceMin: e.target.value })}
              placeholder="от"
              className="w-20 rounded-md border border-slate-300 px-2 py-2 text-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
            />
            <input
              value={filters.priceMax}
              onChange={(e) => setFilters({ ...filters, priceMax: e.target.value })}
              placeholder="до"
              className="w-20 rounded-md border border-slate-300 px-2 py-2 text-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
            />
          </div>
        </div>

        <label className="flex items-center gap-2 pb-2 text-sm">
          <input
            type="checkbox"
            checked={filters.inStock}
            onChange={(e) => setFilters({ ...filters, inStock: e.target.checked })}
            className="accent-slate-900"
          />
          Только в наличии
        </label>

        {activeFilters > 0 && (
          <button
            onClick={() => {
              setDraft("");
              setFilters(EMPTY);
            }}
            className="pb-2 text-sm text-slate-500 hover:text-slate-800 dark:hover:text-slate-200"
          >
            Сбросить ({activeFilters})
          </button>
        )}
      </div>

      <div className="flex items-center justify-between">
        {/* Сводка считается по текущим фильтрам, а не по всему каталогу. */}
        <p className="text-sm text-slate-500 dark:text-slate-400">
          Найдено:{" "}
          <span className="font-medium text-slate-900 dark:text-slate-100">
            {total.toLocaleString("ru")}
          </span>
          {" · "}моделей в наличии:{" "}
          <span
            className={`font-medium ${
              summary.inStock > 0
                ? "text-slate-900 dark:text-slate-100"
                : "text-amber-600 dark:text-amber-400"
            }`}
          >
            {summary.inStock.toLocaleString("ru")}
          </span>
          {" · "}остаток:{" "}
          <span className="font-medium text-slate-900 dark:text-slate-100">
            {summary.rest.toLocaleString("ru")} шт.
          </span>
          {summary.buffer > 0 && (
            <span className="text-slate-400 dark:text-slate-500">
              {" "}
              (буфер {summary.buffer} шт. на позицию)
            </span>
          )}
          {selected.size > 0 && (
            <>
              {" · "}выбрано:{" "}
              <span className="font-medium text-slate-900 dark:text-slate-100">
                {selected.size}
              </span>
            </>
          )}
        </p>

        {selected.size > 0 && (
          <div className="flex items-center gap-2">
            <span className="text-sm text-slate-500 dark:text-slate-400">Интегрировать в:</span>
            <button
              onClick={() => setConfirm(["wb"])}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
            >
              Wildberries
            </button>
            <button
              onClick={() => setConfirm(["ozon"])}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
            >
              Ozon
            </button>
            <button
              onClick={() => setConfirm(["wb", "ozon"])}
              className="rounded-md bg-slate-900 px-3 py-1.5 text-sm text-white dark:bg-slate-100 dark:text-slate-900"
            >
              Обе площадки
            </button>
          </div>
        )}
      </div>

      <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="min-w-[1360px]">
          <div
            className={`grid ${GRID} items-center gap-3 border-b border-slate-200 bg-slate-50 px-4 py-2.5 text-xs font-medium text-slate-500 dark:border-slate-800 dark:bg-slate-800/50 dark:text-slate-400`}
          >
            <input
              type="checkbox"
              checked={allLoadedSelected}
              onChange={() =>
                setSelected(allLoadedSelected ? new Set() : new Set(items.map((i) => i.id)))
              }
              className="accent-slate-900"
            />

            {COLUMNS.map((col) => {
              const active = col.sort === sort;
              return (
                <button
                  key={col.title}
                  onClick={() => col.sort && onSort(col.sort)}
                  title="Клик: по возрастанию → по убыванию → без сортировки"
                  className={`flex items-center gap-1 text-xs font-medium transition hover:text-slate-900 dark:hover:text-slate-100 ${
                    col.align === "right" ? "justify-end" : ""
                  } ${active ? "text-slate-900 dark:text-slate-100" : ""}`}
                >
                  <span className="truncate">{col.title}</span>
                  <span className="w-2.5 shrink-0 text-[10px] leading-none">
                    {active ? (order === "asc" ? "▲" : "▼") : ""}
                  </span>
                </button>
              );
            })}
          </div>

          <div ref={scrollRef} className="h-[calc(100vh-28rem)] min-h-80 overflow-y-auto">
            {items.length === 0 && !loading ? (
              <p className="px-4 py-16 text-center text-sm text-slate-500 dark:text-slate-400">
                {activeFilters > 0
                  ? "Под фильтры ничего не подошло."
                  : "Товаров нет. Задайте доступы к 4tochki и нажмите «Обновить каталог из 4tochki»."}
              </p>
            ) : (
              <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
                {virtualRows.map((row) => {
                  const p = items[row.index];
                  const checked = selected.has(p.id);
                  return (
                    <div
                      key={p.id}
                      onClick={() => toggle(p.id)}
                      className={`absolute left-0 grid w-full cursor-pointer ${GRID} items-center gap-3 border-b border-slate-100 px-4 text-sm hover:bg-slate-50 dark:border-slate-800 dark:hover:bg-slate-800/50 ${
                        checked ? "bg-slate-50 dark:bg-slate-800/50" : ""
                      }`}
                      style={{ height: row.size, transform: `translateY(${row.start}px)` }}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggle(p.id)}
                        onClick={(e) => e.stopPropagation()}
                        className="accent-slate-900"
                      />

                      <span className="truncate font-mono text-xs text-slate-600 dark:text-slate-400">
                        {p.cae}
                      </span>

                      <span className="flex min-w-0 items-center gap-2">
                        {p.img_small && (
                          <img
                            src={p.img_small}
                            alt=""
                            loading="lazy"
                            onMouseEnter={(e) => {
                              const r = e.currentTarget.getBoundingClientRect();
                              setZoom({
                                src: p.img_big ?? p.img_small!,
                                top: r.top,
                                left: r.right,
                              });
                            }}
                            onMouseLeave={() => setZoom(null)}
                            className="h-9 w-9 shrink-0 rounded object-contain transition hover:scale-110"
                          />
                        )}
                        <span className="min-w-0">
                          <span className="block truncate">{p.name ?? "—"}</span>
                          <span className="block truncate text-xs text-slate-500 dark:text-slate-400">
                            {p.brand} {p.model}
                          </span>
                        </span>
                      </span>

                      <span className={cell}>{tyreType(p.tyre_type)}</span>
                      <span className={cell}>{num(p.width)}</span>
                      <span className={cell}>{num(p.height)}</span>
                      <span className={cell}>
                        {p.diameter ? `R${num(p.diameter)}` : "—"}
                      </span>
                      <span className={cell}>{p.constr ?? "—"}</span>
                      <span className={cell} title={p.camera ?? undefined}>
                        {p.camera ?? "—"}
                      </span>
                      <span className={cell}>
                        {season(p.season)}
                        {p.thorn && <span className="ml-1 text-xs text-slate-400">шип</span>}
                      </span>
                      <span className={cell}>{p.noise ?? "—"}</span>
                      <span className={cell}>{p.strengthening ? "да" : "—"}</span>

                      {/* Наведение на остаток показывает разбивку по складам:
                          агрегат сам по себе не объясняет, откуда он и почему такой. */}
                      <span
                        onMouseEnter={(e) => {
                          const r = e.currentTarget.getBoundingClientRect();
                          setStockHover({
                            productId: p.id,
                            totalRest: p.total_rest,
                            marketplaceRest: p.marketplace_rest,
                            top: r.bottom + 6,
                            left: r.left - 200,
                          });
                        }}
                        onMouseLeave={() => setStockHover(null)}
                        className={`cursor-help text-right tabular-nums ${
                          p.total_rest > 0
                            ? "text-slate-900 underline decoration-dotted underline-offset-4 dark:text-slate-100"
                            : "text-slate-400 dark:text-slate-600"
                        }`}
                      >
                        {p.total_rest}
                      </span>

                      <span className="text-right tabular-nums">
                        {p.min_price ? Number(p.min_price).toLocaleString("ru") : "—"}
                      </span>

                      <span className="flex flex-wrap gap-1">
                        {p.integrations.length === 0 ? (
                          <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-500 dark:bg-slate-800 dark:text-slate-400">
                            Не интегрирован
                          </span>
                        ) : (
                          p.integrations.map((link) => {
                            const badge = PLATFORM_BADGE[link.platform];
                            if (!badge) return null;
                            const active = link.status === "active";
                            return (
                              <span
                                key={link.platform}
                                title={
                                  `${badge.label}: ${LINK_STATUS_LABEL[link.status] ?? link.status}` +
                                  (link.status_message ? ` — ${link.status_message}` : "")
                                }
                                className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                                  active ? badge.active : badge.muted
                                }`}
                              >
                                {badge.label}
                                {!active && (
                                  <span className="ml-1 opacity-70">
                                    {link.status === "pending" ? "⏳" : "!"}
                                  </span>
                                )}
                              </span>
                            );
                          })
                        )}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}

            {loading && (
              <p className="px-4 py-3 text-center text-sm text-slate-400">Загружаем…</p>
            )}
          </div>
        </div>
      </div>

      {/* Всплывающие слои рендерим вне таблицы: внутри контейнера со скроллом
          их обрезало бы по краю. */}
      {zoom && (
        <div
          className="pointer-events-none fixed z-50 rounded-lg border border-slate-200 bg-white p-2 shadow-xl dark:border-slate-700 dark:bg-slate-800"
          style={{
            top: Math.min(zoom.top - 80, window.innerHeight - 260),
            left: Math.min(zoom.left + 12, window.innerWidth - 260),
          }}
        >
          <img src={zoom.src} alt="" className="h-56 w-56 object-contain" />
        </div>
      )}

      {confirm && (
        <IntegrateDialog
          products={items.filter((p) => selected.has(p.id))}
          platforms={confirm}
          wbCred={creds.find((c) => c.platform === "wb")}
          busy={integrating}
          onConfirm={() => void integrate(confirm)}
          onClose={() => setConfirm(null)}
        />
      )}

      {stockHover && supplierId && (
        <StockTooltip
          supplierId={supplierId}
          productId={stockHover.productId}
          totalRest={stockHover.totalRest}
          marketplaceRest={stockHover.marketplaceRest}
          buffer={summary.buffer}
          top={stockHover.top}
          left={stockHover.left}
        />
      )}
    </div>
  );
}
