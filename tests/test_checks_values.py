"""Etape 0 — le verificateur chiffre.

Repris de `tests/unit/test_live_scenarios.py` du projet `data-analyst-agent`,
adapte au contrat decouple de `engine/checks/values.py`.

Ces tests verifient surtout ce que le verificateur doit **REJETER** : un
verificateur trop tolerant est pire que pas de verificateur du tout, il
transforme une reponse fausse en succes vert.
"""

from __future__ import annotations

import pytest

from engine.checks.values import (
    decimals_of,
    matches_in_prose,
    matches_in_table,
    missing_values,
    numbers_in,
)


def table(columns: list[str], rows: list[list]) -> dict:
    return {"columns": columns, "rows": rows}


# ---- lecture des nombres -------------------------------------------------


def test_numbers_in_rend_la_precision_affichee() -> None:
    assert numbers_in("Taux : 62,96 %") == [(62.96, 2)]
    assert numbers_in("environ 63 %") == [(63.0, 0)]


def test_numbers_in_accepte_les_separateurs_de_milliers() -> None:
    assert numbers_in("total de 1 234 lignes")[0][0] == 1234.0


def test_numbers_in_ignore_ce_qui_n_est_pas_un_nombre() -> None:
    assert numbers_in("aucun chiffre ici") == []
    assert numbers_in("") == []


def test_decimals_of() -> None:
    assert decimals_of(62.96) == 2
    assert decimals_of(891) == 0
    assert decimals_of(1e-13) == 12  # notation scientifique : on ne tronque pas


# ---- valeurs justes acceptees --------------------------------------------


def test_valeur_juste_acceptee() -> None:
    assert missing_values([60], prose="Il y a 60 SKU.") == []


def test_format_francais_accepte() -> None:
    assert missing_values([62.96], prose="Taux : 62,96 %") == []


def test_arrondi_a_la_precision_affichee_accepte() -> None:
    """« environ 63 % » est un arrondi juste de 62,96 : on juge la valeur, pas la forme."""
    assert missing_values([62.96], prose="Taux : environ 63 %") == []
    assert missing_values([5.8433], prose="environ 5,84 unites") == []


def test_valeur_cherchee_aussi_dans_le_tableau() -> None:
    """Le chiffre vit souvent dans le tableau, pas dans la phrase de synthese."""
    t = table(["sex", "n"], [["female", 314], ["male", 577]])
    assert missing_values([314, 577], prose="voir tableau", tables=[t]) == []


def test_valeur_pleine_precision_acceptee_face_a_une_reference_arrondie() -> None:
    """Le tableau rend 62.96296..., la reference notee a la main est 62,96.
    C'est la MEME valeur — la rejeter accuserait l'agent a tort."""
    t = table(["label", "rate"], [["1re classe", 62.96296296296296]])
    assert missing_values([62.96], prose="voir tableau", tables=[t]) == []


def test_chaine_attendue_presente_acceptee() -> None:
    assert missing_values(["1re classe"], prose="La 1re classe...") == []


def test_chaine_attendue_trouvee_dans_le_tableau() -> None:
    t = table(["jour", "ca"], [["samedi", 1820157]])
    assert missing_values(["samedi"], prose="voir tableau", tables=[t]) == []


# ---- valeurs fausses rejetees (le coeur du sujet) ------------------------


@pytest.mark.parametrize("faux", [59, 61, 600])
def test_valeur_fausse_rejetee(faux: int) -> None:
    """Un comptage faux ne doit jamais passer pour un arrondi."""
    assert missing_values([60], prose=f"Il y a {faux} SKU."), f"{faux} accepte a la place de 60"


def test_troncature_rejetee() -> None:
    """62,9 n'est pas un arrondi de 62,96 — c'est une valeur differente."""
    assert missing_values([62.96], prose="Taux : 62.9 %")


def test_chaine_attendue_absente_rejetee() -> None:
    assert missing_values(["1re classe"], prose="reparti par classe")


def test_valeur_pleine_precision_mais_fausse_reste_rejetee() -> None:
    t = table(["label", "rate"], [["1re classe", 59.11111111]])
    assert missing_values([62.96], prose="voir tableau", tables=[t])


