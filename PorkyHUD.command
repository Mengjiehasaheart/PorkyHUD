#!/bin/zsh
APP_DIR="${0:A:h}"
cd "$APP_DIR" || exit 1

if [[ -z "$TERM" || "$TERM" == "dumb" ]]; then
  export TERM=xterm-256color
fi

printf '\033]0;PorkyHUD\007'
printf '\033[8;42;132t'
clear

echo "Launching PorkyHUD..."
echo "Keys: h help, t theme, a animation, u unlock sensors, m sort, r rescan, q quit."
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "PorkyHUD needs python3. Install Apple's Command Line Tools or Python 3, then reopen this launcher."
  echo
  echo "Press Return to close this window."
  read -r _
  exit 1
fi

echo "Advanced CPU/GPU power and fan-style sensors require administrator access on macOS."
printf "Unlock advanced sensors now with sudo? [y/N] "
read -r unlock_reply
if [[ "$unlock_reply" == [Yy]* ]]; then
  sudo -v
  if [[ $? -ne 0 ]]; then
    echo "Continuing without advanced sensor unlock."
  fi
fi
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
