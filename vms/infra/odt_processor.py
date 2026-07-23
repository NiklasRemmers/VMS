"""
ODT Processor Module
Handles ODT template processing, placeholder replacement, and signature insertion.
"""

import os
import re
import shutil
import tempfile
import zipfile
from typing import Dict, Optional


def process_odt_template(
    template_path: str,
    output_path: str,
    replacements: Dict[str, str],
    signature_path: Optional[str] = None,
    row_items: Optional[list] = None
) -> str:
    """
    Process an ODT template by replacing placeholders and inserting signature.

    Args:
        template_path: Path to the ODT template file
        output_path: Path where the processed ODT will be saved
        replacements: Dictionary mapping placeholder names to replacement values
        signature_path: Optional path to signature PNG file
        row_items: Optional list of item dicts (name, quantity, price, unit) used
            to expand the invoice article table row (one row per item) before the
            global replacements are applied.

    Returns:
        Path to the processed ODT file
    """
    # Create a temporary directory for extraction
    with tempfile.TemporaryDirectory() as temp_dir:
        # Extract OTD (it's a ZIP archive)
        with zipfile.ZipFile(template_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)

        # Read and process content.xml
        content_xml_path = os.path.join(temp_dir, 'content.xml')
        with open(content_xml_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Expand the per-item article table row (if requested) before the
        # single global replacements run.
        if row_items is not None:
            content = _expand_item_rows(content, row_items)

        # Build the layout-constrained blocks before any placeholder is filled in,
        # so the generated markup takes part in the normal replacement pass.
        content = _expand_signature_block(content)
        content = _autogrow_text_frames(content)
        content = _apply_keep_together(content)

        # Replace placeholders
        content = replace_placeholders(content, replacements)

        # Insert signature if provided
        if signature_path and os.path.exists(signature_path):
            content = insert_signature(content, temp_dir, signature_path)
        else:
            # Without a signature the placeholder would otherwise be printed verbatim.
            content = content.replace('#UNTERSCHRIFT#', '')

        # Write modified content.xml
        with open(content_xml_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        # Update manifest if signature was added
        if signature_path and os.path.exists(signature_path):
            update_manifest(temp_dir)
        
        # Create new ODT file
        create_odt_from_directory(temp_dir, output_path)
        
    return output_path


SIGNATURE_BLOCK_PLACEHOLDER = '#UNTERSCHRIFTSBLOCK#'
KEEP_TOGETHER_START = '#ZUSAMMEN_START#'
KEEP_TOGETHER_END = '#ZUSAMMEN_ENDE#'

# Styles for the generated signature block. The table must never split across a
# page, and both columns are pinned to fixed cells so the underline and the text
# beneath it always start at the same x position — regardless of how long the
# names substituted into them turn out to be.
_SIGNATURE_STYLES = (
    '<style:style style:name="TSig" style:family="table">'
    '<style:table-properties style:rel-width="100%" table:align="margins" '
    'style:may-break-between-rows="false"/></style:style>'
    '<style:style style:name="TSig.col" style:family="table-column">'
    '<style:table-column-properties style:rel-column-width="1*"/></style:style>'
    '<style:style style:name="TSig.row" style:family="table-row">'
    '<style:table-row-properties fo:keep-together="always"/></style:style>'
    '<style:style style:name="TSig.cell" style:family="table-cell">'
    '<style:table-cell-properties fo:padding="0cm" fo:border="none"/></style:style>'
    '<style:style style:name="PSig" style:family="paragraph" '
    'style:parent-style-name="Standard">'
    '<style:paragraph-properties fo:margin-left="0cm" fo:margin-right="0cm" '
    'fo:text-indent="0cm" fo:text-align="start" fo:keep-with-next="always"/>'
    '<style:text-properties fo:font-size="11pt" style:font-size-asian="11pt" '
    'style:font-size-complex="11pt"/></style:style>'
    '<style:style style:name="TSigName" style:family="text">'
    '<style:text-properties fo:font-weight="bold" style:font-weight-asian="bold" '
    'style:font-weight-complex="bold"/></style:style>'
)


def _inject_styles(content: str, styles: str) -> str:
    """Add automatic styles to the document, skipping any that are already there."""
    missing = []
    for style in re.findall(r'<style:style .*?</style:style>', styles, re.DOTALL):
        name = re.search(r'style:name="([^"]*)"', style).group(1)
        if f'style:name="{name}"' not in content:
            missing.append(style)
    if not missing:
        return content
    return content.replace('</office:automatic-styles>',
                           ''.join(missing) + '</office:automatic-styles>')


def _signature_block_xml() -> str:
    """Build the two-column signature table that replaces #UNTERSCHRIFTSBLOCK#.

    The placeholders inside it (#HEUTE#, #VERLEIHER#, #VORNAME NACHNAME#,
    #UNTERSCHRIFT#) are filled in by the regular replacement pass that runs
    afterwards.
    """
    def p(inner=''):
        return f'<text:p text:style-name="PSig">{inner}</text:p>'

    def cell(inner):
        return ('<table:table-cell table:style-name="TSig.cell" office:value-type="string">'
                + inner + '</table:table-cell>')

    def row(left, right):
        return ('<table:table-row table:style-name="TSig.row">'
                + cell(left) + cell(right) + '</table:table-row>')

    def named(label, placeholder):
        return (f'{label} <text:span text:style-name="TSigName">{placeholder}</text:span>')

    date = 'Ulm, den #HEUTE#'
    line = '_' * 27

    return (
        '<table:table table:name="Unterschriften" table:style-name="TSig">'
        '<table:table-column table:style-name="TSig.col" table:number-columns-repeated="2"/>'
        + row(p(date), p(date))
        + row(p(), p())
        + row(p('#UNTERSCHRIFT#'), p())
        + row(p(line), p(line))
        + row(p(named('Für die StuVe:', '#VERLEIHER#')),
              p(named('Für den Entleiher:', '#VORNAME NACHNAME#')))
        + '</table:table>'
        # A table must not be the last element in the body, and this restores the
        # trailing spacing the original block had.
        + '<text:p text:style-name="PSig"/>'
    )


def _expand_signature_block(content: str) -> str:
    """Replace the #UNTERSCHRIFTSBLOCK# paragraph with the generated table.

    Anchoring on the placeholder — rather than on LibreOffice's auto-generated
    style names, which change on every save — is what lets uploaded templates
    keep the layout guarantees without containing the table themselves.
    """
    content = normalize_fragmented_placeholders(content)
    if SIGNATURE_BLOCK_PLACEHOLDER not in content:
        return content

    block = _signature_block_xml()
    # Swallow the whole enclosing <text:p>, otherwise the table would be nested
    # inside a paragraph, which ODF does not allow.
    pattern = re.compile(
        r'<text:p\b[^>]*>(?:(?!</text:p>).)*?'
        + re.escape(SIGNATURE_BLOCK_PLACEHOLDER)
        + r'(?:(?!</text:p>).)*?</text:p>',
        re.DOTALL,
    )
    content, replaced = pattern.subn(lambda _m: block, content)
    if not replaced:
        content = content.replace(SIGNATURE_BLOCK_PLACEHOLDER, block)

    return _inject_styles(content, _SIGNATURE_STYLES)


# A frame open tag followed by its text box, e.g. the "vertreten durch #VERLEIHER#"
# box in the Leihvertrag. Only frames shaped like this are auto-grown.
_TEXT_FRAME_RE = re.compile(
    r'(<draw:frame\b[^>]*>)(\s*)(<draw:text-box\b[^>]*?>)', re.DOTALL
)


def _autogrow_text_frames(content: str) -> str:
    """Let paragraph-anchored text frames grow with the text put into them.

    The templates draw the contracting parties in fixed-height text boxes sized
    around the placeholder, not around the value. A #VERLEIHER# or address that
    wraps to one line more than the author's sample overflows the box, and
    the box clips it mid-glyph.

    Converting the fixed svg:height into fo:min-height on the text box makes the
    frame grow with its content instead. The frames keep their "run-through"
    wrap: the paragraphs below them sit at fixed flow positions with a few lines
    of slack, and making them yield instead would reserve each frame's full
    height and push the rest of the contract onto the next page. Frames
    positioned relative to the *page* — the letterhead and address blocks — are
    left alone: they are meant to sit at a fixed spot.
    """
    # Styles that place their frame relative to the running text, not the page.
    flow_styles = {
        m.group(1)
        for m in re.finditer(
            r'<style:style style:name="([^"]*)" style:family="graphic".*?</style:style>',
            content, re.DOTALL,
        )
        if 'style:vertical-rel="paragraph"' in m.group(0)
    }
    if not flow_styles:
        return content

    def convert(match):
        frame_tag, gap, box_tag = match.groups()
        style = re.search(r'draw:style-name="([^"]*)"', frame_tag)
        height = re.search(r'\ssvg:height="([^"]*)"', frame_tag)
        if not style or not height or style.group(1) not in flow_styles:
            return match.group(0)

        frame_tag = frame_tag.replace(height.group(0), '')
        # The box keeps the authored height as a floor, so a short value still
        # occupies the space the layout was designed around.
        if 'fo:min-height' not in box_tag:
            box_tag = box_tag.replace(
                '<draw:text-box', f'<draw:text-box fo:min-height="{height.group(1)}"', 1
            )
        return frame_tag + gap + box_tag

    return _TEXT_FRAME_RE.sub(convert, content)


def _ensure_keep_style(content: str, parent_style: str):
    """Return (content, style_name) for a keep-with-next variant of parent_style."""
    keep_style_name = f'{parent_style}_keep'
    if f'style:name="{keep_style_name}"' not in content:
        keep_style_def = (
            f'<style:style style:name="{keep_style_name}" style:family="paragraph" '
            f'style:parent-style-name="{parent_style}">'
            f'<style:paragraph-properties fo:keep-with-next="always"/>'
            f'</style:style>'
        )
        content = content.replace(
            '</office:automatic-styles>',
            keep_style_def + '</office:automatic-styles>'
        )
    return content, keep_style_name


_PARAGRAPH_RE = re.compile(r'<text:p\b[^>]*/>|<text:p\b[^>]*>.*?</text:p>', re.DOTALL)


def _paragraph_text(paragraph: str) -> str:
    """Strip all XML tags from a paragraph, leaving its visible text."""
    return re.sub(r'<[^>]*>', '', paragraph).strip()


def _set_keep_with_next(content: str, paragraph: str):
    """Return (content, paragraph) with the paragraph switched to a keep style."""
    open_tag = re.match(r'<text:p\b[^>]*?/?>', paragraph).group(0)
    style_match = re.search(r'text:style-name="([^"]*)"', open_tag)
    parent_style = style_match.group(1) if style_match else 'Standard'

    content, keep_style_name = _ensure_keep_style(content, parent_style)

    if style_match:
        new_open = open_tag.replace(
            f'text:style-name="{parent_style}"',
            f'text:style-name="{keep_style_name}"',
        )
    else:
        new_open = open_tag.replace('<text:p', f'<text:p text:style-name="{keep_style_name}"', 1)

    return content, new_open + paragraph[len(open_tag):]


def _apply_keep_together(content: str) -> str:
    """Keep everything between #ZUSAMMEN_START# and #ZUSAMMEN_ENDE# on one page.

    Every paragraph in the range but the last gets fo:keep-with-next, which
    chains them together. The markers themselves are removed: a paragraph that
    holds nothing but a marker disappears entirely, otherwise just the marker
    text is stripped so surrounding text survives.
    """
    content = normalize_fragmented_placeholders(content)

    # Bounded loop — each pass consumes one marker pair.
    while KEEP_TOGETHER_START in content and KEEP_TOGETHER_END in content:
        start = content.index(KEEP_TOGETHER_START)
        end = content.index(KEEP_TOGETHER_END, start)
        if end < start:
            break

        # Widen to whole paragraphs so we never cut a tag in half.
        region_start = content.rfind('<text:p', 0, start)
        region_end = content.find('</text:p>', end)
        if region_start == -1 or region_end == -1:
            break
        region_end += len('</text:p>')

        region = content[region_start:region_end]
        paragraphs = _PARAGRAPH_RE.findall(region)
        if not paragraphs:
            break

        rebuilt = []
        for paragraph in paragraphs:
            cleaned = paragraph.replace(KEEP_TOGETHER_START, '').replace(KEEP_TOGETHER_END, '')
            # A paragraph that only carried a marker leaves no blank line behind.
            if _paragraph_text(paragraph) in (KEEP_TOGETHER_START, KEEP_TOGETHER_END):
                continue
            rebuilt.append(cleaned)

        for i in range(len(rebuilt) - 1):
            content, rebuilt[i] = _set_keep_with_next(content, rebuilt[i])

        # _set_keep_with_next may have inserted styles, so re-locate the region.
        region_start = content.index(region)
        content = content[:region_start] + ''.join(rebuilt) + content[region_start + len(region):]

    return content


def normalize_fragmented_placeholders(content: str) -> str:
    """
    Normalize placeholders that LibreOffice split across multiple XML tags.

    LibreOffice sometimes splits placeholders across multiple XML tags like:
    <text:span>#</text:span>PLACEHOLDER<text:span>#</text:span>

    This collapses those back into a clean ``#PLACEHOLDER#`` token.
    """
    # Find potential fragmented placeholders (# followed by content with possible tags, ending with #)
    fragmented_pattern = r'#(?:<[^>]*>)*([A-ZÄÖÜ][A-ZÄÖÜ0-9_ ]*?)(?:<[^>]*>)*#'
    return re.sub(fragmented_pattern, lambda m: f'#{m.group(1)}#', content)


def replace_placeholders(content: str, replacements: Dict[str, str]) -> str:
    """
    Replace placeholders in content, handling fragmented XML tags.

    This function handles both simple and fragmented cases.
    """
    # First, normalize fragmented placeholders
    content = normalize_fragmented_placeholders(content)

    # Now replace all placeholders
    for placeholder, value in replacements.items():
        # Ensure placeholder has # markers
        if not placeholder.startswith('#'):
            placeholder = f'#{placeholder}#'

        # Coerce missing/null values to an empty string so a None never reaches
        # the XML replacement logic (which would crash on `'\n' in None` /
        # escape_xml(None)). This keeps the PDF blank at that spot instead.
        if value is None:
            value = ''
        elif not isinstance(value, str):
            value = str(value)

        if '\n' in value:
            # Multi-line value: replace the entire <text:p> element containing
            # the placeholder with separate <text:p> elements per line.
            # This avoids inheriting tab stops that break formatting.
            lines = value.split('\n')
            
            # Find the enclosing <text:p ...>...</text:p> around the placeholder
            pattern = r'(<text:p[^>]*>)([^<]*?' + re.escape(placeholder) + r'[^<]*?)(</text:p>)'
            match = re.search(pattern, content)
            
            if match:
                open_tag = match.group(1)
                close_tag = match.group(3)
                
                # Extract original style name for keep-with-next variant
                style_match = re.search(r'text:style-name="([^"]*)"', open_tag)
                parent_style = style_match.group(1) if style_match else 'Standard'
                keep_style_name = f'{parent_style}_keep'
                keep_tag = open_tag.replace(
                    f'text:style-name="{parent_style}"',
                    f'text:style-name="{keep_style_name}"'
                )
                
                # Split into blocks (separated by empty lines)
                # and use keep-with-next for lines within the same block
                replacement_paragraphs = []
                blocks = _split_into_blocks(lines)
                
                for block in blocks:
                    for i, line in enumerate(block):
                        escaped_line = escape_xml(line)
                        is_last_in_block = (i == len(block) - 1)
                        if is_last_in_block or not line.strip():
                            # Last line of block or empty line: normal style
                            replacement_paragraphs.append(f'{open_tag}{escaped_line}{close_tag}')
                        else:
                            # Inner line: keep with next paragraph
                            replacement_paragraphs.append(f'{keep_tag}{escaped_line}{close_tag}')
                
                content = content[:match.start()] + ''.join(replacement_paragraphs) + content[match.end():]
                
                # Inject the keep-with-next style — only now that the splice above
                # is done, since inserting styles shifts every offset in `match`.
                content, _ = _ensure_keep_style(content, parent_style)
            else:
                # Fallback: simple replacement with line breaks
                escaped_value = escape_xml(value)
                content = content.replace(placeholder, escaped_value)
        else:
            # Single-line value: simple replacement
            escaped_value = escape_xml(value)
            content = content.replace(placeholder, escaped_value)
    
    return content


def format_money_de(value) -> str:
    """Format a number as a German amount, e.g. 1234.5 -> '1.234,50' (no currency)."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = 0.0
    # Format with thousands sep and 2 decimals, then swap separators to German.
    s = f"{amount:,.2f}"  # e.g. '1,234.50'
    return s.replace(',', '#').replace('.', ',').replace('#', '.')


def _expand_item_rows(content: str, items: list) -> str:
    """
    Expand the single article table row of an invoice/rebooking template into one
    row per item.

    The templates contain exactly one ``<table:table-row>`` whose cells hold the
    per-item placeholders ``#ARTIKEL#``, ``#MENGE#``, ``#STÜCKPREIS#`` and
    ``#GESAMTPREIS_POS#``. This function locates that row, duplicates it for each
    item with the per-item values filled in, and splices the result back in.
    Global placeholders (e.g. ``#GESAMTPREIS#``) are left untouched.
    """
    # Ensure placeholders are not fragmented across XML tags before matching.
    content = normalize_fragmented_placeholders(content)

    # Locate the table row that carries the item placeholder (non-greedy so we
    # match a single <table:table-row>...</table:table-row>).
    row_pattern = re.compile(
        r'<table:table-row\b[^>]*>(?:(?!</table:table-row>).)*?#ARTIKEL#.*?</table:table-row>',
        re.DOTALL,
    )
    match = row_pattern.search(content)
    if not match:
        # Nothing to expand — return content unchanged.
        return content

    row_template = match.group(0)

    rendered_rows = []
    for item in (items or []):
        name = item.get('name') or ''
        try:
            quantity = int(item.get('quantity', 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Ungültige Menge für Position {name!r}: {item.get('quantity')!r}"
            ) from exc
        try:
            price = float(item.get('price', 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Ungültiger Preis für Position {name!r}: {item.get('price')!r}"
            ) from exc
        pos_total = price * quantity

        row = row_template
        row = row.replace('#ARTIKEL#', escape_xml(str(name)))
        row = row.replace('#MENGE#', escape_xml(str(quantity)))
        row = row.replace('#STÜCKPREIS#', escape_xml(format_money_de(price)))
        row = row.replace('#GESAMTPREIS_POS#', escape_xml(format_money_de(pos_total)))
        rendered_rows.append(row)

    replacement = ''.join(rendered_rows)
    return content[:match.start()] + replacement + content[match.end():]


def _split_into_blocks(lines):
    """Split lines into blocks separated by empty lines."""
    blocks = []
    current_block = []
    for line in lines:
        if line.strip() == '':
            if current_block:
                blocks.append(current_block)
            # Add the empty line as its own block
            blocks.append([line])
            current_block = []
        else:
            current_block.append(line)
    if current_block:
        blocks.append(current_block)
    return blocks


def escape_xml(text: str) -> str:
    """Escape special XML characters, convert newlines/tabs/spaces to ODF elements."""
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    text = text.replace('"', '&quot;')
    text = text.replace("'", '&apos;')
    # Convert tabs to ODF tab stops
    text = text.replace('\t', '<text:tab/>')
    # Convert multiple consecutive spaces to ODF space elements
    # (ODF collapses multiple spaces like HTML — use <text:s/> to preserve them)
    import re
    text = re.sub(r' {2,}', lambda m: ' ' + '<text:s/>' * (len(m.group(0)) - 1), text)
    # Convert newlines to ODF line breaks
    text = text.replace('\n', '<text:line-break/>')
    return text


def insert_signature(content: str, temp_dir: str, signature_path: str) -> str:
    """
    Insert signature image at the #UNTERSCHRIFT# placeholder.
    """
    # Copy signature to Pictures directory
    pictures_dir = os.path.join(temp_dir, 'Pictures')
    os.makedirs(pictures_dir, exist_ok=True)
    
    signature_dest = os.path.join(pictures_dir, 'signature.png')
    shutil.copy2(signature_path, signature_dest)
    
    # Create the draw:frame element for the signature
    # Using reasonable default dimensions for a signature
    signature_frame = '''<draw:frame draw:style-name="fr1" draw:name="Unterschrift" text:anchor-type="as-char" svg:width="5cm" svg:height="2cm" draw:z-index="0">
        <draw:image xlink:href="Pictures/signature.png" xlink:type="simple" xlink:show="embed" xlink:actuate="onLoad" loext:mime-type="image/png"/>
    </draw:frame>'''
    
    # Replace the placeholder
    content = content.replace('#UNTERSCHRIFT#', signature_frame)
    
    return content


def update_manifest(temp_dir: str) -> None:
    """
    Update the manifest.xml to include the signature image.
    """
    manifest_path = os.path.join(temp_dir, 'META-INF', 'manifest.xml')
    
    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = f.read()
    
    # Add entry for signature.png if not already present
    if 'signature.png' not in manifest:
        # Insert before closing </manifest:manifest> tag
        signature_entry = ' <manifest:file-entry manifest:full-path="Pictures/signature.png" manifest:media-type="image/png"/>\n'
        manifest = manifest.replace('</manifest:manifest>', signature_entry + '</manifest:manifest>')
        
        with open(manifest_path, 'w', encoding='utf-8') as f:
            f.write(manifest)


def create_odt_from_directory(source_dir: str, output_path: str) -> None:
    """
    Create an ODT file from a directory structure.
    The mimetype file must be the first entry and stored without compression.
    """
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        # Add mimetype first, uncompressed (required by ODF spec)
        mimetype_path = os.path.join(source_dir, 'mimetype')
        if os.path.exists(mimetype_path):
            zipf.write(mimetype_path, 'mimetype', compress_type=zipfile.ZIP_STORED)
        
        # Add all other files
        for root, dirs, files in os.walk(source_dir):
            for file in files:
                if file == 'mimetype':
                    continue
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, source_dir)
                zipf.write(file_path, arcname)


def convert_to_pdf(odt_path: str, output_dir: str) -> str:
    """
    Convert ODT to PDF using LibreOffice.
    
    Args:
        odt_path: Path to the ODT file
        output_dir: Directory where the PDF will be saved
        
    Returns:
        Path to the generated PDF file
    """
    import subprocess
    
    # Run LibreOffice in headless mode
    cmd = [
        'libreoffice',
        '--headless',
        '--convert-to', 'pdf',
        '--outdir', output_dir,
        odt_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice conversion failed: {result.stderr}")
    
    # Return the path to the generated PDF
    pdf_name = os.path.splitext(os.path.basename(odt_path))[0] + '.pdf'
    return os.path.join(output_dir, pdf_name)
