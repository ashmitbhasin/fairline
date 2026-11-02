#!/usr/bin/env bash
# One-time setup on the EC2 recorder (Ubuntu 24.04, run as ubuntu). Called by `infra/fl install`
# after the code has been copied to ~/fairline. Safe to re-run.
set -euo pipefail
cd ~/fairline

# Logs are written in local time and the tracker reads them as the Mac's (Eastern) time.
sudo timedatectl set-timezone America/New_York

sudo apt-get update -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -yq python3-venv python3-pip rsync chrony

# 1 GB of RAM (t4g.micro); swap gives the recorders headroom for a busy Sunday.
if [ ! -f /swapfile ]; then
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

sudo install -m 644 infra/sshd-fairline.conf /etc/ssh/sshd_config.d/10-fairline.conf
sudo sshd -t && sudo systemctl reload ssh

touch ~/.hushlogin  # no login banner in infra/fl output
mkdir -p data/logs data/run secrets
chmod 700 secrets
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e .  # no dev extras: nothing installed here that the recorders do not need

sudo install -m 644 infra/fairline@.service /etc/systemd/system/fairline@.service
sudo systemctl daemon-reload
echo "setup done: $(python3 --version), $(timedatectl show -p Timezone --value), $(chronyc -n tracking | grep -m1 'System time')"
