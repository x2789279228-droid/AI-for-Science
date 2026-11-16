@echo off
REM ============================================================
REM  Register + run a Task Scheduler entry that launches the
REM  production stack OUTSIDE any terminal process group.
REM  Why: services started from an agent/terminal session die
REM  when that session tears down (job-object kill / console
REM  signals). Tasks launched by the Scheduler service are not
REM  in that group and survive; a daily 04:00 trigger self-heals.
REM  Run on demand: powershell Start-ScheduledTask -TaskName
REM  'MeridianProdStack'  (schtasks /Run may fail on this task).
REM  Keep this file ASCII-only (cmd parses it in the OEM codepage).
REM ============================================================
schtasks /Create /TN "MeridianProdStack" /TR "D:\meridian\deploy\start-prod.bat" /SC DAILY /ST 04:00 /F
if errorlevel 1 (
  echo [meridian] task create FAILED - fallback to direct start.
  cmd /c "D:\meridian\deploy\start-prod.bat"
  goto :eof
)
powershell -NoProfile -Command "Start-ScheduledTask -TaskName 'MeridianProdStack'"
echo [meridian] stack launched via Task Scheduler (survives terminal exit).
