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
2. [Schritt 1 – Spotify-App anlegen](#schritt-1--spotify-app-anlegen)
3. [Schritt 2 – Spotify-Token erzeugen](#schritt-2--spotify-token-erzeugen)
4. [Schritt 3 – Deezer vorbereiten](#schritt-3--deezer-vorbereiten)
5. [Schritt 4 – Playlist-IDs heraussuchen](#schritt-4--playlist-ids-heraussuchen)
6. [Schritt 5 – Stack in Portainer anlegen](#schritt-5--stack-in-portainer-anlegen)
7. [Schritt 6 – Trockenlauf, dann scharf schalten](#schritt-6--trockenlauf-dann-scharf-schalten)
8. [Alternative: Steuerung über n8n](#alternative-steuerung-über-n8n)
9. [Environment-Variablen](#environment-variablen)
10. [Betrieb](#betrieb)
11. [Fehlersuche](#fehlersuche)
12. [Grenzen](#grenzen)
13. [Entwicklung](#entwicklung)

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

## Schritt 1 – Spotify-App anlegen

Damit das Tool in deinem Namen Playlisten schreiben darf, braucht es eine
eigene App-Registrierung. Kostenlos, dauert drei Minuten.

1. <https://developer.spotify.com/dashboard> öffnen, mit deinem Spotify-Konto
   anmelden, **Create app**.
2. Name und Beschreibung frei wählen.
3. **Redirect URI** exakt so eintragen — Zeichen für Zeichen:
   ```
   http://127.0.0.1:8080/callback
   ```
   Es muss `127.0.0.1` sein. Spotify lehnt `localhost` bei `http` inzwischen ab.
4. Unter **Which API/SDKs are you planning to use?** die **Web API** ankreuzen.
5. Speichern, dann *Settings* öffnen: dort stehen **Client ID** und (hinter
   *View client secret*) das **Client Secret**. Beide brauchst du gleich.

---

## Schritt 2 – Spotify-Token erzeugen

### Warum dieser Schritt existiert

Client ID und Secret allein genügen nicht: sie identifizieren die App, nicht
dich. Damit die App auf *dein* Konto zugreifen darf, musst du das einmal im
Browser bestätigen. Heraus kommt ein **Refresh Token** — ein dauerhaft
gültiger Wert, mit dem der Container sich später selbst anmeldet, ohne dass du
je wieder etwas bestätigen musst.

Der Container kann diesen Schritt nicht selbst machen, weil er keinen Browser
hat. Deshalb einmal von Hand. Ergebnis ist **eine einzige Textzeile**, die du
später in Portainer einträgst.

### Wo ausführen?

Auf dem Rechner, auf dem auch dein **Browser** läuft — typischerweise dein
Laptop. Docker muss dort installiert sein. Läuft Docker nur auf dem Server,
siehe [weiter unten](#variante-server-ohne-browser).

### Die zwei Befehle

**1. Image bauen** (holt den Code direkt von GitHub, kein Clone nötig):

```bash
docker build -t music-sync "https://github.com/jeb94-code/music-sync.git#claude/spotify-deezer-sync-vgh7hl"
```

**2. Helper starten** — die beiden Werte aus Schritt 1 einsetzen:

```bash
docker run --rm -p 8080:8080 \
  -e SPOTIFY_CLIENT_ID=hier_deine_client_id \
  -e SPOTIFY_CLIENT_SECRET=hier_dein_client_secret \
  --entrypoint python music-sync -m musicsync.auth spotify
```

### Was dann passiert

Das Terminal zeigt:

```
Open this URL in your browser and approve the access:

    https://accounts.spotify.com/authorize?client_id=...

Waiting for the redirect to http://127.0.0.1:8080/callback ...
```

Und bleibt stehen — das ist richtig so, es wartet auf dich.

1. Die lange URL kopieren und im Browser öffnen.
2. Spotify fragt, ob die App auf dein Konto zugreifen darf → **Agree**.
3. Der Browser landet auf einer Seite „Spotify connected“. Die kannst du
   schließen.
4. Im Terminal steht jetzt:

```
====================================================================
Add these to your Portainer stack environment (never commit them):
====================================================================
SPOTIFY_REFRESH_TOKEN=AQDx7f9...
====================================================================
```

Diese Zeile ist das Ergebnis. Kopier sie dir weg — sie kommt in Schritt 5 in
Portainer. Der Container hat sich selbst beendet, es läuft nichts weiter.

### Variante: Server ohne Browser

Der Redirect geht an `127.0.0.1:8080`, also an den Rechner mit dem Browser.
Läuft der Helper auf dem Server, leitet ein SSH-Tunnel das dorthin weiter:

```bash
ssh -L 8080:localhost:8080 user@dein-server
```

In dieser SSH-Sitzung dann dieselben zwei Befehle von oben ausführen. Die
ausgegebene URL im Browser deines **Laptops** öffnen; der Tunnel bringt den
Redirect zum Helper auf dem Server. Wichtig: die SSH-Sitzung offen lassen,
bis der Token im Terminal steht.

> Wenn du das Repository ohnehin geklont hast, geht statt der zwei Befehle
> auch `docker compose -f docker-compose.auth.yml run --rm --service-ports auth spotify`.

---

## Schritt 3 – Deezer vorbereiten

**Für öffentliche Playlisten ist hier nichts zu tun.** Deezers öffentliche API
liefert sie ohne jede Anmeldung — inklusive aller Titel und ihrer ISRC-Codes.
Du brauchst weder App noch Token.

Prüfen kannst du das mit der Playlist-ID aus Schritt 4:

```bash
curl -s "https://api.deezer.com/playlist/DEINE_ID" | head -c 200
```

Kommt der Titel deiner Playliste zurück, ist alles gut. Kommt
`{"error":{"type":"DataException","message":"no data","code":800}}`, ist die
Playliste privat oder die ID falsch.

### Private Playlisten

Dafür wäre ein Deezer-Token nötig — und das ist derzeit **nicht erhältlich**:
Deezer hat die Registrierung neuer Apps geschlossen
(„We're not accepting new application creation at this time.“). Ohne
bestehende App gibt es keinen legitimen Weg zu einem Token.

Praktikable Lösung: die betroffene Playliste in Deezer auf öffentlich stellen.
In der Deezer-App: Playliste → **⋯** → *Playlist bearbeiten* → Schalter
**Geheim/Privat** ausschalten. Öffentlich heißt „über den Link erreichbar und
in deinem Profil sichtbar“ — sie wird dadurch nicht beworben.

Hast du eine **ältere** Deezer-App, funktioniert `DEEZER_ACCESS_TOKEN`
unverändert; den Wert liefert
`docker compose -f docker-compose.auth.yml run --rm --service-ports auth deezer`.

---

## Schritt 4 – Playlist-IDs heraussuchen

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

## Schritt 5 – Stack in Portainer anlegen

1. **Stacks → Add stack → Repository**
2. **Repository URL**: `https://github.com/jeb94-code/music-sync`
3. **Repository reference**: der Branch, in dem der Code liegt. Solange nichts
   nach `main` gemerged ist, ist das
   `refs/heads/claude/spotify-deezer-sync-vgh7hl` — nach einem Merge
   entsprechend `refs/heads/main`.
4. **Compose path**: `docker-compose.yml`
5. **Environment variables** — mindestens diese vier:

   | Name | Wert |
   | --- | --- |
   | `SPOTIFY_CLIENT_ID` | aus Schritt 1 |
   | `SPOTIFY_CLIENT_SECRET` | aus Schritt 1 |
   | `SPOTIFY_REFRESH_TOKEN` | aus Schritt 2 |
   | `PLAYLISTS` | aus Schritt 4 |

   `DEEZER_ACCESS_TOKEN` nur, falls du eine ältere Deezer-App hast (siehe Schritt 3).
   Für den ersten Lauf zusätzlich `DRY_RUN` = `true`.

6. **Deploy the stack.** Portainer baut das Image aus dem Repository und
   startet den Container.

Fehlt eine Pflichtvariable, bricht das Deployment mit einer klaren Meldung ab
(`required variable SPOTIFY_CLIENT_ID is missing a value: ...`) statt einen
halb konfigurierten Container zu starten.

**Automatische Updates:** In den Stack-Einstellungen *GitOps updates*
aktivieren, damit Portainer neue Commits selbstständig ausrollt.

---

## Schritt 6 – Trockenlauf, dann scharf schalten

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
deployen. Die Deezer-Seite ist schnell (zwei Anfragen je 100 Titel), die
Spotify-Suche bestimmt die Dauer: grob eine Anfrage pro noch unbekanntem
Titel. Ab dem zweiten Lauf greift der Cache und es bleibt fast nichts zu tun.

---

## Alternative: Steuerung über n8n

Wenn n8n ohnehin läuft, kann es den Zeitplan übernehmen. Der Container macht
dann von sich aus nichts mehr, sondern wartet auf einen HTTP-Trigger und gibt
das Ergebnis als JSON zurück — damit kannst du in n8n direkt auf Fehler
reagieren.

### Stack umstellen

Gleiches Repository, gleiche Variablen, nur zwei Unterschiede:

- **Compose path** in Portainer auf `docker-compose.n8n.yml` setzen
- zusätzlich `API_TOKEN` als Variable setzen:
  ```bash
  openssl rand -hex 32
  ```

`SYNC_INTERVAL_MINUTES` ist in diesem Modus wirkungslos — n8n bestimmt den
Takt. Ohne `API_TOKEN` startet der Container bewusst nicht: ein `POST /sync`
schreibt deine Playlisten um, und das darf im Heimnetz nicht jeder auslösen.

### Endpoints

| Methode | Pfad | Auth | Zweck |
| --- | --- | --- | --- |
| `GET` | `/health` | nein | Erreichbarkeit, für Healthcheck und n8n-Test |
| `GET` | `/status` | ja | letztes Ergebnis, und ob gerade ein Lauf läuft |
| `POST` | `/sync` | ja | Lauf starten, Ergebnis abwarten |
| `POST` | `/sync?wait=false` | ja | Lauf starten, sofort `202` zurück, später `/status` pollen |

Authentifiziert wird per `X-Auth-Token: <API_TOKEN>` oder
`Authorization: Bearer <API_TOKEN>`. Läuft bereits ein Sync, antwortet ein
zweiter Trigger mit `409` statt sich anzustellen.

Schneller Test von der Kommandozeile:

```bash
curl -s -X POST http://localhost:8477/sync \
  -H "X-Auth-Token: $API_TOKEN" | jq .totals
```

### Antwortformat

```jsonc
{
  "started_at": "2026-01-01T09:00:00+00:00",
  "finished_at": "2026-01-01T09:00:21+00:00",
  "duration_s": 21.4,
  "dry_run": false,
  "totals": {
    "playlists": 2,
    "failed": 0,          // Playlisten mit Fehler
    "skipped": 0,         // von einer Sicherung abgebrochen
    "changed": 1,         // tatsächlich geändert
    "unmatched_tracks": 3 // Titel ohne Spotify-Entsprechung
  },
  "playlists": [
    {
      "deezer_id": "908622995",
      "deezer_title": "Rock",
      "spotify_name": "Rock Mirror",
      "source_tracks": 42, "matched_tracks": 41,
      "changed": true, "added": 3, "removed": 1, "reordered": false,
      "error": null, "skipped_reason": null,
      "unmatched": [{ "deezer_id": "123", "label": "Band - Titel", "isrc": null }]
    }
  ]
}
```

Ein abgeschlossener Lauf antwortet immer mit `200`, auch wenn einzelne
Playlisten fehlgeschlagen sind — verzweige in n8n auf `totals.failed`, nicht
auf den HTTP-Status. `500` bedeutet, dass der Lauf als Ganzes gescheitert ist
(z. B. Deezer nicht erreichbar).

### Fertiger Workflow

`n8n/music-sync-workflow.json` importieren (n8n → *Workflows* → *Import from
File*). Enthalten: Schedule Trigger (stündlich) → HTTP Request → IF auf
`totals.failed` → vorbereiteter Alert-Text. Zu tun bleibt:

1. Im Node **Trigger sync** eine *Header Auth*-Credential anlegen:
   Name `X-Auth-Token`, Value = dein `API_TOKEN`.
2. Die URL prüfen. Stehen n8n und music-sync im selben Docker-Netz, passt
   `http://music-sync:8477/sync` direkt und du kannst den Port im Compose gar
   nicht erst veröffentlichen. Sonst `http://<host-ip>:8477/sync`.
3. Den Node **Build alert** durch deine Benachrichtigung ersetzen (Telegram,
   Gotify, Mail …) — `subject` und `body` sind fertig befüllt.

Der HTTP-Request-Node hat 10 Minuten Timeout. Das reicht für einige tausend
Titel; bei einer sehr großen Bibliothek nimm für den ersten Lauf einmalig
`/sync?wait=false` und poll danach `/status`. Ab dem zweiten Lauf greift der
Cache und es geht deutlich schneller.

---

## Environment-Variablen

### Pflicht

| Variable | Beschreibung |
| --- | --- |
| `SPOTIFY_CLIENT_ID` | Client ID der Spotify-App |
| `SPOTIFY_CLIENT_SECRET` | Client Secret der Spotify-App |
| `SPOTIFY_REFRESH_TOKEN` | Refresh-Token aus dem Auth-Helper |
| `PLAYLISTS` | Zuordnung Deezer → Spotify (siehe Schritt 4) |

### Optional

| Variable | Standard | Beschreibung |
| --- | --- | --- |
| `DEEZER_ACCESS_TOKEN` | – | Nötig für private Deezer-Playlisten |
| `MODE` | `schedule` | `schedule` (eigener Takt), `server` (HTTP-Trigger, siehe n8n), `once` (ein Lauf, dann Ende) |
| `SYNC_INTERVAL_MINUTES` | `60` | Abstand zwischen zwei Läufen, nur bei `MODE=schedule` |
| `API_TOKEN` | – | Pflicht bei `MODE=server`, schützt `/sync` und `/status` |
| `HTTP_PORT` | `8477` | Port im Server-Modus |
| `HTTP_BIND` | `0.0.0.0` | Bind-Adresse im Server-Modus |
| `RUN_ONCE` | `false` | Altes Flag, entspricht `MODE=once`; `MODE` hat Vorrang |
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

**Health:** Im Schedule-Modus schreibt der Container nach jedem Lauf einen
Heartbeat; bleibt er länger als zwei Intervalle aus, meldet Docker den
Container als `unhealthy`. Im Server-Modus gibt es zwischen den Triggern
nichts zu schlagen — dort prüft der Healthcheck stattdessen, ob `/health`
noch antwortet.

**Cache zurücksetzen** (erzwingt eine komplette Neuzuordnung):

```bash
docker exec music-sync rm -f /data/match-cache.sqlite3
docker restart music-sync
```

Negative Treffer (Titel, die auf Spotify nicht gefunden wurden) werden ohnehin
nach 7 Tagen automatisch neu geprüft — Spotify-Kataloge wachsen.

**Eigene Zeitsteuerung statt Dauerbetrieb:** `MODE=once` setzen und den
Container per Cron oder Portainer starten. Der Exit-Code ist `1`, wenn eine
Playliste fehlgeschlagen ist, `2` bei Konfigurationsfehlern. Wer n8n nutzt,
fährt mit `MODE=server` besser — siehe
[Steuerung über n8n](#alternative-steuerung-über-n8n).

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
| `Deezer has no readable data for /playlist/...` | ID falsch, oder die Playliste ist nicht öffentlich. Deezer meldet beides gleich. Mit `curl -s "https://api.deezer.com/playlist/DEINE_ID"` prüfen. |
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
  runner.py       Einen Lauf zusammenbauen, Ergebnis als JSON
  server.py       HTTP-Trigger für n8n (MODE=server)
  auth.py         Einmaliger OAuth-Helper
  healthcheck.py  Docker HEALTHCHECK
  __main__.py     Entrypoint und Betriebsmodi
```

Lokal laufen lassen:

```bash
cp .env.example .env   # ausfüllen
docker compose up --build
```
