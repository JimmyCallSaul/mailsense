"""
MailSense AI - Powered by Jev
Streamlit prototype: a Gmail-style inbox with 250 (synthetic) e-mails that Jev
sorts semantically while you type.

* Type a search term -> after a short pause (no Enter needed) it goes to Python.
* Python sends the 250 e-mails to Jev in parallel packages of 50 (backend-to-backend,
  so no CORS problem). Jev returns a probability (0..1) per e-mail.
* The browser part (custom Streamlit component, plain JavaScript) moves the matching
  e-mails to the top of the inbox. Click an e-mail to read it.

All e-mails are invented (no real data). No fake results: if the API fails, the exact
HTTP status and body are shown.
"""

import os
import random
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import requests
import streamlit as st
import streamlit.components.v1 as components


def _get_secret(name, default=None):
    """Read a value from Streamlit secrets (cloud), else env variable, else default."""
    try:
        return st.secrets[name]
    except Exception:
        return os.environ.get(name, default)


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
# Local use: paste your key into the SECOND pair of quotes below.
# Cloud use: leave the placeholder and put JEV_API_KEY into the app's Secrets.
API_KEY = _get_secret("JEV_API_KEY", "YOUR_API_KEY_HERE")
# Optional: if APP_PASSWORD is set in the Secrets, visitors must enter it first.
APP_PASSWORD = _get_secret("APP_PASSWORD", None)

REQUEST_TIMEOUT_S = 40
N_EMAILS = 250
CHUNK_SIZE = 50      # questions per API request (keep <= 64)
MAX_PARALLEL = 5     # parallel requests; lower this if you get HTTP 429 errors
MIN_QUERY_CHARS = 2  # API is only called from this many characters on
BODY_CHARS_FOR_API = 700

# ----------------------------------------------------------------------------
# Synthetic e-mail data (deterministic: same 250 mails on every start)
# ----------------------------------------------------------------------------
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
          "September", "Oktober", "November", "Dezember"]
MONTHS_SHORT = ["Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.",
                "Sept.", "Okt.", "Nov.", "Dez."]
BASE_TIME = datetime(2026, 9, 28, 9, 30)  # "now" inside the demo inbox

FIRST = ["Anna", "Lukas", "Sabrina", "Marco", "Julia", "Reto", "Nadine", "Stefan", "Elena",
         "Patrick", "Miriam", "Thomas", "Laura", "David", "Sandra", "Fabian", "Claudia",
         "Simon", "Nicole", "Jonas", "Petra", "Andreas", "Karin", "Beat", "Livia"]
LAST = ["Müller", "Meier", "Schmid", "Keller", "Weber", "Huber", "Fischer", "Brunner",
        "Steiner", "Baumann", "Gerber", "Frei", "Zimmermann", "Moser", "Wyss", "Kunz",
        "Roth", "Graf", "Suter", "Lüthi"]
COMPANIES = [
    ("Alpina Logistik AG", "alpina-logistik.ch"), ("Bergmann Treuhand", "bergmann-treuhand.ch"),
    ("NovaTech Solutions", "novatech.ch"), ("Seeblick Hotels", "seeblick-hotels.ch"),
    ("Kaufmann Maschinenbau", "kaufmann-maschinen.ch"), ("Helvetia Consulting", "helvetia-consulting.ch"),
    ("Pilatus Elektro", "pilatus-elektro.ch"), ("Rigi Backwaren", "rigi-backwaren.ch"),
    ("Swiss Print GmbH", "swissprint.ch"), ("Limmat Software", "limmat-software.ch"),
]
OWN_DOMAIN = "alpenwerk.ch"
PRODUKTE = ["Bürostühle", "Laserdrucker", "Notebooks", "Verpackungsmaterial", "Ersatzteile",
            "Kaffeemaschinen", "Sicherheitsschuhe", "Regale", "Bildschirme", "Werkzeugsets"]
STELLEN = ["Marketing Manager", "Softwareentwickler", "Buchhalterin", "Projektleiter",
           "Kundenberaterin", "Data Analyst", "Logistikfachmann", "Sachbearbeiterin"]
PROJEKTE = ["Phoenix", "Neuland", "Kundenportal 2.0", "Lagerdigitalisierung", "Rebranding"]
THEMEN = ["Künstliche Intelligenz im Alltag", "Cybersicherheit für KMU", "Nachhaltige Lieferketten",
          "Steuern und Abgaben 2027", "Agiles Projektmanagement"]
ORTE = ["Berlin", "Wien", "Mailand", "Hamburg", "München", "Barcelona", "Amsterdam"]

