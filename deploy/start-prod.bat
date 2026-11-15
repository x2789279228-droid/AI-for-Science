@echo off
REM ============================================================
REM  Meridian production stack (Windows): API :8100 + Web :3000
REM  Idempotent: skips a component if its port is already served.
REM  Registered in the user's Startup folder (meridian-prod.bat).
REM  Logs: %LOCALAPPDATA%\meridian-prod\
REM  NOTE: keep this file ASCII-only (cmd parses it in the OEM codepage).
REM ============================================================
setlocal
set "ROOT=%~dp0.."
set "LOGDIR=%LOCALAPPDATA%\meridian-prod"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

REM ---- API :8100 (FastAPI, current code) ----
REM findstr splits its pattern on spaces (multi-pattern OR), so filter
REM twice: first by port, then by state.
netstat -ano | findstr ":8100" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
  echo [meridian] starting API on :8100 ...
  start "meridian-api" /min cmd /c "cd /d %ROOT%\api && uv run uvicorn app.main:app --host 127.0.0.1 --port 8100 --log-level warning > %LOGDIR%\api.log 2>&1"
) else (
  echo [meridian] API :8100 already running, skip.
)

REM ---- Web :3003 (Next.js production server; 3000 is taken by soc-grafana) ----
REM NEXT_DIST_DIR keeps prod artifacts (.next-prod) separate from dev (.next).
netstat -ano | findstr ":3003" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
  echo [meridian] starting Web on :3003 ...
  start "meridian-web" /min cmd /c "cd /d %ROOT%\web && set NEXT_PUBLIC_API_BASE=http://127.0.0.1:8100&& set NEXT_DIST_DIR=.next-prod&& pnpm start -p 3003 > %LOGDIR%\web.log 2>&1"
) else (
  echo [meridian] Web :3003 already running, skip.
)

echo [meridian] done. Web: http://localhost:3003  API: http://localhost:8100/api/health
endlocal
