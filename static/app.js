/**
 * VMS - Frontend JavaScript
 * Handles material selection, signature upload, and PDF generation
 */

// Global state
let materials = {};
let equipment = {};
let cases = {};
let packages = {};
let customMaterials = {}; // Temporary custom materials (not saved to JSON)
let signatureData = null;
let candidateTasks = []; // Loaded candidate tasks for contract

// DOM Elements
const contractForm = document.getElementById('contractForm');
// const materialContainer = document.getElementById('materialContainer'); // Removed due to split
const addMaterialBtn = document.getElementById('addMaterialBtn');
const addCustomMaterialBtn = document.getElementById('addCustomMaterialBtn');
const customMaterialModal = document.getElementById('customMaterialModal');
const packageSelect = document.getElementById('packageSelect');
const loadPackageBtn = document.getElementById('loadPackageBtn');
const signatureInput = document.getElementById('signatureInput');
const signaturePreview = document.getElementById('signaturePreview');
const clearSignatureBtn = document.getElementById('clearSignatureBtn');
const generateBtn = document.getElementById('generateBtn');
const loadingIndicator = document.getElementById('loadingIndicator');
const errorToast = document.getElementById('errorToast');
const errorMessage = document.getElementById('errorMessage');

// Candidate Import Elements
const taskSelect = document.getElementById('taskSelect');
const fillFormBtn = document.getElementById('fillFormBtn');
const importStatus = document.getElementById('importStatus');

// Initialize
document.addEventListener('DOMContentLoaded', async () => {
    await loadMaterials();
    initializeTimePickers();
    initializePackageSelector();
    addDropdownItem('equipment');
    addDropdownItem('case');
    setupEventListeners();
    await loadCandidatesForContract(); // Auto-load candidates
    await loadSavedSignature(); // Auto-load signature from account
});

/**
 * Load saved signature from user account settings
 */
async function loadSavedSignature() {
    try {
        const response = await fetch('/api/signature');
        const data = await response.json();
        if (data.signature) {
            signatureData = data.signature;
            signaturePreview.innerHTML = `<img src="${signatureData}" alt="Unterschrift" class="max-w-full max-h-[150px] object-contain p-4">`;
            signaturePreview.classList.remove('border-dashed');
            signaturePreview.classList.add('border-solid', 'border-primary-500');
            clearSignatureBtn.classList.remove('hidden');
            validateForm();
        }
    } catch (e) {
        // Silently ignore - user just doesn't have a saved signature
    }
}

/**
 * Load materials and packages from JSON
 */
async function loadMaterials() {
    try {
        const response = await fetch('/api/materials');
        if (response.ok) {
            const data = await response.json();
            // Check if new structure with equipment and cases
            if (data.equipment && data.cases) {
                equipment = data.equipment;
                cases = data.cases;
                materials = data.materials; // Keep for retro-compatibility
                packages = data.packages || {};
            } else if (data.materials) {
                // Fallback for intermediate structure
                equipment = data.materials;
                cases = {};
                materials = data.materials;
                packages = data.packages || {};
            } else {
                // Fallback for oldest structure
                equipment = data;
                cases = {};
                materials = data;
                packages = {};
            }
        }
    } catch (error) {
        console.error('Failed to load materials:', error);
        showError('Material konnte nicht geladen werden');
    }
}

/**
 * Initialize time picker dropdowns with hours and minutes
 */
function initializeTimePickers() {
    const hourSelects = document.querySelectorAll('[id$="_stunde"]');
    const minuteSelects = document.querySelectorAll('[id$="_minute"]');

    // Populate hour options (0-23)
    hourSelects.forEach(select => {
        for (let h = 0; h < 24; h++) {
            const option = document.createElement('option');
            option.value = h.toString().padStart(2, '0');
            option.textContent = h.toString().padStart(2, '0');
            select.appendChild(option);
        }
    });

    // Populate minute options (0, 15, 30, 45)
    minuteSelects.forEach(select => {
        [0, 15, 30, 45].forEach(m => {
            const option = document.createElement('option');
            option.value = m.toString().padStart(2, '0');
            option.textContent = m.toString().padStart(2, '0');
            select.appendChild(option);
        });
    });
}

