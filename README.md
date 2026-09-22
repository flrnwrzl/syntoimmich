# Synology Photos → Immich Migration Tool

Ein eigenständiges, browserbasiertes Migrationswerkzeug, um Fotos und Alben
von **Synology Photos** nach [**Immich**](https://immich.app) zu übertragen —
inklusive Mehrbenutzer-Unterstützung, korrekter Zuordnung von Beitragenden bei
geteilten Alben, Freigabe-Alben (Passphrase-Links) und sicherem
Wiederaufnehmen unterbrochener Läufe.

Keine Installation von Zusatzsoftware auf dem NAS nötig — das Tool läuft als
einzelnes Python-Skript (oder als vorgefertigte `.exe`) auf deinem eigenen
Rechner und spricht beide APIs direkt an.

<img width="1360" height="900" alt="01-setup" src="https://github.com/user-attachments/assets/dea7d2cc-9fac-4642-a12d-4c9c9a4cda9b" />


\---

## Inhalt

* [Features](#features)
* [Screenshots](#screenshots)
* [Voraussetzungen](#voraussetzungen)
* [Installation](#installation)
* [Schnellstart](#schnellstart)
* [Wie die Migration funktioniert](#wie-die-migration-funktioniert)
* [Wiederaufnehmen \& erneutes Ausführen](#wiederaufnehmen--erneutes-ausführen)
* [Häufige Fehler](#häufige-fehler)
* [Windows-.exe selbst bauen](#windows-exe-selbst-bauen)
* [Bekannte Einschränkungen](#bekannte-einschränkungen)
* [Mitwirken](#mitwirken)
* [Lizenz](#lizenz)

\---

## Features

* 🔀 **Mehrbenutzer-fähig** — jeder Synology-Nutzer meldet sich mit den
eigenen Zugangsdaten an (Synology Personal Space ist selbst für Admins nur
für den jeweiligen Besitzer einsehbar), jeder bekommt seinen eigenen
Immich-API-Key.
* 📸 **Korrekte Zuordnung bei geteilten Alben** — jedes Foto in einem Album
wird dem Immich-Account des *tatsächlichen* Beitragenden zugeordnet, nicht
pauschal dem Album-Ersteller.
* 🔗 **Freigabe-Alben (Passphrase-Links)** werden ebenso unterstützt wie
normale und geteilte Alben.
* ✅ **Bereits migrierte Fotos werden live erkannt** — per SHA-1-Checksumme
gegen Immich abgeglichen, keine doppelten Uploads bei erneuten Läufen.
* 🔍 **„Bereits in Immich"-Anzeige pro Album** in der Auswahl — vollständig
migrierte Alben werden ausgegraut und automatisch abgewählt, teilweise
migrierte zeigen den Fortschritt (z. B. „50/100 Fotos"). Der Abgleich läuft
live gegen Immich, nicht gegen eine lokale Datei — funktioniert also auch,
wenn die Konfigurationsdatei gelöscht wurde.
* 🙈 **Alben manuell ausschließen** — für Übersicht bei sehr großen
Bibliotheken lassen sich einzelne Alben dauerhaft ausblenden.
* ⏸️ **Pause/Fortsetzen** und sicheres Wiederaufnehmen nach Abbruch, ohne
Duplikate oder doppelt angelegte Alben zu erzeugen.
* 🧪 **Dry-Run-Modus** zum risikofreien Testen, bevor tatsächlich etwas
hochgeladen wird.
* 📊 **Live-Fortschritt** (Fotos *und* Alben) mit Aktivitäts-Log,
Report-Export.
* 🖥️ Läuft als reines Python-Skript **oder** als eigenständige
Windows-`.exe` — keine Installation auf dem NAS oder Immich-Server nötig.

## Screenshots

|Setup|Optionen|
<img width="1360" height="900" alt="02-optionen" src="https://github.com/user-attachments/assets/d8df45ab-2b20-4957-b4c3-e193e35751b8" />


|Album-Auswahl|Migration|
<img width="1360" height="900" alt="03-album-auswahl" src="https://github.com/user-attachments/assets/aed84b39-e26d-49b4-b4d0-cd9a7c76004a" />
<img width="1360" height="900" alt="04-migration" src="https://github.com/user-attachments/assets/f502f2f1-79e7-4f57-a734-097f317b234f" />



*(Die Screenshots zeigen die Oberfläche mit Beispieldaten, nicht echte
Zugangsdaten.)*

## Voraussetzungen

* Python **3.9+** (getestet mit 3.12) — oder einfach die vorgefertigte
Windows-`.exe` verwenden, dann wird keine Python-Installation benötigt.
* Ein laufender [Immich](https://immich.app)-Server mit API-Zugriff.
* Synology DSM mit **Synology Photos**. Jeder zu migrierende Benutzer muss
Mitglied der `administrators`-Gruppe sein (für den API-Zugriff auf geteilte
Alben) und sein eigenes Synology-Passwort bereithalten.
* Für jeden zu migrierenden Benutzer ein eigener **Immich-API-Key**
(Immich → Account-Einstellungen → API-Keys).

Die einzige externe Python-Abhängigkeit ist [`requests`](https://pypi.org/project/requests/)
— das Skript installiert sie beim ersten Start automatisch, falls sie fehlt.

## Installation

### Option A — Python-Skript (alle Plattformen)

```bash
git clone https://github.com/flrnwrzl/syntoimmich.git
cd <dein-repo>
python synology\_to\_immich.py
```

Ein Browser-Fenster öffnet sich automatisch unter `http://localhost:8765`.

Ein anderer Port lässt sich per Argument setzen:

```bash
python synology\_to\_immich.py --port 8080
```

### Option B — Windows: `Start\_Migration.bat`

Einfach `Start\_Migration.bat` doppelklicken — startet das Skript mit Python,
falls installiert.

### Option C — Windows: eigenständige `.exe`

Siehe [Windows-.exe selbst bauen](#windows-exe-selbst-bauen). Es wird kein
Python auf dem Zielrechner benötigt.

## Schnellstart

1. **Setup**: Synology- und Immich-URL eintragen, Admin-Zugangsdaten für die
UID-Auflösung, danach für jeden zu migrierenden Benutzer eine Zeile mit
Synology-Login und Immich-API-Key hinzufügen. Mit „Verbindung testen"
prüfen.
2. **Optionen**: Festlegen, was migriert werden soll (persönliche Fotos,
Alben, Team Space) und ob zunächst nur ein risikofreier **Dry Run**
laufen soll.
3. **Album-Auswahl**: Scannen lassen und die gewünschten Alben auswählen.
Bereits vollständig migrierte Alben sind automatisch abgewählt und grau
dargestellt; mit dem ✕-Button lassen sich einzelne Alben dauerhaft
ausblenden.
4. **Migration**: Start klicken und den Fortschritt live verfolgen. Pause und
Fortsetzen sind jederzeit möglich.

## Wie die Migration funktioniert

Das Tool arbeitet in bis zu drei Phasen (abhängig von den gewählten
Optionen):

1. **Persönliche Fotos** — pro Benutzer, wird übersprungen sobald gezielt
Alben ausgewählt wurden.
2. **Alben** (normal, geteilt, Freigabe-Link) — Kernstück der Migration.
Jedes Album wird unter dem Immich-Account des „Album-Besitzers" angelegt
(bzw. wiederverwendet, siehe unten) und mit allen Beitragenden geteilt;
jedes Foto wird über die Session seines *tatsächlichen* Besitzers
heruntergeladen und unter dem Account des *Beitragenden* hochgeladen.
3. **Team/Shared Space** (optional) — physischer Ordner, per Admin-Zugriff.

Duplikat-Erkennung läuft über den SHA-1-Hash der Datei gegen Immichs
`bulk-upload-check`-Endpunkt — ein Foto wird nie zweimal tatsächlich
hochgeladen, auch nicht bei wiederholten Läufen.

## Wiederaufnehmen \& erneutes Ausführen

* Ein Album wird nur dann dauerhaft als „fertig" gespeichert, wenn **alle**
seine Fotos erfolgreich hochgeladen **und** dem Album hinzugefügt wurden.
Ist auch nur eines fehlgeschlagen, wird das Album beim nächsten Lauf
automatisch erneut geprüft.
* Bei einem erneuten Lauf wird geprüft, ob in Immich bereits ein Album mit
demselben Namen existiert — falls ja, wird dieses **ergänzt**, nicht
dupliziert.
* Über „↺ Fortschritt zurücksetzen" auf der Migrationsseite lässt sich der
gespeicherte Fortschritt (`migration\_state.json`) jederzeit löschen, um
alle ausgewählten Alben komplett neu zu prüfen — bereits vorhandene Fotos
werden dabei einfach als Duplikate erkannt und nicht erneut hochgeladen.

## Häufige Fehler

<details>
<summary><code>Download fehlgeschlagen item=…: code=117</code></summary>

Synology-Fehlercode **117** bedeutet: *„Diese Datei liegt nicht im Space
dieser Session."* Synology trennt strikt zwischen Personal Space und
Team/Shared Space, und Dateien lassen sich nur mit der Synology-Session des
tatsächlichen Besitzers herunterladen. Das Tool probiert dafür automatisch
alle konfigurierten Benutzer-Logins durch — dieser Fehler bedeutet, dass
**keiner** der hinterlegten Logins Zugriff auf die betreffende Datei hat.

**Lösung:** Prüfen, welchem Synology-Benutzer die Datei/das Album tatsächlich
gehört, und sicherstellen, dass genau dieser Benutzer mit korrektem Passwort
in der User-Tabelle im Setup eingetragen ist.

</details>

<details>
<summary>Zwei Benutzer sehen dasselbe geteilte Album doppelt</summary>

Das ist erwartetes Verhalten für *normale geteilte Alben* (nicht
Freigabe-Links) — jeder Benutzer mit Zugriff „findet" das Album beim Scannen
über seine eigene Session, das Tool dedupliziert es aber intern über die
Album-ID, sodass es nur einmal migriert wird.

</details>

<details>
<summary>User müssen auf Synology Administratoren Rechte haben</summary>

Um die Fotos beim Upload wieder richtig zuzuordnen müssen die User in der Synology-Nas als Administrator freigegeben werden.

</details>


## Windows-`.exe` selbst bauen

`Build\_EXE.bat` nutzt [PyInstaller](https://pyinstaller.org/), um eine
eigenständige `.exe` zu erzeugen, die kein separat installiertes Python
benötigt:

```
Build\_EXE.bat
```

Die fertige `.exe` liegt danach im Ordner `dist/`.

## Bekannte Einschränkungen

* Der Namensabgleich für „Bereits in Immich" und die Wiederverwendung
bestehender Alben erfolgt über den **Albumnamen** — zwei unterschiedliche
Synology-Alben mit identischem Namen unter demselben Immich-Account können
daher (in seltenen Fällen) fälschlich zusammengeführt werden.
* Das Tool läuft lokal auf `127.0.0.1` und ist nicht für den Betrieb im
öffentlichen Netz vorgesehen.
* Getestet gegen aktuelle DSM- und Immich-API-Versionen; ältere DSM-Versionen
können abweichende API-Antworten liefern.

## Mitwirken

Issues und Pull Requests sind willkommen. Bitte beim Melden eines Bugs die
Ausgabe aus der Live-Aktivität-Box (bzw. den exportierten Report) beilegen —
das beschleunigt die Fehlersuche erheblich.

if you want to support me:

<img width="400" height="400" alt="image" src="https://github.com/user-attachments/assets/42266785-46b3-4c78-a4a1-cb92e7942801" />

https://buymeacoffee.com/florianwuel

## Lizenz

[MIT](LICENSE)

