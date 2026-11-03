"""Parsing + encoding edge cases seen in 1990s-2020s mail and on hosted IMAP servers."""
import unittest
from helpers import dcrmine as D


class HeaderDecoding(unittest.TestCase):
    def test_rfc2047_known_charsets(self):
        self.assertEqual(D.dh("=?iso-8859-1?Q?R=E9union_d=27=E9quipe?="), "Réunion d'équipe")
        self.assertEqual(D.dh("=?UTF-8?B?Q2Fmw6kgc2V0?="), "Café set")
        self.assertEqual(D.dh("=?ks_c_5601-1987?B?x9Gx2w==?="), "한글")

    def test_rfc2047_unknown_charset_falls_back_instead_of_leaving_encoded_word(self):
        for v in ("=?x-unknown?Q?caf=E9?= ok", "=?unknown-8bit?Q?caf=E9?= ok"):
            out = D.dh(v)
            self.assertNotIn("=?", out, v); self.assertIn("caf", out); self.assertTrue(out.endswith("ok"))

    def test_folded_and_multiline_headers_become_one_line(self):
        self.assertEqual(D.dh("Hello\r\n  world"), "Hello world")

    def test_raw_8bit_latin1_header_is_not_mangled(self):
        hb = b"From: Ren\xe9 Dupont <rene@x.example>\r\nSubject: R\xe9union d'\xe9quipe\r\nDate: Mon, 4 May 98 10:00:00 -0500\r\nMessage-ID: <a@b>\r\n\r\n"
        h = D.parse_headers(hb)
        self.assertEqual(h["subject"], "Réunion d'équipe")
        self.assertEqual(h["from"], ("rene@x.example", "René Dupont"))
        self.assertIn("raw-8bit-header", h["issues"])
        self.assertNotIn("�", h["subject"])

    def test_raw_8bit_utf8_header(self):
        h = D.parse_headers("Subject: Café set\r\nFrom: a@b.example\r\n\r\n".encode())
        self.assertEqual(h["subject"], "Café set")

    def test_to_ascii_for_search_terms(self):
        self.assertEqual(D.to_ascii("Café Müller"), "Cafe Muller")
        self.assertEqual(D.qs('say "hi"'), '"say hi"')


class Dates(unittest.TestCase):
    def ts(self, raw, idate=None):
        return D.parse_headers(f"Date: {raw}\r\nFrom: a@b.example\r\n\r\n".encode() if raw is not None else b"From: a@b.example\r\n\r\n", idate)

    def test_two_digit_year_and_named_zone(self):
        h = self.ts("Mon, 4 May 98 10:00:00 EST")
        self.assertEqual(D.ymd(h["ts"]), "1998-05-04"); self.assertEqual(h["date_bad"], 0)

    def test_missing_date_uses_internaldate_and_flags(self):
        h = self.ts(None, "14-Mar-2009 09:00:00 +0000")
        self.assertIn("no-date", h["issues"]); self.assertEqual(h["date_bad"], 1); self.assertEqual(D.ymd(h["ts"]), "2009-03-14")

    def test_garbage_date(self):
        h = self.ts("Sometime in March", "03-Mar-2006 10:00:00 +0000")
        self.assertIn("bad-date", h["issues"]); self.assertEqual(D.ymd(h["ts"]), "2006-03-03")

    def test_out_of_range_dates_fall_back(self):
        for raw in ("Thu, 01 Jan 1970 00:00:00 +0000", "Fri, 1 Jan 2100 00:00:00 +0000"):
            h = self.ts(raw, "05-Jun-2010 10:00:00 +0000")
            self.assertIn("date-out-of-range", h["issues"]); self.assertEqual(D.year_of(h["ts"]), 2010)

    def test_mbox_import_keeps_header_date_and_flags_divergence(self):
        h = self.ts("Wed, 3 Jun 1998 10:00:00 -0500", "14-Mar-2009 09:00:00 +0000")
        self.assertEqual(D.year_of(h["ts"]), 1998); self.assertIn("date-vs-internal", h["issues"])

    def test_comment_after_zone_and_no_weekday(self):
        self.assertEqual(D.ymd(self.ts("Tue, 2 Feb 2004 10:00:00 -0800 (PST)")["ts"]), "2004-02-02")
        self.assertEqual(D.ymd(self.ts("2 Feb 2004 10:00:00 +0000")["ts"]), "2004-02-02")

    def test_imap_date(self):
        self.assertEqual(D.imap_date(2019), "1-Jan-2019"); self.assertEqual(D.imap_date("2024-06-15"), "15-Jun-2024")
        with self.assertRaises(SystemExit): D.imap_date("June 2024")