/**
 * Initialize package selector dropdown
 */
function initializePackageSelector() {
    // Clear existing options except default
    while (packageSelect.options.length > 1) {
        packageSelect.remove(1);
    }

    // Add package options
    for (const packageName of Object.keys(packages)) {
        const option = document.createElement('option');
        option.value = packageName;
        option.textContent = packageName;
        packageSelect.appendChild(option);
    }
}

/**
 * Load selected package into material list
 */
function loadPackage() {
    const selectedPackage = packageSelect.value;
    if (!selectedPackage || !packages[selectedPackage]) {
        showError('Bitte ein Paket auswählen');
        return;
    }

    // Clear existing materials
    document.getElementById('equipmentContainer').innerHTML = '';
    document.getElementById('casesContainer').innerHTML = '';

    // Load package items
    const packageItems = packages[selectedPackage];
    packageItems.forEach(item => {
        addDropdownItem(item.type || 'equipment', item.count, item.text);
    });
}

/**
 * Setup event listeners
 */
function setupEventListeners() {
    // Add equipment button
    document.getElementById('addEquipmentBtn').addEventListener('click', () => addDropdownItem('equipment'));

    // Add case button
    document.getElementById('addCaseBtn').addEventListener('click', () => addDropdownItem('case'));

    // Load package button
    loadPackageBtn.addEventListener('click', loadPackage);

    // Candidate import
    fillFormBtn.addEventListener('click', fillFormFromCandidate);

    // Custom material modal
    addCustomMaterialBtn.addEventListener('click', openCustomMaterialModal);
    document.getElementById('cancelCustomMaterial').addEventListener('click', closeCustomMaterialModal);
    document.getElementById('confirmCustomMaterial').addEventListener('click', addCustomMaterial);

    // Close modal on background click
    document.getElementById('customMaterialBackdrop').addEventListener('click', () => {
        closeCustomMaterialModal();
    });

    // Signature upload
    signaturePreview.addEventListener('click', () => signatureInput.click());
    signatureInput.addEventListener('change', handleSignatureUpload);
    clearSignatureBtn.addEventListener('click', clearSignature);

    // Drag and drop for signature
    signaturePreview.addEventListener('dragover', (e) => {
        e.preventDefault();
        signaturePreview.style.borderColor = 'var(--primary-color)';
    });

    signaturePreview.addEventListener('dragleave', () => {
        if (!signatureData) {
            signaturePreview.style.borderColor = '';
        }
    });

    signaturePreview.addEventListener('drop', (e) => {
        e.preventDefault();
        const file = e.dataTransfer.files[0];
        if (file && file.type === 'image/png') {
            processSignatureFile(file);
        } else {
            showError('Bitte nur PNG-Dateien verwenden');
        }
    });

    // Form submission
    contractForm.addEventListener('submit', handleSubmit);

    // Form validation - listen for changes on all inputs
    const formFields = contractForm.querySelectorAll('input[required], textarea[required], select[required]');
    formFields.forEach(field => {
        field.addEventListener('input', validateForm);
        field.addEventListener('change', validateForm);
    });

    // Also watch for material changes
    const observer = new MutationObserver(validateForm);
    observer.observe(document.getElementById('equipmentContainer'), { childList: true, subtree: true });
    observer.observe(document.getElementById('casesContainer'), { childList: true, subtree: true });

    // Initial validation
    validateForm();
}

/**
 * Validate form and enable/disable submit button
 */
function validateForm() {
    let isValid = true;

    // Check all required HTML fields
    const requiredFields = contractForm.querySelectorAll('input[required], textarea[required], select[required]');
    requiredFields.forEach(field => {
        if (!field.value || !field.value.trim()) {
            isValid = false;
        }
    });

    // Check material selection
    const equipmentSelects = document.querySelectorAll('.equipment-select');
    const caseSelects = document.querySelectorAll('.case-select');
    const customTexts = document.querySelectorAll('.custom-text');
    let hasMaterial = false;
    equipmentSelects.forEach(sel => { if (sel.value) hasMaterial = true; });
    caseSelects.forEach(sel => { if (sel.value) hasMaterial = true; });
    customTexts.forEach(txt => { if (txt.value && txt.value.trim()) hasMaterial = true; });
    if (!hasMaterial) isValid = false;

    // Check signature
    if (!signatureData) isValid = false;

    generateBtn.disabled = !isValid;
}