# (subject, body, sender)   sender: ("p",) external person | ("c",) colleague |
#                                    ("s", prefix) company system mail | ("f", name, address) fixed
TEMPLATES = {
    "rechnung": [
        ("Rechnung Nr. {nr} – fällig am {faellig}",
         "Guten Tag,\n\nim Anhang finden Sie die Rechnung Nr. {nr} über {betrag} für die gelieferten {produkt}. Bitte überweisen Sie den Betrag bis zum {faellig} auf unser Konto.\n\nFreundliche Grüsse\n{name}\nBuchhaltung, {company}", ("p",)),
        ("Zahlungserinnerung: Rechnung {nr}",
         "Guten Tag,\n\nleider konnten wir bis heute keinen Zahlungseingang für die Rechnung {nr} ({betrag}) feststellen. Bitte begleichen Sie den offenen Betrag innerhalb von {tage} Tagen, damit keine Mahngebühren anfallen.\n\nFreundliche Grüsse\n{name}, {company}", ("p",)),
        ("Gutschrift zu Rechnung {nr}",
         "Guten Tag,\n\nwir haben Ihnen eine Gutschrift über {betrag} ausgestellt, da ein Teil der Lieferung retourniert wurde. Der Betrag wird mit Ihrer nächsten Zahlung verrechnet.\n\nBeste Grüsse\n{name}, {company}", ("p",)),
        ("Zahlungseingang bestätigt",
         "Hallo,\n\nvielen Dank, Ihre Zahlung über {betrag} für Rechnung {nr} ist bei uns eingegangen. Eine Quittung finden Sie im Anhang.\n\nFreundliche Grüsse\n{name}, {company}", ("p",)),
    ],
    "anfrage": [
        ("Anfrage Offerte {produkt}",
         "Guten Tag,\n\nwir planen die Anschaffung von {menge} {produkt} und bitten Sie um eine Offerte inklusive Lieferzeit und Mengenrabatt. Könnten Sie uns diese bis {datum} zukommen lassen?\n\nBesten Dank und freundliche Grüsse\n{name}, {company}", ("p",)),
        ("Rückfrage zu Ihrem Angebot",
         "Hallo,\n\nvielen Dank für Ihr Angebot. Wir haben noch eine Rückfrage zu den Zahlungsbedingungen und zur Garantie. Wäre ein kurzes Telefonat diese Woche möglich?\n\nFreundliche Grüsse\n{name}, {company}", ("p",)),
        ("Preisliste {jahr} gewünscht",
         "Guten Tag,\n\nkönnten Sie uns bitte Ihre aktuelle Preisliste für {produkt} zusenden? Wir sind an einer langfristigen Zusammenarbeit interessiert.\n\nBeste Grüsse\n{name}, {company}", ("p",)),
    ],
    "reklamation": [
        ("Reklamation Bestellung {nr}",
         "Guten Tag,\n\ndie gelieferten {produkt} sind beschädigt bei uns angekommen. Zwei Kartons waren nass, und die Ware ist unbrauchbar. Wir erwarten eine rasche Lösung und Ersatz.\n\nFreundliche Grüsse\n{name}, {company}", ("p",)),
        ("Falsche Ware geliefert",
         "Hallo,\n\nbei Bestellung {nr} haben wir leider die falschen Artikel erhalten. Bestellt waren {produkt}, geliefert wurde etwas völlig anderes. Bitte melden Sie sich so schnell wie möglich.\n\nGruss\n{name}, {company}", ("p",)),
        ("Beschwerde: Lieferung verspätet",
         "Guten Tag,\n\nunsere Lieferung hätte bereits vor {tage} Tagen eintreffen sollen und ist noch immer nicht da. Das bringt unseren Betrieb in Schwierigkeiten. Bitte klären Sie umgehend den Stand.\n\nMit unzufriedenen Grüssen\n{name}, {company}", ("p",)),
    ],
    "bewerbung": [
        ("Bewerbung als {stelle}",
         "Sehr geehrte Damen und Herren,\n\nmit grossem Interesse habe ich Ihre Ausschreibung als {stelle} gelesen. Ich bringe {jahre} Jahre Berufserfahrung mit und würde mich über ein Vorstellungsgespräch freuen. Lebenslauf und Zeugnisse finden Sie im Anhang.\n\nFreundliche Grüsse\n{name}", ("f", None, None)),
        ("Initiativbewerbung {stelle}",
         "Guten Tag,\n\nauch wenn aktuell keine Stelle ausgeschrieben ist, möchte ich mich gerne als {stelle} bei Ihnen bewerben. Ich habe kürzlich mein Studium abgeschlossen und suche eine neue Herausforderung.\n\nBeste Grüsse\n{name}", ("f", None, None)),
    ],
    "meeting": [
        ("Terminbestätigung {wochentag}, {uhrzeit} Uhr",
         "Hallo,\n\nhiermit bestätige ich unser Meeting am {wochentag} um {uhrzeit} Uhr im Sitzungszimmer 3. Wir besprechen den Projektstand und die nächsten Schritte.\n\nBis dann\n{name}", ("c",)),
        ("Einladung: Kick-off Projekt {projekt}",
         "Guten Tag,\n\nich lade Sie herzlich zum Kick-off des Projekts {projekt} ein. Termin: {wochentag}, {uhrzeit} Uhr, per Videocall. Bitte bestätigen Sie kurz Ihre Teilnahme.\n\nFreundliche Grüsse\n{name}", ("c",)),
        ("Terminverschiebung",
         "Hallo,\n\nleider muss ich unseren Termin vom {wochentag} verschieben, da ich kurzfristig beim Kunden sein muss. Hättest du am {wochentag2} um {uhrzeit} Uhr Zeit?\n\nDanke für dein Verständnis\n{name}", ("c",)),
    ],
    "newsletter": [
        ("{prozent}% Rabatt nur diese Woche!",
         "Liebe Kundin, lieber Kunde,\n\nnur bis Sonntag erhalten Sie {prozent}% Rabatt auf unser gesamtes Sortiment. Nutzen Sie den Code SPARE{prozent} an der Kasse und profitieren Sie von kostenlosem Versand.\n\nIhr Team von {company}", ("s", "newsletter")),
        ("Newsletter {monat}: Neuheiten und Trends",
         "Hallo,\n\nin unserem Newsletter erfahren Sie, was diesen Monat neu ist: neue Produkte, spannende Kundenprojekte und Tipps für den Alltag. Jetzt lesen und inspirieren lassen!\n\nIhr Team von {company}\nAbmelden", ("s", "newsletter")),
    ],
    "phishing": [
        ("Ihr Konto wurde gesperrt – sofort handeln!",
         "Sehr geehrter Kunde,\n\nwir haben ungewöhnliche Aktivitäten festgestellt. Ihr Konto wird in 24 Stunden gesperrt. Bestätigen Sie umgehend Ihre Daten über den folgenden Link: http://sicherheits-check-login.xyz/verify\n\nIhr Sicherheitsteam", ("f", "Sicherheitsdienst", "support@konto-sicherheit-check.xyz")),
        ("Herzlichen Glückwunsch! Sie haben {betrag} gewonnen",
         "Sie wurden als Gewinner von {betrag} ausgewählt! Klicken Sie jetzt auf den Link und geben Sie Ihre Bankdaten an, um den Gewinn zu erhalten: http://gewinn-auszahlung.top/claim Angebot läuft in 2 Stunden ab!", ("f", "Gewinnspiel-Zentrale", "info@gewinn-auszahlung.top")),
        ("Letzte Mahnung – Paket kann nicht zugestellt werden",
         "Ihr Paket konnte nicht zugestellt werden. Bezahlen Sie eine Gebühr von CHF 1.99 über den folgenden Link, um die Zustellung zu veranlassen: http://post-zustellung-info.top/pay", ("f", "Paketdienst Info", "noreply@post-zustellung-info.top")),
    ],
    "it": [
        ("Passwort läuft in {tage} Tagen ab",
         "Hallo,\n\ndein Passwort für das Firmennetzwerk läuft in {tage} Tagen ab. Bitte ändere es rechtzeitig über das Self-Service-Portal, damit du weiterhin Zugriff auf E-Mail und Laufwerke hast.\n\nIT-Support", ("f", "IT-Support", "it-support@" + OWN_DOMAIN)),
        ("Geplante Wartung: ERP-System nicht verfügbar",
         "Liebe Kolleginnen und Kollegen,\n\nam {wochentag} von 22:00 bis 02:00 Uhr wird das ERP-System gewartet und steht nicht zur Verfügung. Bitte speichert eure Arbeit rechtzeitig.\n\nIT-Support", ("f", "IT-Support", "it-support@" + OWN_DOMAIN)),
        ("Ticket #{nr} gelöst: Drucker im 2. Stock",
         "Hallo,\n\nder Drucker im 2. Stock funktioniert wieder. Es war ein Papierstau in der Einzugseinheit. Melde dich, falls das Problem erneut auftritt.\n\nIT-Support", ("f", "IT-Support", "it-support@" + OWN_DOMAIN)),
    ],
    "lieferung": [
        ("Ihre Bestellung {nr} ist unterwegs",
         "Guten Tag,\n\nIhre Bestellung {nr} wurde heute versendet und wird voraussichtlich am {wochentag} zugestellt. Mit der Sendungsnummer {track} können Sie das Paket verfolgen.\n\nIhr Versandteam von {company}", ("s", "versand")),
        ("Lieferverzögerung {produkt}",
         "Guten Tag,\n\nleider verzögert sich die Lieferung Ihrer {produkt} um ca. {tage} Tage, da unser Lieferant Engpässe hat. Wir entschuldigen uns für die Unannehmlichkeiten.\n\nFreundliche Grüsse\n{name}, {company}", ("p",)),
    ],
    "hr": [
        ("Ferienplanung Sommer {jahr}",
         "Liebes Team,\n\nbitte tragt eure Ferienwünsche bis Ende Monat in die gemeinsame Liste ein. Wir versuchen, alle Wünsche zu berücksichtigen, achten aber auf eine ausreichende Abdeckung.\n\nGruss\n{name}, HR", ("c",)),
        ("Einladung Teamevent",
         "Hallo zusammen,\n\ndas diesjährige Teamevent findet am {wochentag} statt. Wir treffen uns zum gemeinsamen Grillabend am See. Bitte meldet euch bis Freitag an und gebt Allergien bekannt.\n\nBis bald\n{name}", ("c",)),
    ],
    "dringend": [
        ("DRINGEND: Server nicht erreichbar",
         "Hallo,\n\nder Produktivserver antwortet seit 10 Minuten nicht mehr, der Webshop ist offline. Bitte sofort prüfen, wir verlieren gerade Umsatz!\n\n{name}", ("c",)),
        ("Wasserschaden im Lager – sofort Rückmeldung",
         "Guten Tag,\n\nheute Nacht gab es einen Wasserschaden im Lager. Ein Teil der Ware ist betroffen. Bitte kommen Sie so schnell wie möglich vorbei, die Feuerwehr ist vor Ort.\n\n{name}", ("c",)),
        ("Produktionsstopp Linie 2",
         "Achtung: Linie 2 steht seit 07:15 Uhr wegen eines Maschinenausfalls. Der Techniker ist unterwegs. Aufträge für heute müssen umgeplant werden.\n\n{name}", ("c",)),
    ],
    "vertrag": [
        ("Kündigung des Wartungsvertrags",
         "Guten Tag,\n\nhiermit kündigen wir den Wartungsvertrag Nr. {nr} fristgerecht auf das Ende der Vertragslaufzeit. Bitte bestätigen Sie uns den Eingang der Kündigung schriftlich.\n\nFreundliche Grüsse\n{name}, {company}", ("p",)),
        ("Vertragsverlängerung {jahr}",
         "Hallo,\n\nunser Rahmenvertrag läuft Ende Jahr aus. Wir würden ihn gerne zu den bisherigen Konditionen um zwei Jahre verlängern. Den Entwurf haben wir beigelegt.\n\nBeste Grüsse\n{name}, {company}", ("p",)),
    ],
    "lob": [
        ("Herzlichen Dank für die tolle Zusammenarbeit",
         "Hallo,\n\nich wollte mich kurz für die super Betreuung im letzten Projekt bedanken. Alles lief reibungslos, und das Ergebnis hat unsere Erwartungen übertroffen. Wir empfehlen Sie gerne weiter.\n\nLiebe Grüsse\n{name}, {company}", ("p",)),
        ("Feedback zum Kundendienst",
         "Guten Tag,\n\nder Kundendienst hat mein Problem innert Minuten gelöst. Sehr freundlich und kompetent. Vielen Dank an das ganze Team!\n\n{name}, {company}", ("p",)),
    ],
    "event": [
        ("Einladung zum Webinar: {thema}",
         "Guten Tag,\n\nwir laden Sie herzlich zu unserem kostenlosen Webinar «{thema}» am {wochentag} um {uhrzeit} Uhr ein. Erfahren Sie von Expertinnen und Experten, was das für Ihr Unternehmen bedeutet.\n\nIhr Team von {company}", ("s", "events")),
        ("Fachmesse {ort}: Sichern Sie sich Ihr Ticket",
         "Hallo,\n\nvom 12. bis 14. November findet in {ort} die grosse Fachmesse für Industrie und Handel statt. Als Kunde erhalten Sie ein kostenloses Ticket und Zutritt zur VIP-Lounge.\n\nBeste Grüsse\n{company}", ("s", "events")),
    ],
    "reise": [
        ("Spesenabrechnung Geschäftsreise {ort}",
         "Hallo,\n\nanbei meine Spesenabrechnung für die Geschäftsreise nach {ort}: Zugticket, Hotel und Verpflegung, total {betrag}. Bitte um Freigabe.\n\nDanke\n{name}", ("c",)),
        ("Flugbuchung bestätigt: Zürich – {ort}",
         "Ihre Buchung ist bestätigt. Hinflug am {wochentag} um {uhrzeit} Uhr ab Zürich, Rückflug am {wochentag2}. Bitte checken Sie 24 Stunden vor Abflug online ein.\n\nIhr Reiseteam von {company}", ("s", "reisen")),
    ],
}


