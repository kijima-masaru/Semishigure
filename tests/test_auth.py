import hashlib

from semishigure.sip.auth import DigestChallenge, DigestClient


def test_rfc2617_example_md5_no_qop():
    ch = DigestChallenge.parse('Digest realm="testrealm@host.com", nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", opaque="5ccc069c403ebaf9f0171e9517f40e41"')
    c = DigestClient("Mufasa", "Circle Of Life")
    hdr = c.authorization(ch, "GET", "/dir/index.html")
    ha1 = hashlib.md5(b"Mufasa:testrealm@host.com:Circle Of Life").hexdigest()
    ha2 = hashlib.md5(b"GET:/dir/index.html").hexdigest()
    expected = hashlib.md5(f"{ha1}:dcd98b7102dd2f0e8b11d0f600bfb0c093:{ha2}".encode()).hexdigest()
    assert f'response="{expected}"' in hdr
    assert 'opaque="5ccc069c403ebaf9f0171e9517f40e41"' in hdr
    assert "qop=" not in hdr


def test_qop_auth_nonce_count_increments():
    ch = DigestChallenge.parse('Digest realm="pbx", nonce="abc", qop="auth,auth-int", algorithm=MD5')
    c = DigestClient("9100", "pw")
    h1 = c.authorization(ch, "INVITE", "sip:8001@pbx")
    h2 = c.authorization(ch, "INVITE", "sip:8001@pbx")
    assert "nc=00000001" in h1 and "nc=00000002" in h2
    assert "qop=auth," in h1 or "qop=auth" in h1


def test_sha256_algorithm():
    ch = DigestChallenge.parse('Digest realm="pbx", nonce="n1", algorithm=SHA-256, qop="auth"')
    c = DigestClient("u", "p")
    hdr = c.authorization(ch, "REGISTER", "sip:pbx")
    assert "algorithm=SHA-256" in hdr and "response=" in hdr


def test_stale_flag():
    ch = DigestChallenge.parse('Digest realm="pbx", nonce="n1", stale=true')
    assert ch.stale
