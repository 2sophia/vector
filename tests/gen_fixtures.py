"""
Genera un file di esempio per ogni formato supportato, in tests/fixtures/.

Riproducibile: i sorgenti testuali (html/csv/md/adoc/txt/eml/fodp) sono scritti
direttamente; i formati binari (Office 97-2003, ODF, RTF, PDF, immagini) sono
derivati con LibreOffice headless e ImageMagick. Tutti i sample condividono lo
stesso contenuto (keyword + IBAN + una tabella) così la verifica è confrontabile.

Uso:  .venv/bin/python tests/gen_fixtures.py
Richiede: soffice (LibreOffice) e convert (ImageMagick) nel PATH.

NB: .msg (Outlook) non è generabile in modo affidabile senza Outlook/MAPI:
va messo a mano un sample reale in tests/fixtures/sample.msg (vedi README test).
"""

import os
import shutil
import subprocess

FIX = os.path.join(os.path.dirname(__file__), "fixtures")

# Contenuto condiviso ---------------------------------------------------------
TITLE = "Documento di prova Sophia Vector"
KEYWORDS = ("obblighi di trasparenza verso il cliente, "
            "IBAN IT60X0542811101000000123456, codice fiscale RSSMRA80A01H501U")

HTML = f"""<!doctype html><html><head><meta charset="utf-8"><title>{TITLE}</title></head>
<body>
<h1>{TITLE}</h1>
<p>Questo documento verifica la pipeline di ingestion. Parole chiave: {KEYWORDS}.</p>
<h2>Tabella anagrafica</h2>
<table border="1">
<tr><th>Nome</th><th>IBAN</th><th>Filiale</th></tr>
<tr><td>Mario Rossi</td><td>IT60X0542811101000000123456</td><td>Torino</td></tr>
<tr><td>Luca Bianchi</td><td>IT60X0542811101000000999888</td><td>Milano</td></tr>
</table>
</body></html>"""

CSV = ("nome,iban,filiale\n"
       "Mario Rossi,IT60X0542811101000000123456,Torino\n"
       "Luca Bianchi,IT60X0542811101000000999888,Milano\n")

MD = f"""# {TITLE}

Parole chiave: **obblighi di trasparenza**, {KEYWORDS}.

| Nome | IBAN | Filiale |
|------|------|---------|
| Mario Rossi | IT60X0542811101000000123456 | Torino |
| Luca Bianchi | IT60X0542811101000000999888 | Milano |
"""

ADOC = f"""= {TITLE}

Parole chiave: obblighi di trasparenza, {KEYWORDS}.

|===
| Nome | IBAN | Filiale
| Mario Rossi | IT60X0542811101000000123456 | Torino
|===
"""

TXT = f"{TITLE}\n\nParole chiave: {KEYWORDS}.\n"

TEX = r"""\documentclass{article}
\begin{document}
\section{Documento di prova Sophia Vector}
Parole chiave: obblighi di trasparenza verso il cliente,
IBAN IT60X0542811101000000123456, codice fiscale RSSMRA80A01H501U.
\end{document}
"""

VTT = """WEBVTT

00:00:00.000 --> 00:00:05.000
Comunicazione sugli obblighi di trasparenza verso il cliente.

00:00:05.000 --> 00:00:10.000
IBAN IT60X0542811101000000123456, filiale di Torino.
"""

# Paragrafo per le immagini: testo "da documento" (più righe) così con OCR
# acceso l'hybrid chunker produce chunk veri (un solo titolo verrebbe scartato).
IMG_PARAGRAPH = (
    "Comunicazione sugli obblighi di trasparenza verso il cliente. "
    "Il presente documento illustra le condizioni economiche del conto corrente. "
    "IBAN IT60X0542811101000000123456. "
    "Per informazioni rivolgersi alla filiale di Torino."
)

# Flat ODF Presentation (XML testuale) → soffice ne deriva pptx/ppt/odp.
FODP = f"""<?xml version="1.0" encoding="UTF-8"?>
<office:document
  xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
  xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
  xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
  xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
  xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"
  office:version="1.2"
  office:mimetype="application/vnd.oasis.opendocument.presentation">
 <office:body><office:presentation>
  <draw:page draw:name="Slide1">
   <draw:frame svg:width="22cm" svg:height="3cm" svg:x="2cm" svg:y="2cm">
    <draw:text-box><text:p>{TITLE}</text:p></draw:text-box>
   </draw:frame>
   <draw:frame svg:width="22cm" svg:height="6cm" svg:x="2cm" svg:y="6cm">
    <draw:text-box>
     <text:p>Obblighi di trasparenza verso il cliente.</text:p>
     <text:p>IBAN IT60X0542811101000000123456.</text:p>
    </draw:text-box>
   </draw:frame>
  </draw:page>
 </office:presentation></office:body>
</office:document>"""


