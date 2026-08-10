# music-sync

Spiegelt Deezer-Playlisten automatisch auf Spotify — als Container, deploybar
über die Git-Anbindung von Portainer.

**Deezer ist Master.** Bei jedem Lauf wird die Spotify-Playliste exakt an die
Deezer-Playliste angeglichen: gleiche Titel, gleiche Reihenfolge, Löschungen
inklusive. Änderungen, die du auf der Spotify-Seite machst, werden beim
nächsten Lauf überschrieben — das ist so gewollt.

> **Achtung:** Richte den Sync nie auf eine Spotify-Playliste, die du selbst
> pflegst. Deren Inhalt wird ersetzt. Lass das Tool im Zweifel eine eigene
> Playliste anlegen (Standardverhalten).

Dieses Repository ist öffentlich. Es enthält **keine** Zugangsdaten — alle
Keys und Tokens kommen ausschließlich aus Environment-Variablen.

---

## Inhalt

1. [Wie abgeglichen wird](#wie-abgeglichen-wird)
2. [Schritt 1 – Apps registrieren](#schritt-1--apps-registrieren)
3. [Schritt 2 – Tokens erzeugen](#schritt-2--tokens-erzeugen)
4. [Schritt 3 – Playlist-IDs heraussuchen](#schritt-3--playlist-ids-heraussuchen)
5. [Schritt 4 – Stack in Portainer anlegen](#schritt-4--stack-in-portainer-anlegen)
6. [Schritt 5 – Trockenlauf, dann scharf schalten](#schritt-5--trockenlauf-dann-scharf-schalten)
7. [Environment-Variablen](#environment-variablen)
8. [Betrieb](#betrieb)
9. [Fehlersuche](#fehlersuche)
10. [Grenzen](#grenzen)
11. [Entwicklung](#entwicklung)

---

## Wie abgeglichen wird

Pro Lauf und Playliste:

1. Deezer-Playliste komplett einlesen (inkl. Paginierung).
2. Jeden Titel auf einen Spotify-Track abbilden:
   - **Über ISRC** — die eindeutige Aufnahme-Kennung. Das ist der Normalfall
     und praktisch immer korrekt.
   - **Sonst über Suche + Bewertung** aus Titel-, Interpreten- und
     Längenähnlichkeit. Kosmetische Zusätze wie `(2011 Remaster)` oder
     `(Album Version)` werden ignoriert; `(Live)`, `(Remix)` oder `(Acoustic)`
     dagegen bewusst nicht — das sind andere Aufnahmen. Karaoke- und
     Tribute-Versionen werden ausgeschlossen, und ein nicht passender
     Interpret disqualifiziert einen Treffer, selbst wenn Titel und Länge
     exakt stimmen.
3. Ergebnis in einem SQLite-Cache auf dem Volume ablegen. Der stündliche Lauf
   kostet danach fast nichts mehr, weil nur neue Titel aufgelöst werden.
4. Spotify-Playliste auf genau diese Trackliste setzen.

Titel ohne Spotify-Entsprechung werden übersprungen und in
`/data/unmatched.log` protokolliert.

### Eingebaute Sicherungen

Ein Spiegel, der Löschungen überträgt, kann bei schlechten Daten Schaden
anrichten. Deshalb bricht der Lauf für die betroffene Playliste ab, wenn:

- Deezer weniger Titel liefert als die Playliste laut eigener Angabe hat
  (abgebrochene Seite → sähe sonst aus wie „alles gelöscht“;
  `ALLOW_PARTIAL_SOURCE` hebt das auf),
- **kein einziger** Titel zugeordnet werden konnte, die Spotify-Playliste aber
  gefüllt ist (`ALLOW_EMPTY_MIRROR` hebt das auf),
- der Anteil nicht zuordenbarer Titel über `MAX_UNMATCHED_RATIO` liegt.

Fehler in einer Playliste stoppen die anderen nicht.

---

## Schritt 1 – Apps registrieren

### Spotify

1. <https://developer.spotify.com/dashboard> → **Create app**
2. Name und Beschreibung frei wählen.
3. **Redirect URI** exakt so eintragen:
   ```
   http://127.0.0.1:8080/callback
   ```
   Spotify akzeptiert bei `http` nur noch die IP `127.0.0.1`, **nicht**
   `localhost`.
4. Unter **APIs used**: *Web API* auswählen.
5. Nach dem Speichern unter *Settings* die **Client ID** und das
   **Client Secret** notieren.

### Deezer

Nur nötig, wenn du **private** Playlisten spiegeln willst. Öffentliche
Playlisten liest das Tool ohne Token.

1. <https://developers.deezer.com/myapps> → **Create a new Application**
2. **Application domain**: `localhost`
3. **Redirect URL after authentication**:
   ```
   http://localhost:8080/callback
   ```
4. **Application ID** und **Secret Key** notieren.

---

## Schritt 2 – Tokens erzeugen

Der Container läuft unbeaufsichtigt und kann sich nicht interaktiv anmelden.
Die Anmeldung machst du deshalb **einmal** auf einem Rechner mit Browser; das
Ergebnis sind zwei Werte, die du später in Portainer einträgst.

```bash
git clone https://github.com/jeb94-code/music-sync.git
cd music-sync
cp .env.example .env
# In .env eintragen: SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET,
#                    DEEZER_APP_ID, DEEZER_APP_SECRET
docker compose run --rm --service-ports auth
```

Das Skript gibt eine URL aus. Im Browser öffnen, Zugriff bestätigen, fertig —
am Ende stehen im Terminal:

```
SPOTIFY_REFRESH_TOKEN=AQD...
DEEZER_ACCESS_TOKEN=frb...
```

Beide Werte gut aufheben. Nur ein Provider nötig? Dann
`... run --rm --service-ports auth spotify` bzw. `... auth deezer`.

> Das Spotify-Refresh-Token läuft nicht ab. Das Deezer-Token wird mit
> `offline_access` angefragt und ist damit ebenfalls dauerhaft gültig.
> Beides wird ungültig, wenn du in den Kontoeinstellungen den App-Zugriff
> entziehst — dann den Helper erneut laufen lassen.

---

## Schritt 3 – Playlist-IDs heraussuchen

**Deezer:** Playliste öffnen, die Zahl aus der URL nehmen.
`https://www.deezer.com/de/playlist/908622995` → `908622995`

**Spotify (optional):** Playliste → *Teilen* → *Link kopieren*.
`https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=...`
→ `37i9dQZF1DXcBWIGoYBM5M`

Die Zuordnung schreibst du in die Variable `PLAYLISTS`. Mehrere Einträge
werden durch `;` oder Zeilenumbruch getrennt — in der Portainer-Oberfläche ist
`;` praktischer:

```
908622995 -> "Rock Mirror"; 123456789 -> "Chill Mirror"
```

Vier Schreibweisen sind möglich:

| Eintrag | Bedeutung |
| --- | --- |
| `908622995` | Spotify-Playliste heißt wie die Deezer-Playliste, wird bei Bedarf angelegt |
| `908622995 -> "Rock Mirror"` | Spotify-Playliste mit diesem Namen, wird bei Bedarf angelegt |
| `908622995 -> 37i9dQZF1DXcBWIGoYBM5M` | genau diese vorhandene Spotify-Playliste |
| `908622995 -> https://open.spotify.com/playlist/37i9...` | dasselbe, als Link |

Auf der Deezer-Seite geht statt der ID auch die volle URL. Zeilen mit `#` am
Anfang sind Kommentare. Zwei Deezer-Playlisten auf dasselbe Spotify-Ziel zu
richten wird beim Start abgelehnt — die zweite würde die erste überschreiben.

---

## Schritt 4 – Stack in Portainer anlegen

1. **Stacks → Add stack → Repository**
2. **Repository URL**: `https://github.com/jeb94-code/music-sync`
3. **Repository reference**: `refs/heads/main`
4. **Compose path**: `docker-compose.yml`
5. **Environment variables** — mindestens diese vier:

   | Name | Wert |
   | --- | --- |
   | `SPOTIFY_CLIENT_ID` | aus dem Spotify-Dashboard |
   | `SPOTIFY_CLIENT_SECRET` | aus dem Spotify-Dashboard |
   | `SPOTIFY_REFRESH_TOKEN` | aus Schritt 2 |
   | `PLAYLISTS` | aus Schritt 3 |

   Für private Deezer-Playlisten zusätzlich `DEEZER_ACCESS_TOKEN`.
   Für den ersten Lauf zusätzlich `DRY_RUN` = `true`.

6. **Deploy the stack.** Portainer baut das Image aus dem Repository und
   startet den Container.

Fehlt eine Pflichtvariable, bricht das Deployment mit einer klaren Meldung ab
(`required variable SPOTIFY_CLIENT_ID is missing a value: ...`) statt einen
halb konfigurierten Container zu starten.

**Automatische Updates:** In den Stack-Einstellungen *GitOps updates*
aktivieren, damit Portainer neue Commits selbstständig ausrollt.

---

## Schritt 5 – Trockenlauf, dann scharf schalten

Mit `DRY_RUN=true` protokolliert der Container jede geplante Änderung, ohne
Spotify anzufassen:

```
INFO  Deezer playlist 'Rock' (908622995): 42 tracks
INFO  No Spotify match for Some Band - Some Rare Track
INFO  Spotify playlist 'Rock Mirror': 0 -> 41 tracks (+41/-0)
INFO  [dry-run] not writing to Spotify
INFO  Sync finished in 21.4s (1 playlist(s))
INFO    UPDATED 'Rock Mirror': 41 tracks (+41/-0, 1 unmatched)
```

Sieht das plausibel aus, `DRY_RUN` auf `false` setzen und den Stack neu
deployen. Der erste echte Lauf dauert je nach Playlistgröße ein paar Minuten
(ein Deezer-Abruf pro Titel für den ISRC); alle weiteren sind dank Cache
deutlich schneller.

---

## Environment-Variablen

### Pflicht

| Variable | Beschreibung |
| --- | --- |
| `SPOTIFY_CLIENT_ID` | Client ID der Spotify-App |
| `SPOTIFY_CLIENT_SECRET` | Client Secret der Spotify-App |
| `SPOTIFY_REFRESH_TOKEN` | Refresh-Token aus dem Auth-Helper |
| `PLAYLISTS` | Zuordnung Deezer → Spotify (siehe Schritt 3) |

### Optional

| Variable | Standard | Beschreibung |
| --- | --- | --- |
| `DEEZER_ACCESS_TOKEN` | – | Nötig für private Deezer-Playlisten |
| `SYNC_INTERVAL_MINUTES` | `60` | Abstand zwischen zwei Läufen |
| `RUN_ONCE` | `false` | Einmal laufen und beenden (für eigene Zeitsteuerung) |
| `DRY_RUN` | `false` | Nur protokollieren, nichts schreiben |
| `LOG_LEVEL` | `INFO` | `DEBUG` zeigt jede Zuordnungsentscheidung |
| `TZ` | `Europe/Berlin` | Zeitzone für die Log-Zeitstempel |
| `CREATE_MISSING_PLAYLISTS` | `true` | Fehlende Spotify-Playlisten anlegen |
| `SPOTIFY_PLAYLISTS_PUBLIC` | `false` | Neu angelegte Playlisten öffentlich machen |
| `SPOTIFY_PLAYLIST_DESCRIPTION` | siehe `.env.example` | Beschreibung neuer Playlisten |
| `MATCH_THRESHOLD` | `0.72` | Höher = strenger, weniger Fehltreffer, mehr Lücken |
| `DURATION_TOLERANCE_SECONDS` | `8` | Erlaubte Längenabweichung ohne Abwertung |
| `SEARCH_LIMIT` | `8` | Suchtreffer pro Titel (max. 50) |
| `ALLOW_EMPTY_MIRROR` | `false` | Leeren der Spotify-Playliste zulassen |
| `ALLOW_PARTIAL_SOURCE` | `false` | Spiegeln, auch wenn Deezer weniger Titel liefert als angekündigt |
| `MAX_UNMATCHED_RATIO` | `1.0` | Abbruch, wenn mehr als dieser Anteil fehlt (z. B. `0.3`) |
| `HTTP_TIMEOUT_SECONDS` | `30` | Timeout pro API-Aufruf |
| `HTTP_MAX_RETRIES` | `5` | Wiederholungen bei Netz-/Ratelimit-Fehlern |
| `DATA_DIR` | `/data` | Ablage für Cache, Report und Heartbeat |

---

## Betrieb

**Logs:** Portainer → Containers → `music-sync` → *Logs*, oder
`docker logs -f music-sync`. Die Logausgabe ist auf 3 × 10 MB begrenzt.

**Nicht zugeordnete Titel:** stehen nach jedem Lauf in `/data/unmatched.log`:

```bash
docker exec music-sync cat /data/unmatched.log
```

Eine Zeile je Titel mit Playliste, Interpret, Deezer-ID und ISRC — genug, um
den Titel bei Bedarf von Hand nachzutragen oder `MATCH_THRESHOLD` zu senken.

**Health:** Der Container schreibt nach jedem Lauf einen Heartbeat. Bleibt er
länger als zwei Intervalle aus, meldet Docker den Container als `unhealthy`.

**Cache zurücksetzen** (erzwingt eine komplette Neuzuordnung):

```bash
docker exec music-sync rm -f /data/match-cache.sqlite3
docker restart music-sync
```

Negative Treffer (Titel, die auf Spotify nicht gefunden wurden) werden ohnehin
nach 7 Tagen automatisch neu geprüft — Spotify-Kataloge wachsen.

**Eigene Zeitsteuerung statt Dauerbetrieb:** `RUN_ONCE=true` setzen und den
Container per Cron oder Portainer starten. Der Exit-Code ist `1`, wenn eine
Playliste fehlgeschlagen ist, `2` bei Konfigurationsfehlern.

---

## Fehlersuche

| Meldung | Ursache und Lösung |
| --- | --- |
| `Configuration error: Required environment variable ... is not set` | Variable fehlt im Stack. Container startet bewusst nicht halb konfiguriert. |
| `Spotify did not return an access token` | `SPOTIFY_CLIENT_ID`/`SECRET`/`REFRESH_TOKEN` passen nicht zusammen — Auth-Helper erneut laufen lassen. |
| `Deezer rejected the request ... (code 300)` | Deezer-Token ungültig oder zurückgezogen. Helper erneut laufen lassen. |
| `Spotify playlist ... is owned by ..., not by you` | Auf eine fremde Playliste gezeigt. Spotify erlaubt nur Schreibzugriff auf eigene. |
| `Refusing to mirror a partial playlist` | Deezer hat eine Seite nicht geliefert. Normalerweise nichts zu tun — der nächste Lauf holt es nach. Tritt es bei *jedem* Lauf auf, zählt Deezer Titel mit, die es nicht mehr ausliefert: dann `ALLOW_PARTIAL_SOURCE=true`. |
| `Mirroring would empty Spotify playlist ...` | Kein einziger Titel zugeordnet — meist ein Token-Problem. Erst prüfen, dann ggf. `ALLOW_EMPTY_MIRROR=true`. |
| Viele `No Spotify match` | Titel fehlen im Spotify-Katalog deiner Region, oder `MATCH_THRESHOLD` ist zu hoch. `LOG_LEVEL=DEBUG` zeigt die Bewertungen. |
| `INVALID_CLIENT: Invalid redirect URI` beim Auth-Helper | Redirect-URI im Spotify-Dashboard muss exakt `http://127.0.0.1:8080/callback` lauten. |

Tokens werden nirgends im Klartext geloggt: Deezer überträgt sie als
URL-Parameter, und alles, was in Logs oder Fehlermeldungen landen kann, wird
vorher durch `<redacted>` ersetzt. Logs können also gefahrlos geteilt werden.

---

## Grenzen

- **Eine Richtung.** Deezer → Spotify. Spotify-seitige Änderungen werden
  überschrieben, nicht zurückgespielt.
- **Nicht jeder Titel existiert auf beiden Plattformen.** Lokale Dateien,
  regional gesperrte und exklusive Titel bleiben außen vor.
- **Kein Podcast-Support.** Nur Musiktitel.
- **Die Spotify-Playliste wird ersetzt, nicht gemerged** — inklusive
  Reihenfolge. „Hinzugefügt am“ ist danach für alle Titel identisch.
- Die Deezer-API begrenzt auf ~50 Anfragen / 5 s; das Tool drosselt sich
  selbst und wiederholt bei Ratelimits mit wachsender Wartezeit.

---

## Entwicklung

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests -q
```

Die Tests laufen vollständig offline gegen In-Memory-Doubles der beiden APIs
und decken Konfigurations-Parsing, Titelzuordnung, Retry-/Redaction-Verhalten
und die Spiegel-Logik samt Sicherungen ab.

```
musicsync/
  config.py       Environment einlesen und validieren
  httpclient.py   Retries, Backoff, Redaction
  deezer.py       Deezer-API (Master)
  spotify.py      Spotify-API (Ziel)
  matching.py     Normalisierung und Bewertung der Treffer
  cache.py        SQLite-Cache für Zuordnungen
  sync.py         Spiegel-Logik und Sicherungen
  auth.py         Einmaliger OAuth-Helper
  healthcheck.py  Docker HEALTHCHECK
  __main__.py     Entrypoint und Zeitplan
```

Lokal laufen lassen:

```bash
cp .env.example .env   # ausfüllen
docker compose up --build
```
