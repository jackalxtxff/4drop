import { useEffect, useState } from "react";

import {
  api,
  ApiError,
  type Credential,
  type PlatformMappingView,
  type Warehouse,
  type WarehouseMappingsView,
} from "../api";
import { useSupplier } from "../components/Layout";

const STATUS_STYLE: Record<string, string> = {
  ok: "bg-emerald-50 text-emerald-700 ring-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:ring-emerald-800",
  error: "bg-red-50 text-red-700 ring-red-200 dark:bg-red-950 dark:text-red-300 dark:ring-red-800",
  not_configured: "bg-slate-100 text-slate-600 ring-slate-200 dark:bg-slate-800 dark:text-slate-400 dark:ring-slate-700",
};

const STATUS_LABEL: Record<string, string> = {
  ok: "Подключено",
  error: "Ошибка",
  not_configured: "Не настроено",
};

function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${
        STATUS_STYLE[status] ?? STATUS_STYLE.not_configured
      }`}
    >
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}

function Card({
  title,
  subtitle,
  cred,
  children,
  onCheck,
  checking,
}: {
  title: string;
  subtitle: string;
  cred?: Credential;
  children: React.ReactNode;
  onCheck: () => void;
  checking: boolean;
}) {
  return (
    <section className="rounded-xl border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-900">
      <div className="flex items-start justify-between">
        <div>
          <h2 className="font-semibold tracking-tight">{title}</h2>
          <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">{subtitle}</p>
        </div>
        <StatusBadge status={cred?.status ?? "not_configured"} />
      </div>

      {cred?.account_name && (
        <div className="mt-3 flex items-center gap-2 rounded-lg bg-emerald-50 px-3 py-2 dark:bg-emerald-950/50">
          <svg
            className="h-4 w-4 shrink-0 text-emerald-600 dark:text-emerald-400"
            viewBox="0 0 20 20"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
          >
            <circle cx="10" cy="6.5" r="3" />
            <path d="M4 16c0-3 2.7-5 6-5s6 2 6 5" strokeLinecap="round" />
          </svg>
          <span className="truncate font-medium text-emerald-800 dark:text-emerald-300">
            {cred.account_name}
          </span>
        </div>
      )}

      {cred?.status_message && (
        <p
          className={`mt-3 rounded-md px-3 py-2 text-sm ${
            cred.status === "error"
              ? "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300"
              : "bg-slate-50 text-slate-600 dark:bg-slate-800 dark:text-slate-400"
          }`}
        >
          {cred.status_message}
        </p>
      )}

      <div className="mt-4 space-y-3">{children}</div>

      <button
        onClick={onCheck}
        disabled={checking}
        className="mt-4 rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800 disabled:opacity-50"
      >
        {checking ? "Проверяем…" : "Проверить подключение"}
      </button>
    </section>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  type = "text",
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
}) {
  return (
    <div>
      <label className="block text-sm font-medium text-slate-700 dark:text-slate-300">{label}</label>
      <input
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-900 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 dark:focus:border-slate-400"
      />
    </div>
  );
}

/** Контур оформления заказов у поставщика. Выключение тестового режима означает, что
 *  заказы становятся настоящей закупкой шин, поэтому переключение идёт через
 *  подтверждение, а не одним кликом. Сохраняется сразу, отдельной кнопки нет. */
function TestModeToggle({
  cred,
  supplierId,
  onSaved,
}: {
  cred: Credential;
  supplierId: number;
  onSaved: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const apply = async (testMode: boolean) => {
    setBusy(true);
    setError(null);
    try {
      await api.put(`/suppliers/${supplierId}/connections/fourtochki/test-mode`, {
        test_mode: testMode,
      });
      setConfirming(false);
      await onSaved();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Не удалось переключить контур");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-700">
      <label className="flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          checked={cred.test_mode}
          disabled={busy}
          onChange={(e) => (e.target.checked ? void apply(true) : setConfirming(true))}
          className="mt-0.5 accent-slate-900"
        />
        <span>
          Тестовый контур заказов
          <span className="mt-0.5 block text-xs text-slate-500 dark:text-slate-400">
            {cred.test_mode
              ? "Заказы уходят как тестовые (CreateOrder is_test) — 4tochki их принимает, но реальной отгрузки нет."
              : "Боевой режим: оформление заказа — настоящая закупка у поставщика."}
          </span>
        </span>
      </label>

      {!cred.test_mode && (
        <p className="mt-2 rounded-md bg-amber-50 px-2 py-1.5 text-xs text-amber-800 dark:bg-amber-950 dark:text-amber-300">
          Боевой режим включён. Каждый заказ с маркетплейса — реальная закупка шин.
        </p>
      )}

      {confirming && (
        <div className="mt-2 rounded-md border border-amber-300 bg-amber-50 p-2 dark:border-amber-900 dark:bg-amber-950/50">
          <p className="text-xs text-amber-800 dark:text-amber-300">
            Выключить тестовый контур? Заказы начнут оформляться у поставщика по-настоящему.
            На уже оформленные это не влияет.
          </p>
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              onClick={() => setConfirming(false)}
              disabled={busy}
              className="rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs hover:bg-slate-50 disabled:opacity-40 dark:border-slate-700 dark:bg-slate-900 dark:hover:bg-slate-800"
            >
              Отмена
            </button>
            <button
              type="button"
              onClick={() => void apply(false)}
              disabled={busy}
              className="rounded-md bg-amber-600 px-2.5 py-1.5 text-xs font-medium text-white hover:bg-amber-700 disabled:opacity-40"
            >
              {busy ? "…" : "Включить боевой режим"}
            </button>
          </div>
        </div>
      )}

      {error && <p className="mt-2 text-xs text-red-600 dark:text-red-400">{error}</p>}
    </div>
  );
}

/** Какие права нужно выдать токену. Список — часть настройки, а не справка:
 *  без этих галочек интеграция не заработает, а площадка вернёт 403. */
function Scopes({
  title,
  required,
  optional,
  note,
}: {
  title: string;
  required: [string, string][];
  optional?: [string, string][];
  note?: string;
}) {
  return (
    <div className="rounded-lg bg-slate-50 p-3 dark:bg-slate-800/60">
      <p className="text-xs font-medium text-slate-700 dark:text-slate-300">{title}</p>

      <ul className="mt-2 space-y-1">
        {required.map(([name, why]) => (
          <li key={name} className="flex gap-2 text-xs">
            <span className="mt-px text-emerald-600 dark:text-emerald-400">✓</span>
            <span className="min-w-0">
              <span className="font-medium text-slate-800 dark:text-slate-200">{name}</span>
              <span className="text-slate-500 dark:text-slate-400"> — {why}</span>
            </span>
          </li>
        ))}
      </ul>

      {optional && optional.length > 0 && (
        <>
          <p className="mt-2.5 text-xs font-medium text-slate-500 dark:text-slate-400">
            Желательно:
          </p>
          <ul className="mt-1 space-y-1">
            {optional.map(([name, why]) => (
              <li key={name} className="flex gap-2 text-xs">
                <span className="mt-px text-slate-400">+</span>
                <span className="min-w-0">
                  <span className="font-medium text-slate-700 dark:text-slate-300">{name}</span>
                  <span className="text-slate-500 dark:text-slate-400"> — {why}</span>
                </span>
              </li>
            ))}
          </ul>
        </>
      )}

      {note && (
        <p className="mt-2.5 border-t border-slate-200 pt-2 text-xs text-slate-500 dark:border-slate-700 dark:text-slate-400">
          {note}
        </p>
      )}
    </div>
  );
}

const MP_LABEL: Record<string, string> = {
  wb: "Wildberries",
  ozon: "Ozon",
};
/** Привязка одной площадки (мультисклад).
 *
 *  У каждого FBS-склада свой адрес доставки 4tochki: ижевский FBS кормится с ижевского
 *  адреса, московский — с московского. Поэтому сначала выбирается адрес, а склады-
 *  источники предлагаются только те, что доступны с него, и со сроками именно для него.
 *  Один склад 4tochki можно привязать только к одному FBS — иначе его остаток
 *  опубликовался бы дважды и мы получили бы оверселл. */
function PlatformBinding({
  platform,
  addresses,
  warehousesByAddress,
  supplierId,
  onSaved,
}: {
  platform: PlatformMappingView;
  addresses: WarehouseMappingsView["addresses"];
  warehousesByAddress: WarehouseMappingsView["warehouses_by_address"];
  supplierId: number;
  onSaved: () => Promise<void>;
}) {
  // Локально: склад 4tochki → id FBS-склада, к которому он привязан.
  const buildAssign = () => {
    const a: Record<number, string> = {};
    for (const m of platform.mappings) a[m.fourtochki_wrh] = m.fbs_warehouse_id;
    return a;
  };
  // Локально: FBS-склад → выбранный адрес доставки.
  const buildAddr = () => {
    const a: Record<string, number> = {};
    for (const w of platform.fbs_warehouses) if (w.address_id) a[w.id] = w.address_id;
    return a;
  };
  const buildDisabled = () =>
    new Set(platform.fbs_warehouses.filter((w) => !w.enabled).map((w) => w.id));

  const [assign, setAssign] = useState<Record<number, string>>(buildAssign);
  const [fbsAddr, setFbsAddr] = useState<Record<string, number>>(buildAddr);
  const [disabledFbs, setDisabledFbs] = useState<Set<string>>(buildDisabled);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  // Какие списки складов раскрыты (по id FBS-склада). Свёрнуты по умолчанию:
  // складов бывает под сотню, иначе страница превращается в простыню.
  const [openPickers, setOpenPickers] = useState<Set<string>>(new Set());
  const [openDelivery, setOpenDelivery] = useState<Set<string>>(new Set());

  const flip = (set: Set<string>, id: string) => {
    const next = new Set(set);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  };
  const togglePicker = (id: string) => setOpenPickers((s) => flip(s, id));
  const toggleDelivery = (id: string) => setOpenDelivery((s) => flip(s, id));

  // Извне пришли обновлённые данные (после сохранения/перезагрузки) — пересобрать.
  useEffect(() => {
    setAssign(buildAssign());
    setFbsAddr(buildAddr());
    setDisabledFbs(buildDisabled());
    setSaved(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [platform.mappings, platform.fbs_warehouses]);

  const toggle = (fbsId: string, wrh: number) => {
    setSaved(false);
    setAssign((a) => {
      const next = { ...a };
      if (next[wrh] === fbsId) delete next[wrh];
      else next[wrh] = fbsId;
      return next;
    });
  };

  const toggleFbs = (fbsId: string) => {
    setSaved(false);
    setDisabledFbs((d) => {
      const next = new Set(d);
      if (next.has(fbsId)) next.delete(fbsId);
      else next.add(fbsId);
      return next;
    });
  };

  /** Смена адреса у FBS-склада снимает его привязки: с другого адреса доступны
   *  другие склады, старые там просто не существуют. */
  const changeFbsAddress = (fbsId: string, addressId: number) => {
    setSaved(false);
    setFbsAddr((a) => ({ ...a, [fbsId]: addressId }));
    setAssign((a) => {
      const next = { ...a };
      for (const [wrh, fbs] of Object.entries(a)) if (fbs === fbsId) delete next[Number(wrh)];
      return next;
    });
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const mappings = Object.entries(assign).map(([wrh, fbs]) => ({
        fourtochki_wrh: Number(wrh),
        fbs_warehouse_id: fbs,
        address_id: fbsAddr[fbs] ?? null,
        priority: 0,
      }));
      await api.put(
        `/suppliers/${supplierId}/connections/warehouse-mappings/${platform.platform}`,
        { mappings, disabled_fbs: [...disabledFbs], fbs_addresses: fbsAddr },
      );
      await onSaved();
      setSaved(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось сохранить привязки");
    } finally {
      setSaving(false);
    }
  };

  /** Карточка одного склада-источника с чекбоксом. Вынесена, чтобы одинаково
   *  рисовать локальные склады и группу «с доставкой». */
  const renderWarehouse = (w: Warehouse, fbsId: string, enabled: boolean) => {
    const here = assign[w.id] === fbsId;
    const elsewhere = assign[w.id] != null && assign[w.id] !== fbsId;
    const locked = elsewhere || !enabled;
    const days = w.logistic_days ?? 0;
    return (
      <label
        key={w.id}
        className={`flex items-start gap-2 rounded-lg border p-2.5 text-sm transition ${
          locked
            ? "cursor-not-allowed border-slate-100 opacity-50 dark:border-slate-800"
            : here
              ? "cursor-pointer border-slate-900 bg-slate-50 dark:border-slate-400 dark:bg-slate-800"
              : "cursor-pointer border-slate-200 hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
        }`}
      >
        <input
          type="checkbox"
          checked={here}
          disabled={locked}
          onChange={() => toggle(fbsId, w.id)}
          className="mt-0.5"
        />
        <span className="min-w-0">
          <span className="block truncate">
            {w.name}
            <span className="ml-1.5 text-xs font-normal text-slate-400">
              {w.total_rest ?? 0} шт
            </span>
          </span>
          <span
            className={`text-xs ${
              days === 0
                ? "text-emerald-600 dark:text-emerald-400"
                : days > 2
                  ? "text-amber-600 dark:text-amber-400"
                  : "text-slate-400"
            }`}
          >
            {days === 0 ? "день в день" : `логистика ${days} дн.`}
            {days > 2 && " — риск сорвать SLA"}
          </span>
          {elsewhere && (
            <span className="block text-xs text-slate-400">уже на другом FBS-складе</span>
          )}
        </span>
      </label>
    );
  };

  const label = MP_LABEL[platform.platform] ?? platform.platform;

  if (!platform.configured || platform.fbs_warehouses.length === 0) {
    return (
      <div className="rounded-lg border border-slate-200 p-4 dark:border-slate-800">
        <p className="font-medium">{label}</p>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          {platform.message ?? "FBS-складов нет"}
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-slate-200 dark:border-slate-800">
      <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3 dark:border-slate-800">
        <p className="font-medium">{label}</p>
        <div className="flex items-center gap-3">
          {saved && <span className="text-xs text-emerald-600 dark:text-emerald-400">Сохранено</span>}
          <button
            onClick={() => void save()}
            disabled={saving}
            className="rounded-md bg-slate-900 px-3 py-1.5 text-sm text-white disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900"
          >
            {saving ? "Сохранение…" : "Сохранить"}
          </button>
        </div>
      </div>

      {error && <p className="px-4 pt-3 text-sm text-red-600">{error}</p>}
      {platform.message && (
        <p className="px-4 pt-3 text-xs text-amber-600 dark:text-amber-400">{platform.message}</p>
      )}

      <div className="space-y-5 p-4">
        {platform.fbs_warehouses.map((fbs) => {
          const enabled = !disabledFbs.has(fbs.id);
          const addressId = fbsAddr[fbs.id];
          const available = addressId ? (warehousesByAddress[String(addressId)] ?? []) : [];
          const chosen = available.filter((w) => assign[w.id] === fbs.id);
          // Живой остаток привязанных складов — пересчитывается сразу при клике.
          const boundStock = chosen.reduce((s, w) => s + (w.total_rest ?? 0), 0);
          // Группируем по СРОКУ, а не по флагу have_delivery: он означает «4tochki
          // может привезти», а не скорость, и со сроками не коррелирует (Домодедово —
          // have_delivery, но 0 дней; «Склад 2» — самовывоз, но 11 дней). Для FBS
          // решает именно срок, поэтому «день в день» на виду, остальные — свёрнуты.
          const local = available.filter((w) => (w.logistic_days ?? 0) === 0);
          const delivery = available
            .filter((w) => (w.logistic_days ?? 0) > 0)
            .sort((a, b) => (a.logistic_days ?? 0) - (b.logistic_days ?? 0));
          const pickerOpen = openPickers.has(fbs.id);
          const deliveryOpen = openDelivery.has(fbs.id);

          return (
            <div key={fbs.id} className={enabled ? "" : "opacity-60"}>
              <div className="mb-2 flex flex-wrap items-center gap-2 text-sm font-medium">
                <span className="rounded bg-slate-100 px-2 py-0.5 text-xs dark:bg-slate-800">
                  FBS
                </span>
                {fbs.name ?? `Склад ${fbs.id}`}
                <span className="text-xs font-normal text-slate-400">#{fbs.id}</span>
                <span
                  className={`rounded px-1.5 py-0.5 text-xs font-normal ${
                    enabled
                      ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300"
                      : "bg-slate-100 text-slate-500 line-through dark:bg-slate-800"
                  }`}
                  title="Реальный остаток привязанных складов 4tochki (до вычета буфера)"
                >
                  остаток: {boundStock} шт
                </span>
                {!enabled && (
                  <span className="rounded bg-red-50 px-1.5 py-0.5 text-xs font-normal text-red-600 dark:bg-red-950 dark:text-red-300">
                    выключен · на площадку 0
                  </span>
                )}
                <button
                  type="button"
                  role="switch"
                  aria-checked={enabled}
                  onClick={() => toggleFbs(fbs.id)}
                  title={enabled ? "Выключить склад (остаток → 0 после сохранения)" : "Включить склад"}
                  className={`relative ml-auto h-5 w-9 shrink-0 rounded-full transition ${
                    enabled ? "bg-emerald-500" : "bg-slate-300 dark:bg-slate-600"
                  }`}
                >
                  <span
                    className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${
                      enabled ? "left-[18px]" : "left-0.5"
                    }`}
                  />
                </button>
              </div>

              {/* Адрес приёмки для этого FBS-склада: от него зависят и доступные
                  склады-источники, и сроки, и куда поедет заказ. */}
              <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
                <span className="text-slate-500 dark:text-slate-400">Адрес приёмки:</span>
                <select
                  value={addressId ?? ""}
                  disabled={!enabled}
                  onChange={(e) => changeFbsAddress(fbs.id, Number(e.target.value))}
                  className="min-w-[260px] rounded-md border border-slate-300 bg-white px-2 py-1 text-sm disabled:opacity-50 dark:border-slate-700 dark:bg-slate-800"
                >
                  <option value="">— выберите город —</option>
                  {addresses.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.title}
                      {a.warehouse_count !== null
                        ? ` — складов ${a.warehouse_count}, «день в день» ${a.same_day_count}`
                        : ""}
                    </option>
                  ))}
                </select>
              </div>

              {!addressId ? (
                <p className="text-xs text-slate-500 dark:text-slate-400">
                  Выберите город приёмки — после этого появятся склады, доступные с него.
                </p>
              ) : (
                <>
                  {/* Список источников свёрнут: складов бывает под сотню, развёрнутыми
                      они превращают страницу в простыню. В свёрнутом виде показываем
                      главное — что уже выбрано. */}
                  <button
                    type="button"
                    onClick={() => togglePicker(fbs.id)}
                    className="flex w-full items-center gap-2 rounded-md border border-slate-200 px-3 py-2 text-left text-sm hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
                  >
                    <span className="text-slate-400">{pickerOpen ? "▾" : "▸"}</span>
                    <span className="font-medium">Склады-источники</span>
                    <span className="text-slate-500 dark:text-slate-400">
                      выбрано {chosen.length} из {available.length}
                      <span className="ml-1 text-xs text-emerald-600 dark:text-emerald-400">
                        («день в день»: {local.length})
                      </span>
                    </span>
                    {chosen.length > 0 && (
                      <span className="min-w-0 flex-1 truncate text-xs text-slate-400">
                        {chosen.map((w) => w.name).join(", ")}
                      </span>
                    )}
                  </button>

                  {pickerOpen && (
                    <div className="mt-2 space-y-3">
                      {local.length > 0 && (
                        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                          {local.map((w) => renderWarehouse(w, fbs.id, enabled))}
                        </div>
                      )}

                      {local.length > 0 && delivery.length > 0 && (
                        <p className="text-xs text-slate-400">
                          Выше — склады «день в день» ({local.length}). Остальные едут
                          дольше и для FBS обычно не подходят по срокам.
                        </p>
                      )}

                      {/* Едут дольше одного дня — отдельной свёрнутой группой, по
                          возрастанию срока: сверху те, что ещё пригодны для FBS. */}
                      {delivery.length > 0 && (
                        <div>
                          <button
                            type="button"
                            onClick={() => toggleDelivery(fbs.id)}
                            className="flex items-center gap-2 text-sm text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200"
                          >
                            <span className="text-slate-400">{deliveryOpen ? "▾" : "▸"}</span>
                            С доставкой ({delivery.length})
                            <span className="text-xs text-slate-400">
                              — от {Math.min(...delivery.map((w) => w.logistic_days ?? 0))} до{" "}
                              {Math.max(...delivery.map((w) => w.logistic_days ?? 0))} дн.
                            </span>
                          </button>
                          {deliveryOpen && (
                            <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                              {delivery.map((w) => renderWarehouse(w, fbs.id, enabled))}
                            </div>
                          )}
                        </div>
                      )}

                      {available.length === 0 && (
                        <p className="text-xs text-slate-500">
                          С этого адреса складов не найдено — проверьте подключение 4tochki.
                        </p>
                      )}
                    </div>
                  )}
                </>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function WarehouseBinding({
  view,
  supplierId,
  onSaved,
}: {
  view: WarehouseMappingsView;
  supplierId: number;
  onSaved: () => Promise<void>;
}) {
  return (
    <section className="rounded-xl border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-900">
      <h2 className="font-semibold tracking-tight">Склады: 4tochki → FBS</h2>
      <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">
        Мультисклад: у каждого FBS-склада свой город приёмки. Ижевский FBS продаёт с
        ижевских складов, московский — с московских. Выберите городу FBS-склада адрес и
        отметьте склады-источники: остаток на площадку считается как их сумма, а заказ
        поедет на адрес этого же FBS-склада. Один склад 4tochki можно отдать только
        одному FBS — иначе его остаток ушёл бы дважды и получился бы оверселл.
      </p>

      {view.addresses.length === 0 ? (
        <p className="mt-4 rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:bg-amber-950 dark:text-amber-300">
          Нет адресов доставки. Заведите адрес в личном кабинете 4tochki и нажмите
          «Проверить подключение».
        </p>
      ) : (
        <div className="mt-4 space-y-4">
          {view.platforms.map((p) => (
            <PlatformBinding
              key={p.platform}
              platform={p}
              addresses={view.addresses}
              warehousesByAddress={view.warehouses_by_address}
              supplierId={supplierId}
              onSaved={onSaved}
            />
          ))}
        </div>
      )}
    </section>
  );
}

export function ConnectionsPage() {
  const { current } = useSupplier();
  const supplierId = current?.id;

  const [creds, setCreds] = useState<Credential[]>([]);
  const [mappings, setMappings] = useState<WarehouseMappingsView | null>(null);
  const [checking, setChecking] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [ft, setFt] = useState({ login: "", password: "" });
  const [wb, setWb] = useState({ api_key: "" });
  const [ozon, setOzon] = useState({ client_id: "", api_key: "" });

  const load = async () => {
    if (!supplierId) return;
    setCreds(await api.get<Credential[]>(`/suppliers/${supplierId}/connections`));
  };

  const loadMappings = async () => {
    if (!supplierId) return;
    setMappings(
      await api.get<WarehouseMappingsView>(
        `/suppliers/${supplierId}/connections/warehouse-mappings`,
      ),
    );
  };

  useEffect(() => {
    void load();
    void loadMappings();
  }, [supplierId]);

  const byPlatform = (p: string) => creds.find((c) => c.platform === p);
  const ftCred = byPlatform("fourtochki");

  const run = async (label: string, fn: () => Promise<unknown>) => {
    setChecking(label);
    setError(null);
    try {
      await fn();
      await load();
      // Проверка WB/Ozon освежает FBS-склады в БД, смена складов 4tochki — список для
      // привязки: и то и другое отражаем сразу.
      await loadMappings();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка");
    } finally {
      setChecking(null);
    }
  };

  // Отдельного выбора складов больше нет: набор отслеживаемых складов выводится из
  // привязок к FBS-складам (см. блок «Склады: 4tochki → FBS»), а адрес выбирается
  // там же — свой для каждого FBS-склада.

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Подключения</h1>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          Доступы привязаны к поставщику «{current?.name}». Секреты хранятся в базе только
          в зашифрованном виде.
        </p>
      </div>

      {error && (
        <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">{error}</p>
      )}

      <div className="grid gap-6 lg:grid-cols-3">
        <Card
          title="4tochki"
          subtitle="SOAP B2B API — логин и пароль кабинета"
          cred={ftCred}
          checking={checking === "fourtochki"}
          onCheck={() =>
            run("fourtochki", async () => {
              if (ft.login && ft.password) {
                await api.put(`/suppliers/${supplierId}/connections/fourtochki`, {
                  login: ft.login,
                  password: ft.password,
                  selected_warehouses: ftCred?.selected_warehouses ?? [],
                });
                setFt({ login: "", password: "" });
              }
              await api.post(`/suppliers/${supplierId}/connections/fourtochki/check`);
            })
          }
        >
          <Field
            label="Логин"
            value={ft.login}
            onChange={(v) => setFt({ ...ft, login: v })}
            placeholder={ftCred?.secrets_masked.login ?? "логин B2B-кабинета"}
          />
          <Field
            label="Пароль"
            type="password"
            value={ft.password}
            onChange={(v) => setFt({ ...ft, password: v })}
            placeholder={ftCred?.secrets_masked.password ?? "пароль"}
          />
          {ftCred && <TestModeToggle cred={ftCred} supplierId={supplierId!} onSaved={load} />}
        </Card>

        <Card
          title="Wildberries"
          subtitle="Один токен Seller API — категории отмечаются при его создании"
          cred={byPlatform("wb")}
          checking={checking === "wb"}
          onCheck={() =>
            run("wb", async () => {
              if (wb.api_key) {
                await api.put(`/suppliers/${supplierId}/connections/wb`, wb);
                setWb({ api_key: "" });
              }
              await api.post(`/suppliers/${supplierId}/connections/wb/check`);
            })
          }
        >
          <Field
            label="API-ключ"
            type="password"
            value={wb.api_key}
            onChange={(v) => setWb({ api_key: v })}
            placeholder={byPlatform("wb")?.secrets_masked.api_key ?? "токен Seller API"}
          />

          <Scopes
            title="При создании токена в кабинете WB отметьте категории:"
            required={[
              ["Контент", "создание и редактирование карточек товара"],
              ["Цены и скидки", "установка цен с наценкой"],
              ["Маркетплейс", "остатки FBS и сборочные задания (заказы)"],
            ]}
            note="Уровень доступа — «Чтение и запись». Остальные категории (Статистика, Финансы, Аналитика, Продвижение, Вопросы и отзывы, Чат, Поставки, Возвраты, Документы, Пользователи) не нужны — не включайте их, чтобы не расширять права токена."
          />
        </Card>

        <Card
          title="Ozon"
          subtitle="Seller API — Client-Id и Api-Key"
          cred={byPlatform("ozon")}
          checking={checking === "ozon"}
          onCheck={() =>
            run("ozon", async () => {
              if (ozon.client_id && ozon.api_key) {
                await api.put(`/suppliers/${supplierId}/connections/ozon`, ozon);
                setOzon({ client_id: "", api_key: "" });
              }
              await api.post(`/suppliers/${supplierId}/connections/ozon/check`);
            })
          }
        >
          <Field
            label="Client-Id"
            value={ozon.client_id}
            onChange={(v) => setOzon({ ...ozon, client_id: v })}
            placeholder={byPlatform("ozon")?.secrets_masked.client_id ?? "Client-Id"}
          />
          <Field
            label="Api-Key"
            type="password"
            value={ozon.api_key}
            onChange={(v) => setOzon({ ...ozon, api_key: v })}
            placeholder={byPlatform("ozon")?.secrets_masked.api_key ?? "Api-Key"}
          />

          <Scopes
            title="При создании Api-Key отметьте типы токена:"
            required={[
              ["Product", "создание и обновление товаров, цены, остатки"],
              ["Description Category", "дерево категорий и характеристики — без них карточку не собрать"],
              ["Warehouse", "склады: остатки FBS привязаны к складу"],
              ["Posting FBS", "заказы FBS: получение и отгрузка"],
            ]}
            optional={[
              ["Notification", "push о новых заказах вместо опроса"],
              ["Certification", "шины подлежат обязательной сертификации"],
              ["Brand", "проверка, что бренд разрешён к продаже"],
            ]}
            note="Не выбирайте роль Admin: она даёт доступ ко всем 460 методам Seller API, и утечка такого токена равносильна утечке всего кабинета."
          />
        </Card>
      </div>

      {ftCred && ftCred.addresses.length > 0 && (
        <section className="rounded-xl border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-900">
          <h2 className="font-semibold tracking-tight">Адреса приёмки 4tochki</h2>
          <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">
            Адреса заводятся в личном кабинете 4tochki — мы их только читаем. От адреса
            зависят и набор доступных складов, и срок доставки с каждого: один и тот же
            склад в разные города едет разное время. Какие склады использовать —
            настраивается ниже, отдельно для каждого FBS-склада.
          </p>

          <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {ftCred.addresses.map((a) => (
              <div
                key={a.id}
                className="rounded-lg border border-slate-200 p-3 dark:border-slate-700"
              >
                <p className="truncate text-sm font-medium">{a.title}</p>
                <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                  Складов: {a.warehouse_count ?? "—"}
                  {a.same_day_count !== null && (
                    <>
                      {" · "}
                      <span className="text-emerald-600 dark:text-emerald-400">
                        «день в день»: {a.same_day_count}
                      </span>
                    </>
                  )}
                </p>
              </div>
            ))}
          </div>

          <p className="mt-3 text-xs text-slate-400">
            Отслеживаются склады, привязанные к FBS: {ftCred.selected_warehouses.length}.
            Остатки в каталоге считаются по ним.
          </p>
        </section>
      )}

      {mappings && (
        <WarehouseBinding
          view={mappings}
          supplierId={supplierId!}
          onSaved={loadMappings}
        />
      )}
    </div>
  );
}
