"use client";

import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { api, type LiveAccount } from "@/lib/api";
import { countryLabel } from "@/lib/country";

type Tab = "live" | "single" | "bulk";

type Field = { label: string; value: string };
type CheckResult = {
  ok?: boolean;
  error?: string;
  subscribed?: boolean;
  status?: string;
  plan_label?: string;
  fields?: Field[];
  nftoken?: {
    ok?: boolean;
    error?: string;
    links?: { label: string; url: string }[];
  };
};

function copyText(text: string) {
  if (navigator.clipboard) void navigator.clipboard.writeText(text || "");
}

type LinkItem = { label: string; url: string };

function pickPreferred(links: LinkItem[], kind: "pc" | "phone"): LinkItem | null {
  const filtered = links.filter((l) => {
    const lab = (l.label || "").toLowerCase();
    const isPhone =
      lab.includes("phone") || lab.includes("mobile") || lab.includes("unsupported");
    if (kind === "phone") return isPhone;
    // PC: exclude phone variants
    return (lab.includes("pc") || lab.includes("desktop")) && !isPhone;
  });
  // Prefer browser-safe variants
  const safe = filtered.find((l) => /safe|browser/i.test(l.label));
  if (safe) return safe;
  return filtered[0] || null;
}

function shortUrl(url: string, max = 42): string {
  if (!url) return "";
  try {
    const u = new URL(url);
    const token = u.searchParams.get("nftoken") || "";
    const tip = token ? `…${token.slice(0, 8)}…` : u.pathname;
    const s = `${u.host}${tip ? ` · ${tip}` : ""}`;
    return s.length > max ? s.slice(0, max - 1) + "…" : s;
  } catch {
    return url.length > max ? url.slice(0, max - 1) + "…" : url;
  }
}

