from xml.dom import minidom

from deid.formats.xmltok import mask_chars, replace_in_text_nodes


def test_mask_uses_full_width_for_cjk_and_half_width_for_ascii_and_keeps_spaces():
    assert mask_chars("王小明 A12", "■", "*") == "■■■ ***"
    assert mask_chars("０９１２", "■", "*") == "■■■■"
    assert mask_chars("a\tb", "■", "*") == "*\t*"


def test_text_nodes_with_entities_stay_wellformed():
    xml = '<r><w:t xmlns:w="u">A&amp;B 王小明 &lt;x&gt;</w:t></r>'
    out = replace_in_text_nodes(xml, ["王小明", "A&B"], {"w:t"}, set(), "■", "*")
    assert out == '<r><w:t xmlns:w="u">*** ■■■ &lt;x&gt;</w:t></r>'
    minidom.parseString(out)


def test_only_listed_elements_and_attributes_are_touched():
    xml = ('<w:p xmlns:w="u"><w:tcW w:w="2301"/><w:t>分機 2301</w:t><w:rsid>2301</w:rsid>'
           '<pic descr="王小明的照片" name="2301"/></w:p>')
    out = replace_in_text_nodes(xml, ["2301", "王小明"], {"w:t"}, {"descr"}, "■", "*")
    assert 'w:w="2301"' in out and "<w:rsid>2301</w:rsid>" in out and 'name="2301"' in out
    assert "<w:t>分機 ****</w:t>" in out and 'descr="■■■的照片"' in out


def test_none_means_every_text_node():
    out = replace_in_text_nodes("<a><b>王小明</b><c>王小明</c></a>", ["王小明"], None, set(), "■", "*")
    assert out == "<a><b>■■■</b><c>■■■</c></a>"


def test_prefix_and_ancestor_rules():
    xml = ('<x><dc:creator>王小明</dc:creator><c:strCache><c:pt><c:v>王小明</c:v></c:pt></c:strCache>'
           '<c:numCache><c:pt><c:v>2301</c:v></c:pt></c:numCache></x>')
    out = replace_in_text_nodes(xml, ["王小明", "2301"], set(), set(), "■", "*", prefixes=("dc:",),
                                under={"c:v": {"c:strCache", "c:strLit"}})
    assert "<dc:creator>■■■</dc:creator>" in out and "<c:v>■■■</c:v>" in out and "<c:v>2301</c:v>" in out


def test_longer_strings_win_over_their_substrings():
    out = replace_in_text_nodes("<t>王小明先生</t>", ["小明", "王小明先生"], None, set(), "■", "*")
    assert out == "<t>■■■■■</t>"


def test_comments_and_declarations_are_left_alone():
    xml = '<?xml version="1.0"?><!-- 王小明 --><t>王小明</t>'
    assert replace_in_text_nodes(xml, ["王小明"], None, set(), "■", "*") == '<?xml version="1.0"?><!-- 王小明 --><t>■■■</t>'


def test_unchanged_input_is_returned_identically():
    xml = "<t>沒有個資</t>"
    assert replace_in_text_nodes(xml, ["王小明"], None, set(), "■", "*") is xml