def _soffice(src: str, target: str) -> None:
    profile = os.path.join(FIX, ".loprofile")
    subprocess.run(
        ["soffice", "--headless", "--norestore", "--nolockcheck",
         f"-env:UserInstallation=file://{profile}",
         "--convert-to", target, "--outdir", FIX, src],
        check=True, capture_output=True, text=True, timeout=180,
    )


def _convert(src: str, dst: str) -> None:
    subprocess.run(["convert", src, dst], check=True, capture_output=True, text=True, timeout=60)


def main():
    os.makedirs(FIX, exist_ok=True)
    made, failed = [], []

    def write(name, content):
        with open(os.path.join(FIX, name), "w", encoding="utf-8") as f:
            f.write(content)
        made.append(name)

    # 1) Sorgenti testuali (alcuni sono già formati nativi)
    write("sample.html", HTML)
    write("sample.csv", CSV)
    write("sample.md", MD)
    write("sample.adoc", ADOC)
    write("sample.txt", TXT)
    write("sample.tex", TEX)
    write("sample.vtt", VTT)
    # .fodp è solo l'intermediario per generare le presentazioni: non è un
    # formato dichiarato, viene scritto qui e rimosso a fine generazione.
    with open(os.path.join(FIX, "sample.fodp"), "w", encoding="utf-8") as f:
        f.write(FODP)

    # .eml via stdlib (multipart plain+html)
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = "Mario Rossi <mario@example.com>"
    msg["To"] = "ufficio@vivibanca.it"
    msg["Subject"] = "Richiesta apertura conto"
    msg["Date"] = "Mon, 25 May 2026 10:00:00 +0200"
    msg.set_content(f"Buongiorno, parole chiave: {KEYWORDS}.")
    msg.add_alternative(HTML, subtype="html")
    with open(os.path.join(FIX, "sample.eml"), "wb") as f:
        f.write(bytes(msg))
    made.append("sample.eml")

    # 2) Derivati LibreOffice
    soffice_jobs = [
        ("sample.html", "docx"), ("sample.html", "doc"), ("sample.html", "odt"),
        ("sample.html", "rtf"), ("sample.html", "pdf"),
        ("sample.csv", "xlsx"), ("sample.csv", "xls"), ("sample.csv", "ods"),
        ("sample.fodp", "pptx"), ("sample.fodp", "ppt"), ("sample.fodp", "odp"),
    ]
    for src, target in soffice_jobs:
        try:
            _soffice(os.path.join(FIX, src), target)
            made.append(f"sample.{target}")
        except Exception as e:
            failed.append(f"sample.{target}: {str(e)[:120]}")

    # 3) Immagini (ImageMagick): png con un paragrafo (caption auto-wrappa),
    # poi derivati negli altri formati immagine. Il paragrafo serve perché con
    # OCR acceso il chunker scarta un'immagine col solo titolo.
    png = os.path.join(FIX, "sample.png")
    try:
        subprocess.run(
            ["convert", "-size", "760x", "-background", "white", "-fill", "black",
             "-pointsize", "24", f"caption:{IMG_PARAGRAPH}",
             "-bordercolor", "white", "-border", "20", png],
            check=True, capture_output=True, text=True, timeout=60,
        )
        made.append("sample.png")
        for ext in ("jpg", "tiff", "bmp", "gif", "webp"):
            try:
                _convert(png, os.path.join(FIX, f"sample.{ext}"))
                made.append(f"sample.{ext}")
            except Exception as e:
                failed.append(f"sample.{ext}: {str(e)[:120]}")
    except Exception as e:
        failed.append(f"sample.png: {str(e)[:120]}")

    # 4) Audio/Video (ffmpeg): clip brevi di test. NB: sono TONI (nessun parlato)
    # → la trascrizione dà un VTT vuoto; servono per provare estrazione + routing
    # ASR, non la qualità di trascrizione (whisper è tecnologia provata).
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=2", "-ar", "16000", "-ac", "1",
             os.path.join(FIX, "sample.wav"), "-y"],
            check=True, capture_output=True, text=True, timeout=60,
        )
        made.append("sample.wav")
    except Exception as e:
        failed.append(f"sample.wav: {str(e)[:120]}")
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-ar", "16000", "-ac", "1", "-shortest",
             os.path.join(FIX, "sample.mp4"), "-y"],
            check=True, capture_output=True, text=True, timeout=60,
        )
        made.append("sample.mp4")
    except Exception as e:
        failed.append(f"sample.mp4: {str(e)[:120]}")

    # pulizia: profilo LibreOffice + intermediario .fodp
    shutil.rmtree(os.path.join(FIX, ".loprofile"), ignore_errors=True)
    fodp = os.path.join(FIX, "sample.fodp")
    if os.path.exists(fodp):
        os.remove(fodp)

    print(f"\n✅ generati {len(made)}: {', '.join(sorted(made))}")
    if failed:
        print(f"\n⚠️  falliti {len(failed)}:")
        for f in failed:
            print(f"   - {f}")
    print(f"\nFixtures in: {FIX}")


if __name__ == "__main__":
    main()
