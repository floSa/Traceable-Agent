# Post-mortem migration infrastructure — mai 2026

La migration du 12 février a doublé la latence p95 de l'API publique :
180 ms avant, 410 ms après. Le p99 est passé de 400 ms à 1,2 s.

Le correctif partiel du 3 avril a ramené le p95 à 260 ms. Le p99 reste à 900 ms.
Les clients Entreprise sont les plus exposés : ils appellent l'API en batch,
donc ils prennent le p99 de plein fouet.

Aucun client PME n'a ouvert de ticket sur la latence.