def _ascii(text):
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue")):
        text = text.replace(a, b)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _chf(x):
    return "CHF " + f"{x:,.2f}".replace(",", "'")


def build_emails(n=N_EMAILS, seed=7):
    rng = random.Random(seed)
    cats = list(TEMPLATES)
    emails = []
    for i in range(n):
        cat = cats[i % len(cats)]                    # even mix of all categories
        subject_t, body_t, sender = rng.choice(TEMPLATES[cat])
        company = rng.choice(COMPANIES)
        first, last = rng.choice(FIRST), rng.choice(LAST)
        person = f"{first} {last}"
        if sender[0] == "p":
            name, addr = person, f"{_ascii(first).lower()}.{_ascii(last).lower()}@{company[1]}"
        elif sender[0] == "c":
            name, addr = person, f"{_ascii(first).lower()}.{_ascii(last).lower()}@{OWN_DOMAIN}"
        elif sender[0] == "s":
            name, addr = company[0], f"{sender[1]}@{company[1]}"
        else:  # fixed sender; applicants get a private address
            if sender[1]:
                name, addr = sender[1], sender[2]
            else:
                name = person
                addr = f"{_ascii(first).lower()}.{_ascii(last).lower()}@{rng.choice(['gmail.com', 'bluewin.ch', 'gmx.ch'])}"
        wd = rng.choice(WEEKDAYS[:5])
        vals = dict(
            nr=rng.randint(10000, 99999), betrag=_chf(rng.randint(120, 18500) + rng.choice([0, 0.5, 0.9])),
            tage=rng.choice([7, 10, 14, 30]), produkt=rng.choice(PRODUKTE), menge=rng.choice([20, 50, 120, 300]),
            stelle=rng.choice(STELLEN), jahre=rng.randint(2, 12), wochentag=wd,
            wochentag2=rng.choice([d for d in WEEKDAYS[:5] if d != wd]),
            uhrzeit=rng.choice(["09:00", "10:30", "14:00", "15:30"]),
            datum=f"{rng.randint(1, 28)}. {rng.choice(MONTHS[9:])}", faellig=f"{rng.randint(1, 28)}. {rng.choice(MONTHS[9:])}",
            prozent=rng.choice([15, 20, 25, 30]), projekt=rng.choice(PROJEKTE), thema=rng.choice(THEMEN),
            ort=rng.choice(ORTE), monat=rng.choice(MONTHS[7:10]), jahr=2027,
            track=f"99.{rng.randint(10, 99)}.{rng.randint(100000, 999999)}", name=name, company=company[0],
        )
        minutes_ago = rng.randint(5, 90) + int((rng.random() ** 1.8) * 25 * 24 * 60)
        ts = BASE_TIME - timedelta(minutes=minutes_ago)
        subject = subject_t.format(**vals)
        if sender[0] == "p" and "{nr}" not in subject_t and "{company}" not in subject_t and rng.random() < 0.6:
            subject += f" – {company[0]}"
        if rng.random() < 0.10 and sender[0] in ("p", "c"):
            subject = "AW: " + subject
        emails.append(dict(
            ts=ts, name=name, addr=addr, subject=subject, body=body_t.format(**vals),
            unread=rng.random() < 0.3,
        ))
    emails.sort(key=lambda m: m["ts"], reverse=True)
    out = []
    for i, m in enumerate(emails):
        teaser = " ".join(m["body"].split())
        ts = m.pop("ts")
        short = ts.strftime("%H:%M") if ts.date() == BASE_TIME.date() else f"{ts.day}. {MONTHS_SHORT[ts.month - 1]}"
        long_ = f"{WEEKDAYS[ts.weekday()]}, {ts.day}. {MONTHS[ts.month - 1]} {ts.year}, {ts.strftime('%H:%M')}"
        out.append(dict(m, id=f"m{i}", teaser=teaser[:140], short=short, long=long_))
    return out


