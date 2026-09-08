@echo off
rem Launch the Wyrd Network search tool (Yuzuki's memory) by double-click.
rem Comments are ASCII only: cmd.exe reads .bat in the OEM codepage (cp932)
rem BEFORE chcp runs, so Japanese here would be mis-parsed as commands.

rem Switch console to UTF-8 so Japanese output is not garbled.
chcp 65001 > nul

rem Move to this batch file's folder (project root). %~dp0 ends with a backslash.
cd /d "%~dp0"

rem Run the interactive mode with the venv Python.
"%~dp0venv\Scripts\python.exe" "%~dp0scripts\search_wyrd.py"

echo.
pause
