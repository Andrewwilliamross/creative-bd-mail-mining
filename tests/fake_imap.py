#!/usr/bin/env python3
"""Synthetic mailbox + minimal READ-ONLY IMAP server for testing dcrmine (plain TCP, localhost only).
All people/companies are fictional (*.example domains). Usage: python3 fake_imap.py serve --port 1143 [--drop-after N] [--log cmds.log]
Records every command; any non-read-only command is logged as VIOLATION and refused."""
import argparse, datetime as dt, email, email.header, email.utils, random, re, socketserver, sys, threading, time
from email.message import EmailMessage

MON = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
def idate(d): return f"{d.day:02d}-{MON[d.month-1]}-{d.year} {d.hour:02d}:{d.minute:02d}:{d.second:02d} +0000"
ME = ("David Clay Ross", "dcr.demo@mac.com")
SIG = lambda name, title, co, phone, site: f"\n\n{name}\n{title}\n{co}\nT {phone}\n{site}"
P = {  # key: (name, email, title, company, phone, site)
 "charlie": ("Charlie Ward", "charlie.ward@paramount-demo.example", "VP, Creative Advertising", "Paramount Pictures", "323-555-0141", "www.paramount-demo.example"),
 "pepsi": ("Maria Lopez", "maria.lopez@pepsi-demo.example", "Senior Manager, Brand Experiences", "PepsiCo", "914-555-0188", "www.pepsi-demo.example"),
 "tom": ("Tom Reyes", "tom@reyesmetalworks.example", "Owner", "Reyes Metalworks", "615-555-0177", "reyesmetalworks.example"),
 "jordan": ("Jordan Blake", "jordan@fieldhouse-demo.example", "Executive Producer", "Fieldhouse Creative", "212-555-0133", "www.fieldhouse-demo.example"),
 "sam": ("Sam Park", "sam@noblgear.example", "Creative Director", "NOBL Travel", "917-555-0120", "noblgear.example"),
 "lena": ("Lena Cho", "lena@propcloset.example", "Prop House Manager", "Prop Closet LA", "818-555-0166", "propcloset.example"),
 "dana1": ("Dana Moore", "dana.moore@brightline-demo.example", "Agency Producer", "Brightline Agency", "310-555-0155", "www.brightline-demo.example"),
 "dana2": ("Dana Moore", "dana.moore@halobev-demo.example", "Director of Creative Production", "Halo Beverages", "310-555-0199", "www.halobev-demo.example"),
 "ray": ("Ray Hollis", "ray@sparkpictures-demo.example", "Executive Producer", "Spark Pictures", "310-555-0102", "sparkpictures-demo.example"),
 "pat": ("Patricia Lund", "plund@lundphoto-demo.example", "Photographer Rep", "Lund & Co Photography", "212-555-0111", "lundphoto-demo.example"),
 "gary": ("Gary Finch", "gary@finchevents.example", "Owner", "Finch Events", "615-555-0144", "finchevents.example"),
 "patkim": ("Pat Kim", "pat.kim@oldagency-demo.example", "Art Producer", "Old Agency", "", ""),
}
def sig(k): n, e, t, c, ph, s = P[k]; return SIG(n, t, c, ph, s) if t else f"\n\n{n}"
class Box:
    def __init__(s): s.f = {}; s.n = 0
    def add(s, folder, raw, d, seen=None):
        s.n += 1
        if seen is None: seen = s.n % 6 != 0          # every 6th message starts unread: proves mining never sets \Seen
        s.f.setdefault(folder, []).append({"raw": raw, "d": d, "uid": len(s.f.get(folder, [])) + 1, "flags": {"\\Seen"} if seen else set()})
    def unseen(s): return {k: sorted(m["uid"] for m in v if "\\Seen" not in m["flags"]) for k, v in s.f.items()}
def mk(frm, to, subj, body, date, mid, irt=None, refs=None, cc=None, atts=(), hdrs=None, nodate=False, rawdate=None):
    m = EmailMessage(); m["From"] = email.utils.formataddr(frm) if frm[0] else frm[1]; m["To"] = ", ".join(email.utils.formataddr(t) if t[0] else t[1] for t in to)
    if cc: m["Cc"] = ", ".join(email.utils.formataddr(t) for t in cc)
    m["Subject"] = subj
    if not nodate and not rawdate: m["Date"] = email.utils.format_datetime(date.replace(tzinfo=dt.timezone.utc))
    m["Message-ID"] = f"<{mid}@demo.example>"
    if irt: m["In-Reply-To"] = f"<{irt}@demo.example>"
    if refs: m["References"] = " ".join(f"<{r}@demo.example>" for r in refs)
    for k, v in (hdrs or {}).items(): m[k] = v
    m.set_content(body)
    for fn, typ, data in atts:
        mt, st = typ.split("/"); m.add_attachment(data, maintype=mt, subtype=st, filename=fn)
    raw = m.as_bytes()
    return (b"Date: " + rawdate.encode() + b"\n" + raw) if (rawdate and not nodate) else raw   # inserted raw: old Pythons refuse to build junk dates
