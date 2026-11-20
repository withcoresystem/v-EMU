@echo off
"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "C:\vemu\soak\soak.ps1" %* > "C:\vemu\soak\soak-task-%RANDOM%.log" 2>&1
