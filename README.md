# Magiczna Walizka — bot Discord

Asynchroniczny bot kolekcjonerski dla Discorda. Spotkania uruchamiają się samoczynnie po aktywności na dozwolonych kanałach, a zgadywanie nazw odbywa się zwykłą wiadomością. Kolekcje, spotkania i ustawienia są zapisywane w SQLite.

## Wymagania i instalacja

Wymagany jest Python 3.12+ oraz aplikacja bota utworzona w Discord Developer Portal. Jeśli `py -3.12` zgłasza brak runtime'u, zainstaluj go raz poleceniem `py install 3.12`, po czym ponownie uruchom terminal.

1. W folderze projektu utwórz środowisko: `py -3.12 -m venv .venv`, następnie aktywuj je (`.venv\Scripts\Activate.ps1` w PowerShell) i uruchom `python -m pip install -r requirements.txt`.
2. Skopiuj `.env.example` do `.env` i wpisz `DISCORD_TOKEN`.
3. W panelu Discord Developer Portal włącz **Message Content Intent**. Zaproś bota z zakresami `bot` i `applications.commands`; nadaj mu dostęp do kanałów, odczyt historii, wysyłanie wiadomości, osadzanie linków i załączanie plików.
4. Uruchom `python bot.py`.

Przy pierwszym uruchomieniu bot utworzy `data/magic_suitcase.sqlite3` i zaimportuje `data/creatures.json`. Nie dodawaj pliku `.env` do repozytorium.

## Darmowy hosting 24/7 na Northflank

Projekt zawiera `Dockerfile` i można wdrożyć go jako pojedynczy kontener. Plan Northflank Sandbox ma limit darmowych zasobów, dlatego po wdrożeniu kontroluj użycie w panelu.

1. Umieść projekt w prywatnym repozytorium GitHub. Plik `.dockerignore` wyklucza `.env`, lokalne środowisko i bazę z obrazu.
2. W Northflank utwórz projekt i usługę typu **Combined Service**, połącz repozytorium i wybierz **Dockerfile** jako metodę budowania.
3. Dodaj zmienne środowiskowe usługi: `DISCORD_TOKEN` z tokenem bota oraz `DATABASE_PATH=/persistent/magic_suitcase.sqlite3`. Nie dodawaj tokenu do repozytorium.
4. W ustawieniach usługi dodaj trwały wolumin o rozmiarze 1 GB i zamontuj go pod `/persistent`. Pozostaw jedną replikę.
5. Wdróż usługę. Kontener uruchamia `python bot.py`; nie potrzebuje publicznego portu.

Trwały wolumin zachowuje bazę SQLite po przebudowie lub restarcie kontenera. Zasoby Sandbox są darmowe do określonego limitu; wolumin i obliczenia korzystają z tego limitu. Pilnuj zużycia i nie wybieraj planów płatnych.

## Konfiguracja

Wartości domyślne są w `.env.example`: przedział progów aktywności, długość spotkania oraz wagi rzadkości. Możesz opcjonalnie ustawić `GUILD_ID`, aby komendy slash zsynchronizowały się od razu na jednym serwerze testowym. Bez tego komendy są globalne i ich publikacja może potrwać.

Administrator z uprawnieniem **Zarządzanie serwerem** może użyć `/admin settings` do konfiguracji progów, czasu spotkania, włączenia mechaniki oraz spotkań równoległych. `/admin set-channel` ogranicza spotkania do podanego kanału; wywołanie bez kanału zezwala na wszystkie kanały tekstowe.

## Komendy graczy

- `/collection` — prywatna, stronicowana walizka i liczniki duplikatów.
- `/dex [rarity]` — bestiariusz z filtrem rzadkości; nieodkryte gatunki są ukryte.
- `/profile [member]` — statystyki kolekcjonera.
- `/leaderboard category` — ranking schwytanych, gatunków lub rzadkich stworzeń.
- `/creature name` — karta gatunku już posiadanego przez gracza.
- `/stats` — statystyki całej gry.

Komendy administracyjne: `/admin spawn`, `/admin add-creature`, `/admin remove-creature`, `/admin reload-creatures`, `/admin set-channel`, `/admin settings`.

## Dodawanie stworzeń

Najprościej dopisać rekord do `data/creatures.json`: `id` musi być unikatowym numerem, `name` nazwą do zgadywania, `aliases` listą dodatkowych nazw, `rarity` jedną z wartości `common`, `uncommon`, `rare`, `epic`, `legendary`, `mythical`, a `image` nazwą pliku w `assets/`. Każdy rekord wskazuje jedną stałą grafikę. Potem użyj `/admin reload-creatures` (lub zrestartuj bota). Alternatywnie użyj `/admin add-creature` i podaj aliasy rozdzielone znakiem `|`.

Seedowe nazwy i klasy rzadkości powstały na podstawie nazw plików w `assets/`; przed publicznym uruchomieniem warto poprawić ich pisownię, aliasy i rzadkość w JSON. Grafiki są używane bez wariantów wyglądu.

## Dane i bezpieczeństwo

SQLite zapisuje użytkowników, grupowane liczniki gatunków, konfigurację serwera i historię spotkań. Zmiana statusu spotkania i zapis schwytania odbywają się w jednej transakcji, więc tylko jedna poprawna odpowiedź może wygrać. Spotkania trwające podczas restartu są zamykane, aby nie można było łapać nieaktualnych spotkań. Wiadomości tego samego użytkownika w kanale są wliczane najwyżej raz na 2 sekundy; boty, webhooki, puste wiadomości i komendy są pomijane.

## Testy

Uruchom: `python -m unittest discover -s tests -v`.
