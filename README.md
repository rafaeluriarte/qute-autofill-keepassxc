# qute-autofill-keepassxc

Two [qutebrowser](https://qutebrowser.org) userscripts:

* **autofill** fills address and contact forms in **Italian, Portuguese (PT and BR) and English** from a local profile file.
* **keepassxc-login / keepassxc-newpass** make KeePassXC logins easier: a lookup that still finds your entry when you are not on the exact page it was saved from, and a one-key "generate a password, store it in KeePassXC, then fill it".

Everything runs in qutebrowser's isolated JavaScript world, so websites cannot read your profile. Nothing is ever submitted for you.

## Features

### autofill (`pf`, `pF`, `pu`)

* Recognises fields by `autocomplete` attributes first, then by label, name, id, placeholder and aria-label keywords in Italian, Portuguese and English, accent insensitive.
* Handles the awkward cases: Italian `nome` vs `nome e cognome`, Portuguese `apelido` (surname in Portugal, nickname in Brazil), `numero` as house number vs phone.
* Country and region selects matched by ISO code or by name in any of the three languages.
* Postal codes per country: CAP (IT), `0000-000` (PT), CEP `00000-000` (BR), adapted to a field's `maxlength`.
* Tax and person IDs: codice fiscale, partita IVA, NIF, CPF.
* Dates of birth as one field or as day/month/year parts, in the page's own order.
* Fills inside open **shadow DOM** and same-origin frames, and keeps watching for fields that appear a few seconds later (multi-step checkouts).
* Fills only empty fields unless `--overwrite`; never submits; skips passwords, card numbers, CVV, captchas and search boxes.
* Works with React, Vue and Angular forms (native value setter plus bubbling `input`/`change` events).
* `--undo` restores the previous values.
* Per-site rules in the profile file: which profile to use, which fields to skip.
* Logs the **labels** of fields it could not classify (never values) so the keyword lists can be improved: `autofill --misses` writes a report and opens it.

### keepassxc-login (`pw`, `pT`)

KeePassXC only matches a saved entry to a page when the entry's URL is the same host or a **parent** of it, so an entry saved on `accounts.example.com` is invisible from `www.example.com`. This script therefore tries, in order:

1. a site you picked before for this host (remembered),
2. the exact page URL,
3. the site root, and the other scheme (http/https),
4. parent domains, with and without `www`,
5. common login subdomains (`accounts.`, `login.`, `auth.`, `id.`, `sso.`, ...),
6. and finally asks, via rofi, which site to search.

Whatever works is remembered for next time (`--forget` clears it). Several matches are shown in a rofi list (login, title, URL, never a password). `--totp` fills a TOTP code instead.

If even that finds nothing, because the entry has **no URL at all**, or lives in a database that is not currently open in KeePassXC, the script falls back to searching the database *file* with `keepassxc-cli`:

* it uses your configured database (a rofi picker only if there are several),
* asks for the master password with rofi (passed to `keepassxc-cli` on stdin only: never on the command line, in an environment variable, in a file, or in any log),
* one prompt doubles as search box and result list, pre-filled with the site name; entries **without a URL are listed like any other**, because the search goes by title and username,
* fills the entry you pick,
* then offers (defaulting to yes) to write the page's URL into that entry, so the fast path works next time. KeePassXC's window may ask to reload the database afterwards, because the file changed on disk.

Databases and the per-site choices are remembered in `~/.config/qutebrowser/keepassxc-login.toml` (no secrets). `--cli` goes straight to this path.

### keepassxc-newpass (`pn`)

1. Asks for the login (your profile email by default) via rofi.
2. Generates the password with **KeePassXC's own generator** (`keepassxc-cli generate`, 14 characters, every character class, no look-alikes), adapted to the form's `minlength`, `maxlength` and `pattern`. Falls back to Python's `secrets` if the CLI is missing.
3. **Saves it to KeePassXC first.** On a change-password form it updates the existing entry (KeePassXC keeps the old password in the entry history); on a sign-up form with an existing login it asks whether to update or create.
4. Only after a confirmed save does it fill the new-password and confirm fields. There is no mode that fills a password without storing it.

## Requirements

* qutebrowser 3.x (tested on 3.7 with QtWebEngine 6.11)
* Python 3.11+ (`tomllib`)
* [qute-keepassxc](https://github.com/MB-Tech/qute-keepassxc) by Markus Blöchl for the KeePassXC scripts: they reuse its protocol client and its existing association, so `pw` keeps working as before. Install it in the same userscripts directory, or point `$QUTE_KEEPASSXC` at it.
* KeePassXC 2.7+ with browser integration enabled, plus `keepassxc-cli` for password generation
* `rofi` for the pickers
* `python-pynacl` for the KeePassXC protocol, `tomlkit` for the lookup config file
* optional: `node` + `jsdom` for the missed-field report and the test suite

## Install

```sh
git clone <this-repo-url>
cd qute-autofill-keepassxc
./install.sh          # symlinks the scripts, creates profiles.toml from the template
```

Then put your data in `~/.config/qutebrowser/autofill/profiles.toml` (the file is created `chmod 600`), add the key bindings printed by the installer to `config.py`, and run `:config-source`.

## Your data

* Profiles live only in `~/.config/qutebrowser/autofill/profiles.toml`, owner-readable only, and are **never** part of this repository.
* The profile is passed to the page through qutebrowser's isolated world; page scripts cannot read it (verified by a test that tries).
* Nothing is written to disk while filling: no temporary file with your data.
* The missed-field log stores field labels only, never values.
* Remembered KeePassXC sites are host names only, no credentials.
* Passwords never appear in qutebrowser messages, logs or rofi.

## Tests

```sh
cd tests/autofill   && npm install && node run-tests.js && python3 -m unittest test_site_settings
cd ../keepassxc     && python3 -m unittest discover -p 'test_*.py'
```

The JavaScript tests use jsdom; the KeePassXC tests run against a mock of the KeePassXC browser protocol and an isolated qutebrowser instance, never a real database.

## Credits

`qute-keepassxc` by Markus Blöchl (MIT) provides the KeePassXC protocol client these scripts build on. Written with the help of Claude Code.
