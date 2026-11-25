#include <stdio.h>
#include "tampon.h"

static int echecs = 0;
#define VERIFIER(c) do { if (!(c)) { printf("ÉCHEC %s:%d : %s\n", __FILE__, __LINE__, #c); echecs++; } } while (0)

static void test_vide_au_depart(void)
{
    uint8_t mem[4];
    tampon_t t;
    tampon_init(&t, mem, sizeof mem);
    VERIFIER(tampon_vide(&t));
    VERIFIER(!tampon_plein(&t));
    VERIFIER(tampon_nb(&t) == 0);
    uint8_t o;
    VERIFIER(!tampon_lire(&t, &o));
}

static void test_fifo(void)
{
    uint8_t mem[4];
    tampon_t t;
    tampon_init(&t, mem, sizeof mem);
    VERIFIER(tampon_ecrire(&t, 1));
    VERIFIER(tampon_ecrire(&t, 2));
    VERIFIER(tampon_ecrire(&t, 3));
    VERIFIER(tampon_nb(&t) == 3);
    uint8_t o = 0;
    VERIFIER(tampon_lire(&t, &o) && o == 1);
    VERIFIER(tampon_lire(&t, &o) && o == 2);
    VERIFIER(tampon_lire(&t, &o) && o == 3);
    VERIFIER(tampon_vide(&t));
}

static void test_plein_refuse(void)
{
    uint8_t mem[3];
    tampon_t t;
    tampon_init(&t, mem, sizeof mem);
    for (uint8_t i = 0; i < 3; i++)
        VERIFIER(tampon_ecrire(&t, i));
    VERIFIER(tampon_plein(&t));
    VERIFIER(!tampon_ecrire(&t, 99));
    uint8_t o = 0;
    VERIFIER(tampon_lire(&t, &o) && o == 0);
}

static void test_rebouclage(void)
{
    uint8_t mem[3];
    tampon_t t;
    tampon_init(&t, mem, sizeof mem);
    uint8_t o = 0;
    for (int tour = 0; tour < 10; tour++) {
        VERIFIER(tampon_ecrire(&t, (uint8_t)tour));
        VERIFIER(tampon_ecrire(&t, (uint8_t)(tour + 100)));
        VERIFIER(tampon_lire(&t, &o) && o == (uint8_t)tour);
        VERIFIER(tampon_lire(&t, &o) && o == (uint8_t)(tour + 100));
    }
    VERIFIER(tampon_vide(&t));
}

static void test_vider(void)
{
    uint8_t mem[2];
    tampon_t t;
    tampon_init(&t, mem, sizeof mem);
    tampon_ecrire(&t, 7);
    tampon_ecrire(&t, 8);
    tampon_vider(&t);
    VERIFIER(tampon_vide(&t));
    VERIFIER(tampon_ecrire(&t, 9));
    uint8_t o = 0;
    VERIFIER(tampon_lire(&t, &o) && o == 9);
}

int main(void)
{
    test_vide_au_depart();
    test_fifo();
    test_plein_refuse();
    test_rebouclage();
    test_vider();
    if (echecs) {
        printf("%d vérification(s) en échec\n", echecs);
        return 1;
    }
    printf("Tous les tests passent\n");
    return 0;
}
