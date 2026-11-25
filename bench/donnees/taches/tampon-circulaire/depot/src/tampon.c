#include "tampon.h"

/* TODO : à implémenter. */

void tampon_init(tampon_t *t, uint8_t *mem, size_t taille)
{
    (void)t; (void)mem; (void)taille;
}

bool tampon_ecrire(tampon_t *t, uint8_t octet)
{
    (void)t; (void)octet;
    return false;
}

bool tampon_lire(tampon_t *t, uint8_t *octet)
{
    (void)t; (void)octet;
    return false;
}

size_t tampon_nb(const tampon_t *t)
{
    (void)t;
    return 0;
}

bool tampon_vide(const tampon_t *t)
{
    (void)t;
    return false;
}

bool tampon_plein(const tampon_t *t)
{
    (void)t;
    return false;
}

void tampon_vider(tampon_t *t)
{
    (void)t;
}
