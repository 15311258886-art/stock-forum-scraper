@echo off
rem ===========================================================================
rem Forward-ledger daily entry point (decision desk + 3 adopted criteria).
rem
rem NOTE: this file is deliberately ASCII-only. cmd.exe parses .bat using the
rem system ANSI codepage (cp936 on Chinese Windows), so a UTF-8 Chinese comment
rem turns into mojibake and can even split lines -- that is what broke the first
rem version of this script. All Chinese explanation lives in
rem docs (Chinese file name, see repo docs folder) instead.
rem
rem Why a .bat instead of the in-process scheduler: the scheduler lives in
rem viewer/server.py (_daily_job + task table, 14k+ lines) and hooking it would
rem mean touching server.py and tools/sched_job.py while other work is in
rem flight. These three scripts are idempotent and dependency-free, so a
rem scheduled task is equivalent.
rem
rem Recommended: TWO triggers, both pointing at this file
rem   15:35  record the three criteria (kline increment is done by then)
rem   16:45  catch up on the decision desk (it often lands after 16:02)
rem Running twice a day is safe: rows are keyed by (snap_date, src, code).
rem Timestamps come from Python (this file must stay ASCII, and %date%/%time%
rem would be written in the ANSI codepage and mangle the UTF-8 log).
rem ===========================================================================
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
set "LOG=%~dp0viewer\logs\forward_daily.log"
if not exist "%~dp0viewer\logs" mkdir "%~dp0viewer\logs"

echo.>> "%LOG%"
echo === forward_daily start ===>> "%LOG%"

"%PY%" "%~dp0tools\forward_snapshot.py" >> "%LOG%" 2>&1
echo --- snapshot rc=%ERRORLEVEL% >> "%LOG%"

"%PY%" "%~dp0tools\forward_score.py" >> "%LOG%" 2>&1
echo --- score rc=%ERRORLEVEL% >> "%LOG%"

"%PY%" "%~dp0tools\forward_scorecard.py" >> "%LOG%" 2>&1
echo --- scorecard rc=%ERRORLEVEL% >> "%LOG%"

echo === forward_daily end ===>> "%LOG%"

rem All three scripts exit 0 when there is nothing to do (holiday / data not
rem ready / db locked) and print the reason. Not propagating child exit codes
rem keeps Task Scheduler from flagging every holiday as a failure.
endlocal
exit /b 0
