CFLAGS ?= -std=c11 -Wall -Wextra -Werror -O2

test: tests/test_tampon
	./tests/test_tampon

tests/test_tampon: src/tampon.c src/tampon.h tests/test_tampon.c
	$(CC) $(CFLAGS) -Isrc -o $@ src/tampon.c tests/test_tampon.c

clean:
	rm -f tests/test_tampon

.PHONY: test clean
