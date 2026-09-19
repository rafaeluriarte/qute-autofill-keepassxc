#!/usr/bin/env node
/*
 * run-classify.js -- detached background classifier for the qutebrowser
 * `autofill` userscript's "learn from misses" feature (see requirement #5 in
 * the task: privacy-first miss logging).
 *
 * The Python userscript (../autofill) spawns this as a fully detached
 * background process (fire-and-forget, start_new_session=True, stdout/stderr
 * discarded) right after sending the fill commands to qutebrowser. It is
 * NEVER run inside the browser/isolated JS world -- it runs as a plain Node
 * process, completely outside qutebrowser, over a static DOM DUMP (a copy of
 * $QUTE_HTML) that the Python script already made. It has no access to the
 * live page, no access to profiles.toml, and is given no field values at
 * all (only structural metadata), so it cannot leak personal data even in
 * principle.
 *
 * Single source of truth: this file requires THE SAME ../autofill.js that
 * runs in the browser (via jseval) and in the unit tests (via jsdom) -- the
 * classifier/keyword tables live in exactly one place. This file only adds
 * the jsdom-loading, argv-parsing and misses.jsonl-writing glue around
 * QuteAutofill.extractMisses().
 *
 * Usage: node run-classify.js <html-dump-path> <host> <misses-jsonl-path> <date>
 *
 * Degrades COMPLETELY SILENTLY (exit 0, no output) whenever anything is
 * missing or goes wrong -- node itself missing is handled on the Python
 * side (it never even tries to spawn this file then); jsdom missing here,
 * inside this file, is exactly why this needs its own try/catch around the
 * require(), since jsdom is a real npm dependency (installed into
 * ./node_modules next to this file, NOT the tests/ folder -- see
 * package.json in this directory) that a from-scratch checkout of the
 * dotfiles might not have installed yet.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const [, , htmlPath, host, missesPath, dateArg] = process.argv;

// Every exit path must remove the private DOM-dump copy (it can contain page content).
function bail() {
  if (htmlPath) { try { fs.unlinkSync(htmlPath); } catch (e) { /* already gone */ } }
  process.exit(0);
}
if (!htmlPath || !missesPath) bail();

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (e) {
  bail(); // jsdom not installed under autofill-lib/node_modules -- silently skip
}

let html;
try {
  html = fs.readFileSync(htmlPath, 'utf8');
} catch (e) {
  bail(); // the temp DOM-dump copy is gone or unreadable -- silently skip
}

const AUTOFILL_JS = path.join(__dirname, '..', 'autofill.js');
let QuteAutofill;
try {
  QuteAutofill = require(AUTOFILL_JS);
} catch (e) {
  bail();
}

let dom;
try {
  dom = new JSDOM(html, { url: 'https://' + (host || 'example.invalid') + '/' });
} catch (e) {
  bail();
}

let misses;
try {
  misses = QuteAutofill.extractMisses(dom.window.document, dom.window, { strictVisibility: false });
} catch (e) {
  bail();
}

// Always clean up the temp DOM-dump copy Python made for us, miss or no miss.
try { fs.unlinkSync(htmlPath); } catch (e) { /* already gone, ignore */ }

if (!misses || misses.length === 0) process.exit(0);

const date = dateArg || new Date().toISOString().slice(0, 10);
const lines = misses.map((m) => JSON.stringify({
  date: date,
  host: host || '',
  type: m.type,
  name: m.name,
  id: m.id,
  placeholder: m.placeholder,
  autocomplete: m.autocomplete,
  label_text: m.label_text,
}));

try {
  fs.appendFileSync(missesPath, lines.join('\n') + '\n', { mode: 0o600 });
  fs.chmodSync(missesPath, 0o600); // enforced every write, in case umask loosened it on creation
} catch (e) {
  /* can't write misses.jsonl -- silently skip, nothing the user needs to see */
}
