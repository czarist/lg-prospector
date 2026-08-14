#!/usr/bin/env bash
# Close the remaining root-level holes that XFCE / logind can still use
# to power the machine off. User-level settings are applied separately.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi

mkdir -p /etc/systemd/logind.conf.d /etc/systemd/sleep.conf.d

cat > /etc/systemd/logind.conf.d/no-idle-poweroff.conf << 'EOF'
[Login]
IdleAction=ignore
IdleActionSec=0
HandleLidSwitch=ignore
HandleLidSwitchDocked=ignore
HandleLidSwitchExternalPower=ignore
HandleSuspendKey=ignore
HandleHibernateKey=ignore
HandlePowerKey=ignore
HandlePowerKeyLongPress=ignore
HandleRebootKey=ignore
SleepOperation=suspend
EOF

cat > /etc/systemd/sleep.conf.d/deny-sleep.conf << 'EOF'
[Sleep]
AllowSuspend=no
AllowHibernation=no
AllowSuspendThenHibernate=no
AllowHybridSleep=no
EOF

systemctl mask --now \
  sleep.target \
  suspend.target \
  hibernate.target \
  hybrid-sleep.target \
  suspend-then-hibernate.target

systemctl restart systemd-logind.service

echo "logind ignores idle/lid/power/sleep keys; sleep targets masked; sleep types denied."
echo "Check: systemd-inhibit --list && systemctl status suspend-then-hibernate.target"
