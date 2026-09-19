/*
 * autofill.js -- field detection/classification/fill logic for the qutebrowser
 * `autofill` userscript.
 *
 * This file is loaded two ways:
 *   1. In the browser, the Python userscript reads this file's text, appends a
 *      short invocation snippet, and sends it to qutebrowser with
 *      `:jseval --world main` so it runs as a plain classic script in the
 *      page's own JS world (same world as the site's own scripts, so
 *      framework reactivity picks up our changes; see setNativeValue below).
 *   2. In Node (with jsdom) for unit tests, via require('./autofill.js').
 *      When required, `window`/`document` are undefined at load time, so the
 *      module only registers itself on `module.exports`; tests build a DOM
 *      with jsdom and pass `doc`/`win` explicitly to QuteAutofill.run().
 *
 * Precedence for classifying a field into a semantic "kind" (documented in
 * detail above classify()):
 *   1. HTML `autocomplete` attribute tokens (highest priority - explicit
 *      author intent).
 *   2. `type` attribute, but only for the unambiguous cases (email, tel).
 *   3. Multilingual keyword matching over name/id/placeholder/aria-label/
 *      label text/nearby text, itself split into two tiers: TIER1 = specific
 *      multi-word or unambiguous phrases (e.g. "nome completo", "numero
 *      civico", "cognome"), TIER2 = short, ambiguous, fallback-only single
 *      words (e.g. bare "nome", bare "numero"). TIER1 is scanned in full
 *      before TIER2 is ever consulted, so a specific phrase always wins over
 *      a generic one anywhere on the page, not just within one field.
 */