/**
 * Open custom material modal
 */
function openCustomMaterialModal() {
    customMaterialModal.classList.remove('hidden');
    document.getElementById('customMaterialName').value = '';
    document.getElementById('customMaterialText').value = '';
    document.getElementById('customMaterialCount').value = '1';
    document.getElementById('saveToJson').checked = false;
    document.getElementById('customMaterialName').focus();
}

/**
 * Close custom material modal
 */
function closeCustomMaterialModal() {
    customMaterialModal.classList.add('hidden');
}

/**
 * Add custom material from modal
 */
async function addCustomMaterial() {
    const name = document.getElementById('customMaterialName').value.trim();
    const text = document.getElementById('customMaterialText').value.trim();
    const count = parseInt(document.getElementById('customMaterialCount').value);
    const saveToJson = document.getElementById('saveToJson').checked;

    if (!name || !text) {
        showError('Bitte Bezeichnung und Beschreibung eingeben');
        return;
    }

    // If saving to JSON, call API
    if (saveToJson) {
        try {
            const response = await fetch('/api/materials/add', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, text })
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Speichern fehlgeschlagen');
            }

            // Add to local materials object
            materials[name] = text;

            // Refresh all material dropdowns
            refreshMaterialDropdowns();

            showSuccess(`"${name}" wurde dauerhaft gespeichert`);
        } catch (error) {
            showError(error.message);
            return;
        }
    } else {
        // Add to temporary custom materials
        customMaterials[name] = text;
    }

    // Add the material item to the list
    addCustomMaterialItem(count, name, text);

    closeCustomMaterialModal();
}

/**
 * Add a custom material item (not from dropdown)
 */
function addCustomMaterialItem(quantity, name, text) {
    const item = document.createElement('div');
    item.className = 'material-item custom-material flex items-center gap-3 p-3 bg-primary-50 dark:bg-primary-900/20 border border-primary-200 dark:border-primary-800 rounded-lg';
    item.dataset.customName = name;
    item.dataset.customText = text;

    // Quantity display
    const quantityDisplay = document.createElement('span');
    quantityDisplay.className = 'px-3 py-1 bg-white dark:bg-gray-700 rounded text-sm font-medium text-gray-700 dark:text-gray-300';
    quantityDisplay.textContent = `${quantity} x`;

    // Material name display
    const nameDisplay = document.createElement('span');
    nameDisplay.className = 'flex-1 text-sm text-gray-700 dark:text-gray-300';
    nameDisplay.textContent = text;
    nameDisplay.title = `Eigenes Material: ${name}`;

    // Hidden inputs to store values
    const quantityInput = document.createElement('input');
    quantityInput.type = 'hidden';
    quantityInput.className = 'custom-quantity';
    quantityInput.value = quantity;

    const textInput = document.createElement('input');
    textInput.type = 'hidden';
    textInput.className = 'custom-text';
    textInput.value = text;

    // Remove button
    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'w-8 h-8 flex items-center justify-center text-gray-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 rounded transition-colors duration-150';
    removeBtn.innerHTML = '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>';
    removeBtn.addEventListener('click', () => item.remove());

    item.appendChild(quantityDisplay);
    item.appendChild(nameDisplay);
    item.appendChild(quantityInput);
    item.appendChild(textInput);
    item.appendChild(removeBtn);
    // Add to equipment container by default
    document.getElementById('equipmentContainer').appendChild(item);
}

/**
 * Refresh all material dropdowns after adding a new material
 */
function refreshMaterialDropdowns() {
    // Refresh both equipment and case dropdowns
    ['equipment', 'case'].forEach(type => {
        const selectClass = type === 'equipment' ? '.equipment-select' : '.case-select';
        const dataObj = type === 'equipment' ? equipment : cases;

        const selects = document.querySelectorAll(selectClass);
        selects.forEach(select => {
            const currentValue = select.value;

            // Clear all except default
            while (select.options.length > 1) {
                select.remove(1);
            }

            // Re-add all materials of this type
            for (const [tag, description] of Object.entries(dataObj)) {
                const option = document.createElement('option');
                option.value = tag;
                option.textContent = tag;
                option.title = description;
                select.appendChild(option);
            }

            // Restore selection
            select.value = currentValue;
        });
    });
}

