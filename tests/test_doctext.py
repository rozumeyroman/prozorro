from docfiles import make_docx, make_pdf, make_xlsx, make_zip, sign

from prozorro_mcp.doctext import extract_text


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_pdf_text_and_scan(tmp_path):
    r = extract_text(write(tmp_path, "a.pdf", make_pdf("FortiGate-200F 2 pcs 410000 UAH")))
    assert "FortiGate-200F" in r.text and r.note is None
    r = extract_text(write(tmp_path, "scan.pdf", make_pdf(None)))
    assert not r.ok and "OCR" in r.note


def test_office_formats(tmp_path):
    r = extract_text(
        write(tmp_path, "t.docx", make_docx(["Цінова пропозиція"], [["Товар", "Ціна"], ["FortiGate-200F", "410 000"]]))
    )
    assert r.text.splitlines() == ["Цінова пропозиція", "Товар | Ціна", "FortiGate-200F | 410 000"]
    r = extract_text(write(tmp_path, "t.xlsx", make_xlsx([["Модель", "Кількість"], ["FortiGate-200F", 2]])))
    assert "FortiGate-200F | 2" in r.text
    # format is detected by content, not by the (often wrong) title
    r = extract_text(write(tmp_path, "без розширення", make_docx(["Sophos XGS 4300"])))
    assert r.text == "Sophos XGS 4300"


def test_signed_and_archived(tmp_path):
    r = extract_text(write(tmp_path, "пропозиція.docx.p7s", sign(make_docx(["Check Point 9100"]))))
    assert r.text == "Check Point 9100"
    r = extract_text(write(tmp_path, "sign.p7s", sign(None)))
    assert not r.ok and "окремий файл підпису" in r.note
    archive = make_zip({"a/ціна.docx.p7s": sign(make_docx(["ESET PROTECT"])), "скан.jpg": b"\xff\xd8\xff\xe0jpeg"})
    r = extract_text(write(tmp_path, "docs.zip", archive))
    assert "ESET PROTECT" in r.text and "скан.jpg" in r.note


def test_broken_and_unknown(tmp_path):
    r = extract_text(write(tmp_path, "x.pdf", b"%PDF-fake broken"))
    assert not r.ok and r.note
    r = extract_text(write(tmp_path, "old.doc", b"\xd0\xcf\x11\xe0" + b"\0" * 100))
    assert "старий формат" in r.note
