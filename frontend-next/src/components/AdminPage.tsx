"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Batch, type BatchItem, type LiveAccount } from "@/lib/api";
import { countryLabel } from "@/lib/country";

/** Files per upload API call — keep modest for Render body/timeout. */
const UPLOAD_CHUNK = 100;
const LIVE_ADMIN_PAGE = 50;

function isFinished(st?: string) {
  return ["done", "stopped", "error"].includes(String(st || "").toLowerCase());
}

function isRunning(st?: string) {
  return ["running", "queued"].includes(String(st || "").toLowerCase());
}

/** Phiếu có thể bấm Check: ready / stopped / error / done (còn PENDING). */
function canStartCheck(st?: string) {
  const s = String(st || "").toLowerCase();
  return s === "ready" || isFinished(s);
}

export default function AdminPage() {
  const [token, setToken] = useState("");
  const [password, setPassword] = useState("");
  const [loggedIn, setLoggedIn] = useState(false);
  const [loginMsg, setLoginMsg] = useState<{ type: string; text: string } | null>(null);
  const [dbBadge, setDbBadge] = useState("DB: …");
  const [dbOk, setDbOk] = useState(false);

  const [files, setFiles] = useState<File[]>([]);
  const [batchName, setBatchName] = useState("");
  const [statusMsg, setStatusMsg] = useState<{ type: string; text: string } | null>(null);
  const [activeBatch, setActiveBatch] = useState<Batch | null>(null);
  const [items, setItems] = useState<BatchItem[]>([]);
  const [batches, setBatches] = useState<Batch[]>([]);
  const [uploading, setUploading] = useState(false);
  const [checking, setChecking] = useState(false);
  const [uploadPct, setUploadPct] = useState(0);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // LIVE catalog recheck (daily hygiene)
  const [liveRows, setLiveRows] = useState<LiveAccount[]>([]);
  const [liveTotal, setLiveTotal] = useState(0);
  const [livePage, setLivePage] = useState(0);
  const [livePlan, setLivePlan] = useState("");
  const [livePlans, setLivePlans] = useState<{ plan: string; count?: number }[]>([]);
  const [liveQ, setLiveQ] = useState("");
  const [liveSelected, setLiveSelected] = useState<Set<number>>(new Set());
  const [liveBusy, setLiveBusy] = useState(false);
  const [liveMsg, setLiveMsg] = useState<{ type: string; text: string } | null>(null);
  const [liveLastResults, setLiveLastResults] = useState<
    { id: number; action?: string; status?: string; email?: string; message?: string }[]
  >([]);
  const [recheckJob, setRecheckJob] = useState<{
    id?: string;
    status?: string;
    total?: number;
    done?: number;
    kept?: number;
    deleted?: number;
    skipped?: number;
    missing?: number;
    percent?: number;
    message?: string;
    current_label?: string;
  } | null>(null);
  const recheckPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    const t = localStorage.getItem("admin_token") || "";
    setToken(t);
    if (t) {
      void api("/api/admin/me", { token: t }).then((me) => {
        if (me.ok) setLoggedIn(true);
        else {
          localStorage.removeItem("admin_token");
          setToken("");
        }
      });
    }
  }, []);

  const stopPoll = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const refreshHealth = useCallback(async (tok: string) => {
    try {
      const h = await api<{ total_live?: number }>("/api/admin/db-health", { token: tok });
      if (h.ok) {
        setDbOk(true);
        setDbBadge(`DB OK · ${h.total_live ?? 0} live`);
      } else {
        setDbOk(false);
        setDbBadge(`DB: ${h.error || "fail"}`);
      }
    } catch {
      setDbOk(false);
      setDbBadge("BE offline?");
    }
  }, []);

  const loadLiveCatalog = useCallback(
    async (tok: string, page = livePage, plan = livePlan, search = liveQ) => {
      try {
        const q = new URLSearchParams({
          limit: String(LIVE_ADMIN_PAGE),
          offset: String(page * LIVE_ADMIN_PAGE),
          status: "LIVE",
          plans: "1",
        });
        if (plan) q.set("plan", plan);
        if (search.trim()) q.set("q", search.trim());
        const data = await api<{
          rows?: LiveAccount[];
          total?: number;
          plans?: { plan: string; count?: number }[];
        }>(`/api/admin/list?${q}`, { token: tok });
        if (!data.ok) {
          setLiveMsg({ type: "error", text: data.error || "Load LIVE fail" });
          return;
        }
        setLiveRows(data.rows || []);
        setLiveTotal(data.total || 0);
        if (data.plans) setLivePlans(data.plans);
        setLiveSelected(new Set());
      } catch {
        setLiveMsg({ type: "error", text: "BE offline? (load LIVE)" });
      }
    },
    [livePage, livePlan, liveQ]
  );

  const loadItems = useCallback(async (batchId: string, tok: string) => {
    const data = await api<{ batch?: Batch; rows?: BatchItem[] }>(
      `/api/admin/batches/${batchId}/items?limit=200`,
      { token: tok }
    );
    if (data.ok) {
      if (data.batch) setActiveBatch(data.batch);
      setItems(data.rows || []);
    }
  }, []);

  const startPoll = useCallback(
    (batchId: string, tok: string) => {
      stopPoll();
      // Poll lightly: status every 2.5s; full item table every ~7.5s (less DB fight with user APIs).
      let tick = 0;
      pollRef.current = setInterval(() => {
        void (async () => {
          tick += 1;
          const b = await api<{ batch?: Batch }>(`/api/admin/batches/${batchId}`, {
            token: tok,
          });
          if (b.ok && b.batch) {
            setActiveBatch(b.batch);
            if (tick % 3 === 1) {
              await loadItems(batchId, tok);
            }
            if (isFinished(b.batch.status)) {
              stopPoll();
              await loadItems(batchId, tok);
              setStatusMsg({
                type: b.batch.status === "error" ? "error" : "ok",
                text:
                  b.batch.status === "done"
                    ? `Xong · LIVE ${b.batch.kept || 0} · loại ${b.batch.discarded || 0}`
                    : b.batch.message || b.batch.status || "",
              });
              void refreshHealth(tok);
              void refreshBatches(tok);
            } else if (isRunning(b.batch.status)) {
              setStatusMsg({
                type: "loading",
                text: `${b.batch.done || 0}/${b.batch.total || 0} · ${b.batch.current_file || "check..."}`,
              });
            }
          }
        })();
      }, 2500);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [loadItems, refreshHealth]
  );

  const refreshBatches = useCallback(
    async (tok: string) => {
      try {
        const data = await api<{ batches?: Batch[]; active_batch?: Batch }>(
          "/api/admin/batches",
          { token: tok }
        );
        if (!data.ok) return;
        setBatches(data.batches || []);
        const active = data.active_batch;
        if (active?.id && isRunning(active.status) && !pollRef.current) {
          setActiveBatch(active);
          localStorage.setItem("active_batch_id", active.id);
          void loadItems(active.id, tok);
          startPoll(active.id, tok);
        }
      } catch {
        /* ignore */
      }
    },
    [loadItems, startPoll]
  );

  useEffect(() => {
    if (!loggedIn || !token) return;
    void refreshHealth(token);
    void refreshBatches(token);
    return () => stopPoll();
  }, [loggedIn, token, refreshHealth, refreshBatches]);

  useEffect(() => {
    if (!loggedIn || !token) return;
    void loadLiveCatalog(token, livePage);
  }, [loggedIn, token, livePage, livePlan, loadLiveCatalog]);

  // Resume progress if a recheck job is already running on BE
  useEffect(() => {
    if (!loggedIn || !token) return;
    void (async () => {
      try {
        const data = await api<{ job?: { id?: string; status?: string } }>(
          "/api/admin/cookies/recheck-job",
          { token }
        );
        if (data.ok && data.job?.id) {
          const st = String(data.job.status || "").toLowerCase();
          if (st === "running" || st === "queued") {
            setRecheckJob(data.job as typeof recheckJob);
            setLiveBusy(true);
            setLiveMsg({
              type: "loading",
              text: "Đang có job recheck chạy trên BE — theo dõi tiến độ...",
            });
            pollRecheckJob(data.job.id, token);
          }
        }
      } catch {
        /* ignore */
      }
    })();
    return () => stopRecheckPoll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loggedIn, token]);

  async function login() {
    if (!password) {
      setLoginMsg({ type: "error", text: "Nhập password" });
      return;
    }
    try {
      const data = await api<{ token?: string }>("/api/admin/login", {
        method: "POST",
        json: { password },
      });
      if (data.ok && data.token) {
        localStorage.setItem("admin_token", data.token);
        setToken(data.token);
        setLoggedIn(true);
        setLoginMsg({ type: "ok", text: "OK" });
      } else {
        setLoginMsg({ type: "error", text: data.error || "Sai password" });
      }
    } catch {
      setLoginMsg({ type: "error", text: "BE offline?" });
    }
  }

  function openBatch(batchId: string) {
    if (!token) return;
    stopPoll();
    localStorage.setItem("active_batch_id", batchId);
    setStatusMsg({ type: "loading", text: `Mở phiếu ${batchId.slice(0, 8)}...` });
    void (async () => {
      const b = await api<{ batch?: Batch }>(`/api/admin/batches/${batchId}`, { token });
      if (!b.ok || !b.batch) {
        setStatusMsg({ type: "error", text: b.error || "Không tìm thấy" });
        return;
      }
      setActiveBatch(b.batch);
      await loadItems(batchId, token);
      if (isRunning(b.batch.status)) {
        setStatusMsg({
          type: "loading",
          text: `${b.batch.done || 0}/${b.batch.total || 0} · ${b.batch.current_file || "check..."}`,
        });
        startPoll(batchId, token);
      } else if (isFinished(b.batch.status)) {
        setStatusMsg({
          type: b.batch.status === "error" ? "error" : "ok",
          text:
            b.batch.status === "done"
              ? `Xong · LIVE ${b.batch.kept || 0} · loại ${b.batch.discarded || 0}`
              : b.batch.message || b.batch.status || "",
        });
      } else {
        setStatusMsg({
          type: "ok",
          text:
            b.batch.message ||
            `Phiếu ready · ${b.batch.total || 0} cookie · bấm Check để chạy`,
        });
      }
    })();
  }

  /** Recursively collect all .txt files from a FileSystemEntry (drag-drop). */
  async function collectTxtFromEntry(entry: FileSystemEntry): Promise<File[]> {
    if (entry.isFile) {
      const fileEntry = entry as FileSystemFileEntry;
      if (!entry.name.toLowerCase().endsWith(".txt")) return [];
      return new Promise<File[]>((resolve) => {
        fileEntry.file(
          (f) => resolve([f]),
          () => resolve([])
        );
      });
    }
    if (entry.isDirectory) {
      const dirEntry = entry as FileSystemDirectoryEntry;
      const reader = dirEntry.createReader();
      const allEntries: FileSystemEntry[] = [];
      await new Promise<void>((resolve) => {
        function readBatch() {
          reader.readEntries((batch) => {
            if (!batch.length) { resolve(); return; }
            allEntries.push(...batch);
            readBatch();
          }, () => resolve());
        }
        readBatch();
      });
      const nested = await Promise.all(allEntries.map((e) => collectTxtFromEntry(e)));
      return nested.flat();
    }
    return [];
  }

  async function handleDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    e.currentTarget.classList.remove("drag-over");
    const items = Array.from(e.dataTransfer.items);
    const collected: File[] = [];
    for (const item of items) {
      if (item.kind !== "file") continue;
      const entry = item.webkitGetAsEntry?.();
      if (entry) {
        const found = await collectTxtFromEntry(entry);
        collected.push(...found);
      } else {
        const f = item.getAsFile();
        if (f && f.name.toLowerCase().endsWith(".txt")) collected.push(f);
      }
    }
    if (!collected.length) return;
    setFiles((prev) => {
      const map = new Map<string, File>();
      [...prev, ...collected].forEach((f) => map.set(`${f.name}::${f.size}`, f));
      return Array.from(map.values());
    });
  }

  /** 1) Tạo phiếu + upload chunk nhiều API — không check. */
  async function uploadToDb() {
    if (!files.length || !token) {
      setStatusMsg({ type: "error", text: "Chọn file cookie" });
      return;
    }
    setUploading(true);
    setUploadPct(0);
    stopPoll();

    const totalFiles = files.length;
    const chunks = Math.ceil(totalFiles / UPLOAD_CHUNK);

    try {
      setStatusMsg({
        type: "loading",
        text: `Tạo phiếu · sẽ upload ${totalFiles} file / ${chunks} lô...`,
      });

      const created = await api<{ batch?: Batch }>("/api/admin/batches", {
        method: "POST",
        token,
        json: { name: batchName.trim() || undefined },
      });
      if (!created.ok || !created.batch?.id) {
        setStatusMsg({ type: "error", text: created.error || "Tạo phiếu fail" });
        return;
      }

      const batchId = created.batch.id;
      setActiveBatch(created.batch);
      localStorage.setItem("active_batch_id", batchId);

      let uploaded = 0;
      for (let i = 0; i < totalFiles; i += UPLOAD_CHUNK) {
        const slice = files.slice(i, i + UPLOAD_CHUNK);
        const chunkIdx = Math.floor(i / UPLOAD_CHUNK) + 1;
        setStatusMsg({
          type: "loading",
          text: `Upload lô ${chunkIdx}/${chunks} · ${uploaded}/${totalFiles} file...`,
        });

        const fd = new FormData();
        for (const f of slice) {
          const text = await f.text();
          if (!text.trim()) continue;
          fd.append(
            "files",
            new File([text], f.name || "cookie.txt", { type: "text/plain" })
          );
        }

        // If all empty in slice, skip
        if (![...fd.keys()].length) {
          uploaded += slice.length;
          setUploadPct(Math.round((100 * uploaded) / totalFiles));
          continue;
        }

        const data = await api<{ batch?: Batch; added?: number }>(
          `/api/admin/batches/${batchId}/items`,
          { method: "POST", token, formData: fd }
        );
        if (!data.ok) {
          setStatusMsg({
            type: "error",
            text: `Lô ${chunkIdx} fail: ${data.error || "upload error"} · đã có ${uploaded} file trong phiếu`,
          });
          if (data.batch) setActiveBatch(data.batch);
          await loadItems(batchId, token);
          void refreshBatches(token);
          return;
        }
        if (data.batch) setActiveBatch(data.batch);
        uploaded += slice.length;
        setUploadPct(Math.round((100 * Math.min(uploaded, totalFiles)) / totalFiles));
      }

      const final = await api<{ batch?: Batch }>(`/api/admin/batches/${batchId}`, {
        token,
      });
      if (final.ok && final.batch) setActiveBatch(final.batch);
      await loadItems(batchId, token);
      void refreshBatches(token);

      setStatusMsg({
        type: "ok",
        text: `Upload xong · ${(final.batch || created.batch)?.total || uploaded} cookie trong DB · status ready — chọn phiếu & bấm Check`,
      });
      setUploadPct(100);
      setFiles([]);
      setBatchName("");
    } catch (e) {
      setStatusMsg({ type: "error", text: (e as Error).message });
    } finally {
      setUploading(false);
    }
  }

  /** 2) Check PENDING trong phiếu đang chọn (đã nằm DB). */
  async function startCheck() {
    if (!token || !activeBatch?.id) {
      setStatusMsg({ type: "error", text: "Chọn phiếu trong danh sách trước" });
      return;
    }
    if (isRunning(activeBatch.status)) {
      setStatusMsg({ type: "error", text: "Phiếu đang check rồi" });
      return;
    }
    setChecking(true);
    try {
      const data = await api<{ batch?: Batch; pending?: number }>(
        `/api/admin/batches/${activeBatch.id}/start`,
        {
          method: "POST",
          token,
          json: { workers: 2, timeout: 10 },
        }
      );
      if (!data.ok) {
        setStatusMsg({ type: "error", text: data.error || "Start fail" });
        return;
      }
      if (data.batch) setActiveBatch(data.batch);
      setStatusMsg({
        type: "loading",
        text: `Đang check · PENDING ${data.pending ?? "?"} · BE process riêng`,
      });
      startPoll(activeBatch.id, token);
      void refreshBatches(token);
    } catch (e) {
      setStatusMsg({ type: "error", text: (e as Error).message });
    } finally {
      setChecking(false);
    }
  }

  async function stopCheck() {
    if (!activeBatch || !token) return;
    await api(`/api/admin/batches/${activeBatch.id}/stop`, {
      method: "POST",
      token,
    });
    setStatusMsg({ type: "loading", text: "Đã gửi stop — xong item hiện tại sẽ dừng" });
  }

  function toggleLiveId(id: number) {
    setLiveSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleLivePageAll() {
    const ids = liveRows.map((r) => r.id);
    const allOn = ids.length > 0 && ids.every((id) => liveSelected.has(id));
    setLiveSelected((prev) => {
      const next = new Set(prev);
      if (allOn) ids.forEach((id) => next.delete(id));
      else ids.forEach((id) => next.add(id));
      return next;
    });
  }

  /** Fetch every matching LIVE id in DB (not just current page). */
  async function fetchAllLiveIds(): Promise<number[]> {
    if (!token) return [];
    const q = new URLSearchParams({ status: "LIVE" });
    if (livePlan) q.set("plan", livePlan);
    if (liveQ.trim()) q.set("q", liveQ.trim());
    const data = await api<{
      ids?: number[];
      total?: number;
      truncated?: boolean;
      error?: string;
    }>(`/api/admin/list/ids?${q}`, { token });
    if (!data.ok) {
      throw new Error(data.error || "Không lấy được danh sách id");
    }
    if (data.truncated) {
      setLiveMsg({
        type: "error",
        text: `DB có ${data.total} TK nhưng chỉ lấy ${data.ids?.length || 0} (giới hạn). Recheck phần đã lấy.`,
      });
    }
    return data.ids || [];
  }

  async function selectAllInDb() {
    if (!token) return;
    setLiveBusy(true);
    try {
      setLiveMsg({ type: "loading", text: "Đang lấy ALL id LIVE trong DB..." });
      const ids = await fetchAllLiveIds();
      setLiveSelected(new Set(ids));
      setLiveMsg({
        type: "ok",
        text: `Đã chọn ${ids.length} TK LIVE trong DB (filter hiện tại) — không chỉ trang này`,
      });
    } catch (e) {
      setLiveMsg({ type: "error", text: (e as Error).message });
    } finally {
      setLiveBusy(false);
    }
  }

  function stopRecheckPoll() {
    if (recheckPollRef.current) {
      clearInterval(recheckPollRef.current);
      recheckPollRef.current = null;
    }
  }

  const pollRecheckJob = useCallback(
    (jobId: string, tok: string) => {
      stopRecheckPoll();
      recheckPollRef.current = setInterval(() => {
        void (async () => {
          const data = await api<{
            job?: {
              id?: string;
              status?: string;
              total?: number;
              done?: number;
              kept?: number;
              deleted?: number;
              skipped?: number;
              missing?: number;
              percent?: number;
              message?: string;
              current_label?: string;
            };
          }>(`/api/admin/cookies/recheck-job/${jobId}`, { token: tok });
          if (!data.ok || !data.job) return;
          setRecheckJob(data.job);
          const st = String(data.job.status || "").toLowerCase();
          setLiveMsg({
            type: ["done", "stopped"].includes(st)
              ? "ok"
              : st === "error"
                ? "error"
                : "loading",
            text:
              data.job.message ||
              `${data.job.done || 0}/${data.job.total || 0} · xóa ${data.job.deleted || 0}`,
          });
          if (["done", "stopped", "error"].includes(st)) {
            stopRecheckPoll();
            setLiveBusy(false);
            setLiveSelected(new Set());
            void loadLiveCatalog(tok, livePage);
            void refreshHealth(tok);
          }
        })();
      }, 2000);
    },
    [livePage, loadLiveCatalog, refreshHealth]
  );

  /**
   * Start BE background recheck job — survives closing the browser tab.
   */
  async function startRecheckJob(opts: {
    all?: boolean;
    ids?: number[];
    label: string;
  }) {
    if (!token) return;
    setLiveBusy(true);
    setLiveLastResults([]);
    try {
      const data = await api<{
        job?: {
          id?: string;
          status?: string;
          total?: number;
          done?: number;
          kept?: number;
          deleted?: number;
          skipped?: number;
          missing?: number;
          percent?: number;
          message?: string;
        };
        message?: string;
        total?: number;
      }>("/api/admin/cookies/recheck-job", {
        method: "POST",
        token,
        json: {
          all: Boolean(opts.all),
          ids: opts.ids || [],
          plan: livePlan || "",
          q: liveQ.trim() || "",
          timeout: 10,
          delete_on_error: false,
        },
      });
      if (!data.ok || !data.job?.id) {
        setLiveMsg({ type: "error", text: data.error || "Start recheck job fail" });
        setLiveBusy(false);
        return;
      }
      setRecheckJob(data.job);
      setLiveMsg({
        type: "loading",
        text:
          data.message ||
          `${opts.label} · job nền ${data.total ?? data.job.total} TK — tắt web vẫn chạy`,
      });
      pollRecheckJob(data.job.id, token);
    } catch (e) {
      setLiveMsg({ type: "error", text: (e as Error).message });
      setLiveBusy(false);
    }
  }

  async function recheckSelected() {
    const ids = Array.from(liveSelected);
    if (ids.length === 0) {
      setLiveMsg({ type: "error", text: "Chọn ít nhất 1 TK LIVE" });
      return;
    }
    if (
      !confirm(
        `Recheck ${ids.length} TK đã chọn trên BE (nền)?\nTắt web vẫn chạy. DIE/FREE/HOLD → xóa.`
      )
    ) {
      return;
    }
    await startRecheckJob({ all: false, ids, label: "Recheck đã chọn" });
  }

  /** One-click: ALL LIVE in DB — BE job, not browser loop. */
  async function recheckAllInDb() {
    if (!token) return;
    if (
      !confirm(
        `Recheck TOÀN BỘ ~${liveTotal} TK LIVE trên server?\n` +
          `Job chạy nền — TẮT WEB VẪN CHẠY.\n` +
          `DIE/FREE/HOLD sẽ bị xóa. Lỗi mạng tạm = giữ.`
      )
    ) {
      return;
    }
    await startRecheckJob({ all: true, label: "Recheck ALL DB" });
  }

  async function stopRecheckJob() {
    if (!token || !recheckJob?.id) return;
    await api(`/api/admin/cookies/recheck-job/${recheckJob.id}/stop`, {
      method: "POST",
      token,
    });
    setLiveMsg({ type: "loading", text: "Đã gửi stop recheck — xong chunk hiện tại sẽ dừng" });
  }

  async function deleteSelectedManual() {
    if (!token || liveSelected.size === 0) return;
    if (!confirm(`Xóa ${liveSelected.size} TK khỏi DB?`)) return;
    setLiveBusy(true);
    try {
      const data = await api("/api/admin/cookies/delete", {
        method: "POST",
        token,
        json: { ids: Array.from(liveSelected) },
      });
      if (!data.ok) {
        setLiveMsg({ type: "error", text: data.error || "Xóa fail" });
        return;
      }
      setLiveMsg({ type: "ok", text: `Đã xóa ${liveSelected.size} TK` });
      setLiveSelected(new Set());
      await loadLiveCatalog(token, livePage);
      void refreshHealth(token);
    } catch (e) {
      setLiveMsg({ type: "error", text: (e as Error).message });
    } finally {
      setLiveBusy(false);
    }
  }

  const livePageCount = Math.max(1, Math.ceil(liveTotal / LIVE_ADMIN_PAGE) || 1);

  const pct = activeBatch
    ? activeBatch.percent ??
      (activeBatch.total
        ? Math.round((100 * (activeBatch.done || 0)) / activeBatch.total)
        : 0)
    : 0;

  if (!loggedIn) {
    return (
      <div className="wrap">
        <header className="hero">
          <h1>Admin · Phiếu cookie</h1>
        </header>
        <section className="card">
          <label className="field">
            <span>Password</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void login()}
            />
          </label>
          <div className="actions">
            <button type="button" className="btn primary" onClick={() => void login()}>
              Đăng nhập
            </button>
            <Link href="/" className="btn soft" style={{ textAlign: "center" }}>
              User
            </Link>
          </div>
          {loginMsg && <div className={`status ${loginMsg.type}`}>{loginMsg.text}</div>}
        </section>
      </div>
    );
  }

  return (
    <div className="wrap">
      <header className="hero">
        <h1>Admin · Phiếu cookie</h1>
        <div className="admin-nav">
          <Link href="/">← User</Link>
          <span className={`pill ${dbOk ? "ok" : "bad"}`}>{dbBadge}</span>
          <button
            type="button"
            className="btn soft"
            style={{ padding: "6px 12px", fontSize: 12 }}
            onClick={() => {
              localStorage.removeItem("admin_token");
              setToken("");
              setLoggedIn(false);
              stopPoll();
            }}
          >
            Đăng xuất
          </button>
        </div>
      </header>

      <section className="card">
        <div className="db-card-head">
          <strong>0 · TK LIVE trong DB (re-check hàng ngày)</strong>
          <button
            type="button"
            className="btn ghost"
            style={{ padding: "6px 12px", fontSize: 12 }}
            disabled={liveBusy || !token}
            onClick={() => token && void loadLiveCatalog(token, livePage)}
          >
            ↻
          </button>
        </div>
        <p className="hint" style={{ marginTop: 0 }}>
          <strong>Recheck ALL DB</strong> chạy <em>job nền trên BE</em> — tắt / đóng web{" "}
          <strong>vẫn tiếp tục</strong> (giống phiếu check cookie). Mở lại admin để xem tiến độ.
          DIE/FREE/HOLD → xóa DB; lỗi mạng tạm → giữ. Bảng 50/trang chỉ để xem.
        </p>
        <div className="row options" style={{ margin: 0, flexWrap: "wrap", gap: 10 }}>
          <label className="field compact">
            <span>Gói</span>
            <select
              value={livePlan}
              disabled={liveBusy}
              onChange={(e) => {
                setLivePlan(e.target.value);
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
          <label className="field compact grow">
            <span>Tìm</span>
            <input
              type="text"
              value={liveQ}
              disabled={liveBusy}
              placeholder="email / file / country"
              onChange={(e) => setLiveQ(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && token) {
                  setLivePage(0);
                  void loadLiveCatalog(token, 0);
                }
              }}
            />
          </label>
          <button
            type="button"
            className="btn soft"
            disabled={liveBusy || !token}
            onClick={() => {
              setLivePage(0);
              if (token) void loadLiveCatalog(token, 0);
            }}
          >
            Lọc
          </button>
        </div>
        <div className="actions" style={{ marginTop: 10 }}>
          <button
            type="button"
            className="btn primary"
            disabled={liveBusy || liveTotal === 0}
            onClick={() => void recheckAllInDb()}
          >
            {liveBusy ? "Đang recheck..." : `Recheck ALL DB (${liveTotal})`}
          </button>
          <button
            type="button"
            className="btn soft"
            disabled={liveBusy || liveSelected.size === 0}
            onClick={() => void recheckSelected()}
          >
            Recheck đã chọn ({liveSelected.size})
          </button>
          <button
            type="button"
            className="btn soft"
            disabled={liveBusy || liveTotal === 0}
            onClick={() => void selectAllInDb()}
          >
            Chọn ALL trong DB
          </button>
          <button
            type="button"
            className="btn soft"
            disabled={liveBusy || liveRows.length === 0}
            onClick={toggleLivePageAll}
          >
            {liveRows.length > 0 && liveRows.every((r) => liveSelected.has(r.id))
              ? "Bỏ chọn trang này"
              : "Chọn trang này (50)"}
          </button>
          <button
            type="button"
            className="btn soft"
            disabled={liveBusy || liveSelected.size === 0}
            onClick={() => void deleteSelectedManual()}
          >
            Xóa tay đã chọn
          </button>
          <button
            type="button"
            className="btn soft"
            disabled={!recheckJob?.id || !["running", "queued"].includes(String(recheckJob.status || "").toLowerCase())}
            onClick={() => void stopRecheckJob()}
          >
            Dừng recheck BE
          </button>
          <span className="muted">
            {liveTotal > 0
              ? `Trang ${livePage * LIVE_ADMIN_PAGE + 1}–${Math.min(liveTotal, (livePage + 1) * LIVE_ADMIN_PAGE)} · tổng DB ${liveTotal} LIVE · đã chọn ${liveSelected.size}`
              : "0 LIVE"}
          </span>
        </div>
        {recheckJob && ["running", "queued"].includes(String(recheckJob.status || "").toLowerCase()) && (
          <div className="inline-progress show" style={{ marginTop: 10 }}>
            <div className="track">
              <div
                className="fill"
                style={{
                  width: `${
                    recheckJob.percent ??
                    (recheckJob.total
                      ? Math.round((100 * (recheckJob.done || 0)) / recheckJob.total)
                      : 0)
                  }%`,
                }}
              />
            </div>
            <div className="meta">
              <span>
                BE job · {recheckJob.done || 0}/{recheckJob.total || 0}
                {recheckJob.current_label ? ` · ${recheckJob.current_label}` : ""}
                {" · "}xóa {recheckJob.deleted || 0} · giữ {recheckJob.kept || 0}
              </span>
              <span>
                {recheckJob.percent ??
                  (recheckJob.total
                    ? Math.round((100 * (recheckJob.done || 0)) / recheckJob.total)
                    : 0)}
                %
              </span>
            </div>
          </div>
        )}
        {liveMsg && <div className={`status ${liveMsg.type}`}>{liveMsg.text}</div>}
        <div className="bulk-table-wrap live-table-wrap" style={{ marginTop: 12 }}>
          <table className="bulk-table live-table">
            <thead>
              <tr>
                <th className="col-num">
                  <input
                    type="checkbox"
                    checked={
                      liveRows.length > 0 && liveRows.every((r) => liveSelected.has(r.id))
                    }
                    onChange={toggleLivePageAll}
                    disabled={liveBusy || liveRows.length === 0}
                    aria-label="Chọn tất cả trang"
                  />
                </th>
                <th className="col-num">#</th>
                <th>Email</th>
                <th>Plan</th>
                <th>Country</th>
                <th>Status</th>
                <th>Recheck</th>
              </tr>
            </thead>
            <tbody>
              {liveRows.length === 0 ? (
                <tr className="empty-row">
                  <td colSpan={7}>Chưa có TK LIVE (hoặc lọc trống)</td>
                </tr>
              ) : (
                liveRows.map((r, idx) => {
                  const last = liveLastResults.find((x) => x.id === r.id);
                  return (
                    <tr key={r.id}>
                      <td className="col-num">
                        <input
                          type="checkbox"
                          checked={liveSelected.has(r.id)}
                          onChange={() => toggleLiveId(r.id)}
                          disabled={liveBusy}
                        />
                      </td>
                      <td className="col-num">{livePage * LIVE_ADMIN_PAGE + idx + 1}</td>
                      <td className="fn" title={r.email || r.filename}>
                        {r.email || r.filename || `#${r.id}`}
                      </td>
                      <td>{r.plan || "—"}</td>
                      <td>{countryLabel(r.country)}</td>
                      <td>
                        <span className="pill ok">{r.status || "LIVE"}</span>
                      </td>
                      <td className="msg">
                        {last ? (
                          <span
                            className={
                              last.action === "deleted"
                                ? "pill bad"
                                : last.action === "kept"
                                  ? "pill ok"
                                  : "pill info"
                            }
                          >
                            {last.action} · {last.status}
                          </span>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
        {liveTotal > LIVE_ADMIN_PAGE && (
          <div
            className="row actions"
            style={{ marginTop: 10, justifyContent: "space-between" }}
          >
            <button
              type="button"
              className="btn soft"
              disabled={liveBusy || livePage <= 0}
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
              disabled={liveBusy || livePage + 1 >= livePageCount}
              onClick={() => setLivePage((p) => p + 1)}
            >
              Sau →
            </button>
          </div>
        )}
        {liveLastResults.some((r) => r.action === "deleted") && (
          <p className="hint">
            Đã xóa:{" "}
            {liveLastResults
              .filter((r) => r.action === "deleted")
              .map((r) => r.email || `#${r.id}`)
              .join(", ")}
          </p>
        )}
      </section>

      <section
        className="card"
        style={{ marginTop: 14 }}
        onDragOver={(e) => { e.preventDefault(); e.currentTarget.classList.add("drag-over"); }}
        onDragLeave={(e) => { e.currentTarget.classList.remove("drag-over"); }}
        onDrop={(e) => void handleDrop(e)}
      >
        <div className="db-card-head">
          <strong>1 · Upload cookie lên DB</strong>
        </div>
        <p className="hint" style={{ marginTop: 0 }}>
          Chọn bao nhiêu file cũng được (vd 2000). FE chia lô {UPLOAD_CHUNK} file/request → 1
          phiếu. <strong>Chưa check</strong> — chỉ lưu DB (status ready).
          {" "}<strong>Hỗ trợ kéo thả / chọn cả folder</strong> — tự tìm tất cả file <code>.txt</code> bên trong.
        </p>
        <label className="field" style={{ marginBottom: 10 }}>
          <span>Tên phiếu</span>
          <input
            type="text"
            value={batchName}
            onChange={(e) => setBatchName(e.target.value)}
            placeholder="vd: Lô 12/07"
            disabled={uploading}
          />
        </label>
        <div className="row options" style={{ flexWrap: "wrap", gap: 8 }}>
          <label className="file-btn file-btn-lg">
            <input
              type="file"
              multiple
              accept=".txt,.json,.cookie,text/plain,application/json"
              disabled={uploading}
              onChange={(e) => {
                const list = Array.from(e.target.files || []);
                const map = new Map<string, File>();
                [...files, ...list].forEach((f) => map.set(`${f.name}::${f.size}`, f));
                setFiles(Array.from(map.values()));
              }}
            />
            Chọn file .txt
          </label>
          <label className="file-btn file-btn-lg" title="Chọn folder — tự tìm tất cả .txt bên trong (kể cả sub-folder)">
            <input
              type="file"
              // @ts-expect-error — webkitdirectory is non-standard but supported in all modern browsers
              webkitdirectory=""
              multiple
              disabled={uploading}
              onChange={async (e) => {
                const list = Array.from(e.target.files || []).filter((f) =>
                  f.name.toLowerCase().endsWith(".txt")
                );
                if (!list.length) return;
                setFiles((prev) => {
                  const map = new Map<string, File>();
                  [...prev, ...list].forEach((f) => map.set(`${f.name}::${f.size}`, f));
                  return Array.from(map.values());
                });
              }}
            />
            📁 Chọn folder
          </label>
          <span className="muted" style={{ alignSelf: "center" }}>
            {files.length > 0 ? `${files.length} file .txt` : "Hoặc kéo thả folder / file vào đây"}
          </span>
        </div>
        {files.length > 0 && (
          <div className="bulk-file-list">
            {files.length <= 30
              ? files.map((f) => f.name).join("\n")
              : `${files
                  .slice(0, 20)
                  .map((f) => f.name)
                  .join("\n")}\n… +${files.length - 20} file nữa`}
          </div>
        )}
        {uploading && (
          <div className="inline-progress show">
            <div className="track">
              <div className="fill" style={{ width: `${uploadPct}%` }} />
            </div>
            <div className="meta">
              <span>Upload DB</span>
              <span>{uploadPct}%</span>
            </div>
          </div>
        )}
        <div className="actions">
          <button
            type="button"
            className="btn primary"
            disabled={uploading || checking || !files.length}
            onClick={() => void uploadToDb()}
          >
            {uploading ? "Đang upload..." : "Upload lên DB"}
          </button>
          <button
            type="button"
            className="btn soft"
            disabled={uploading || !files.length}
            onClick={() => setFiles([])}
          >
            Xóa list
          </button>
        </div>
      </section>

      <section className="card" style={{ marginTop: 14 }}>
        <div className="db-card-head">
          <strong>2 · Check phiếu đã upload</strong>
        </div>
        <p className="hint" style={{ marginTop: 0 }}>
          Chọn phiếu bên dưới (hoặc phiếu vừa upload) → bấm <strong>Check</strong>. BE chỉ check
          item <code>PENDING</code> trong DB, process riêng — không upload lại.
        </p>
        {activeBatch && (
          <div className="summary-pills" style={{ marginBottom: 10 }}>
            <span className="pill info">{activeBatch.status}</span>
            <span className="pill info">
              {activeBatch.done || 0}/{activeBatch.total || 0}
            </span>
            <span className="pill ok">LIVE {activeBatch.kept || 0}</span>
            <span className="pill bad">Loại {activeBatch.discarded || 0}</span>
            <span className="muted" style={{ fontSize: 12 }}>
              {activeBatch.name || activeBatch.id}
            </span>
          </div>
        )}
        {activeBatch && isRunning(activeBatch.status) && (
          <div className="inline-progress show">
            <div className="track">
              <div className="fill" style={{ width: `${pct}%` }} />
            </div>
            <div className="meta">
              <span>
                {activeBatch.done || 0}/{activeBatch.total || 0}
                {activeBatch.current_file ? ` · ${activeBatch.current_file}` : ""}
              </span>
              <span>{pct}%</span>
            </div>
          </div>
        )}
        <div className="actions">
          <button
            type="button"
            className="btn primary"
            disabled={
              uploading ||
              checking ||
              !activeBatch ||
              isRunning(activeBatch.status) ||
              !canStartCheck(activeBatch.status)
            }
            onClick={() => void startCheck()}
          >
            {checking ? "Starting..." : "Check phiếu này"}
          </button>
          <button
            type="button"
            className="btn soft"
            disabled={!activeBatch || !isRunning(activeBatch.status)}
            onClick={() => void stopCheck()}
          >
            Dừng check
          </button>
        </div>
        {activeBatch && (
          <p className="hint">{activeBatch.message}</p>
        )}
      </section>

      {statusMsg && <div className={`status ${statusMsg.type}`}>{statusMsg.text}</div>}

      <section className="card" style={{ marginTop: 14 }}>
        <div className="db-card-head">
          <strong>Chi tiết phiếu</strong>
          <button
            type="button"
            className="btn ghost"
            style={{ padding: "6px 12px", fontSize: 12 }}
            onClick={() => activeBatch && token && void loadItems(activeBatch.id, token)}
          >
            ↻
          </button>
        </div>
        <div className="bulk-table-wrap" style={{ marginTop: 0 }}>
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
              {items.length === 0 ? (
                <tr className="empty-row">
                  <td colSpan={7}>Chọn phiếu để xem (hiển thị tối đa 200 dòng)</td>
                </tr>
              ) : (
                items.map((r, i) => (
                  <tr
                    key={r.id}
                    className={
                      r.status === "CHECKING" || r.status === "PENDING" ? "is-checking" : ""
                    }
                  >
                    <td>{i + 1}</td>
                    <td className="fn">{r.filename || "—"}</td>
                    <td>
                      <span className={`st st-${r.status || "WAIT"}`}>
                        {r.status_label || r.status}
                      </span>
                    </td>
                    <td>{r.plan || "—"}</td>
                    <td>{r.email || "—"}</td>
                    <td>{countryLabel(r.country)}</td>
                    <td className="msg">{r.message || ""}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card" style={{ marginTop: 14 }}>
        <div className="db-card-head">
          <strong>Danh sách phiếu</strong>
          <button
            type="button"
            className="btn ghost"
            style={{ padding: "6px 12px", fontSize: 12 }}
            onClick={() => token && void refreshBatches(token)}
          >
            ↻
          </button>
        </div>
        {batches.length === 0 && <div className="muted">Chưa có phiếu</div>}
        {batches.map((b) => {
          const live = isRunning(b.status);
          const ready = String(b.status || "").toLowerCase() === "ready";
          const p =
            b.percent ?? (b.total ? Math.round((100 * (b.done || 0)) / b.total) : 0);
          return (
            <div
              key={b.id}
              className={`job-card ${live ? "job-live" : ""} ${activeBatch?.id === b.id ? "job-live" : ""}`}
              onClick={() => openBatch(b.id)}
              onKeyDown={(e) => e.key === "Enter" && openBatch(b.id)}
              role="button"
              tabIndex={0}
            >
              <div className="job-card-top">
                <span className={`pill ${live ? "ok" : ready ? "info" : "info"}`}>
                  {b.status}
                </span>
                <strong>{b.name || b.id}</strong>
                <span>{b.total || 0} TK</span>
                <span>{p}%</span>
                <span className="pill ok">L {b.kept || 0}</span>
                <span className="pill bad">X {b.discarded || 0}</span>
              </div>
              <div className="job-card-msg">{b.message || ""}</div>
              <div className="job-card-bar">
                <i style={{ width: `${p}%` }} />
              </div>
            </div>
          );
        })}
      </section>
    </div>
  );
}