def build(seed=7):
    R = random.Random(seed); B = Box(); cnt = [0]
    def nid(): cnt[0] += 1; return f"m{cnt[0]:05d}"
    PDF = lambda t: b"%PDF-1.4\n% fake " + t.encode() + b"\n" + b"x" * 4000
    JPG = lambda n: b"\xff\xd8\xff\xe0" + bytes(R.getrandbits(8) for _ in range(2000)) * (n // 2000 + 1)
    def thread(subject, steps, inbox="INBOX", sentf="Sent Messages", archive=None):
        ids = []; 
        for i, (who, d, body, atts, *rest) in enumerate(steps):
            mid = nid(); to = rest[0] if rest else None; frm = ME if who == "me" else (P[who][0], P[who][1])
            others = [k for k in dict.fromkeys(s[0] for s in steps) if k != "me"]; peer = to or (others[0] if others else None)
            tolist = [(P[peer][0], P[peer][1])] if who == "me" else [ME]
            txt = body + (sig(who) if who != "me" else "\n\nThanks,\nDavid")
            cc = [(P[k][0], P[k][1]) for k in rest[1]] if len(rest) > 1 else None
            raw = mk(frm, tolist, ("Re: " if i else "") + subject, txt, d, mid, irt=ids[-1] if ids else None, refs=ids[:] if ids else None, atts=atts, cc=cc)
            B.add(sentf if who == "me" else (archive or inbox), raw, d)
            if who == "me" and R.random() < 0.3: B.add("Sent Items", raw, d)   # duplicate Sent folder
            ids.append(mid)
    D = lambda y, m, d, h=10: dt.datetime(y, m, d, h, R.randint(0, 59))
    # ---- Pepsi Rookie of the Year trophy: annual, 2019-2025
    base = {2019: 4800, 2020: 5000, 2021: 5200, 2022: 5600, 2023: 5900, 2024: 6200, 2025: 6500}
    for y, q in base.items():
        thread(f"Rookie of the Year trophy {y}", [
            ("pepsi", D(y, 2, 11), f"Hi David, time to kick off this year's Pepsi Rookie of the Year trophy for the NFL season awards. Same spec as last year? We need delivery by Jan 20.", []),
            ("me", D(y, 2, 12), f"Good to hear from you Maria. Same spec works. Rough quote for the trophy including fabrication, base and crate is ${q:,} with a 50% deposit. Rendering attached.", [(f"ROY_render_{y}.jpg", "image/jpeg", JPG(1_700_000))]),
            ("pepsi", D(y, 2, 15), f"Approved. PO attached, deposit of ${q//2:,} will be sent this week.", [(f"PepsiPO_{y}.pdf", "application/pdf", PDF("po"))]),
            ("me", D(y, 3, 2), f"Fabrication is underway with Reyes Metalworks, progress photos attached. Will ship Jan 10.", [(f"progress_{y}.jpg", "image/jpeg", JPG(400_000))], None, ["tom"] if y in (2021, 2022) else []),
            ("pepsi", D(y, 3, 20), "Love the finish, can we make the base a touch darker? Feedback from the league team attached.", []),
            ("me", D(y, 4, 5), f"Delivered, tracking number 1Z{y}9X. Invoice attached for the balance, ${q//2:,}, net 30.", [(f"Invoice_ROY_{y}.pdf", "application/pdf", PDF("inv"))]),
            ("pepsi", D(y, 5, 3), "Thank you for the payment details; payment sent today. Great work as always!", [])])
        thread(f"ROY trophy base fabrication quote {y}", [("me", D(y, 2, 13), f"Tom, can you quote the cast metal trophy base and crate again for this year's Pepsi trophy?", []), ("tom", D(y, 2, 14), f"Quote for fabrication: ${int(q*0.45):,}. 3 weeks. Quote attached.", [(f"Reyes_quote_{y}.pdf", "application/pdf", PDF("q"))])])
    thread("Shop schedule and crate sizes", [("me", D(2021, 3, 8), "Tom, here are the crate dimensions and the shop schedule for the next two weeks: 48x30x30, three crates, pickup Friday.", []), ("tom", D(2021, 3, 9), "Got it. We can have the base cast and finished by the 19th. Nothing else on the books that week.", [])])
    # ---- Paramount conference rooms
    for title, y, mo in (("The SpongeBob Movie", 2019, 6), ("Street Fighter Movie", 2022, 3), ("The Ebenezer Movie", 2024, 10), ("Dune Harbor", 2020, 9)):
        q = R.choice([9800, 12500, 14200, 11000])
        thread(f"{title} executive conference room", [
            ("charlie", D(y, mo, 3), f"David, we're pitching {title} to the marketing team on the 28th and want to transform the executive conference room into the world of the film. Can you design and build it? Budget around ${q:,}.", []),
            ("me", D(y, mo, 4), f"Absolutely. Here's a quick concept with renderings and a floor plan. Quote ${q:,} including load-in and strike.", [(f"{title.replace(' ','_')}_renders.jpg", "image/jpeg", JPG(1_200_000)), ("floorplan.pdf", "application/pdf", PDF("fp"))]),
            ("charlie", D(y, mo, 6), "Approved, we'll cut a purchase order. Can we add a hero prop? Notes from the team attached.", [("team_notes.pdf", "application/pdf", PDF("n"))]),
            ("me", D(y, mo, 24), "Load-in complete, installed and ready. Photos attached. Invoice will follow.", [("install1.jpg", "image/jpeg", JPG(500_000))]),
            ("charlie", D(y, mo + 1, 2), f"The room was a hit. Please send the invoice, we'll pay net 30.", [])])
    # ---- NOBL via Fieldhouse
    thread("NOBL Travel airport set - luggage spot", [
        ("jordan", D(2022, 4, 5), "David, Fieldhouse is producing a NOBL Travel luggage commercial and we need a designed airport set to showcase the new suitcase. Are you available the week of May 16? Brief attached.", [("NOBL_brief.pdf", "application/pdf", PDF("b"))]),
        ("me", D(2022, 4, 7), "Available. Estimate for the airport set design, build and props is $32,000. Mood board attached.", [("NOBL_moodboard.pdf", "application/pdf", PDF("mb"))]),
        ("jordan", D(2022, 4, 12), "Booked! Sam at NOBL signed off. Call sheet to follow.", []),
        ("sam", D(2022, 4, 13), "Excited to work with you, love the check-in counter concept. Can we add the NOBL logo to the departure board?", []),
        ("me", D(2022, 6, 1), "Shoot wrapped, strike complete. Invoice attached $32,000.", [("Invoice_NOBL.pdf", "application/pdf", PDF("i"))]),
        ("jordan", D(2022, 6, 20), "Payment sent, thanks David.", [])])
    thread("NOBL airport prop rentals", [("me", D(2022, 4, 20), "Lena, need airport seating, a baggage cart and signage for the NOBL commercial. Quote?", []), ("lena", D(2022, 4, 21), "Rental quote $3,400 for the week.", [("rental_quote.pdf", "application/pdf", PDF("r"))])])
    # ---- Dana: job change Brightline -> Halo
    thread("Fall catalog set build", [("dana1", D(2013, 8, 5), "David, we need a set for the Brightline fall catalog shoot, interior living room. Quote please.", []), ("me", D(2013, 8, 6), "Quote is $12,400 for design, build, props and strike.", []), ("dana1", D(2013, 8, 9), "Approved. Thanks!", []), ("me", D(2013, 9, 20), "Wrapped. Invoice attached $12,400.", [("inv.pdf", "application/pdf", PDF("i"))]), ("dana1", D(2013, 10, 15), "Payment sent. Loved working together.", [])])
    thread("Halo Summer launch backdrop", [("dana2", D(2021, 5, 3), "David! I've moved to Halo Beverages as Director of Creative Production. We need a summer launch backdrop for a shoot, can you quote?", []), ("me", D(2021, 5, 4), "Congrats on the new role! Quote $7,800 for the backdrop design and build.", []), ("dana2", D(2021, 5, 6), "Approved.", []), ("me", D(2021, 6, 15), "Delivered and installed. Invoice $7,800 attached.", [("inv2.pdf", "application/pdf", PDF("i"))]), ("dana2", D(2021, 7, 1), "Paid, thank you.", [])])
    thread("Holiday spread set - bid", [("patkim", D(2012, 10, 2), "David, we need a holiday spread set for the Old Agency client shoot. Budget $9,500. Can you bid?", []), ("me", D(2012, 10, 3), "Bid is $9,500 for design, build, props and strike.", []), ("patkim", D(2012, 10, 5), "Approved, go ahead.", []), ("me", D(2012, 11, 20), "Delivered and installed. Invoice attached $9,500.", [("inv3.pdf", "application/pdf", PDF("i"))]), ("me", D(2012, 12, 3), "Hi Pat, a gentle reminder that the balance due on the invoice is still outstanding.", []), ("patkim", D(2012, 12, 10), "Payment sent. Thanks!", [])])
    # ---- negative history (dispute) + bounce
    thread("Invoice 1188 overdue", [("me", D(2015, 3, 3), "Hi Gary, a reminder that invoice 1188 for $6,200 for the Finch Events stage set is past due.", []), ("gary", D(2015, 3, 5), "I'm disputing this invoice. I was unhappy with the delivery and I won't pay the full amount.", []), ("me", D(2015, 3, 6), "Understood, let's talk by phone.", [])])
    B.add("Sent Messages", mk(ME, [(P["patkim"][0], P["patkim"][1])], "Catching up", "Hi Pat, hope you're well. Thinking of our Old Agency days.\n\nDavid", D(2016, 4, 2), nid()), D(2016, 4, 2))
    B.add("INBOX", mk(("Mail Delivery System", "MAILER-DAEMON@mail.example"), [ME], "Undeliverable: Catching up", f"Delivery to the following recipient failed permanently:\n\n     {P['patkim'][1]}\n\nFinal-Recipient: rfc822; {P['patkim'][1]}\nStatus: 5.1.1\n550 5.1.1 User unknown", D(2016, 4, 2, 11), nid()), D(2016, 4, 2, 11))
    # ---- imported archive 1997-2004 ("Sets and Props Unlimited"), INTERNALDATE = import day, Date header = original
    imp = dt.datetime(2009, 3, 14, 9, 0)
    for subj, who, y, mo, q in (("Coke spot set - bid", "ray", 1998, 5, 18500), ("Pepsi print set quote", "pat", 1999, 9, 7200), ("Sets and Props Unlimited - invoice 1042", "ray", 2001, 2, 9100), ("Music video set bid", "ray", 2003, 7, 15000)):
        ids = []; steps = [(who, f"Hi David, we need a set built for our {subj.split(' - ')[0].lower()}. Please bid. We have a budget of ${q:,}.", []), ("me", f"Our bid is ${q:,}. Includes design, build, props and strike.", []), (who, "You got the job. Purchase order coming.", []), ("me", f"Delivered and installed. Invoice attached for ${q:,}.", []), (who, "Payment sent, thanks.", [])]
        for i, (w, body, atts) in enumerate(steps):
            d = dt.datetime(y, mo, 3 + i * 4, 10); mid = nid(); frm = ME if w == "me" else (P[w][0], P[w][1]); to = [(P[who][0], P[who][1])] if w == "me" else [ME]
            raw = mk(frm, to, ("Re: " if i else "") + subj, body + (sig(w) if w != "me" else "\n\nDavid"), d, mid, irt=ids[-1] if ids else None, refs=ids[:] if ids else None,
                     rawdate=email.utils.format_datetime(d.replace(tzinfo=dt.timezone.utc)) if y > 1999 else d.strftime("%a, %d %b %y %H:%M:%S -0500"))
            B.add("Archive/Sets and Props Unlimited (imported)", raw, imp); ids.append(mid)
    # ---- odd messages: no Date, bad Date, latin-1 subject
    B.add("INBOX", mk(("Unknown Sender", "someone@old-isp.example"), [ME], "Hi", "Old note with no date.", None, nid(), nodate=True), D(2005, 1, 2))
    B.add("INBOX", mk(("Weird Date", "weird@old-isp.example"), [ME], "Photos", "Bad date header.", None, nid(), rawdate="Sometime in March"), D(2006, 3, 3))
    B.add("INBOX", mk(("Rene Dupont", "rene@pariscreative.example"), [ME], "=?iso-8859-1?Q?R=E9union_d=27=E9quipe?=", "Bonjour David, reunion tomorrow.", D(2010, 6, 6), nid()), D(2010, 6, 6))
    # ---- noise: newsletters (ESP + list headers), receipts, promos, junk
    for i in range(70):
        y = R.randint(2008, 2026); d = D(y, R.randint(1, 12), R.randint(1, 28)); mid = nid()
        B.add("INBOX", mk(("Design Weekly", "news@designweekly.example"), [ME], f"Design Weekly #{i}: sets, trends and 20% off", "Read our newsletter. Unsubscribe anytime.", d, mid, hdrs={"List-Unsubscribe": "<mailto:unsub@designweekly.example>", "X-Mailchimp-ID": "abc", "Precedence": "bulk"}), d)
    for i in range(35):
        y = R.randint(2012, 2026); d = D(y, R.randint(1, 12), R.randint(1, 28))
        B.add("INBOX", mk(("Amazon", "no-reply@amazon-demo.example"), [ME], f"Your order #{1000+i} has shipped", "Your package is on its way.", d, nid()), d)
    for i in range(18):
        d = D(R.randint(2015, 2026), R.randint(1, 12), R.randint(1, 28))
        B.add("INBOX", mk(("Home Depot Deals", "deals@homedepot-demo.example"), [ME], f"Sale ends soon: save big on lumber ({i})", "Big savings.", d, nid()), d)
    for i in range(25):
        d = D(R.randint(2009, 2026), R.randint(1, 12), R.randint(1, 28))
        B.add("Junk", mk(("Prince", "x%d@spam.example" % i), [ME], "You won a prize", "Click here", d, nid()), d)
    B.add("Trash", mk(("Tom Reyes", P["tom"][1]), [ME], "old quote", "Quote $900 for shelves.", D(2012, 1, 1), nid(), atts=[("q.pdf", "application/pdf", PDF("q"))]), D(2012, 1, 1))
    B.add("Drafts", mk(ME, [("x", "x@x.example")], "draft", "unsent", D(2020, 1, 1), nid()), D(2020, 1, 1))
    # Sent mail with personal notes (voice sample)
    for i, (k, t) in enumerate((("charlie", "Charlie - saw the trailer for the new release, congrats to you and the team. Hope the Paramount lot is treating you well."), ("pepsi", "Maria, happy new year! Nashville is treating us well; the shop is busy."))):
        d = D(2023, 12, 20 + i); B.add("Sent Messages", mk(ME, [(P[k][0], P[k][1])], "Happy holidays", t + "\n\nTalk soon,\nDavid", d, nid()), d)
    # ---- 1990s encoding hell: raw 8-bit Latin-1 headers (no RFC 2047), 2-digit year + named zone, unknown charset label, mbox-import INTERNALDATE
    raw90 = (b"From: Ren\xe9 Dupont <rene@pariscreative.example>\r\nTo: dcr.demo@mac.com\r\nSubject: Devis d\xe9cor - caf\xe9 set\r\n"
             b"Date: Mon, 4 May 98 10:00:00 EST\r\nMessage-ID: <" + nid().encode() + b"@demo.example>\r\nContent-Type: text/plain; charset=iso-8859-1\r\n"
             b"Content-Transfer-Encoding: 8bit\r\n\r\nBonjour David, voici le devis pour le d\xe9cor du caf\xe9: $4,200.\r\n")
    B.add("Archive/Sets and Props Unlimited (imported)", raw90, imp)
    rawq = mk(("Studio Ko", "ko@studioko.example"), [ME], "=?x-unknown?Q?Caf=E9_set_quote?=", "Quote for the cafe set is $3,100.", D(2004, 2, 2), nid())
    B.add("Archive/Sets and Props Unlimited (imported)", rawq, imp)
    # ---- a client folder whose name contains 'Bulk' (must NOT be treated as junk) and a modified-UTF-7 folder sent as a LIST literal
    B.add("Bulk orders", mk(("Nina Ortiz", "nina@bulkbuy-demo.example"), [ME], "Quote request: 40 trade show booth backdrops", "Hi David, can you quote 40 backdrops for our trade show booth? Drawings attached.\n\nNina Ortiz\nProducer", D(2024, 9, 9), nid(), atts=[("drawings.pdf", "application/pdf", PDF("d"))]), D(2024, 9, 9))
    B.add("Clients/M&APw-ller Studio", mk(("Jonas M\u00fcller", "jonas@mueller-studio.example"), [ME], "Set build for the M\u00fcller showroom", "Hallo David, we would like a quote for the showroom set build.", D(2023, 5, 5), nid()), D(2023, 5, 5))
    # ---- a sender with a bare address (no display name): the contact's name must come from the signature
    k1, k2 = nid(), nid()
    B.add("INBOX", mk(("", "kwells@wellsscenic.example"), [ME], "Scenic paint quote for the showroom backdrop", "Hi David, paint quote attached for the showroom backdrop.\n\nThanks,\nKaren Wells\nCharge Artist\nWells Scenic\nT 615-555-0190",
                      D(2024, 6, 3), k1, atts=[("paint_quote.pdf", "application/pdf", PDF("pq"))]), D(2024, 6, 3))
    B.add("Sent Messages", mk(ME, [("", "kwells@wellsscenic.example")], "Re: Scenic paint quote for the showroom backdrop", "Thanks Karen, approved.\n\nDavid", D(2024, 6, 4), k2, irt=k1, refs=[k1]), D(2024, 6, 4))
    # ---- David's own second address (sent to under his own name) and a bare first.last address with no name anywhere
    B.add("Sent Messages", mk(ME, [("David Clay Ross", "david@dcrstudio.example")], "note to self: shop inventory", "Shop inventory list.\n\nDavid", D(2024, 1, 5), nid()), D(2024, 1, 5))
    B.add("Sent Messages", mk(ME, [ME, ("David Clay Ross", "david@dcrstudio.example")], "backup: render files", "Render files for backup.\n\nDavid", D(2024, 1, 6), nid()), D(2024, 1, 6))
    B.add("Sent Messages", mk(ME, [("", "jamie.rivera@riveraprops.example")], "Prop rental quote for the showroom", "Hi Jamie, can you quote the prop rental for the showroom set?\n\nDavid", D(2024, 6, 10), nid()), D(2024, 6, 10))
    # ---- clutter that must not become contacts (found on a real mailbox)
    LU = {"List-Unsubscribe": "<mailto:unsubscribe@lists.westsidelumber.example>"}
    for i in range(4):   # a store's shared inbox: bulk promos, plus one real support exchange David started
        B.add("INBOX", mk(("Westside Lumber", "hello@westsidelumber.example"), [ME], f"Westside Lumber weekly picks #{i}", "This week's picks.", D(2024, 3, 1 + i), nid(), hdrs=LU), D(2024, 3, 1 + i))
    w1, w2 = nid(), nid()
    B.add("Sent Messages", mk(ME, [("Westside Lumber", "hello@westsidelumber.example")], "Plywood order for the gallery build", "Hi, is my plywood order ready?\n\nDavid", D(2024, 3, 10), w1), D(2024, 3, 10))
    B.add("INBOX", mk(("Westside Lumber", "hello@westsidelumber.example"), [ME], "Re: Plywood order for the gallery build", "Yes, ready for pickup.", D(2024, 3, 11), w2, irt=w1, refs=[w1]), D(2024, 3, 11))
    B.add("Sent Messages", mk(ME, [("", "unsubscribe@lists.westsidelumber.example")], "unsubscribe", "unsubscribe", D(2024, 3, 12), nid()), D(2024, 3, 12))   # a mailto: unsubscribe click
    B.add("Sent Messages", mk(ME, [("", "leave-8f3a91c2@leave.e.shopdeals.example")], "", "", D(2024, 3, 12), nid()), D(2024, 3, 12))
    for i in range(2):   # a bank's sending subdomain, no list headers
        B.add("INBOX", mk(("Acme Bank", "rewards@mail.acmebank.example"), [ME], f"Your rewards summary {i}", "Points summary.", D(2024, 4, 1 + i), nid()), D(2024, 4, 1 + i))
    h1, h2 = nid(), nid()   # iCloud Hide-My-Email relays the real sender as name_at_domain_<id>_<id>@icloud.com
    B.add("INBOX", mk(("Kim Ng", "kim.ng_at_ngstudio_example_qx4k81mn2p7rtz_9fw3c2ab@icloud.com"), [ME], "Lighting rental availability", "Hi David, the lights are free that week.", D(2024, 5, 1), h1), D(2024, 5, 1))
    B.add("Sent Messages", mk(ME, [("Kim Ng", "kim.ng_at_ngstudio_example_qx4k81mn2p7rtz_9fw3c2ab@icloud.com")], "Re: Lighting rental availability", "Great, booking them.\n\nDavid", D(2024, 5, 2), h2, irt=h1, refs=[h1]), D(2024, 5, 2))
    guests = [(f"Guest {i}", f"guest{i}@partyguests.example") for i in range(14)]   # a mass email: the other recipients are not contacts
    B.add("INBOX", mk(("Paul Grant", "paul@grantevents.example"), [ME] + guests, "Holiday party photos", "Photos from the party!", D(2024, 12, 20), nid()), D(2024, 12, 20))
    return B
FOLDERS = {"INBOX": "", "Sent Messages": "\\Sent", "Sent Items": "", "Archive": "\\Archive", "Archive/Sets and Props Unlimited (imported)": "", "Junk": "\\Junk", "Trash": "\\Trash", "Drafts": "\\Drafts",
           "Bulk orders": "", "Clients": "\\Noselect \\HasChildren", "Clients/M&APw-ller Studio": "\\HasNoChildren"}
LITERAL_NAMES = {"Clients/M&APw-ller Studio"}   # sent as {n} literals in LIST, like some servers do for names with spaces/specials

# ---------------------------------------------------------------- server
def bs(m):
    if m.is_multipart(): return "(" + "".join(bs(p) for p in m.get_payload()) + f' "{m.get_content_subtype().upper()}")'
    mt, st = m.get_content_maintype().upper(), m.get_content_subtype().upper(); fn = m.get_filename(); payload = m.get_payload(decode=False); size = len(payload if isinstance(payload, (str, bytes)) else "")
    cs = m.get_content_charset(); enc = (m.get("Content-Transfer-Encoding") or "7bit").upper()
    prm = f'("CHARSET" "{cs.upper()}")' if mt == "TEXT" and cs else (f'("NAME" "{fn}")' if fn else "NIL")
    ext = f' NIL ("ATTACHMENT" ("FILENAME" "{fn}"))' if fn else " NIL NIL"
    return f'("{mt}" "{st}" {prm} NIL NIL "{enc}" {size}{" " + str(payload.count(chr(10)) if isinstance(payload, str) else 0) if mt == "TEXT" else ""}{ext})'
def part(m, path):
    if not m.is_multipart(): return m if path == "1" else None
    cur = m
    for i in path.split("."):
        if not cur.is_multipart(): return None
        cur = cur.get_payload()[int(i) - 1]
    return cur
STATE = {"fetches": 0, "drop_after": None, "dropped": False, "log": None, "violations": [], "uidvalidity": {}, "omit_uidvalidity": False,
         "drop_mid_at": None, "no_uids": set(), "fetch_delay": 0.0}
def uv(n): return STATE["uidvalidity"].get(n, 1)
class H(socketserver.StreamRequestHandler):
    def send(s, t): s.wfile.write(t if isinstance(t, bytes) else t.encode("latin-1")); s.wfile.flush()
    def log(s, line):
        if STATE["log"]:
            with open(STATE["log"], "a") as fh: fh.write(line + "\n")
    def handle(s):
        s.sel = None; s.send("* OK fake imap ready\r\n")
        while True:
            line = s.rfile.readline()
            if not line: return
            line = line.decode("latin-1").rstrip("\r\n"); tag, _, rest = line.partition(" "); cmd, _, args = rest.partition(" "); C = cmd.upper()
            safe = re.sub(r'(LOGIN\s+\S+\s+).*', r'\1***', line); s.log(safe)
            if C == "CAPABILITY": s.send("* CAPABILITY IMAP4rev1\r\n" + f"{tag} OK done\r\n")
            elif C == "LOGIN": s.send(f"{tag} OK logged in\r\n")
            elif C == "NOOP": s.send(f"{tag} OK done\r\n")
            elif C == "LOGOUT": s.send(f"* BYE\r\n{tag} OK done\r\n"); return
            elif C == "LIST":
                out = ""
                for n, fl in FOLDERS.items():
                    if n in LITERAL_NAMES: out += f'* LIST ({fl}) "/" {{{len(n)}}}\r\n{n}\r\n'
                    else: out += f'* LIST ({fl}) "/" "{n}"\r\n' if " " in n or "/" in n else f'* LIST ({fl}) "/" {n}\r\n'
                s.send(out + f"{tag} OK done\r\n")
            elif C == "STATUS":
                n = re.match(r'"?([^"]*)"?', args).group(1); msgs = BOX.f.get(n, []); un = sum(1 for m in msgs if "\\Seen" not in m["flags"])
                s.send(f'* STATUS "{n}" (MESSAGES {len(msgs)} UNSEEN {un} UIDNEXT {len(msgs)+1} UIDVALIDITY {uv(n)})\r\n{tag} OK done\r\n')
            elif C == "EXAMINE":
                n = re.match(r'"?([^"]*)"?', args).group(1)
                if n not in BOX.f and n not in FOLDERS: s.send(f"{tag} NO no such folder\r\n"); continue
                if "\\Noselect" in FOLDERS.get(n, ""): s.send(f"{tag} NO [NONEXISTENT] not selectable\r\n"); continue
                s.sel = n; uvl = "" if STATE["omit_uidvalidity"] else f"* OK [UIDVALIDITY {uv(n)}] ok\r\n"
                s.send(f"* {len(BOX.f.get(n, []))} EXISTS\r\n{uvl}{tag} OK [READ-ONLY] examined\r\n")
            elif C == "UID" and args.split(" ", 1)[0].upper() in ("FETCH", "SEARCH"):
                sub, _, a2 = args.partition(" ")
                if sub.upper() == "SEARCH": s.search(tag, a2)
                else: s.fetch(tag, a2, line)
            else:
                STATE["violations"].append(line); s.log("VIOLATION " + safe); s.send(f"{tag} BAD command refused (read-only fake)\r\n")
    # --- search
    def search(s, tag, a2):
        toks = re.findall(r'"[^"]*"|\S+', a2); pos = [0]; msgs = BOX.f.get(s.sel, [])
        def pd(x): 
            d, mo, y = x.strip('"').split("-"); return dt.date(int(y), MON.index(mo) + 1, int(d))
        def parse():
            t = toks[pos[0]]; pos[0] += 1; T = t.upper()
            if T == "ALL": return lambda m: True
            if T == "UNSEEN": return lambda m: "\\Seen" not in m["flags"]
            if T == "SEEN": return lambda m: "\\Seen" in m["flags"]
            if T in ("SINCE", "BEFORE", "SENTSINCE", "SENTBEFORE"):
                d = pd(toks[pos[0]]); pos[0] += 1
                def f(m, T=T, d=d):
                    if T.startswith("SENT"):
                        try: x = email.utils.parsedate_to_datetime(email.message_from_bytes(m["raw"])["Date"]).date()
                        except Exception: return False
                    else: x = m["d"].date()
                    return x >= d if T.endswith("SINCE") else x < d
                return f
            if T in ("SUBJECT", "FROM", "TO", "CC", "TEXT", "BODY"):
                v = toks[pos[0]].strip('"').lower(); pos[0] += 1
                def f(m, T=T, v=v):
                    e = email.message_from_bytes(m["raw"])
                    if T in ("TEXT", "BODY"): return v in m["raw"].decode("utf-8", "replace").lower() if T == "TEXT" else v in (e.get_payload(decode=False) if isinstance(e.get_payload(decode=False), str) else "").lower()
                    h = str(email.header.make_header(email.header.decode_header(str(e.get(T.title(), ""))))).lower(); return v in h
                return f
            if T == "OR": a, b = parse(), parse(); return lambda m: a(m) or b(m)
            if T == "NOT": a = parse(); return lambda m: not a(m)
            if T == "UID" or re.match(r"^[\d:*,]+$", T):
                if T == "UID": spec = toks[pos[0]]; pos[0] += 1
                else: spec = t
                rng = []
                for part_ in spec.split(","):
                    lo, _, hi = part_.partition(":"); rng.append((int(lo) if lo != "*" else 10**9, (10**9 if hi == "*" else int(hi)) if hi else int(lo) if lo != "*" else 10**9))
                return lambda m: any(min(a, b) <= m["uid"] <= max(a, b) for a, b in rng)
            raise ValueError("unsupported criterion " + t)
        preds = []
        try:
            while pos[0] < len(toks): preds.append(parse())
        except Exception as e: s.send(f"{tag} BAD {e}\r\n"); return
        hit = [m["uid"] for m in msgs if all(p(m) for p in preds)]
        s.send("* SEARCH " + " ".join(map(str, hit)) + "\r\n" + f"{tag} OK done\r\n" if hit else f"* SEARCH\r\n{tag} OK done\r\n")
    # --- fetch
    def fetch(s, tag, a2, line):
        spec, _, items = a2.partition(" "); STATE["fetches"] += 1
        nonpeek = bool(re.search(r"BODY\[|RFC822(?!\.SIZE)", items.replace("BODY.PEEK[", "BODY.PEEK(")))
        if nonpeek: STATE["violations"].append("non-PEEK fetch: " + line); s.log("VIOLATION non-peek")
        if STATE["fetch_delay"]: time.sleep(STATE["fetch_delay"])
        if STATE["drop_after"] and STATE["fetches"] > STATE["drop_after"] and not STATE["dropped"]:
            STATE["dropped"] = True; s.log("INJECTED DISCONNECT"); s.connection.close(); return
        uids = set()
        for p_ in spec.split(","):
            lo, _, hi = p_.partition(":"); uids.update(range(int(lo), int(hi or lo) + 1))
        msgs = {m["uid"]: m for m in BOX.f.get(s.sel, [])}
        if any((s.sel, u) in STATE["no_uids"] for u in uids): s.log("INJECTED NO"); s.send(f"{tag} NO [UNAVAILABLE] backend unavailable for one message\r\n"); return
        out = b""
        for u in sorted(uids):
            if u not in msgs: continue
            m = msgs[u]; msg = email.message_from_bytes(m["raw"]); seq = u; pieces = [f"UID {u}".encode()]
            for tok in re.finditer(r"RFC822\.SIZE|INTERNALDATE|FLAGS|BODYSTRUCTURE|BODY\.PEEK\[([^\]]*)\](<\d+\.\d+>)?", items):
                T = tok.group(0)
                if T == "RFC822.SIZE": pieces.append(f"RFC822.SIZE {len(m['raw'])}".encode())
                elif T == "INTERNALDATE": pieces.append(f'INTERNALDATE "{idate(m["d"])}"'.encode())
                elif T == "FLAGS": pieces.append(("FLAGS (" + " ".join(sorted(m["flags"])) + ")").encode())
                elif T == "BODYSTRUCTURE": pieces.append(("BODYSTRUCTURE " + bs(msg)).encode("latin-1", "replace"))
                else:
                    sec = tok.group(1); hdr, _, body = m["raw"].partition(b"\r\n\r\n") if b"\r\n\r\n" in m["raw"] else m["raw"].partition(b"\n\n")
                    if sec.upper().startswith("HEADER.FIELDS"):
                        want = {x.upper() for x in re.search(r"\(([^)]*)\)", sec).group(1).split()}; lines = re.split(rb"\r?\n(?![ \t])", hdr)
                        data = b"\r\n".join(l for l in lines if l.split(b":")[0].decode("latin-1").upper() in want) + b"\r\n\r\n"
                    elif sec.upper() == "HEADER": data = hdr + b"\r\n\r\n"
                    elif sec == "1" and not msg.is_multipart(): data = body          # raw bytes, exactly as stored (8-bit Latin-1 stays Latin-1)
                    else:
                        p = part(msg, sec); pl = p.get_payload(decode=False) if p is not None else ""; data = pl.encode("utf-8", "surrogateescape") if isinstance(pl, str) else (pl if isinstance(pl, bytes) else b"")
                    lab = f"BODY[{sec}]"
                    if tok.group(2): a_, b_ = re.findall(r"\d+", tok.group(2)); data = data[int(a_):int(a_) + int(b_)]; lab += f"<{a_}>"
                    pieces.append(lab.encode() + b" {" + str(len(data)).encode() + b"}\r\n" + data)
            out += f"* {seq} FETCH (".encode() + b" ".join(pieces) + b")\r\n"
            if nonpeek: m["flags"].add("\\Seen")       # what a real server does on BODY[] / RFC822: the audit must catch it
        if STATE["drop_mid_at"] and STATE["fetches"] == STATE["drop_mid_at"] and len(out) > 40:
            s.log("INJECTED MID-FETCH DISCONNECT"); s.send(out[:len(out) // 2]); s.connection.close(); return
        s.send(out + f"{tag} OK done\r\n".encode())
BOX = None
class Srv(socketserver.ThreadingTCPServer): allow_reuse_address = True; daemon_threads = True
def serve(port, drop_after=None, log=None, quiet=False):
    global BOX; BOX = build(); STATE.update(drop_after=drop_after, log=log, fetches=0, dropped=False, violations=[], uidvalidity={}, omit_uidvalidity=False, drop_mid_at=None, no_uids=set(), fetch_delay=0.0)
    if log:
        with open(log, "w"): pass
    srv = Srv(("127.0.0.1", port), H)
    if not quiet: print(f"fake imap on 127.0.0.1:{srv.server_address[1]} - {sum(len(v) for v in BOX.f.values())} messages in {len(BOX.f)} folders", flush=True)
    return srv
if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("serve"); ap.add_argument("--port", type=int, default=1143); ap.add_argument("--drop-after", type=int); ap.add_argument("--log", default="imap-commands.log")
    a = ap.parse_args(); serve(a.port, a.drop_after, a.log).serve_forever()
