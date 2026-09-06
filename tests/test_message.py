from semishigure.sip.message import NameAddr, SipMessage, SipUri, Via

INVITE = (
    b"INVITE sip:8001@pbx.example SIP/2.0\r\n"
    b"Via: SIP/2.0/UDP 10.0.0.2:5070;branch=z9hG4bKabc;rport\r\n"
    b"f: \"Caller\" <sip:9100@pbx.example>;tag=t1\r\n"
    b"To: <sip:8001@pbx.example>\r\n"
    b"Call-ID: cid@10.0.0.2\r\n"
    b"CSeq: 2 INVITE\r\n"
    b"Record-Route: <sip:proxy1;lr>\r\n"
    b"Record-Route: <sip:proxy2;lr>\r\n"
    b"X-LANG: en\r\n"
    b"Content-Type: application/sdp\r\n"
    b"l: 5\r\n"
    b"\r\n"
    b"v=0\r\nextra"
)


def test_parse_request_and_compact_headers():
    m = SipMessage.parse(INVITE)
    assert m.is_request and m.method == "INVITE"
    assert m.uri == "sip:8001@pbx.example"
    assert m.from_addr.tag == "t1"
    assert m.from_addr.display == "Caller"
    assert m.to_addr.tag is None
    assert m.cseq == (2, "INVITE")
    assert m.branch == "z9hG4bKabc"
    assert m.get("X-LANG") == "en"
    assert m.body == b"v=0\r\n"  # truncated by Content-Length
    assert m.record_routes == ["<sip:proxy1;lr>", "<sip:proxy2;lr>"]


def test_roundtrip_preserves_header_order():
    m = SipMessage.request("INVITE", "sip:x@y")
    m.add("Via", "SIP/2.0/UDP 1.2.3.4:5060;branch=z9hG4bK1")
    m.add("X-LANG", "en")
    m.add("X-ENABLE-TRANSCRIBE", "true")
    m.body = b"abc"
    raw = m.to_bytes()
    assert raw.startswith(b"INVITE sip:x@y SIP/2.0\r\nVia: ")
    assert b"X-LANG: en\r\nX-ENABLE-TRANSCRIBE: true\r\n" in raw
    assert b"Content-Length: 3\r\n\r\nabc" in raw
    back = SipMessage.parse(raw)
    assert back.get("X-ENABLE-TRANSCRIBE") == "true"


def test_parse_response():
    raw = b"SIP/2.0 401 Unauthorized\r\nVia: SIP/2.0/UDP 1.2.3.4;branch=z9hG4bKq\r\nCSeq: 1 REGISTER\r\nWWW-Authenticate: Digest realm=\"r\", nonce=\"n\"\r\nContent-Length: 0\r\n\r\n"
    m = SipMessage.parse(raw)
    assert m.is_response and m.status == 401
    assert m.cseq_method == "REGISTER"
    assert m.get("www-authenticate").startswith("Digest")


def test_uri_and_nameaddr():
    u = SipUri.parse("sip:9001@10.0.0.5:5080;transport=udp")
    assert (u.user, u.host, u.port) == ("9001", "10.0.0.5", 5080)
    assert u.params == {"transport": "udp"}
    assert str(u) == "sip:9001@10.0.0.5:5080;transport=udp"
    n = NameAddr.parse("<sip:9001@pbx>;tag=abc;expires=300")
    assert n.tag == "abc" and n.params["expires"] == "300"
    n2 = NameAddr.parse("sip:9001@pbx;tag=zz")
    assert n2.uri.user == "9001" and n2.tag == "zz"


def test_via():
    v = Via.parse("SIP/2.0/UDP 10.0.0.2:5070;branch=z9hG4bKabc;rport=5070;received=10.0.0.2")
    assert v.host == "10.0.0.2" and v.port == 5070 and v.branch == "z9hG4bKabc"
    assert v.params["rport"] == "5070"
