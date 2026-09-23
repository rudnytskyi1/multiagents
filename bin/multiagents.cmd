@echo off
rem multiagents CLI for cmd/PowerShell on Windows (Git Bash uses the sibling "multiagents" script).
setlocal
set "MA=%~dp0..\worker\multiagents.py"
where py >nul 2>nul && ( py -3 "%MA%" %* ) || ( python "%MA%" %* )
