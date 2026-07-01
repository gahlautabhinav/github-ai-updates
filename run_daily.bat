@echo off
REM Daily AI-repo tracker -> Obsidian. Point Windows Task Scheduler at THIS file.
REM Spawn python in its OWN process group (via `start`) and return immediately.
REM A Ctrl+C sent to the task's console during a sleep/wake or logoff transition
REM then can't reach python -- that console-teardown was killing the run
REM (0xC000013A) and losing whole days.
cd /d "%~dp0"
start "AIRepoTracker" /min cmd /c "py -3.10 -u github_ai_tracker.py >> run.log 2>&1"
