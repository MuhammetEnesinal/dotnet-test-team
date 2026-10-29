"""Safe XML parsing for project and NuGet files that come from an untrusted repository.

Only UTF-8 / ASCII documents are accepted: UTF-16/UTF-32 (with or without BOM), NUL bytes and any other declared encoding are refused
BEFORE the document is looked at, because a DTD or entity declaration written in another encoding would not be seen by a byte scan.
Then <!DOCTYPE and <!ENTITY are refused (byte scan) and, as a second barrier, the parser itself raises on a doctype or entity declaration."""
from __future__ import annotations

import re

_DECLARED_ENCODING = re.compile(rb"^\s*<\?xml[^>]*?\bencoding\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_OK_ENCODINGS = (b"utf-8", b"utf8", b"us-ascii", b"ascii")


class XmlRefused(ValueError):
    pass


def check_bytes(data: bytes) -> bytes:
    """Returns the data without a UTF-8 BOM, or raises XmlRefused."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")):
        raise XmlRefused("encoding not accepted")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    if b"\x00" in data:
        raise XmlRefused("NUL byte")
    m = _DECLARED_ENCODING.match(data)
    if m and m.group(1).strip().lower() not in _OK_ENCODINGS:
        raise XmlRefused("declared encoding not accepted")
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise XmlRefused("DTD refused")
    return data


def _refuse(*_args):
    raise XmlRefused("DTD refused")


def parse_xml_bytes(data: bytes):
    """Root element of a safe document."""
    import xml.etree.ElementTree as ET
    from xml.parsers import expat
    data = check_bytes(data)
    guard = expat.ParserCreate()                 # second barrier: expat itself reports a doctype / entity declaration
    guard.StartDoctypeDeclHandler = _refuse
    guard.EntityDeclHandler = _refuse
    try:
        guard.Parse(data, True)
    except expat.ExpatError:
        pass                                     # not well-formed: ElementTree below reports it
    return ET.fromstring(data)
