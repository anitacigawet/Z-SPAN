@echo off
rem zspan.bat — Windows convenience wrapper.
rem Forwards everything to the package so `zspan ...` works from this
rem folder without PATH setup. Prefers the py launcher (ships with the
rem python.org installer), falls back to python on PATH.
where py >nul 2>nul
if %errorlevel%==0 (
    py -m zspan_cli %*
) else (
    python -m zspan_cli %*
)