/**
 * Add a dropdown item with quantity selector
 * @param {string} type - 'equipment' or 'case'
 * @param {number} quantity - Pre-selected quantity
 * @param {string} materialKey - Pre-selected material key
 */
function addDropdownItem(type = 'equipment', quantity = 1, materialKey = '') {
    const containerId = type === 'equipment' ? 'equipmentContainer' : 'casesContainer';
    const container = document.getElementById(containerId);
    if (!container) return;

    const dataObj = type === 'equipment' ? equipment : cases;

    const item = document.createElement('div');
    item.className = 'material-item flex items-center gap-3 p-3 bg-gray-50 dark:bg-gray-700/50 border border-gray-200 dark:border-gray-600 rounded-lg';

    // Quantity selector
    const quantitySelect = document.createElement('select');
    quantitySelect.className = 'quantity-select w-20 px-3 py-2 bg-white dark:bg-gray-700 border border-gray-300 dark:border-gray-600 rounded-lg text-sm text-gray-700 dark:text-gray-300 focus:outline-none focus:ring-2 focus:ring-primary-500';
    for (let i = 1; i <= 20; i++) {
        const option = document.createElement('option');
        option.value = i;
        option.textContent = `${i} x`;
        if (i === quantity) {
            option.selected = true;
        }
        quantitySelect.appendChild(option);
    }

    // Material selector
    const select = document.createElement('select');
    const selectClass = type === 'equipment' ? 'equipment-select' : 'case-select';
    select.className = `${selectClass} flex-1 px-3 py-2 bg-white dark:bg-gray-700 border border-gray-300 dark:border-gray-600 rounded-lg text-sm text-gray-700 dark:text-gray-300 focus:outline-none focus:ring-2 focus:ring-primary-500`;
    select.required = container.children.length === 0 && !materialKey;

    // Add default option
    const defaultOption = document.createElement('option');
    defaultOption.value = '';
    defaultOption.textContent = type === 'equipment' ? 'Equipment auswählen...' : 'Case auswählen...';
    select.appendChild(defaultOption);

    // Add material options
    for (const [tag, description] of Object.entries(dataObj)) {
        const option = document.createElement('option');
        option.value = tag;
        option.textContent = tag;
        option.title = description;
        if (tag === materialKey) {
            option.selected = true;
        }
        select.appendChild(option);
    }

    // Remove button
    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'w-8 h-8 flex items-center justify-center text-gray-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 rounded transition-colors duration-150';
    removeBtn.innerHTML = '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>';
    removeBtn.addEventListener('click', () => {
        item.remove();
        // Make first item required if it's the only one
        const remaining = container.querySelectorAll(`.${selectClass}`);
        if (remaining.length === 1) {
            remaining[0].required = true;
        }
    });

    item.appendChild(quantitySelect);
    item.appendChild(select);
    item.appendChild(removeBtn);
    container.appendChild(item);
}

/**
 * Handle signature file upload
 */
function handleSignatureUpload(e) {
    const file = e.target.files[0];
    if (file) {
        processSignatureFile(file);
    }
}

/**
 * Process signature file
 */
function processSignatureFile(file) {
    const reader = new FileReader();
    reader.onload = (e) => {
        signatureData = e.target.result;

        // Show preview
        signaturePreview.innerHTML = `<img src="${signatureData}" alt="Unterschrift" class="max-w-full max-h-[150px] object-contain p-4">`;
        signaturePreview.classList.remove('border-dashed');
        signaturePreview.classList.add('border-solid', 'border-primary-500');
        clearSignatureBtn.classList.remove('hidden');
        validateForm();
    };
    reader.readAsDataURL(file);
}

/**
 * Clear signature
 */
