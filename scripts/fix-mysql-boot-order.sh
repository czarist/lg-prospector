#!/usr/bin/env bash
# Garante que MySQL só sobe DEPOIS de /storage montado.
# Evita o race: Docker liga cedo, bind-mount pega pasta vazia no disco raiz,
# depois /storage monta por cima e o container fica no datadir errado.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Rode com sudo: sudo $0"
  exit 1
fi

echo "==> 1) Docker espera storage.mount"
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/wait-storage.conf <<'EOF'
[Unit]
# mysql-local bind-monta /storage/mysql-data
After=storage.mount
RequiresMountsFor=/storage
EOF

echo "==> 2) Unit que sobe mysql-local só com /storage pronto"
cat > /etc/systemd/system/mysql-local-docker.service <<'EOF'
[Unit]
Description=Start mysql-local Docker container after /storage is mounted
Documentation=file:///home/lucas/lg-prospector/scripts/fix-mysql-boot-order.sh
After=docker.service storage.mount network-online.target
Wants=network-online.target
Requires=docker.service
RequiresMountsFor=/storage

[Service]
Type=oneshot
RemainAfterExit=yes
# Se o container já estiver up com datadir errado, force recreate do mount
ExecStartPre=-/usr/bin/docker stop mysql-local
ExecStart=/usr/bin/docker start mysql-local
ExecStartPost=/bin/bash -c 'for i in $(seq 1 30); do /usr/bin/docker exec mysql-local mysqladmin ping -h127.0.0.1 -uroot -proot --silent && exit 0; sleep 1; done; exit 1'
ExecStop=/usr/bin/docker stop mysql-local
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
EOF

echo "==> 3) Reload + enable"
systemctl daemon-reload
systemctl enable mysql-local-docker.service

echo "==> 4) Verificação"
systemctl cat docker.service | grep -A5 'wait-storage' || systemctl cat docker.service | tail -15
echo "---"
systemctl is-enabled mysql-local-docker.service
systemctl status mysql-local-docker.service --no-pager -l || true

echo
echo "OK. No próximo reboot:"
echo "  1) storage.mount sobe"
echo "  2) docker sobe (depois de /storage)"
echo "  3) mysql-local-docker sobe o container no datadir certo"
echo
echo "Teste agora (opcional):"
echo "  sudo systemctl start mysql-local-docker.service"
echo "  docker exec mysql-local mysql -uroot -proot -e 'SHOW DATABASES;'"
