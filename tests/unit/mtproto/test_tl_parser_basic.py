from __future__ import annotations

from telecraft.tl import parse_tl


def test_parse_tl_basic_sections_and_params() -> None:
    schema = """
// Example minimal schema
---types---
user#2e13f4c3 id:long first_name:string flags:# last_name:flags.1?string = User;
testVector#11111111 items:Vector<int> = TestVector;
vector#1cb5c415 {t:Type} # [ t ] = Vector t;

---functions---
help.getConfig#c4f9186b = Config;
messages.sendMessage#a0b5a75c peer:InputPeer message:string random_id:long = Updates;
"""
    parsed = parse_tl(schema)
    assert len(parsed.constructors) == 3
    assert len(parsed.methods) == 2

    user = parsed.constructors[0]
    assert user.name == "user"
    assert user.constructor_id is not None
    assert user.result.raw == "User"
    assert [p.name for p in user.params][:3] == ["id", "first_name", "flags"]
    assert user.params[3].type_ref.raw == "flags.1?string"

    send = parsed.methods[1]
    assert send.name == "messages.sendMessage"
    assert send.result.raw == "Updates"
    assert send.params[0].type_ref.raw == "InputPeer"
    assert send.params[2].name == "random_id"