function clearSignature() {
    signatureData = null;
    signatureInput.value = '';
    validateForm();
    signaturePreview.innerHTML = `
        <div class="text-center text-gray-500 dark:text-gray-400 p-6">
            <svg class="w-10 h-10 mx-auto mb-2 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"></path>
            </svg>
            <p class="text-sm">PNG-Datei hierher ziehen oder klicken zum Auswählen</p>
        </div>
    `;
    signaturePreview.classList.remove('border-solid', 'border-primary-500');
    signaturePreview.classList.add('border-dashed');
    clearSignatureBtn.classList.add('hidden');
}

/**
 * Get time from hour and minute selects
 */
function getTimeFromSelects(hourId, minuteId) {
    const hour = document.getElementById(hourId).value;
    const minute = document.getElementById(minuteId).value;
    if (hour && minute) {
        return `${hour}:${minute}`;
    }
    return '';
}

/**
 * Format date and time for display
 */
function formatDateTimeFromParts(dateValue, timeValue) {
    if (!dateValue) return '';
    // Parse ISO date parts directly to avoid timezone shift
    const [year, month, day] = dateValue.split('-');
    const formattedDate = `${day}.${month}.${year}`;

    if (timeValue) {
        return `${formattedDate}, ${timeValue} Uhr`;
    }
    return formattedDate;
}

/**
 * Format date for display
 */
function formatDate(dateValue) {
    if (!dateValue) return '';
    // Parse ISO date parts directly to avoid timezone shift
    const [year, month, day] = dateValue.split('-');
    return `${day}.${month}.${year}`;
}

/**
 * Get selected materials as formatted text with quantities
 */
function getSelectedMaterials() {
    const selectedItems = [];

    // Custom items
    document.querySelectorAll('.custom-material').forEach(item => {
        const quantity = item.querySelector('.custom-quantity').value;
        const text = item.querySelector('.custom-text').value;
        if (text) selectedItems.push(`${quantity} x ${text}`);
    });

    // Equipment items
    document.querySelectorAll('.equipment-select').forEach(select => {
        if (select.value && equipment[select.value]) {
            const quantity = select.parentElement.querySelector('.quantity-select').value;
            selectedItems.push(`${quantity} x ${equipment[select.value]}`);
        }
    });

    // Case items
    document.querySelectorAll('.case-select').forEach(select => {
        if (select.value && cases[select.value]) {
            const quantity = select.parentElement.querySelector('.quantity-select').value;
            selectedItems.push(`${quantity} x ${cases[select.value]}`);
        }
    });

    return selectedItems.join('\n');
}

/**
 * Handle form submission
 */
async function handleSubmit(e) {
    e.preventDefault();

    // Validate all required HTML fields first
    if (!contractForm.reportValidity()) {
        return;
    }

    // Validate that at least one material is selected
    const materialItems = document.querySelectorAll('.equipment-select, .case-select, .custom-text');
    let hasMaterial = false;
    materialItems.forEach(item => {
        if (item.value && item.value.trim()) hasMaterial = true;
    });
    if (!hasMaterial) {
        showError('Bitte mindestens ein Material auswählen.');
        return;
    }

    // Validate signature
    if (!signatureData) {
        showError('Bitte eine Unterschrift hochladen.');
        return;
    }

    // Disable submit button and show loading
    generateBtn.disabled = true;
    generateBtn.classList.add('hidden');
    loadingIndicator.classList.remove('hidden');

    try {
        const formData = {
            vorname_nachname: document.getElementById('vorname_nachname').value,
            privatanschrift: document.getElementById('privatanschrift').value,
            rechnungsanschrift: document.getElementById('rechnungsanschrift').value.trim()
                || document.getElementById('privatanschrift').value,
            abholdatum: formatDateTimeFromParts(
                document.getElementById('abholdatum').value,
                getTimeFromSelects('abholzeit_stunde', 'abholzeit_minute')
            ),
            rueckgabedatum: formatDateTimeFromParts(
                document.getElementById('rueckgabedatum').value,
                getTimeFromSelects('rueckgabezeit_stunde', 'rueckgabezeit_minute')
            ),
            veranstaltungsname: document.getElementById('veranstaltungsname').value,
            veranstaltungsdatum: formatDate(document.getElementById('veranstaltungsdatum').value),
            veranstaltungsort: document.getElementById('veranstaltungsort').value,
            material: getSelectedMaterials(),
            signature: signatureData
        };

        const response = await fetch('/api/generate', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': document.querySelector('meta[name="csrf-token"]').content
            },
            body: JSON.stringify(formData)
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || 'PDF-Generierung fehlgeschlagen');
        }

        // Download the PDF
        const blob = await response.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;

        // Get filename from Content-Disposition header or use default
        const contentDisposition = response.headers.get('Content-Disposition');
        let filename = 'Leihvertrag.pdf';
        if (contentDisposition) {
            const match = contentDisposition.match(/filename="(.+)"/);
            if (match) {
                filename = match[1];
            }
        }

        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        window.URL.revokeObjectURL(url);

        // If a candidate was selected, mark it as done
        const candidateId = taskSelect.value;
        if (candidateId) {
            try {
                await fetch(`/api/emails/candidates/${candidateId}/mark-done`, {
                    method: 'PUT',
                    headers: {
                        'X-CSRFToken': document.querySelector('meta[name="csrf-token"]').content
                    }
                });
                // Refresh candidates list to remove the done one
                await loadCandidatesForContract();
                showSuccess('PDF generiert und Anfrage als "Erledigt" markiert.');
            } catch (e) {
                console.error('Error marking candidate as done:', e);
                showError('PDF generiert, aber Fehler beim Markieren der Anfrage als "Erledigt".');
            }
        }

    } catch (error) {
        console.error('Error generating PDF:', error);
        showError(error.message);
    } finally {
        generateBtn.disabled = false;
        generateBtn.classList.remove('hidden');
        loadingIndicator.classList.add('hidden');
    }
}

