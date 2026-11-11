# apigate – tutorial krok po kroku (Windows lokalnie → GitHub → VPS)

Wszystkie komendy `apigate ...` uruchamiasz w PowerShellu w folderze projektu
(folder z `pyproject.toml`). Dane (baza, `master.key`) są w `.\data`.

---------------------------------------------------------------------------
## 0. Zanim zaczniesz: zatrzymaj serwer i uruchom go ponownie po zmianach

Po aktualizacji plików zrestartuj serwer (Ctrl+C w oknie z `apigate serve`, potem `apigate serve`).
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
python scripts\smoke.py --base http://127.0.0.1:8080
```

Skrypt sam wybiera tani model, który odpowiada (pomija te, które zwracają błąd, i pisze `[skip]`).
Konkretny model: `--model gemini/gemini-3.5-flash-lite`. Wszystkie linie powinny mieć `[OK ]`.

Ręczny test przez `curl` (PowerShell: `curl.exe`):

```powershell
curl.exe http://127.0.0.1:8080/v1/chat/completions -H "Authorization: Bearer $env:APIGATE_SMOKE_KEY" -H "Content-Type: application/json" -d "{\"model\":\"gemini/gemma-4-31b-it\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}"
```

Typowe odpowiedzi: `502 Upstream error` = błąd po stronie dostawcy (bramka próbowała kilka razy),
`429` = limit klucza / chwilowa blokada po błędzie, `404` = brak takiego modelu na liście.

---------------------------------------------------------------------------
## 4. Użytkownicy, klucze API i kredyty

W panelu (http://127.0.0.1:8080, zaloguj się jako admin):
* **Users** – tworzenie kont, dodawanie kredytów, blokowanie,
* **API keys** – tworzenie kluczy `sk-gate-...` dla użytkownika (pokazywane **tylko raz**),
* **Routing & prices** – strategia doboru kluczy (round_robin / random / fill do 70% / least_loaded),
  ceny (kredyty za 1M tokenów), aliasy modeli, rejestracja (`allow_signup`).

Z linii komend: `apigate grant NAZWA 100` dodaje 100 kredytów. Admin utworzony przez `create-admin` ma kredyty bez limitu.
Zwykły użytkownik bez kredytów dostaje 402 – tak jest celowo, dopóki nie dodasz mechanizmu zakupu.
**Na serwerze zostaw `allow_signup = false`.**

---------------------------------------------------------------------------
## 5. GitHub – repozytorium PUBLICZNE (część A, robisz na swoim komputerze)

Repo publiczne = każdy zobaczy **cały kod i całą historię commitów na zawsze**. Dlatego sekretów nie wolno wrzucić nawet raz
(usunięcie pliku w kolejnym commicie nic nie daje – wtedy trzeba unieważnić klucz). `.gitignore` już blokuje: `data\`, `master.key`, `*.db`, `.env`, `config.toml`, `keys*.txt`, `ai-credits.txt`, `*.pem`.

**5.1 Licencja.** Otwórz `LICENSE` i zamień `apigate authors` na swoje imię/nazwisko lub pseudonim (MIT – każdy może używać, także komercyjnie).

**5.2 CI (testy na GitHubie).**
```powershell
mkdir .github\workflows
copy deploy\ci.yml .github\workflows\ci.yml
```

**5.3 Tożsamość w commitach.** W publicznym repo e-mail z commita jest widoczny. Użyj adresu „noreply” z GitHuba
(GitHub → Settings → Emails → „Keep my email addresses private”, tam jest adres `ID+login@users.noreply.github.com`):
```powershell
git config --global user.name "Twoje Imię"
git config --global user.email "ID+login@users.noreply.github.com"
```

**5.4 Lokalne repo i kontrola przed pierwszym commitem.**
```powershell
git init -b main
git add .
git ls-files
```
Przejrzyj listę. **Nie może** w niej być: `data/…`, `master.key`, `.env`, `*.db`, `config.toml`, żadnych plików z kluczami.
Powinny być m.in.: `apigate/…`, `tests/…`, `docs/…`, `presets/…`, `deploy/…`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `LICENSE`, `README.md`, `.github/workflows/ci.yml`.

Skan sekretów (szuka kształtu kluczy OpenAI/Google/bramki):
```powershell
git grep -nE "sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,}|sk-gate-[A-Za-z0-9]{20,}"
```
Dozwolone trafienia: tylko `apigate/crypto.py` (wzorzec redaktora) i `tests/test_units.py` (fikcyjne dane). **Wklej mi wynik, jeśli jest coś innego.**

**5.5 Commit.**
```powershell
git commit -m "apigate: initial public release"
```

**5.6 Utwórz repo na github.com** → New repository: nazwa `apigate`, **Public**, **bez** README, .gitignore i licencji (mamy własne). Potem:
```powershell
git remote add origin https://github.com/TWOJ_LOGIN/apigate.git
git push -u origin main
```
(przy pierwszym pushu otworzy się okno logowania GitHub; alternatywa: `gh auth login`).

**5.7 Ustawienia repo (GitHub → Settings → Security):** włącz *Private vulnerability reporting*, *Secret scanning* + *Push protection*, *Dependabot alerts*.
W zakładce *Actions* sprawdź, czy CI przeszło na zielono (testy + build Dockera).

**5.8 Użycie w innych projektach:**
```bash
pip install "git+https://github.com/TWOJ_LOGIN/apigate.git"        # jako biblioteka (Gateway in-process)
```
albo jako zwykły klient OpenAI: `base_url="https://llm-apis.ans3r.eu/v1"`, `api_key="sk-gate-..."`.

---------------------------------------------------------------------------
## 6. VPS (Ubuntu 26.04): jeden kontener apigate + nginx na hoście (część B)

Architektura: **nginx zainstalowany na VPS (nie w Dockerze)** obsługuje HTTPS i kieruje domeny do kontenerów.
Kontener apigate nasłuchuje tylko na `127.0.0.1:18080` (z internetu niedostępny), ma limit **1 CPU / 2 GB RAM** (`docker-compose.yml`).
W kontenerze działa jeden proces, więc nic nie trzeba „balansować” wewnątrz – 2 GB to twardy sufit (przekroczenie = restart kontenera, reszta VPS bezpieczna).

**6.1 Zabezpieczenie VPS (jeśli jeszcze nie zrobione).** Logowanie kluczem SSH, bez roota, firewall:
```bash
sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw enable
sudo apt update && sudo apt install -y unattended-upgrades fail2ban
```
Port 18080 **nie** otwieraj w firewallu.

**6.2 Oprogramowanie.**
```bash
sudo apt install -y nginx certbot python3-certbot-nginx git
sudo apt install -y docker.io docker-compose-v2      # pakiety Ubuntu; jeśli masz już Dockera, pomiń
sudo usermod -aG docker $USER && newgrp docker
docker compose version && nginx -v
```

**6.3 DNS (u rejestratora domeny `ans3r.eu`).** Rekord `A`: nazwa `llm-apis` → IP VPS (opcjonalnie `AAAA` → IPv6 VPS).
Sprawdź z VPS lub komputera: `nslookup llm-apis.ans3r.eu` – ma zwrócić IP VPS (propagacja do kilkunastu minut).
Bez tego krok 6.5 (certyfikat) się nie uda.

**6.4 Kod i konfiguracja.**
```bash
git clone https://github.com/TWOJ_LOGIN/apigate.git && cd apigate
cp .env.example .env
ss -ltn | grep 18080 || echo "port wolny"        # jeśli zajęty – zmień APIGATE_HOST_PORT w .env ORAZ w deploy/nginx/apigate-proxy.conf
docker compose build
docker compose run --rm --no-deps --entrypoint python apigate -c "from apigate.crypto import Vault; print(Vault.generate_master())"
nano .env          # wklej wynik za APIGATE_MASTER_KEY=  (DOMAIN zostaw: llm-apis.ans3r.eu)
chmod 600 .env
```
**Zapisz `APIGATE_MASTER_KEY` w menedżerze haseł** (bez niego zaszyfrowane klucze dostawców są nie do odzyskania).

**6.5 Start kontenera.**
```bash
docker compose up -d
docker compose ps                                   # status: healthy
curl -s http://127.0.0.1:18080/healthz              # {"ok":true}
docker compose exec apigate apigate create-admin TWOJ_LOGIN
```

**6.6 nginx + HTTPS.**
```bash
sudo cp deploy/nginx/apigate-proxy.conf /etc/nginx/snippets/apigate-proxy.conf
sudo cp deploy/nginx/llm-apis.ans3r.eu.conf /etc/nginx/sites-available/llm-apis.ans3r.eu
sudo ln -s /etc/nginx/sites-available/llm-apis.ans3r.eu /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d llm-apis.ans3r.eu           # podaj e-mail, zgódź się na przekierowanie HTTP→HTTPS
sudo nginx -t && sudo systemctl reload nginx
```
Odnowienie certyfikatu jest automatyczne (`systemctl list-timers | grep certbot`).
Po certbocie otwórz plik `/etc/nginx/sites-available/llm-apis.ans3r.eu` i upewnij się, że blok `listen 443 ssl` zawiera te same `location` co blok z portu 80 (certbot kopiuje je automatycznie).

**6.7 Klucze dostawców i limity.** Najprościej w panelu `https://llm-apis.ans3r.eu` → *Providers & keys → Add key*, a limity: *Limits*. Albo z linii komend:
```bash
docker compose exec -T apigate apigate add-key gemini --label main --stdin      # wklej klucz, Enter, Ctrl+D
docker compose exec -T apigate apigate add-key openai --label main --stdin
docker compose exec -T apigate apigate add-key anthropic --label main --stdin
docker compose exec -T apigate apigate add-key deepseek --label main --stdin
docker compose exec apigate apigate load-limits /opt/presets/gemini-free.toml
docker compose exec apigate apigate models gemini
```

