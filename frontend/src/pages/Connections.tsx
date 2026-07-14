import { useEffect, useState } from "react";

import { api, type Credential } from "../api";
import { useSupplier } from "../components/Layout";

const STATUS_STYLE: Record<string, string> = {
  ok: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  error: "bg-red-50 text-red-700 ring-red-200",
  not_configured: "bg-slate-100 text-slate-600 ring-slate-200",
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
    <section className="rounded-xl border border-slate-200 bg-white p-6">
      <div className="flex items-start justify-between">
        <div>
          <h2 className="font-semibold tracking-tight">{title}</h2>
          <p className="mt-0.5 text-sm text-slate-500">{subtitle}</p>
        </div>
        <StatusBadge status={cred?.status ?? "not_configured"} />
      </div>

      {cred?.status_message && (
        <p
          className={`mt-3 rounded-md px-3 py-2 text-sm ${
            cred.status === "error" ? "bg-red-50 text-red-700" : "bg-slate-50 text-slate-600"
          }`}
        >
          {cred.status_message}
        </p>
      )}

      <div className="mt-4 space-y-3">{children}</div>

      <button
        onClick={onCheck}
        disabled={checking}
        className="mt-4 rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50 disabled:opacity-50"
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
      <label className="block text-sm font-medium text-slate-700">{label}</label>
      <input
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-900 focus:outline-none"
      />
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
  const [wb, setWb] = useState({ content: "", prices: "", marketplace: "" });
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
        <p className="mt-1 text-sm text-slate-500">
          Доступы привязаны к поставщику «{current?.name}». Секреты хранятся в базе только
          в зашифрованном виде.
        </p>
      </div>

      {error && (
        <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>
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
          subtitle="API-ключи по разделам Seller API"
          cred={byPlatform("wb")}
          checking={checking === "wb"}
          onCheck={() =>
            run("wb", async () => {
              if (wb.content || wb.prices || wb.marketplace) {
                await api.put(`/suppliers/${supplierId}/connections/wb`, wb);
                setWb({ content: "", prices: "", marketplace: "" });
              }
              await api.post(`/suppliers/${supplierId}/connections/wb/check`);
            })
          }
        >
          <Field
            label="Content (карточки)"
            type="password"
            value={wb.content}
            onChange={(v) => setWb({ ...wb, content: v })}
            placeholder={byPlatform("wb")?.secrets_masked.content ?? "ключ Content API"}
          />
          <Field
            label="Prices & Discounts (цены)"
            type="password"
            value={wb.prices}
            onChange={(v) => setWb({ ...wb, prices: v })}
            placeholder={byPlatform("wb")?.secrets_masked.prices ?? "ключ Prices API"}
          />
          <Field
            label="Marketplace (остатки, заказы)"
            type="password"
            value={wb.marketplace}
            onChange={(v) => setWb({ ...wb, marketplace: v })}
            placeholder={byPlatform("wb")?.secrets_masked.marketplace ?? "ключ Marketplace API"}
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
        </Card>
      </div>

      {ftCred && ftCred.warehouses.length > 0 && (
        <section className="rounded-xl border border-slate-200 bg-white p-6">
          <h2 className="font-semibold tracking-tight">Склады 4tochki</h2>
          <p className="mt-0.5 text-sm text-slate-500">
            Остатки берутся только с выбранных складов. Срок логистики напрямую съедает SLA
            сборки на маркетплейсе — склады с долгой доставкой лучше не включать.
          </p>

          {ftCred.selected_warehouses.length === 0 && (
            <p className="mt-3 rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-800">
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
                    checked ? "border-slate-900 bg-slate-50" : "border-slate-200 hover:bg-slate-50"
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
                    <span className="mt-0.5 block text-xs text-slate-500">
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
