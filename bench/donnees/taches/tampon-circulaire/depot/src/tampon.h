/* Tampon circulaire d'octets, sans allocation dynamique (mémoire fournie par l'appelant). */
#ifndef TAMPON_H
#define TAMPON_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint8_t *mem;    /* zone mémoire fournie par l'appelant */
    size_t taille;   /* capacité en octets */
    size_t tete;     /* prochain emplacement d'écriture */
    size_t queue;    /* prochain emplacement de lecture */
    size_t nb;       /* nombre d'octets présents */
} tampon_t;

/* Initialise un tampon vide sur mem[0..taille-1]. */
void tampon_init(tampon_t *t, uint8_t *mem, size_t taille);
/* Ajoute un octet ; rend false (sans rien écrire) si le tampon est plein. */
bool tampon_ecrire(tampon_t *t, uint8_t octet);
/* Retire l'octet le plus ancien dans *octet ; rend false si le tampon est vide. */
bool tampon_lire(tampon_t *t, uint8_t *octet);
/* Nombre d'octets présents. */
size_t tampon_nb(const tampon_t *t);
bool tampon_vide(const tampon_t *t);
bool tampon_plein(const tampon_t *t);
/* Vide le tampon. */
void tampon_vider(tampon_t *t);

#endif
