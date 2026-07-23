/**
 * Pure formatting helpers — no DOM, no fetch, no globals.
 *
 * Everything in `static/lib/` must be importable by Node (`node --test`) without a
 * browser. That is the whole point: rules live here, `app.js` only wires them to the
 * DOM. Ported verbatim from app.js; behaviour intentionally unchanged.
 */

/** ISO date (YYYY-MM-DD) -> German DD.MM.YYYY. Parsed by parts to avoid TZ shift. */
export function formatDate(dateValue) {
    if (!dateValue) return '';
    const [year, month, day] = dateValue.split('-');
    return `${day}.${month}.${year}`;
}

/** As formatDate, with an optional "HH:MM" appended as ", HH:MM Uhr". */
export function formatDateTimeFromParts(dateValue, timeValue) {
    if (!dateValue) return '';
    const formattedDate = formatDate(dateValue);
    return timeValue ? `${formattedDate}, ${timeValue} Uhr` : formattedDate;
}

/**
 * Normalise a stored address to one line per address part.
 * Handles literal "\n" sequences from imported mails and single-line,
 * comma-separated addresses. Expected result: Straße / PLZ Ort / Land.
 *
 * NOTE: duplicates `format_anschrift` in invoice_routes.py — same rule in two
 * languages. See docs/FINDINGS.md; the two must not drift apart.
 */
export function formatAnschrift(raw) {
    if (!raw) return '';
    const addr = raw.replace(/\\n/g, '\n');
    let lines = addr.split('\n').map(l => l.trim()).filter(l => l.length > 0);
    if (lines.length === 1 && lines[0].includes(',')) {
        lines = lines[0].split(',').map(l => l.trim()).filter(l => l.length > 0);
    }
    return lines.join('\n');
}