# ----------------------------------------------------------------------------
# Browser part: custom Streamlit component (written to a temp folder at start)
# ----------------------------------------------------------------------------
COMPONENT_HTML = r'''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: transparent;
               font-family: Roboto, "Segoe UI", Arial, sans-serif; color: #202124; }
  #app { position: relative; height: 690px; display: flex; flex-direction: column;
         background: #fff; border-radius: 16px; overflow: hidden;
         box-shadow: 0 1px 3px rgba(60,64,67,.3), 0 4px 12px rgba(60,64,67,.15); }

  /* top bar */
  #top { display: flex; align-items: center; gap: 16px; padding: 12px 16px; background: #f6f8fc; }
  #brand { font-size: 19px; color: #5f6368; white-space: nowrap; }
  #brand b { color: #c5221f; }
  #q { flex: 1; min-width: 0; font-size: 16px; padding: 12px 20px; border-radius: 26px;
       border: 1px solid transparent; background: #eaf1fb; color: #202124; outline: none;
       transition: background .15s, box-shadow .15s; }
  #q::placeholder { color: #5f6368; }
  #q:focus { background: #fff; box-shadow: 0 1px 6px rgba(32,33,36,.28); }
  #bar { height: 3px; background: #f6f8fc; position: relative; overflow: hidden; }
  #bar.on::after { content: ""; position: absolute; top: 0; bottom: 0; width: 35%;
                   background: linear-gradient(90deg, transparent, #1a73e8, transparent);
                   animation: sweep 1.1s linear infinite; }
  @keyframes sweep { from { left: -35%; } to { left: 100%; } }

  /* status line */
  #status { display: flex; align-items: center; gap: 16px; padding: 8px 16px; font-size: 13px;
            color: #5f6368; border-bottom: 1px solid #e8eaed; min-height: 38px; }
  #stL { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #stR { white-space: nowrap; font-weight: 600; }
  #stR.ok { color: #188038; }
  #stR.wait { color: #b06000; animation: pulse 1s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: .35; } }
  #onlyLbl { display: flex; align-items: center; gap: 6px; cursor: pointer; user-select: none; white-space: nowrap; }

  /* list */
  #list { flex: 1; overflow-y: auto; position: relative; }
  .row { display: flex; align-items: center; gap: 16px; height: 40px; padding: 0 16px;
         border-bottom: 1px solid #edf0f3; cursor: pointer; font-size: 14px; background: #f2f6fc;
         transition: opacity .35s, background .35s, box-shadow .35s; }
  .row:hover { box-shadow: inset 1px 0 0 #dadce0, inset -1px 0 0 #dadce0, 0 1px 2px rgba(60,64,67,.3); z-index: 2; position: relative; }
  .row.unread { background: #fff; }
  .row.unread .snd, .row.unread .sub { font-weight: 700; color: #202124; }
  .snd { width: 190px; flex: none; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .mid { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sub { font-weight: 400; }
  .tz { color: #5f6368; }
  .chip { flex: none; width: 54px; text-align: center; font-size: 12px; font-weight: 700; color: #174ea6;
          background: #d2e3fc; border-radius: 10px; padding: 2px 0; visibility: hidden; }
  .tm { flex: none; width: 58px; text-align: right; font-size: 12px; color: #5f6368; }
  .row.hit { background: #e8f0fe; box-shadow: inset 4px 0 0 #1a73e8; }
  .row.hit .chip { visibility: visible; }
  .row.dim { opacity: .34; }

  /* detail view */
  #detail { position: absolute; inset: 0; z-index: 10; background: #fff; display: flex; flex-direction: column;
            opacity: 0; transform: translateX(28px); pointer-events: none;
            transition: opacity .2s, transform .2s; }
  #detail.open { opacity: 1; transform: none; pointer-events: auto; }
  #dTop { display: flex; align-items: center; gap: 12px; padding: 10px 16px; background: #f6f8fc;
          border-bottom: 1px solid #e8eaed; }
  #back { border: 0; background: #e8eaed; border-radius: 20px; padding: 8px 16px; font-size: 14px;
          cursor: pointer; color: #202124; }
  #back:hover { background: #dadce0; }
  #dScroll { flex: 1; overflow-y: auto; padding: 22px 32px 32px; }
  #dSubject { font-size: 22px; font-weight: 400; margin: 0 0 18px; line-height: 1.3; }
  #dHead { display: flex; align-items: center; gap: 14px; margin-bottom: 8px; }
  #dAv { flex: none; width: 40px; height: 40px; border-radius: 50%; color: #fff; font-size: 18px;
         display: flex; align-items: center; justify-content: center; font-weight: 500; }
  #dWho { flex: 1; min-width: 0; }
  #dName { font-weight: 700; font-size: 14px; }
  #dAddr { color: #5f6368; font-size: 13px; margin-left: 6px; }
  #dTo { color: #5f6368; font-size: 12px; margin-top: 2px; }
  #dDate { color: #5f6368; font-size: 12.5px; white-space: nowrap; }
  #dJev { display: none; margin: 12px 0 4px 54px; font-size: 12.5px; font-weight: 600; color: #174ea6;
          background: #d2e3fc; border-radius: 12px; padding: 4px 12px; width: fit-content; }
  #dBody { margin: 18px 0 0 54px; font-size: 14.5px; line-height: 1.65; white-space: pre-wrap; max-width: 760px; }
</style>
</head>
<body>
<div id="app">
  <div id="top">
    <div id="brand">&#9993; <b>Post</b>eingang</div>
    <input id="q" type="text" autocomplete="off"
           placeholder="E-Mails durchsuchen ... z. B. Reklamation, dringend, Bewerbung, Betrug">
  </div>
  <div id="bar"></div>
  <div id="status">
    <span id="stL"></span>
    <label id="onlyLbl"><input id="only" type="checkbox"> Nur Treffer anzeigen</label>
    <span id="stR"></span>
  </div>
  <div id="list"></div>

  <div id="detail">
    <div id="dTop"><button id="back">&larr; Zur&uuml;ck zum Posteingang</button></div>
    <div id="dScroll">
      <h1 id="dSubject"></h1>
      <div id="dHead">
        <div id="dAv"></div>
        <div id="dWho"><span id="dName"></span><span id="dAddr"></span><div id="dTo">an mich</div></div>
        <div id="dDate"></div>
      </div>
      <div id="dJev"></div>
      <div id="dBody"></div>
    </div>
  </div>
</div>

<script>
(function () {
  "use strict";
  function $(id) { return document.getElementById(id); }
  var input = $("q"), list = $("list"), stL = $("stL"), stR = $("stR"), bar = $("bar"),
      only = $("only"), detail = $("detail");

  var emails = [], rows = [], ready = false, query = "", lastSig = "", openIdx = -1;
  var state = { scores: {}, threshold: 0.5, latency: null, doneQuery: "" };

  // ------------------------------------------------ Streamlit protocol
  function post(type, data) {
    var msg = { isStreamlitMessage: true, type: type };
    for (var k in (data || {})) { msg[k] = data[k]; }
    window.parent.postMessage(msg, "*");
  }
  window.addEventListener("message", function (ev) {
    var d = ev.data;
    if (d && d.type === "streamlit:render") { onRender(d.args || {}); }
  });
  post("streamlit:componentReady", { apiVersion: 1 });
  post("streamlit:setFrameHeight", { height: 706 });

  function onRender(args) {
    state.scores = args.scores || {};
    state.threshold = (typeof args.threshold === "number") ? args.threshold : 0.5;
    state.latency = (typeof args.latency_ms === "number") ? args.latency_ms : null;
    state.doneQuery = args.done_query || "";
    if (!ready && args.emails && args.emails.length) { init(args.emails); }
    refresh();
  }

  // ---------------------------------------------------- typing input
  var debounce = null, lastSent = null;
  function send() {
    var q = input.value.trim();
    if (q === lastSent) { return; }
    lastSent = q;
    post("streamlit:setComponentValue", { value: { query: q, t: Date.now() }, dataType: "json" });
  }
  input.addEventListener("input", function () {
    query = input.value;
    refresh();
    clearTimeout(debounce);
    debounce = setTimeout(send, 450);
  });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { clearTimeout(debounce); send(); }
  });
  only.addEventListener("change", refresh);

  function isWaiting() {
    var q = query.trim();
    return q.length >= 2 && state.doneQuery !== q;
  }

  // ------------------------------------------------------------ rows
  function init(list_) {
    ready = true;
    emails = list_;
    emails.forEach(function (m, i) {
      var el = document.createElement("div");
      el.className = "row" + (m.unread ? " unread" : "");
      var snd = document.createElement("span"); snd.className = "snd"; snd.textContent = m.name;
      var mid = document.createElement("span"); mid.className = "mid";
      var sub = document.createElement("span"); sub.className = "sub"; sub.textContent = m.subject;
      var tz = document.createElement("span"); tz.className = "tz"; tz.textContent = "  \u2013  " + m.teaser;
      mid.appendChild(sub); mid.appendChild(tz);
      var chip = document.createElement("span"); chip.className = "chip";
      var tm = document.createElement("span"); tm.className = "tm"; tm.textContent = m.short;
      el.appendChild(snd); el.appendChild(mid); el.appendChild(chip); el.appendChild(tm);
      el.addEventListener("click", function () { openDetail(i); });
      list.appendChild(el);
      rows.push({ el: el, chip: chip, hidden: false, score: 0 });
    });
  }

  function effectiveScores() {
    if (query.trim().length < 2) { return null; }
    return Object.keys(state.scores).length ? state.scores : null;
  }

  function refresh() {
    if (!ready) { return; }
    var sc = effectiveScores(), thr = state.threshold, onlyHits = only.checked;
    var hits = [], rest = [], hitSet = {};
    emails.forEach(function (m, i) {
      var s = sc ? sc[m.id] : undefined;
      var isNum = (typeof s === "number");
      rows[i].score = isNum ? s : 0;
      if (sc && isNum && s >= thr) { hits.push(i); hitSet[i] = 1; } else { rest.push(i); }
    });
    hits.sort(function (a, b) { return rows[b].score - rows[a].score; });
    hits.forEach(function (i) { rows[i].chip.textContent = Math.round(rows[i].score * 100) + " %"; });

    updateStatus(sc, hits.length);

    var ord = hits.concat(rest);
    var sig = ord.join(",") + "|" + (sc ? 1 : 0) + "|" + onlyHits;
    if (sig === lastSig) { return; }
    lastSig = sig;
    applyOrder(ord, hitSet, sc, onlyHits);
  }

  function updateStatus(sc, nHits) {
    if (sc) {
      stL.textContent = nHits + " Treffer von " + emails.length + " E-Mails  \u00B7  Suche: \u201E" + state.doneQuery + "\u201C";
    } else {
      stL.textContent = "Posteingang  \u00B7  " + emails.length + " E-Mails";
    }
    if (isWaiting()) {
      stR.className = "wait";
      stR.textContent = "\u25CF Jev analysiert " + emails.length + " E-Mails ...";
      bar.classList.add("on");
    } else {
      bar.classList.remove("on");
      stR.className = "ok";
      stR.textContent = (state.latency !== null && query.trim().length >= 2)
        ? "\u26A1 " + Math.round(state.latency) + " ms" : "";
    }
  }

  // reorder with a FLIP animation: rows glide to their new position
  function applyOrder(ord, hitSet, sc, onlyHits) {
    var vTop = list.scrollTop - 160, vBot = list.scrollTop + list.clientHeight + 160;
    var first = [];
    rows.forEach(function (r, i) { first[i] = r.hidden ? null : r.el.offsetTop; });

    var frag = document.createDocumentFragment();
    ord.forEach(function (i) { frag.appendChild(rows[i].el); });
    list.appendChild(frag);

    ord.forEach(function (i) {
      var r = rows[i], hit = !!hitSet[i];
      var hide = !!(onlyHits && sc && !hit);
      r.hidden = hide;
      r.el.style.display = hide ? "none" : "";
      r.el.classList.toggle("hit", !!(sc && hit));
      r.el.classList.toggle("dim", !!(sc && !hit));
    });

    var n = 0;
    ord.forEach(function (i) {
      var r = rows[i];
      if (r.hidden || first[i] === null) { return; }
      var now = r.el.offsetTop, dy = first[i] - now;
      if (!dy) { return; }
      var visNow = now >= vTop && now <= vBot, visBefore = first[i] >= vTop && first[i] <= vBot;
      if (!(visNow || visBefore)) { return; }
      r.el.animate([{ transform: "translateY(" + dy + "px)" }, { transform: "none" }],
                   { duration: 560, delay: Math.min(n * 12, 300),
                     easing: "cubic-bezier(.2,.8,.2,1)", fill: "backwards" });
      n++;
    });
    if (sc) { list.scrollTo({ top: 0, behavior: "smooth" }); }
  }

  // ------------------------------------------------------------ detail
  function hueOf(s) { var h = 0; for (var i = 0; i < s.length; i++) { h = (h * 31 + s.charCodeAt(i)) % 360; } return h; }

  function openDetail(i) {
    var m = emails[i];
    openIdx = i;
    rows[i].el.classList.remove("unread");
    $("dSubject").textContent = m.subject;
    $("dName").textContent = m.name;
    $("dAddr").textContent = "<" + m.addr + ">";
    $("dDate").textContent = m.long;
    $("dBody").textContent = m.body;
    var av = $("dAv");
    av.textContent = m.name.charAt(0).toUpperCase();
    av.style.background = "hsl(" + hueOf(m.name) + ",55%,45%)";
    var jev = $("dJev"), sc = effectiveScores();
    if (sc && typeof sc[m.id] === "number" && sc[m.id] >= state.threshold) {
      jev.style.display = "block";
      jev.textContent = "Jev: " + Math.round(sc[m.id] * 100) + " % passend zu \u201E" + state.doneQuery + "\u201C";
    } else {
      jev.style.display = "none";
    }
    $("dScroll").scrollTop = 0;
    detail.classList.add("open");
  }
  function closeDetail() { detail.classList.remove("open"); openIdx = -1; }
  $("back").addEventListener("click", closeDetail);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") { closeDetail(); } });

  window.__mail = { rows: function () { return rows; }, emails: function () { return emails; },
                    openIdx: function () { return openIdx; } };
})();
</script>
</body>
</html>
'''

