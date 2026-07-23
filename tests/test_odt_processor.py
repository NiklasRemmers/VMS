"""
Tests for odt_processor.py.

odt_processor has zero Flask/DB coupling (only os, re, shutil, tempfile,
zipfile, typing) so these tests use plain Python data and tmp_path — never the
app/db_session/client fixtures from conftest.py.
"""
import os
import zipfile

import pytest

import vms.infra.odt_processor as odt_processor


# ---------------------------------------------------------------------------
# escape_xml
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_escape_xml_escapes_special_characters():
    result = odt_processor.escape_xml('A & B <tag> "quoted" \'apos\'')

    assert result == 'A &amp; B &lt;tag&gt; &quot;quoted&quot; &apos;apos&apos;'


@pytest.mark.unit
def test_escape_xml_converts_tab_to_odf_tab_stop():
    result = odt_processor.escape_xml('a\tb')

    assert result == 'a<text:tab/>b'


@pytest.mark.unit
def test_escape_xml_converts_newline_to_odf_line_break():
    result = odt_processor.escape_xml('line1\nline2')

    assert result == 'line1<text:line-break/>line2'


@pytest.mark.unit
def test_escape_xml_single_space_untouched():
    result = odt_processor.escape_xml('a b')

    assert result == 'a b'


@pytest.mark.unit
def test_escape_xml_multiple_spaces_become_text_s_elements():
    # 3 consecutive spaces -> keep one literal space, then 2 <text:s/> markers.
    result = odt_processor.escape_xml('a   b')

    assert result == 'a <text:s/><text:s/>b'


# ---------------------------------------------------------------------------
# format_money_de
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_format_money_de_formats_thousands_and_decimals():
    assert odt_processor.format_money_de(1234.5) == '1.234,50'


@pytest.mark.unit
def test_format_money_de_formats_integer_value():
    assert odt_processor.format_money_de(7) == '7,00'


@pytest.mark.unit
def test_format_money_de_formats_negative_value():
    assert odt_processor.format_money_de(-42.1) == '-42,10'


@pytest.mark.unit
def test_format_money_de_none_falls_back_to_zero():
    # Deliberate defensive default for a generic display formatter that may be
    # fed arbitrary/missing values from many call sites (not specific to a
    # single invoice line) -- pinning as intended behaviour, not a bug.
    assert odt_processor.format_money_de(None) == '0,00'


@pytest.mark.unit
def test_format_money_de_non_numeric_string_falls_back_to_zero():
    assert odt_processor.format_money_de('not-a-number') == '0,00'


# ---------------------------------------------------------------------------
# _split_into_blocks
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_split_into_blocks_separates_on_empty_lines():
    blocks = odt_processor._split_into_blocks(['a', 'b', '', 'c'])

    assert blocks == [['a', 'b'], [''], ['c']]


@pytest.mark.unit
def test_split_into_blocks_trailing_block_included_without_terminal_blank():
    blocks = odt_processor._split_into_blocks(['a', '', 'b', 'c'])

    assert blocks == [['a'], [''], ['b', 'c']]


@pytest.mark.unit
def test_split_into_blocks_empty_input_returns_empty_list():
    assert odt_processor._split_into_blocks([]) == []


@pytest.mark.unit
def test_split_into_blocks_consecutive_empty_lines_do_not_append_empty_current_block():
    # Second blank line hits the "line.strip() == ''" branch while
    # current_block is already [] (falsy) -> the nested "if current_block"
    # append is skipped, only the blank-line-as-its-own-block append runs.
    blocks = odt_processor._split_into_blocks(['a', '', '', 'b'])

    assert blocks == [['a'], [''], [''], ['b']]


# ---------------------------------------------------------------------------
# normalize_fragmented_placeholders
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_normalize_fragmented_placeholders_collapses_split_token():
    fragmented = '#<text:span>VERLEIHER</text:span>#'

    result = odt_processor.normalize_fragmented_placeholders(fragmented)

    assert result == '#VERLEIHER#'


@pytest.mark.unit
def test_normalize_fragmented_placeholders_leaves_clean_placeholder_untouched():
    result = odt_processor.normalize_fragmented_placeholders('#VERLEIHER#')

    assert result == '#VERLEIHER#'


