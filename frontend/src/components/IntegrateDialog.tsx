import { useEffect, useMemo, useState } from "react";

import { type Credential, type Product } from "../api";

interface Props {
  products: Product[];
  platforms: string[];
  wbCred?: Credential;
  busy: boolean;
  onConfirm: () => void;
  onClose: () => void;
}

const PLATFORM_LABEL: Record<string, string> = {
  wb: "Wildberries",
  ozon: "Ozon",
};

/** Что войдёт в карточку — считаем на клиенте, чтобы показать это ДО отправки,
 *  а не объяснять потом, почему WB отклонил половину. */
function classify(products: Product[]) {
  const ready: Product[] = [];
  const noPrice: Product[] = [];
  const noBrand: Product[] = [];
  const unsupported: Product[] = [];
  const noStock: Product[] = [];

  for (const p of products) {
    if (p.goods_type !== "tyre" && p.goods_type !== "rim") unsupported.push(p);
    else if (!p.brand) noBrand.push(p);
    else if (!p.min_price) noPrice.push(p);
    else {
      ready.push(p);
      if (p.total_rest === 0) noStock.push(p);
    }
  }
  return { ready, noPrice, noBrand, unsupported, noStock };
}

export function IntegrateDialog({
  products,
  platforms,
  wbCred,
  busy,
  onConfirm,
  onClose,
}: Props) {
  const [ack, setAck] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !busy && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, busy]);

  const { ready, noPrice, noBrand, unsupported, noStock } = useMemo(
    () => classify(products),
    [products],
  );

  const sandbox = wbCred?.status_message?.includes("песочница") ?? false;
  const wbOk = wbCred?.status === "ok";
  const targets = platforms.map((p) => PLATFORM_LABEL[p] ?? p).join(" и ");

  const blocked = ready.length === 0 || !wbOk || (sandbox && !ack);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4 dark:bg-slate-950/60"
      onMouseDown={(e) => e.target === e.currentTarget && !busy && onClose()}
    >
      <div className="max-h-[85vh] w-full max-w-lg overflow-y-auto rounded-xl border border-slate-200 bg-white p-6 shadow-xl dark:border-slate-800 dark:bg-slate-900">
        <h2 className="text-lg font-semibold tracking-tight">
          Создать карточки в {targets}
        </h2>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          Выбрано товаров: {products.length}. Проверьте, что именно уедет на площадку.
        </p>

        {sandbox && (
          <div className="mt-4 rounded-lg bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-200">
            <p className="font-medium">Токен WB — тестовый.</p>
            <p className="mt-1">
              Карточки будут созданы в <b>песочнице</b> Wildberries, а не в реальном
              кабинете. Ни на витрину, ни в продажи они не попадут.
            </p>
          </div>
        )}

        {!wbOk && (
          <div className="mt-4 rounded-lg bg-red-50 p-3 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">
            Подключение к Wildberries не проверено или с ошибкой. Зайдите в
            «Подключения» и нажмите «Проверить подключение».
          </div>
        )}

        <div className="mt-5 space-y-2 text-sm">
          <p className="font-medium">Что произойдёт:</p>
          <ol className="list-inside list-decimal space-y-1.5 text-slate-600 dark:text-slate-400">
            <li>
              Для <b className="text-slate-900 dark:text-slate-100">{ready.length}</b>{" "}
              товаров соберутся карточки: бренд, модель, типоразмер, сезонность, индексы
              скорости и нагрузки, шумность.
            </li>
            <li>
              Цена возьмётся из закупочной с применением правил наценки. Штрихкод —
              детерминированный (<code className="text-xs">4D + CAE</code>), повторный
              запуск не создаст дублей.
            </li>
            <li>
              Карточки уйдут на <b>модерацию</b> WB. Это не мгновенно: статус станет
              «На модерации», а после проверки — «Активен» или «Отклонён» с причиной.
            </li>
            <li>
              Мы сохраним <code className="text-xs">nmID</code> и{" "}
              <code className="text-xs">chrtID</code> — по chrtID WB принимает остатки.
            </li>
          </ol>
        </div>

        {(noPrice.length > 0 ||
          noBrand.length > 0 ||
          unsupported.length > 0 ||
          noStock.length > 0) && (
          <div className="mt-5 rounded-lg bg-slate-50 p-3 text-sm dark:bg-slate-800/60">
            <p className="font-medium text-slate-700 dark:text-slate-300">
              Требует внимания:
            </p>
            <ul className="mt-2 space-y-1 text-slate-600 dark:text-slate-400">
              {unsupported.length > 0 && (
                <li>
                  <b>{unsupported.length}</b> — не шины и не диски, карточки для них не
                  создаются. Будут пропущены.
                </li>
              )}
              {noBrand.length > 0 && (
                <li>
                  <b>{noBrand.length}</b> — без бренда. WB не примет такую карточку.
                  Будут пропущены.
                </li>
              )}
              {noPrice.length > 0 && (
                <li>
                  <b>{noPrice.length}</b> — без цены поставщика (нет на выбранных
                  складах). Будут пропущены.
                </li>
              )}
              {noStock.length > 0 && (
                <li>
                  <b>{noStock.length}</b> — карточка создастся, но остаток нулевой:
                  товара нет на выбранных складах.
                </li>
              )}
            </ul>
          </div>
        )}

        {sandbox && (
          <label className="mt-5 flex cursor-pointer items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={ack}
              onChange={(e) => setAck(e.target.checked)}
              className="mt-0.5 accent-slate-900"
            />
            <span className="text-slate-600 dark:text-slate-400">
              Понимаю, что карточки создаются в тестовой песочнице WB.
            </span>
          </label>
        )}

        <div className="mt-6 flex items-center justify-end gap-2">
          <button
            onClick={onClose}
            disabled={busy}
            className="rounded-md border border-slate-300 px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-800"
          >
            Отмена
          </button>
          <button
            onClick={onConfirm}
            disabled={blocked || busy}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-40 dark:bg-slate-100 dark:text-slate-900"
          >
            {busy
              ? "Отправляем…"
              : ready.length === 0
                ? "Нечего отправлять"
                : `Создать ${ready.length} карточек`}
          </button>
        </div>
      </div>
    </div>
  );
}
