/**
 * SearchableSelect
 *
 * Wertet ein natives <select> zu einem durchsuchbaren Dropdown (Combobox) auf,
 * ohne die bestehende Logik zu brechen: Das <select> bleibt im DOM und ist
 * weiterhin die "Source of Truth" (value, selectedIndex, change-Events, Options).
 * Bestehender Code, der select.value liest/setzt oder Options manipuliert,
 * funktioniert unverändert – die Anzeige wird per MutationObserver / change-Event
 * synchron gehalten.
 *
 * Verwendung:
 *   SearchableSelect.enhance(selectEl);
 *   SearchableSelect.enhanceAll('select[data-searchable]');
 */
(function () {
    'use strict';

    function enhance(select) {
        if (!select || select.tagName !== 'SELECT') return;
        if (select.dataset.ssEnhanced === '1') return;
        select.dataset.ssEnhanced = '1';

        // Wrapper an Stelle des Selects einfügen und Select hineinziehen
        const wrapper = document.createElement('div');
        wrapper.className = 'ss-wrapper relative';
        // Layout-Breite des Selects übernehmen, damit sich nichts verschiebt
        if (select.classList.contains('flex-1')) wrapper.classList.add('flex-1');
        else if (select.classList.contains('w-full')) wrapper.classList.add('w-full');
        else wrapper.classList.add('w-full');

        select.parentNode.insertBefore(wrapper, select);
        wrapper.appendChild(select);

        // Natives Select verstecken (bleibt aber funktional)
        select.style.display = 'none';

        // Anzeige-/Sucheingabe – übernimmt das Aussehen des Selects.
        // Achtung: dabei werden ALLE Klassen des Selects mitkopiert, also auch
        // fachliche Marker wie .equipment-select. Ein querySelectorAll('.equipment-select')
        // findet deshalb Select UND Input und zählt jeden Eintrag doppelt – solche
        // Abfragen müssen mit dem Tag qualifiziert werden ('select.equipment-select').
        // Die Zusatzklasse ss-input macht den Anzeige-Input erkennbar.
        const input = document.createElement('input');
        input.type = 'text';
        input.autocomplete = 'off';
        input.className = select.className + ' ss-input cursor-text';
        input.classList.remove('flex-1'); // Breite regelt der Wrapper
        input.classList.add('block', 'w-full'); // füllt die volle Breite des Dialogs/Wrappers
        wrapper.appendChild(input);

        // Dropdown-Panel
        const panel = document.createElement('div');
        panel.className = 'ss-panel absolute left-0 right-0 mt-1 z-50 hidden max-h-60 overflow-y-auto rounded-lg border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-700 shadow-lg';
        wrapper.appendChild(panel);

        let isOpen = false;
        let activeIndex = -1;   // Index in der aktuell gerenderten Liste
        let rendered = [];      // [{ value, text }]

        function placeholderText() {
            // Erste Option ohne Wert gilt als Platzhalter
            const first = select.options[0];
            return first && first.value === '' ? first.text : 'Suchen…';
        }

        function syncDisplay() {
            const opt = select.selectedOptions[0];
            input.value = opt && opt.value !== '' ? opt.text : '';
            input.placeholder = placeholderText();
        }

        function renderList(filter) {
            const f = (filter || '').trim().toLowerCase();
            panel.innerHTML = '';
            rendered = [];
            for (const opt of select.options) {
                if (opt.value === '') continue; // Platzhalter nicht listen
                const text = opt.text;
                if (f && !text.toLowerCase().includes(f)) continue;
                rendered.push({ value: opt.value, text: text, disabled: opt.disabled });
            }

            if (rendered.length === 0) {
                const empty = document.createElement('div');
                empty.className = 'px-3 py-2 text-sm text-gray-400 dark:text-gray-500';
                empty.textContent = 'Keine Treffer';
                panel.appendChild(empty);
                activeIndex = -1;
                return;
            }

            rendered.forEach((item, idx) => {
                const row = document.createElement('div');
                if (item.disabled) {
                    row.className = 'ss-option px-3 py-2 text-sm text-gray-400 dark:text-gray-500 cursor-not-allowed';
                    row.textContent = item.text;
                    panel.appendChild(row);
                    return;
                }
                row.className = 'ss-option px-3 py-2 text-sm cursor-pointer text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-600';
                if (item.value === select.value) {
                    row.classList.add('bg-gray-100', 'dark:bg-gray-600', 'font-medium');
                }
                row.textContent = item.text;
                row.addEventListener('mousedown', (e) => {
                    e.preventDefault(); // Blur der Eingabe verhindern
                    choose(item.value);
                });
                row.addEventListener('mouseenter', () => setActive(idx));
                panel.appendChild(row);
            });

            // Aktive Zeile auf aktuelle Auswahl setzen, sonst erste auswählbare
            const selIdx = rendered.findIndex(r => r.value === select.value && !r.disabled);
            setActive(selIdx >= 0 ? selIdx : nextEnabled(-1, 1));
        }

        function nextEnabled(from, dir) {
            if (rendered.length === 0) return -1;
            let i = from;
            for (let c = 0; c < rendered.length; c++) {
                i = (i + dir + rendered.length) % rendered.length;
                if (!rendered[i].disabled) return i;
            }
            return -1; // alle Optionen deaktiviert
        }

        function setActive(idx) {
            activeIndex = idx;
            const rows = panel.querySelectorAll('.ss-option');
            rows.forEach((r, i) => {
                if (i === idx) {
                    r.classList.add('bg-gray-200', 'dark:bg-gray-500');
                    r.scrollIntoView({ block: 'nearest' });
                } else {
                    r.classList.remove('bg-gray-200', 'dark:bg-gray-500');
                }
            });
        }

        // selectText=false, wenn das Öffnen durch Tippen ausgelöst wurde: dort
        // würde input.select() den gerade eingegebenen Buchstaben markieren, den
        // der nächste Tastendruck dann überschreibt.
        function open(selectText) {
            if (isOpen) return;
            isOpen = true;
            renderList('');
            panel.classList.remove('hidden');
            if (selectText !== false) input.select();
        }

        function close() {
            if (!isOpen) return;
            isOpen = false;
            panel.classList.add('hidden');
            syncDisplay();
        }

        function choose(value) {
            const opt = Array.from(select.options).find(o => o.value === value);
            if (opt && opt.disabled) return;
            select.value = value;
            select.dispatchEvent(new Event('change', { bubbles: true }));
            close();
        }

        input.addEventListener('focus', () => open());
        input.addEventListener('click', () => open());
        input.addEventListener('input', () => {
            if (!isOpen) open(false);
            renderList(input.value);
        });

        // Signalisiert "Enter gedrückt" an den umgebenden Code (z.B. um den
        // gewählten Eintrag direkt hinzuzufügen, statt den Button zu klicken).
        // Der Listener bestätigt die Behandlung mit preventDefault(); nur dann
        // wird auch das Enter im Formular unterdrückt.
        function emitEnter() {
            const ev = new CustomEvent('ss:enter', { bubbles: true, cancelable: true });
            select.dispatchEvent(ev);
            return ev.defaultPrevented;
        }

        input.addEventListener('keydown', (e) => {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                if (!isOpen) { open(); return; }
                setActive(nextEnabled(activeIndex, 1));
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                setActive(nextEnabled(activeIndex, -1));
            } else if (e.key === 'Enter') {
                if (isOpen && activeIndex >= 0 && rendered[activeIndex] && !rendered[activeIndex].disabled) {
                    e.preventDefault();
                    choose(rendered[activeIndex].value);
                    emitEnter();
                } else if (!isOpen) {
                    // Auswahl steht bereits – Enter bestätigt sie erneut
                    if (emitEnter()) e.preventDefault();
                }
            } else if (e.key === 'Escape') {
                if (isOpen) { e.preventDefault(); close(); }
            }
        });

        // Klick außerhalb schließt das Panel
        document.addEventListener('mousedown', (e) => {
            if (isOpen && !wrapper.contains(e.target)) close();
        });

        // Programmatische Änderungen am Select (neue Options, value-Reset,
        // geänderte Option-Texte) in die Anzeige übernehmen.
        select.addEventListener('change', () => { if (!isOpen) syncDisplay(); });
        const mo = new MutationObserver(() => {
            if (isOpen) renderList(input.value);
            else syncDisplay();
        });
        mo.observe(select, { childList: true, subtree: true, characterData: true, attributes: true });

        syncDisplay();
    }

    function enhanceAll(selector) {
        document.querySelectorAll(selector).forEach(enhance);
    }

    // Fokussiert die Anzeige-Eingabe eines aufgewerteten Selects
    function focus(select) {
        if (!select) return;
        const input = select.parentNode && select.parentNode.querySelector('.ss-input');
        if (input) input.focus();
        else select.focus();
    }

    window.SearchableSelect = { enhance: enhance, enhanceAll: enhanceAll, focus: focus };

    // Auto-Enhance für statische Selects mit [data-searchable]
    document.addEventListener('DOMContentLoaded', function () {
        enhanceAll('select[data-searchable]');
    });
})();