@pytest.mark.unit
def test_normalize_fragmented_placeholders_lowercase_token_not_matched():
    # The pattern only matches [A-ZÄÖÜ...] (uppercase), so a lowercase-looking
    # placeholder is left as-is (it isn't a real placeholder in this scheme).
    text = '#verleiher#'

    result = odt_processor.normalize_fragmented_placeholders(text)

    assert result == '#verleiher#'


# ---------------------------------------------------------------------------
# _paragraph_text
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_paragraph_text_strips_tags_and_whitespace():
    paragraph = '<text:p text:style-name="Standard">  Hello <text:span>World</text:span>  </text:p>'

    assert odt_processor._paragraph_text(paragraph) == 'Hello World'


# ---------------------------------------------------------------------------
# _inject_styles
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_inject_styles_appends_missing_style():
    content = '<office:automatic-styles></office:automatic-styles>'
    styles = '<style:style style:name="Foo" style:family="paragraph"></style:style>'

    result = odt_processor._inject_styles(content, styles)

    assert 'style:name="Foo"' in result
    assert result.endswith('</office:automatic-styles>')
    assert result.index('style:name="Foo"') < result.index('</office:automatic-styles>')


@pytest.mark.unit
def test_inject_styles_skips_style_already_present():
    styles = '<style:style style:name="Foo" style:family="paragraph"></style:style>'
    content = f'<office:automatic-styles>{styles}</office:automatic-styles>'

    result = odt_processor._inject_styles(content, styles)

    # Early-return branch: content unchanged, no duplicate injected.
    assert result == content
    assert result.count('style:name="Foo"') == 1


@pytest.mark.unit
def test_inject_styles_raises_attributeerror_when_style_missing_name():
    content = '<office:automatic-styles></office:automatic-styles>'
    styles_without_name = '<style:style style:family="paragraph"></style:style>'

    with pytest.raises(AttributeError):
        odt_processor._inject_styles(content, styles_without_name)


# ---------------------------------------------------------------------------
# _ensure_keep_style
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_ensure_keep_style_inserts_new_style_when_absent():
    content = '<office:automatic-styles></office:automatic-styles>'

    new_content, keep_name = odt_processor._ensure_keep_style(content, 'Standard')

    assert keep_name == 'Standard_keep'
    assert 'style:name="Standard_keep"' in new_content
    assert 'fo:keep-with-next="always"' in new_content


@pytest.mark.unit
def test_ensure_keep_style_reuses_existing_style():
    existing = '<style:style style:name="Standard_keep" style:family="paragraph"></style:style>'
    content = f'<office:automatic-styles>{existing}</office:automatic-styles>'

    new_content, keep_name = odt_processor._ensure_keep_style(content, 'Standard')

    assert keep_name == 'Standard_keep'
    # Not duplicated.
    assert new_content.count('style:name="Standard_keep"') == 1
    assert new_content == content


# ---------------------------------------------------------------------------
# _set_keep_with_next
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_set_keep_with_next_swaps_existing_style_name():
    content = '<office:automatic-styles></office:automatic-styles>'
    paragraph = '<text:p text:style-name="Standard">Hello</text:p>'

    new_content, new_paragraph = odt_processor._set_keep_with_next(content, paragraph)

    assert 'text:style-name="Standard_keep"' in new_paragraph
    assert 'Hello' in new_paragraph
    assert 'style:name="Standard_keep"' in new_content


@pytest.mark.unit
def test_set_keep_with_next_paragraph_without_style_attribute_gets_one_inserted():
    content = '<office:automatic-styles></office:automatic-styles>'
    paragraph = '<text:p>Hello</text:p>'

    new_content, new_paragraph = odt_processor._set_keep_with_next(content, paragraph)

    assert new_paragraph.startswith('<text:p text:style-name="Standard_keep"')
    assert 'style:name="Standard_keep"' in new_content


