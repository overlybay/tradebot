@echo off
title TradeBot
echo ============================================
echo   TradeBot - starting...
echo   (Paper mode by default. Fake money only.)
echo ============================================
python -m pip install --quiet -r requirements.txt
python app.py
pause
