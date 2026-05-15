#!/bin/zsh
APP_DIR="${0:A:h}"
cd "$APP_DIR" || exit 1

if [[ -z "$TERM" || "$TERM" == "dumb" ]]; then
  export TERM=xterm-256color
fi

printf '\033]0;PorkyHUD\007'
printf '\033]10;#e6f7ff\007'
printf '\033]11;#05070d\007'
printf '\033]12;#5df2ff\007'
printf '\033[40m\033[97m'
printf '\033[8;42;132t'
clear

echo "Launching PorkyHUD..."
echo "Keys: h help, t theme, l layout, a animation, u unlock sensors, m sort, r rescan, q quit."
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "PorkyHUD needs python3. Install Apple's Command Line Tools or Python 3, then reopen this launcher."
  echo
  echo "Press Return to close this window."
  read -r _
  exit 1
fi

echo "Terminal-first build: l cycles layouts, m cycles CPU/MEM/GPU process sort."
echo "Diagnostics: ./porkyhud.py --snapshot or ./porkyhud.py --json"
echo
echo "Read-only fans and temperatures use AppleSMC when available."
echo "Advanced CPU/GPU power and residency counters require administrator access on macOS."
echo "One-time advanced setup: ./porkyhud.py --setup-sensors"
echo "Temporary session unlock inside the HUD: press u."
echo

/usr/bin/env python3 "$APP_DIR/porkyhud.py"
exit_code=$?
echo
if [[ $exit_code -ne 0 ]]; then
  echo "PorkyHUD exited with status $exit_code."
else
  echo "PorkyHUD closed."
fi
echo "Press Return to close this window."
read -r _
