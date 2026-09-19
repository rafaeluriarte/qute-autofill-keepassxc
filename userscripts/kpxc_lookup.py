"""Shared helpers for the KeePassXC qutebrowser userscripts.

Used by `keepassxc-login` (the domain-widening replacement for the
third-party `qute-keepassxc`'s `pw` binding) and by `keepassxc-newpass`
(which is refactored to reuse the bits it used to duplicate).

This module is stdlib-only except for what its callers already require
(`pynacl`, transitively, via qute-keepassxc). It never touches the network
and never prints/logs credential values.

Contents
--------
  - load_qute_keepassxc()          import the sibling qute-keepassxc script
                                    (no .py suffix) as a module, unmodified -
                                    same technique keepassxc-newpass already
                                    used, moved here so both scripts share it.
  - qb_quote / fifo_send /
    message_info / message_error / _dump_one_line
                                    qutebrowser FIFO plumbing, moved here
                                    from keepassxc-newpass verbatim so
                                    keepassxc-login can reuse it too.
  - Alias storage (aliases_path, load_aliases, get_alias, save_alias,
    forget_alias)
                                    the "remembered site for this host"
                                    mapping backing cascade step (a) and
                                    keepassxc-login's --forget.
  - The lookup cascade (url_cascade_candidates, term_cascade_candidates,
    build_cascade, lookup)
                                    see the "Lookup cascade" section below.

Lookup cascade
--------------
qute-keepassxc's `get-logins` call passes KeePassXC the *exact* page URL
and nothing else. KeePassXC's own matching (read from its actual C++
source for the installed version, 2.7.12 - see
`src/browser/BrowserService.cpp`'s `handleURL()`/`shouldIncludeEntry()`/
`searchEntries()`, and `src/core/UrlTools.cpp`'s `getBaseDomainFromUrl()`;
fetched from https://github.com/keepassxreboot/keepassxc at tag 2.7.12)
is, by default (`[Browser]` in keepassxc.ini has only `Enabled=true` -
`MatchUrlScheme`/`BestMatchOnly` are unset, i.e. at their defaults):

  - host-only: the entry's URL's *path/query is never part of the match*,
    only scheme+host+port feed the boolean allow/reject decision (path is
    only used afterwards to *rank* already-matching entries).
  - scheme-insensitive: `matchUrlScheme` defaults to false, so http vs
    https on either side does not by itself reject a match (this cascade
    still tries both schemes explicitly, both because a user might enable
    that setting later and because it is cheap).
  - port-sensitive only when the *entry* URL specifies an explicit port.
  - base-domain-then-suffix: `handleURL()` first rejects unless
    `getBaseDomainFromUrl(site.host) == getBaseDomainFromUrl(entry.host)`
    (registrable/public-suffix-aware - `UrlTools::getBaseDomainFromUrl`
    uses Qt's `QNetworkCookie` public-suffix machinery), THEN requires
    `site.host.endsWith(entry.host)`. This means an entry saved at a
    *broader* host (e.g. `example.com`) matches any subdomain of it
    automatically (`shop.example.com` ends with `example.com`) - but an
    entry saved at a *narrower*/sibling host (e.g. `accounts.example.com`
    or `www.example.com`) is never found while browsing `example.com` or a
    different subdomain, since `endsWith()` only goes one direction. There
    is also no cross-domain fallback of any kind (SSO on a different
    registrable domain is never found via get-logins on the original
    domain - `handleURL()` rejects on the base-domain check first).
  - `BrowserService::searchEntries(siteUrl, formUrl, keyList, ...)` (the
    entry point actually used for `get-logins`) *looks* like it retries
    with the hostname's first label stripped
    (`do { ... } while (entries.isEmpty() && removeFirstDomain(hostname))`)
    but the loop body calls `searchEntries(db, siteUrl, formUrl, ...)`
    with the *original*, unmodified `siteUrl` every iteration - `hostname`
    is computed and shortened but never actually substituted back into the
    query. Read literally, this retry loop is dead code: every iteration
    performs an identical search. Confirmed by reading the fetched source
    directly (not summarized) at that pinned tag. Practically: KeePassXC
    does not widen the search domain-by-domain on its own, which is
    exactly the gap this cascade's step (e) below fills from the client
    side instead.
  - an entry with no URL at all can never be returned by ANY `get-logins`
    query (`handleURL()`'s first line is `if (entryUrl.isEmpty()) return
    false;`) - no cascade step can find it; there is no
    `get-database-entries`/list-all/search-by-title action in this
    KeePassXC build (confirmed absent from `/usr/bin/keepassxc`'s strings,
    matching what was already established before this module was written).

The keepassxc-browser browser extension installed locally
(~/.var/app/com.google.Chrome/config/google-chrome/Default/Extensions/
oboonakemofpalcgghocfoadofidjkkk/*/background/{keepass,page}.js,
`keepass.retrieveCredentials`/`page.retrieveCredentials`) does no
client-side host filtering of its own - it just forwards the page's
`url` (and, if available, `submitUrl`) to `get-logins` verbatim and
returns whatever KeePassXC sends back; all matching happens on the
KeePassXC application side, confirming there is nothing to learn from the
extension beyond "what wire fields get sent" (already known from
qute-keepassxc).

An attempt was made to confirm this empirically against a real, isolated
KeePassXC 2.7.12 instance (separate HOME/XDG_*_HOME, its own throwaway
database, `--config`, `--pw-stdin`, under Xvfb) rather than relying on
reading the source alone. It could not be completed safely: KeePassXC
enforces a single-instance lock that is independent of HOME/XDG_*_HOME
(the isolated process exited immediately, forwarding its file-open request
over IPC to the user's *real*, already-running KeePassXC instance instead
of starting its own). Continuing down that path risked interacting with
the user's real KeePassXC, which the task explicitly forbids, so it was
abandoned in favour of the source-reading above - no data was read from or
written to the user's real database, and the real KeePassXC process was
not touched (only two of this session's own throwaway processes - an Xvfb
server and a helper `sleep` - were ever started or killed).

Given host-only matching, cascade steps (b) "the full page URL" and (c)
"the origin" collapse to the same query whenever the page URL has no
extra path/query/fragment (a bare "https://example.com" load); this
module skips emitting a redundant duplicate for that case (see
`url_cascade_candidates()`) while still emitting both, in order, whenever
they would genuinely differ.

Step (e) - trying each parent domain down to the registrable domain, each
as both the bare domain and its `www.` subdomain - is the step that finds
an entry saved at a *narrower* host than (or a host unrelated-by-suffix
to) the current page's host, most commonly the classic "current page is
`app.example.com`, the login entry was saved at `www.example.com` (or
bare `example.com`)" case. By itself it cannot find an entry saved under
an arbitrary *sibling* subdomain picked by the site itself (e.g.
`accounts.example.com`, `login.example.com`) - there is no way to
enumerate the site's actual choice without a list-all-entries action,
which this KeePassXC build does not expose.

Step (f) narrows that last gap for the common case: it tries the
registrable domain with each of LOGIN_SUBDOMAIN_PREFIXES (accounts,
login, auth, sso, ...) as a guessed subdomain, e.g.
`https://accounts.<registrable-domain>/`, after everything else and
before giving up. It's a curated guess, not a search - a genuinely
unusual login subdomain (or a login on an entirely different registrable
domain, e.g. a third-party SSO provider) still needs the alias mechanism
(step a / `keepassxc-login`'s "Search which site?" prompt), which exists
specifically to let the user teach the cascade that association once.
When step (f) itself finds the match, keepassxc-login remembers it as
that host's alias too - see `is_login_subdomain_guess()` - so the guess
only ever has to work once per host.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import ipaddress
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

SCRIPT_DIR = Path(__file__).resolve().parent
def _find_qute_keepassxc() -> Path:
    """qute-keepassxc (third party, not shipped with this repo): next to these
    scripts, else in the standard qutebrowser userscripts dir, else $QUTE_KEEPASSXC."""
    env = os.environ.get("QUTE_KEEPASSXC")
    if env:
        return Path(env).expanduser()
    local = SCRIPT_DIR / "qute-keepassxc"
    if local.exists():
        return local
    return Path.home() / ".local/share/qutebrowser/userscripts/qute-keepassxc"


QUTE_KEEPASSXC_PATH = _find_qute_keepassxc()

ALIASES_DIRNAME = "keepassxc-login"


# ---------------------------------------------------------------------------
# qutebrowser <-> userscript plumbing (FIFO commands)
#
# Moved here, unmodified in behaviour, from keepassxc-newpass so
# keepassxc-login can reuse the exact same quoting/embedding logic instead
# of duplicating it.
# ---------------------------------------------------------------------------

def qb_quote(text: str) -> str:
    """Quote a string as a single argument for qutebrowser's FIFO command parser."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped + '"'


