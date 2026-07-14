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

        # Replace placeholders
        content = replace_placeholders(content, replacements)
        
        # Insert signature if provided
        if signature_path and os.path.exists(signature_path):
            content = insert_signature(content, temp_dir, signature_path)
        
        # Write modified content.xml
        with open(content_xml_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        # Update manifest if signature was added
        if signature_path and os.path.exists(signature_path):
            update_manifest(temp_dir)
        
        # Create new ODT file
        create_odt_from_directory(temp_dir, output_path)
        
    return output_path


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
                
                # Inject the keep-with-next style into automatic-styles
                keep_style_def = (
                    f'<style:style style:name="{keep_style_name}" style:family="paragraph" '
                    f'style:parent-style-name="{parent_style}">'
                    f'<style:paragraph-properties fo:keep-with-next="always"/>'
                    f'</style:style>'
                )
                if keep_style_name not in content:
                    content = content.replace(
                        '</office:automatic-styles>',
                        keep_style_def + '</office:automatic-styles>'
                    )
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
        except (TypeError, ValueError):
            quantity = 0
        try:
            price = float(item.get('price', 0))
        except (TypeError, ValueError):
            price = 0.0
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
