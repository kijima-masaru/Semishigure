#!/bin/bash
# FusionPBX 5.5 on Ubuntu 24.04 with a source-built FreeSWITCH (prefix /usr/local/freeswitch), as used for the
# Semishigure verification. Assumes: PostgreSQL, PHP 8.x (cli, pgsql, xml, mbstring, curl, gd, sqlite3, zip)
# and a FreeSWITCH built with mod_pgsql + mod_lua (+ the modules in ../freeswitch/README.md).
# Usage: sudo ./install-native.sh <domain name> <admin password> <db password>
set -euo pipefail
DOMAIN=${1:-pbx.semishigure.test}; ADMIN_PW=${2:-semishigure-admin}; DB_PW=${3:-semishigure-db}
FS_PREFIX=${FS_PREFIX:-/usr/local/freeswitch}
SRC=${FUSIONPBX_SRC:-/usr/src/fusionpbx}
[ -d "$SRC" ] || git clone --depth 1 --branch 5.5 https://github.com/fusionpbx/fusionpbx.git "$SRC"
# database
service postgresql start
su - postgres -c "psql -tAc \"select 1 from pg_roles where rolname='fusionpbx'\"" | grep -q 1 || su - postgres -c "psql -c \"CREATE ROLE fusionpbx WITH SUPERUSER LOGIN PASSWORD '$DB_PW';\""
su - postgres -c "psql -tAc \"select 1 from pg_database where datname='fusionpbx'\"" | grep -q 1 || su - postgres -c "psql -c \"CREATE DATABASE fusionpbx WITH OWNER fusionpbx;\""
# web tree and config
rm -rf /var/www/fusionpbx && cp -r "$SRC" /var/www/fusionpbx && rm -rf /var/www/fusionpbx/.git
mkdir -p /etc/fusionpbx /var/cache/fusionpbx /etc/freeswitch /var/lib/freeswitch/db /var/lib/freeswitch/recordings /var/lib/freeswitch/storage /usr/share/freeswitch/scripts /usr/share/freeswitch/sounds /var/log/freeswitch /var/run/freeswitch
cat > /etc/fusionpbx/config.conf <<CONF
database.0.type = pgsql
database.0.host = 127.0.0.1
database.0.port = 5432
database.0.sslmode = prefer
database.0.name = fusionpbx
database.0.username = fusionpbx
database.0.password = $DB_PW
database.1.type = sqlite
database.1.path = /var/lib/freeswitch/db
database.1.name = core.db
document.root = /var/www/fusionpbx
project.path =
temp.dir = /tmp
php.dir = /usr/bin
php.bin = php
cache.method = file
cache.location = /var/cache/fusionpbx
cache.settings = true
switch.conf.dir = /etc/freeswitch
switch.sounds.dir = /usr/share/freeswitch/sounds
switch.database.dir = /var/lib/freeswitch/db
switch.recordings.dir = /var/lib/freeswitch/recordings
switch.storage.dir = /var/lib/freeswitch/storage
switch.voicemail.dir = /var/lib/freeswitch/storage/voicemail
switch.scripts.dir = /usr/share/freeswitch/scripts
switch.bin = $FS_PREFIX/bin
xml_handler.fs_path = false
xml_handler.reg_as_number_alias = false
xml_handler.number_as_presence_id = true
error.reporting = user
CONF
# FreeSWITCH conf + lua scripts from FusionPBX
cp -R /var/www/fusionpbx/app/switch/resources/conf/* /etc/freeswitch/
cp -R /var/www/fusionpbx/app/switch/resources/scripts/* /usr/share/freeswitch/scripts/
grep -q mod_pgsql /etc/freeswitch/autoload_configs/modules.conf.xml || sed -i 's#<load module="mod_lua"/>#<load module="mod_pgsql"/>\n\t\t<load module="mod_lua"/>#' /etc/freeswitch/autoload_configs/modules.conf.xml
sed -i '/mod_spandsp/d' /etc/freeswitch/autoload_configs/modules.conf.xml
sed -i -e 's#{v_http_protocol}#http#' -e 's#{domain_name}#127.0.0.1:8090#' -e 's#{v_project_path}##' -e 's#{v_user}#xmlcdr#' -e 's#{v_pass}#xmlcdr#' /etc/freeswitch/autoload_configs/xml_cdr.conf.xml
chown -R www-data:www-data /var/www/fusionpbx /var/cache/fusionpbx /etc/freeswitch /var/lib/freeswitch /usr/share/freeswitch /var/log/freeswitch /var/run/freeswitch
# schema, domain, defaults, admin user
export PGPASSWORD=$DB_PW
cd /var/www/fusionpbx
php core/upgrade/upgrade.php --schema
DOMAIN_UUID=$(php resources/uuid.php)
psql -h 127.0.0.1 -U fusionpbx -d fusionpbx -c "insert into v_domains (domain_uuid, domain_name, domain_enabled) values('$DOMAIN_UUID', '$DOMAIN', 'true');"
php core/upgrade/upgrade.php --defaults
USER_UUID=$(php resources/uuid.php); SALT=$(php resources/uuid.php); HASH=$(php -r "echo md5('${SALT}${ADMIN_PW}');")
psql -h 127.0.0.1 -U fusionpbx -d fusionpbx -c "insert into v_users (user_uuid, domain_uuid, username, password, salt, user_enabled) values('$USER_UUID', '$DOMAIN_UUID', 'admin', '$HASH', '$SALT', 'true');"
GROUP_UUID=$(psql -h 127.0.0.1 -U fusionpbx -d fusionpbx -qtAX -c "select group_uuid from v_groups where group_name = 'superadmin';")
psql -h 127.0.0.1 -U fusionpbx -d fusionpbx -c "insert into v_user_groups (user_group_uuid, domain_uuid, group_name, group_uuid, user_uuid) values('$(php resources/uuid.php)', '$DOMAIN_UUID', 'superadmin', '$GROUP_UUID', '$USER_UUID');"
php core/upgrade/upgrade.php --permissions
cp "$(dirname "$0")/router.php" /var/www/fusionpbx/router.php
cat <<MSG
FusionPBX installed. Start:
  cd /var/www/fusionpbx && php -S 127.0.0.1:8090 -t /var/www/fusionpbx router.php &     # or nginx + php-fpm
  $FS_PREFIX/bin/freeswitch -nonat -nc -nf -conf /etc/freeswitch -log /var/log/freeswitch -db /var/lib/freeswitch/db \\
     -run /var/run/freeswitch -scripts /usr/share/freeswitch/scripts -mod $FS_PREFIX/lib/freeswitch/mod
Login: http://127.0.0.1:8090/  admin@$DOMAIN / $ADMIN_PW
MSG
