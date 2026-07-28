# Questions de référence — jeu d'évaluation

13 questions de difficulté croissante, avec le SQL attendu et **la réponse
réellement obtenue** sur ce jeu de données.

**À quoi ça sert.** Sans réponses connues, on ne peut pas savoir si un agent
text-to-SQL a raison — seulement s'il a l'air d'avoir raison. Ici, chaque réponse
ci-dessous a été produite en exécutant la requête sur les fichiers de `tables/`.
C'est donc un vrai jeu d'évaluation : posez la question en langage naturel à
l'agent, comparez son résultat à la référence.

**Reproductibilité.** Toutes les requêtes ont été exécutées avec DuckDB sur les
CSV livrés. La syntaxe est standard et passe aussi sur PostgreSQL. Les seules
divergences possibles sont signalées question par question.

---

## Q1 — facile · agrégat simple
> **Quel est le chiffre d'affaires total de 2025 ?**

```sql
SELECT ROUND(SUM(revenue), 2) AS ca_2025
FROM sales_daily
WHERE date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31';
```
**Réponse : 10 186 669,50 €**

---

## Q2 — facile · 1 jointure + tri
> **Quels sont les 5 magasins qui réalisent le plus de CA en 2025 ?**

```sql
SELECT st.store_name, ROUND(SUM(s.revenue), 2) AS ca
FROM sales_daily s
JOIN stores st ON st.store_id = s.store_id
WHERE s.date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
GROUP BY st.store_name
ORDER BY ca DESC
LIMIT 5;
```
| store_name | ca |
|---|---:|
| Canal Online | 2 064 988,99 |
| Paris | 1 041 046,77 |
| Lyon | 952 207,99 |
| Marseille | 950 336,13 |
| Toulouse | 898 606,91 |

> **Le piège :** `Canal Online` arrive premier. Un agent qui répond « Paris est le
> premier magasin » a oublié que le e-commerce est une ligne de `stores`.

---

## Q3 — facile · part relative (fonction fenêtre)
> **Comment se répartit le CA 2025 par univers produit ?**

```sql
SELECT p.commodity_group,
       ROUND(SUM(s.revenue), 2) AS ca,
       ROUND(100.0 * SUM(s.revenue) / SUM(SUM(s.revenue)) OVER (), 1) AS pct
FROM sales_daily s
JOIN products p ON p.sku_id = s.sku_id
WHERE s.date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
GROUP BY p.commodity_group
ORDER BY ca DESC;
```
| commodity_group | ca | pct |
|---|---:|---:|
| Chat | 3 790 391,44 | 37,2 |
| Chien | 3 620 136,70 | 35,5 |
| Hygiène & Soins | 792 494,12 | 7,8 |
| Oiseau | 579 384,09 | 5,7 |
| Rongeur | 555 424,33 | 5,5 |
| Aquariophilie | 477 955,28 | 4,7 |
| Accessoires & Jouets | 210 890,56 | 2,1 |
| Reptile | 159 992,98 | 1,6 |

---

## Q4 — moyen · évolution d'une part dans le temps
> **La part du e-commerce progresse-t-elle ?**

```sql
SELECT EXTRACT(YEAR FROM s.date) AS annee,
       ROUND(100.0 * SUM(CASE WHEN st.is_online = 1 THEN s.revenue ELSE 0 END)
             / SUM(s.revenue), 2) AS pct_online
FROM sales_daily s
JOIN stores st ON st.store_id = s.store_id
GROUP BY annee
ORDER BY annee;
```
| annee | pct_online |
|---|---:|
| 2021 | 13,34 |
| 2022 | 15,13 |
| 2023 | 16,94 |
| 2024 | 18,38 |
| 2025 | 20,27 |
| 2026 | 22,40 |

