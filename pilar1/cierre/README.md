# Cierre del Pilar 1 — Comparativa CPU vs GPU (BC-10)

Una batería fija de casos que corre, con los mismos parámetros, en los tres mineros del [Hit 7](../hit7/). Los tres tienen la misma interfaz (`<cadena> <prefijo> <min> <max>`):

| Implementación | Binario | Qué es |
|---|---|---|
| `gpu` | [`hit7/pow_rango`](../hit7/pow_rango.cu) | CUDA, un hilo por nonce, tandas de 2^20 |
| `cpu` | [`hit7/pow_rango_cpu`](../hit7/pow_rango_cpu.cpp) | C++, un hilo |
| `python` | [`hit7/referencia.py`](../hit7/referencia.py) | `hashlib`, un proceso: el mismo bucle que usa cada proceso del minero CPU del Pilar 2 ([`minero_cpu/busqueda.py`](../../pilar2/minero_cpu/busqueda.py)) |

Los tres devuelven el **menor** nonce del rango, así que tienen que dar exactamente el mismo número.

## Cómo correrlo

```bash
cd pilar1/cierre
python3 bateria.py          # un solo comando: GPU, CPU C++ y Python, 3 repeticiones -> resultados.csv
python3 graficar.py         # tablas en Markdown + comparativa.png (necesita matplotlib)
```

- `bateria.py` compila `pow_rango` (`nvcc -arch=native`) y `pow_rango_cpu` (`g++`) si faltan o si su fuente es más nueva. Si no hay GPU, la saltea con un aviso y corre solo CPU.
- Opciones: `--impl gpu,cpu` (por defecto `gpu,cpu,python`), `--repeticiones 1`, `--largos 1-5`.
- Tarda unos 6 minutos con las 3 repeticiones. Python se lleva casi todo el tiempo: ~2 min por repetición.
- Termina con código 1 si alguna corrida no coincide con el resultado esperado.

En Colab:

```bash
!cd BlockChain-Distribuida && git pull
!cd BlockChain-Distribuida/pilar1/cierre && python3 bateria.py && python3 graficar.py
```

## La batería (`casos.csv`)

La generó [`generar_casos.py`](generar_casos.py) con `hashlib`, **no** con los mineros que se prueban. Así el resultado esperado de cada caso es independiente y sirve para verificar a los tres. Son 36 casos:

- **Prefijos** de ceros de largo 1 a 6.
- **Tres cadenas**, elegidas por cuántos bloques de 64 bytes de MD5 ocupa `cadena + nonce`:
  - `corta`: `bc10`, 1 bloque.
  - `media`: `chimera-pilar1-cierre-etapa-inicial`, 35 caracteres, 1 bloque.
  - `bloque`: 77 caracteres con el formato real de un bloque del Pilar 2 (`previous_hash;timestamp;raiz;`), así que son **2 bloques de MD5 por nonce**. Es lo que mina de verdad el sistema.
- **Dos rangos por cadena y largo:**
  - **con solución:** `[0, tercera solución]`. Contiene varias, y lo correcto es devolver la primera. Mide el **tiempo hasta encontrar**.
  - **sin solución:** el hueco más largo entre dos soluciones seguidas. Se recorre entero sin encontrar nada, así que mide los **hashes/s sostenidos**. Para el largo 6 son entre 8 y 40 millones de nonces.

Cada corrida se verifica de tres formas:
1. El código de salida (0 o 2).
2. Que el `nonce`, el `hash` y los `evaluados` sean exactamente los esperados.
3. Que el hash, recalculado con `hashlib`, empiece con el prefijo.

`resultados.csv` guarda, para cada corrida, los parámetros, el resultado, el tiempo, los hashes/s y si salió correcta.

---

## Sección para el informe

### Hardware

Google Colab: GPU **Tesla T4** (`sm_75`); CPU **Intel Xeon @ 2.00GHz**, usada por la versión C++ y por la de Python. 3 repeticiones por caso.

### Verificación

```text
Resumen:
  gpu     108/108 corridas correctas
  cpu     108/108 corridas correctas
  python  108/108 corridas correctas
```

Las 324 corridas devolvieron el nonce, el hash y la cantidad de evaluados esperados. La GPU, la CPU y Python encuentran **exactamente el mismo número** en todos los casos, porque las tres devuelven el menor nonce del rango.

### Hashes/s y speedup por largo de prefijo

Rangos sin solución, recorridos enteros: suma de nonces evaluados / suma de segundos, sumando las 3 cadenas y las 3 repeticiones.

| Largo | Nonces recorridos | MH/s GPU (CUDA) | MH/s CPU C++ (1 hilo) | MH/s Python (1 proceso) | Speedup GPU/CPU C++ | Speedup GPU/Python |
|---|---|---|---|---|---|---|
| 1 | 129 | 0,12 | 2,22 | 0,50 | ×0,1 | ×0,2 |
| 2 | 2.718 | 2,36 | 2,82 | 0,99 | ×0,8 | ×2,4 |
| 3 | 82.869 | 71,44 | 3,42 | 1,22 | ×20,9 | ×58,7 |
| 4 | 1.501.026 | 574,67 | 3,36 | 1,20 | ×171,2 | ×478,5 |
| 5 | 14.467.506 | 1.007,49 | 4,03 | 1,09 | ×250,2 | ×926,6 |
| 6 | 241.185.126 | 1.014,38 | 3,14 | 1,10 | ×322,8 | ×925,9 |

