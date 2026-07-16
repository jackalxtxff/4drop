import { useEffect, useState } from "react";

import { api, type Credential } from "../api";
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

export function ConnectionsPage() {
  const { current } = useSupplier();
  const supplierId = current?.id;

  const [creds, setCreds] = useState<Credential[]>([]);
  const [checking, setChecking] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [ft, setFt] = useState({ login: "", password: "" });
  const [wb, setWb] = useState({ api_key: "" });
  const [ozon, setOzon] = useState({ client_id: "", api_key: "" });

  const load = async () => {
    if (!supplierId) return;
    setCreds(await api.get<Credential[]>(`/suppliers/${supplierId}/connections`));
  };

  useEffect(() => {
    void load();
  }, [supplierId]);

  const byPlatform = (p: string) => creds.find((c) => c.platform === p);
  const ftCred = byPlatform("fourtochki");

  const run = async (label: string, fn: () => Promise<unknown>) => {
    setChecking(label);
    setError(null);
    try {
      await fn();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка");
    } finally {
      setChecking(null);
    }
  };

  const toggleWarehouse = async (id: number) => {
    if (!ftCred) return;
    const next = ftCred.selected_warehouses.includes(id)
      ? ftCred.selected_warehouses.filter((w) => w !== id)
      : [...ftCred.selected_warehouses, id];
    await run("wrh", () =>
      api.put(`/suppliers/${supplierId}/connections/fourtochki/warehouses`, next),
    );
  };

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

      {ftCred && ftCred.warehouses.length > 0 && (
        <section className="rounded-xl border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-900">
          <h2 className="font-semibold tracking-tight">Склады 4tochki</h2>
          <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">
            Остатки берутся только с выбранных складов. Срок логистики напрямую съедает SLA
            сборки на маркетплейсе — склады с долгой доставкой лучше не включать.
          </p>

          {ftCred.selected_warehouses.length === 0 && (
            <p className="mt-3 rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:bg-amber-950 dark:text-amber-300">
              Ни один склад не выбран — остатки по всем товарам будут нулевыми.
            </p>
          )}

          <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {ftCred.warehouses.map((w) => {
              const checked = ftCred.selected_warehouses.includes(w.id);
              const slow = (w.logistic_days ?? 0) > 2;
              return (
                <label
                  key={w.id}
                  className={`flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition ${
                    checked
                      ? "border-slate-900 bg-slate-50 dark:border-slate-400 dark:bg-slate-800"
                      : "border-slate-200 hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => void toggleWarehouse(w.id)}
                    className="mt-0.5"
                  />
                  <span className="min-w-0">
                    <span className="block truncate text-sm font-medium">{w.name}</span>
                    <span className="mt-0.5 block text-xs text-slate-500 dark:text-slate-400">
                      Логистика: {w.logistic_days ?? "—"} дн.
                      {slow && <span className="ml-1 text-amber-700">риск для SLA</span>}
                      {w.is_paid_delivery && <span className="ml-1">· платная доставка</span>}
                    </span>
                  </span>
                </label>
              );
            })}
          </div>
        </section>
      )}
    </div>
  );
}