/**
 * Show error toast
 */
function showError(message) {
    errorMessage.textContent = message;
    errorToast.classList.remove('hidden');
    errorToast.classList.add('show');

    setTimeout(() => {
        errorToast.classList.remove('show');
        setTimeout(() => {
            errorToast.classList.add('hidden');
        }, 300);
    }, 5000);
}

/**
 * Show success toast
 */
function showSuccess(message) {
    // Reuse error toast with different styling
    errorMessage.textContent = message;
    errorToast.classList.remove('error-toast');
    errorToast.classList.add('success-toast');
    errorToast.classList.remove('hidden');
    errorToast.classList.add('show');

    setTimeout(() => {
        errorToast.classList.remove('show');
        setTimeout(() => {
            errorToast.classList.add('hidden');
            errorToast.classList.remove('success-toast');
            errorToast.classList.add('error-toast');
        }, 300);
    }, 3000);
}

// ===========================
// Candidate Import Functions
// ===========================

/**
 * Load processed candidates for contract creation
 */
async function loadCandidatesForContract() {
    try {
        const response = await fetch('/api/emails/candidates/for-contract');

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || 'Laden fehlgeschlagen');
        }

        candidateTasks = await response.json();

        // Populate task dropdown
        taskSelect.innerHTML = '<option value="">Leihanfrage auswählen...</option>';

        if (candidateTasks.length === 0) {
            const option = document.createElement('option');
            option.value = '';
            option.textContent = 'Keine offenen Leihanfragen';
            option.disabled = true;
            taskSelect.appendChild(option);
            return;
        }

        candidateTasks.forEach(candidate => {
            const option = document.createElement('option');
            option.value = candidate.id;
            const name = candidate.vorname_nachname || 'Unbekannt';
            const event = candidate.veranstaltungsname || 'Keine Veranstaltung';
            option.textContent = `${name} – ${event}`;
            taskSelect.appendChild(option);
        });

    } catch (error) {
        console.error('Candidate load error:', error);
    }
}

/**
 * Format address string: normalize escaped newlines, clean up whitespace.
 * If the address is a single line with commas, split into multi-line.
 * Expected format: Straße\nPLZ Ort\nLand
 */
function formatAnschrift(raw) {
    if (!raw) return '';
    // Replace escaped newline sequences with actual newlines
    let addr = raw.replace(/\\n/g, '\n');
    // Split by newlines first
    let lines = addr.split('\n').map(l => l.trim()).filter(l => l.length > 0);
    // If it's a single line with commas, split by commas into separate lines
    if (lines.length === 1 && lines[0].includes(',')) {
        lines = lines[0].split(',').map(l => l.trim()).filter(l => l.length > 0);
    }
    return lines.join('\n');
}

