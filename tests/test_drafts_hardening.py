from invoice_chaser.drafts import build_eml, write_drafts


def test_header_values_strip_newlines():
    msg = build_eml("a@example.com\r\nBcc: x@example.com", "Me", "me@example.com", "Hi\nthere", "body")
    assert "\n" not in msg["To"] and "Bcc" not in msg.keys()
    assert msg["Subject"] == "Hi there"


def test_colliding_ids_do_not_overwrite(tmp_path):
    base = dict(stage="nudge", to="a@example.com", from_name="Me", from_email="me@example.com", subject="s", body="b")
    paths = write_drafts([dict(base, invoice_id="A/1"), dict(base, invoice_id="A_1")], tmp_path)
    assert len({p.name for p in paths}) == 2
