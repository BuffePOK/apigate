# apigate – tutorial krok po kroku (Windows lokalnie → GitHub → VPS z Podmanem)

Wszystkie komendy `apigate ...` uruchamiasz w PowerShellu w folderze projektu
(folder z `pyproject.toml`). Dane (baza, `master.key`) są w `.\data`.

---------------------------------------------------------------------------
## 0. Zanim zaczniesz: zatrzymaj serwer i uruchom go ponownie po zmianach

Po aktualizacji plików zrestartuj serwer (Ctrl+C w oknie z `apigate serve`, potem `apigate serve`).
Domyślny port lokalny to teraz **18731** (był 8080, który jest zbyt popularny). Inny: `apigate serve --port 9999`.
Restart czyści też „blokady” modeli (np. 30 min cooldownu po błędzie „model no longer available”).

---------------------------------------------------------------------------
## 1. Limity Gemini (z Twojej tabeli AI Studio)

Limity są **na klucz** (projekt Google) i sprawdzane przez bramkę, zanim wyśle zapytanie do Google.

```powershell
apigate load-limits presets\gemini-free.toml
apigate models gemini
```

Co to robi:
* reguła `*` z `rpm = 0` **ukrywa wszystkie modele Gemini** (u Ciebie w AI Studio mają 0/0),
* następne reguły włączają tylko: Gemini 3.5 Flash Lite (15 RPM / 250K TPM / 500 RPD), 3.1 Flash Lite,
  3.8 / 3.6 / 3 Flash (5 / 250K / 20), Gemma 4 31B i 26B (30 / 16K / 14 400), Embedding (100 / 30K / 1000),
* komenda można uruchamiać wiele razy (aktualizuje, nie dubluje).

`apigate models gemini` pokazuje każdy model jako `SERVED` (można używać) lub `hidden`.
**Sprawdź, czy ID modeli pasują do wzorców** (np. czy Gemma to `gemma-4-31b-it`). Jeśli model, który chcesz,
jest `hidden`, popraw wzorzec w `presets\gemini-free.toml` (lub w panelu: *Admin → Limits*) i uruchom `load-limits` jeszcze raz.
Gdy kiedyś przejdziesz na płatny plan Google – zmień liczby w pliku i uruchom ponownie.

Uwaga: Gemma ma TPM 16 000, więc pojedyncze zapytanie większe niż ~16K tokenów zostanie odrzucone
z czytelnym błędem 400 (to limit Google, nie bramki).

---------------------------------------------------------------------------
## 2. Klucz Claude

```powershell
apigate add-key claude --label main     # „claude” = alias do „anthropic”
apigate status
```

---------------------------------------------------------------------------
## 3. Test bramki

Klucz bramki (`sk-gate-...`) podaj przez zmienną środowiskową (nie ląduje w historii komend):

```powershell
$env:APIGATE_SMOKE_KEY = "sk-gate-..."
python scripts\smoke.py --base http://127.0.0.1:18731
```

Skrypt sam wybiera tani model, który odpowiada (pomija te, które zwracają błąd, i pisze `[skip]`).
Konkretny model: `--model gemini/gemini-3.5-flash-lite`. Wszystkie linie powinny mieć `[OK ]`.

Ręczny test przez `curl` (PowerShell: `curl.exe`):

```powershell
curl.exe http://127.0.0.1:18731/v1/chat/completions -H "Authorization: Bearer $env:APIGATE_SMOKE_KEY" -H "Content-Type: application/json" -d "{\"model\":\"gemini/gemma-4-31b-it\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}"
```

Typowe odpowiedzi: `502 Upstream error` = błąd po stronie dostawcy (bramka próbowała kilka razy),
`429` = limit klucza / chwilowa blokada po błędzie, `404` = brak takiego modelu na liście.

---------------------------------------------------------------------------
## 4. Użytkownicy, klucze API i kredyty

