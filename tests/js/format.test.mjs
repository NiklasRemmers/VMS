// JS unit tests — zero dependencies, no build step, no browser.
//
//   node --test "tests/js/**/*.test.mjs"
//
// Quote the glob, and do not pass the directory — `node --test tests/js` resolves
// the path as a module and fails.
//
// Canonical example for the frontend section of .claude/skills/bugfix/SKILL.md.
// Only pure modules from static/lib/ are testable here; anything touching the DOM
// belongs in app.js and is out of scope by design.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
    formatDate,
    formatDateTimeFromParts,
    formatAnschrift,
} from '../../static/lib/format.mjs';

test('formatDate: ISO wird zu deutschem Format', () => {
    assert.equal(formatDate('2026-12-31'), '31.12.2026');
});

test('formatDate: leere Eingabe ergibt leeren String', () => {
    assert.equal(formatDate(''), '');
    assert.equal(formatDate(null), '');
    assert.equal(formatDate(undefined), '');
});

test('formatDate: kein Tagesversatz durch Zeitzone', () => {
    // Regression: Date-Parsing verschob den 1. eines Monats auf den Vortag.
    assert.equal(formatDate('2026-01-01'), '01.01.2026');
});

test('formatDateTimeFromParts: Uhrzeit wird angehaengt', () => {
    assert.equal(formatDateTimeFromParts('2026-06-05', '14:30'), '05.06.2026, 14:30 Uhr');
});

test('formatDateTimeFromParts: ohne Uhrzeit nur das Datum', () => {
    assert.equal(formatDateTimeFromParts('2026-06-05', ''), '05.06.2026');
});

test('formatAnschrift: echte Zeilenumbrueche bleiben erhalten', () => {
    assert.equal(
        formatAnschrift('Musterweg 1\n89073 Ulm\nDeutschland'),
        'Musterweg 1\n89073 Ulm\nDeutschland'
    );
});

test('formatAnschrift: literale \\n-Sequenzen aus Mail-Import werden aufgeloest', () => {
    assert.equal(formatAnschrift('Musterweg 1\\n89073 Ulm'), 'Musterweg 1\n89073 Ulm');
});

test('formatAnschrift: einzeilige Komma-Adresse wird aufgeteilt', () => {
    assert.equal(
        formatAnschrift('Musterweg 1, 89073 Ulm, Deutschland'),
        'Musterweg 1\n89073 Ulm\nDeutschland'
    );
});

test('formatAnschrift: Leerzeilen und Rand-Leerzeichen werden entfernt', () => {
    assert.equal(formatAnschrift('  Musterweg 1  \n\n  89073 Ulm '), 'Musterweg 1\n89073 Ulm');
});

test('formatAnschrift: leere Eingabe ergibt leeren String', () => {
    assert.equal(formatAnschrift(''), '');
    assert.equal(formatAnschrift(null), '');
});