class FolderNames(unittest.TestCase):
    def test_modified_utf7(self):
        self.assertEqual(D.utf7d("Entw&APw-rfe"), "Entwürfe")
        self.assertEqual(D.utf7d("Clients/M&APw-ller Studio"), "Clients/Müller Studio")
        self.assertEqual(D.utf7d("R&AOk-sum&AOk-s &- notes"), "Résumés & notes")
        self.assertEqual(D.utf7d("&ZeVnLIqe-"), "日本語")

    def test_list_lines(self):
        f = D.parse_list_line(b'(\\HasNoChildren \\Sent) "/" "Sent Messages"')
        self.assertEqual((f["name"], f["delim"]), ("Sent Messages", "/")); self.assertTrue(D.is_sent_folder(f))
        f = D.parse_list_line(b'(\\HasNoChildren) "." INBOX.Archive')
        self.assertEqual((f["name"], f["delim"]), ("INBOX.Archive", "."))
        f = D.parse_list_line((b'(\\HasNoChildren) "/" {25}', b"Clients/M&APw-ller Studio"))
        self.assertEqual(f["name"], "Clients/M&APw-ller Studio"); self.assertEqual(f["display"], "Clients/Müller Studio")
        f = D.parse_list_line(b'(\\Noselect) NIL "Shared"')
        self.assertEqual(f["delim"], "NIL")
        f = D.parse_list_line(b'() "/" "He said \\"hi\\""')
        self.assertEqual(f["name"], 'He said "hi"')

    def test_folder_kinds(self):
        k = lambda flags, name: {"flags": flags, "display": name}
        self.assertTrue(D.is_junk_folder(k("", "Deleted Messages")))
        self.assertTrue(D.is_junk_folder(k("\\Junk", "Spam")))
        self.assertFalse(D.is_junk_folder(k("", "Bulk orders")))      # a client folder, not junk
        self.assertFalse(D.is_junk_folder(k("", "Junkyard Films")))
        self.assertTrue(D.is_drafts_folder(k("\\Drafts", "Entwürfe")))
        self.assertTrue(D.is_sent_folder(k("", "Archive/Sent Items")))
        self.assertTrue(D.is_nonmail_folder(k("", "Notes")))

    def test_quote(self):
        self.assertEqual(D.Conn.q('a "b" \\c'), '"a \\"b\\" \\\\c"')


class FetchResponses(unittest.TestCase):
    HDR = b"Subject: Hi\r\nFrom: a@b.example\r\n\r\n"

    def test_literals_and_multiple_messages(self):
        data = [(b'1 (UID 5 RFC822.SIZE 900 INTERNALDATE "14-Mar-2009 09:00:00 +0000" BODY[HEADER.FIELDS (SUBJECT FROM)] {%d}' % len(self.HDR), self.HDR),
                b' BODYSTRUCTURE ("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 12 1 NIL NIL NIL))',
                (b'2 (UID 9 BODY[HEADER.FIELDS (SUBJECT FROM)] {%d}' % len(self.HDR), self.HDR), b")"]
        r = D.parse_fetch(data)
        self.assertEqual(sorted(r), [5, 9]); self.assertEqual(r[5]["size"], "900")
        self.assertEqual(r[5]["sections"]["HEADER.FIELDS (SUBJECT FROM)"], self.HDR)
        self.assertEqual(r[5]["struct"][0:2], ["TEXT", "PLAIN"])

    def test_unsolicited_flags_update_is_ignored(self):
        data = [b"3 FETCH (FLAGS (\\Seen))", (b"4 (UID 11 BODY[1]<0> {5}", b"hello"), b")"]
        r = D.parse_fetch([b"3 (FLAGS (\\Seen))"] + data[1:])
        self.assertEqual(list(r), [11]); self.assertEqual(r[11]["sections"]["1"], b"hello")

    def test_literal_filename_inside_bodystructure(self):
        fn = "Café.pdf".encode()
        data = [(b'1 (UID 7 BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 10 1 NIL NIL NIL)("APPLICATION" "PDF" ("NAME" {%d}' % len(fn), fn),
                b') NIL NIL "BASE64" 5000 NIL ("ATTACHMENT" ("FILENAME" "x.pdf")) NIL) "MIXED"))']
        r = D.parse_fetch(data)
        parts, atts = D.struct_info(r[7]["struct"])
        self.assertEqual(len(parts), 1); self.assertEqual(atts[0]["type"], "application/pdf")
        leaves = list(D.walk_struct(r[7]["struct"]))
        self.assertEqual([x["path"] for x in leaves], ["1", "2"])

    def test_nested_multipart_paths(self):
        st = D.sexp('((("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "QUOTED-PRINTABLE" 100 3 NIL NIL NIL)("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "QUOTED-PRINTABLE" 300 9 NIL NIL NIL) "ALTERNATIVE")("IMAGE" "JPEG" ("NAME" "a.jpg") NIL NIL "BASE64" 9000 NIL ("ATTACHMENT" ("FILENAME" "a.jpg")) NIL) "MIXED")')[0]
        leaves = list(D.walk_struct(st))
        self.assertEqual([(x["path"], x["type"]) for x in leaves], [("1.1", "text/plain"), ("1.2", "text/html"), ("2", "image/jpeg")])
        parts, atts = D.struct_info(st)
        self.assertEqual([p["type"] for p in parts], ["text/plain", "text/html"]); self.assertEqual(atts[0]["name"], "a.jpg")

    def test_uid_set(self):
        self.assertEqual(D.uid_set([10, 1, 2, 3, 7, 9]), "1:3,7,9:10"); self.assertEqual(D.uid_set([5]), "5")


