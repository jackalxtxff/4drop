// По умолчанию — относительный путь через прокси Vite (см. vite.config.ts).
// Абсолютный VITE_API_URL сломал бы доступ к интерфейсу с любого хоста, кроме localhost.
const BASE = "/api";

const TOKEN_KEY = "4drop.token";

export const auth = {
  get token() {
    return localStorage.getItem(TOKEN_KEY);
  },
  set token(value: string | null) {
    if (value) localStorage.setItem(TOKEN_KEY, value);
    else localStorage.removeItem(TOKEN_KEY);
  },
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  if (auth.token) headers.set("Authorization", `Bearer ${auth.token}`);

  const resp = await fetch(`${BASE}${path}`, { ...init, headers });

  if (resp.status === 401) {
    auth.token = null;
    window.location.href = "/login";
    throw new ApiError("Сессия истекла", 401);
  }

  if (!resp.ok) {
    // FastAPI кладёт текст ошибки в detail; показываем его пользователю как есть,
    // иначе на экране будет бесполезное "500".
    let detail = `Ошибка ${resp.status}`;
    try {
      const body = await resp.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* тело не JSON — оставляем код */
    }
    throw new ApiError(detail, resp.status);
  }

  if (resp.status === 204) return undefined as T;
  return resp.json();
}

export const api = {
  get: <T,>(path: string) => request<T>(path),
  post: <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body ? JSON.stringify(body) : undefined }),
  put: <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: "PUT", body: body ? JSON.stringify(body) : undefined }),
  patch: <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  del: (path: string) => request<void>(path, { method: "DELETE" }),
};

// --- типы ------------------------------------------------------------------

export interface Supplier {
  id: number;
  name: string;
  comment: string | null;
  is_active: boolean;
  catalog_synced_at: string | null;
  product_count: number;
}

export interface Warehouse {
  id: number;
  name: string;
  short_name: string | null;
  logistic_days: number | null;
  have_delivery: boolean;
  is_paid_delivery: boolean;
}

export type ConnectionStatus = "not_configured" | "ok" | "error";

export interface Credential {
  platform: "fourtochki" | "wb" | "ozon";
  status: ConnectionStatus;
  status_message: string | null;
  checked_at: string | null;
  secrets_masked: Record<string, string>;
  warehouses: Warehouse[];
  selected_warehouses: number[];
}

export interface Product {
  id: number;
  cae: string;
  goods_type: string;
  brand: string | null;
  model: string | null;
  name: string | null;
  season: string | null;
  thorn: boolean | null;
  tyre_type: string | null;
  constr: string | null;
  camera: string | null;
  noise: string | null;
  strengthening: boolean | null;
  width: string | null;
  height: string | null;
  diameter: string | null;
  load_index: string | null;
  speed_index: string | null;
  img_small: string | null;
  img_big: string | null;
  total_rest: number;
  marketplace_rest: number;
  min_price: string | null;
  price_rozn: string | null;
  integration_status: "none" | "pending" | "active" | "rejected" | "error";
  sync_blocked: boolean;
  integrations: ProductLink[];
}

export interface ProductLink {
  platform: "wb" | "ozon";
  status: "none" | "pending" | "active" | "rejected" | "error";
  status_message: string | null;
  nm_id: number | null;
}

export interface ProductPage {
  items: Product[];
  total: number;
  page: number;
  page_size: number;
  in_stock_count: number;
  total_rest: number;
  stock_buffer: number;
}

export interface Facets {
  brands: string[];
  seasons: string[];
  goods_types: string[];
  diameters: string[];
  widths: string[];
  heights: string[]; // профиль
  tyre_types: string[];
  constrs: string[];
  cameras: string[];
}

export interface ProductStock {
  wrh: number;
  name: string | null;
  rest: number;
  price: string | null;
  logistic_days: number | null;
  selected: boolean;
}

export type SortField =
  | "cae"
  | "brand"
  | "model"
  | "name"
  | "season"
  | "width"
  | "height"
  | "diameter"
  | "tyre_type"
  | "constr"
  | "camera"
  | "noise"
  | "total_rest"
  | "min_price"
  | "integration_status";

export interface SyncSettings {
  catalog_interval_minutes: number;
  stocks_interval_minutes: number;
  push_interval_minutes: number;
  cards_update_interval_minutes: number;
  auto_mode: boolean;
  auto_cards_interval_minutes: number;
  auto_cards_batch_limit: number;
  missing_strategy: "zero_stock" | "delete";
  stock_buffer: number;
  wb_price_formula: string;
  ozon_price_formula: string;
  wb_price_before_formula: string;
  ozon_price_before_formula: string;
  updated_at: string;
}

export interface FormulaPreview {
  ok: boolean;
  price: string | null;
  error: string | null;
}

export interface SyncJobPage {
  items: SyncJob[];
  total: number;
  offset: number;
  limit: number;
}

export interface SyncJob {
  id: number;
  kind: string;
  status: string;
  total: number;
  processed: number;
  failed: number;
  message: string | null;
  started_at: string;
  finished_at: string | null;
}
