import { useCallback, useEffect, useState } from "react";

import { api, ApiError, type Order, type OrdersSyncResult } from "../api";
import { useSupplier } from "../components/Layout";

const PLATFORM_LABEL: Record<string, string> = {
  wb: "Wildberries",
  ozon: "Ozon",
};

const STATUS_STYLE: Record<string, string> = {
  new: "bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  confirm: "bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
  complete: "bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
  cancel: "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300",
};

// Отмена приходит либо от продавца (mp_status), либо от покупателя/WB (mp_wb_status):
// при отмене покупателем supplierStatus остаётся new, и по нему отмену не видно.
const CANCELLED_MP = ["cancel", "cancel_carrier"];
const CANCELLED_WB = [
  "canceled",
  "canceled_by_client",
  "declined_by_client",
  "canceled_by_carrier",
  "defect",
];

function isCancelled(o: Order): boolean {
  return (
    (o.mp_status != null && CANCELLED_MP.includes(o.mp_status)) ||
    (o.mp_wb_status != null && CANCELLED_WB.includes(o.mp_wb_status))
  );
}

/** Карточка заказа в ЛК 4tochki. В адресе — числовой orderID (17915279), а не номер
 *  заказа (F7915279), поэтому ссылку строим по supplier_order_id. */
function supplierOrderUrl(orderId: number): string {
  return `https://b2b.4tochki.ru/Order/Details/${orderId}`;
}

function money(v: number | null): string {
  if (v == null) return "—";
  return v.toLocaleString("ru", { maximumFractionDigits: 2 }) + " ₽";
}

// --- список заказов ----------------------------------------------------------

