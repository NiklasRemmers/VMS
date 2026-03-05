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
    signature_path: Optional[str] = None
) -> str:
    """
    Process an ODT template by replacing placeholders and inserting signature.
    
    Args:
        template_path: Path to the ODT template file
        output_path: Path where the processed ODT will be saved
        replacements: Dictionary mapping placeholder names to replacement values
        signature_path: Optional path to signature PNG file
        
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


def replace_placeholders(content: str, replacements: Dict[str, str]) -> str:
    """
    Replace placeholders in content, handling fragmented XML tags.
    
    LibreOffice sometimes splits placeholders across multiple XML tags like:
    <text:span>#</text:span>PLACEHOLDER<text:span>#</text:span>
    
    This function handles both simple and fragmented cases.
    """
    # First, normalize fragmented placeholders
    # Pattern matches #...WORD...# where the content might be split by XML tags
    
    # Handle fragmented placeholders by removing XML tags between # markers
    # This regex finds patterns like #<tags>TEXT</tags># and extracts the text
    def normalize_placeholder(match):
        full_match = match.group(0)
        # Remove all XML tags to get the pure placeholder
        clean = re.sub(r'<[^>]+>', '', full_match)
        return clean
    
    # Find potential fragmented placeholders (# followed by content with possible tags, ending with #)
    fragmented_pattern = r'#(?:<[^>]*>)*([A-ZÄÖÜ][A-ZÄÖÜ0-9_ ]*?)(?:<[^>]*>)*#'
    content = re.sub(fragmented_pattern, lambda m: f'#{m.group(1)}#', content)
    
    # Now replace all placeholders
    for placeholder, value in replacements.items():
        # Ensure placeholder has # markers
        if not placeholder.startswith('#'):
            placeholder = f'#{placeholder}#'
        
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
                # Create separate paragraphs for each line
                replacement_paragraphs = []
                for line in lines:
                    escaped_line = escape_xml(line)
                    replacement_paragraphs.append(f'{open_tag}{escaped_line}{close_tag}')
                content = content[:match.start()] + ''.join(replacement_paragraphs) + content[match.end():]
            else:
                # Fallback: simple replacement with line breaks
                escaped_value = escape_xml(value)
                content = content.replace(placeholder, escaped_value)
        else:
            # Single-line value: simple replacement
            escaped_value = escape_xml(value)
            content = content.replace(placeholder, escaped_value)
    
    return content


def escape_xml(text: str) -> str:
    """Escape special XML characters and convert newlines to line breaks."""
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    text = text.replace('"', '&quot;')
    text = text.replace("'", '&apos;')
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