@pytest.mark.unit
def test_set_keep_with_next_raises_attributeerror_for_non_paragraph_input():
    # Precondition: caller must supply text that starts with a <text:p ...> tag.
    content = '<office:automatic-styles></office:automatic-styles>'
    not_a_paragraph = 'just plain text, no tag at all'

    with pytest.raises(AttributeError):
        odt_processor._set_keep_with_next(content, not_a_paragraph)


# ---------------------------------------------------------------------------
# _autogrow_text_frames
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_autogrow_text_frames_no_flow_styles_returns_content_unchanged():
    content = (
        '<office:automatic-styles>'
        '<style:style style:name="fr1" style:family="graphic">'
        '<style:graphic-properties style:vertical-rel="page"/></style:style>'
        '</office:automatic-styles>'
        '<draw:frame draw:style-name="fr1" svg:height="2cm">'
        '<draw:text-box></draw:text-box></draw:frame>'
    )

    result = odt_processor._autogrow_text_frames(content)

    assert result == content


@pytest.mark.unit
def test_autogrow_text_frames_injects_min_height_for_flow_anchored_frame():
    content = (
        '<office:automatic-styles>'
        '<style:style style:name="fr1" style:family="graphic">'
        '<style:graphic-properties style:vertical-rel="paragraph"/></style:style>'
        '</office:automatic-styles>'
        '<draw:frame draw:style-name="fr1" svg:height="2cm">'
        '<draw:text-box attr="x"></draw:text-box></draw:frame>'
    )

    result = odt_processor._autogrow_text_frames(content)

    assert 'fo:min-height="2cm"' in result
    # The fixed svg:height on the frame itself is stripped.
    assert 'svg:height="2cm"' not in result.split('<draw:text-box')[0]


@pytest.mark.unit
def test_autogrow_text_frames_leaves_min_height_alone_if_already_present():
    content = (
        '<office:automatic-styles>'
        '<style:style style:name="fr1" style:family="graphic">'
        '<style:graphic-properties style:vertical-rel="paragraph"/></style:style>'
        '</office:automatic-styles>'
        '<draw:frame draw:style-name="fr1" svg:height="2cm">'
        '<draw:text-box fo:min-height="9cm"></draw:text-box></draw:frame>'
    )

    result = odt_processor._autogrow_text_frames(content)

    assert result.count('fo:min-height') == 1
    assert 'fo:min-height="9cm"' in result


@pytest.mark.unit
def test_autogrow_text_frames_skips_frame_whose_style_is_not_flow_anchored():
    content = (
        '<office:automatic-styles>'
        '<style:style style:name="fr1" style:family="graphic">'
        '<style:graphic-properties style:vertical-rel="paragraph"/></style:style>'
        '</office:automatic-styles>'
        '<draw:frame draw:style-name="fr-other" svg:height="2cm">'
        '<draw:text-box></draw:text-box></draw:frame>'
    )

    result = odt_processor._autogrow_text_frames(content)

    assert result == content


@pytest.mark.unit
def test_autogrow_text_frames_skips_frame_without_height_attribute():
    content = (
        '<office:automatic-styles>'
        '<style:style style:name="fr1" style:family="graphic">'
        '<style:graphic-properties style:vertical-rel="paragraph"/></style:style>'
        '</office:automatic-styles>'
        '<draw:frame draw:style-name="fr1">'
        '<draw:text-box></draw:text-box></draw:frame>'
    )

    result = odt_processor._autogrow_text_frames(content)

    assert result == content


# ---------------------------------------------------------------------------
# _expand_signature_block
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_expand_signature_block_absent_placeholder_returns_unchanged():
    content = '<office:automatic-styles></office:automatic-styles><office:text>Hello</office:text>'

    result = odt_processor._expand_signature_block(content)

    assert result == content


@pytest.mark.unit
def test_expand_signature_block_replaces_enclosing_paragraph_with_table():
    content = (
        '<office:automatic-styles></office:automatic-styles>'
        '<office:text><text:p text:style-name="Standard">#UNTERSCHRIFTSBLOCK#</text:p></office:text>'
    )

    result = odt_processor._expand_signature_block(content)

    assert '<table:table' in result
    assert '#UNTERSCHRIFTSBLOCK#' not in result
    # The enclosing <text:p> that held the placeholder is gone (swallowed).
    assert 'text:style-name="Standard">#UNTERSCHRIFTSBLOCK#' not in result
    # Styles were injected since they were missing.
    assert 'style:name="TSig"' in result