def fifo_send(*lines: str) -> None:
    fifo_path = os.environ.get("QUTE_FIFO")
    text = "".join(line + "\n" for line in lines)
    if not fifo_path:
        sys.stderr.write(text)
        return
    with open(fifo_path, "a", encoding="utf-8") as fifo:
        fifo.write(text)


def message_info(text: str) -> None:
    fifo_send("message-info " + qb_quote(text))


def message_error(text: str) -> None:
    print(text, file=sys.stderr)
    fifo_send("message-error " + qb_quote(text))


def _dump_one_line(obj) -> str:
    """JSON-encode `obj` for direct embedding in a single jseval FIFO line.

    json.dumps() always backslash-escapes control characters (including
    newlines), so the result can never contain a raw newline, which matters
    because qutebrowser's FIFO reader executes one command per physical
    line. \\u2028/\\u2029 are escaped defensively too.
    """
    text = json.dumps(obj, ensure_ascii=False)
    text = text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    if "\n" in text or "\r" in text:
        raise RuntimeError("internal error: JSON payload contained a raw newline")
    return text


# ---------------------------------------------------------------------------
# Reusing qute-keepassxc (unmodified) as a module
# ---------------------------------------------------------------------------

def load_qute_keepassxc(path: Path = QUTE_KEEPASSXC_PATH):
    """Import the sibling qute-keepassxc script (no .py suffix) as a module.

    qute-keepassxc is never modified by these scripts; only its
    `KeepassXC` protocol client, `SecretKeyStore` and
    `connect_to_keepassxc()` are reused, so an existing association (and
    its GPG-encrypted key file) is shared between `pw`/`pn`/`pT` and the
    new `keepassxc-login`.
    """
    loader = importlib.machinery.SourceFileLoader("_qute_keepassxc_reused", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Alias storage: ~/.local/share/qutebrowser/keepassxc-login/aliases.json
# {host: term}, no secrets. dir 700, file 600.
# ---------------------------------------------------------------------------

def aliases_dir() -> Path:
    """Directory holding aliases.json.

    QUTE_KEEPASSXC_ALIASES_DIR overrides it (tests point this at a temp
    dir so the real file is never touched); default matches the path
    specified for this feature.
    """
    override = os.environ.get("QUTE_KEEPASSXC_ALIASES_DIR")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~/.local/share/qutebrowser")) / ALIASES_DIRNAME


def aliases_path() -> Path:
    return aliases_dir() / "aliases.json"


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def load_aliases() -> dict:
    """{host: term}, or {} on any error (missing file, bad JSON, ...)."""
    path = aliases_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_aliases(data: dict) -> None:
    directory = aliases_dir()
    _ensure_private_dir(directory)
    path = aliases_path()
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def get_alias(host: str):
    return load_aliases().get(host)


def save_alias(host: str, term: str) -> None:
    data = load_aliases()
    data[host] = term
    _write_aliases(data)


def forget_alias(host: str) -> bool:
    """Remove the alias for `host`; returns whether one existed."""
    data = load_aliases()
    if host not in data:
        return False
    del data[host]
    _write_aliases(data)
    return True


# ---------------------------------------------------------------------------
# Registrable-domain handling (public suffix aware)
# ---------------------------------------------------------------------------

# "handle common multi-part public suffixes ... at least" - the task's
# required minimum, plus a modest set of other very common two-label
# suffixes so the built-in fallback is useful beyond the minimum list too.
_MULTI_PART_SUFFIXES = frozenset({
    "co.uk", "com.br", "com.pt", "org.br", "net.br", "gov.br",
    "com.au", "co.jp", "gov.it", "edu.it",
    "org.uk", "ac.uk", "gov.uk", "co.nz", "co.za", "co.in",
    "com.mx", "com.ar", "co.kr", "or.jp", "ne.jp",
})


def _is_ip_or_unqualified(host: str) -> bool:
    """True for IPv4/IPv6 literals and single-label hosts (e.g. localhost).

    Matches getBaseDomainFromUrl()'s own `isIpAddress(host)` early-return,
    and (for single-label hosts) the fact that there is no parent domain
    to descend to anyway.
    """
    if not host:
        return True
    stripped = host.strip("[]")
    try:
        ipaddress.ip_address(stripped)
        return True
    except ValueError:
        pass
    return "." not in host


def _builtin_registrable_domain(host: str) -> str:
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last_two = ".".join(labels[-2:])
    if last_two in _MULTI_PART_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return last_two


def registrable_domain(host: str) -> str:
    """Best-effort registrable ("base") domain for `host`.

    Uses `tldextract` or `publicsuffix2` if (and only if) already
    installed, else the built-in multi-part-suffix list, per the task's
    instructions. IP literals and single-label hosts are returned as-is.
    """
    host = (host or "").lower()
    if _is_ip_or_unqualified(host):
        return host
    try:
        import tldextract  # type: ignore
        ext = tldextract.extract(host)
        if ext.suffix and ext.domain:
            return f"{ext.domain}.{ext.suffix}"
        return host
    except ImportError:
        pass
    try:
        import publicsuffix2  # type: ignore
        sld = publicsuffix2.get_sld(host)
        if sld:
            return sld
    except ImportError:
        pass
    return _builtin_registrable_domain(host)


def domain_ladder(host: str) -> list[str]:
    """Parent domains from just above `host` down to (and including) the
    registrable domain, e.g. "checkout.shop.example.co.uk" ->
    ["shop.example.co.uk", "example.co.uk"]. Always includes the
    registrable domain itself (even when host == registrable domain, so
    its "www." variant still gets tried by the caller). Empty for
    IP/single-label hosts.
    """
    host = (host or "").lower()
    if _is_ip_or_unqualified(host):
        return []
    reg = registrable_domain(host)
    labels = host.split(".")
    reg_labels = reg.split(".")
    result = []
    cur = labels
    while len(cur) > len(reg_labels):
        cur = cur[1:]
        result.append(".".join(cur))
    if reg not in result:
        result.append(reg)
    return result


# ---------------------------------------------------------------------------
# The lookup cascade
# ---------------------------------------------------------------------------

MAX_CANDIDATES = 48  # "keep the total bounded"

# Step (f), the last-resort automatic guess: KeePassXC's own matching is
# broader-entry-matches-narrower-site only (see module docstring), so an
# entry saved on a *sibling* login subdomain the site itself chose (most
# commonly "accounts.<domain>") is structurally invisible while browsing
# "www.<domain>" or the bare domain - the user shouldn't have to guess
# that subdomain by hand. Tried against the registrable domain only (not
# every domain_ladder() level), as https://<prefix>.<registrable-domain>/,
# after everything else in the cascade and before giving up. A plain
# module-level constant so it's easy to edit/extend.
LOGIN_SUBDOMAIN_PREFIXES = [
    "accounts", "account", "login", "signin", "auth", "id", "sso",
    "secure", "my", "www", "passport", "identity", "idp", "portal",
]


def _origin(scheme: str, host: str, port) -> str:
    port_part = f":{port}" if port else ""
    return f"{scheme}://{host}{port_part}/"


def _dedup(items: list[str]) -> list[str]:
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _base_cascade_candidates(url: str) -> tuple[list[str], str]:
    """Steps b/c/d/e only (no step-f prefix guesses) for a real URL, plus
    the host they were built from (used by is_login_subdomain_guess() to
    tell a genuine step-f-only match apart from a coincidental string
    match with something steps b-e already covered).
    """
    parsed = urlsplit(url)
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()
    if not host:
        return [], host
    port = parsed.port
    out = []
    has_extra = bool((parsed.path not in ("", "/")) or parsed.query or parsed.fragment)
    if has_extra:
        out.append(url)
    out.append(_origin(scheme, host, port))
    other_scheme = "http" if scheme == "https" else "https"
    out.append(_origin(other_scheme, host, port))
    for dom in domain_ladder(host):
        out.append(f"https://{dom}/")
        out.append(f"https://www.{dom}/")
    return _dedup(out), host


def login_subdomain_guess_candidates(host: str) -> list[str]:
    """Step (f): https://<prefix>.<registrable-domain>/ for each prefix in
    LOGIN_SUBDOMAIN_PREFIXES. Empty for IP/single-label hosts (no
    registrable domain to guess subdomains of).
    """
    host = (host or "").lower()
    if _is_ip_or_unqualified(host):
        return []
    reg = registrable_domain(host)
    return [f"https://{prefix}.{reg}/" for prefix in LOGIN_SUBDOMAIN_PREFIXES]


def url_cascade_candidates(url: str) -> list[str]:
    """Steps b through f for a real (scheme-qualified) URL, in cascade
    order.

    b: the exact URL (only emitted when it carries something an origin
       doesn't - see module docstring), c: the origin, d: the origin with
       the other scheme, e: each parent domain down to the registrable
       domain (bare and www, https only), f: the registrable domain with
       each of LOGIN_SUBDOMAIN_PREFIXES, https only.
    """
    base, host = _base_cascade_candidates(url)
    return _dedup(base + login_subdomain_guess_candidates(host))


def is_login_subdomain_guess(matched_url: str, url: str) -> bool:
    """True if `matched_url` (as returned by lookup() against `url`) could
    only have been found through step (f)'s prefix guessing - i.e. it
    isn't also reachable via steps (b)-(e) for this same URL. Used to
    decide whether to say "matched <host>" and remember the alias
    automatically.
    """
    if not matched_url:
        return False
    base, host = _base_cascade_candidates(url)
    if matched_url in base:
        return False
    return matched_url in login_subdomain_guess_candidates(host)


def term_cascade_candidates(term: str) -> list[str]:
    """Same cascade (including step (f)'s prefix guesses) starting from a
    typed/remembered term instead of the real page URL - treated as a URL
    if it has a scheme, else as a bare domain (optionally with a path).
    """
    term = (term or "").strip()
    if not term:
        return []
    if "://" in term:
        return url_cascade_candidates(term)
    return url_cascade_candidates("https://" + term)


def build_cascade(page_url: str, alias_term: str | None = None) -> list[str]:
    """The full ordered, deduplicated, bounded candidate-URL list.

    (a) is `alias_term`'s own sub-cascade (tried first, in full, before
    falling back to the real page URL's own b-f) - this is what makes a
    remembered alias behave as "look this host up as if it were
    <alias_term>".
    """
    seen: list[str] = []

    def add_all(cands):
        for c in cands:
            if c not in seen:
                seen.append(c)

    if alias_term:
        add_all(term_cascade_candidates(alias_term))
    add_all(url_cascade_candidates(page_url))
    return seen[:MAX_CANDIDATES]


def lookup(kp, page_url: str, alias_term: str | None = None):
    """Run the cascade against an already-connected `kp`
    (qute-keepassxc.KeepassXC instance); return (matched_url, entries) for
    the first non-empty result, or (None, []) if nothing matched anywhere
    in the (bounded) cascade. Never logs a URL's entries or any secret.
    """
    for candidate in build_cascade(page_url, alias_term):
        try:
            entries = kp.get_logins(candidate) or []
        except Exception:
            entries = []
        if entries:
            return candidate, entries
    return None, []
