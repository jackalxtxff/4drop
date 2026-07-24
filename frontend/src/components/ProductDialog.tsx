import { useEffect, useState } from "react";

import { api, type ProductDetail } from "../api";

/** Человекочитаемые названия атрибутов 4tochki. Ключи, которых тут нет, показываем
 *  как есть — чтобы новые атрибуты поставщика не пропадали из карточки. */
const ATTR_LABEL: Record<string, string> = {
  aquaplaning: "Аквапланирование",
  axle: "Ось",
  brand: "Бренд",
  camera: "Камерность",
  code: "Артикул 4tochki (CAE)",
  comfort: "Комфорт",
  constr: "Конструкция",
  diameter: "Диаметр, дюйм",
  diameter_out: "Наружный диаметр",
  grip: "Сцепление",
  height: "Высота профиля, %",
  initial_tread_depth: "Глубина протектора (новая)",
  load_index: "Индекс нагрузки",
  marker_color: "Цвет маркера",
  model: "Модель",
  moto_use: "Мотоприменение",
  name: "Наименование",
  noise: "Шумность",
  number_layers_treadmill: "Слоёв в протекторе",
  omolog: "Омологация",
  passability: "Проходимость",
  protection: "Защита",
  protector_type: "Тип протектора",
  puncture: "Проколостойкость",
  season: "Сезон",
  side: "Сторона",
  sloy: "Слойность",
  softness: "Мягкость",
  speed_index: "Индекс скорости",
  strengthening: "Усиленная",
  sub_diameter: "Доп. диаметр",
  sub_diameter_out: "Доп. наружный диаметр",
  subheight: "Доп. высота профиля",
  subwidth: "Доп. ширина",
  tech: "Технология",
  thorn: "Шипы",
  thorn_type: "Тип шипов",
  tn_ved: "ТН ВЭД",
  tonnage: "Нагрузочность (XL/C)",
  tread_width: "Ширина протектора",
  type: "Тип товара",
  usa: "Рынок США",
  use_type: "Назначение",
  volume: "Объём, м³",
  wear_index: "Индекс износа",
  weight: "Вес, кг",
  width: "Ширина, мм",
};

/** Медиа и разметка — это не характеристики, в списке они только шумят. */
const HIDDEN_ATTRS = new Set([
  "img_big",
  "img_small",
  "img_big_my",
  "img_big_pish",
  "marka_html",
  "marka_logo",
  "model_html",
]);

const SEASON: Record<string, string> = { s: "Летняя", w: "Зимняя", u: "Всесезонная" };
const GOODS_TYPE: Record<string, string> = {
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

/** Значение атрибута в читаемый вид: коды сезона/типа расшифровываем, булевы — Да/Нет. */
function formatValue(key: string, value: unknown): string {
  if (typeof value === "boolean") return value ? "Да" : "Нет";
  if (key === "season" && typeof value === "string") return SEASON[value] ?? value;
  if (key === "type" && typeof value === "string") return GOODS_TYPE[value] ?? value;
  if (typeof value === "number") {
    // 185.0 → 185, но 0.075 оставляем как есть
    return Number.isInteger(value) ? String(value) : String(value);
  }
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

const isEmpty = (v: unknown) =>
  v === null || v === undefined || v === "" || (Array.isArray(v) && v.length === 0);

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-3 border-b border-slate-100 py-1.5 last:border-0 dark:border-slate-800">
      <span className="w-1/2 shrink-0 text-slate-500 dark:text-slate-400">{label}</span>
      <span className="min-w-0 flex-1 break-words text-slate-900 dark:text-slate-100">
        {value}
      </span>
    </div>
  );
}

const money = (v: string | null) =>
  v === null ? "—" : Number(v).toLocaleString("ru", { maximumFractionDigits: 2 }) + " ₽";

