import { useEffect, useMemo, useState } from "react";

import { api, type FormulaPreview } from "../api";

interface Props {
  supplierId: number;
  label: string;
  value: string;
  onChange: (v: string) => void;
  /** Ориентир по комиссии площадки — для подсказки в примерах. */
  commissionHint: string;
  /** Пресеты формул. Для цены до скидки — свои, с переменной цены. */
  presets: { label: string; formula: string }[];
  /** Имя переменной цены (wb_price/ozon_price). Задано → это формула цены до скидки:
   *  считаем её от нашей цены и показываем итоговую скидку. */
  priceVar?: string;
}

export function FormulaField({
  supplierId,
  label,
  value,
  onChange,
  commissionHint,
  presets,
  priceVar,
}: Props) {
  const [preview, setPreview] = useState<FormulaPreview | null>(null);
  const [sample, setSample] = useState(5000);
  // Для формулы цены до скидки нужна наша цена — берём ориентировочную от закупки.
  const ourPrice = Math.round(sample * 1.25);

  // Дебаунс: предпросмотр считается на сервере тем же вычислителем, что и реальная
  // цена, — чтобы UI и пуш никогда не разошлись.
  useEffect(() => {
    if (!value.trim()) {
      setPreview(null);
      return;
    }
    const t = setTimeout(() => {
      void api
        .post<FormulaPreview>(`/suppliers/${supplierId}/sync/settings/preview-formula`, {
          formula: value,
          purchase: sample,
          rrp: Math.round(sample * 1.4),
          weight: 10,
          price: priceVar ? ourPrice : undefined,
        })
        .then(setPreview)
        .catch(() => setPreview({ ok: false, price: null, error: "Ошибка проверки" }));
    }, 300);
    return () => clearTimeout(t);
  }, [value, sample, supplierId, priceVar, ourPrice]);

  const margin = useMemo(() => {
    if (!preview?.ok || !preview.price) return null;
    // База сравнения: для цены до скидки — наша цена, для основной — закупка.
    const base = priceVar ? ourPrice : sample;
    const p = Number(preview.price);
    // Для цены до скидки показываем ещё и итоговый процент скидки.
    const discount =
      priceVar && p > ourPrice ? Math.round((1 - ourPrice / p) * 100) : null;
    return { rub: p - base, pct: Math.round(((p - base) / base) * 100), discount };
  }, [preview, sample, priceVar, ourPrice]);

  return (
    <div className="rounded-lg border border-slate-200 p-4 dark:border-slate-700">
      <div className="flex items-center justify-between">
        <p className="font-medium">{label}</p>
        <span className="text-xs text-slate-400">{commissionHint}</span>
      </div>

      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        spellCheck={false}
        className={`mt-2 w-full rounded-md border px-3 py-2 font-mono text-sm focus:outline-none dark:bg-slate-800 dark:text-slate-100 ${
          preview && !preview.ok
            ? "border-red-400 focus:border-red-500"
            : "border-slate-300 focus:border-slate-900 dark:border-slate-700 dark:focus:border-slate-400"
        }`}
      />

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <span className="text-xs text-slate-500 dark:text-slate-400">Пример: закупка</span>
        <input
          type="number"
          min={1}
          value={sample}
          onChange={(e) => setSample(Math.max(1, Number(e.target.value) || 1))}
          className="w-24 rounded-md border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
        />
        <span className="text-slate-400">
          ₽{priceVar ? `, наша цена ≈ ${ourPrice.toLocaleString("ru")} ₽ →` : " →"}
        </span>
        {preview?.ok ? (
          <span className="font-medium text-slate-900 dark:text-slate-100">
            {Number(preview.price).toLocaleString("ru")} ₽
            {margin?.discount != null ? (
              <span className="ml-1 font-normal text-slate-500 dark:text-slate-400">
                (скидка на витрине ≈ {margin.discount}%)
              </span>
            ) : (
              margin && (
                <span className="ml-1 font-normal text-slate-500 dark:text-slate-400">
                  (+{margin.rub.toLocaleString("ru")} ₽ / +{margin.pct}%)
                </span>
              )
            )}
          </span>
        ) : preview ? (
          <span className="text-sm text-red-600 dark:text-red-400">{preview.error}</span>
        ) : (
          <span className="text-sm text-slate-400">…</span>
        )}
      </div>

      <div className="mt-3 flex flex-wrap gap-1.5">
        {presets.map((p) => (
          <button
            key={p.label}
            type="button"
            onClick={() => onChange(p.formula)}
            className="rounded-full border border-slate-200 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-400 dark:hover:bg-slate-800"
          >
            {p.label}
          </button>
        ))}
      </div>
    </div>
  );
}