@pytest.mark.unit
def test_expand_signature_block_fallback_replace_when_not_inside_paragraph():
    # Placeholder present but with no enclosing <text:p>...</text:p> around it
    # -> the regex swallow misses and the simple string-replace fallback runs.
    content = (
        '<office:automatic-styles></office:automatic-styles>'
        '<office:text>#UNTERSCHRIFTSBLOCK#</office:text>'
    )

    result = odt_processor._expand_signature_block(content)

    assert '<table:table' in result
    assert '#UNTERSCHRIFTSBLOCK#' not in result


# ---------------------------------------------------------------------------
# _apply_keep_together
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_apply_keep_together_single_paragraph_no_keep_with_next_added():
    content = (
        '<office:automatic-styles></office:automatic-styles>'
        '<office:text>'
        '<text:p text:style-name="Standard">#ZUSAMMEN_START#Hello#ZUSAMMEN_ENDE#</text:p>'
        '</office:text>'
    )

    result = odt_processor._apply_keep_together(content)

    assert '#ZUSAMMEN_START#' not in result
    assert '#ZUSAMMEN_ENDE#' not in result
    assert 'Hello' in result
    # Only one paragraph in range -> range(len(rebuilt)-1) == range(0) -> no
    # keep-with-next style is ever applied.
    assert 'keep-with-next' not in result


@pytest.mark.unit
def test_apply_keep_together_multiple_paragraphs_chain_keep_with_next():
    content = (
        '<office:automatic-styles></office:automatic-styles>'
        '<office:text>'
        '<text:p text:style-name="Standard">#ZUSAMMEN_START#First</text:p>'
        '<text:p text:style-name="Standard">Second#ZUSAMMEN_ENDE#</text:p>'
        '</office:text>'
    )

    result = odt_processor._apply_keep_together(content)

    assert '#ZUSAMMEN_START#' not in result
    assert '#ZUSAMMEN_ENDE#' not in result
    assert 'First' in result and 'Second' in result
    # The first (non-last) paragraph got switched to the keep style; the
    # style definition itself was injected once.
    assert 'text:style-name="Standard_keep"' in result
    assert result.count('style:name="Standard_keep"') == 1


@pytest.mark.unit
def test_apply_keep_together_marker_only_paragraph_is_dropped():
    content = (
        '<office:automatic-styles></office:automatic-styles>'
        '<office:text>'
        '<text:p text:style-name="Standard">Before</text:p>'
        '<text:p text:style-name="Standard">#ZUSAMMEN_START#</text:p>'
        '<text:p text:style-name="Standard">Middle</text:p>'
        '<text:p text:style-name="Standard">#ZUSAMMEN_ENDE#</text:p>'
        '<text:p text:style-name="Standard">After</text:p>'
        '</office:text>'
    )

    result = odt_processor._apply_keep_together(content)

    assert 'Before' in result and 'Middle' in result and 'After' in result
    # The two marker-only paragraphs are removed entirely, not just emptied.
    assert result.count('<text:p') == 3


@pytest.mark.unit
def test_apply_keep_together_no_markers_returns_unchanged():
    content = (
        '<office:automatic-styles></office:automatic-styles>'
        '<office:text><text:p text:style-name="Standard">Plain</text:p></office:text>'
    )

    result = odt_processor._apply_keep_together(content)

    assert result == content


@pytest.mark.unit
def test_apply_keep_together_bails_when_no_preceding_paragraph_tag():
    # No '<text:p' anywhere before the START marker -> rfind returns -1 ->
    # the region_start == -1 guard bails without touching the markers.
    content = '<office:text>#ZUSAMMEN_START#Text#ZUSAMMEN_ENDE#</office:text>'

    result = odt_processor._apply_keep_together(content)

    assert result == content
    assert '#ZUSAMMEN_START#' in result


