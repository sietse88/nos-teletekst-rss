#!/usr/bin/env bash
# Werkt de feed elk kwartier bij, LOOPTIJD_MINUTEN lang (standaard 330 = 5,5 uur).
#
# GitHub voert de geplande runs maar een paar keer per dag uit, met gaten van
# vijf à acht uur. Door elke run lang door te laten lopen en tussentijds op te
# halen, wordt toch het grootste deel van de dag gekeken.
#
# Er wordt alleen gecommit als de artikelen veranderen (nieuw artikel, andere
# tekst, dagbestand), niet als alleen de tijdstempels "laatst gezien" of
# lastBuildDate verschuiven. Aan het eind volgt één commit om die tijdstempels
# te bewaren voor de volgende run.
set -u

LOOPTIJD_MINUTEN="${LOOPTIJD_MINUTEN:-330}"
INTERVAL_MINUTEN="${INTERVAL_MINUTEN:-15}"
BESTANDEN="docs/feed.xml seen.json archive.json dagen/"

# Wat de lezer ziet: de feed zonder bouwtijd, plus de dagbestanden.
inhoud() {
  grep -v '<lastBuildDate>' docs/feed.xml 2>/dev/null
  cat dagen/* 2>/dev/null
}

klaarzetten() {
  # Het weekoverzicht is vervangen door dagbestanden (okt 2026). Hier en niet in
  # een eigen commit weggehaald, zodat een run met de oude code die nog loopt
  # zijn weekbestand kan blijven pushen zonder dat zijn rebase vastloopt.
  git rm -r -q --ignore-unmatch weken
  git add $BESTANDEN
  if ! git diff --staged --quiet; then
    git commit -q -m "Feed bijgewerkt ($(date -u +'%Y-%m-%d %H:%M UTC'))"
  fi
}

commit_en_push() {
  klaarzetten
  # Ook een eerder mislukte push wordt hier alsnog geprobeerd.
  for poging in 1 2 3; do
    if ! git fetch -q origin main; then
      sleep 10
      continue
    fi
    if ! git rebase -q origin/main; then
      # Botsing in de bestanden van de robot: er staat iets nieuwers op GitHub
      # dan waar deze run op begon. Opnieuw rebasen botst dan elke keer weer, en
      # de run kon 5,5 uur lang niets kwijt (13 okt 2026). Dus: de nieuwste
      # versie nemen en daarop opnieuw ophalen. Wat alleen deze run sinds zijn
      # laatste push zag en nu niet meer te zien is, gaat daarbij verloren.
      echo "Botsing met GitHub; opnieuw ophalen op de nieuwste versie." >&2
      git rebase --abort 2>/dev/null
      git reset -q --hard origin/main
      python3 nos_teletekst.py || echo "Ophalen mislukt; volgende keer opnieuw." >&2
      klaarzetten
    fi
    if [ "$(git rev-list --count origin/main..HEAD)" = 0 ]; then
      return 0
    fi
    if git push -q origin HEAD:main; then
      echo "Gepusht."
      return 0
    fi
    sleep 10
  done
  echo "Pushen mislukt; volgende keer opnieuw." >&2
}

einde=$(( $(date +%s) + LOOPTIJD_MINUTEN * 60 ))
vorige=$(mktemp)
inhoud > "$vorige"
while :; do
  echo "=== $(date -u +'%H:%M UTC')"
  python3 nos_teletekst.py || echo "Ophalen mislukt; volgende keer opnieuw." >&2
  nu=$(mktemp)
  inhoud > "$nu"
  if cmp -s "$vorige" "$nu"; then
    rm "$nu"
    echo "Geen veranderingen in de artikelen."
  else
    mv "$nu" "$vorige"
    commit_en_push
  fi
  [ $(( $(date +%s) + INTERVAL_MINUTEN * 60 )) -lt "$einde" ] || break
  sleep $(( INTERVAL_MINUTEN * 60 ))
done
commit_en_push
rm -f "$vorige"
