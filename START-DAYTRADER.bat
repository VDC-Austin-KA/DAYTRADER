@echo off
REM ===================================================================
REM  DAYTRADER one-click launcher
REM  Starts the dashboard, opens a Cloudflare tunnel for remote access,
REM  and prints the URL + credentials. Double-click from the desktop.
REM ===================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title DAYTRADER Launcher
color 0B

echo.
echo  ===============================================
echo   DAYTRADER - starting up
echo  ===============================================
echo.

REM --- 1. Python venv -------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo  [X] No .venv found. Run:  python -m venv .venv
    echo      then: .venv\Scripts\pip install -r requirements.txt
    pause & exit /b 1
)
set "PY=.venv\Scripts\python.exe"
echo  [OK] Python environment

REM --- 2. OpenD gateway check ----------------------------------------
REM OpenD is a GUI app with a manual trading unlock (moomoo policy forbids
REM unlocking from the SDK), so this script cannot start it for you.
netstat -ano | findstr /C:":11111" | findstr /C:"LISTENING" >nul
if errorlevel 1 (
    echo  [X] OpenD is NOT running on port 11111.
    echo.
    echo      Start moomoo OpenD, log in, and click "Unlock Trade"
    echo      in its window. Then run this script again.
    echo.
    pause & exit /b 1
)
echo  [OK] OpenD gateway listening on 11111

REM --- 3. Dashboard password (required for the tunnel) ---------------
REM The tunnel is a PUBLIC url and this dashboard places live orders,
REM so we refuse to expose it unauthenticated. Generate one if absent.
findstr /B /C:"DASHBOARD_PASSWORD=" .env >nul 2>&1
if errorlevel 1 (
    echo  [..] No DASHBOARD_PASSWORD set - generating one
    for /f %%p in ('%PY% -c "import secrets;print(secrets.token_urlsafe(12))"') do set "GENPW=%%p"
    echo.>> .env
    echo DASHBOARD_USER=trader>> .env
    echo DASHBOARD_PASSWORD=!GENPW!>> .env
    echo  [OK] Password generated and saved to .env
)
for /f "tokens=2 delims==" %%p in ('findstr /B /C:"DASHBOARD_PASSWORD=" .env') do set "DASHPW=%%p"
for /f "tokens=2 delims==" %%u in ('findstr /B /C:"DASHBOARD_USER=" .env') do set "DASHUSER=%%u"
if "!DASHUSER!"=="" set "DASHUSER=trader"

REM --- 4. Start the dashboard ----------------------------------------
echo  [..] Starting dashboard on port 8000
start "DAYTRADER server" /min cmd /c "%PY% -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > server.log 2>&1"

REM Wait for it to answer before tunnelling (up to ~40s; first run trains).
set /a tries=0
:waitloop
set /a tries+=1
call :sleep 2
curl -s -o nul -m 2 http://127.0.0.1:8000/api/health && goto serverup
if !tries! LSS 20 goto waitloop
echo  [X] Server did not come up. Check server.log
pause & exit /b 1
:serverup
echo  [OK] Dashboard running

REM --- 5. Cloudflare tunnel ------------------------------------------
where cloudflared >nul 2>&1
if errorlevel 1 (
    echo  [!] cloudflared not found - remote access disabled.
    echo      Install:  winget install --id Cloudflare.cloudflared
    goto localonly
)
echo  [..] Opening Cloudflare tunnel
if exist tunnel.log del tunnel.log
start "DAYTRADER tunnel" /min cmd /c "cloudflared tunnel --url http://localhost:8000 > tunnel.log 2>&1"

set /a tries=0
:tunnelwait
set /a tries+=1
call :sleep 2
for /f "tokens=*" %%u in ('findstr /R /C:"https://.*trycloudflare.com" tunnel.log 2^>nul') do (
    for %%t in (%%u) do echo %%t | findstr /C:"trycloudflare.com" >nul && set "TUNNEL=%%t"
)
if not "!TUNNEL!"=="" goto tunnelup
if !tries! LSS 30 goto tunnelwait
echo  [!] Tunnel did not report a URL - check tunnel.log
goto localonly

:tunnelup
echo  [OK] Tunnel live

REM --- 6. Publish the tunnel URL to the stable Railway front door -------
REM One HTTP call: the phone bookmark (https://<railway-app>/go) then
REM redirects here. Skipped silently if not configured.
if not "!TUNNEL!"=="" (
    for /f "tokens=2 delims==" %%s in ('findstr /B /C:"TUNNEL_UPDATE_SECRET=" .env 2^>nul') do set "TSECRET=%%s"
    for /f "tokens=2 delims==" %%r in ('findstr /B /C:"TUNNEL_PUBLISH_URL=" .env 2^>nul') do set "TPUB=%%r"
    if not "!TSECRET!"=="" if not "!TPUB!"=="" (
        echo  [..] Publishing tunnel URL to !TPUB!
        curl -s -m 15 -X POST -H "Content-Type: application/json" ^
             -d "{\"url\":\"!TUNNEL!\",\"secret\":\"!TSECRET!\"}" ^
             "!TPUB!/api/tunnel" >nul 2>&1
        if errorlevel 1 (echo  [!] Publish failed - bookmark may point at the old URL) else (echo  [OK] Phone bookmark now points here)
    )
)

:localonly
echo.
echo  ===============================================
echo   READY TO TRADE
echo  ===============================================
echo.
echo   Local:   http://127.0.0.1:8000
if not "!TUNNEL!"=="" echo   Remote:  !TUNNEL!
echo.
echo   Username: !DASHUSER!
echo   Password: !DASHPW!
echo.
echo   MODE: LIVE - buttons place REAL orders on your
echo         real margin account. Check the Qty box.
echo.
echo  ===============================================
echo.
start "" http://127.0.0.1:8000
echo  Close this window to keep running, or press a
echo  key to SHUT DOWN the server and tunnel.
pause >nul

echo  Shutting down...
taskkill /FI "WINDOWTITLE eq DAYTRADER server*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq DAYTRADER tunnel*" /T /F >nul 2>&1
taskkill /IM cloudflared.exe /F >nul 2>&1
echo  Done.
call :sleep 2
goto :eof

:sleep
ping -n %~1 127.0.0.1 >nul 2>&1
goto :eof

