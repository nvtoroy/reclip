# ReClip on the Contabo host

Public address: **https://d.cegth.cc:8900**

This host is shared. Before changing anything here, read why each constraint
exists — several obvious-looking shortcuts break a neighbouring service days
or weeks later, silently.

## What else lives on this box

| What | Ports | Notes |
|---|---|---|
| `mypvn` | 443, 8443 | Reality VPN. Hardened container, config in a 600 `.env`. Do not touch |
| `rs-*` stack | 4443, and 127.0.0.1 only for its databases | postgres, mariadb, opensearch, rabbitmq, valkey, memcached |
| `davinci-assist` | 5177, 5178 | dva.melekh.in |

## Rules

1. **Never bind port 80.** All four certificate lineages renew with certbot's
   `standalone` authenticator, which binds 80 itself at renewal time. A
   permanent listener there breaks renewal for the other three, and the failure
   only surfaces when a certificate actually expires.
2. **Never touch ufw.** A `ufw reload` flushes the filter, nat and mangle
   tables, removing Docker's `DOCKER`, `DOCKER-USER` and `DOCKER-ISOLATION`
   chains, which drops networking for every container on the host at once.
   It also buys nothing: Docker inserts its rules ahead of ufw, so a published
   port is internet-reachable whatever ufw says.
3. **Never `docker system prune -a --volumes`.** The rs database volumes and
   the locally built `rs-app`, `rs-search` and `rs-mining` images exist nowhere
   else — no registry holds a copy.
4. **Never `docker pull nginx:1.27-alpine`.** `rs-edge-1` and
   `davinci-assist-ingress` run that tag. Pulling re-points it locally and arms
   a surprise recreate for both on their next restart. The compose file pins
   the digest instead.
5. **Never put a hook in `/etc/letsencrypt/renewal-hooks/deploy/`.** Scripts
   there run after *every* lineage renews, not just this one.

## First install

```sh
# 1. A 5G store of its own, so filling it cannot reach the rs databases on /
fallocate -l 5G /var/lib/reclip.img
mkfs.ext4 -m0 -q /var/lib/reclip.img
mkdir -p /var/lib/reclip
mount -o loop,noatime /var/lib/reclip.img /var/lib/reclip
chown 1000:1000 /var/lib/reclip          # appuser inside the image
echo '/var/lib/reclip.img /var/lib/reclip ext4 loop,noatime,nofail 0 0' >> /etc/fstab

# 2. Source and config
mkdir -p /opt/reclip
#   rsync the repo to /opt/reclip/src, then copy this directory's
#   docker-compose.yml, nginx.conf and reload.sh into /opt/reclip/

# 3. The access token
printf 'AUTH_TOKEN=%s\n' "$(openssl rand -hex 24)" > /opt/reclip/.env
chmod 600 /opt/reclip/.env

# 4. The certificate. --no-directory-hooks stops issuance from firing
#    davinci-assist's global hook and restarting an unrelated ingress.
#    --deploy-hook registers ours per-lineage instead.
systemctl stop certbot.timer                     # avoid the shared lock
certbot certonly --standalone -d d.cegth.cc \
    --no-directory-hooks \
    --deploy-hook /opt/reclip/reload.sh
systemctl start certbot.timer

# 5. Confirm certbot did not persist no_directory_hooks into the lineage —
#    it would disable our own reload at every future renewal.
grep -E 'no_directory_hooks|renew_hook|authenticator' \
    /etc/letsencrypt/renewal/d.cegth.cc.conf

# 6. Up
cd /opt/reclip && docker compose up -d --build
```

## Updating

```sh
cd /opt/reclip
git -C src pull            # or re-rsync
docker compose up -d --build
docker image prune -f      # narrow form only, never system prune
```
