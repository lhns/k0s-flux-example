<?php
// Merged after config.php (Nextcloud loads config/*.config.php last), this
// repoints the DB + Redis for the cluster and adds reverse-proxy trust. The
// preserved config.php keeps instanceid/secret/passwordsalt. The DB password
// comes from the POSTGRES_PASSWORD env (reflected CNPG secret) via getenv() so
// no secret is stored in this ConfigMap. The `redis` array fully replaces the
// old one (dropping its password — the in-cluster Redis has no auth).
$CONFIG = array(
  'dbhost' => 'postgres-rw.postgres.svc.cluster.local',
  'dbport' => '5432',
  // The old install connected as `oc_admin`; the dump was restored --no-owner so
  // the CNPG `nextcloud` role owns the tables. Point the app at that role.
  'dbuser' => 'nextcloud',
  'dbpassword' => getenv('POSTGRES_PASSWORD'),
  'redis' => array(
    'host' => 'nextcloud-redis',
    'port' => 6379,
  ),
  'trusted_proxies' => array('172.18.0.0/16'),
  'overwriteprotocol' => 'https',
  'overwrite.cli.url' => 'https://nextcloud.example.com',
  // The migrated config.php had loglevel 0 (DEBUG) which grew nextcloud.log to
  // 20G; pin WARN so it can't run away again.
  'loglevel' => 2,
);