_COMPONENT_DIR = Path(tempfile.gettempdir()) / "mailsense_component"
_COMPONENT_DIR.mkdir(parents=True, exist_ok=True)
_index = _COMPONENT_DIR / "index.html"
if not _index.exists() or _index.read_text(encoding="utf-8") != COMPONENT_HTML:
    _index.write_text(COMPONENT_HTML, encoding="utf-8")

inbox_component = components.declare_component("mailsense_inbox", path=str(_COMPONENT_DIR))


# ----------------------------------------------------------------------------
# API calls
# ----------------------------------------------------------------------------
def _ask_chunk(query, chunk):
    """One API request for one package of e-mails. Returns (scores, raw, error)."""
    questions = {
        m["id"]: {
            "type": "noul",
            "instructions": (
                f"E-Mail\nVon: {m['name']}\nBetreff: {m['subject']}\nText: {m['body'][:BODY_CHARS_FOR_API]}\n\n"
                f"Ist diese E-Mail inhaltlich relevant für den Suchbegriff? "
                f"Bewerte die Bedeutung, nicht Wortüberschneidungen."
            ),
            "criteria": {
                "true": "Die E-Mail gehört klar zum Suchbegriff oder handelt eindeutig davon.",
                "false": "Die E-Mail hat keinen erkennbaren Bezug zum Suchbegriff.",
            },
        }
        for m in chunk
    }
    payload = {"state": f"Suchbegriff: '{query}'", "model": MODEL, "questions": questions}
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}
    try:
        response = requests.post(API_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_S)
    except requests.exceptions.RequestException as exc:
        return None, None, f"Request failed before a response was received: {exc!r}"
    if response.status_code != 200:
        return None, None, f"HTTP {response.status_code}\n\n{response.text}"
    try:
        raw = response.json()
        scores = {m["id"]: float(raw["answers"][m["id"]]["noul"]) for m in chunk}
    except (ValueError, KeyError, TypeError) as exc:
        return None, None, f"HTTP 200, but the response could not be parsed ({exc!r}).\n\nRaw body:\n\n{response.text}"
    return scores, raw, None