(function (root, factory) {
  var mod = factory();
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = mod;
  }
  if (typeof window !== 'undefined') {
    window.QuteAutofill = mod;
  } else if (root) {
    root.QuteAutofill = mod;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  // ---------------------------------------------------------------------
  // Text normalization
  // ---------------------------------------------------------------------

  function stripDiacritics(s) {
    return s.normalize ? s.normalize('NFD').replace(/[̀-ͯ]/g, '') : s;
  }

  function normalize(s) {
    if (s === null || s === undefined) return '';
    s = stripDiacritics(String(s));
    s = s.toLowerCase();
    s = s.replace(/[_\-.]/g, ' ');
    s = s.replace(/\s+/g, ' ').trim();
    return s;
  }

  function matchesAny(haystack, patterns) {
    for (var i = 0; i < patterns.length; i++) {
      if (patterns[i].test(haystack)) return true;
    }
    return false;
  }

  // ---------------------------------------------------------------------
  // Page locale detection (drives apelido PT-vs-BR meaning, date format,
  // postal code shape default)
  // ---------------------------------------------------------------------

  function detectLocale(doc) {
    var htmlLang = '';
    try {
      htmlLang = ((doc.documentElement && doc.documentElement.getAttribute('lang')) || '').toLowerCase();
    } catch (e) { /* ignore */ }

    var bodyText = '';
    try { bodyText = normalize(doc.body ? doc.body.textContent : ''); } catch (e) { /* ignore */ }

    var hasBR = /\bcpf\b/.test(bodyText) || /\bcep\b/.test(bodyText) ||
      /\bbairro\b/.test(bodyText) || /\blogradouro\b/.test(bodyText);
    var hasPT = /\bnif\b/.test(bodyText) || /\bmorada\b/.test(bodyText) ||
      /telemovel/.test(bodyText) || /\bconcelho\b/.test(bodyText) || /\bdistrito\b/.test(bodyText);
    var hasIT = /codice fiscale/.test(bodyText) || /partita iva/.test(bodyText) ||
      /\bcomune\b/.test(bodyText) || /\bcap\b/.test(bodyText);

    if (htmlLang.indexOf('it') === 0) return 'it';
    if (htmlLang.indexOf('pt-br') === 0 || htmlLang.indexOf('pt_br') === 0) return 'pt-br';
    if (htmlLang.indexOf('pt') === 0) return (hasBR && !hasPT) ? 'pt-br' : 'pt-pt';
    if (htmlLang.indexOf('en-us') === 0) return 'en-us';
    if (htmlLang.indexOf('en-gb') === 0) return 'en-gb';
    if (htmlLang.indexOf('en') === 0) {
      if (hasBR) return 'pt-br';
      if (hasPT) return 'pt-pt';
      if (hasIT) return 'it';
      return 'en';
    }

    if (hasBR) return 'pt-br';
    if (hasPT) return 'pt-pt';
    if (hasIT) return 'it';
    return 'generic';
  }

  // ---------------------------------------------------------------------
  // Classification tables
  // ---------------------------------------------------------------------

  // autocomplete tokens we must NEVER fill (passwords, payment card data,
  // one-time codes) even if a later heuristic would otherwise match.
  var SKIP_AUTOCOMPLETE = {
    'current-password': 1, 'new-password': 1,
    'cc-name': 1, 'cc-given-name': 1, 'cc-additional-name': 1, 'cc-family-name': 1,
    'cc-number': 1, 'cc-exp': 1, 'cc-exp-month': 1, 'cc-exp-year': 1, 'cc-csc': 1, 'cc-type': 1,
    'one-time-code': 1, 'transaction-amount': 1, 'transaction-currency': 1,
  };

  // keyword-based skip: search boxes, captcha, card/cvv, coupon/promo codes.
  var SKIP_KEYWORDS = [
    /\bsearch\b/, /\bcerca\b/, /\bricerca\b/, /\bpesquisa\b/, /\bbuscar\b/, /\bprocurar\b/,
    /captcha/,
    /card ?number/, /numero della carta/, /numero do cartao/, /numero de la tarjeta/,
    /\bcvv\b/, /\bcvc\b/, /codice di sicurezza/, /codigo de seguranca/,
    /titular do cartao/, /intestatario/, /cardholder/,
    /one[- ]?time[- ]?code/, /\botp\b/, /codice otp/,
    /\bcoupon\b/, /promo ?code/, /codice sconto/, /\bcupom\b/,
  ];

  // WHATWG autocomplete tokens -> our semantic "kind". Checked first, wins
  // over everything else per the precedence rules above.
  var AUTOCOMPLETE_MAP = {
    'given-name': 'given_name',
    'additional-name': null,
    'family-name': 'family_name',
    'name': 'full_name',
    'honorific-prefix': null,
    'email': 'email',
    'tel': 'phone',
    'tel-national': 'phone_national',
    'organization': 'organization',
    'street-address': 'street_full',
    'address-line1': 'street',
    'address-line2': 'address_line2',
    'address-line3': 'neighborhood',
    'address-level1': 'province',
    'address-level2': 'city',
    'address-level3': 'neighborhood',
    'postal-code': 'postal_code',
    'country': 'country_code',
    'country-name': 'country',
    'bday': 'birth_date',
    'bday-day': 'birth_day',
    'bday-month': 'birth_month',
    'bday-year': 'birth_year',
  };

  // `type` attribute fallback -- deliberately narrow: only types that are
  // unambiguous regardless of language/label.
  var TYPE_MAP = { email: 'email', tel: 'phone' };

  // TIER1: specific / unambiguous phrases. Scanned in full before TIER2.
  var TIER1 = [
    { kind: 'full_name', patterns: [/nome e cognome/, /nome completo/, /\bfull ?name\b/] },
    { kind: 'given_name', patterns: [/first ?name/, /given ?name/, /nome pr[oó]prio/] },
    { kind: 'family_name', patterns: [/last ?name/, /\bsurname\b/, /\bcognome\b/, /\bsobrenome\b/] },
    // PT-PT "apelido" = surname; PT-BR "apelido" = nickname (unmapped).
    { kind: function (locale) { return locale === 'pt-br' ? null : 'family_name'; }, patterns: [/\bapelidos?\b/] },
    { kind: 'email', patterns: [/posta elettronica/, /correio eletr[oó]nico/, /e[- ]?mail/] },
    {
      kind: 'phone',
      patterns: [
        /numero di telefono/, /n[uú]mero de telefone/, /telefone m[oó]vel/, /telem[oó]vel/,
        /\bcelular\b/, /\bcellulare\b/, /\bmobile\b/, /phone ?number/, /\btelefono\b/, /\btelefone\b/,
        /\bphone\b/,
      ],
    },
    { kind: 'organization', patterns: [/ragione sociale/, /\bazienda\b/, /\bcompany\b/, /\bempresa\b/, /organi[sz]ation/] },
    {
      kind: 'house_number',
      patterns: [/numero civico/, /\bcivico\b/, /n[uú]mero da casa/, /house ?number/, /street ?number/, /n[uú]mero do im[oó]vel/],
    },
    {
      kind: 'address_line2',
      patterns: [/complemento/, /apartment/, /\bapt\b/, /\bsuite\b/, /\bandar\b/, /\bbloco\b/, /address ?line ?2/],
    },
    { kind: 'neighborhood', patterns: [/\bbairro\b/, /neighbo(u)?rhood/] },
    // PT single-line full postal address.
    { kind: 'street_full', patterns: [/\bmorada\b/] },
    {
      kind: 'street',
      patterns: [/indirizzo di spedizione/, /indirizzo di fatturazione/, /\bindirizzo\b/, /\blogradouro\b/, /endere[cç]o/, /street ?address/],
    },
    { kind: 'postal_code', patterns: [/codice postale/, /c[oó]digo postal/, /\bcep\b/, /\bcap\b/, /postal ?code/, /zip ?code/, /\bzip\b/] },
    {
      kind: 'city',
      patterns: [/\bcitt[aà]\b/, /\bcomune\b/, /localit[aà]\b/, /localidade/, /\bconcelho\b/, /munic[ií]pio/, /\bcidade\b/, /city\/town/, /\btown\b/, /\bcity\b/],
    },
    {
      kind: 'province',
      patterns: [/\bprovincia\b/, /\bprovince\b/, /state\/province/, /\bcounty\b/, /\bestado\b/, /\bdistrito\b/, /\buf\b/, /\bstate\b/],
    },
    { kind: 'country', patterns: [/\bpaese\b/, /\bnazione\b/, /\bpa[ií]s\b/, /\bcountry\b/] },
    { kind: 'birth_date', patterns: [/data di nascita/, /data de nascimento/, /date ?of ?birth/, /\bbirthdate\b/, /\bbirthday\b/, /\bdob\b/] },
    { kind: 'codice_fiscale', patterns: [/codice fiscale/] },
    { kind: 'partita_iva', patterns: [/partita iva/] },
    { kind: 'nif', patterns: [/\bnif\b/, /\bcontribuinte\b/] },
    { kind: 'cpf', patterns: [/\bcpf\b/] },
    { kind: 'vat', patterns: [/\bvat\b/, /vat ?number/] },
  ];

  // TIER2: short / ambiguous fallback words. Only consulted if nothing in
  // TIER1 matched anywhere in the field's combined text.
  var TIER2 = [
    { kind: 'given_name', patterns: [/\bnome\b/] },
    { kind: 'full_name', patterns: [/\bname\b/] },
    // bare "numero"/"número" defaults to house number, not phone: phone
    // fields are virtually always caught by a TIER1 phrase first (e.g.
    // "numero di telefono"/"telefone"); a lone "numero"/"n." near an
    // address block is far more commonly the house number.
    { kind: 'house_number', patterns: [/\bnumero\b/, /\bn[uº]\.?\b/] },
    { kind: 'street_full', patterns: [/\baddress\b/] },
    { kind: 'phone', patterns: [/\btel\b/, /\bfone\b/] },
  ];

  // ---------------------------------------------------------------------
  // Field extraction
  // ---------------------------------------------------------------------

  var HARD_SKIP_TYPES = {
    password: 1, hidden: 1, submit: 1, button: 1, reset: 1,
    image: 1, checkbox: 1, radio: 1, file: 1, range: 1, color: 1,
    search: 1,
  };

  function cssEscapeId(id) {
    if (typeof CSS !== 'undefined' && CSS.escape) return CSS.escape(id);
    return String(id).replace(/([^a-zA-Z0-9_-])/g, '\\$1');
  }

  // The "root" a field's labels live in: its ShadowRoot when the field is
  // inside an (open) shadow tree, or its ownerDocument otherwise. Both
  // implement the same DocumentOrShadowRoot mixin (querySelector,
  // getElementById), so label[for]/aria-labelledby lookups below are scoped
  // to that root and never leak into -- or reach across from -- an
  // unrelated shadow tree or the outer document. Closed shadow roots
  // (`{mode: 'closed'}`) are invisible to script entirely: `host.shadowRoot`
  // reads back as null for them, indistinguishable from "no shadow root", so
  // fields inside a closed root can never be discovered or labelled by this
  // script. There is no workaround for that from outside the page.
  function rootOf(el, doc) {
    try {
      if (el.getRootNode) return el.getRootNode();
    } catch (e) { /* ignore */ }
    return (el.ownerDocument || doc);
  }

  function labelTextFor(el, root) {
    var texts = [];
    if (el.id) {
      try {
        var lbl = root.querySelector('label[for="' + cssEscapeId(el.id) + '"]');
        if (lbl) texts.push(lbl.textContent);
      } catch (e) { /* invalid selector, ignore */ }
    }
    var wrapping = el.closest ? el.closest('label') : null;
    if (wrapping) texts.push(wrapping.textContent);
    var labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      labelledBy.split(/\s+/).forEach(function (id) {
        var n = root.getElementById ? root.getElementById(id) : null;
        if (n) texts.push(n.textContent);
      });
    }
    return texts.join(' ');
  }

  function nearbyText(el) {
    var node = el.previousSibling;
    while (node) {
      if (node.nodeType === 3 && node.textContent && node.textContent.trim()) return node.textContent;
      if (node.nodeType === 1 && node.textContent && node.textContent.trim()) return node.textContent;
      node = node.previousSibling;
    }
    if (el.parentElement && el.parentElement.previousElementSibling) {
      return el.parentElement.previousElementSibling.textContent || '';
    }
    return '';
  }

  function extractMeta(el, doc) {
    var autocompleteRaw = (el.getAttribute('autocomplete') || '').toLowerCase();
    var tokens = autocompleteRaw.split(/\s+/).filter(Boolean);
    var root = rootOf(el, doc);
    var label = labelTextFor(el, root);
    var near = label.trim() ? '' : nearbyText(el);
    var maxLength = null;
    if (typeof el.maxLength === 'number' && el.maxLength > 0) maxLength = el.maxLength;
    return {
      el: el,
      tag: el.tagName,
      type: (el.getAttribute('type') || (el.tagName === 'INPUT' ? 'text' : '')).toLowerCase(),
      autocompleteRaw: autocompleteRaw,
      autocompleteTokens: tokens,
      name: el.getAttribute('name') || '',
      id: el.getAttribute('id') || '',
      placeholder: el.getAttribute('placeholder') || '',
      ariaLabel: el.getAttribute('aria-label') || '',
      labelText: label,
      nearbyText: near,
      maxLength: maxLength,
      pattern: el.getAttribute('pattern') || '',
      inShadow: root !== (el.ownerDocument || doc),
    };
  }

  function haystackOf(meta) {
    return normalize([meta.name, meta.id, meta.placeholder, meta.ariaLabel, meta.labelText, meta.nearbyText].join(' '));
  }

  // ---------------------------------------------------------------------
  // Classification
  // ---------------------------------------------------------------------

  function matchKeywordTiers(haystack, locale, tiers) {
    for (var i = 0; i < tiers.length; i++) {
      var rule = tiers[i];
      if (matchesAny(haystack, rule.patterns)) {
        var kind = typeof rule.kind === 'function' ? rule.kind(locale) : rule.kind;
        if (kind) return kind;
      }
    }
    return null;
  }

  // True when a field is deliberately never filled (password/payment/OTP/
  // search-ish autocomplete token or keyword match) -- as opposed to a field
  // classify() simply has no rule for. Exported/reused by extractMisses() so
  // deliberately-skipped fields are never logged as "misses": logging them
  // would not help improve the keyword tables and would leak more page
  // structure than necessary.
  function shouldSkip(meta) {
    for (var i = 0; i < meta.autocompleteTokens.length; i++) {
      if (SKIP_AUTOCOMPLETE[meta.autocompleteTokens[i]]) return true;
    }
    return matchesAny(haystackOf(meta), SKIP_KEYWORDS);
  }

  function classify(meta, locale) {
    if (shouldSkip(meta)) return null;
    var haystack = haystackOf(meta);

    // 1. autocomplete attribute
    for (var j = 0; j < meta.autocompleteTokens.length; j++) {
      var mapped = AUTOCOMPLETE_MAP[meta.autocompleteTokens[j]];
      if (mapped) return mapped;
    }
    // 2. type attribute (narrow set)
    if (TYPE_MAP[meta.type]) return TYPE_MAP[meta.type];
    // 3. keyword tiers
    var k = matchKeywordTiers(haystack, locale, TIER1);
    if (k) return k;
    return matchKeywordTiers(haystack, locale, TIER2);
  }

  // ---------------------------------------------------------------------
  // Value derivation
  // ---------------------------------------------------------------------

  function fullName(profile) {
    if (profile.full_name) return profile.full_name;
    return [profile.given_name, profile.family_name].filter(Boolean).join(' ');
  }
  function givenName(profile) {
    if (profile.given_name) return profile.given_name;
    if (profile.full_name) return profile.full_name.split(/\s+/)[0];
    return '';
  }
  function familyName(profile) {
    if (profile.family_name) return profile.family_name;
    if (profile.full_name) {
      var parts = profile.full_name.split(/\s+/);
      return parts.length > 1 ? parts.slice(1).join(' ') : '';
    }
    return '';
  }
  function streetFull(profile) {
    var s = profile.street || '';
    if (profile.house_number) s = s ? (s + ', ' + profile.house_number) : profile.house_number;
    return s;
  }

  function formatPostal(raw, meta) {
    if (!raw) return '';
    var digits = raw.replace(/\D/g, '');
    var maxLen = meta.maxLength;
    if (maxLen && maxLen > 0) {
      if (raw.length <= maxLen) return raw;
      if (digits.length <= maxLen) return digits;
      return raw.slice(0, maxLen);
    }
    if (meta.pattern) {
      try {
        var re = new RegExp('^(?:' + meta.pattern + ')$');
        if (re.test(raw)) return raw;
        if (re.test(digits)) return digits;
      } catch (e) { /* invalid pattern, ignore */ }
    }
    return raw;
  }

  function pad2(n) { n = String(n); return n.length < 2 ? '0' + n : n; }

  function parseBirthDate(iso) {
    if (!iso) return null;
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
    if (!m) return null;
    return { year: m[1], month: m[2], day: m[3] };
  }

  function birthPart(iso, part) {
    var p = parseBirthDate(iso);
    if (!p) return '';
    return p[part];
  }

  function formatBirthDate(iso, meta, locale) {
    var p = parseBirthDate(iso);
    if (!p) return '';
    if (meta.type === 'date') return iso; // native date input always wants ISO
    if (locale === 'en-us') return p.month + '/' + p.day + '/' + p.year;
    return p.day + '/' + p.month + '/' + p.year; // IT / PT / BR / generic default
  }

  function valueForKind(kind, profile, meta, locale) {
    switch (kind) {
      case 'given_name': return givenName(profile);
      case 'family_name': return familyName(profile);
      case 'full_name': return fullName(profile);
      case 'email': return profile.email || '';
      case 'phone': return profile.phone || profile.phone_national || '';
      case 'phone_national': return profile.phone_national || profile.phone || '';
      case 'organization': return profile.organization || '';
      case 'street': return profile.street || '';
      case 'house_number': return profile.house_number || '';
      case 'address_line2': return profile.address_line2 || '';
      case 'neighborhood': return profile.neighborhood || '';
      case 'street_full': return streetFull(profile);
      case 'postal_code': return formatPostal(profile.postal_code || '', meta);
      case 'city': return profile.city || '';
      case 'province': return profile.province || '';
      case 'province_code': return profile.province_code || '';
      case 'country': return profile.country || '';
      case 'country_code': return profile.country_code || '';
      case 'birth_date': return formatBirthDate(profile.birth_date, meta, locale);
      case 'birth_day': return birthPart(profile.birth_date, 'day');
      case 'birth_month': return birthPart(profile.birth_date, 'month');
      case 'birth_year': return birthPart(profile.birth_date, 'year');
      case 'codice_fiscale': return profile.codice_fiscale || '';
      case 'partita_iva': return profile.partita_iva || '';
      case 'nif': return profile.nif || '';
      case 'cpf': return profile.cpf || '';
      case 'vat': return profile.vat || '';
      default: return '';
    }
  }

  // ---------------------------------------------------------------------
  // Select (country / province) matching
  // ---------------------------------------------------------------------

  var COUNTRY_ALIASES = {
    IT: ['italy', 'italia', 'italie'],
    PT: ['portugal'],
    BR: ['brazil', 'brasil'],
    US: ['united states', 'united states of america', 'usa', 'estados unidos'],
    GB: ['united kingdom', 'great britain', 'uk', 'regno unito', 'reino unido'],
    DE: ['germany', 'germania', 'alemanha'],
    FR: ['france', 'francia', 'franca'],
    ES: ['spain', 'spagna', 'espanha', 'espana'],
  };

  function bestOptionIndex(el, scorer) {
    var best = -1, bestScore = 0;
    for (var i = 0; i < el.options.length; i++) {
      var score = scorer(el.options[i]);
      if (score > bestScore) { bestScore = score; best = i; }
    }
    return best;
  }

  function matchCountryOption(el, profile) {
    var code = normalize(profile.country_code || '');
    var name = normalize(profile.country || '');
    var aliases = (profile.country_code && COUNTRY_ALIASES[profile.country_code.toUpperCase()]) || [];
    return bestOptionIndex(el, function (opt) {
      var ov = normalize(opt.value);
      var ot = normalize(opt.textContent);
      if (code && ov === code) return 4;
      if (name && ot === name) return 3;
      if (aliases.indexOf(ot) !== -1) return 3;
      if (name && name.length > 2 && ot.indexOf(name) !== -1) return 2;
      return 0;
    });
  }

  function matchProvinceOption(el, profile) {
    var code = normalize(profile.province_code || '');
    var name = normalize(profile.province || '');
    return bestOptionIndex(el, function (opt) {
      var ov = normalize(opt.value);
      var ot = normalize(opt.textContent);
      if (code && (ov === code || ot === code)) return 4;
      if (name && ot === name) return 3;
      if (name && name.length > 2 && ot.indexOf(name) !== -1) return 2;
      return 0;
    });
  }

  function matchGenericOption(el, desired) {
    var want = normalize(desired);
    if (!want) return -1;
    return bestOptionIndex(el, function (opt) {
      var ov = normalize(opt.value);
      var ot = normalize(opt.textContent);
      if (ov === want || ot === want) return 3;
      if (ot.indexOf(want) !== -1) return 1;
      return 0;
    });
  }

  function matchNumericOption(el, desired) {
    if (!desired) return -1;
    var want = String(parseInt(desired, 10));
    var wantPadded = pad2(want);
    return bestOptionIndex(el, function (opt) {
      var ov = opt.value.trim();
      var ot = opt.textContent.trim();
      if (ov === want || ov === wantPadded || ot === want || ot === wantPadded) return 3;
      return 0;
    });
  }

  // ---------------------------------------------------------------------
  // Visibility / eligibility
  // ---------------------------------------------------------------------

  // `strictVisibility` uses layout signals (offsetParent / getClientRects)
  // that real browser engines compute but that jsdom (used for unit tests)
  // never populates (jsdom does no layout at all). Unit tests therefore run
  // with strictVisibility: false and rely only on the style/attribute based
  // checks below, which jsdom does support for the simple inline styles our
  // fixtures use.
  function isVisible(el, win, strictVisibility) {
    if (el.hidden) return false;
    var style = null;
    try { style = (win.getComputedStyle || win.window.getComputedStyle)(el); } catch (e) { /* ignore */ }
    if (style) {
      if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
      var opacity = parseFloat(style.opacity);
      if (!isNaN(opacity) && opacity === 0) return false;
    }
    if (strictVisibility) {
      var noOffsetParent = (typeof el.offsetParent !== 'undefined') && el.offsetParent === null;
      var noRects = (typeof el.getClientRects === 'function') && el.getClientRects().length === 0;
      if (noOffsetParent && noRects && (!style || style.position !== 'fixed')) return false;
    }
    return true;
  }

  function eligible(el, win, strictVisibility) {
    var tag = el.tagName;
    if (tag === 'INPUT') {
      var type = (el.getAttribute('type') || 'text').toLowerCase();
      if (HARD_SKIP_TYPES[type]) return false;
    } else if (tag !== 'SELECT' && tag !== 'TEXTAREA') {
      return false;
    }
    if (el.disabled) return false;
    if ((tag === 'INPUT' || tag === 'TEXTAREA') && el.readOnly) return false;
    if (!isVisible(el, win, strictVisibility)) return false;
    return true;
  }

  // ---------------------------------------------------------------------
  // Element discovery (document + same-origin iframes + open shadow roots,
  // recursively and in any combination -- a shadow root inside a same-origin
  // iframe, a shadow root nested inside another shadow root, etc.)
  // ---------------------------------------------------------------------

  var MAX_DISCOVERY_DEPTH = 8; // guards against pathological/cyclic nesting

  function collectCandidates(root, depth) {
    depth = depth || 0;
    var out = [];
    if (depth > MAX_DISCOVERY_DEPTH) return out;

    var nodes = root.querySelectorAll('input, select, textarea');
    for (var i = 0; i < nodes.length; i++) out.push(nodes[i]);

    // Open shadow roots: `el.shadowRoot` is only non-null for shadow roots
    // attached with {mode: 'open'} (or accessed via a script that already
    // held the reference); a closed root reads back as null here just like
    // "no shadow root at all" -- there is no way to enumerate or fill fields
    // inside those from outside the page.
    var all = root.querySelectorAll('*');
    for (var k = 0; k < all.length; k++) {
      var host = all[k];
      if (host.shadowRoot) {
        out = out.concat(collectCandidates(host.shadowRoot, depth + 1));
      }
    }

    if (depth < 3) {
      var iframes = root.querySelectorAll('iframe');
      for (var j = 0; j < iframes.length; j++) {
        try {
          var idoc = iframes[j].contentDocument;
          if (idoc) out = out.concat(collectCandidates(idoc, depth + 1));
        } catch (e) { /* cross-origin iframe, skip */ }
      }
    }
    return out;
  }

  // ---------------------------------------------------------------------
  // Filling (native setter + bubbling events, for React/Vue/Angular)
  // ---------------------------------------------------------------------

  function elementWindow(el) {
    return (el.ownerDocument && el.ownerDocument.defaultView) || (typeof window !== 'undefined' ? window : null);
  }

  function nativeValueSetter(el, win) {
    var proto;
    if (el.tagName === 'TEXTAREA') proto = win.HTMLTextAreaElement.prototype;
    else if (el.tagName === 'SELECT') proto = win.HTMLSelectElement.prototype;
    else proto = win.HTMLInputElement.prototype;
    var desc = Object.getOwnPropertyDescriptor(proto, 'value');
    return desc && desc.set;
  }

  function setNativeValue(el, value) {
    var win = elementWindow(el);
    var setter = win && nativeValueSetter(el, win);
    if (setter) setter.call(el, value);
    else el.value = value;
  }

  function fireEvents(el) {
    var win = elementWindow(el);
    var EventCtor = (win && win.Event) || Event;
    el.dispatchEvent(new EventCtor('input', { bubbles: true }));
    el.dispatchEvent(new EventCtor('change', { bubbles: true }));
  }

  function highlight(el) {
    try {
      var prevOutline = el.style.outline;
      var prevTransition = el.style.transition;
      el.style.transition = 'outline-color 0.2s ease';
      el.style.outline = '2px solid #2ecc71';
      var win = elementWindow(el) || (typeof window !== 'undefined' ? window : null);
      if (win && win.setTimeout) {
        win.setTimeout(function () {
          el.style.outline = prevOutline;
          el.style.transition = prevTransition;
        }, 2000);
      }
    } catch (e) { /* non-visual test environment, ignore */ }
  }

  // ---------------------------------------------------------------------
  // Undo: remember each field's value from BEFORE autofill first touched it
  // (this run or an earlier one in the same page load -- see recordChange),
  // so `undo()` can restore it later. `changedElements` holds strong
  // references (order of filling, for a stable/predictable undo) so we can
  // iterate them; `previousValues` is a WeakMap so it never keeps a removed
  // element alive by itself. Both live for the lifetime of this module
  // instance, i.e. until the isolated world's QuteAutofill global is
  // redefined by the next `jseval --file` load of this script (a fresh
  // `pf`/`pF` invocation always does that before filling again) or the page
  // navigates away.
  var changedElements = [];
  var previousValues = new WeakMap();

  function recordChange(el) {
    if (previousValues.has(el)) return; // keep the ORIGINAL pre-autofill value
    previousValues.set(el, {
      value: el.value,
      selectedIndex: el.tagName === 'SELECT' ? el.selectedIndex : undefined,
    });
    changedElements.push(el);
  }

  function fillTextLike(el, value) {
    recordChange(el);
    el.focus();
    setNativeValue(el, value);
    fireEvents(el);
    el.blur();
    highlight(el);
  }

  function fillSelectByIndex(el, idx) {
    if (idx < 0) return false;
    recordChange(el);
    el.focus();
    setNativeValue(el, el.options[idx].value);
    el.selectedIndex = idx;
    fireEvents(el);
    el.blur();
    highlight(el);
    return true;
  }

  function elementIsAttached(el) {
    var doc = el.ownerDocument;
    return !!(doc && doc.contains && doc.contains(el));
  }

  function undo() {
    var n = 0;
    for (var i = 0; i < changedElements.length; i++) {
      var el = changedElements[i];
      var prev = previousValues.get(el);
      if (!prev) continue;
      if (!elementIsAttached(el)) continue; // removed from the DOM meanwhile, skip safely
      try {
        setNativeValue(el, prev.value);
        if (el.tagName === 'SELECT' && typeof prev.selectedIndex === 'number' && prev.selectedIndex >= 0) {
          el.selectedIndex = prev.selectedIndex;
        }
        fireEvents(el);
        n++;
      } catch (e) { /* leave this one as-is, keep undoing the rest */ }
    }
    changedElements = [];
    previousValues = new WeakMap();
    return { undone: n, summary: 'autofill: undid ' + n + ' field' + (n === 1 ? '' : 's') };
  }

  function pickSelectIndex(kind, el, profile, meta, locale) {
    if (kind === 'country_code' || kind === 'country') return matchCountryOption(el, profile);
    if (kind === 'province_code' || kind === 'province') return matchProvinceOption(el, profile);
    if (kind === 'birth_day' || kind === 'birth_month' || kind === 'birth_year') {
      return matchNumericOption(el, valueForKind(kind, profile, meta, locale));
    }
    return matchGenericOption(el, valueForKind(kind, profile, meta, locale));
  }

  // ---------------------------------------------------------------------
  // Late-appearing fields: after the initial fill, keep watching the page
  // for ~8s (default) for fields that did not exist yet (multi-step forms,
  // a country/step selector that reveals an address block, ...) and fill
  // any newly-appearing, still-empty, classifiable ones with the same
  // profile/options. This phase NEVER overwrites (even with --overwrite:
  // overwriting only ever makes sense for the fields that were on the page
  // when the user asked us to fill it) and never touches a field the user
  // has since typed into, tracked via a one-shot 'input' listener attached
  // the moment we first consider a field -- independent of its value, so
  // even "user cleared it back to empty" still counts as edited.
  // ---------------------------------------------------------------------

  var LATE_OBSERVE_DEFAULT_MS = 8000;
  var LATE_OBSERVE_POLL_MS = 500;

  function startLateObserver(doc, win, profile, opts, locale) {
    if (!win || typeof win.MutationObserver === 'undefined') return null;
    var strictVisibility = opts.strictVisibility !== false;
    var skipKinds = {};
    (opts.skipKinds || []).forEach(function (k) { skipKinds[k] = 1; });
    var durationMs = typeof opts.lateObserveMs === 'number' ? opts.lateObserveMs : LATE_OBSERVE_DEFAULT_MS;
    var pollMs = typeof opts.lateObservePollMs === 'number' ? opts.lateObservePollMs : LATE_OBSERVE_POLL_MS;

    var seen = new WeakSet();     // fields already considered once (filled, skipped, or ineligible)
    var touched = new WeakSet();  // fields the user has typed into -- never fill, ever
    var shadowObserved = new WeakSet();
    var observers = [];
    var stopped = false;

    function markTouched(ev) {
      try { touched.add(ev.target); } catch (e) { /* ignore */ }
    }

    function considerElement(el) {
      if (seen.has(el)) return;
      seen.add(el);
      var elWin = elementWindow(el) || win;
      if (!eligible(el, elWin, strictVisibility)) return;
      // Attach the edit guard before we do anything else with this field, so
      // there is no window between "we noticed it" and "we might fill it"
      // where a keystroke could slip past undetected.
      try { el.addEventListener('input', markTouched, { once: true }); } catch (e) { /* ignore */ }

      var meta = extractMeta(el, el.ownerDocument || doc);
      var kind = classify(meta, locale);
      if (!kind || skipKinds[kind]) return;
      if (touched.has(el)) return;

      var tag = el.tagName;
      if (tag === 'SELECT') {
        if (el.value) return; // already has a value: never overwrite in this phase
        var idx = pickSelectIndex(kind, el, profile, meta, locale);
        if (idx < 0 || touched.has(el)) return;
        fillSelectByIndex(el, idx);
      } else {
        if (el.value && el.value.length) return; // already has a value: never overwrite in this phase
        var value = valueForKind(kind, profile, meta, locale);
        if (!value || touched.has(el)) return;
        fillTextLike(el, value);
      }
    }

    // MutationObserver does not cross shadow boundaries: a <div> mutating
    // inside someone's shadow root is invisible to an observer rooted at
    // `document`. So alongside the document-level observer, walk the tree
    // for shadow hosts and attach a dedicated observer to each shadow root
    // we find, recursively. There is no DOM event for "a shadow root was
    // just attached", so newly-attached shadow roots are only found by
    // re-walking the tree -- done here on every mutation callback tick AND
    // on a low-frequency poll (see pollMs below), which is "feasible" but
    // not watertight: a shadow root attached to a host element that itself
    // never mutates again, between two poll ticks, could in theory be
    // missed. Good enough for real-world multi-step/web-component forms.
    function observeShadowRootsIn(root) {
      var all;
      try { all = root.querySelectorAll('*'); } catch (e) { return; }
      for (var i = 0; i < all.length; i++) {
        var host = all[i];
        if (host.shadowRoot && !shadowObserved.has(host.shadowRoot)) {
          shadowObserved.add(host.shadowRoot);
          observe(host.shadowRoot);
          observeShadowRootsIn(host.shadowRoot);
        }
      }
    }

    function scanAll() {
      if (stopped) return;
      observeShadowRootsIn(doc);
      var candidates = collectCandidates(doc, 0);
      for (var i = 0; i < candidates.length; i++) considerElement(candidates[i]);
    }

    function observe(root) {
      try {
        var mo = new win.MutationObserver(scanAll);
        mo.observe(root, { childList: true, subtree: true });
        observers.push(mo);
      } catch (e) { /* ignore */ }
    }

    scanAll(); // fields that appeared between run()'s own scan and here
    observe(doc);

    var pollTimer = win.setInterval ? win.setInterval(scanAll, pollMs) : null;

    function stop() {
      if (stopped) return;
      stopped = true;
      observers.forEach(function (o) { try { o.disconnect(); } catch (e) { /* ignore */ } });
      observers.length = 0;
      if (pollTimer !== null && win.clearInterval) win.clearInterval(pollTimer);
    }

    if (win.setTimeout) win.setTimeout(stop, durationMs);

    return { stop: stop };
  }

  // ---------------------------------------------------------------------
  // Learn from misses (classify-only, no filling): used by the detached
  // background `node` runner (autofill-lib/run-classify.js), which loads
  // THIS SAME file (the one source of truth for the keyword tables) via
  // jsdom over a dump of the page's DOM, and by extension by the
  // `--misses` report. Only records fields that survived the exact same
  // eligibility/skip rules the filler itself uses (so passwords, hidden
  // fields, search boxes, card numbers, OTP codes, etc. are never logged --
  // they are not classifier gaps, they're deliberate), and never any field
  // *value* -- only structural metadata useful for spotting a missing
  // keyword.
  // ---------------------------------------------------------------------

  function truncate(s, n) {
    s = String(s || '').replace(/\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n) : s;
  }

  function extractMisses(doc, win, opts) {
    opts = opts || {};
    win = win || (typeof window !== 'undefined' ? window : null);
    var strictVisibility = opts.strictVisibility === true; // default false: no real layout in this context
    var locale = detectLocale(doc);
    var candidates = collectCandidates(doc, 0);
    var misses = [];

    for (var i = 0; i < candidates.length; i++) {
      var el = candidates[i];
      var elWin = (win && elementWindow(el)) || win;
      if (!eligible(el, elWin, strictVisibility)) continue;

      var meta = extractMeta(el, el.ownerDocument || doc);
      if (shouldSkip(meta)) continue; // deliberately skipped, not a classifier gap
      var kind = classify(meta, locale);
      if (kind) continue; // classified fine

      misses.push({
        type: meta.type,
        name: meta.name,
        id: meta.id,
        placeholder: meta.placeholder,
        autocomplete: meta.autocompleteRaw,
        label_text: truncate(meta.labelText || meta.nearbyText || meta.ariaLabel, 80),
      });
    }
    return misses;
  }

  // ---------------------------------------------------------------------
  // Main entry point
  // ---------------------------------------------------------------------

  function run(profileJson, optsJson, doc, win) {
    doc = doc || (typeof document !== 'undefined' ? document : null);
    win = win || (typeof window !== 'undefined' ? window : null);
    if (!doc || !win) throw new Error('QuteAutofill.run: no document/window available');

    var profile = typeof profileJson === 'string' ? JSON.parse(profileJson) : (profileJson || {});
    var opts = typeof optsJson === 'string' ? JSON.parse(optsJson) : (optsJson || {});
    var overwrite = !!opts.overwrite;
    var profileName = opts.profileName || '';
    var strictVisibility = opts.strictVisibility !== false; // default true (real browser)
    var skipKinds = {};
    (opts.skipKinds || []).forEach(function (k) { skipKinds[k] = 1; });

    var locale = detectLocale(doc);
    var candidates = collectCandidates(doc, 0);
    var filled = [];
    var skippedFilled = 0; // already had a value, overwrite not requested

    for (var i = 0; i < candidates.length; i++) {
      var el = candidates[i];
      var elWin = elementWindow(el) || win;
      if (!eligible(el, elWin, strictVisibility)) continue;

      var meta = extractMeta(el, el.ownerDocument || doc);
      var kind = classify(meta, locale);
      if (!kind || skipKinds[kind]) continue;

      var tag = el.tagName;
      var hasValue = tag === 'SELECT' ? !!el.value : !!(el.value && el.value.length);

      if (tag === 'SELECT') {
        var idx = pickSelectIndex(kind, el, profile, meta, locale);
        if (idx < 0) continue;
        if (hasValue && !overwrite) { skippedFilled++; continue; }
        if (fillSelectByIndex(el, idx)) filled.push(kind);
      } else {
        var value = valueForKind(kind, profile, meta, locale);
        if (!value) continue;
        if (hasValue && !overwrite) { skippedFilled++; continue; }
        fillTextLike(el, value);
        filled.push(kind);
      }
    }

    var summary = 'autofill: ' + filled.length + ' field' + (filled.length === 1 ? '' : 's') +
      ' filled (profile ' + profileName + ')' +
      (skippedFilled ? ', ' + skippedFilled + ' already filled (use --overwrite)' : '');

    if (opts.lateObserve !== false) {
      try { startLateObserver(doc, win, profile, opts, locale); } catch (e) { /* non-fatal: late fill is best-effort */ }
    }

    return { filled: filled.length, fields: filled, skipped: skippedFilled, locale: locale, summary: summary };
  }

  return {
    run: run,
    undo: undo,
    extractMisses: extractMisses,
    startLateObserver: startLateObserver,
    normalize: normalize,
    detectLocale: detectLocale,
    classify: classify,
    shouldSkip: shouldSkip,
    extractMeta: extractMeta,
    haystackOf: haystackOf,
    valueForKind: valueForKind,
    formatPostal: formatPostal,
    formatBirthDate: formatBirthDate,
    matchCountryOption: matchCountryOption,
    matchProvinceOption: matchProvinceOption,
    collectCandidates: collectCandidates,
    eligible: eligible,
    isVisible: isVisible,
    rootOf: rootOf,
    AUTOCOMPLETE_MAP: AUTOCOMPLETE_MAP,
    TIER1: TIER1,
    TIER2: TIER2,
  };
});
