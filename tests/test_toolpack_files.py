"""Etape 3 — le pack `files` et le contrat ToolPack."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.config import Config, FilesPackConfig, ToolpackConfig
from engine.toolpacks.base import Ok, Refus, ToolPack, build_toolpack, glob_to_regex, matches_any
from engine.toolpacks.files import FilesToolPack


def cfg_for(corpus: Path, **kw) -> Config:
    c = Config(toolpack=ToolpackConfig(files=FilesPackConfig(root=str(corpus), **kw)))
    c.root = corpus.parent
    return c


@pytest.fixture
def pack(corpus: Path) -> FilesToolPack:
    return FilesToolPack(cfg_for(corpus), {"globs": ["notes/**/*.md"]})


# ---- contrat -------------------------------------------------------------


def test_le_pack_respecte_le_protocole(pack: FilesToolPack) -> None:
    assert isinstance(pack, ToolPack)
    assert pack.name == "files"
    assert {t.name for t in pack.tools()} == {"grep", "read", "list"}


def test_describe_annonce_le_perimetre_et_les_outils(pack: FilesToolPack) -> None:
    d = pack.describe()
    assert "notes/**/*.md" in d
    assert "`grep(" in d and "`read(" in d and "`list(" in d


# ---- glob ----------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        ("**/*.md", "notes/a.md", True),
        ("**/*.md", "a.md", True),
        ("notes/**/*.md", "notes/a.md", True),
        ("notes/**/*.md", "notes/sub/a.md", True),
        ("notes/**/*.md", "autre/a.md", False),
        ("*.md", "notes/a.md", False),
        ("**/*", "secret.txt", True),
    ],
)
def test_glob_to_regex(pattern: str, path: str, expected: bool) -> None:
    assert bool(glob_to_regex(pattern).match(path)) is expected
    assert matches_any(path, [pattern]) is expected


# ---- validate : le refus arrive AVANT l'execution ------------------------


def test_validate_refuse_hors_perimetre(pack: FilesToolPack) -> None:
    v = pack.validate("read", {"path": "secret.txt"})
    assert isinstance(v, Refus) and "hors perimetre" in v.reason


def test_validate_refuse_la_remontee_de_repertoire(pack: FilesToolPack) -> None:
    assert isinstance(pack.validate("read", {"path": "../../etc/passwd"}), Refus)
    assert isinstance(pack.validate("read", {"path": "/etc/passwd"}), Refus)


def test_validate_refuse_un_outil_inconnu(pack: FilesToolPack) -> None:
    v = pack.validate("rm", {})
    assert isinstance(v, Refus) and "inconnu" in v.reason


def test_validate_refuse_un_argument_manquant(pack: FilesToolPack) -> None:
    assert isinstance(pack.validate("grep", {"glob": "**/*.md"}), Refus)


def test_validate_refuse_une_regex_invalide(pack: FilesToolPack) -> None:
    v = pack.validate("grep", {"pattern": "churn(", "glob": "notes/**/*.md"})
    assert isinstance(v, Refus) and "reguliere invalide" in v.reason


def test_validate_refuse_un_glob_hors_perimetre(pack: FilesToolPack) -> None:
    assert isinstance(pack.validate("list", {"glob": "*.txt"}), Refus)


def test_validate_accepte_dans_le_perimetre(pack: FilesToolPack) -> None:
    assert isinstance(pack.validate("read", {"path": "notes/a.md"}), Ok)
    assert isinstance(pack.validate("grep", {"pattern": "churn", "glob": "notes/**/*.md"}), Ok)


# ---- call ----------------------------------------------------------------


def test_list_ne_voit_que_le_perimetre(pack: FilesToolPack) -> None:
    res = pack.call("list", {"glob": "**/*"})
    assert res.ok
    assert "notes/a.md" in res.content and "notes/b.md" in res.content
    assert "secret.txt" not in res.content


def test_grep_renvoie_fichier_ligne_contenu(pack: FilesToolPack) -> None:
    res = pack.call("grep", {"pattern": "churn", "glob": "notes/**/*.md"})
    assert res.ok
    assert "notes/a.md:2: Le churn augmente de 12% au T3." in res.content
    assert res.meta["matches"] == 2 and res.meta["files_scanned"] == 2


def test_read_numerote_et_borne(pack: FilesToolPack) -> None:
    res = pack.call("read", {"path": "notes/a.md", "start": 2, "end": 2})
    assert res.ok
    assert res.content.strip().startswith("2|")
    assert "churn" in res.content and "Cause probable" not in res.content


def test_read_fichier_absent(pack: FilesToolPack) -> None:
    res = pack.call("read", {"path": "notes/zzz.md"})
    assert not res.ok and "introuvable" in res.error


def test_troncature_du_resultat(corpus: Path) -> None:
    big = corpus / "notes" / "big.md"
    big.write_text("ligne de remplissage\n" * 5000, encoding="utf-8")
    pack = FilesToolPack(cfg_for(corpus, max_bytes=500), {"globs": ["notes/**/*.md"]})
    res = pack.call("read", {"path": "notes/big.md"})
    assert len(res.content) < 700 and "tronque" in res.content


def test_max_results_plafonne_grep(corpus: Path) -> None:
    pack = FilesToolPack(cfg_for(corpus, max_results=1), {"globs": ["notes/**/*.md"]})
    res = pack.call("grep", {"pattern": "churn", "glob": "notes/**/*.md"})
    assert res.meta["matches"] == 2 and res.meta["matches_shown"] == 1
    assert "de plus" in res.content


def test_symlink_hors_racine_ignore(corpus: Path, tmp_path: Path) -> None:
    dehors = tmp_path / "dehors.md"
    dehors.write_text("secret externe\n", encoding="utf-8")
    (corpus / "notes" / "lien.md").symlink_to(dehors)
    pack = FilesToolPack(cfg_for(corpus), {"globs": ["notes/**/*.md"]})
    assert "lien.md" not in pack.call("list", {"glob": "**/*"}).content


def test_scope_vide_ouvre_tout_le_corpus(corpus: Path) -> None:
    pack = FilesToolPack(cfg_for(corpus), {})
    assert "secret.txt" in pack.call("list", {"glob": "**/*"}).content


# ---- artefact ------------------------------------------------------------


def test_artifact_est_une_commande_rejouable(pack: FilesToolPack) -> None:
    args = {"pattern": "churn", "glob": "notes/**/*.md"}
    trace = pack.artifact("grep", args, pack.call("grep", args))
    assert trace.startswith("$ grep -rnE")
    assert "matches=2" in trace


# ---- fabrique ------------------------------------------------------------


def test_build_toolpack_files(corpus: Path) -> None:
    assert build_toolpack("files", cfg_for(corpus), {"globs": ["**/*"]}).name == "files"


def test_build_toolpack_inconnu(corpus: Path) -> None:
    with pytest.raises(KeyError):
        build_toolpack("mongo", cfg_for(corpus), {})


def test_scope_hint_decrit_l_arborescence_reelle(corpus: Path) -> None:
    """Le brief_builder recoit ce texte : sans lui, il laisse `scope` vide."""
    hint = FilesToolPack(cfg_for(corpus), {}).scope_hint()
    assert '"globs"' in hint
    assert "notes" in hint and ".md" in hint


def test_describe_dit_qu_un_seul_glob_large_couvre_tout_le_perimetre(corpus: Path) -> None:
    """Constate en run reel : l'agent grepait fichier par fichier et epuisait
    ses 5 tours, alors qu'un `**/*` intersecte deja son perimetre."""
    pack = FilesToolPack(cfg_for(corpus), {"globs": ["notes/a.md", "notes/b.md"]})
    assert "INTERSECTE" in pack.describe()
    # et c'est vrai : un glob large ne rend que le perimetre
    res = pack.call("grep", {"pattern": "churn", "glob": "**/*"})
    assert "notes/a.md" in res.content and "notes/b.md" in res.content
    assert res.meta["files_scanned"] == 2  # pas secret.txt


def test_un_refus_de_glob_donne_le_motif_qui_marche(corpus: Path) -> None:
    """Un refus qui n'indique pas l'alternative fait tourner l'agent en rond."""
    pack = FilesToolPack(cfg_for(corpus), {"globs": ["notes/a.md", "notes/b.md"]})
    v = pack.validate("grep", {"pattern": "churn", "glob": "notes/{a,b}.md"})
    assert isinstance(v, Refus)
    assert "`**/*`" in v.reason and "pas de liste ni d'accolades" in v.reason
