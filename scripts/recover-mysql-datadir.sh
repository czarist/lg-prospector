#!/usr/bin/env bash
# Se MySQL subiu sem /storage: para, garante mount, sobe de novo.
set -euo pipefail
echo "Checando /storage..."
if ! findmnt /storage >/dev/null; then
  echo "Montando /storage..."
  sudo mount /storage
fi
findmnt /storage
echo "Restart mysql-local..."
docker stop mysql-local 2>/dev/null || true
sleep 1
docker start mysql-local
for i in $(seq 1 30); do
  if docker exec mysql-local mysqladmin ping -h127.0.0.1 -uroot -proot --silent 2>/dev/null; then
    break
  fi
  sleep 1
done
echo "Databases:"
docker exec -e MYSQL_PWD=root mysql-local mysql -uroot -e "SHOW DATABASES;"
echo -n "HOST inode: "; ls -li /storage/mysql-data/auto.cnf | awk '{print $1}'
echo -n "CTR  inode: "; docker exec mysql-local ls -li /var/lib/mysql/auto.cnf | awk '{print $1}'
