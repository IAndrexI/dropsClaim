@echo off
title Auto Loot Claimer - WARP Claim Helper
echo ========================================================
echo    CONNECTING CLOUDFLARE WARP FOR TWITCH CLAIM...
echo ========================================================
python "%~dp0scripts\warp_claim_helper.py" --claim --duration 10
echo.
echo ========================================================
echo    WARP DISCONNECTED. YOU ARE BACK ON NORMAL WI-FI.
echo ========================================================
timeout /t 3 >nul
