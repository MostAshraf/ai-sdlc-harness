@echo off
rem Windows sibling of `bin/harness` (same resolution contract): prefer the
rem plugin venv, fall back to a system Python pre-setup. PYTHONPATH — not
rem `cd` — makes the package importable, so the caller's cwd (the TARGET
rem workspace; `--workspace` defaults to it) is never disturbed.
rem `harness/__main__.py` pops PYTHONPATH as its first statement, so it does
rem not leak into subprocesses the CLI spawns (test probes, verify-red).
setlocal
set "HERE=%~dp0"
set "PY=%HERE%..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONPATH=%HERE%.."
"%PY%" -m harness %*
endlocal & exit /b %ERRORLEVEL%