function OrdersTable({
  orders,
  supplierId,
  onChanged,
}: {
  orders: Order[];
  supplierId: number;
  onChanged: () => Promise<void>;
}) {
  const [busy, setBusy] = useState<number | null>(null);

  const [actionError, setActionError] = useState<string | null>(null);

  const run = async (order: Order, path: string) => {
    setBusy(order.id);
    setActionError(null);
    try {
      await api.post(`/suppliers/${supplierId}/orders/${order.id}/${path}`);
      await onChanged();
    } catch (e) {
      // Отмену площадка может запретить (задание уже передано WB) — причину надо показать,
      // молча гасить нельзя: пользователь иначе решит, что кнопка не работает.
      setActionError(e instanceof ApiError ? e.message : "Не удалось выполнить действие");
    } finally {
      setBusy(null);
    }
  };

  const createSupplierOrder = (order: Order) => run(order, "supplier-order");
  const cancelOrder = (order: Order) => run(order, "cancel");
  const testDecline = (order: Order) => run(order, "test-decline");

  if (orders.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-slate-300 p-8 text-center text-sm text-slate-500 dark:border-slate-700 dark:text-slate-400">
        Заказов пока нет. Нажмите «Обновить заказы», чтобы стянуть их с площадок.
      </div>
    );
  }

  return (
    <>
      {actionError && (
        <div className="mb-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
          {actionError}
        </div>
      )}
      <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
      <table className="w-full min-w-[1000px] text-sm">
        <thead>
          <tr className="border-b border-slate-100 text-left text-xs uppercase tracking-wide text-slate-400 dark:border-slate-800">
            <th className="px-4 py-3 font-medium">Площадка</th>
            <th className="px-4 py-3 font-medium">Заказ</th>
            <th className="px-4 py-3 font-medium">Товар</th>
            <th className="px-4 py-3 font-medium">Кол-во</th>
            <th className="px-4 py-3 font-medium">Цена</th>
            <th className="px-4 py-3 font-medium">FBS-склад (куда)</th>
            <th className="px-4 py-3 font-medium">Склад 4tochki (откуда)</th>
            <th className="px-4 py-3 font-medium">Заказ в 4tochki</th>
            <th className="px-4 py-3 font-medium">Отмена</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((o) => {
            const item = o.items[0];
            return (
              <tr key={o.id} className="border-b border-slate-50 dark:border-slate-800/50">
                <td className="px-4 py-3">
                  <div className="flex items-center gap-2">
                    <span>{PLATFORM_LABEL[o.platform]}</span>
                    {o.is_test && (
                      <span className="rounded bg-purple-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-purple-700 dark:bg-purple-950 dark:text-purple-300">
                        тест
                      </span>
                    )}
                  </div>
                </td>
                <td className="px-4 py-3">
                  <div className="font-mono text-xs">{o.mp_order_id}</div>
                  {o.mp_status && (
                    <span
                      className={`mt-1 inline-block rounded px-1.5 py-0.5 text-[11px] ${
                        STATUS_STYLE[o.mp_status] ??
                        "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400"
                      }`}
                    >
                      {o.mp_status}
                    </span>
                  )}
                </td>
                <td className="px-4 py-3">
                  <div className="max-w-xs truncate" title={item?.name ?? ""}>
                    {item?.name ?? <span className="text-slate-400">не сопоставлен</span>}
                  </div>
                  {item?.cae && <div className="font-mono text-xs text-slate-400">{item.cae}</div>}
                </td>
                <td className="px-4 py-3">{item?.qty ?? "—"}</td>
                <td className="px-4 py-3">{money(item?.price ?? null)}</td>
                <td className="px-4 py-3">
                  {/* Имя склада, а не его id: по «35498» не понять, из какого города заказ.
                      Голый id остаётся только если склада нет в кэше подключения. */}
                  {o.fbs_warehouse_name ??
                    (o.fbs_warehouse_id ? `Склад ${o.fbs_warehouse_id}` : "—")}
                </td>
                <td className="px-4 py-3">
                  {o.source_warehouse_name ? (
                    <span className="text-slate-900 dark:text-slate-100">
                      {o.source_warehouse_name}
                    </span>
                  ) : (
                    <span
                      className="text-amber-600 dark:text-amber-400"
                      title="Нет привязки склада для этого FBS-склада — настройте на «Подключениях»"
                    >
                      не определён
                    </span>
                  )}
                </td>
                <td className="px-4 py-3">
                  {o.supplier_order_id ? (
                    <div>
                      <a
                        href={supplierOrderUrl(o.supplier_order_id)}
                        target="_blank"
                        rel="noreferrer"
                        title="Открыть заказ в личном кабинете 4tochki"
                        className="text-xs text-blue-600 hover:underline dark:text-blue-400"
                      >
                        № {o.supplier_order_number ?? o.supplier_order_id}
                      </a>
                      <div className="text-[11px] text-slate-500">{o.supplier_status}</div>
                    </div>
                  ) : o.error ? (
                    <span className="text-xs text-red-600" title={o.error}>
                      ошибка
                    </span>
                  ) : (
                    <button
                      onClick={() => void createSupplierOrder(o)}
                      disabled={busy === o.id || !o.source_warehouse_id || !item?.cae}
                      title={
                        !o.source_warehouse_id
                          ? "Сначала привяжите склад 4tochki к FBS-складу (на «Подключениях»)"
                          : !item?.cae
                            ? "Позиция не сопоставлена с товаром 4tochki"
                            : "Оформит тестовый заказ в 4tochki (без реальной отгрузки)"
                      }
                      className="rounded-md border border-slate-300 px-2.5 py-1.5 text-xs hover:bg-slate-50 disabled:opacity-40 dark:border-slate-700 dark:hover:bg-slate-800"
                    >
                      {busy === o.id ? "…" : "Оформить (тест)"}
                    </button>
                  )}
                </td>
                <td className="px-4 py-3">
                  {isCancelled(o) ? (
                    <div>
                      <div className="text-xs text-red-600 dark:text-red-400">отменён</div>
                      <div className="text-[11px] text-slate-500">
                        {o.supplier_cancelled_at
                          ? "в 4tochki тоже"
                          : o.supplier_order_id
                            ? "в 4tochki — нет"
                            : "заказа у поставщика не было"}
                      </div>
                    </div>
                  ) : (
                    <div className="flex flex-col gap-1.5">
                      <button
                        onClick={() => void cancelOrder(o)}
                        disabled={busy === o.id}
                        title="Отменит задание на площадке и заказ у поставщика"
                        className="rounded-md border border-slate-300 px-2.5 py-1.5 text-xs hover:bg-slate-50 disabled:opacity-40 dark:border-slate-700 dark:hover:bg-slate-800"
                      >
                        {busy === o.id ? "…" : "Отменить"}
                      </button>
                      {o.is_test && o.platform === "wb" && (
                        <button
                          onClick={() => void testDecline(o)}
                          disabled={busy === o.id}
                          title="Песочница: эмулировать отмену покупателем"
                          className="rounded-md border border-dashed border-slate-300 px-2.5 py-1.5 text-[11px] text-slate-500 hover:bg-slate-50 disabled:opacity-40 dark:border-slate-700 dark:hover:bg-slate-800"
                        >
                          отмена покупателем
                        </button>
                      )}
                    </div>
                  )}
                </td>
              </tr>
            );
          })}
          </tbody>
        </table>
      </div>
    </>
  );
}

