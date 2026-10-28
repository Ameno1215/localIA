"""
title: Sources docs-internes
description: Remplace les références [a1b2c3d4] écrites par le modèle par des liens numérotés qui ouvrent le document à la bonne page, et ajoute la liste des sources.
version: 0.2
"""

# À installer dans Open WebUI : Panneau d'administration > Fonctions > + (coller ce fichier),
# puis activer la fonction et la rendre « Globale ». Le modèle recopie des références courtes,
# que ce filtre résout auprès de mcp-docs ; la section « Sources » qu'il écrit (utile sans filtre,
# dans OpenCode) est remplacée par la nôtre, aux liens vérifiés.

import re

import requests
from pydantic import BaseModel, Field

# [a1b2c3d4], [réf a1b2c3d4], [a1b2c3d4, e5f6a7b8]
REFS = re.compile(r"\[\s*(?:réf\.?\s*)?([0-9a-f]{8}(?:\s*[,;]\s*(?:réf\.?\s*)?[0-9a-f]{8})*)\s*\]", re.IGNORECASE)
CODE = re.compile(r"[0-9a-f]{8}", re.IGNORECASE)
# Section « Sources » écrite par le modèle malgré la consigne : elle est remplacée par la nôtre
SECTION_SOURCES = re.compile(r"^\s*(?:#{1,6}\s*|\*\*)?\s*Sources?\s*(?:\*\*)?\s*:?\s*(?:\*\*)?\s*$", re.IGNORECASE | re.MULTILINE)


class Filter:
    class Valves(BaseModel):
        mcp_url: str = Field("http://mcp-docs:9901", description="Adresse de mcp-docs vue depuis Open WebUI")

    def __init__(self):
        self.valves = self.Valves()

    def outlet(self, body: dict, __user__: dict | None = None) -> dict:
        messages = body.get("messages") or []
        if not messages or messages[-1].get("role") != "assistant":
            return body
        msg = messages[-1]
        textes = [msg.get("content") or ""] + [p.get("text") or "" for p in self._parties(msg)]
        codes = list(dict.fromkeys(c.lower() for t in textes for m in REFS.finditer(t) for c in CODE.findall(m.group(1))))
        if not codes:
            return body
        try:
            r = requests.get(f"{self.valves.mcp_url}/refs", params={"r": ",".join(codes)}, timeout=10)
            r.raise_for_status()
            infos = r.json()
        except Exception:
            return body

        numeros = {}  # code -> n, dans l'ordre d'apparition, seulement pour les références valides
        for c in codes:
            if infos.get(c):
                numeros[c] = len(numeros) + 1

        def lien(m: re.Match) -> str:
            sortie = ""
            for c in CODE.findall(m.group(1)):
                c = c.lower()
                sortie += f"[[{numeros[c]}]]({infos[c]['url']})" if c in numeros else "[réf. inconnue]"
            return sortie

        def source(c: str, n: int) -> str:
            i = infos[c]
            ligne = f"{n}. [{i['source']}" + (f", p.{i['page']}" if i.get("page") else "") + f"]({i['url']})"
            # Le lien principal ouvre le PDF à la page ; l'extrait reste accessible à côté
            if i.get("extrait") and i["extrait"] != i["url"]:
                ligne += f" · [extrait]({i['extrait']})"
            return ligne

        sources = "\n\n**Sources**\n\n" + "\n".join(source(c, n) for c, n in numeros.items())

        def reecrire(texte: str, avec_sources: bool) -> str:
            sections = list(SECTION_SOURCES.finditer(texte))
            if sections:
                texte = texte[: sections[-1].start()]
            texte = REFS.sub(lien, texte).rstrip()
            return texte + sources if avec_sources and numeros else texte

        if msg.get("content"):
            msg["content"] = reecrire(msg["content"], True)
        parties = self._parties(msg)
        for i, p in enumerate(parties):
            if p.get("text"):
                p["text"] = reecrire(p["text"], i == len(parties) - 1)
        return body

    @staticmethod
    def _parties(msg: dict) -> list[dict]:
        """Morceaux de texte de la réponse dans le format « output » d'Open WebUI (hors appels d'outils)."""
        return [
            p
            for item in msg.get("output") or []
            if isinstance(item, dict) and item.get("type") == "message"
            for p in item.get("content") or []
            if isinstance(p, dict) and "text" in p
        ]