def call_jev(query, emails):
    """
    Sends all e-mails to Jev in parallel packages of CHUNK_SIZE.
    Returns (scores, latency_ms, info, error_text). Latency = wall-clock time for ALL packages.
    """
    chunks = [emails[i:i + CHUNK_SIZE] for i in range(0, len(emails), CHUNK_SIZE)]
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        results = list(pool.map(lambda c: _ask_chunk(query, c), chunks))
    latency_ms = (time.perf_counter() - start) * 1000

    for i, (_, _, err) in enumerate(results):
        if err:
            return None, latency_ms, None, f"Paket {i + 1} von {len(chunks)} fehlgeschlagen:\n\n{err}"

    scores, tokens_in, tokens_out, model = {}, 0, 0, "?"
    for s, raw, _ in results:
        scores.update(s)
        usage = raw.get("usage", {})
        tokens_in += usage.get("input_tokens", 0) or 0
        tokens_out += usage.get("output_tokens", 0) or 0
        model = raw.get("model", model)
    info = {"model": model, "requests": len(chunks), "tokens_in": tokens_in, "tokens_out": tokens_out}
    return scores, latency_ms, info, None


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
st.set_page_config(page_title="MailSense AI", page_icon="📬", layout="wide")
st.title("MailSense AI - Powered by Jev")

