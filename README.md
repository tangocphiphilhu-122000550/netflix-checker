# Netflix Cookie Checker

FE (Next.js) + BE (Flask) + Supabase.  
**Chỉ dùng cookie tài khoản bạn sở hữu / được phép kiểm tra.**

Cookie live lưu trong **database** (Supabase).

## Cấu trúc

```text
backend/            ← API Flask + job nền + Supabase
  app.py
  checker/          ← logic check / batch / DB (dùng bởi BE)
  .env.example
  requirements.txt
  start_backend.bat

frontend-next/      ← Next.js 15
  src/app/          ← / (user)  ·  /admin
  src/components/
  src/lib/
  .env.example
  start_frontend.bat
```

## Chạy local

### 1) Backend

```bash
cd backend
copy .env.example .env
# sửa DATABASE_URL, ADMIN_PASSWORD trong .env
python -m venv ..\venv
..\venv\Scripts\pip.exe install -r requirements.txt
..\venv\Scripts\python.exe app.py
```

→ `http://127.0.0.1:5050`

### 2) Frontend (Next.js)

```bash
cd frontend-next
copy .env.example .env.local
# sửa .env.local: NEXT_PUBLIC_API_BASE=<URL backend>
npm install
npm run dev
```

→ User: `http://127.0.0.1:3000/`  
→ Admin: `http://127.0.0.1:3000/admin`

URL BE **không** hardcode trong source — chỉ nằm trong `.env.local` (gitignored).

## Flow

| Ai | Làm gì |
|----|--------|
| **Admin** | Upload list cookie → BE tạo phiếu → check nền → **LIVE** vào DB |
| **User** | Xem catalog LIVE · tạo login link · xem cookie từ DB |
| **HOLD / FREE / DEAD** | Không lưu catalog user |

Tắt FE **không** dừng job check trên BE.

## Env

### `backend/.env`

```env
DATABASE_URL=postgresql://...
ADMIN_PASSWORD=...
ADMIN_SECRET=...
CHECK_WORKERS=6
CHECK_TIMEOUT=10
CORS_ORIGINS=*
CHECKER_HOST=127.0.0.1
CHECKER_PORT=5050
```

### `frontend-next/.env.local` (không commit)

```env
NEXT_PUBLIC_API_BASE=https://your-backend.onrender.com
```

## Deploy backend lên Render

### 1) Dashboard

1. [render.com](https://render.com) → **New** → **Web Service**
2. Connect repo `phiphi171004/netflix-checker`
3. Điền form:

| Field | Giá trị |
|-------|---------|
| **Name** | `netflix-checker-api` (tuỳ tên) |
| **Region** | gần Supabase (vd Singapore) |
| **Root Directory** | `backend` |
| **Runtime** | Python 3 |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 16 --timeout 300` |
| **Instance type** | Free (hoặc Starter) |

> **Bắt buộc `--workers 1`**: token admin + job check chạy trong process (thread). Nhiều worker sẽ mất session / job.

### 2) Environment Variables (Render → Environment)

| Key | Value |
|-----|--------|
| `DATABASE_URL` | Connection string Supabase (pooler `:6543`, URL-encode password nếu có ký tự đặc biệt) |
| `ADMIN_PASSWORD` | password đăng nhập admin |
| `ADMIN_SECRET` | chuỗi ngẫu nhiên dài (session/token) |
| `CHECK_WORKERS` | `3` (free tier đừng set cao — check chạy process riêng) |
| `CHECK_WORKERS_MAX` | `4` (trần cứng, kể cả khi admin gửi workers lớn) |
| `CHECK_USE_PROCESS` | `1` (tách process check khỏi API — tránh lag user) |
| `DB_POOL_API` | `8` (pool DB dành riêng HTTP API) |
| `DB_POOL_WORKER` | `4` (pool DB dành riêng batch check) |
| `CHECK_TIMEOUT` | `10` |
| `CORS_ORIGINS` | URL FE, vd `https://xxx.vercel.app` hoặc `*` tạm thời |

Render tự set `PORT` — **không** cần `CHECKER_PORT`.

### 3) Sau khi deploy

- API: URL Render cấp (vd `https://….onrender.com`)
- Health: mở `https://…/api/live-accounts`
- FE: ghi URL đó vào `frontend-next/.env.local` → `NEXT_PUBLIC_API_BASE=...`

### 4) Lưu ý free tier

- Service **sleep** sau ~15 phút không request → request đầu chậm / timeout
- Job check dài có thể bị cắt khi sleep — dùng Starter nếu check lô lớn
- Supabase: dùng **Transaction pooler** (`:6543`) + `sslmode=require` nếu connection string chưa có

### Blueprint (tuỳ chọn)

File `render.yaml` ở root repo — Render → **New Blueprint Instance** → chọn repo.

## Deploy frontend lên Vercel

### 1) Dashboard

1. [vercel.com](https://vercel.com) → **Add New…** → **Project**
2. Import repo `phiphi171004/netflix-checker`
3. Điền form:

| Field | Giá trị |
|-------|---------|
| **Framework Preset** | Next.js (auto) |
| **Root Directory** | `frontend-next` ← **bắt buộc** (Edit → chọn folder) |
| **Build Command** | `npm run build` (mặc định) |
| **Output Directory** | để trống (Next.js tự lo) |
| **Install Command** | `npm install` |

### 2) Environment Variables (Vercel → Settings → Environment Variables)

| Key | Value | Environments |
|-----|--------|--------------|
| `NEXT_PUBLIC_API_BASE` | `https://netflix-checker-api.onrender.com` | Production, Preview, Development |

> Không có dấu `/` cuối URL.

### 3) Deploy

Bấm **Deploy**. Xong sẽ có URL kiểu:

`https://netflix-checker-xxx.vercel.app`

### 4) Nối CORS phía Render (BE)

Vào Render → service API → **Environment** → sửa:

```text
CORS_ORIGINS=https://netflix-checker-xxx.vercel.app
```

Hoặc tạm:

```text
CORS_ORIGINS=*
```

Rồi **Manual Deploy** / restart BE một lần.

### 5) Local vẫn dùng env riêng

`frontend-next/.env.local` (gitignored) — không ảnh hưởng Vercel.

### CLI (tuỳ chọn)

```bash
cd frontend-next
npx vercel login
npx vercel          # preview
npx vercel --prod   # production
# khi hỏi: set root = frontend-next, env NEXT_PUBLIC_API_BASE=...
```

## Bảo mật

- Không commit `.env` / cookie thật
- Chỉ commit `.env.example`
- URL API public qua `NEXT_PUBLIC_*` (client bundle) — không để secret thật vào biến `NEXT_PUBLIC_`
