#!/bin/sh
# Faux agent pour tester la phase 7 sans OpenCode : écrit une solution correcte du ticket
# tampon-circulaire, et modifie un test (fichier protégé) pour vérifier que c'est détecté.
cat > src/tampon.c <<'EOF'
#include "tampon.h"

void tampon_init(tampon_t *t, uint8_t *mem, size_t taille)
{
    t->mem = mem;
    t->taille = taille;
    tampon_vider(t);
}

bool tampon_ecrire(tampon_t *t, uint8_t octet)
{
    if (tampon_plein(t))
        return false;
    t->mem[t->tete] = octet;
    t->tete = (t->tete + 1) % t->taille;
    t->nb++;
    return true;
}

bool tampon_lire(tampon_t *t, uint8_t *octet)
{
    if (tampon_vide(t))
        return false;
    *octet = t->mem[t->queue];
    t->queue = (t->queue + 1) % t->taille;
    t->nb--;
    return true;
}

size_t tampon_nb(const tampon_t *t) { return t->nb; }
bool tampon_vide(const tampon_t *t) { return t->nb == 0; }
bool tampon_plein(const tampon_t *t) { return t->nb == t->taille; }

void tampon_vider(tampon_t *t)
{
    t->tete = t->queue = t->nb = 0;
}
EOF
echo "/* touché par l'agent */" >> tests/test_tampon.c
echo "faux agent : fait"