ss = st.session_state

# Optional password gate (protects your API credit when the link is shared)
if APP_PASSWORD and not ss.get("authed"):
    pw = st.text_input("Passwort", type="password")
    if pw and pw == str(APP_PASSWORD):
        ss.authed = True
        st.rerun()
    if pw:
        st.error("Falsches Passwort.")
    st.stop()

if "emails" not in ss:
    ss.emails = build_emails()
    ss.scores = {}
    ss.latency_ms = None
    ss.info = None
    ss.error = None
    ss.last_query = None
    ss.done_query = ""

threshold = st.sidebar.slider(
    "Schwellenwert (Treffer ab Wahrscheinlichkeit)", 0.05, 0.95, 0.50, 0.05
)

# Containers reserve the layout order; they are filled further below.
metric_box = st.container()
error_box = st.container()

# The component holds the search field AND the inbox.
result = inbox_component(
    emails=ss.emails,
    scores=ss.scores,
    threshold=threshold,
    latency_ms=ss.latency_ms,
    done_query=ss.done_query,
    key="inbox",
    default=None,
)

# New search term from the browser? -> (maybe) call the API, then rerun.
query = ((result or {}).get("query") or "").strip()
if result is not None and query != ss.last_query:
    if len(query) < MIN_QUERY_CHARS:
        ss.scores, ss.error = {}, None
    elif API_KEY == "YOUR_API_KEY_HERE":
        ss.scores = {}
        ss.error = (
            "No API key configured. Set the environment variable JEV_API_KEY "
            "or edit API_KEY in app.py."
        )
    else:
        scores, latency, info, error = call_jev(query, ss.emails)
        ss.scores = scores or {}
        ss.latency_ms = latency
        ss.info = info
        ss.error = error
    ss.done_query = query
    ss.last_query = query
    st.rerun()

# Metrics / errors
n_hits = sum(1 for m in ss.emails if ss.scores.get(m["id"], 0.0) >= threshold)
with metric_box:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("JEV Latenz (ms)", f"{ss.latency_ms:,.0f}" if ss.latency_ms is not None else "—")
    c2.metric("Treffer", n_hits)
    c3.metric("E-Mails gesamt", len(ss.emails))
    c4.metric("API-Anfragen (parallel)", ss.info["requests"] if ss.info else "—")
    if ss.info:
        st.caption(
            f"Modell laut Server: {ss.info['model']} · "
            f"Tokens ein/aus: {ss.info['tokens_in']}/{ss.info['tokens_out']}"
        )
with error_box:
    if ss.error:
        st.error(ss.error)

if ss.scores:
    with st.expander("Debug: Wahrscheinlichkeit pro E-Mail (von Jev, Top 30)"):
        by_id = {m["id"]: m for m in ss.emails}
        top = sorted(ss.scores.items(), key=lambda kv: -kv[1])[:30]
        st.table(
            [
                {"P(passt)": round(s, 3), "Von": by_id[i]["name"], "Betreff": by_id[i]["subject"]}
                for i, s in top
            ]
        )
