/** Set in .env.local — never hardcode production URL in source. */
const API_BASE = (process.env.NEXT_PUBLIC_API_BASE || "").replace(/\/$/, "");

export function getApiBase() {
  return API_BASE;
}

function requireApiBase(): string {
  if (!API_BASE) {
    throw new Error(
      "Missing NEXT_PUBLIC_API_BASE. Copy frontend-next/.env.example → .env.local and set the backend URL."
    );
  }
  return API_BASE;
}

export async function api<T = Record<string, unknown>>(
  path: string,
  opts: {
    method?: string;
    json?: unknown;
    token?: string;
    formData?: FormData;
  } = {}
): Promise<T & { ok?: boolean; error?: string; _status?: number }> {
  const headers: Record<string, string> = {};
  if (opts.token) headers["X-Admin-Token"] = opts.token;

  let body: BodyInit | undefined;
  if (opts.formData) {
    body = opts.formData;
  } else if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.json);
  }

  const res = await fetch(`${requireApiBase()}${path}`, {
    method: opts.method || "GET",
    headers,
    body,
  });

  const data = (await res.json().catch(() => ({}))) as T & {
    ok?: boolean;
    error?: string;
  };
  if (!res.ok && !data.error) {
    (data as { error?: string }).error = `HTTP ${res.status}`;
  }
  (data as { _status?: number })._status = res.status;
  return data as T & { ok?: boolean; error?: string; _status?: number };
}

export type LiveAccount = {
  id: number;
  email?: string;
  plan?: string;
  country?: string;
  status?: string;
  filename?: string;
};

export type Batch = {
  id: string;
  name?: string;
  status?: string;
  total?: number;
  done?: number;
  kept?: number;
  discarded?: number;
  failed?: number;
  percent?: number;
  current_file?: string;
  message?: string;
};

export type BatchItem = {
  id: number;
  filename?: string;
  status?: string;
  status_label?: string;
  email?: string;
  plan?: string;
  country?: string;
  message?: string;
};
