#!/bin/bash
# Symlink the userscripts into qutebrowser's userscripts directory and create
# a profile file from the template on first run. Nothing is overwritten.
set -e
src="$(cd "$(dirname "$0")" && pwd)"
dest="${XDG_DATA_HOME:-$HOME/.local/share}/qutebrowser/userscripts"
conf="${XDG_CONFIG_HOME:-$HOME/.config}/qutebrowser/autofill"
mkdir -p "$dest" "$conf"
chmod 700 "$conf"
for f in autofill autofill.js keepassxc-login keepassxc-newpass kpxc_lookup.py autofill-lib; do
    ln -sfn "$src/userscripts/$f" "$dest/$f"
done
if [ ! -e "$conf/profiles.toml" ]; then
    cp "$src/profiles.toml.template" "$conf/profiles.toml"
    chmod 600 "$conf/profiles.toml"
    echo "created $conf/profiles.toml - fill it with your data"
fi
cat <<'KEYS'

Add to ~/.config/qutebrowser/config.py (then run :config-source):

  config.bind('pf', 'spawn --userscript autofill', mode='normal')
  config.bind('pF', 'spawn --userscript autofill --choose', mode='normal')
  config.bind('pu', 'spawn --userscript autofill --undo', mode='normal')
  config.bind('pw', 'spawn --userscript keepassxc-login --key YOUR_GPG_KEY_ID', mode='normal')
  config.bind('pT', 'spawn --userscript keepassxc-login --key YOUR_GPG_KEY_ID --totp', mode='normal')
  config.bind('pn', 'spawn --userscript keepassxc-newpass --key YOUR_GPG_KEY_ID', mode='normal')

KEYS