/**
 * Fill form from selected candidate
 */
function fillFormFromCandidate() {
    const candidateId = taskSelect.value;

    if (!candidateId) {
        showImportStatus('Bitte eine Leihanfrage auswählen', 'error');
        return;
    }

    const candidate = candidateTasks.find(c => c.id == candidateId);
    if (!candidate) {
        showImportStatus('Kandidat nicht gefunden', 'error');
        return;
    }

    // ===== CLEAR ALL FIELDS FIRST =====
    document.getElementById('vorname_nachname').value = '';
    document.getElementById('privatanschrift').value = '';
    document.getElementById('rechnungsanschrift').value = '';
    document.getElementById('veranstaltungsname').value = '';
    document.getElementById('veranstaltungsort').value = '';
    document.getElementById('veranstaltungsdatum').value = '';
    document.getElementById('abholdatum').value = '';
    document.getElementById('abholzeit_stunde').value = '';
    document.getElementById('abholzeit_minute').value = '';
    document.getElementById('rueckgabedatum').value = '';
    document.getElementById('rueckgabezeit_stunde').value = '';
    document.getElementById('rueckgabezeit_minute').value = '';
    // Clear materials
    document.getElementById('equipmentContainer').innerHTML = '';
    document.getElementById('casesContainer').innerHTML = '';

    // ===== FILL FROM CANDIDATE =====
    document.getElementById('vorname_nachname').value = candidate.vorname_nachname || '';
    document.getElementById('veranstaltungsname').value = candidate.veranstaltungsname || '';
    document.getElementById('veranstaltungsort').value = candidate.veranstaltungsort || '';
    document.getElementById('privatanschrift').value = formatAnschrift(candidate.anschrift || '');

    // Fill date fields
    const dateIso = candidate.datum_iso || '';
    if (dateIso) {
        document.getElementById('veranstaltungsdatum').value = dateIso;

        // Auto-fill rental period based on event date
        document.getElementById('abholdatum').value = dateIso;
        document.getElementById('abholzeit_stunde').value = '12';
        document.getElementById('abholzeit_minute').value = '00';

        // Rückgabedatum: Next day at 18:00
        const eventDate = new Date(dateIso);
        eventDate.setDate(eventDate.getDate() + 1);
        const nextDay = eventDate.toISOString().split('T')[0];
        document.getElementById('rueckgabedatum').value = nextDay;
        document.getElementById('rueckgabezeit_stunde').value = '18';
        document.getElementById('rueckgabezeit_minute').value = '00';
    }

    // Add materials from tags
    if (candidate.tags && candidate.tags.length > 0) {
        // Collect unknown tags for mapping
        const unknownTags = [];
        const knownTags = [];
        const foundPackages = [];

        candidate.tags.forEach(tag => {
            const matchedMaterialKey = Object.keys(materials).find(
                key => key.toLowerCase() === tag.toLowerCase()
            );

            const matchedPackageKey = Object.keys(packages).find(
                key => key.toLowerCase() === tag.toLowerCase()
            );

            if (matchedMaterialKey) {
                knownTags.push(matchedMaterialKey);
            } else if (matchedPackageKey) {
                foundPackages.push(matchedPackageKey);
            } else {
                unknownTags.push(tag);
            }
        });

        // Add known materials
        knownTags.forEach(key => addDropdownItem(cases[key] ? 'case' : 'equipment', 1, key));

        // Add packages (expanded)
        foundPackages.forEach(pkgKey => {
            const packageItems = packages[pkgKey];
            packageItems.forEach(item => {
                const itemType = item.type || 'equipment';
                addDropdownItem(itemType, item.count, item.text);
            });
        });

        // Process unknown tags with popup
        if (unknownTags.length > 0) {
            processUnknownTags(unknownTags);
        } else if (document.getElementById('equipmentContainer').children.length === 0 && document.getElementById('casesContainer').children.length === 0) {
            addDropdownItem('equipment');
            addDropdownItem('case');
        }
    } else {
        // No tags — add one empty row for each
        addDropdownItem('equipment');
        addDropdownItem('case');
    }

    const displayName = candidate.veranstaltungsname || candidate.vorname_nachname || 'Leihanfrage';
    showImportStatus(`"${displayName}" übernommen`, 'success');
    validateForm();
}

