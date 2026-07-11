#!/bin/zsh
APP_DIR="${0:A:h}"
LAUNCHER_PATH="${0:A}"
cd "$APP_DIR" || exit 1

if [[ -z "$TERM" || "$TERM" == "dumb" ]]; then
  export TERM=xterm-256color
fi

printf '\033]0;PorkyHUD\007'
palette_changed=0
restore_terminal_palette() {
  if (( palette_changed )); then
    printf '\033]110\007'
    printf '\033]111\007'
    printf '\033]112\007'
    printf '\033[0m'
    palette_changed=0
  fi
}
trap restore_terminal_palette EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -z "${NO_COLOR+x}" ]]; then
  palette_changed=1
  printf '\033]10;#e6f7ff\007'
  printf '\033]11;#05070d\007'
  printf '\033]12;#5df2ff\007'
  printf '\033[40m\033[97m'
fi
printf '\033[8;42;132t'
clear

echo "Launching PorkyHUD..."
echo "Keys: h help, t theme, l layout, a animation, u unlock sensors, m sort, r refresh, q quit."
echo

if [[ -f "$APP_DIR/porkyhud.py" ]]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "PorkyHUD needs python3. Install Apple's Command Line Tools or Python 3, then reopen this launcher."
    echo
    echo "Press Return to close this window."
    read -r _
    exit 1
  fi
  run_command=(/usr/bin/env python3 "$APP_DIR/porkyhud.py")
  cli_hint="./porkyhud.py"
else
  installed_porkyhud="$(command -v porkyhud 2>/dev/null)"
  if [[ -z "$installed_porkyhud" ||
        ! -f "$installed_porkyhud" ||
        ! -x "$installed_porkyhud" ||
        "${installed_porkyhud:A}" == "$LAUNCHER_PATH" ]]; then
    echo "PorkyHUD could not find a sibling porkyhud.py or a separate installed porkyhud executable."
    echo
    echo "Press Return to close this window."
    read -r _
    exit 1
  fi
  run_command=("$installed_porkyhud")
  cli_hint="porkyhud"
fi

echo "Terminal-first build: l cycles layouts, m cycles CPU/MEM/GPU process sort."
echo "Diagnostics: $cli_hint --snapshot or $cli_hint --json"
echo
echo "Read-only fans and temperatures use AppleSMC when available."
echo "Advanced CPU/GPU power and residency counters require administrator access on macOS."
echo "One-time advanced setup: $cli_hint --setup-sensors"
echo "Temporary session unlock inside the HUD: press u."
echo

"${run_command[@]}"
exit_code=$?
echo
if [[ $exit_code -ne 0 ]]; then
  echo "PorkyHUD exited with status $exit_code."
else
  echo "PorkyHUD closed."
fi
echo "Press Return to close this window."
read -r _
