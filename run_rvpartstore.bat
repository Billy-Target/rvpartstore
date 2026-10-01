@echo off
REM Task-Scheduler wrapper. Pass the job name (and optional flags) as args,
REM e.g. run_rvpartstore.bat upload --ignore-quiet
REM This is the ONLY place a full path is allowed. The three paths below are
REM THIS DEV MACHINE's (billy's) — when deploying to the real Task Scheduler
REM machine (Francis's PC, same one francis-shopify\run_rvmarines.bat runs
REM on), edit all three lines to that machine's actual project path first.
cd /d C:\Users\billy\PycharmProjects\rvpartstore
call C:\Users\billy\PycharmProjects\rvpartstore\.venv\Scripts\activate
python C:\Users\billy\PycharmProjects\rvpartstore\main.py %*
