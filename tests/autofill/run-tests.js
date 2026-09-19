#!/usr/bin/env node
/*
 * Unit tests for the qutebrowser `autofill` userscript's classifier/filler
 * (../../../../.local/share/qutebrowser/userscripts/autofill.js).
 *
 * Uses jsdom (installed locally in this dir's node_modules, `npm install
 * jsdom` -- not global) to build a real DOM from the fixture HTML files in
 * ./fixtures, then calls QuteAutofill.run() exactly like the browser would,
 * with strictVisibility disabled (jsdom does no layout at all, so
 * offsetParent/getClientRects are always empty/null regardless of real
 * visibility; see the comment on isVisible() in autofill.js).
 *
 * Run with:  node run-tests.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const REPO_JS = path.join(__dirname, '..', '..', 'userscripts', 'autofill.js');
const AUTOFILL_JS = fs.existsSync(REPO_JS) ? REPO_JS : path.join(__dirname, '..', '..', '..', '..',
  '.local', 'share', 'qutebrowser', 'userscripts', 'autofill.js');
const QuteAutofill = require(AUTOFILL_JS);

let pass = 0;
let fail = 0;
const failures = [];
const pendingAsync = []; // promises the async (late-field observer) sections push onto

function loadFixture(name) {
  const html = fs.readFileSync(path.join(__dirname, 'fixtures', name), 'utf8');
  const dom = new JSDOM(html, { url: 'https://example.test/' });
  return dom;
}

function runOn(name, profile, opts) {
  const dom = loadFixture(name);
  const doc = dom.window.document;
  const win = dom.window;
  const res = QuteAutofill.run(profile, Object.assign({ strictVisibility: false }, opts), doc, win);
  return { dom, doc, win, res };
}

function valueOf(doc, id) {
  const el = doc.getElementById(id);
  if (!el) return undefined;
  return el.value;
}

function check(label, actual, expected) {
  const ok = actual === expected;
  if (ok) {
    pass++;
  } else {
    fail++;
    failures.push(`${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
  console.log(`  ${ok ? 'ok ' : 'FAIL'} ${label} = ${JSON.stringify(actual)}${ok ? '' : ' (expected ' + JSON.stringify(expected) + ')'}`);
}

function section(title) {
  console.log('\n=== ' + title + ' ===');
}

// ---------------------------------------------------------------------
// Test profiles (mirrors the fake example data shipped in profiles.toml)
// ---------------------------------------------------------------------

const italia = {
  given_name: 'Mario', family_name: 'Rossi', email: 'mario.rossi@example.com',
  phone: '+39 333 1234567', phone_national: '333 1234567', organization: 'Acme SRL',
  street: 'Via Roma', house_number: '1', address_line2: '', neighborhood: '',
  postal_code: '50100', city: 'Firenze', province: 'Firenze', province_code: 'FI',
  country: 'Italy', country_code: 'IT', birth_date: '1990-05-12',
  codice_fiscale: 'RSSMRA90E12D612X', partita_iva: '12345678901', nif: '', cpf: '', vat: '',
};

const portugal = {
  given_name: 'Joao', family_name: 'Silva', email: 'joao.silva@example.com',
  phone: '+351 912 345 678', phone_national: '912 345 678', organization: 'Silva Lda',
  street: 'Rua Augusta', house_number: '10', address_line2: '2 Esq', neighborhood: 'Baixa',
  postal_code: '1100-053', city: 'Lisboa', province: 'Lisboa', province_code: 'Lisboa',
  country: 'Portugal', country_code: 'PT', birth_date: '1988-03-22',
  codice_fiscale: '', partita_iva: '', nif: '123456789', cpf: '', vat: 'PT123456789',
};

const brasil = {
  given_name: 'Joao', family_name: 'Souza', email: 'joao.souza@example.com',
  phone: '+55 11 91234-5678', phone_national: '(11) 91234-5678', organization: 'Souza Ltda',
  street: 'Avenida Paulista', house_number: '1000', address_line2: 'Apto 45', neighborhood: 'Bela Vista',
  postal_code: '01310-100', city: 'Sao Paulo', province: 'Sao Paulo', province_code: 'SP',
  country: 'Brazil', country_code: 'BR', birth_date: '1995-11-30',
  codice_fiscale: '', partita_iva: '', nif: '', cpf: '123.456.789-09', vat: '',
};

const us = {
  given_name: 'John', family_name: 'Doe', email: 'john.doe@example.com',
  phone: '+1 415 555 0100', organization: 'Doe Inc',
  street: 'Market Street', house_number: '1', address_line2: 'Suite 400',
  postal_code: '94105', city: 'San Francisco', province: 'California', province_code: 'CA',
  country: 'United States', country_code: 'US', birth_date: '1985-07-04',
};

// ---------------------------------------------------------------------
// 1. Italian e-commerce checkout
// ---------------------------------------------------------------------
section('it-checkout.html (profile: italia)');
{
  const { doc, res } = runOn('it-checkout.html', italia, { profileName: 'italia' });
  console.log('  summary:', res.summary);
  check('nome', valueOf(doc, 'nome'), 'Mario');
  check('cognome', valueOf(doc, 'cognome'), 'Rossi');
  check('email', valueOf(doc, 'email'), 'mario.rossi@example.com');
  check('telefono', valueOf(doc, 'telefono'), '+39 333 1234567');
  check('indirizzo', valueOf(doc, 'indirizzo'), 'Via Roma');
  check('numero civico', valueOf(doc, 'civico'), '1');
  check('CAP', valueOf(doc, 'cap'), '50100');
  check('citta', valueOf(doc, 'citta'), 'Firenze');
  check('provincia (select)', valueOf(doc, 'provincia'), 'FI');
  check('codice fiscale', valueOf(doc, 'cf'), 'RSSMRA90E12D612X');
  // decoys must stay untouched
  check('search box untouched', valueOf(doc, 'q'), '');
  check('password untouched', valueOf(doc, 'pw'), '');
  check('honeypot untouched', valueOf(doc, 'honeypot') === undefined ? doc.querySelector('[name=honeypot]').value : '', '');
  check('card number untouched', doc.querySelector('[name=numero_carta]').value, '');
}

// ---------------------------------------------------------------------
// 2. Portuguese (PT) form
// ---------------------------------------------------------------------
section('pt-form.html (profile: portugal)');
{
  const { doc, res } = runOn('pt-form.html', portugal, { profileName: 'portugal' });
  console.log('  summary:', res.summary);
  check('nome', valueOf(doc, 'nome'), 'Joao');
  check('apelido (PT = surname)', valueOf(doc, 'apelido'), 'Silva');
  check('e-mail', valueOf(doc, 'email'), 'joao.silva@example.com');
  check('telemovel', valueOf(doc, 'tel'), '+351 912 345 678');
  check('morada (composed street+number)', valueOf(doc, 'morada'), 'Rua Augusta, 10');
  check('codigo postal (fits maxlength=8)', valueOf(doc, 'cp'), '1100-053');
  check('localidade', valueOf(doc, 'loc'), 'Lisboa');
  check('NIF', valueOf(doc, 'nif'), '123456789');
}

// ---------------------------------------------------------------------
// 3. Brazilian form
// ---------------------------------------------------------------------
section('br-form.html (profile: brasil)');
{
  const { doc, res } = runOn('br-form.html', brasil, { profileName: 'brasil' });
  console.log('  summary:', res.summary);
  check('nome completo', valueOf(doc, 'nome'), 'Joao Souza');
  check('CPF', valueOf(doc, 'cpf'), '123.456.789-09');
  check('CEP (maxlength=9, dash kept)', valueOf(doc, 'cep'), '01310-100');
  check('logradouro', valueOf(doc, 'logradouro'), 'Avenida Paulista');
  check('numero', valueOf(doc, 'numero'), '1000');
  check('complemento', valueOf(doc, 'complemento'), 'Apto 45');
  check('bairro', valueOf(doc, 'bairro'), 'Bela Vista');
  check('cidade', valueOf(doc, 'cidade'), 'Sao Paulo');
  check('UF (select)', valueOf(doc, 'uf'), 'SP');
  check('apelido (BR = nickname, must stay empty)', valueOf(doc, 'apelido'), '');
}

// ---------------------------------------------------------------------
// 4. English/US form using autocomplete attributes
// ---------------------------------------------------------------------
section('en-us-autocomplete.html (profile: us)');
{
  const { doc, res } = runOn('en-us-autocomplete.html', us, { profileName: 'us' });
  console.log('  summary:', res.summary);
  check('given-name', valueOf(doc, 'fn'), 'John');
  check('family-name', valueOf(doc, 'ln'), 'Doe');
  check('email', valueOf(doc, 'em'), 'john.doe@example.com');
  check('tel', valueOf(doc, 'ph'), '+1 415 555 0100');
  check('organization', valueOf(doc, 'org'), 'Doe Inc');
  check('address-line1', valueOf(doc, 'a1'), 'Market Street');
  check('address-line2', valueOf(doc, 'a2'), 'Suite 400');
  check('address-level2 (city)', valueOf(doc, 'city'), 'San Francisco');
  check('address-level1 (state select)', valueOf(doc, 'state'), 'CA');
  check('postal-code', valueOf(doc, 'zip'), '94105');
  check('country (select)', valueOf(doc, 'country'), 'US');
  check('bday (date input, ISO)', valueOf(doc, 'dob'), '1985-07-04');
  check('cc-number untouched', valueOf(doc, 'ccnum'), '');
  check('new-password untouched', valueOf(doc, 'pw'), '');
}

// ---------------------------------------------------------------------
// 5. No labels, placeholders only
// ---------------------------------------------------------------------
section('no-labels-placeholders.html (profile: us)');
{
  const { doc, res } = runOn('no-labels-placeholders.html', us, { profileName: 'us' });
  console.log('  summary:', res.summary);
  check('placeholder "First Name"', valueOf(doc, undefined) , undefined); // no-op, ids absent
  const byName = (n) => doc.querySelector(`[name="${n}"]`).value;
  check('placeholder First Name -> given_name', byName('f1'), 'John');
  check('placeholder Last Name -> family_name', byName('f2'), 'Doe');
  check('placeholder Email Address -> email', byName('f3'), 'john.doe@example.com');
  check('placeholder Phone Number -> phone', byName('f4'), '+1 415 555 0100');
  check('placeholder City -> city', byName('f5'), 'San Francisco');
}

// ---------------------------------------------------------------------
// 5b/5c. Field classification on the two privacy-verification fixtures
// (react-tracker.html, monkeypatch-spy.html). These fixtures also embed a
// <script> that does real cross-realm/prototype tricks (React-style value
// tracking, builtin monkeypatching) to prove the isolated-`--world jseval`
// privacy properties -- that part is NOT meaningfully testable under jsdom
// (a single JS realm, no concept of isolated worlds) and is instead
// verified against the real qutebrowser/QtWebEngine in the E2E round (see
// the task report). loadFixture() here uses jsdom's default `runScripts`
// (scripts do NOT execute), so this only exercises field classification,
// exactly like the other fixtures above.
// ---------------------------------------------------------------------
section('react-tracker.html (profile: italia) -- classification only');
{
  const { doc, res } = runOn('react-tracker.html', italia, { profileName: 'italia' });
  console.log('  summary:', res.summary);
  check('given (autocomplete=given-name)', valueOf(doc, 'given'), 'Mario');
  check('email (autocomplete=email)', valueOf(doc, 'email'), 'mario.rossi@example.com');
  check('phone (autocomplete=tel)', valueOf(doc, 'phone'), '+39 333 1234567');
}

section('monkeypatch-spy.html (profile: italia) -- classification only');
{
  const { doc, res } = runOn('monkeypatch-spy.html', italia, { profileName: 'italia' });
  console.log('  summary:', res.summary);
  check('given (autocomplete=given-name)', valueOf(doc, 'given'), 'Mario');
  check('family (autocomplete=family-name)', valueOf(doc, 'family'), 'Rossi');
  check('cf (placeholder Codice Fiscale)', valueOf(doc, 'cf'), 'RSSMRA90E12D612X');
  check('cpf (profile field empty -> untouched)', valueOf(doc, 'cpf'), '');
  check('nif (profile field empty -> untouched)', valueOf(doc, 'nif'), '');
  check('dob (autocomplete=bday, text input, non-US locale -> DD/MM/YYYY)', valueOf(doc, 'dob'), '12/05/1990');
}

// ---------------------------------------------------------------------
// 6. --overwrite behaviour (pure logic, reuse it-checkout fixture)
// ---------------------------------------------------------------------
section('overwrite flag behaviour');
{
  const dom = loadFixture('it-checkout.html');
  dom.window.document.getElementById('nome').value = 'Existing Value';
  let res = QuteAutofill.run(italia, { strictVisibility: false, profileName: 'italia', overwrite: false }, dom.window.document, dom.window);
  check('without --overwrite, pre-filled field kept', dom.window.document.getElementById('nome').value, 'Existing Value');
  check('without --overwrite, skipped count > 0', res.skipped > 0, true);

  res = QuteAutofill.run(italia, { strictVisibility: false, profileName: 'italia', overwrite: true }, dom.window.document, dom.window);
  check('with --overwrite, pre-filled field replaced', dom.window.document.getElementById('nome').value, 'Mario');
}

// ---------------------------------------------------------------------
// 7. Disabled / readonly / hidden fields are skipped
// ---------------------------------------------------------------------
section('disabled/readonly/hidden are never touched');
{
  const dom = new JSDOM(`<!DOCTYPE html><html lang="en"><body><form>
    <input id="d1" name="d1" placeholder="First Name" disabled>
    <input id="d2" name="d2" placeholder="Last Name" readonly>
    <input id="d3" name="d3" type="hidden" value="">
  </form></body></html>`, { url: 'https://example.test/' });
  const res = QuteAutofill.run(us, { strictVisibility: false, profileName: 'us' }, dom.window.document, dom.window);
  check('disabled field untouched', dom.window.document.getElementById('d1').value, '');
  check('readonly field untouched', dom.window.document.getElementById('d2').value, '');
  check('hidden field untouched', dom.window.document.getElementById('d3').value, '');
  check('nothing filled', res.filled, 0);
}

// ---------------------------------------------------------------------
// 8. Shadow DOM: fields inside an open shadow root are discovered, and
// label[for]/aria-labelledby lookups are scoped to the shadow root the
// field actually lives in (not the outer document, and not some OTHER
// component's shadow root). jsdom does not parse declarative Shadow DOM
// (<template shadowrootmode>) and does not run <script> tags by default
// (see the comment on react-tracker.html/monkeypatch-spy.html above), so
// shadow roots here are attached imperatively in this test file itself --
// exactly what a real custom element's constructor/connectedCallback does
// in the browser (see fixtures/shadow-dom-form.html, used by the E2E round
// against real QtWebEngine, where the <script> genuinely executes).
// ---------------------------------------------------------------------
section('Shadow DOM: fields inside an open shadow root are discovered and filled');
{
  const dom = new JSDOM(`<!DOCTYPE html><html lang="en"><body><div id="host"></div></body></html>`,
    { url: 'https://example.test/' });
  const doc = dom.window.document;
  const shadow = doc.getElementById('host').attachShadow({ mode: 'open' });
  shadow.innerHTML = `
    <label for="sfn">First Name</label><input id="sfn" name="sfn">
    <label for="sem">Email Address</label><input id="sem" name="sem" type="email">
  `;
  const res = QuteAutofill.run(us, { strictVisibility: false, profileName: 'us' }, doc, dom.window);
  console.log('  summary:', res.summary);
  check('shadow DOM given_name filled', shadow.getElementById('sfn').value, 'John');
  check('shadow DOM email filled', shadow.getElementById('sem').value, 'john.doe@example.com');
}

section('Shadow DOM: label lookup is scoped per-root, not shared across components');
{
  // Two independent components, each with an input id="x" but a DIFFERENT
  // label in its OWN shadow root -- proves label[for] resolution cannot
  // accidentally cross into a different shadow tree (or the outer document).
  const dom = new JSDOM(`<!DOCTYPE html><html lang="en"><body>
    <div id="h1"></div><div id="h2"></div><label for="x">WRONG (outer doc)</label>
  </body></html>`, { url: 'https://example.test/' });
  const doc = dom.window.document;
  const s1 = doc.getElementById('h1').attachShadow({ mode: 'open' });
  s1.innerHTML = '<label for="x">First Name</label><input id="x" name="x1">';
  const s2 = doc.getElementById('h2').attachShadow({ mode: 'open' });
  s2.innerHTML = '<label for="x">City</label><input id="x" name="x2">';
  const res = QuteAutofill.run(us, { strictVisibility: false, profileName: 'us' }, doc, dom.window);
  console.log('  summary:', res.summary);
  check('component 1 (own label "First Name") -> given_name', s1.getElementById('x').value, 'John');
  check('component 2 (own label "City") -> city', s2.getElementById('x').value, 'San Francisco');
}

section('Shadow DOM: nested shadow root and a shadow root inside a same-origin iframe');
{
  const dom = new JSDOM(`<!DOCTYPE html><html lang="en"><body>
    <div id="outer"></div><iframe id="fr"></iframe>
  </body></html>`, { url: 'https://example.test/', runScripts: 'dangerously' });
  const doc = dom.window.document;

  const outer = doc.getElementById('outer').attachShadow({ mode: 'open' });
  const innerHost = doc.createElement('div');
  outer.appendChild(innerHost);
  const inner = innerHost.attachShadow({ mode: 'open' });
  inner.innerHTML = '<label for="e">Email Address</label><input id="e" name="e">';

  const iframeDoc = doc.getElementById('fr').contentDocument;
  iframeDoc.body.innerHTML = '<div id="ihost"></div>';
  const iframeShadow = iframeDoc.getElementById('ihost').attachShadow({ mode: 'open' });
  iframeShadow.innerHTML = '<label for="p">Phone Number</label><input id="p" name="p">';

  const res = QuteAutofill.run(us, { strictVisibility: false, profileName: 'us' }, doc, dom.window);
  console.log('  summary:', res.summary);
  check('nested shadow root field filled', inner.getElementById('e').value, 'john.doe@example.com');
  check('shadow root inside a same-origin iframe filled', iframeShadow.getElementById('p').value, '+1 415 555 0100');
}

// ---------------------------------------------------------------------
// 9. Late-appearing fields: filled after they appear, without overwriting
// or touching a field the user has started editing. Uses short
// lateObserveMs/lateObservePollMs (the real browser default is 8000/500ms)
// so the test suite stays fast; the polling fallback (not just
// MutationObserver) is what QuteAutofill.startLateObserver documents as
// covering shadow roots attached after the initial scan.
// Runs asynchronously -- see the scheduling note at the bottom of this file.
// ---------------------------------------------------------------------
section('Late-appearing fields: filled once they appear, edited/pre-filled ones left alone');
{
  const dom = new JSDOM(`<!DOCTYPE html><html lang="en"><body>
    <form id="step1"><label for="fn">First Name</label><input id="fn" name="fn"></form>
  </body></html>`, { url: 'https://example.test/' });
  const doc = dom.window.document;
  const win = dom.window;

  const res = QuteAutofill.run(us, {
    strictVisibility: false, profileName: 'us', lateObserveMs: 300, lateObservePollMs: 25,
  }, doc, win);
  check('initial-pass field filled synchronously as before', doc.getElementById('fn').value, 'John');

  win.setTimeout(() => {
    const form = doc.getElementById('step1');
    form.insertAdjacentHTML('beforeend', `
      <label for="em">Email Address</label><input id="em" name="em">
      <label for="ct">City</label><input id="ct" name="ct" value="preexisting">
      <label for="edited">Phone Number</label><input id="edited" name="edited">
    `);
    const editedEl = doc.getElementById('edited');
    editedEl.value = 'user typed this';
    editedEl.dispatchEvent(new win.Event('input', { bubbles: true }));
  }, 60);

  pendingAsync.push(new Promise((resolve) => {
    win.setTimeout(() => {
      check('late field filled after appearing', doc.getElementById('em').value, 'john.doe@example.com');
      check('late field with a pre-existing value is left alone', doc.getElementById('ct').value, 'preexisting');
      check('late field the user edited is left alone', doc.getElementById('edited').value, 'user typed this');
      resolve();
    }, 220);
  }));
}

// ---------------------------------------------------------------------
// 10. Undo: WeakMap-recorded previous values, restored (native setter +
// input/change events). QuteAutofill.undo() first, to discard any history
// accumulated by earlier sections in this same process (this file requires
// autofill.js once at the top, exactly like the browser keeps ONE
// QuteAutofill instance alive across repeated run() calls in a page until
// the next `jseval --file` reload -- see the module docstring), so this
// section starts from a clean slate.
// ---------------------------------------------------------------------
section('Undo: restores fields to their pre-autofill values, including overwritten ones');
{
  QuteAutofill.undo(); // discard any history from earlier sections above

  const dom = loadFixture('it-checkout.html');
  const doc = dom.window.document;
  doc.getElementById('nome').value = 'Existing Value';
  QuteAutofill.run(italia, { strictVisibility: false, profileName: 'italia', overwrite: true }, doc, dom.window);
  check('nome overwritten before undo', doc.getElementById('nome').value, 'Mario');
  check('cognome filled before undo', doc.getElementById('cognome').value, 'Rossi');

  const res = QuteAutofill.undo();
  console.log('  undo summary:', res.summary);
  check('nome restored to its pre-autofill (overwritten) value', doc.getElementById('nome').value, 'Existing Value');
  check('cognome restored to empty (had no prior value)', doc.getElementById('cognome').value, '');
  check('undo() reports a non-zero count', res.undone > 0, true);

  const res2 = QuteAutofill.undo();
  check('second consecutive undo: nothing left to undo', res2.summary, 'autofill: undid 0 fields');
}

section('Undo: a fresh module instance (simulating a fresh isolated world) has nothing to undo');
{
  delete require.cache[require.resolve(AUTOFILL_JS)];
  const FreshQuteAutofill = require(AUTOFILL_JS);
  const res = FreshQuteAutofill.undo();
  check('fresh instance: nothing to undo', res.summary, 'autofill: undid 0 fields');
}

// ---------------------------------------------------------------------
// 11. Learn from misses (classify-only entry point, no filling, no values
// ever recorded). Exercises the exact same function the detached `node`
// background runner (autofill-lib/run-classify.js) calls over a DOM dump.
// ---------------------------------------------------------------------
section('extractMisses: records genuinely unclassifiable fields only, never values');
{
  const dom = new JSDOM(`<!DOCTYPE html><html lang="en"><body><form>
    <label for="fc">Fav colour</label><input id="fc" name="fc" value="topsecretvalue">
    <label for="ok">Email Address</label><input id="ok" name="ok" type="email">
    <input type="password" name="pw" value="hunter2">
    <input type="hidden" name="csrf" value="abc">
    <input type="search" name="q" placeholder="Search the site">
    <label for="ts">T-shirt size</label><input id="ts" name="ts">
  </form></body></html>`, { url: 'https://shop.example.test/' });
  const misses = QuteAutofill.extractMisses(dom.window.document, dom.window, { strictVisibility: false });
  console.log('  misses:', JSON.stringify(misses));
  const byId = {};
  misses.forEach((m) => { byId[m.id] = m; });

  check('classifiable field (email) is not a miss', !!byId.ok, false);
  check('password field is not a miss', !!byId.pw, false);
  check('hidden field is not a miss', !!byId.csrf, false);
  check('search field is not a miss', !!byId.q, false);
  check('"Fav colour" is recorded as a miss', !!byId.fc, true);
  check('"Fav colour" miss label_text', byId.fc && byId.fc.label_text, 'Fav colour');
  check('"T-shirt size" is recorded as a miss', !!byId.ts, true);
  check('no miss record contains any field value', misses.every((m) => Object.keys(m).every((k) => m[k] !== 'topsecretvalue')), true);
  check('exactly 2 misses found', misses.length, 2);
}

// ---------------------------------------------------------------------
// Some sections above (late-field observer) are asynchronous: their checks
// run inside a setTimeout/Promise, so the final pass/fail totals and
// process.exit() must wait for those to settle first.
// ---------------------------------------------------------------------
Promise.all(pendingAsync).then(() => {
  console.log(`\n${pass} passed, ${fail} failed`);
  if (fail > 0) {
    console.log('\nFailures:');
    failures.forEach((f) => console.log('  - ' + f));
    process.exit(1);
  }
});
