@echo off
REM ---------------------------------------------------------------------
REM Oracle SQL Preflight
REM Author:  Federico Guzman  (github.com/kraiosis)
REM Website: https://fedeguzman.com    Blog: https://weblantropia.com
REM
REM Built with AI assistance from Claude (Anthropic). The analyzer's
REM runtime behavior stays deterministic and AI-free -- see README.md,
REM "Author & Credits", for what the AI assistance covers.
REM ---------------------------------------------------------------------
REM Oracle SQL Preflight Analyzer - Windows startup script
REM Creates a virtual environment if needed, installs dependencies,
REM starts the local server, and opens the browser.

setlocal

cd /d "%~dp0"

if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

echo Installing/checking dependencies...
pip install -q -r requirements.txt

echo Starting Oracle SQL Preflight Analyzer at http://127.0.0.1:8000
start "" http://127.0.0.1:8000

python -m app.main

endlocal
