@echo off
cd /d "%~dp0"
if not exist "node_modules\" (
  echo Installing npm packages...
  call npm install
)
echo.
echo Frontend Next.js  http://127.0.0.1:3000
echo User:  http://127.0.0.1:3000/
echo Admin: http://127.0.0.1:3000/admin
echo (Backend must run on :5050)
echo.
start http://127.0.0.1:3000/admin
call npm run dev
pause