> Oui : +7 points en 4 ans. 2021 et 2026 sont des **années partielles**
> (l'historique va du 2021-07-01 au 2026-06-30) — un bon agent le signale.

---

## Q5 — moyen · jointure de deux faits de grains différents
> **Quel est le panier article moyen par type de magasin en 2025 ?**

```sql
WITH q AS (
  SELECT store_id, date, SUM(quantity) AS qty
  FROM sales_daily
  WHERE date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
  GROUP BY store_id, date
)
SELECT st.store_type,
       ROUND(SUM(q.qty) / SUM(t.nb_tickets), 2) AS panier_article
FROM q
JOIN traffic_daily t ON t.store_id = q.store_id AND t.date = q.date
JOIN stores st ON st.store_id = q.store_id
WHERE t.nb_tickets > 0
GROUP BY st.store_type
ORDER BY panier_article DESC;
```
| store_type | panier_article |
|---|---:|
| online | 2,76 |
| grand | 2,37 |
| moyen | 2,21 |
| petit | 2,06 |

> **Le piège :** il faut agréger `sales_daily` au grain magasin × jour **avant**
> de joindre `traffic_daily`, sinon `nb_tickets` est dupliqué 60 fois (une fois
> par SKU) et le panier est divisé par 60.

---

## Q6 — moyen · top N avec attributs
> **Quels sont les 10 SKU les plus vendus en CA en 2025 ?**

```sql
SELECT p.sku_id, p.sku_label, p.brand_type, ROUND(SUM(s.revenue), 2) AS ca
FROM sales_daily s
JOIN products p ON p.sku_id = s.sku_id
WHERE s.date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
GROUP BY p.sku_id, p.sku_label, p.brand_type
ORDER BY ca DESC
LIMIT 10;
```
Top 3 : `SKU001` Croquettes chien adulte 12 kg (1 438 611,18 €),
`SKU015` Croquettes chat stérilisé 10 kg (1 308 100,18 €),
`SKU019` Litière agglomérante 15 L (715 202,82 €).

---

## Q7 — moyen · saisonnalité hebdomadaire par canal
> **Quel jour de la semaine vend le mieux, en magasin et en ligne ?**

```sql
SELECT EXTRACT(ISODOW FROM s.date) AS jour_1lundi,
       ROUND(SUM(CASE WHEN st.is_online = 0 THEN s.revenue ELSE 0 END), 0) AS ca_magasin,
       ROUND(SUM(CASE WHEN st.is_online = 1 THEN s.revenue ELSE 0 END), 0) AS ca_online
FROM sales_daily s
JOIN stores st ON st.store_id = s.store_id
WHERE s.date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
GROUP BY jour_1lundi
ORDER BY jour_1lundi;
```
| jour (1 = lundi) | ca_magasin | ca_online |
|---|---:|---:|
| 1 | 1 013 691 | 323 099 |
| 2 | 1 112 964 | 305 394 |
| 3 | 1 363 225 | 296 519 |
| 4 | 1 068 460 | 276 851 |
| 5 | 1 319 803 | 286 649 |
| 6 | **1 820 157** | 248 285 |
| 7 | 423 381 | **328 192** |

> Deux signaux opposés : le **samedi** en magasin, le **dimanche** en ligne
> (beaucoup de magasins sont fermés le dimanche, le CA bascule sur le web).
> `EXTRACT(ISODOW)` donne 1 = lundi sur PostgreSQL comme sur DuckDB. Attention à
> `EXTRACT(DOW)`, qui donne 0 = dimanche.

---

## Q8 — moyen · lecture d'un flag métier
> **Quelle est l'ampleur des ruptures de stock en 2025 ?**

```sql
SELECT COUNT(*) AS lignes_rupture,
       ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM sales_daily
             WHERE date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'), 2) AS pct_lignes,
       ROUND(SUM(revenue), 2) AS ca_realise_en_rupture
FROM sales_daily
WHERE is_rupture = 1
  AND date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31';
```
**3 041 lignes (1,08 %), 42 066,45 € de CA réalisé malgré la rupture.**

> **Le piège :** ce n'est **pas** le CA perdu. Les ventes sont censurées : on ne
> connaît pas la demande qu'on aurait servie sans rupture. Un agent qui répond
> « la rupture a coûté 42 066 € » se trompe de sens.

---

## Q9 — moyen · sous-population et cold start
> **Quels produits ont été lancés en cours d'historique, et quand ont-ils commencé à vendre ?**

```sql
SELECT p.sku_id, p.sku_label, p.launch_date, MIN(s.date) AS premiere_ligne,
       ROUND(SUM(s.revenue), 2) AS ca_depuis_lancement
FROM products p
JOIN sales_daily s ON s.sku_id = p.sku_id
WHERE p.launch_date IS NOT NULL
GROUP BY p.sku_id, p.sku_label, p.launch_date
ORDER BY p.launch_date;
```
| sku_id | sku_label | launch_date | premiere_ligne | ca |
|---|---|---|---|---:|
| SKU003 | Croquettes chien senior light 8 kg | 2023-03-15 | 2023-03-15 | 1 225 919,91 |
| SKU026 | Fontaine à eau 2 L | 2024-09-01 | 2024-09-01 | 174 948,04 |
| SKU049 | Lampe UVB 100 W | 2025-02-15 | 2025-02-15 | 81 239,64 |
| SKU060 | Coffret cadeau Noël animaux | 2025-10-01 | 2025-10-01 | 42 420,22 |

> `premiere_ligne` colle exactement à `launch_date` : aucune ligne n'existe avant
> le lancement (ce n'est pas un zéro, c'est une absence).

---

## Q10 — difficile · la question qui justifie le modèle
> **Quel est l'uplift des campagnes de type « produits » sur les SKU ciblés,
> comparé aux 4 semaines précédant la campagne ?**

```sql
WITH p AS (
  SELECT promo_id, campaign_name, discount_rate, date_start, date_end
  FROM promo_calendar
  WHERE promo_type = 'produits' AND date_end <= DATE '2026-06-30'
),
pendant AS (
  SELECT p.promo_id, AVG(s.quantity) AS qty_pendant
  FROM p
  JOIN promo_scope ps ON ps.promo_id = p.promo_id
  JOIN sales_daily s ON s.sku_id = ps.sku_id
                    AND s.date BETWEEN p.date_start AND p.date_end
  GROUP BY p.promo_id
),
avant AS (
  SELECT p.promo_id, AVG(s.quantity) AS qty_avant
  FROM p
  JOIN promo_scope ps ON ps.promo_id = p.promo_id
  JOIN sales_daily s ON s.sku_id = ps.sku_id
                    AND s.date >= p.date_start - INTERVAL '28' DAY
                    AND s.date <  p.date_start
  GROUP BY p.promo_id
)
SELECT p.campaign_name, p.discount_rate,
       ROUND(avant.qty_avant, 3)   AS qty_avant,
       ROUND(pendant.qty_pendant, 3) AS qty_pendant,
       ROUND(pendant.qty_pendant / avant.qty_avant, 2) AS uplift
FROM p
JOIN pendant ON pendant.promo_id = p.promo_id
JOIN avant   ON avant.promo_id   = p.promo_id
ORDER BY uplift DESC
LIMIT 8;
```
| campaign_name | remise | qty_avant | qty_pendant | uplift |
|---|---:|---:|---:|---:|
| Black Friday 2025 | 0,30 | 1,431 | 2,803 | **×1,96** |
| Black Friday 2023 | 0,30 | 2,290 | 4,297 | ×1,88 |
| Black Friday 2022 | 0,30 | 1,701 | 2,994 | ×1,76 |
| Black Friday 2024 | 0,30 | 1,695 | 2,725 | ×1,61 |
| Black Friday 2021 | 0,30 | 0,895 | 1,367 | ×1,53 |
| Cat Days 2024 | 0,20 | 1,929 | 2,930 | ×1,52 |
| Cat Days 2026 | 0,20 | 4,240 | 6,392 | ×1,51 |
| Été sans parasites 2023 | 0,15 | 1,857 | 2,786 | ×1,50 |

> **La vérification qui vaut de l'or.** L'uplift théorique injecté dans les
> données est `1 + 2,2 × remise`, soit **×1,66 pour une remise de 30 %**. Les
> valeurs mesurées (×1,53 à ×1,96) encadrent bien cette cible — le reste de
> l'écart vient de la saisonnalité (Black Friday tombe en novembre) et du mix SKU.
> C'est exactement le genre de question où l'on peut trancher si l'agent a raison.
>
> Les campagnes classées ensuite par remise décroissante (30 % → 20 % → 15 %)
> confirment que l'uplift suit la profondeur de remise.
>
> `INTERVAL '28' DAY` fonctionne sur PostgreSQL et DuckDB.

---

## Q11 — difficile · effet météo
> **La chaleur fait-elle baisser la fréquentation des magasins physiques en été ?**

```sql
SELECT CASE WHEN w.temp_anomaly > 2 THEN 'anomalie chaude (> +2 °C)'
            ELSE 'temps normal' END AS regime,
       COUNT(*) AS nb_jours_magasin,
       ROUND(AVG(t.nb_tickets), 1) AS tickets_moyens
FROM traffic_daily t
JOIN stores  st ON st.store_id = t.store_id
JOIN weather w  ON w.store_id = t.store_id AND w.date = t.date
WHERE st.is_online = 0 AND t.nb_tickets > 0
  AND EXTRACT(MONTH FROM t.date) BETWEEN 6 AND 8
GROUP BY regime
ORDER BY regime;
```
| regime | nb_jours_magasin | tickets_moyens |
|---|---:|---:|
| anomalie chaude (> +2 °C) | 1 163 | **51,2** |
| temps normal | 3 901 | 54,5 |

> **Oui : −6 % de tickets** les jours nettement plus chauds que la normale.
> **Le piège :** il faut raisonner sur `temp_anomaly` (écart à la normale) et non
> sur `temp_mean_c` — sinon on compare l'été à l'hiver et on mesure la
> saisonnalité, pas la canicule.

---

## Q12 — difficile · fonction fenêtre sur le grain horaire
> **Quelle est l'heure de pointe de chaque magasin physique en 2025 ?**

```sql
WITH h AS (
  SELECT sh.store_id, sh.hour, SUM(sh.ca) AS ca,
         ROW_NUMBER() OVER (PARTITION BY sh.store_id ORDER BY SUM(sh.ca) DESC) AS rg
  FROM sales_hourly sh
  JOIN stores st ON st.store_id = sh.store_id
  WHERE st.is_online = 0
    AND sh.date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
  GROUP BY sh.store_id, sh.hour
)
SELECT st.store_name, h.hour AS heure_de_pointe, ROUND(h.ca, 0) AS ca_sur_cette_heure
FROM h JOIN stores st ON st.store_id = h.store_id
WHERE h.rg = 1
ORDER BY ca_sur_cette_heure DESC;
```
Résultat : **11 h** pour Paris, Lyon, Marseille, Toulouse, Nantes et Strasbourg ;
**18 h** pour Bordeaux, Rennes, Angers, Dijon, Colmar et Brive.

---

## Q13 — contrôle · cohérence entre deux grains
> **Le CA horaire recompose-t-il bien le CA journalier ?**

```sql
WITH d AS (SELECT store_id, date, SUM(revenue) AS ca_j FROM sales_daily  GROUP BY store_id, date),
     h AS (SELECT store_id, date, SUM(ca)      AS ca_h FROM sales_hourly GROUP BY store_id, date)
SELECT ROUND(MAX(ABS(d.ca_j - h.ca_h)), 4) AS ecart_max_eur
FROM d JOIN h ON h.store_id = d.store_id AND h.date = d.date;
```
**Écart maximum : 0,05 €** sur 1,36 M de lignes (arrondis au centime). Les deux
grains sont cohérents par construction.

---

## Idées de questions plus dures

Sans réponse de référence — à utiliser pour pousser l'agent dans ses retranchements :

- **Report d'achat** : les SKU en promo `produits` vendent-ils moins que d'habitude
  la semaine **suivant** la fin de la campagne ? (effet injecté : ×0,88 sur 7 jours)
- **Effet décalé** : pour les campagnes `influence`, où se situe le pic de ventes
  par rapport à `date_start` ? (injecté : ~J+10, étalé sur 28 jours)
- **Panier vs volume** : les campagnes `seuils` augmentent-elles le nombre
  d'articles par ticket sans augmenter le nombre de tickets ?
- **Ciseau prix/volume** : sur 5 ans, quelle part de la croissance du CA vient de
  l'inflation (`unit_price`) et quelle part du volume (`quantity`) ?
- **Cannibalisation** : le lancement de `SKU003` (croquettes chien senior) a-t-il
  fait baisser les ventes des autres croquettes chien ?
- **Saisonnalité croisée** : quel univers a la saisonnalité la plus marquée,
  mesurée par l'écart-type des indices mensuels ?