### Tiempo hasta encontrar la solución

Rangos con solución, desde el nonce 0; promedio de las 3 cadenas × 3 repeticiones.

| Largo | Tiempo GPU (CUDA) | Tiempo CPU C++ (1 hilo) | Tiempo Python (1 proceso) |
|---|---|---|---|
| 1 | 0,000121 s | 0,000006 s | 0,000033 s |
| 2 | 0,000127 s | 0,000088 s | 0,000236 s |
| 3 | 0,000126 s | 0,001218 s | 0,003864 s |
| 4 | 0,000172 s | 0,009529 s | 0,024314 s |
| 5 | 0,000743 s | 0,189327 s | 0,522783 s |
| 6 | 0,014114 s | 5,607807 s | 20,539431 s |

### Efecto de la cadena

Hashes/s de todos los rangos sin solución de cada cadena.

| Cadena | MD5 por nonce | MH/s GPU (CUDA) | MH/s CPU C++ (1 hilo) | MH/s Python (1 proceso) | Speedup GPU/CPU C++ | Speedup GPU/Python |
|---|---|---|---|---|---|---|
| `corta` (4 caracteres) | 1 bloque | 2.106,44 | 4,19 | 1,07 | ×503 | ×1.973 |
| `media` (35 caracteres) | 1 bloque | 1.046,87 | 4,64 | 1,07 | ×226 | ×978 |
| `bloque` (77 caracteres, formato del Pilar 2) | 2 bloques | 700,43 | 2,49 | 1,13 | ×281 | ×622 |

![Comparativa CPU vs GPU](comparativa.png)

### Cómo leer los resultados

- **Hashes/s por largo:** en los largos chicos los rangos sin solución tienen pocas decenas o cientos de nonces. Ahí la GPU no llega a llenarse y el tiempo medido es casi todo latencia: lanzar el kernel y copiar el resultado (~0,1 ms, ver [Hit 6](../hit6/)). El rendimiento sostenido de cada implementación es el de los largos 5 y 6.
- **Tiempo hasta encontrar:** depende de dónde cae la primera solución. Crece ~×16 por carácter, porque los intentos esperados son 16^n (ver [Hit 6](../hit6/)). Para un mismo caso, todas las implementaciones recorren los mismos nonces, así que la diferencia de tiempo es solo de velocidad.
- **Speedup:** se calcula con los hashes/s de los rangos sin solución, recorridos enteros. Es una comparación justa: las dos implementaciones hacen exactamente el mismo trabajo.
- **GPU contra un solo núcleo:** la CPU C++ y Python usan un hilo. El minero CPU del Pilar 2 reparte el rango entre varios procesos; con *N* núcleos el speedup de la GPU se divide aproximadamente por *N*.

### Conclusiones

- **La GPU es entre 226 y 503 veces más rápida que un núcleo de CPU en C++, y entre 622 y 1.973 veces más que Python, según la cadena.** Con prefijo de 6 ceros encuentra la solución en 14 ms en promedio, contra 5,6 s de la CPU C++ (×397) y 20,5 s de Python (×1.455).
- **En rangos chicos la CPU gana.** Lanzar el kernel y traer el resultado cuesta un piso fijo de ~0,12 ms (entre 0,11 y 0,16 ms en todas las corridas de largo 1 a 3). En ese tiempo un núcleo de CPU prueba unos 400 nonces, así que con rangos de hasta unos mil nonces la GPU pierde: ×0,1 en largo 1 (~40 nonces por rango) y ×0,8 en largo 2 (~900). Con decenas de miles ya gana: ×21 en largo 3 (~28.000 por rango). Su ritmo sostenido (~1–2 GH/s) lo alcanza recién con rangos de millones (largos 5 y 6).
- **El speedup "por largo" del largo 6 (×323) mezcla las tres cadenas**: lo dominan las de más nonces. La comparación limpia es la tabla por cadena.
- **La cadena de 2 bloques de MD5 cuesta lo esperado en la CPU C++:** rinde el 59% de la corta (2,49 contra 4,19 MH/s), cerca de la mitad, porque hace el doble de rondas de MD5 por nonce. **En Python casi no cambia** (1,07 a 1,13 MH/s): ahí pesa el intérprete en cada intento (`str(nonce)`, copiar el estado, `hexdigest`), no el MD5 en sí.
- **En la GPU también importa el largo dentro de un mismo bloque:** `media` rinde la mitad que `corta` (1.047 contra 2.106 MH/s) aunque las dos son 1 bloque de MD5. Lo medido es eso; la explicación más probable es que el kernel del Hit 7 copia byte a byte la cadena y el bloque de relleno en la memoria local de cada hilo, y ese trabajo crece con el largo. La cadena de 77 caracteres rinde el 33% de la corta.
- **Para el Pilar 2:** con el texto real del bloque (77 caracteres), el kernel del Hit 7 hace 700 MH/s en la T4, unas 620 veces lo que hace un proceso del minero CPU en Python (1,1 MH/s). El minero GPU le pasa al binario trozos de 2^30 nonces, muy por encima del piso de latencia, y el *midstate* de [`minero_gpu/minar.cu`](../../pilar2/minero_gpu/minar.cu) ataca justamente el costo de la cadena larga.
