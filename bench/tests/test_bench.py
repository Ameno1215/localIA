"""Tests de la suite elle-même, contre le faux serveur (aucun GPU ni service requis).

  python3 -m unittest bench.tests.test_bench -v      (depuis la racine du dépôt, ~1 min)
"""
import csv
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from bench import commun
from bench.tests import faux_serveur

DEPOT = Path(__file__).resolve().parents[2]


class TestUnitaires(unittest.TestCase):
    def test_centile(self):
        self.assertEqual(commun.centile([1, 2, 3, 4, 5], 50), 3)
        self.assertAlmostEqual(commun.centile([0, 10], 95), 9.5)
        self.assertIsNone(commun.centile([None], 95))

    def test_tpot_et_resume(self):
        m = commun.Mesure("x", 0, statut=200, ttft=1.0, duree=3.0, tokens_sortie=21)
        self.assertAlmostEqual(m.tpot, 0.1)
        r = commun.resumer([m, commun.Mesure("x", 0, statut=429, erreur="trop")], 10)
        self.assertEqual(r["taux_erreur"], 0.5)
        self.assertEqual(r["erreurs"], {"429": 1})
        self.assertAlmostEqual(r["vitesse_p95"], 10.0)

    def test_cle_directe(self):
        self.assertEqual(commun.cle_directe({}, "maitre"), "maitre")
        os.environ["BENCH_TEST_CLE_VLLM"] = "sk-vllm"
        try:
            self.assertEqual(commun.cle_directe({"cle_env": "BENCH_TEST_CLE_VLLM"}, "maitre"), "sk-vllm")
        finally:
            del os.environ["BENCH_TEST_CLE_VLLM"]
        with self.assertRaises(SystemExit):
            commun.cle_directe({"cle_env": "BENCH_TEST_CLE_ABSENTE"}, "maitre")

    def test_generateur_longueur(self):
        g = commun.Generateur(graine=1, tokens_par_mot=2.0)
        self.assertEqual(len(g.texte(1000).split()), 500)


