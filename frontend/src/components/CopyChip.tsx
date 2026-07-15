import { useState } from "react";

interface Props {
  /** Текст, который вставляется в формулу и копируется в буфер. */
  value: string;
  /** Пояснение справа от чипа (необязательно). */
  hint?: string;
  /** Цветовая группа: переменная или функция. */
  kind?: "var" | "func";
  /** Клик по чипу помимо копирования — например, вставить в активное поле. */
  onPick?: (value: string) => void;
}

const STYLE = {
  var: "bg-violet-50 text-violet-700 ring-violet-200 hover:bg-violet-100 dark:bg-violet-950/50 dark:text-violet-300 dark:ring-violet-900 dark:hover:bg-violet-900/50",
  func: "bg-sky-50 text-sky-700 ring-sky-200 hover:bg-sky-100 dark:bg-sky-950/50 dark:text-sky-300 dark:ring-sky-900 dark:hover:bg-sky-900/50",
};

/** Копирование, работающее и по HTTP. navigator.clipboard доступен только в
 *  secure context (https/localhost); при доступе по IP его нет, поэтому нужен
 *  запасной путь через скрытый textarea + execCommand. */
async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      /* провалимся в fallback ниже */
    }
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

export function CopyChip({ value, hint, kind = "var", onPick }: Props) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    const ok = await copyText(value);
    onPick?.(value);
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1000);
    }
  };

  return (
    <button
      type="button"
      onClick={copy}
      title={`Нажмите, чтобы скопировать «${value}»${hint ? ` — ${hint}` : ""}`}
      className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 font-mono text-xs ring-1 ring-inset transition ${STYLE[kind]}`}
    >
      {copied ? (
        <>
          <svg className="h-3 w-3" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2.5">
            <path d="M4 10l4 4 8-8" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          скопировано
        </>
      ) : (
        value
      )}
    </button>
  );
}
