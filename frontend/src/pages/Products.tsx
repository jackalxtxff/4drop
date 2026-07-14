import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";

import { api, type Facets, type Product, type ProductPage, type SyncJob } from "../api";
import { useSupplier } from "../components/Layout";

const PAGE_SIZE = 200;
const ROW_HEIGHT = 56;

const INTEGRATION_LABEL: Record<Product["integration_status"], string> = {
  none: "Не интегрирован",
  pending: "На модерации",
  active: "Активен",
  rejected: "Отклонён",
  error: "Ошибка",
};

const INTEGRATION_STYLE: Record<Product["integration_status"], string> = {
  none: "bg-slate-100 text-slate-600",
  pending: "bg-amber-50 text-amber-700",
  active: "bg-emerald-50 text-emerald-700",
  rejected: "bg-red-50 text-red-700",
  error: "bg-red-50 text-red-700",
};

interface Filters {
  q: string;
  brand: string[];
  season: string[];
  inStock: boolean;
  priceMin: string;
  priceMax: string;
  integrationStatus: string[];
}

const EMPTY: Filters = {
  q: "",
  brand: [],
  season: [],
  inStock: false,
  priceMin: "",
  priceMax: "",
  integrationStatus: [],
};

function buildQuery(f: Filters, page: number): string {
  const p = new URLSearchParams();
  if (f.q) p.set("q", f.q);
  f.brand.forEach((b) => p.append("brand", b));
  f.season.forEach((s) => p.append("season", s));
  f.integrationStatus.forEach((s) => p.append("integration_status", s));
  if (f.inStock) p.set("in_stock", "true");
  if (f.priceMin) p.set("price_min", f.priceMin);
  if (f.priceMax) p.set("price_max", f.priceMax);
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

  const [items, setItems] = useState<Product[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);

  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [job, setJob] = useState<SyncJob | null>(null);
  const [error, setError] = useState<string | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);

  // Поиск дебаунсим: каталог в десятки тысяч позиций фильтруется на сервере,
  // и запрос на каждое нажатие клавиши положит и бэкенд, и таблицу.
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
          `/suppliers/${supplierId}/products?${buildQuery(filters, pageNo)}`,
        );
        setTotal(data.total);
        setItems((prev) => (append ? [...prev, ...data.items] : data.items));
        setPage(pageNo);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Не удалось загрузить товары");
      } finally {
        setLoading(false);
      }
    },
    [supplierId, filters],
  );

  useEffect(() => {
    setSelected(new Set());
    scrollRef.current?.scrollTo({ top: 0 });
    void fetchPage(1, false);
  }, [fetchPage]);

  useEffect(() => {
    if (!supplierId) return;
    void api.get<Facets>(`/suppliers/${supplierId}/products/facets`).then(setFacets);
  }, [supplierId]);

  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
  });

  // Догружаем следующую страницу, когда виртуализатор дошёл до хвоста загруженного.
  const virtualRows = virtualizer.getVirtualItems();
  useEffect(() => {
    const last = virtualRows.at(-1);
    if (!last || loading) return;
    if (last.index >= items.length - 20 && items.length < total) {
      void fetchPage(page + 1, true);
    }
  }, [virtualRows, items.length, total, loading, page, fetchPage]);

  const toggle = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const allLoadedSelected = items.length > 0 && items.every((i) => selected.has(i.id));
  const toggleAllLoaded = () => {
    setSelected(allLoadedSelected ? new Set() : new Set(items.map((i) => i.id)));
  };

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
    setError(null);
    try {
      setJob(
        await api.post<SyncJob>(`/suppliers/${supplierId}/products/integrate`, {
          product_ids: [...selected],
          platforms,
        }),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось запустить интеграцию");
    }
  };

  // Пока задача в работе — опрашиваем её; когда завершилась, обновляем каталог и счётчики.
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
        if (fresh.status === "done") {
          await Promise.all([fetchPage(1, false), reload()]);
        }
      }
    }, 2000);

    return () => clearInterval(t);
  }, [job, supplierId, fetchPage, reload]);

  const syncedAt = useMemo(() => {
    if (!current?.catalog_synced_at) return "никогда";
    return new Date(current.catalog_synced_at).toLocaleString("ru");
  }, [current?.catalog_synced_at]);

  const multi = (
    label: string,
    values: string[],
    selectedValues: string[],
    onChange: (v: string[]) => void,
  ) => (
    <div>
      <label className="block text-xs font-medium text-slate-500">{label}</label>
      <select
        multiple
        value={selectedValues}
        onChange={(e) =>
          onChange([...e.target.selectedOptions].map((o) => o.value))
        }
        className="mt-1 h-24 w-40 rounded-md border border-slate-300 px-2 py-1 text-sm"
      >
        {values.map((v) => (
          <option key={v} value={v}>
            {v}
          </option>
        ))}
      </select>
    </div>
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Товары</h1>
          <p className="mt-1 text-sm text-slate-500">
            Каталог 4tochki поставщика «{current?.name}». Последняя синхронизация: {syncedAt}.
          </p>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={() => void syncCatalog()}
            disabled={job?.status === "running" || job?.status === "queued"}
            className="rounded-md border border-slate-300 px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
          >
            Обновить каталог из 4tochki
          </button>
        </div>
      </div>

      {job && (
        <div
          className={`rounded-lg px-4 py-3 text-sm ${
            job.status === "failed"
              ? "bg-red-50 text-red-700"
              : job.status === "done"
                ? "bg-emerald-50 text-emerald-700"
                : "bg-blue-50 text-blue-700"
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

      {error && <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}

      <div className="flex flex-wrap items-end gap-4 rounded-xl border border-slate-200 bg-white p-4">
        <div className="grow">
          <label className="block text-xs font-medium text-slate-500">Поиск</label>
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="CAE, бренд, модель, наименование"
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-900 focus:outline-none"
          />
        </div>

        {facets && multi("Бренд", facets.brands, filters.brand, (v) =>
          setFilters({ ...filters, brand: v }),
        )}
        {facets && multi("Сезон", facets.seasons, filters.season, (v) =>
          setFilters({ ...filters, season: v }),
        )}

        <div>
          <label className="block text-xs font-medium text-slate-500">Цена, ₽</label>
          <div className="mt-1 flex gap-1">
            <input
              value={filters.priceMin}
              onChange={(e) => setFilters({ ...filters, priceMin: e.target.value })}
              placeholder="от"
              className="w-20 rounded-md border border-slate-300 px-2 py-2 text-sm"
            />
            <input
              value={filters.priceMax}
              onChange={(e) => setFilters({ ...filters, priceMax: e.target.value })}
              placeholder="до"
              className="w-20 rounded-md border border-slate-300 px-2 py-2 text-sm"
            />
          </div>
        </div>

        <label className="flex items-center gap-2 pb-2 text-sm">
          <input
            type="checkbox"
            checked={filters.inStock}
            onChange={(e) => setFilters({ ...filters, inStock: e.target.checked })}
          />
          Только в наличии
        </label>

        <button
          onClick={() => {
            setDraft("");
            setFilters(EMPTY);
          }}
          className="pb-2 text-sm text-slate-500 hover:text-slate-800"
        >
          Сбросить
        </button>
      </div>

      <div className="flex items-center justify-between">
        <p className="text-sm text-slate-500">
          Найдено: <span className="font-medium text-slate-900">{total.toLocaleString("ru")}</span>
          {selected.size > 0 && (
            <>
              {" · "}выбрано:{" "}
              <span className="font-medium text-slate-900">{selected.size}</span>
            </>
          )}
        </p>

        {selected.size > 0 && (
          <div className="flex items-center gap-2">
            <span className="text-sm text-slate-500">Интегрировать в:</span>
            <button
              onClick={() => void integrate(["wb"])}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50"
            >
              Wildberries
            </button>
            <button
              onClick={() => void integrate(["ozon"])}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50"
            >
              Ozon
            </button>
            <button
              onClick={() => void integrate(["wb", "ozon"])}
              className="rounded-md bg-slate-900 px-3 py-1.5 text-sm text-white"
            >
              Обе площадки
            </button>
          </div>
        )}
      </div>

      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white">
        <div className="grid grid-cols-[40px_120px_1fr_110px_100px_90px_110px_130px] items-center gap-3 border-b border-slate-200 bg-slate-50 px-4 py-2.5 text-xs font-medium text-slate-500">
          <input type="checkbox" checked={allLoadedSelected} onChange={toggleAllLoaded} />
          <span>CAE</span>
          <span>Наименование</span>
          <span>Типоразмер</span>
          <span>Сезон</span>
          <span className="text-right">Остаток</span>
          <span className="text-right">Закупка, ₽</span>
          <span>Интеграция</span>
        </div>

        <div ref={scrollRef} className="h-[calc(100vh-26rem)] min-h-80 overflow-auto">
          {items.length === 0 && !loading ? (
            <p className="px-4 py-16 text-center text-sm text-slate-500">
              Товаров нет. Задайте доступы к 4tochki и нажмите «Обновить каталог из 4tochki».
            </p>
          ) : (
            <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
              {virtualRows.map((row) => {
                const p = items[row.index];
                const size = [p.width, p.height, p.diameter].some(Boolean)
                  ? `${p.width ?? "—"}/${p.height ?? "—"} R${p.diameter ?? "—"}`
                  : "—";
                return (
                  <div
                    key={p.id}
                    onClick={() => toggle(p.id)}
                    className={`absolute left-0 grid w-full cursor-pointer grid-cols-[40px_120px_1fr_110px_100px_90px_110px_130px] items-center gap-3 border-b border-slate-100 px-4 text-sm hover:bg-slate-50 ${
                      selected.has(p.id) ? "bg-slate-50" : ""
                    }`}
                    style={{ height: row.size, transform: `translateY(${row.start}px)` }}
                  >
                    <input
                      type="checkbox"
                      checked={selected.has(p.id)}
                      onChange={() => toggle(p.id)}
                      onClick={(e) => e.stopPropagation()}
                    />
                    <span className="truncate font-mono text-xs text-slate-600">{p.cae}</span>
                    <span className="flex min-w-0 items-center gap-2">
                      {p.img_small && (
                        <img
                          src={p.img_small}
                          alt=""
                          loading="lazy"
                          className="h-9 w-9 shrink-0 rounded object-contain"
                        />
                      )}
                      <span className="min-w-0">
                        <span className="block truncate">{p.name ?? "—"}</span>
                        <span className="block truncate text-xs text-slate-500">
                          {p.brand} {p.model}
                        </span>
                      </span>
                    </span>
                    <span className="text-slate-600">{size}</span>
                    <span className="text-slate-600">
                      {p.season ?? "—"}
                      {p.thorn && <span className="ml-1 text-xs text-slate-400">шип</span>}
                    </span>
                    <span
                      className={`text-right tabular-nums ${
                        p.total_rest > 0 ? "text-slate-900" : "text-slate-400"
                      }`}
                    >
                      {p.total_rest}
                    </span>
                    <span className="text-right tabular-nums">
                      {p.min_price ? Number(p.min_price).toLocaleString("ru") : "—"}
                    </span>
                    <span>
                      <span
                        className={`rounded-full px-2 py-0.5 text-xs ${INTEGRATION_STYLE[p.integration_status]}`}
                      >
                        {INTEGRATION_LABEL[p.integration_status]}
                      </span>
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
  );
}
