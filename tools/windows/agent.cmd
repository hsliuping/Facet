@echo off
rem Browser agent entry for the Windows bundle. Use it from Task Scheduler:
rem   "C:\Users\<you>\AppData\Local\Programs\Facet\agent.cmd" --ensure-login https://www.modelscope.cn/my/overview
rem Exit code 0 = signed in, 3 = a human must refresh the cookie or password.
setlocal
set "FACET_ROOT=%~dp0"
set "FACET_DATA_DIR=%FACET_ROOT%data"
set "PLAYWRIGHT_BROWSERS_PATH=%FACET_ROOT%ms-playwright"
set "PYTHONUTF8=1"
"%FACET_ROOT%runtime\python.exe" "%FACET_ROOT%app\tools\computer_use_agent.py" %*
