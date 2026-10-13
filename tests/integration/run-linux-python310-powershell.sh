#!/bin/sh
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates libicu76 libssl3t64 libunwind8 libgssapi-krb5-2 >/tmp/configbackup-install.log 2>&1
curl --fail --location --retry 3 -o /tmp/powershell.tar.gz https://github.com/PowerShell/PowerShell/releases/download/v7.6.6/powershell-7.6.6-linux-arm64.tar.gz
printf '%s  %s\n' '924829e54c983648f6f1419a2dc7f9433c861b2fb5bd57736ff096c24f133729' '/tmp/powershell.tar.gz' | sha256sum -c -
mkdir -p /opt/powershell
tar -xzf /tmp/powershell.tar.gz -C /opt/powershell
chmod +x /opt/powershell/pwsh
ln -s /opt/powershell/pwsh /usr/local/bin/pwsh
pwsh --version
python -m pip install -q -r /project/requirements.txt
cp -R /project /tmp/project
cd /tmp/project
python -m unittest discover -s tests -v
