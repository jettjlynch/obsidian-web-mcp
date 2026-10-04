"""Port of upstream a4aa3b2 (#99): vault_write / vault_append content is byte-exact."""

import pytest

from obsidian_vault_mcp.models import VaultAppendInput, VaultWriteInput
from obsidian_vault_mcp.tools.write import vault_append, vault_write


def test_write_model_keeps_content_whitespace_but_strips_path():
    m = VaultWriteInput(path="  a.md  ", content="  indented\nbody\n\n")
    assert m.path == "a.md"
    assert m.content == "  indented\nbody\n\n"


def test_append_model_keeps_content_whitespace():
    m = VaultAppendInput(path=" a.md ", content="\n- item\n")
    assert m.path == "a.md" and m.content == "\n- item\n"


@pytest.mark.parametrize("content", ["# T\n\nbody\n", "    code first\n", "no newline", "trailing  \n\n"])
def test_vault_write_writes_bytes_exactly(vault_dir, content):
    inp = VaultWriteInput(path="verbatim.md", content=content)
    vault_write(inp.path, inp.content)
    assert (vault_dir / "verbatim.md").read_bytes() == content.encode("utf-8")
