@echo off
rem Three-color host: sample/send every 30s; firmware owns the display refresh schedule.
rem Does not create or overwrite config.json. All original flags remain available.
cd /d "%~dp0"
echo Three-color host: OK means receipt only. Display must be checked separately. 1>&2
call "%~dp0run_host.bat" %*
exit /b %errorlevel%
