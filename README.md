# github-sync

Une GitHub Action qui fait chaque jour **1 à 3 petits commits utiles** sur mes
projets data-engineering. Elle tourne gratuitement sur GitHub Actions, avec
l'IA gratuite GitHub Models.

## Ce que fait chaque commit

| Fichier | Changement | Contrôle de sécurité |
|---|---|---|
| `.py` | ajoute des docstrings manquantes | l'AST doit être identique hors docstrings, donc le code ne peut pas changer |
| `.sql` | ajoute des commentaires `--` | le SQL doit être identique hors commentaires |
| `README.md` | une petite amélioration (typo, section) | diff limité |

Un changement est rejeté si son diff dépasse `max_changed_lines` (40) ou si une
ligne dépasse le `line-length` du `pyproject.toml`. Les messages suivent le
format conventionnel : `docs(clean): add docstrings to clean helpers`.

Les fichiers modifiés récemment sont notés dans `history.jsonl` et ne sont pas
repris tout de suite.

## Installation (5 min)

1. **Créer le repo** `github-sync` sur GitHub, puis pousser ce dossier :
   ```bash
   git init && git add . && git commit -m "feat: daily commit bot"
   git branch -M main
   git remote add origin https://github.com/AymanMady/github-sync.git
   git push -u origin main
   ```
2. **Créer un token** : GitHub → Settings → Developer settings →
   *Fine-grained tokens* → Generate new token
   - Repository access : *Only select repositories* → tes 6 repos data-engineering
   - Permissions : **Contents → Read and write**
3. **Ajouter le secret** : dans le repo `github-sync` → Settings → Secrets and
   variables → Actions → *New repository secret* : nom `GH_PAT`, valeur = le token.
4. **Tester** : onglet Actions → *daily-commits* → *Run workflow* avec
   "Dry run" coché. Les diffs proposés s'affichent dans les logs, rien n'est poussé.
5. Ensuite, la tâche planifiée tourne seule chaque jour à 08:23 UTC.

## Configuration (`config.json`)

- `repos` : les repos ciblés
- `min_commits` / `max_commits` : nombre de commits par jour (tirage aléatoire)
- `model` : modèle GitHub Models (`openai/gpt-4.1-mini` par défaut)
- `author_email` : ton e-mail *noreply* GitHub, pour que les commits comptent
  dans ton graphe de contributions

## Test en local

```bash
DRY_RUN=1 MODELS_TOKEN=$(gh auth token) python3 sync.py
```

## Pourquoi pas Vercel ?

Les fonctions Vercel n'ont pas de `git` utilisable, leur système de fichiers est
temporaire, et le cron gratuit est limité. GitHub Actions est gratuit, a déjà
`git` et l'accès à tes repos : c'est l'outil adapté.

## Notes

- Une fois tous les fichiers documentés, le bot fera moins de commits, voire
  aucun. Ajoute des projets dans `repos` pour continuer.
- GitHub met en pause les crons d'un repo inactif depuis 60 jours. Le commit
  quotidien de `history.jsonl` garde le repo actif.