@pytest.mark.unit
def test_apply_keep_together_bails_when_no_closing_paragraph_tag_follows():
    # A <text:p is opened before the markers but no '</text:p>' ever follows
    # -> the region_end == -1 guard bails without touching the markers.
    content = '<office:text><text:p style="x">#ZUSAMMEN_START#Text#ZUSAMMEN_ENDE#</office:text>'

    result = odt_processor._apply_keep_together(content)

    assert result == content
    assert '#ZUSAMMEN_START#' in result


@pytest.mark.unit
def test_apply_keep_together_bails_when_region_contains_no_real_paragraph():
    # The literal substring '<text:p' is present (inside a differently-named
    # tag whose name starts with "p"), so rfind/find both succeed and widen a
    # region -- but _PARAGRAPH_RE's `\b` after "text:p" never matches inside
    # it, so _PARAGRAPH_RE.findall(region) comes back empty and the function
    # bails rather than operate on a bogus region.
    content = (
        '<office:text><text:phantom>#ZUSAMMEN_START#Text#ZUSAMMEN_ENDE#</text:p>'
        '</office:text>'
    )

    result = odt_processor._apply_keep_together(content)

    assert result == content
    assert '#ZUSAMMEN_START#' in result


# ---------------------------------------------------------------------------
# replace_placeholders
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_replace_placeholders_wraps_bare_key_with_hash_markers():
    content = '<text:p>#NAME#</text:p>'

    result = odt_processor.replace_placeholders(content, {'NAME': 'Alice'})

    assert result == '<text:p>Alice</text:p>'


@pytest.mark.unit
def test_replace_placeholders_none_value_becomes_empty_string():
    content = '<text:p>Hello #NAME#!</text:p>'

    result = odt_processor.replace_placeholders(content, {'#NAME#': None})

    assert result == '<text:p>Hello !</text:p>'


@pytest.mark.unit
def test_replace_placeholders_non_string_value_is_stringified():
    content = '<text:p>#COUNT#</text:p>'

    result = odt_processor.replace_placeholders(content, {'#COUNT#': 42})

    assert result == '<text:p>42</text:p>'


@pytest.mark.unit
def test_replace_placeholders_single_line_value_simple_replace():
    content = '<text:p text:style-name="Standard">Wert: #NAME#</text:p>'

    result = odt_processor.replace_placeholders(content, {'#NAME#': 'Bob & Sons'})

    assert result == '<text:p text:style-name="Standard">Wert: Bob &amp; Sons</text:p>'


@pytest.mark.unit
def test_replace_placeholders_multiline_value_splits_into_paragraphs_with_keep_style():
    content = '<office:automatic-styles></office:automatic-styles><text:p text:style-name="Standard">#ADRESSE#</text:p>'

    result = odt_processor.replace_placeholders(content, {'#ADRESSE#': 'Street 1\nCity'})

    # Two separate <text:p> elements, one per line.
    assert result.count('<text:p') == 2
    assert 'Street 1' in result and 'City' in result
    # Inner (non-last) line of the block gets the keep-with-next style variant.
    assert 'text:style-name="Standard_keep">Street 1</text:p>' in result
    # Last line of the block keeps the plain parent style.
    assert 'text:style-name="Standard">City</text:p>' in result
    assert 'style:name="Standard_keep"' in result


@pytest.mark.unit
def test_replace_placeholders_multiline_value_blank_line_uses_normal_style():
    content = '<office:automatic-styles></office:automatic-styles><text:p text:style-name="Standard">#TEXT#</text:p>'

    result = odt_processor.replace_placeholders(content, {'#TEXT#': 'A\n\nB'})

    # Blank line in the middle is its own block -> "is_last_in_block or not
    # line.strip()" keeps it on the normal (non-keep) style, not the keep style.
    assert result.count('<text:p') == 3
    assert 'text:style-name="Standard"></text:p>' in result


@pytest.mark.unit
def test_replace_placeholders_multiline_fallback_when_no_enclosing_paragraph_matches():
    # Placeholder present but not inside a <text:p>...</text:p> pair that the
    # multi-line lookup pattern can match -> falls back to a simple replace
    # (line breaks become <text:line-break/>, not separate paragraphs).
    content = '<office:text>#NOTE#</office:text>'

    result = odt_processor.replace_placeholders(content, {'#NOTE#': 'Line1\nLine2'})

    assert result == '<office:text>Line1<text:line-break/>Line2</office:text>'
    assert '<text:p' not in result


