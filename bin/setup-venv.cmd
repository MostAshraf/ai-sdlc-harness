@echo off
rem cmd.exe half of the venv-bootstrap launcher pair -- setup-venv (the
rem bash sibling) documents the full design: why init-workspace's SKILL.md
rem invokes a dual-clause command and how cmd's PATHEXT resolution lands
rem here when a non-bash platform shell (Qwen Code on Windows outside an
rem MSYS terminal) runs the unquoted fallback clause. Mirrors bin/
rem harness.cmd's probe: plugin venv Scripts\ layout, else system `python`
rem (python.org installs ship no python3). Idempotent: exits 0 immediately
rem once PyYAML is already importable.
setlocal
set "ROOT=%~dp0.."
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" goto :bootstrap
"%PY%" -c "import yaml" >nul 2>nul
if not errorlevel 1 goto :done
:bootstrap
set "SYS=python3"
where %SYS% >nul 2>nul || set "SYS=python"
where %SYS% >nul 2>nul || (
  echo ERROR: no system python3/python found to create the plugin venv 1>&2
  exit /b 1
)
"%SYS%" -m venv "%ROOT%\.venv" || exit /b 1
set "PY=%ROOT%\.venv\Scripts\python.exe"
"%PY%" -m pip install --quiet pyyaml
:done
endlocal & exit /b %ERRORLEVEL%