def test_pas_de_faux_positif_par_coincidence_dans_un_grand_tableau() -> None:
    """Un tableau porte des milliers de nombres. Un identifiant valant 97 ne doit
    PAS valider un taux attendu de 96,81 : un tableau n'arrondit pas."""
    t = table(["passenger_id", "age", "fare"], [[97, 38.0, 71.2833], [98, 35.0, 53.1]])
    assert missing_values(
        [(96.81, 0.9681)], prose="94 lignes retournees", tables=[t]
    ), "97 accepte comme 96,81 : le verificateur ment"


def test_valeur_juste_dans_un_tableau_reste_acceptee() -> None:
    """La regle stricte ne doit pas rejeter une vraie valeur pleine precision."""
    t = table(["label", "rate"], [["1re classe", 96.8085106382979]])
    assert missing_values([96.81], prose="voir tableau", tables=[t]) == []


def test_arrondi_trop_grossier_ne_prouve_rien() -> None:
    """Le « 1 » de « 1re classe » ne doit pas valider un taux attendu de 0,9681 :
    un arrondi a +/-0,5 sur une valeur de 0,97 ne discrimine plus rien."""
    prose = "Pour les femmes de 1re classe, precisez ce que vous voulez obtenir."
    assert missing_values([(96.81, 0.9681)], prose=prose)


# ---- alternatives et robustesse -----------------------------------------


def test_alternatives_taux_fraction_ou_pourcentage() -> None:
    """Un taux est aussi juste en 0,742 qu'en 74,2 : les deux passent, 0,31 non."""
    attendu = [(74.2, 0.742)]
    assert missing_values(attendu, prose="taux : 0.7420382165605095") == []
    assert missing_values(attendu, prose="taux : 74,2 %") == []
    assert missing_values(attendu, prose="taux : 0.31")


def test_fraction_juste_reste_acceptee() -> None:
    """La garde anti-arrondi-grossier ne doit pas rejeter une vraie fraction."""
    attendu = [(96.81, 0.9681)]
    assert missing_values(attendu, prose="taux : 0.9681") == []
    assert missing_values(attendu, prose="taux : 96,81 %") == []


def test_sans_attente_ne_verifie_rien() -> None:
    assert missing_values([], prose="n'importe quoi") == []


def test_un_tableau_malforme_ne_fait_pas_tomber_le_verificateur() -> None:
    assert missing_values([60], prose="60 SKU", tables=[{}, {"rows": None}]) == []


def test_les_booleens_ne_sont_pas_des_nombres() -> None:
    """True vaut 1 en Python : sans garde, il validerait une valeur attendue de 1."""
    t = table(["actif"], [[True], [False]])
    assert missing_values([1], prose="voir tableau", tables=[t])


def test_le_message_nomme_la_valeur_manquante() -> None:
    (message,) = missing_values([2.76], prose="panier de 0,047")
    assert "2.76" in message and "absente ou fausse" in message


# ---- les deux tolerances, directement ------------------------------------


def test_les_deux_tolerances_different() -> None:
    """La raison d'etre des deux fonctions : le meme nombre se juge autrement
    selon qu'il est REDIGE ou LU DANS UN TABLEAU.

    Une phrase arrondit — « 97 % » pour 96,81, « 63 % » pour 62,96 : c'est un
    rendu honnete. Un tableau n'arrondit pas : 97 y est 97, et le prendre pour
    96,81 laisserait n'importe quel identifiant valider n'importe quel taux.
    """
    assert matches_in_prose(97.0, 0, 96.81) is True
    assert matches_in_table(97.0, 96.81) is False

    assert matches_in_prose(63.0, 0, 62.96) is True
    assert matches_in_table(63.0, 62.96) is False


def test_la_garde_a_5_pourcent_neutralise_les_arrondis_qui_ne_disent_rien() -> None:
    """Sur une valeur inferieure a 1, un arrondi a l'entier vaut +/-0,5 — soit la
    moitie de la valeur. Il ne discrimine plus rien, donc on exige la precision
    de la reference."""
    assert matches_in_prose(1.0, 0, 0.9681) is False
    assert matches_in_prose(0.9681, 4, 0.9681) is True
