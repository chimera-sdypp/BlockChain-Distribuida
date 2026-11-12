# Hit 4 — MD5 en GPU

- `md5.cuh`: función `__device__ md5()` propia, escrita a partir del [RFC 1321](https://www.rfc-editor.org/rfc/rfc1321). Acepta cualquier largo.
- `md5.cu`: recibe un string, lo copia a la GPU, calcula el hash en un kernel y lo imprime en hex.
- `probar.sh`: compara contra `md5sum` los ejemplos del RFC y los largos donde cambia el relleno (55, 56, 64 y 65 bytes).

## Ejecución

```bash
nvcc -arch=sm_89 md5.cu -o md5 && ./md5 "hola"   # en Colab (T4): -arch=sm_75
./probar.sh
```

## Salida (RTX 4060)

```
$ ./md5 "hola"
4d186321c1a7f0f354b297e8914ab240
$ ./probar.sh
OK    d41d8cd98f00b204e9800998ecf8427e  largo   0  ""
OK    900150983cd24fb0d6963f7d28e17f72  largo   3  "abc"
...
12/12 iguales a md5sum
```

## Decisiones

- Implementación propia y no una biblioteca: el kernel de fuerza bruta del Hit 5 incluye el mismo `md5.cuh`.
- Un hash se calcula en un solo hilo (`<<<1, 1>>>`): cada bloque de 64 bytes depende del anterior, así que no se puede repartir. La GPU gana al calcular muchos hashes distintos a la vez (Hit 5).
- Las constantes van en memoria `__constant__`, y el bucle de 64 pasos está desenrollado (`#pragma unroll`) para que los `if` de cada ronda se resuelvan al compilar.
