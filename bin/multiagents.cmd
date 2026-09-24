@echo off
rem multiagents CLI for cmd/PowerShell on Windows (Git Bash uses the sibling "multiagents" script).
rem Pick the interpreter first, then run exactly once: a failing run must not be retried with
rem another Python (a failed worker round would be paid for twice).
setlocal
set "MA=%~dp0..\worker\multiagents.py"
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && goto use_py
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && goto use_python
echo multiagents: Python 3.9+ not found (tried py -3 and python) 1>&2
exit /b 2
:use_py
py -3 "%MA%" %*
exit /b %errorlevel%
:use_python
python "%MA%" %*
exit /b %errorlevel%