# ---------------------------------------------------------------------------
# _expand_item_rows
# ---------------------------------------------------------------------------

_ITEM_ROW_TEMPLATE = (
    '<table:table-row>'
    '<table:table-cell><text:p>#ARTIKEL#</text:p></table:table-cell>'
    '<table:table-cell><text:p>#MENGE#</text:p></table:table-cell>'
    '<table:table-cell><text:p>#STÜCKPREIS#</text:p></table:table-cell>'
    '<table:table-cell><text:p>#GESAMTPREIS_POS#</text:p></table:table-cell>'
    '</table:table-row>'
)


@pytest.mark.unit
def test_expand_item_rows_no_article_row_returns_content_unchanged():
    content = '<table:table-row><table:table-cell>no placeholder here</table:table-cell></table:table-row>'

    result = odt_processor._expand_item_rows(content, [{'name': 'Zelt', 'quantity': 1, 'price': 5}])

    assert result == content


@pytest.mark.unit
def test_expand_item_rows_none_items_produces_no_rows():
    content = f'<table:table>{_ITEM_ROW_TEMPLATE}</table:table>'

    result = odt_processor._expand_item_rows(content, None)

    assert result == '<table:table></table:table>'


@pytest.mark.unit
def test_expand_item_rows_empty_list_produces_no_rows():
    content = f'<table:table>{_ITEM_ROW_TEMPLATE}</table:table>'

    result = odt_processor._expand_item_rows(content, [])

    assert result == '<table:table></table:table>'


@pytest.mark.unit
def test_expand_item_rows_renders_one_row_per_item_with_computed_total():
    content = f'<table:table>{_ITEM_ROW_TEMPLATE}</table:table>'
    items = [
        {'name': 'Zelt', 'quantity': 2, 'price': 10.5},
        {'name': 'Hering', 'quantity': 3, 'price': 1},
    ]

    result = odt_processor._expand_item_rows(content, items)

    assert result.count('<table:table-row>') == 2
    assert '<text:p>Zelt</text:p>' in result
    assert '<text:p>2</text:p>' in result
    assert '<text:p>10,50</text:p>' in result
    # Position total = price * quantity = 21,00
    assert '<text:p>21,00</text:p>' in result
    assert '<text:p>Hering</text:p>' in result
    assert '<text:p>3,00</text:p>' in result  # 1 * 3


@pytest.mark.unit
def test_expand_item_rows_missing_name_defaults_to_empty_string():
    content = f'<table:table>{_ITEM_ROW_TEMPLATE}</table:table>'

    result = odt_processor._expand_item_rows(content, [{'quantity': 1, 'price': 1}])

    assert '<text:p></text:p>' in result


@pytest.mark.unit
def test_expand_item_rows_missing_quantity_key_defaults_to_zero():
    content = f'<table:table>{_ITEM_ROW_TEMPLATE}</table:table>'

    result = odt_processor._expand_item_rows(content, [{'name': 'Zelt', 'price': 5}])

    assert '<text:p>0</text:p>' in result  # quantity
    assert '<text:p>0,00</text:p>' in result  # pos total = 5 * 0


@pytest.mark.unit
def test_expand_item_rows_invalid_quantity_should_not_be_silently_zeroed():
    content = f'<table:table>{_ITEM_ROW_TEMPLATE}</table:table>'
    items = [{'name': 'Zelt', 'quantity': 'zwei', 'price': 10}]

    # Domain-correct: malformed quantity data for an invoice line must not
    # silently become a priced-at-zero row -- it should raise so the caller
    # notices the corrupt input, rather than quietly producing an under-total.
    with pytest.raises(ValueError):
        odt_processor._expand_item_rows(content, items)


@pytest.mark.unit
def test_expand_item_rows_invalid_price_should_not_be_silently_zeroed():
    content = f'<table:table>{_ITEM_ROW_TEMPLATE}</table:table>'
    items = [{'name': 'Zelt', 'quantity': 2, 'price': 'zehn'}]

    with pytest.raises(ValueError):
        odt_processor._expand_item_rows(content, items)