export function ProductDialog({
  supplierId,
  productId,
  onClose,
}: {
  supplierId: number;
  productId: number;
  onClose: () => void;
}) {
  const [product, setProduct] = useState<ProductDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    let alive = true;
    api
      .get<ProductDetail>(`/suppliers/${supplierId}/products/${productId}`)
      .then((p) => alive && setProduct(p))
      .catch((e) => alive && setError(e instanceof Error ? e.message : "Не удалось загрузить"));
    return () => {
      alive = false;
    };
  }, [supplierId, productId]);

  // Все атрибуты 4tochki, кроме медиа: непустые — в список, пустые считаем для подписи.
  const attrs = product?.attrs ?? {};
  const shown = Object.entries(attrs)
    .filter(([k, v]) => !HIDDEN_ATTRS.has(k) && !isEmpty(v))
    .sort(([a], [b]) => (ATTR_LABEL[a] ?? a).localeCompare(ATTR_LABEL[b] ?? b, "ru"));
  const emptyCount = Object.entries(attrs).filter(
    ([k, v]) => !HIDDEN_ATTRS.has(k) && isEmpty(v),
  ).length;

  const image = product?.img_big || product?.img_small;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-slate-900/40 p-4 dark:bg-slate-950/60"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="my-8 w-full max-w-3xl rounded-xl border border-slate-200 bg-white shadow-xl dark:border-slate-800 dark:bg-slate-900">
        <div className="flex items-start gap-3 border-b border-slate-100 p-5 dark:border-slate-800">
          <div className="min-w-0 flex-1">
            <h2 className="font-semibold tracking-tight">
              {product?.name ?? (error ? "Ошибка" : "Загрузка…")}
            </h2>
            {product && (
              <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">
                {[product.brand, product.model].filter(Boolean).join(" ")}
                <span className="ml-2 font-mono text-xs">{product.cae}</span>
              </p>
            )}
          </div>
          <button
            onClick={onClose}
            aria-label="Закрыть"
            className="shrink-0 rounded-md px-2 py-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700 dark:hover:bg-slate-800 dark:hover:text-slate-200"
          >
            ✕
          </button>
        </div>

        {error && <p className="p-5 text-sm text-red-600">{error}</p>}
        {!product && !error && (
          <p className="p-5 text-sm text-slate-500">Загружаем характеристики…</p>
        )}

        {product && (
          <div className="p-5">
            <div className="flex flex-col gap-5 sm:flex-row">
              {image && (
                <img
                  src={image}
                  alt={product.name ?? ""}
                  className="h-40 w-40 shrink-0 self-start rounded-lg border border-slate-200 object-contain p-2 dark:border-slate-700"
                />
              )}

              {/* Наши данные: остаток, цены, интеграции — их нет в атрибутах поставщика. */}
              <div className="min-w-0 flex-1 text-sm">
                <h3 className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">
                  Наличие и цены
                </h3>
                <Row label="Остаток (выбранные склады)" value={String(product.total_rest)} />
                <Row label="Уйдёт на маркетплейс" value={String(product.marketplace_rest)} />
                <Row label="Закупочная (мин.)" value={money(product.min_price)} />
                <Row label="Розница 4tochki" value={money(product.price_rozn)} />
                <Row
                  label="Интеграции"
                  value={
                    product.integrations.length === 0
                      ? "нет"
                      : product.integrations
                          .map((l) => `${l.platform.toUpperCase()}: ${l.status}`)
                          .join(", ")
                  }
                />
                <Row
                  label="Синхронизация"
                  value={product.sync_blocked ? "заблокирована" : "активна"}
                />
              </div>
            </div>

            <h3 className="mb-1 mt-6 text-xs font-medium uppercase tracking-wide text-slate-400">
              Характеристики 4tochki
            </h3>
            <div className="grid gap-x-8 text-sm sm:grid-cols-2">
              {shown.map(([key, value]) => (
                <Row
                  key={key}
                  label={ATTR_LABEL[key] ?? key}
                  value={formatValue(key, value)}
                />
              ))}
            </div>
            {shown.length === 0 && (
              <p className="text-sm text-slate-500">Атрибуты не заполнены поставщиком.</p>
            )}
            {emptyCount > 0 && (
              <p className="mt-3 text-xs text-slate-400">
                Скрыто незаполненных атрибутов: {emptyCount}
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
