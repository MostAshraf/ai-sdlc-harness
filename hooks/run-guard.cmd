@echo off
rem cmd.exe half of the hook launcher pair — run-guard (the bash sibling)
rem documents the full design: why hooks.json registers a dual-clause
rem command and how cmd's PATHEXT resolution lands here when a non-bash
rem platform shell (Qwen Code on Windows outside an MSYS terminal) runs
rem the unquoted fallback clause. Mirrors bin/harness.cmd's probe: plugin
rem venv Scripts\ layout, else `python` (python.org installs ship no
rem python3; guards.py documents the missing-interpreter / Store-alias
rem residual as degrade-open). stdin (the hook's JSON payload) and the
rem exit code (0 allow / 2 block) pass through untouched. No PYTHONPATH:
rem guards.py puts the plugin root on sys.path itself.
setlocal
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0guards.py" %*
endlocal & exit /b %ERRORLEVEL%