/**
 * Show import status message
 */
function showImportStatus(message, type) {
    importStatus.textContent = message;
    // Reset classes but keep base styling
    importStatus.className = 'mt-3 px-4 py-2 rounded-lg text-sm';

    // Add type-specific classes for Tailwind
    if (type === 'success') {
        importStatus.classList.add('bg-green-100', 'dark:bg-green-900/30', 'text-green-700', 'dark:text-green-400');
    } else if (type === 'error') {
        importStatus.classList.add('bg-red-100', 'dark:bg-red-900/30', 'text-red-700', 'dark:text-red-400');
    } else {
        importStatus.classList.add('bg-blue-100', 'dark:bg-blue-900/30', 'text-blue-700', 'dark:text-blue-400');
    }

    // Auto-hide after delay
    if (type === 'success' || type === 'info') {
        setTimeout(() => {
            importStatus.classList.add('hidden');
        }, 5000);
    }
}

// ===========================
// Tag Mapping Functions
// ===========================

let tagMappingQueue = [];
let currentMappingTag = null;

const tagMappingModal = document.getElementById('tagMappingModal');
const tagMappingTagName = document.getElementById('tagMappingTagName');
const tagMappingDescription = document.getElementById('tagMappingDescription');
const skipTagMappingBtn = document.getElementById('skipTagMapping');
const confirmTagMappingBtn = document.getElementById('confirmTagMapping');

// Set up event listeners for tag mapping modal
if (skipTagMappingBtn) {
    skipTagMappingBtn.addEventListener('click', skipCurrentTag);
}
if (confirmTagMappingBtn) {
    confirmTagMappingBtn.addEventListener('click', confirmTagMapping);
}

// Close tag mapping modal on background click
const tagMappingBackdrop = document.getElementById('tagMappingBackdrop');
if (tagMappingBackdrop) {
    tagMappingBackdrop.addEventListener('click', skipCurrentTag);
}

/**
 * Process queue of unknown tags
 */
function processUnknownTags(tags) {
    tagMappingQueue = [...tags];
    processNextTag();
}

/**
 * Process next tag in queue
 */
function processNextTag() {
    if (tagMappingQueue.length === 0) {
        // All tags processed, ensure at least one material item
        const eqCount = document.getElementById('equipmentContainer').children.length;
        const caseCount = document.getElementById('casesContainer').children.length;
        if (eqCount === 0 && caseCount === 0) {
            addDropdownItem('equipment');
            addDropdownItem('case');
        }
        return;
    }

    currentMappingTag = tagMappingQueue.shift();
    tagMappingTagName.textContent = currentMappingTag;
    tagMappingDescription.value = '';
    tagMappingDescription.placeholder = `z.B. ${currentMappingTag} (Beschreibung)`;
    tagMappingModal.classList.remove('hidden');
    tagMappingDescription.focus();
}

/**
 * Skip current tag without mapping
 */
function skipCurrentTag() {
    tagMappingModal.classList.add('hidden');
    currentMappingTag = null;
    processNextTag();
}

/**
 * Confirm tag mapping - save to JSON and add material
 */
async function confirmTagMapping() {
    const description = tagMappingDescription.value.trim();

    if (!description) {
        showError('Bitte eine Beschreibung eingeben');
        return;
    }

    const tagName = currentMappingTag;

    try {
        // Save to JSON via API
        const response = await fetch('/api/materials/add', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': document.querySelector('meta[name="csrf-token"]').content
            },
            body: JSON.stringify({ name: tagName, text: description })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || 'Speichern fehlgeschlagen');
        }

        // Add to local materials (equipment by default for newly mapped tags)
        equipment[tagName] = description;
        materials[tagName] = description;

        // Add material item
        addDropdownItem('equipment', 1, tagName);

        // Refresh dropdowns
        refreshMaterialDropdowns();

        showSuccess(`"${tagName}" gespeichert`);

    } catch (error) {
        showError(error.message);
    }

    tagMappingModal.classList.add('hidden');
    currentMappingTag = null;
    processNextTag();
}