/** Compact, mobile-friendly login links panel (replaces 4 bulky rows). */
function LoginLinksPanel({
  links,
  email,
  expires,
  consumed,
  onClose,
  closeLabel = "Đóng",
}: {
  links: LinkItem[];
  email?: string;
  expires?: string;
  consumed?: boolean;
  onClose?: () => void;
  closeLabel?: string;
}) {
  const [copied, setCopied] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [showUrls, setShowUrls] = useState(false);

  const pc = pickPreferred(links, "pc");
  const phone = pickPreferred(links, "phone");
  const primary = [pc, phone].filter(Boolean) as LinkItem[];

  async function doCopy(url: string, key: string) {
    try {
      if (navigator.clipboard) await navigator.clipboard.writeText(url);
      setCopied(key);
      window.setTimeout(() => setCopied(null), 1600);
    } catch {
      /* ignore */
    }
  }

  return (
    <div className="login-panel">
      <div className="login-panel-head">
        <div>
          <div className="login-panel-title">Link đăng nhập Netflix</div>
          {email && <div className="login-panel-sub">{email}</div>}
          {expires && (
            <div className="login-panel-sub muted">Hết hạn (UTC): {expires}</div>
          )}
        </div>
        {consumed && <span className="pill ok">Đã lấy · xóa DB</span>}
      </div>

      {consumed && (
        <p className="login-panel-hint">
          Cookie đã gỡ khỏi catalog. Copy hoặc mở link ngay (token hết hạn nhanh).
        </p>
      )}

      <div className="login-cards">
        {primary.map((l) => {
          const isPhone = /phone|mobile|unsupported/i.test(l.label);
          const key = isPhone ? "phone" : "pc";
          return (
            <div className={`login-card ${isPhone ? "is-phone" : "is-pc"}`} key={l.url}>
              <div className="login-card-top">
                <span className="login-card-icon" aria-hidden>
                  {isPhone ? "📱" : "💻"}
                </span>
                <div className="login-card-meta">
                  <strong>{isPhone ? "Điện thoại" : "PC / Máy tính"}</strong>
                  <span className="muted">
                    {/safe|browser/i.test(l.label) ? "Khuyên dùng (browser-safe)" : l.label}
                  </span>
                </div>
              </div>
              {showUrls && (
                <code className="login-card-url" title={l.url}>
                  {shortUrl(l.url, 56)}
                </code>
              )}
              <div className="login-card-actions">
                <button
                  type="button"
                  className="btn-sm"
                  onClick={() => void doCopy(l.url, key)}
                >
                  {copied === key ? "Đã copy ✓" : "Copy link"}
                </button>
                <a
                  className="btn-sm primary"
                  href={l.url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Mở Netflix
                </a>
              </div>
            </div>
          );
        })}
      </div>

      {links.length > primary.length && (
        <button
          type="button"
          className="login-more-toggle"
          onClick={() => setShowAll((v) => !v)}
        >
          {showAll ? "Thu gọn" : `Hiện thêm ${links.length - primary.length} link khác`}
        </button>
      )}

      {showAll && (
        <div className="login-all-list">
          {links.map((l) => (
            <div className="login-all-item" key={`all-${l.url}-${l.label}`}>
              <span className="login-all-label">{l.label}</span>
              <div className="login-card-actions">
                <button
                  type="button"
                  className="btn-sm"
                  onClick={() => void doCopy(l.url, l.label)}
                >
                  {copied === l.label ? "Đã copy ✓" : "Copy"}
                </button>
                <a
                  className="btn-sm open"
                  href={l.url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Mở
                </a>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="login-panel-footer">
        <button
          type="button"
          className="btn-sm ghost-text"
          onClick={() => setShowUrls((v) => !v)}
        >
          {showUrls ? "Ẩn URL" : "Hiện URL rút gọn"}
        </button>
        {onClose && (
          <button type="button" className="btn soft" onClick={onClose}>
            {closeLabel}
          </button>
        )}
      </div>
    </div>
  );
}

const LIVE_PAGE_SIZE_OPTIONS = [20, 50, 100, 200] as const;
const LIVE_PAGE_SIZE_KEY = "user_live_page_size";

function readStoredPageSize(): number {
  if (typeof window === "undefined") return 50;
  try {
    const n = parseInt(localStorage.getItem(LIVE_PAGE_SIZE_KEY) || "50", 10);
    if ((LIVE_PAGE_SIZE_OPTIONS as readonly number[]).includes(n)) return n;
  } catch {
    /* ignore */
  }
  return 50;
}

export default function UserHome() {
  const [tab, setTab] = useState<Tab>("live");

  // live
  const [liveRows, setLiveRows] = useState<LiveAccount[]>([]);
  const [liveTotal, setLiveTotal] = useState(0);
  const [livePlans, setLivePlans] = useState<{ plan: string; count?: number }[]>([]);
  const [planFilter, setPlanFilter] = useState("");
  const [livePage, setLivePage] = useState(0); // 0-based
  const [livePageSize, setLivePageSize] = useState(50);
  const [liveMsg, setLiveMsg] = useState<{ type: string; text: string } | null>(null);
  const [detail, setDetail] = useState<Record<number, React.ReactNode>>({});
  const [busyId, setBusyId] = useState<number | null>(null);

  useEffect(() => {
    setLivePageSize(readStoredPageSize());
  }, []);

  // single
  const [cookie, setCookie] = useState("");
  const [mode, setMode] = useState("both");
  const [singleMsg, setSingleMsg] = useState<{ type: string; text: string } | null>(null);
  const [singleBusy, setSingleBusy] = useState(false);
  const [singleHtml, setSingleHtml] = useState<React.ReactNode>(null);

  // bulk
  const [bulkFiles, setBulkFiles] = useState<File[]>([]);
  const [bulkRows, setBulkRows] = useState<
    { name: string; status: string; label: string; plan?: string; email?: string; country?: string; msg?: string }[]
  >([]);
  const [bulkMsg, setBulkMsg] = useState<{ type: string; text: string } | null>(null);
  const [bulkRun, setBulkRun] = useState(false);
  const bulkStopRef = useRef(false);
  const [bulkCounts, setBulkCounts] = useState({ LIVE: 0, FREE: 0, HOLD: 0, DEAD: 0, ERROR: 0 });
  const [bulkDone, setBulkDone] = useState(0);
  const [bulkPct, setBulkPct] = useState(0);
  const [bulkProgressText, setBulkProgressText] = useState("0 / 0");

  const livePageCount = Math.max(1, Math.ceil(liveTotal / livePageSize) || 1);
  const liveFrom = liveTotal === 0 ? 0 : livePage * livePageSize + 1;
  const liveTo = Math.min(liveTotal, (livePage + 1) * livePageSize);

  const loadLive = useCallback(async () => {
    setLiveMsg({ type: "loading", text: "Đang tải..." });
    try {
      const q = new URLSearchParams({
        limit: String(livePageSize),
        offset: String(livePage * livePageSize),
      });
      if (planFilter) q.set("plan", planFilter);
      const data = await api<{
        rows?: LiveAccount[];
        total?: number;
        plans?: { plan: string; count?: number }[];
      }>(`/api/live-accounts?${q}`);
      if (!data.ok) {
        setLiveMsg({ type: "error", text: data.error || "Lỗi" });
        setLiveRows([]);
        return;
      }
      setLiveRows(data.rows || []);
      setLiveTotal(data.total || 0);
      if (data.plans) setLivePlans(data.plans);
      const maxPage = Math.max(0, Math.ceil((data.total || 0) / livePageSize) - 1);
      if (livePage > maxPage) {
        setLivePage(maxPage);
      }
      setLiveMsg(null);
    } catch {
      setLiveMsg({ type: "error", text: "BE offline?" });
      setLiveRows([]);
    }
  }, [planFilter, livePage, livePageSize]);

  useEffect(() => {
    if (tab === "live") void loadLive();
  }, [tab, loadLive]);

  async function viewCookie(id: number) {
    setBusyId(id);
    try {
      const data = await api<{ cookie_content?: string }>(`/api/live-accounts/${id}/cookie`);
      if (!data.ok) {
        setDetail((d) => ({
          ...d,
          [id]: <div className="status error">{data.error}</div>,
        }));
        return;
      }
      const content = data.cookie_content || "";
      setDetail((d) => ({
        ...d,
        [id]: (
          <>
            <code className="block">{content}</code>
            <div className="live-actions">
              <button type="button" className="btn-sm" onClick={() => copyText(content)}>
                Copy
              </button>
              <button
                type="button"
                className="btn soft"
                onClick={() => setDetail((x) => ({ ...x, [id]: null }))}
              >
                Đóng
              </button>
            </div>
          </>
        ),
      }));
    } catch (e) {
      setDetail((d) => ({
        ...d,
        [id]: <div className="status error">{(e as Error).message}</div>,
      }));
    } finally {
      setBusyId(null);
    }
  }

  function removeLiveRowLocal(id: number) {
    setLiveRows((rows) => rows.filter((r) => r.id !== id));
    setLiveTotal((t) => Math.max(0, t - 1));
    setDetail((d) => {
      const next = { ...d };
      delete next[id];
      return next;
    });
  }

  async function createLogin(id: number) {
    setBusyId(id);
    setLiveMsg({ type: "loading", text: `Tạo link #${id}...` });
    try {
      const data = await api<{
        links?: { label: string; url: string }[];
        email?: string;
        expires_at_utc?: string;
        consumed?: boolean;
        message?: string;
      }>(`/api/live-accounts/${id}/login-link`, {
        method: "POST",
        // consume=true: xóa cookie khỏi DB sau khi tạo link (đã dùng)
        json: { nftoken_mode: mode, consume: true },
      });
      if (!data.ok) {
        setLiveMsg({ type: "error", text: data.error || "Fail" });
        setDetail((d) => ({
          ...d,
          [id]: <div className="status error">{data.error}</div>,
        }));
        return;
      }
      const consumed = Boolean(data.consumed);
      setLiveMsg({
        type: "ok",
        text: consumed
          ? `Đã tạo link · ${data.email || id} · đã xóa cookie khỏi catalog`
          : `Đã tạo link · ${data.email || id}`,
      });
      setDetail((d) => ({
        ...d,
        [id]: (
          <LoginLinksPanel
            links={data.links || []}
            email={data.email}
            expires={data.expires_at_utc}
            consumed={consumed}
            closeLabel={consumed ? "Đóng & ẩn dòng" : "Đóng"}
            onClose={() => {
              if (consumed) removeLiveRowLocal(id);
              else setDetail((x) => ({ ...x, [id]: null }));
            }}
          />
        ),
      }));
      // Keep row briefly so user can copy links; hide on close or auto-remove after short delay
      if (consumed) {
        window.setTimeout(() => removeLiveRowLocal(id), 120_000);
      }
    } catch (e) {
      setLiveMsg({ type: "error", text: (e as Error).message });
    } finally {
      setBusyId(null);
    }
  }

  async function deleteLiveAccount(id: number, label?: string) {
    if (
      !confirm(
        `Xóa cookie${label ? ` «${label}»` : ""} khỏi catalog?\nKhông thể hoàn tác.`
      )
    ) {
      return;
    }
    setBusyId(id);
    setLiveMsg({ type: "loading", text: `Đang xóa #${id}...` });
    try {
      const data = await api<{ email?: string; message?: string }>(
        `/api/live-accounts/${id}`,
        { method: "DELETE" }
      );
      if (!data.ok) {
        setLiveMsg({ type: "error", text: data.error || "Xóa fail" });
        return;
      }
      removeLiveRowLocal(id);
      setLiveMsg({
        type: "ok",
        text: data.message || `Đã xóa ${data.email || id}`,
      });
    } catch (e) {
      setLiveMsg({ type: "error", text: (e as Error).message });
    } finally {
      setBusyId(null);
    }
  }

  async function runCheck(makeToken: boolean) {
    const c = cookie.trim();
    if (!c) {
      setSingleMsg({ type: "error", text: "Dán cookie trước" });
      return;
    }
    setSingleBusy(true);
    setSingleMsg({ type: "loading", text: makeToken ? "Full check..." : "Lấy info..." });
    setSingleHtml(null);
    try {
      const data = await api<{ results?: CheckResult[] }>("/api/check", {
        method: "POST",
        json: { cookie: c, make_nftoken: makeToken, nftoken_mode: mode },
      });
      if (!data.ok) {
        setSingleMsg({ type: "error", text: data.error || "Fail" });
        return;
      }
      setSingleMsg({ type: "ok", text: "Xong" });
      setSingleHtml(
        <>
          {(data.results || []).map((r, i) => {
            if (!r.ok) {
              return (
                <article className="result-card" key={i}>
                  <div className="error-box">{r.error || "Fail"}</div>
                </article>
              );
            }
            return (
              <article className="result-card" key={i}>
                <div className="result-head">
                  <h2>{r.plan_label || "Account"}</h2>
                  <span className={`pill ${r.subscribed ? "ok" : "info"}`}>{r.status}</span>
                </div>
                <div className="fields">
                  {(r.fields || []).map((f) => (
                    <div className="field-item" key={f.label}>
                      <span className="k">{f.label}</span>
                      <span className="v">{f.value}</span>
                    </div>
                  ))}
                </div>
                {r.nftoken?.ok && r.nftoken.links && (
                  <LoginLinksPanel links={r.nftoken.links} />
                )}
              </article>
            );
          })}
        </>
      );
    } catch (e) {
      setSingleMsg({ type: "error", text: (e as Error).message || "BE offline?" });
    } finally {
      setSingleBusy(false);
    }
  }

  async function runLoginOnly() {
    const c = cookie.trim();
    if (!c) {
      setSingleMsg({ type: "error", text: "Dán cookie trước" });
      return;
    }
    setSingleBusy(true);
    setSingleMsg({ type: "loading", text: "Tạo login link..." });
    setSingleHtml(null);
    try {
      const data = await api<{
        results?: { ok?: boolean; links?: { label: string; url: string }[] }[];
        links?: { label: string; url: string }[];
      }>("/api/login-link", {
        method: "POST",
        json: { cookie: c, nftoken_mode: mode },
      });
      if (!data.ok) {
        setSingleMsg({ type: "error", text: data.error || "Fail" });
        return;
      }
      setSingleMsg({ type: "ok", text: "Đã tạo link" });
      const r = (data.results || []).find((x) => x.ok);
      const links = r?.links || data.links || [];
      setSingleHtml(
        <article className="result-card result-card-links">
          <LoginLinksPanel links={links} />
        </article>
      );
    } catch (e) {
      setSingleMsg({ type: "error", text: (e as Error).message || "BE offline?" });
    } finally {
      setSingleBusy(false);
    }
  }

  async function runBulk() {
    if (!bulkFiles.length || bulkRun) return;
    setBulkRun(true);
    bulkStopRef.current = false;
    const total = bulkFiles.length;
    const counts = { LIVE: 0, FREE: 0, HOLD: 0, DEAD: 0, ERROR: 0 };
    const rows = bulkFiles.map((f) => ({
      name: f.name,
      status: "WAIT",
      label: "WAIT",
    }));
    setBulkRows([...rows]);
    setBulkDone(0);
    setBulkCounts({ ...counts });

    for (let i = 0; i < bulkFiles.length; i++) {
      if (bulkStopRef.current) break;
      const file = bulkFiles[i];
      setBulkProgressText(`${i}/${total} · ${file.name}`);
      setBulkPct(Math.round((i / total) * 100));
      setBulkMsg({ type: "loading", text: `${i + 1}/${total} · ${file.name}` });
      setBulkRows((prev) => {
        const next = [...prev];
        next[i] = { ...next[i], status: "RUN", label: "..." };
        return next;
      });
      try {
        const content = await file.text();
        const row = await api<{
          status?: string;
          status_label?: string;
          plan?: string;
          email?: string;
          country?: string;
          message?: string;
        }>("/api/quick-check", {
          method: "POST",
          json: { cookie: content, filename: file.name, timeout: 10 },
        });
        const st = row.status || "ERROR";
        if (st in counts) counts[st as keyof typeof counts] += 1;
        else counts.ERROR += 1;
        setBulkRows((prev) => {
          const next = [...prev];
          next[i] = {
            name: file.name,
            status: st,
            label: row.status_label || st,
            plan: row.plan,
            email: row.email,
            country: row.country,
            msg: row.message,
          };
          return next;
        });
      } catch (e) {
        counts.ERROR += 1;
        setBulkRows((prev) => {
          const next = [...prev];
          next[i] = {
            name: file.name,
            status: "ERROR",
            label: "ERROR",
            msg: (e as Error).message,
          };
          return next;
        });
      }
      setBulkDone(i + 1);
      setBulkCounts({ ...counts });
      setBulkPct(Math.round(((i + 1) / total) * 100));
      setBulkProgressText(`${i + 1}/${total}`);
    }

    const stopped = bulkStopRef.current;
    setBulkRun(false);
    setBulkMsg({
      type: "ok",
      text: stopped
        ? `Dừng · ${Object.values(counts).reduce((a, b) => a + b, 0)}/${total}`
        : `Xong · LIVE ${counts.LIVE} · DEAD ${counts.DEAD}`,
    });
  }

  return (
    <div className="wrap">
      <header className="hero">
        <h1>Netflix Checker</h1>
      </header>

      <nav className="tabs">
        {(
          [
            ["live", "TK live"],
            ["single", "Check 1 TK"],
            ["bulk", "Check hàng loạt"],
          ] as const
        ).map(([k, label]) => (
          <button
            key={k}
            type="button"
            className={`tab ${tab === k ? "active" : ""}`}
            onClick={() => setTab(k)}
          >
            {label}
          </button>
        ))}
      </nav>

      {tab === "live" && (
        <>
          <section className="card">
            <div className="row options" style={{ margin: 0, justifyContent: "space-between", flexWrap: "wrap", gap: 10 }}>
              <div className="row" style={{ margin: 0, gap: 12, flexWrap: "wrap" }}>
                <label className="field compact">
                  <span>Gói</span>
                  <select
                    value={planFilter}
                    onChange={(e) => {
                      setPlanFilter(e.target.value);
                      setLivePage(0);
                    }}
                  >
                    <option value="">Tất cả</option>
                    {livePlans.map((p) => (
                      <option key={p.plan} value={p.plan}>
                        {p.plan}
                        {p.count != null ? ` (${p.count})` : ""}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field compact">
                  <span>Hiện / trang</span>
                  <select
                    value={livePageSize}
                    onChange={(e) => {
                      const n = parseInt(e.target.value, 10) || 50;
                      setLivePageSize(n);
                      setLivePage(0);
                      try {
                        localStorage.setItem(LIVE_PAGE_SIZE_KEY, String(n));
                      } catch {
                        /* ignore */
                      }
                    }}
                  >
                    {LIVE_PAGE_SIZE_OPTIONS.map((n) => (
                      <option key={n} value={n}>
                        {n} TK
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <div className="row" style={{ margin: 0, gap: 10, flexWrap: "wrap" }}>
                <span className="muted">
                  {liveTotal > 0
                    ? `${liveFrom}–${liveTo} / ${liveTotal} TK`
                    : `0 TK`}
                </span>
                <button type="button" className="btn ghost" onClick={() => void loadLive()}>
                  Tải lại
                </button>
              </div>
            </div>
            {liveTotal > livePageSize && (
              <div
                className="row actions"
                style={{ marginTop: 12, marginBottom: 0, justifyContent: "space-between" }}
              >
                <button
                  type="button"
                  className="btn soft"
                  disabled={livePage <= 0}
                  onClick={() => setLivePage((p) => Math.max(0, p - 1))}
                >
                  ← Trước
                </button>
                <span className="muted">
                  Trang {livePage + 1} / {livePageCount}
                </span>
                <button
                  type="button"
                  className="btn soft"
                  disabled={livePage + 1 >= livePageCount}
                  onClick={() => setLivePage((p) => p + 1)}
                >
                  Sau →
                </button>
              </div>
            )}
          </section>
          {liveMsg && <div className={`status ${liveMsg.type}`}>{liveMsg.text}</div>}
          <section className="card live-table-card">
            <div className="bulk-table-wrap live-table-wrap">
              <table className="bulk-table live-table">
                <thead>
                  <tr>
                    <th className="col-num">#</th>
                    <th>Email</th>
                    <th>Plan</th>
                    <th>Country</th>
                    <th>Status</th>
                    <th className="col-actions">Thao tác</th>
                  </tr>
                </thead>
                <tbody>
                  {liveRows.length === 0 && !liveMsg ? (
                    <tr className="empty-row">
                      <td colSpan={6}>Chưa có TK live</td>
                    </tr>
                  ) : (
                    liveRows.map((r, idx) => (
                      <Fragment key={r.id}>
                        <tr className={detail[r.id] ? "row-open" : undefined}>
                          <td className="col-num">{livePage * livePageSize + idx + 1}</td>
                          <td className="fn" title={r.email || r.filename || String(r.id)}>
                            {r.email || r.filename || `#${r.id}`}
                          </td>
                          <td className="col-plan">{r.plan || "—"}</td>
                          <td className="col-country">{countryLabel(r.country)}</td>
                          <td>
                            <span className="pill ok">{r.status || "LIVE"}</span>
                          </td>
                          <td className="col-actions">
                            <div className="live-actions live-actions-table">
                              <button
                                type="button"
                                className="btn-sm primary"
                                disabled={busyId === r.id}
                                title="Tạo link login và xóa cookie khỏi DB"
                                onClick={() => void createLogin(r.id)}
                              >
                                Login
                              </button>
                              <button
                                type="button"
                                className="btn-sm"
                                disabled={busyId === r.id}
                                onClick={() => void viewCookie(r.id)}
                              >
                                Cookie
                              </button>
                              <button
                                type="button"
                                className="btn-sm danger"
                                disabled={busyId === r.id}
                                title="Xóa cookie khỏi catalog"
                                onClick={() =>
                                  void deleteLiveAccount(
                                    r.id,
                                    r.email || r.filename || String(r.id)
                                  )
                                }
                              >
                                Xóa
                              </button>
                            </div>
                          </td>
                        </tr>
                        {detail[r.id] ? (
                          <tr className="live-detail-row">
                            <td colSpan={6}>
                              <div className="live-detail">{detail[r.id]}</div>
                            </td>
                          </tr>
                        ) : null}
                      </Fragment>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </section>
          {liveTotal > livePageSize && liveRows.length > 0 && (
            <div
              className="row actions card"
              style={{ marginTop: 12, justifyContent: "space-between" }}
            >
              <button
                type="button"
                className="btn soft"
                disabled={livePage <= 0}
                onClick={() => {
                  setLivePage((p) => Math.max(0, p - 1));
                  window.scrollTo({ top: 0, behavior: "smooth" });
                }}
              >
                ← Trước
              </button>
              <span className="muted">
                Trang {livePage + 1} / {livePageCount} · {liveFrom}–{liveTo} / {liveTotal}
              </span>
              <button
                type="button"
                className="btn soft"
                disabled={livePage + 1 >= livePageCount}
                onClick={() => {
                  setLivePage((p) => p + 1);
                  window.scrollTo({ top: 0, behavior: "smooth" });
                }}
              >
                Sau →
              </button>
            </div>
          )}
        </>
      )}

      {tab === "single" && (
        <>
          <section className="card">
            <label className="field grow">
              <span>Cookie</span>
              <textarea
                rows={8}
                value={cookie}
                onChange={(e) => setCookie(e.target.value)}
                placeholder="Dán cookie (NetflixId)..."
              />
            </label>
            <div className="row options">
              <label className="file-btn">
                <input
                  type="file"
                  accept=".txt,.json,.cookie,text/plain,application/json"
                  onChange={async (e) => {
                    const f = e.target.files?.[0];
                    if (f) setCookie(await f.text());
                  }}
                />
                Upload file
              </label>
              <label className="field compact">
                <span>Link</span>
                <select value={mode} onChange={(e) => setMode(e.target.value)}>
                  <option value="both">PC + Mobile</option>
                  <option value="pc">PC</option>
                  <option value="mobile">Mobile</option>
                </select>
              </label>
            </div>
            <div className="actions">
              <button
                type="button"
                className="btn primary"
                disabled={singleBusy}
                onClick={() => void runCheck(true)}
              >
                Full (info + link)
              </button>
              <button
                type="button"
                className="btn ghost"
                disabled={singleBusy}
                onClick={() => void runCheck(false)}
              >
                Chỉ info
              </button>
              <button
                type="button"
                className="btn ghost"
                disabled={singleBusy}
                onClick={() => void runLoginOnly()}
              >
                Chỉ login link
              </button>
              <button
                type="button"
                className="btn soft"
                disabled={singleBusy}
                onClick={() => {
                  setCookie("");
                  setSingleHtml(null);
                  setSingleMsg(null);
                }}
              >
                Xóa
              </button>
            </div>
          </section>
          {singleMsg && <div className={`status ${singleMsg.type}`}>{singleMsg.text}</div>}
          <div>{singleHtml}</div>
        </>
      )}

      {tab === "bulk" && (
        <>
          <section className="card">
            <div className="row options">
              <label className="file-btn file-btn-lg">
                <input
                  type="file"
                  multiple
                  accept=".txt,.json,.cookie,text/plain,application/json"
                  onChange={(e) => {
                    const files = Array.from(e.target.files || []);
                    const map = new Map<string, File>();
                    [...bulkFiles, ...files].forEach((f) => map.set(`${f.name}::${f.size}`, f));
                    setBulkFiles(Array.from(map.values()).slice(0, 200));
                  }}
                />
                Chọn nhiều file
              </label>
              <span className="muted">{bulkFiles.length} file</span>
            </div>
            {bulkFiles.length > 0 && (
              <div className="bulk-file-list">{bulkFiles.map((f) => f.name).join("\n")}</div>
            )}
            <div className="actions">
              <button
                type="button"
                className="btn primary"
                disabled={bulkRun || !bulkFiles.length}
                onClick={() => void runBulk()}
              >
                Bắt đầu check
              </button>
              <button
                type="button"
                className="btn soft"
                disabled={!bulkRun}
                onClick={() => {
                  bulkStopRef.current = true;
                }}
              >
                Dừng
              </button>
              <button
                type="button"
                className="btn soft"
                disabled={bulkRun}
                onClick={() => {
                  setBulkFiles([]);
                  setBulkRows([]);
                  setBulkMsg(null);
                }}
              >
                Xóa list
              </button>
            </div>
            {(bulkRun || bulkDone > 0) && (
              <div className="inline-progress show">
                <div className="track">
                  <div className="fill" style={{ width: `${bulkPct}%` }} />
                </div>
                <div className="meta">
                  <span>{bulkProgressText}</span>
                  <span>{bulkPct}%</span>
                </div>
              </div>
            )}
          </section>
          {bulkMsg && <div className={`status ${bulkMsg.type}`}>{bulkMsg.text}</div>}
          {(bulkDone > 0 || bulkRun) && (
            <section className="card bulk-summary">
              <div className="summary-pills">
                <span className="pill ok">LIVE {bulkCounts.LIVE}</span>
                <span className="pill info">FREE {bulkCounts.FREE}</span>
                <span className="pill warn">HOLD {bulkCounts.HOLD}</span>
                <span className="pill bad">DEAD {bulkCounts.DEAD}</span>
                <span className="pill bad">ERROR {bulkCounts.ERROR}</span>
                <span className="pill info">
                  {bulkDone}/{bulkFiles.length}
                </span>
              </div>
            </section>
          )}
          <section className="card bulk-table-wrap" style={{ padding: 0 }}>
            <table className="bulk-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>File</th>
                  <th>Status</th>
                  <th>Plan</th>
                  <th>Email</th>
                  <th>Country</th>
                  <th>Ghi chú</th>
                </tr>
              </thead>
              <tbody>
                {bulkRows.length === 0 ? (
                  <tr className="empty-row">
                    <td colSpan={7}>Chưa có file</td>
                  </tr>
                ) : (
                  bulkRows.map((r, i) => (
                    <tr key={`${r.name}-${i}`}>
                      <td>{i + 1}</td>
                      <td className="fn">{r.name}</td>
                      <td>
                        <span className={`st st-${r.status}`}>{r.label}</span>
                      </td>
                      <td>{r.plan || "—"}</td>
                      <td>{r.email || "—"}</td>
                      <td>{countryLabel(r.country)}</td>
                      <td className="msg">{r.msg || ""}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </section>
        </>
      )}
    </div>
  );
}
