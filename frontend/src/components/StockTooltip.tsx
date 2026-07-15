import { useEffect, useState } from "react";

import { api, type ProductStock } from "../api";

interface Props {
  supplierId: number;
  productId: number;
  totalRest: number;
  marketplaceRest: number;
  buffer: number;
  top: number;
  left: number;
}

// Склады у товара меняются только при синхронизации, поэтому ответ переживает
// наведение мыши: без кэша каждый проход курсором по колонке бил бы в API.
const cache = new Map<number, ProductStock[]>();

export function StockTooltip({
  supplierId,
  productId,
  totalRest,
  marketplaceRest,
  buffer,
  top,
  left,
}: Props) {
  const [stocks, setStocks] = useState<ProductStock[] | null>(
    () => cache.get(productId) ?? null,
  );

  useEffect(() => {
    if (cache.has(productId)) {
      setStocks(cache.get(productId)!);
      return;
    }
    let alive = true;
    void api
      .get<ProductStock[]>(`/suppliers/${supplierId}/products/${productId}/stocks`)
      .then((data) => {
        cache.set(productId, data);
        if (alive) setStocks(data);
      })
      .catch(() => alive && setStocks([]));
    return () => {
      alive = false;
    };
  }, [supplierId, productId]);

  const selected = stocks?.filter((s) => s.selected) ?? [];
  const other = stocks?.filter((s) => !s.selected) ?? [];

  return (
    <div
      className="pointer-events-none fixed z-50 w-80 rounded-lg border border-slate-200 bg-white p-3 text-sm shadow-xl dark:border-slate-700 dark:bg-slate-800"
      style={{
        top: Math.min(top, window.innerHeight - 280),
        left: Math.min(left, window.innerWidth - 340),
      }}
    >
      {stocks === null ? (
        <p className="text-slate-400">Загружаем остатки…</p>
      ) : stocks.length === 0 ? (
        <p className="text-slate-500 dark:text-slate-400">
          Остатков нет ни на одном складе 4tochki.
        </p>
      ) : (
        <>
          {selected.length > 0 && (
            <table className="w-full">
              <tbody>
                {selected.map((s) => (
                  <tr key={s.wrh}>
                    <td className="truncate py-0.5 pr-2">{s.name ?? `Склад ${s.wrh}`}</td>
                    <td className="whitespace-nowrap py-0.5 pr-2 text-right text-xs text-slate-400">
                      {s.logistic_days ?? "—"} дн.
                    </td>
                    <td className="py-0.5 text-right font-medium tabular-nums">{s.rest}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {other.length > 0 && (
            <>
              {/* Склады вне выбранного набора показываем отдельно и приглушённо:
                  их остаток в колонку не попадает, и это должно быть очевидно. */}
              <p className="mt-2 border-t border-slate-100 pt-2 text-xs text-slate-400 dark:border-slate-700">
                Не выбраны в «Подключениях» — в остаток не входят:
              </p>
              <table className="w-full text-slate-400">
                <tbody>
                  {other.map((s) => (
                    <tr key={s.wrh}>
                      <td className="truncate py-0.5 pr-2">{s.name ?? `Склад ${s.wrh}`}</td>
                      <td className="whitespace-nowrap py-0.5 pr-2 text-right text-xs">
                        {s.logistic_days ?? "—"} дн.
                      </td>
                      <td className="py-0.5 text-right tabular-nums">{s.rest}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          {/* Итог по выбранным складам с учётом буфера — именно столько уйдёт на МП. */}
          <div className="mt-2 flex items-center justify-between border-t border-slate-200 pt-2 text-xs dark:border-slate-700">
            <span className="text-slate-500 dark:text-slate-400">
              На маркетплейс{buffer > 0 ? ` (буфер ${buffer})` : ""}:
            </span>
            <span
              className={`font-semibold tabular-nums ${
                marketplaceRest > 0
                  ? "text-slate-900 dark:text-slate-100"
                  : "text-amber-600 dark:text-amber-400"
              }`}
            >
              {marketplaceRest} из {totalRest}
            </span>
          </div>
        </>
      )}
    </div>
  );
}
