"""A minimal mock of KeePassXC's browser-integration Unix-socket protocol.

Implements just enough of the real NaCl-box protocol (see qute-keepassxc)
for deterministic, offline tests of keepassxc-newpass's save-then-fill flow:
change-public-keys, test-associate, associate, get-logins and set-login
(create AND update-by-uuid - see keepassxc-newpass's `_set_login()`
docstring for where the `uuid` field name was confirmed). It never touches
the user's real KeePassXC (a caller-supplied socket path under a temp dir
is used, and every test asserts that path is not the real one).

Deliberately NOT used for the *generator* tests (those exercise the real
`keepassxc-cli` and the real Python `secrets` fallback) - only for the
save/associate/fill flow, where a real KeePassXC GUI would need manual
confirmation-dialog interaction to run unattended and repeatably (see
test_real_keepassxc.py for a from-first-principles real, isolated instance
that *does* drive those dialogs, kept separate because it is inherently
slower/more fragile than this mock).

get-logins matching
--------------------
This mock's `get-logins` matches entries the same way KeePassXC 2.7.12
itself does by default, read directly from its actual source
(src/browser/BrowserService.cpp's `handleURL()`, fetched from
https://github.com/keepassxreboot/keepassxc at tag 2.7.12 - see
kpxc_lookup.py's module docstring for the full citation and what was
verified this way):

  - host-only: an entry's URL's path/query is never part of whether it
    matches, only scheme+host+port.
  - base-domain-then-suffix: the query URL's registrable ("base") domain
    must equal the entry URL's registrable domain, and then the query
    host must *end with* the entry host (so an entry saved at a broader
    host, e.g. "example.com", matches any subdomain of it, but not the
    reverse, and not a same-registrable-domain sibling like
    "accounts.example.com" while browsing "www.example.com").
  - scheme-insensitive by default (`matchUrlScheme` is off unless a test
    explicitly passes `match_url_scheme=True` to the constructor).
  - port-sensitive only when the *entry* URL specifies an explicit port.
  - an entry with no URL at all never matches anything.

The registrable-domain computation below is deliberately its OWN small
implementation (not imported from kpxc_lookup.registrable_domain) so this
mock verifies the userscript's cascade independently rather than the
cascade's own domain math checking itself.
"""
import base64
import itertools
import json
import os
import socket
import threading
import uuid as uuid_module
from urllib.parse import urlsplit

import nacl.public
import nacl.utils

# Same minimum multi-part-suffix list the task specified for the cascade's
# own registrable-domain fallback (kept independent here on purpose).
_MULTI_PART_SUFFIXES = frozenset({
    "co.uk", "com.br", "com.pt", "org.br", "net.br", "gov.br",
    "com.au", "co.jp", "gov.it", "edu.it",
})


def _base_domain(host):
    host = (host or "").lower()
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last_two = ".".join(labels[-2:])
    if last_two in _MULTI_PART_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return last_two


def _entry_matches(entry_url, site_url, match_url_scheme=False):
    """Port of KeePassXC's BrowserService::handleURL() for this mock."""
    if not entry_url:
        return False
    entry = urlsplit(entry_url if "://" in entry_url else "https://" + entry_url)
    site = urlsplit(site_url if "://" in site_url else "https://" + site_url)
    entry_host = (entry.hostname or "").lower()
    site_host = (site.hostname or "").lower()
    if not entry_host:
        return False
    if entry.port and entry.port != site.port:
        return False
    if match_url_scheme and entry.scheme and site.scheme and entry.scheme != site.scheme:
        return False
    if _base_domain(site_host) != _base_domain(entry_host):
        return False
    return site_host.endswith(entry_host)


