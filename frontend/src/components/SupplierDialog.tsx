import { useEffect, useState } from "react";

import { api, type Supplier } from "../api";

interface Props {
  supplier: Supplier;
  onClose: () => void;
  onSaved: () => Promise<void>;
}

export function SupplierDialog({ supplier, onClose, onSaved }: Props) {
  const [name, setName] = useState(supplier.name);
  const [comment, setComment] = useState(supplier.comment ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const dirty = name.trim() !== supplier.name || comment !== (supplier.comment ?? "");

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !dirty) return;

    setBusy(true);
    setError(null);
    try {
      await api.patch<Supplier>(`/suppliers/${supplier.id}`, {
        name: name.trim(),
        comment: comment.trim() || null,
      });
      await onSaved();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сохранить");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4 dark:bg-slate-950/60"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <form
        onSubmit={save}
        className="w-full max-w-md rounded-xl border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-900 shadow-xl"
      >
        <h2 className="font-semibold tracking-tight">Настройки поставщика</h2>
        <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">
          Переименование не затрагивает доступы, каталог и заказы — они привязаны к
          поставщику, а не к его названию.
        </p>

        <label className="mt-5 block text-sm font-medium text-slate-700 dark:text-slate-300">Название</label>
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={255}
          className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-900 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 dark:focus:border-slate-400"
        />
        {!name.trim() && (
          <p className="mt-1 text-xs text-red-600">Название не может быть пустым</p>
        )}

        <label className="mt-4 block text-sm font-medium text-slate-700 dark:text-slate-300">
          Комментарий <span className="font-normal text-slate-400">— необязательно</span>
        </label>
        <textarea
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          rows={3}
          placeholder="Например: основной кабинет, работаем со складов Ижевска"
          className="mt-1 w-full resize-none rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-900 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 dark:focus:border-slate-400"
        />

        {error && (
          <p className="mt-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">{error}</p>
        )}

        <div className="mt-6 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-slate-300 px-3 py-2 text-sm hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
          >
            Отмена
          </button>
          <button
            type="submit"
            disabled={busy || !dirty || !name.trim()}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm text-white dark:bg-slate-100 dark:text-slate-900 disabled:opacity-40"
          >
            {busy ? "Сохраняем…" : "Сохранить"}
          </button>
        </div>
      </form>
    </div>
  );
}
