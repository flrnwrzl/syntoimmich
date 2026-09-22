# Changelog

## [Unreleased]

### Fixed
- **Kritisch: Fehlgeschlagene Fotos wurden nie erneut versucht.** Ein Album wurde
  bisher *immer* als „fertig" markiert (`done_albums`), selbst wenn einzelne
  Fotos beim Hochladen fehlschlugen. Beim nächsten Lauf wurde das Album dadurch
  komplett übersprungen — der fehlende Rest wurde nie migriert. Ein Album gilt
  jetzt nur noch als fertig, wenn wirklich **alle** Fotos erfolgreich
  hochgeladen und dem Album hinzugefügt wurden.
- **Kritisch: Mögliche doppelte Alben bei erneutem Lauf.** `create_album()` hat
  bisher immer ein neues, leeres Album in Immich erstellt — ganz ohne zu
  prüfen, ob eines mit diesem Namen (z. B. aus einem vorherigen,
  unvollständigen Lauf) bereits existiert. Neue Funktion
  `find_or_create_album()` sucht zuerst nach einem bestehenden Album und
  **ergänzt** dieses, statt ein Duplikat anzulegen.
- **UI komplett unbedienbar nach Update.** Der letzte Umbau hatte an sechs
  Stellen Optional-Chaining-Syntax (`?.`) eingeführt. Auf älteren/nicht
  aktualisierten Browsern führt das zu einem globalen `SyntaxError` beim Laden
  des `<script>`-Blocks — dadurch wurde **keine einzige** JS-Funktion
  definiert und jeder Klick lief ins Leere. Durch abwärtskompatible
  Äquivalente ersetzt.
- **Fortschrittsbalken zeigte nie etwas an.** `migration_state["total"]` und
  `["progress"]` wurden nur in Phase 1 (persönliche Fotos) und Phase 3 (Team
  Space) hochgezählt — nie in Phase 2 (Alben), dem in der Praxis am
  häufigsten genutzten Pfad (gezielte Album-Auswahl). Jetzt wird der
  Fortschritt in allen drei Phasen korrekt erfasst.
- **Logs-Seite zeigte nichts an.** Kein Rendering-Bug, sondern Datenverlust:
  Die Log-Liste existierte nur im Speicher des Browser-Tabs und wurde beim
  Neuladen der Seite nie neu vom Server geholt. Die dedizierte Logs-Seite
  wurde daraufhin komplett entfernt (siehe „Removed"); die funktionierende
  Live-Aktivität-Box ist jetzt fester Bestandteil der Migrationsseite.

### Added
- **„Bereits in Immich"-Anzeige pro Album.** Jedes Album wird live gegen
  Immich abgeglichen (per Namensvergleich, unabhängig von
  `migration_config.json` — funktioniert also auch nach deren Löschung).
  Vollständig migrierte Alben werden ausgegraut/durchgestrichen und
  standardmäßig abgewählt; teilweise migrierte zeigen „50/100 Fotos".
- **Gesamtfortschritts-Übersicht** auf der Album-Auswahl-Seite („X/Y bereits
  in Immich") sowie in der Auswahlzeile für die aktuell markierten Alben.
- **Album-Fortschritt auf der Migrationsseite** („3/12 Alben" statt nur „3").
- **Manuelles Ausschließen von Alben.** Neuer ✕-Button pro Album, um es dauerhaft
  aus der Liste auszublenden (grau/durchgestrichen, per Klick rückgängig zu
  machen) — nützlich zur Übersicht bei sehr großen Migrationen. Wird im
  Browser (`localStorage`) gespeichert, unabhängig von der
  Server-Konfiguration.
- Komplett überarbeitete, übersichtlichere Migrationsseite: Status, aktuelle
  Aktion und Fortschrittsbalken sind jetzt in einer Karte zusammengefasst
  statt lose auf der Seite verteilt.

### Removed
- **immich-go-Integration** entfernt (Status-Anzeige im Setup, Backend-Check,
  zugehörige JS-Referenzen) — wurde nirgends tatsächlich für den eigentlichen
  Upload genutzt und war nur eine optionale, ungenutzte Zusatzanzeige.
- **Dedizierte Logs-Seite** entfernt (siehe „Fixed"). Der Fehler-Zähler sitzt
  jetzt als Badge direkt am Nav-Punkt „Migration".

## [3.0] — Multi-User, Shared-Album-Aware
Erste Version mit Unterstützung für mehrere Synology-/Immich-Konten,
korrekter Zuordnung von Beitragenden bei geteilten Alben sowie
Freigabe-Alben (Passphrase-Links).