class MockKeepassXCServer:
    """Runs in a background thread; one connection at a time, like the real thing."""

    def __init__(self, socket_path, *, known_association_id=None, known_association_key=None,
                 deny_set_login=False, require_new_association_name=None, seed_entries=None,
                 match_url_scheme=False):
        self.socket_path = socket_path
        self.match_url_scheme = match_url_scheme
        self.server_key = nacl.public.PrivateKey.generate()
        # id -> id_key public key bytes, pre-seeded to simulate "already associated"
        self.associations = {}
        if known_association_id and known_association_key is not None:
            self.associations[known_association_id] = known_association_key
        self.deny_set_login = deny_set_login
        # if set, associate() only succeeds when the client offers this name
        # (unused hook, kept simple: our client never sends a name - KeePassXC
        # itself assigns/asks for one via GUI. We accept any associate().)
        self.saved_logins = []  # every set-login call: dict with url/login/password/group/uuid/is_update
        # current entries, keyed by uuid - what get-logins(url) matches against.
        # seed_entries: list of {uuid?, login, password, url, name?}
        self.entries = {}
        for e in (seed_entries or []):
            entry_uuid = e.get("uuid") or uuid_module.uuid4().hex
            self.entries[entry_uuid] = {**e, "uuid": entry_uuid}
        # uuid -> current TOTP value for get-totp (used by keepassxc-login's
        # --totp tests); no entry here == "no TOTP key found", same as real
        # KeePassXC responding with an empty/absent totp.
        self.totp_by_uuid = {}
        self._uuid_counter = itertools.count(1)
        self._sock = None
        self._thread = None
        self._stop = False

    def start(self):
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.socket_path)
        self._sock.listen(1)
        self._sock.settimeout(0.5)
        self._thread = threading.Thread(target=self._serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        if self._thread:
            self._thread.join(timeout=2)
        if self._sock:
            self._sock.close()
        try:
            os.remove(self.socket_path)
        except OSError:
            pass

    def _serve_forever(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                self._handle_connection(conn)
            except (OSError, ConnectionError, json.JSONDecodeError):
                pass
            finally:
                conn.close()

    def _handle_connection(self, conn):
        box = None
        client_id = None
        while not self._stop:
            conn.settimeout(2.0)
            try:
                raw = conn.recv(65536)
            except socket.timeout:
                return
            if not raw:
                return
            req = json.loads(raw.decode("utf-8"))
            action = req["action"]
            client_id = req.get("clientID", client_id)

            if action == "change-public-keys":
                client_pub = nacl.public.PublicKey(base64.b64decode(req["publicKey"]))
                box = nacl.public.Box(self.server_key, client_pub)
                resp = {
                    "action": "change-public-keys",
                    "publicKey": base64.b64encode(self.server_key.public_key.encode()).decode(),
                    "nonce": base64.b64encode(nacl.utils.random(nacl.public.Box.NONCE_SIZE)).decode(),
                    "success": "true",
                }
                conn.send(json.dumps(resp).encode())
                continue

            # Everything else is encrypted the same way qute-keepassxc sends it.
            nonce = base64.b64decode(req["nonce"])
            plaintext = box.decrypt(base64.b64decode(req["message"]), nonce)
            inner = json.loads(plaintext.decode("utf-8"))

            reply_nonce = nacl.utils.random(nacl.public.Box.NONCE_SIZE)

            if inner["action"] == "test-associate":
                assoc_id = inner.get("id")
                known = self.associations.get(assoc_id)
                success = known is not None and known == base64.b64decode(inner["key"])
                payload = {"action": "test-associate", "success": "true" if success else "false",
                           "id": assoc_id, "version": "2.7.12-mock"}
            elif inner["action"] == "associate":
                new_id = "mock-association-{}".format(len(self.associations) + 1)
                self.associations[new_id] = base64.b64decode(inner["idKey"])
                payload = {"action": "associate", "success": "true", "id": new_id, "version": "2.7.12-mock"}
            elif inner["action"] == "set-login":
                if self.deny_set_login:
                    outer_error = {
                        "action": "set-login",
                        "error": "Action cancelled or denied" if self.deny_set_login == "denied" else "locked",
                        "errorCode": 6 if self.deny_set_login == "denied" else 1,
                    }
                    conn.send(json.dumps(outer_error).encode())
                    continue
                req_uuid = inner.get("uuid")
                is_update = bool(req_uuid) and req_uuid in self.entries
                entry_uuid = req_uuid if is_update else "mock-entry-{}".format(next(self._uuid_counter))
                self.entries[entry_uuid] = {
                    "uuid": entry_uuid,
                    "login": inner.get("login"),
                    "password": inner.get("password"),
                    "url": inner.get("url"),
                    "name": inner.get("url"),
                    "group": inner.get("group"),
                }
                self.saved_logins.append({
                    "url": inner.get("url"),
                    "submitUrl": inner.get("submitUrl"),
                    "login": inner.get("login"),
                    "password": inner.get("password"),
                    "group": inner.get("group"),
                    "id": inner.get("id"),
                    "uuid": req_uuid,
                    "is_update": is_update,
                    "entry_uuid": entry_uuid,
                })
                # matches the real response observed against KeePassXC 2.7.12:
                # {"count":0,"entries":0,"error":"success","hash":"...",
                #  "nonce":"...","success":"true","version":"2.7.12"} - the
                # official keepassxc-browser extension's background/keepass.js
                # (keepass.updateCredentials) treats error=='success' (or ''
                # on old KeePassXC) as success for *both* create and update;
                # this mock always uses 'success' for both, same as real 2.7.12.
                payload = {
                    "count": 0, "entries": 0, "error": "success",
                    "hash": "0" * 64, "success": "true", "version": "2.7.12-mock",
                }
            elif inner["action"] == "get-totp":
                totp = self.totp_by_uuid.get(inner.get("uuid"))
                payload = {"action": "get-totp", "success": "true" if totp else "false", "totp": totp or ""}
            elif inner["action"] == "get-logins":
                url = inner.get("url")
                # Real KeePassXC's get-logins entries include the password
                # too (qute-keepassxc's own get_logins()/select_account()
                # reads cred['login'] and cred['password']) - keepassxc-login
                # fills straight from this, unlike keepassxc-newpass which
                # only ever used get-logins for uuid/login dedup.
                matched = [
                    {
                        "login": e["login"], "password": e.get("password"),
                        "name": e.get("name") or e["login"], "uuid": e["uuid"], "url": e.get("url"),
                    }
                    for e in self.entries.values()
                    if _entry_matches(e.get("url"), url, match_url_scheme=self.match_url_scheme)
                ]
                payload = {"action": "get-logins", "count": len(matched), "entries": matched, "success": "true"}
            else:
                payload = {"action": inner["action"], "success": "false", "error": "unsupported action in mock"}

            enc = box.encrypt(json.dumps(payload).encode("utf-8"), reply_nonce)
            resp = {
                "action": inner["action"],
                "message": base64.b64encode(enc.ciphertext).decode(),
                "nonce": base64.b64encode(reply_nonce).decode(),
                "clientID": client_id,
            }
            conn.send(json.dumps(resp).encode())