// --- страница ----------------------------------------------------------------

export function OrdersPage() {
  const { current } = useSupplier();
  const [orders, setOrders] = useState<Order[]>([]);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [syncInfo, setSyncInfo] = useState<string | null>(null);

  const supplierId = current?.id;

  const loadOrders = useCallback(async () => {
    if (!supplierId) return;
    const list = await api.get<Order[]>(`/suppliers/${supplierId}/orders`);
    setOrders(list);
  }, [supplierId]);

  useEffect(() => {
    if (!supplierId) return;
    setLoading(true);
    loadOrders()
      .catch((e) => setError(e instanceof ApiError ? e.message : "Ошибка загрузки"))
      .finally(() => setLoading(false));
  }, [supplierId, loadOrders]);

  const sync = async () => {
    if (!supplierId) return;
    setSyncing(true);
    setError(null);
    setSyncInfo(null);
    try {
      const res = await api.post<OrdersSyncResult>(`/suppliers/${supplierId}/orders/sync`);
      setOrders(res.orders);
      // Свод по площадкам: сколько заказов и что пошло не так (напр. лимит запросов).
      const parts = res.platforms.map((p) => {
        const label = PLATFORM_LABEL[p.platform] ?? p.platform;
        return p.ok ? `${label}: заказов ${p.fetched}` : `${label}: ${p.message}`;
      });
      setSyncInfo(parts.length ? parts.join(" · ") : "Настроенных площадок нет");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Не удалось обновить заказы");
    } finally {
      setSyncing(false);
    }
  };

  if (loading) {
    return <div className="text-sm text-slate-500">Загрузка…</div>;
  }

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Заказы</h1>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            Сборочные задания FBS с площадок. Привязку складов 4tochki к FBS-складам
            настройте на вкладке «Подключения». Оформление заказа в 4tochki идёт через
            тестовый контур — реальной отгрузки не происходит.
          </p>
        </div>
        <button
          onClick={() => void sync()}
          disabled={syncing}
          className="shrink-0 rounded-md bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900"
        >
          {syncing ? "Обновление…" : "Обновить заказы"}
        </button>
      </div>

      {error && (
        <div className="rounded-md bg-red-50 px-4 py-3 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">
          {error}
        </div>
      )}

      {syncInfo && <p className="text-sm text-slate-500 dark:text-slate-400">{syncInfo}</p>}

      <OrdersTable orders={orders} supplierId={supplierId!} onChanged={loadOrders} />
    </div>
  );
}