W panelu (http://127.0.0.1:18731, zaloguj się jako admin):
* **Users** – tworzenie kont, dodawanie kredytów, blokowanie,
* **API keys** – tworzenie kluczy `sk-gate-...` dla użytkownika (pokazywane **tylko raz**),
* **Routing & prices** – strategia doboru kluczy (round_robin / random / fill do 70% / least_loaded),
  ceny (kredyty za 1M tokenów), aliasy modeli, rejestracja (`allow_signup`).

Z linii komend: `apigate grant NAZWA 100` dodaje 100 kredytów. Admin utworzony przez `create-admin` ma kredyty bez limitu.
Zwykły użytkownik bez kredytów dostaje 402 – tak jest celowo, dopóki nie dodasz mechanizmu zakupu.
**Na serwerze zostaw `allow_signup = false`.**

---------------------------------------------------------------------------
## 5. GitHub – repozytorium PUBLICZNE (część A, robisz na swoim komputerze)

Repo publiczne = każdy zobaczy **cały kod i całą historię commitów na zawsze**. Sekretów nie wolno wrzucić nawet raz
(usunięcie pliku w kolejnym commicie nic nie daje – wtedy trzeba unieważnić klucz). `.gitignore` blokuje m.in.: `data\`, `master.key`, `*.db`, `.env`, `config.toml`, `keys*.txt`, `*credits*.txt`, `*kredits*.txt`, `*.pem`.

**5.1 Licencja.** W pliku `LICENSE` zamień `apigate authors` na swoje imię/pseudonim.

**5.2 CI/CD workflow.** Plik `deploy\ci.yml` zawiera: testy (Python 3.11 i 3.13) → dopiero po zielonych testach (tylko na gałęzi `main`) budowa obrazu, test dymny obrazu i wysłanie do rejestru GitHub (`ghcr.io`).
```powershell
mkdir .github\workflows -Force
copy /Y deploy\ci.yml .github\workflows\ci.yml
```

**5.3 Tożsamość w commitach.** W publicznym repo e-mail z commita jest widoczny – użyj adresu „noreply” z GitHuba
(Settings → Emails → „Keep my email addresses private”):
```powershell
git config --global user.name "Twoje Imię"
git config --global user.email "ID+login@users.noreply.github.com"
```

**5.4 Commit i push** (repo już utworzone):
```powershell
git add .
git status --short            # sprawdź: nic z data\, .env, master.key, *.txt z kluczami
git commit -m "podman/quadlet deployment, unix socket, CI/CD"
git push
```
W zakładce **Actions** na GitHubie zobaczysz przebieg `ci`. Po zielonych testach job `image` wyśle obraz `ghcr.io/twoj_login/apigate:latest`.

**5.5 Pakiet publiczny (jednorazowo, po pierwszym zielonym przebiegu).** GitHub → Twój profil → *Packages* → `apigate` → *Package settings* → *Change visibility* → **Public**.
Dzięki temu serwer pobiera obraz bez logowania (kod i tak jest publiczny, więc nic nie tracisz).

**5.6 Ochrona gałęzi `main` – ważne, bo push do `main` = wdrożenie na produkcję.**
Settings → Rules → Rulesets → New branch ruleset → cel: `main` → włącz: *Require a pull request before merging* oraz *Require status checks to pass* (dodaj `test (3.11)` i `test (3.13)`) oraz *Block force pushes*.
Włącz też 2FA na koncie GitHub. Kto może wypchnąć kod do `main`, ten kontroluje serwer.

**5.7 Ustawienia bezpieczeństwa** (Settings → Security): *Private vulnerability reporting*, *Secret scanning* + *Push protection*, *Dependabot alerts*.

**5.8 Użycie w innych projektach:**
```bash
pip install "git+https://github.com/TWOJ_LOGIN/apigate.git"        # biblioteka (Gateway in-process)
```
albo zwykły klient OpenAI: `base_url="https://llm-apis.ans3r.eu/v1"`, `api_key="sk-gate-..."`.

---------------------------------------------------------------------------
## 6. VPS (Ubuntu 26.04): Podman rootless + gniazdo unix + wspólny nginx + automatyczne wdrażanie

### 6.0 Decyzje (dlaczego tak)

| Pytanie | Decyzja |
|---|---|
| Docker czy Podman? | **Podman rootless.** Demon Dockera działa jako root, a członek grupy `docker` ma w praktyce roota. W rootless Podman każdy projekt = osobny użytkownik Linuksa, własne kontenery, własny magazyn (`~/.local/share/containers`), brak demona. Przejęcie kontenera/konta jednego projektu nie daje roota ani dostępu do plików innych projektów. |
| Porty i dostęp lokalny | **Dwa wejścia, oba należą do systemd (przeżywają restart kontenera):** (1) **gniazdo unix** `/srv/apps/apigate/run/apigate.sock` (`0660`, grupa projektu) – tylko dla nginx; (2) **`127.0.0.1:18731`** – **otwarte dla wszystkich lokalnych projektów i użytkowników** (każde wywołanie nadal wymaga klucza `sk-gate-…`). Inne projekty używają więc `http://127.0.0.1:18731/v1` jak zwykłego API OpenAI. Port 18731 jest rzadki (nie 8080); zmienisz go w jednym miejscu: `apigate.socket`. Port 18080 istnieje wyłącznie *wewnątrz* kontenera (health-check), nie koliduje z niczym. Uczciwie: każdy lokalny proces może próbować logować się do panelu przez 18731 (chronią go hasła, limity prób i klucze); jeśli kiedyś tego nie chcesz, usuń linię `ListenStream=127.0.0.1:…`. |
| Wspólny nginx | Jeden nginx na hoście (HTTPS, certyfikaty, routing domen). Dla każdego projektu osobna grupa Linuksa i osobne gniazdo. Uczciwie: proces nginx musi móc połączyć się ze wszystkimi gniazdami, więc jego przejęcie daje dostęp do *API* wszystkich projektów (nie do ich plików, kluczy ani kontenerów). Dlatego nginx trzymaj aktualny i bez dodatkowych modułów. |
| Limity 1 CPU / 2 GB na projekt | Dwie warstwy: (1) **cgroup użytkownika** (`CPUQuota=100%`, `MemoryMax=2G`) – twardy limit dla całego konta, (2) limit samego kontenera (`--cpus=1 --memory=2g`). Wewnątrz działa jeden proces, więc nie ma czego „balansować”. |
| CI/CD | GitHub Actions: testy → (tylko po sukcesie, tylko `main`) obraz do `ghcr.io`. Serwer **sam co ~2 min sprawdza**, czy jest nowy obraz (`podman auto-update`) i wdraża. Nie ma kluczy SSH ani sekretów w GitHubie, nic nie łączy się z serwerem z zewnątrz. |
| „Bezprzerwowe” | Gniazdem zarządza systemd, nie kontener. Podczas wymiany kontenera nowe żądania **czekają w kolejce jądra** (zwykle 3–10 s) zamiast dostawać błąd; trwające odpowiedzi/streamy mają do 120 s na dokończenie przed zatrzymaniem starego kontenera. Dwóch instancji naraz nie uruchamiamy (liczniki limitów i blokady kredytów są w pamięci procesu). Streamy dłuższe niż 120 s w momencie wdrożenia zostaną przerwane. |
| Automatyczny rollback | Kontener uznawany jest za „uruchomiony” dopiero po przejściu health-checka (`Notify=healthy`); jeśli nowy obraz nie wstanie, `podman auto-update` wraca do poprzedniego. |

> **Uwaga o weryfikacji:** logikę aplikacji (gniazdo unix, dziedziczenie gniazda od systemd, restart bez odrzucania połączeń) przetestowałem automatycznie. Plików Quadlet/systemd/nginx **nie mogłem uruchomić** (brak Podmana w moim środowisku), więc przy pierwszym wdrożeniu **wklejaj mi wyniki komend oznaczonych „→ wklej”** – poprawimy ewentualne różnice wersji na Ubuntu 26.04.

Oznaczenia: **[ADMIN]** = Twoje konto z sudo, **[apigate]** = konto projektu.

### 6.1 [ADMIN] Pakiety i firewall

```bash
sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw enable
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx podman uidmap dbus-user-session passt systemd-container curl git
podman --version          # → wklej (potrzebne >= 4.9; Quadlet)
uname -m                  # → wklej (x86_64 = OK; aarch64 = powiedz mi, dodam budowanie obrazu ARM)
ss -ltn | grep 18731 || echo "port 18731 wolny"     # jeśli zajęty, wybierz inny w apigate.socket (6.5)
```
Nie otwieraj żadnych innych portów. Jeśli `apt` nie zna pakietu `passt`, zainstaluj `slirp4netns`.

### 6.2 [ADMIN] Konto projektu i katalog gniazda

```bash
sudo adduser --disabled-password --gecos "" apigate
sudo loginctl enable-linger apigate                 # usługi użytkownika działają bez logowania
grep ^apigate: /etc/subuid /etc/subgid              # → wklej (muszą być wpisy)
sudo usermod -aG apigate www-data                   # nginx może połączyć się z gniazdem TEGO projektu (grupa apigate)
sudo install -d -o apigate -g apigate -m 750 /srv/apps/apigate /srv/apps/apigate/run
sudo systemctl restart nginx                        # restart (nie reload) – żeby nginx zobaczył nową grupę
```
Katalog domowy `/home/apigate` ma uprawnienia 750 – inni użytkownicy go nie czytają. Gniazdo leży osobno, w `/srv/apps/apigate/run`.

### 6.3 [ADMIN] Limity zasobów konta (1 CPU, 2 GB)

```bash
sudo mkdir -p /etc/systemd/system/user@.service.d
printf '[Service]\nDelegate=cpu cpuset io memory pids\n' | sudo tee /etc/systemd/system/user@.service.d/delegate.conf
sudo systemctl daemon-reload
sudo systemctl restart user@$(id -u apigate).service
sudo systemctl set-property user-$(id -u apigate).slice CPUQuota=100% MemoryMax=2G MemorySwapMax=0 TasksMax=1024
systemctl show user-$(id -u apigate).slice -p CPUQuotaPerSecUSec -p MemoryMax     # → wklej
cat /sys/fs/cgroup/user.slice/user-$(id -u apigate).slice/user@$(id -u apigate).service/cgroup.controllers    # powinno zawierać cpu memory pids
```
Dla kolejnego projektu powtórz z jego nazwą i własnymi limitami (suma limitów nie powinna przekraczać zasobów VPS: 4 CPU / 8 GB).

### 6.4 [ADMIN → apigate] Wejście na konto projektu

```bash
sudo machinectl shell apigate@
podman info | grep -iE "rootless|cgroupVersion|networkBackend"      # → wklej
systemctl --user status | head -3
```
Wszystkie komendy w 6.5–6.7 i 6.9–6.10 wykonujesz tu (jako `apigate`). Wyjście: `exit`.

### 6.5 [apigate] Pliki konfiguracyjne

Najpierw musi istnieć obraz (sekcja 5.4–5.5: push + zielone CI + pakiet publiczny).

```bash
git clone --depth 1 https://github.com/TWOJ_LOGIN/apigate.git ~/src
mkdir -p ~/.config/containers/systemd ~/.config/systemd/user
cp ~/src/deploy/podman/apigate.container ~/.config/containers/systemd/
cp ~/src/deploy/podman/apigate.socket    ~/.config/systemd/user/
sed -i 's#OWNER#twoj_login_malymi_literami#' ~/.config/containers/systemd/apigate.container
install -m 600 ~/src/deploy/podman/apigate.env.example ~/apigate.env

podman pull ghcr.io/twoj_login_malymi_literami/apigate:latest
podman run --rm --entrypoint python ghcr.io/twoj_login_malymi_literami/apigate:latest -c "from apigate.crypto import Vault; print(Vault.generate_master())"
nano ~/apigate.env          # wklej wynik za APIGATE_MASTER_KEY=   (resztę zostaw)
```
**Zapisz `APIGATE_MASTER_KEY` w menedżerze haseł** – bez niego zapisane klucze dostawców są nie do odzyskania.

### 6.6 [apigate] Start

```bash
systemctl --user daemon-reload
systemctl --user enable --now apigate.socket
systemctl --user start apigate.service
systemctl --user status apigate.service apigate.socket --no-pager       # → wklej
podman ps && podman healthcheck run apigate && echo healthy
ls -l /srv/apps/apigate/run/                                            # → wklej (srw-rw---- apigate apigate)
curl -s --unix-socket /srv/apps/apigate/run/apigate.sock http://localhost/healthz
curl -s http://127.0.0.1:18731/healthz                                  # drugie wejście (lokalne TCP)  → wklej oba wyniki
podman exec -it apigate apigate create-admin TWOJ_LOGIN
```
Jeśli usługa nie wstaje: `podman logs apigate` i `journalctl --user -u apigate -n 50 --no-pager` → wklej.

### 6.7 [ADMIN] nginx

```bash
sudo cp /home/apigate/src/deploy/nginx/apigate-proxy.conf /etc/nginx/snippets/apigate-proxy.conf
sudo cp /home/apigate/src/deploy/nginx/llm-apis.ans3r.eu.conf /etc/nginx/sites-available/llm-apis.ans3r.eu
sudo ln -s /etc/nginx/sites-available/llm-apis.ans3r.eu /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx                            # → wklej wynik nginx -t

# kontrola izolacji: nginx ma dostęp, obcy użytkownik nie
sudo -u www-data curl -s --unix-socket /srv/apps/apigate/run/apigate.sock http://localhost/healthz     # {"ok":true}
sudo -u nobody   curl -s --unix-socket /srv/apps/apigate/run/apigate.sock http://localhost/healthz     # Permission denied
sudo -u nobody   curl -s http://127.0.0.1:18731/healthz                                                  # {"ok":true}  (celowo otwarte lokalnie)
```
Gniazdo unix **musi** odmówić obcemu użytkownikowi (tylko nginx je ma). Lokalne TCP **ma** odpowiadać każdemu – to wejście dla Twoich innych projektów (`base_url="http://127.0.0.1:18731/v1"`, klucz `sk-gate-…`).

### 6.8 DNS i HTTPS

U rejestratora `ans3r.eu` dodaj rekord `A`: `llm-apis` → IP VPS (opcjonalnie `AAAA`). Gdy `nslookup llm-apis.ans3r.eu` zwraca IP VPS:
```bash
sudo certbot --nginx -d llm-apis.ans3r.eu          # e-mail, zgoda, przekierowanie HTTP→HTTPS
sudo nginx -t && sudo systemctl reload nginx
curl -s https://llm-apis.ans3r.eu/healthz
```
Odnawianie jest automatyczne (`systemctl list-timers | grep certbot`). Potem klucze dostawców dodaj w panelu `https://llm-apis.ans3r.eu` (*Providers & keys*), a limity Gemini komendą
`podman exec apigate apigate load-limits /opt/presets/gemini-free.toml` (jako `apigate`). Test z komputera: `python scripts\smoke.py --base https://llm-apis.ans3r.eu` (klucz `sk-gate-…` utworzony w panelu na serwerze).

### 6.9 [apigate] Automatyczne wdrażanie (CD)

```bash
mkdir -p ~/.config/systemd/user/podman-auto-update.timer.d
cat > ~/.config/systemd/user/podman-auto-update.timer.d/override.conf <<'EOT'
[Timer]
OnCalendar=
OnCalendar=*:0/2
RandomizedDelaySec=30
Persistent=true
EOT
systemctl --user daemon-reload
systemctl --user enable --now podman-auto-update.timer
systemctl --user list-timers | grep auto-update                       # → wklej
podman auto-update --dry-run                                          # → wklej (pokazuje, czy jest nowszy obraz)
```
Przepływ: `git push` do `main` → GitHub uruchamia testy → po sukcesie buduje i wysyła obraz → w ciągu ~2–3 minut serwer pobiera go i podmienia kontener (stary kończy trwające żądania, nowe czekają w kolejce na gnieździe).
Podgląd: `journalctl --user -u podman-auto-update.service -n 30 --no-pager`.

**Wersja przypięta / ręczny rollback:** w `~/.config/containers/systemd/apigate.container` zmień `:latest` na `:<sha commita>` (lista tagów: strona pakietu na GitHubie), usuń linię `AutoUpdate=registry`, a potem `systemctl --user daemon-reload && systemctl --user restart apigate.service`.

### 6.10 [apigate] Kopie zapasowe i logi

```bash
podman exec apigate apigate backup --keep 14             # spójna kopia bazy w /data/backups
mkdir -p ~/backups && podman cp apigate:/data/backups/. ~/backups/
podman logs -f apigate                                   # logi na żywo
podman stats --no-stream apigate                         # → zużycie; limit pamięci 2 GiB
```
Kopię przechowuj poza VPS i **osobno od master key** (kopia bazy zawiera tylko zaszyfrowane klucze dostawców).

### 6.11 Kolejny projekt na tym serwerze

Dla każdego projektu: nowy użytkownik (`adduser`, `enable-linger`), jego grupa dodana do `www-data` (`usermod -aG NAZWA www-data` + **`systemctl restart nginx`**), własny katalog `/srv/apps/NAZWA/run`, własne gniazdo `.socket` (z **własnym** portem `127.0.0.1:…`, np. 18732), własne limity (`set-property user-UID.slice`) i osobny blok `server { }` w nginx dla jego domeny. Żaden projekt nie dostaje cudzej grupy, więc nie dosięgnie cudzych gniazd.

---------------------------------------------------------------------------
## 7. Rozwiązywanie problemów

| Objaw | Przyczyna / rozwiązanie |
|---|---|
| `No such provider` | użyj: `openai`, `anthropic` (alias `claude`), `gemini`, `deepseek`, … |
| `This model ... is no longer available to new users` | Google wyłączył model; użyj nowszego (`apigate models gemini`), stary ukryj limitem `rpm = 0` |
| `high demand` / `Internal error` (502) | chwilowo po stronie Google; bramka ponawia 2×, potem zwraca błąd – spróbuj ponownie lub użyj aliasu z `fallback` na inny model |
| `429 ... cooling down` | po błędzie dostawcy klucz jest chwilowo odsunięty; restart serwera czyści blokadę |
| `402 insufficient_quota` | użytkownik nie ma kredytów (`apigate grant`) |
| certbot nie wystawia certyfikatu | DNS nie wskazuje na VPS albo porty 80/443 zamknięte |
| 502 Bad Gateway z nginx | usługa nie działa (`systemctl --user status apigate.service`) albo nginx nie ma dostępu: w `/var/log/nginx/error.log` `connect() ... (13: Permission denied)` → `sudo usermod -aG apigate www-data` i **`sudo systemctl restart nginx`**, sprawdź `ls -ld /srv/apps/apigate /srv/apps/apigate/run` (750, grupa apigate) |
| `systemctl --user` → `Failed to connect to bus` | wejdź przez `sudo machinectl shell apigate@` (nie `su`), upewnij się, że działa `loginctl enable-linger apigate` |
| Nowy obraz się nie wdraża | `podman auto-update --dry-run`; pakiet GHCR musi być publiczny; w pliku `.container` musi być `AutoUpdate=registry` i poprawny `Image=` |
| Kontener ignoruje `--cpus` | brak delegacji cgroup – krok 6.3 (limit konta `CPUQuota` i tak działa) |
| `Address already in use` przy starcie gniazda | port z `ListenStream=127.0.0.1:…` jest zajęty – zmień go w `apigate.socket`, `systemctl --user daemon-reload && systemctl --user restart apigate.socket` |
| Logowanie w panelu: błąd Origin/CSRF | `APIGATE_PUBLIC_ORIGIN` w `~/apigate.env` ≠ adres w przeglądarce; po zmianie `systemctl --user restart apigate.service` |
