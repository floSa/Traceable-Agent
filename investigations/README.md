# Registre des investigations

Une ligne par enquête, ajoutée par `inv new`. La vérité reste dans chaque
`plan.md` : ce fichier n'est qu'un index humain, jamais relu par le moteur.

Chaque dossier `<YYYY-MM-DD_slug>/` contient :

| chemin | rôle | relu par un LLM ? |
|---|---|---|
| `plan.md` | **la seule mémoire du système** (frontmatter YAML = source de vérité) | oui, filtré par `build_context` |
| `artifacts/` | traces auditables des appels d'outils (le SQL, la commande) | **jamais** — write-only |
| `results/` | snapshots datés immuables (synthèses, rapports) | non |

| date | slug | titre | toolpack | statut à la création |
|---|---|---|---|---|
| 2026-07-25 | [`pourquoi-le-churn-des-clients-entreprise`](2026-07-25_pourquoi-le-churn-des-clients-entreprise/plan.md) | Analyse des causes de l'augmentation du churn des clients Entreprise au S1 2026 | files | ready |
| 2026-07-25 | [`pourquoi-le-churn-des-clients-entreprise`](2026-07-25_pourquoi-le-churn-des-clients-entreprise/plan.md) | Analyse des causes de l'augmentation du churn des clients Entreprise au S1 2026 | files | ready |
| 2026-07-25 | [`pourquoi-le-churn-des-clients-entreprise`](2026-07-25_pourquoi-le-churn-des-clients-entreprise/plan.md) | Analyse des causes de l'augmentation du churn des clients Entreprise au S1 2026 | files | ready |
