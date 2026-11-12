@echo off
rem Stop a running Facet tray + its gateway (used by the uninstaller). Only
rem processes whose command line runs facet_tray.py or run_gateway.py are
rem killed, so other python/pythonw apps survive.
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | Where-Object { $_.CommandLine -like '*facet_tray.py*' -or $_.CommandLine -like '*run_gateway.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