# ---------------------------------------------------------------------------
# File-I/O: insert_signature, update_manifest, create_odt_from_directory,
# process_odt_template (build a minimal .odt ourselves)
# ---------------------------------------------------------------------------

def _write_minimal_odt(path, content_xml=None, include_mimetype=True, include_manifest=True):
    """Build a minimal valid-enough .odt zip for our tests."""
    if content_xml is None:
        content_xml = (
            '<office:document-content>'
            '<office:automatic-styles></office:automatic-styles>'
            '<office:body><office:text>'
            '<text:p text:style-name="Standard">Hallo #NAME#</text:p>'
            '</office:text></office:body>'
            '</office:document-content>'
        )
    manifest_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
        '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
        '</manifest:manifest>'
    )
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
        if include_mimetype:
            zf.writestr('mimetype', 'application/vnd.oasis.opendocument.text', zipfile.ZIP_STORED)
        zf.writestr('content.xml', content_xml)
        if include_manifest:
            zf.writestr('META-INF/manifest.xml', manifest_xml)
    return path


def _tiny_png_bytes():
    # 1x1 transparent PNG.
    return bytes.fromhex(
        '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489'
        '0000000a49444154789c6360000002000155a2415d0000000049454e44ae426082'
    )


@pytest.mark.integration
def test_process_odt_template_without_signature_replaces_placeholder_and_strips_marker(tmp_path):
    template_path = _write_minimal_odt(tmp_path / 'template.odt')
    output_path = tmp_path / 'output.odt'

    result_path = odt_processor.process_odt_template(
        str(template_path), str(output_path), {'#NAME#': 'Welt'}
    )

    assert result_path == str(output_path)
    assert os.path.exists(output_path)
    with zipfile.ZipFile(output_path) as zf:
        content = zf.read('content.xml').decode('utf-8')
    assert 'Hallo Welt' in content
    assert '#UNTERSCHRIFT#' not in content


@pytest.mark.integration
def test_process_odt_template_with_signature_embeds_image_and_updates_manifest(tmp_path):
    content_xml = (
        '<office:document-content>'
        '<office:automatic-styles></office:automatic-styles>'
        '<office:body><office:text>'
        '<text:p text:style-name="Standard">#NAME# #UNTERSCHRIFT#</text:p>'
        '</office:text></office:body>'
        '</office:document-content>'
    )
    template_path = _write_minimal_odt(tmp_path / 'template.odt', content_xml=content_xml)
    output_path = tmp_path / 'output.odt'
    signature_path = tmp_path / 'sig.png'
    signature_path.write_bytes(_tiny_png_bytes())

    odt_processor.process_odt_template(
        str(template_path), str(output_path), {'#NAME#': 'Welt'},
        signature_path=str(signature_path),
    )

    with zipfile.ZipFile(output_path) as zf:
        names = zf.namelist()
        content = zf.read('content.xml').decode('utf-8')
        manifest = zf.read('META-INF/manifest.xml').decode('utf-8')

    assert 'Pictures/signature.png' in names
    assert 'draw:frame' in content
    assert 'signature.png' in manifest


@pytest.mark.integration
def test_process_odt_template_with_row_items_expands_article_rows(tmp_path):
    content_xml = (
        '<office:document-content>'
        '<office:automatic-styles></office:automatic-styles>'
        '<office:body><office:text>'
        '<table:table>' + _ITEM_ROW_TEMPLATE + '</table:table>'
        '</office:text></office:body>'
        '</office:document-content>'
    )
    template_path = _write_minimal_odt(tmp_path / 'template.odt', content_xml=content_xml)
    output_path = tmp_path / 'output.odt'

    odt_processor.process_odt_template(
        str(template_path), str(output_path), {},
        row_items=[{'name': 'Zelt', 'quantity': 1, 'price': 3}],
    )

    with zipfile.ZipFile(output_path) as zf:
        content = zf.read('content.xml').decode('utf-8')
    assert '<text:p>Zelt</text:p>' in content


