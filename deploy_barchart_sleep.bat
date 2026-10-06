@echo off
REM v18.1: Barchart bot sleeps until next market open instead of exiting.
set KEY=C:\Users\parth\.ssh\oracle_bot.key
set VM=ubuntu@129.80.159.101
cd /d "C:\Unusual Whale Bot\uw-options-bot"
echo === 1/2 Commit + push to GitHub ===
git add barchart_bot/barchart_pro_bot.py barchart_bot/barchartbot.service barchart_bot/README_VM.md barchart_bot/setup_vm.sh deploy_barchart_sleep.bat
git commit -m "Barchart bot v18.1: sleep until next market open instead of exiting after hours"
git push
if errorlevel 1 (echo PUSH FAILED - fix and rerun & pause & exit /b 1)
echo.
echo === 2/2 Pull on VM, disable 16:40 stop timer, reload + restart ===
ssh -i "%KEY%" %VM% "cd /home/ubuntu/uw-options-bot && git pull --ff-only && sudo cp barchart_bot/barchartbot.service /etc/systemd/system/ && sudo systemctl disable --now barchartbot-stop.timer && sudo systemctl daemon-reload && sudo systemctl restart barchartbot && sleep 5 && systemctl is-active barchartbot && journalctl -u barchartbot -n 25 --no-pager"
echo.
echo Done. Look for "active" and "Bot started. SCRIPT_VERSION = v18.1_sleep_until_open" above.
pause
