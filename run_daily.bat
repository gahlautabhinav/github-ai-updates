@echo off
REM Daily AI-repo tracker -> Obsidian. Point Windows Task Scheduler at THIS file.
cd /d "%~dp0"
py -3.10 -u github_ai_tracker.py >> run.log 2>&1
