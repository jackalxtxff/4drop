import { createContext, useContext, useEffect, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";

import { api, auth, type Supplier } from "../api";
import { SupplierDialog } from "./SupplierDialog";
import { ThemeToggle } from "./ThemeToggle";

interface SupplierCtx {
  suppliers: Supplier[];
  current: Supplier | null;
  setCurrentId: (id: number) => void;
  reload: () => Promise<void>;
}

const Ctx = createContext<SupplierCtx | null>(null);

/** Активный поставщик — контекст для всего UI: каталог, доступы и заказы принадлежат ему. */
export function useSupplier(): SupplierCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useSupplier вызван вне Layout");
  return ctx;
}

const CURRENT_KEY = "4drop.supplier";

export function Layout() {
  const navigate = useNavigate();
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [currentId, setCurrentIdState] = useState<number | null>(() => {
    const saved = localStorage.getItem(CURRENT_KEY);
    return saved ? Number(saved) : null;
  });
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [editing, setEditing] = useState(false);

  const setCurrentId = (id: number) => {
    setCurrentIdState(id);
    localStorage.setItem(CURRENT_KEY, String(id));
  };

  const reload = async () => {
    const list = await api.get<Supplier[]>("/suppliers");
    setSuppliers(list);
    // Выбранный поставщик мог быть удалён — не оставляем UI указывать в пустоту.
    if (list.length && !list.some((s) => s.id === currentId)) setCurrentId(list[0].id);
  };

  useEffect(() => {
    void reload();
  }, []);

  const current = suppliers.find((s) => s.id === currentId) ?? null;

  const createSupplier = async () => {
    if (!newName.trim()) return;
    const created = await api.post<Supplier>("/suppliers", { name: newName.trim() });
    setNewName("");
    setCreating(false);
    await reload();
    setCurrentId(created.id);
  };

  const logout = () => {
    auth.token = null;
    navigate("/login");
  };

  const linkClass = ({ isActive }: { isActive: boolean }) =>
    `px-3 py-2 text-sm rounded-md transition ${
      isActive
        ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900"
        : "text-slate-600 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800"
    }`;

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <header className="border-b border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="mx-auto flex max-w-[1600px] items-center gap-6 px-6 py-3">
          <span className="font-semibold tracking-tight">4drop</span>

          <nav className="flex gap-1">
            <NavLink to="/products" className={linkClass}>
              Товары
            </NavLink>
            <NavLink to="/orders" className={linkClass}>
              Заказы
            </NavLink>
            <NavLink to="/connections" className={linkClass}>
              Подключения
            </NavLink>
            <NavLink to="/sync" className={linkClass}>
              Синхронизация
            </NavLink>
          </nav>

          <div className="ml-auto flex items-center gap-3">
            {creating ? (
              <div className="flex items-center gap-2">
                <input
                  autoFocus
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && void createSupplier()}
                  placeholder="Название поставщика"
                  className="rounded-md border border-slate-300 px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-800"
                />
                <button
                  onClick={() => void createSupplier()}
                  className="rounded-md bg-slate-900 px-3 py-1.5 text-sm text-white dark:bg-slate-100 dark:text-slate-900"
                >
                  Создать
                </button>
                <button
                  onClick={() => setCreating(false)}
                  className="text-sm text-slate-500 hover:text-slate-800 dark:hover:text-slate-200"
                >
                  Отмена
                </button>
              </div>
            ) : (
              <>
                <label className="text-sm text-slate-500 dark:text-slate-400">Поставщик</label>
                <select
                  value={currentId ?? ""}
                  onChange={(e) => setCurrentId(Number(e.target.value))}
                  className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-800"
                >
                  {suppliers.length === 0 && <option value="">— нет —</option>}
                  {suppliers.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name} ({s.product_count.toLocaleString("ru")})
                    </option>
                  ))}
                </select>
                {current && (
                  <button
                    onClick={() => setEditing(true)}
                    title={`Переименовать «${current.name}»`}
                    aria-label="Настройки поставщика"
                    className="rounded-md border border-slate-300 px-2.5 py-1.5 text-sm hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
                  >
                    Изменить
                  </button>
                )}
                <button
                  onClick={() => setCreating(true)}
                  className="rounded-md border border-slate-300 px-2.5 py-1.5 text-sm hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
                >
                  + Добавить
                </button>
              </>
            )}

            <ThemeToggle />

            <button
              onClick={logout}
              className="text-sm text-slate-500 hover:text-slate-800 dark:hover:text-slate-200"
            >
              Выйти
            </button>
          </div>
        </div>
      </header>

      {editing && current && (
        <SupplierDialog
          supplier={current}
          onClose={() => setEditing(false)}
          onSaved={reload}
        />
      )}

      <main className="mx-auto max-w-[1600px] px-6 py-6">
        {suppliers.length === 0 ? (
          <div className="rounded-lg border border-dashed border-slate-300 bg-white p-12 text-center dark:border-slate-700 dark:bg-slate-900">
            <p className="text-slate-600 dark:text-slate-400">
              Поставщиков пока нет. Создайте первого — к нему привяжутся доступы к 4tochki,
              Wildberries и Ozon, каталог и заказы.
            </p>
            <button
              onClick={() => setCreating(true)}
              className="mt-4 rounded-md bg-slate-900 px-4 py-2 text-sm text-white dark:bg-slate-100 dark:text-slate-900"
            >
              Создать поставщика
            </button>
          </div>
        ) : (
          <Ctx.Provider value={{ suppliers, current, setCurrentId, reload }}>
            <Outlet />
          </Ctx.Provider>
        )}
      </main>
    </div>
  );
}