@pytest.mark.integration
def test_insert_signature_copies_png_and_embeds_frame(tmp_path):
    signature_path = tmp_path / 'sig.png'
    signature_path.write_bytes(_tiny_png_bytes())
    content = '<office:text>#UNTERSCHRIFT#</office:text>'

    result = odt_processor.insert_signature(content, str(tmp_path), str(signature_path))

    dest = tmp_path / 'Pictures' / 'signature.png'
    assert dest.exists()
    assert dest.read_bytes() == signature_path.read_bytes()
    assert '#UNTERSCHRIFT#' not in result
    assert 'xlink:href="Pictures/signature.png"' in result


@pytest.mark.integration
def test_update_manifest_adds_entry_when_absent(tmp_path):
    meta_dir = tmp_path / 'META-INF'
    meta_dir.mkdir()
    manifest_path = meta_dir / 'manifest.xml'
    manifest_path.write_text(
        '<manifest:manifest>'
        '<manifest:file-entry manifest:full-path="/"/>'
        '</manifest:manifest>'
    )

    odt_processor.update_manifest(str(tmp_path))

    updated = manifest_path.read_text()
    assert 'Pictures/signature.png' in updated
    assert updated.count('manifest:file-entry') == 2


@pytest.mark.integration
def test_update_manifest_is_noop_when_entry_already_present(tmp_path):
    meta_dir = tmp_path / 'META-INF'
    meta_dir.mkdir()
    manifest_path = meta_dir / 'manifest.xml'
    original = (
        '<manifest:manifest>'
        '<manifest:file-entry manifest:full-path="Pictures/signature.png"/>'
        '</manifest:manifest>'
    )
    manifest_path.write_text(original)

    odt_processor.update_manifest(str(tmp_path))

    assert manifest_path.read_text() == original


@pytest.mark.integration
def test_create_odt_from_directory_stores_mimetype_uncompressed_and_first(tmp_path):
    source_dir = tmp_path / 'src'
    source_dir.mkdir()
    (source_dir / 'mimetype').write_text('application/vnd.oasis.opendocument.text')
    (source_dir / 'content.xml').write_text('<office:document-content/>')
    output_path = tmp_path / 'out.odt'

    odt_processor.create_odt_from_directory(str(source_dir), str(output_path))

    with zipfile.ZipFile(output_path) as zf:
        infos = zf.infolist()
        assert infos[0].filename == 'mimetype'
        assert infos[0].compress_type == zipfile.ZIP_STORED
        names = zf.namelist()
        assert 'content.xml' in names


@pytest.mark.integration
def test_create_odt_from_directory_without_mimetype_file_still_writes_rest(tmp_path):
    source_dir = tmp_path / 'src'
    source_dir.mkdir()
    (source_dir / 'content.xml').write_text('<office:document-content/>')
    output_path = tmp_path / 'out.odt'

    odt_processor.create_odt_from_directory(str(source_dir), str(output_path))

    with zipfile.ZipFile(output_path) as zf:
        names = zf.namelist()
        assert 'mimetype' not in names
        assert 'content.xml' in names


# ---------------------------------------------------------------------------
# convert_to_pdf — subprocess.run is always patched, never runs LibreOffice.
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_convert_to_pdf_returns_pdf_path_on_success(tmp_path, mocker):
    fake_result = mocker.Mock(returncode=0, stdout='', stderr='')
    run_mock = mocker.patch('subprocess.run', return_value=fake_result)

    output_dir = tmp_path / 'out'
    output_dir.mkdir()
    pdf_path = odt_processor.convert_to_pdf(str(tmp_path / 'Rechnung.odt'), str(output_dir))

    assert pdf_path == str(output_dir / 'Rechnung.pdf')
    run_mock.assert_called_once()


@pytest.mark.unit
def test_convert_to_pdf_raises_runtimeerror_on_nonzero_returncode(tmp_path, mocker):
    fake_result = mocker.Mock(returncode=1, stdout='', stderr='boom: conversion failed')
    mocker.patch('subprocess.run', return_value=fake_result)

    with pytest.raises(RuntimeError, match='boom: conversion failed'):
        odt_processor.convert_to_pdf(str(tmp_path / 'Rechnung.odt'), str(tmp_path))