**6.8 Test z własnego komputera.**
```powershell
$env:APIGATE_SMOKE_KEY = "sk-gate-..."     # nowy klucz utworzony w panelu na serwerze
python scripts\smoke.py --base https://llm-apis.ans3r.eu
```
Wszystko ma być `[OK ]`, łącznie z `HSTS`. Streaming przez nginx sprawdza linia `chat (stream)`.

**6.9 Kontrola limitów zasobów.**
```bash
docker stats --no-stream apigate        # kolumna MEM LIMIT = 2GiB
docker inspect apigate --format '{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}}'     # 1000000000 2147483648
```

**6.10 Aktualizacja i backup.**
```bash
cd ~/apigate && git pull && docker compose up -d --build
docker run --rm -v apigate_apigate-data:/data -v $PWD:/b alpine tar czf /b/apigate-backup-$(date +%F).tgz -C /data .
```
(nazwa wolumenu: `docker volume ls`; kopię przechowuj poza VPS i **osobno od master key**).

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
| 502 Bad Gateway z nginx | kontener nie działa (`docker compose ps`) albo port w `apigate-proxy.conf` ≠ `APIGATE_HOST_PORT` |
| Logowanie w panelu: błąd Origin/CSRF | `DOMAIN` w `.env` ≠ adres w przeglądarce; po zmianie `docker compose up -d` |