class Bodies(unittest.TestCase):
    def test_truncated_utf8_is_not_mojibake(self):
        raw = "café ok déjà".encode()[:-1]          # partial fetch cut a multibyte char
        out = D.decode_part(raw, "8bit", "utf-8", "text/plain", truncated=True)
        self.assertEqual(out, "café ok déj")

    def test_declared_charset_wrong(self):
        self.assertEqual(D.decode_part("café".encode("cp1252"), "8bit", "us-ascii", "text/plain"), "café")
        self.assertEqual(D.decode_part("café".encode(), "8bit", "x-bogus-charset", "text/plain"), "café")

    def test_base64_and_qp(self):
        self.assertEqual(D.decode_part(b"Y2Fmw6k", "base64", "utf-8", "text/plain"), "café")   # missing padding
        self.assertEqual(D.decode_part(b"caf=C3=A9 =\r\nset", "quoted-printable", "utf-8", "text/plain"), "café set")

    def test_html_part_and_meta_charset(self):
        h = '<html><head><meta charset="windows-1252"><style>p{}</style></head><body><p>Caf\xe9</p><script>x()</script><p>Quote $1,200</p></body></html>'.encode("cp1252")
        out = D.decode_part(h, "8bit", "", "text/html")
        self.assertIn("Café", out); self.assertIn("Quote $1,200", out); self.assertNotIn("x()", out); self.assertNotIn("p{}", out)

    def test_quote_and_signature_split(self):
        t = "Sounds good, approved.\n\nThanks,\nMaria Lopez\nSenior Manager, Brand Experiences\nPepsiCo\nT 914-555-0188\n\nOn Mon, Feb 12, 2024 at 10:00 AM David wrote:\n> old text"
        new, quoted = D.split_quote(t)
        self.assertTrue(quoted.startswith("On Mon")); body, sig = D.split_sig(new)
        self.assertEqual(body, "Sounds good, approved."); ps = D.parse_sig(sig, "")
        self.assertEqual(ps["phone"], "914-555-0188"); self.assertIn("Manager", ps["title"])

    def test_outlook_style_quote(self):
        new, quoted = D.split_quote("ok\n\nFrom: Tom <t@x.example>\nSent: Monday\nTo: David\nSubject: x\n\nold")
        self.assertEqual(new, "ok"); self.assertTrue(quoted.startswith("From:"))

    def test_name_from_signature(self):
        self.assertEqual(D.sig_name("Thanks,\nKaren Wells\nCharge Artist\nWells Scenic\nT 615-555-0190", "kwells@wellsscenic.example"), "Karen Wells")
        self.assertEqual(D.sig_name("Best,\nJ. R. Ortiz", "x@y.example"), "")                     # not a clean full-name line
        self.assertEqual(D.sig_name("--\nSenior Producer\nMaria Lopez", "maria.lopez@pepsi.example"), "Maria Lopez")   # matched by the address
        self.assertEqual(D.sig_name("Cheers,\nReyes Metalworks\n615-555-0177", "info@reyes.example"), "")   # company line, not a person
        self.assertEqual(D.sig_name("Sent from my iPhone", "a@b.example"), "")
        self.assertEqual(D.parse_sig("Thanks,\nKaren Wells\nCharge Artist\nWells Scenic", "kwells@wellsscenic.example")["name"], "Karen Wells")

    def test_addr_name(self):
        self.assertEqual(D.addr_name("patrick.casani@cushwake.example"), "Patrick Casani")
        self.assertEqual(D.addr_name("kaler_davis@x.example"), "Kaler Davis")
        for ad in ("info.team@x.example", "sales.la@x.example", "print.marketing0243@x.example", "jsmith@x.example", "a.b@x.example", "anne.marie.lee@x.example"): self.assertEqual(D.addr_name(ad), "", ad)

    def test_clean_name(self):
        self.assertEqual(D.clean_name("Lopez, Maria"), "Maria Lopez")
        self.assertEqual(D.clean_name("'Maria Lopez'"), "Maria Lopez")
        self.assertEqual(D.clean_name("maria@pepsi.example", "maria@pepsi.example"), "")
        self.assertEqual(D.clean_name("Smith, Jones & Co, LLC"), "Smith, Jones & Co, LLC")
        self.assertTrue(D.is_contact_addr("maria@pepsi.example")); self.assertFalse(D.is_contact_addr("no-reply@x.example")); self.assertFalse(D.is_contact_addr("news@mailchimp.com"))

    def test_address_kinds(self):
        self.assertEqual(D.real_addr("jo.smith_at_acme_co_uk_qx4k81mn2p7rtz_9fw3c2ab@icloud.com"), "jo.smith@acme.co.uk")
        self.assertEqual(D.real_addr("orders_at_cafe_orderapp_io_hk29wq7ma1ps_4tt8b6xe@icloud.com"), "orders@cafe.orderapp.io")
        self.assertEqual(D.real_addr("info_at_socialsite_example_mw83jd02ka_b7c1e9f4@privaterelay.appleid.com"), "info@socialsite.example")
        self.assertEqual(D.real_addr("jo_at_home@icloud.com"), "jo_at_home@icloud.com")                 # not a relay shape: unchanged
        for a in ("unsubscribe@promo.example", "unsubscribe+a1b2@shop.example", "leave-x7y8.42@leave.events.example", "listunsubscribe-k3m4@email-store.example", "list-request@lists.example", "x@unsub.newsletterhost.example"):
            self.assertTrue(D.is_listcmd(a), a)
        self.assertFalse(D.is_listcmd("steve.leaver@studio.example")); self.assertFalse(D.is_listcmd("remy@studio.example"))
        for a in ("acct@communication.bigsoft.example", "info@alerts.pharmacy.example", "x@news.e.juicebar.example", "e-ticket@email.airline.example", "a@sportsmail.example", "robot@filmlog.example", "online-do-not-reply@agency.example", "x@privaterelay.appleid.com"):
            self.assertTrue(D.is_automated(a), a)
        for a in ("jo@law.stateu.example", "maria@pepsi.example", "jo@gmail.com", "jo@hotmail.co.uk", "kate@mail.com", "sam@ha.stateu.example"):
            self.assertFalse(D.is_automated(a), a)
        for a in ("info@shop.example", "hello@brand.example", "rsvp@museum.example", "reservations@hotel.example", "studio@dcr.example"): self.assertTrue(D.is_shared_inbox(a), a)
        for a in ("maria@pepsi.example", "infante@x.example", "helen@x.example"): self.assertFalse(D.is_shared_inbox(a), a)

    def test_signature_footer_is_not_a_company(self):
        for junk in ("Pronouns: [he/him]", "Privacy Policy", "Clinical Professor", "Co-Founder", "This email is confidential"): self.assertEqual(D.clean_org(junk), "", junk)
        for ok in ("Wells Scenic", "HARBOR STONE CAPITAL COMPANY, LLC", "Linden Row Fine Art"): self.assertEqual(D.clean_org(ok), ok.strip(" ,."))
        self.assertEqual(D.clean_title("JO SMITH | SVP, Head of Human Resources"), "SVP, Head of Human Resources")
        self.assertEqual(D.clean_title("Jo Smith | SVP, Sales"), "SVP, Sales"); self.assertEqual(D.clean_title("*Founder & Recruiter*"), "Founder & Recruiter")
        self.assertEqual(D.clean_title("more.  All visits go through our front desk and for an"), "")

if __name__ == "__main__": unittest.main()