class TestBoutEnBout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = faux_serveur.demarrer(0)
        url = f"http://127.0.0.1:{cls.srv.server_port}"
        cls.tmp = Path(tempfile.mkdtemp(prefix="bench-test-"))
        agent = DEPOT / "bench/tests/faux_agent.sh"
        panne = f"{sys.executable} -c \"import urllib.request; urllib.request.urlopen('{url}/__panne?duree=3')\""
        profil = f"""
[profils.test]
passerelle = "{url}"
metriques = ["{url}/metrics"]
[profils.test.roles.code-agent]
modele = "faux"
direct = "{url}"
[profils.test.roles.fast]
modele = "faux-fast"
direct = "{url}"
[profils.test.slo]
genou_code_agent = 2
genou_fast = 2
[profils.test.slo.vitesse_min]
code-agent = 50.0
fast = 50.0
[profils.test.capacite]
niveaux = [1, 2]
longueurs = [500, 60000]
duree_palier_s = 2
max_tokens = 20
[profils.test.charge]
paliers = [2, 3]
palier_cible = 3
duree_palier_s = 4
etapes = [2, 3]
contexte_debut = 200
contexte_fin = 1000
systeme_tokens = 100
reflexion_s = [0.1, 0.3]
max_tokens = 20
prob_rag = 0.5
modele_embeddings = "emb"
max_parallel_par_cle = 2
[profils.test.charge.melange]
code-agent = 0.7
fast = 0.3
[profils.test.rag]
backend = "tei"
embed = "{url}"
rerank = "{url}"
qdrant = "{url}"
collection = "docs_internes"
questions = "bench/donnees/questions_rag.jsonl"
concurrences = [1, 2]
requetes_par_niveau = 10
candidats_rerank = 50
[profils.test.limites]
role = "code-agent"
requetes_gourmandes = 6
plafond_sans_max_tokens = 8192
concurrence_outils = 4
essais_outils = 8
[[profils.test.pannes]]
nom = "panne-simulee"
role = "code-agent"
consigne = "panne de 3 s"
commande = '''{panne}'''
[profils.test.qualite]
taches = "bench/donnees/taches"
commande_agent = ["sh", "{agent}"]
"""
        cfg = cls.tmp / "config.toml"
        cfg.write_text((DEPOT / "bench/config.toml").read_text() + profil)
        cls.env = {**os.environ, "BENCH_CONFIG": str(cfg), "BENCH_RESULTATS": str(cls.tmp / "resultats"),
                   "BENCH_PROFIL": "test", "BENCH_CLE": "sk-maitre-test"}

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def bench(self, *args, timeout=180):
        p = subprocess.run([sys.executable, "-m", "bench", *args], cwd=DEPOT, env=self.env,
                           capture_output=True, text=True, timeout=timeout)
        self.assertEqual(p.returncode, 0, msg=f"bench {' '.join(args)}\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}")
        return p.stdout

    def resultat(self, phase):
        f = sorted((self.tmp / "resultats").glob(f"{phase}-*.json"))
        self.assertTrue(f, f"aucun résultat {phase}")
        return json.loads(f[-1].read_text())

    def test_1_calibrer_et_capacite(self):
        self.bench("calibrer")
        ratio = json.loads((self.tmp / "resultats/calibration.json").read_text())["tokens_par_mot"]
        self.assertTrue(1.0 < ratio < 2.0, ratio)
        sortie = self.bench("capacite", "--mode", "les-deux")
        self.assertIn("ignoré (dépasse le contexte", sortie)
        r = self.resultat("capacite")
        essai = r["roles"]["code-agent"]["essais"][0]
        self.assertEqual(essai["genou"], 2)
        p = essai["paliers"][0]
        self.assertGreater(p["requetes"], 0)
        self.assertEqual(p["taux_erreur"], 0)
        self.assertIsNotNone(p["metriques"]["kv_max"])
        self.assertEqual({e["mode"] for e in r["roles"]["fast"]["essais"]}, {"froid", "prefixe"})

    def test_2_charge(self):
        self.bench("charge", "--creer-cles")
        r = self.resultat("charge")
        self.assertEqual([p["devs"] for p in r["paliers"]], [2, 3])
        p = r["paliers"][-1]
        self.assertEqual(p["taux_erreur"], 0, p)
        self.assertEqual(p["surcout_p95_ms"], 4.2)
        self.assertGreater(p["rag"]["appels"], 0)
        self.assertEqual(r["cles_distinctes"], 3)

    def test_3_endurance(self):
        self.bench("charge", "--endurance", "0.003", "--devs", "2", "--fenetre", "3")
        r = self.resultat("endurance")
        self.assertGreaterEqual(len(r["endurance"]["fenetres"]), 3)

    def test_4_rag(self):
        self.bench("rag")
        r = self.resultat("rag")
        self.assertEqual(r["rappel"]["rappel5_avec_reranker"], 1.0)
        self.assertEqual(r["latences"]["rerank"][0]["erreurs"], 0)
        self.assertIsNotNone(r["latences"]["recherche"][0]["p95_ms"])

    def test_5_pannes(self):
        self.bench("pannes", "--executer", "--delai-max", "60", timeout=120)
        s = self.resultat("pannes")["scenarios"][0]
        self.assertGreater(s["echecs"], 0)
        self.assertEqual(set(s["erreurs"]), {"503"})
        self.assertTrue(s["reussi"])
        self.assertLess(s["impact_s"], 10)

    def test_6_limites(self):
        self.bench("calibrer")
        self.bench("limites")
        r = self.resultat("limites")
        self.assertEqual(r["trop_long"]["statut"], 400, r["trop_long"])
        self.assertTrue(r["gourmand"]["reussi"], r["gourmand"])
        self.assertTrue(r["sans_max"]["reussi"])
        self.assertEqual(r["outils"]["mal_formes"], 0)
        self.assertTrue(r["outils"]["reussi"])

    def test_7_qualite(self):
        self.bench("qualite")
        t = self.resultat("qualite")["tickets"][0]
        self.assertTrue(t["reussi"], t)
        self.assertEqual(t["fichiers_proteges_modifies"], ["tests/test_tampon.c"])

    def test_8_thermique_et_rapport(self):
        f = self.tmp / "sonde-spark1.csv"
        with open(f, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["horodatage", "hote", "freq_sm_mhz", "puissance_w", "temp_gpu_c", "acpi_max_c",
                        "mem_dispo_go", "swap_utilise_go", "charge1"])
            t0 = time.time()
            for i in range(30):
                w.writerow([t0 + 10 * i + (200 if i > 20 else 0), "spark1", 1800 - i, 150, 60, 70 + i % 3, 40, 0, 4])
        self.bench("thermique", str(f))
        th = self.resultat("thermique")
        self.assertFalse(th["reussi"])                 # trou de 200 s dans le journal
        self.assertTrue(th["noeuds"][0]["trous_s"])
        sortie = self.bench("rapport")
        self.assertIn("Go / no-go", sortie)
        self.assertIn("Genou de code-agent", sortie)


if __name__ == "__main__":
    unittest.main()
