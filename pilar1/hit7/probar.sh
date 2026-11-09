#!/bin/bash
# ==============================================================================
# Pilar 1 — Hit 7: Suite de pruebas automatizadas y validación cruzada
#
# Evalúa las implementaciones disponibles (Python, CPU C++, GPU CUDA) frente a:
#   - Vectores determinísticos V1-V12 (casos con solución y SIN_SOLUCION, bordes u64)
#   - Auditoría externa con md5sum
#   - Casos de error de argumentos E1-E7 (código 1, stdout vacío)
# ==============================================================================

set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "============================================================"
echo "Iniciando Batería de Pruebas (Hit 7 - Proof of Work con límites)"
echo "============================================================"

# 1. Compilación opcional si los compiladores están presentes
if command -v g++ &> /dev/null && [ -f "pow_rango_cpu.cpp" ]; then
    echo "Compilando minero CPU (pow_rango_cpu.cpp)..."
    g++ -O3 -std=c++17 pow_rango_cpu.cpp -o pow_rango_cpu || echo "Advertencia: falló compilación con g++"
fi

if command -v nvcc &> /dev/null && [ -f "pow_rango.cu" ]; then
    ARCH="sm_75"
    if command -v nvidia-smi &> /dev/null; then
        GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n 1)
        if [[ "$GPU_NAME" =~ "40" ]]; then
            ARCH="sm_89"
        elif [[ "$GPU_NAME" =~ "T4" ]]; then
            ARCH="sm_75"
        fi
    fi
    echo "Compilando minero GPU (pow_rango.cu) con -arch=$ARCH..."
    nvcc -O3 -arch="$ARCH" pow_rango.cu -o pow_rango || echo "Advertencia: falló compilación con nvcc"
fi

# 2. Detección de comando Python
PY_CMD=""
if command -v python3 &> /dev/null; then
    PY_CMD="python3"
elif command -v python &> /dev/null; then
    PY_CMD="python"
fi

# 3. Lista de implementaciones a auditar
IMPLS=()
if [ -n "$PY_CMD" ] && [ -f "referencia.py" ]; then
    IMPLS+=("$PY_CMD referencia.py")
fi
if [ -x "./pow_rango_cpu" ] || [ -f "./pow_rango_cpu" ] || [ -f "./pow_rango_cpu.exe" ]; then
    IMPLS+=("./pow_rango_cpu")
fi
if [ -x "./pow_rango" ] || [ -f "./pow_rango" ] || [ -f "./pow_rango.exe" ]; then
    IMPLS+=("./pow_rango")
fi

if [ ${#IMPLS[@]} -eq 0 ]; then
    echo "Error: No se encontró ninguna implementación ejecutable para probar." >&2
    exit 1
fi

CADENA="hit7_chimera"

# 4. Definición de Vectores de Prueba V1 a V12
# Formato: ID|prefijo|min|max|rc_esperado|salida_esperada_sin_metricas
CASOS_OK=(
    "V1|000|0|20000|0|OK nonce=4409 hash=00094898d022e691b49d68a88e947b2f evaluados=4410"
    "V2|000|4410|11381|2|SIN_SOLUCION evaluados=6972"
    "V3|000|11382|11382|0|OK nonce=11382 hash=000059b92efbd22f05f0389e5c4da5d8 evaluados=1"
    "V4|000|11383|11383|2|SIN_SOLUCION evaluados=1"
    "V5|8e99|0|0|0|OK nonce=0 hash=8e991d9c530d4fa1d5d253ba4cf6ab88 evaluados=1"
    "V6|00000|1048576|1437713|0|OK nonce=1437713 hash=00000aac6f881c98e26f44f9b23dbf78 evaluados=389138"
    "V7|00000|0|1437712|2|SIN_SOLUCION evaluados=1437713"
    "V8|00000|1437713|2106361|0|OK nonce=1437713 hash=00000aac6f881c98e26f44f9b23dbf78 evaluados=1"
    "V9|00000|1437714|2106360|2|SIN_SOLUCION evaluados=668647"
    "V10|00|18446744073709546615|18446744073709551615|0|OK nonce=18446744073709546645 hash=003e50fab74c09ac4aa081cd03ced284 evaluados=31"
    "V11|68ad|18446744073709551600|18446744073709551615|0|OK nonce=18446744073709551615 hash=68ad91213fc460ec8edf04cc5c084c73 evaluados=16"
    "V12|8E99|0|0|0|OK nonce=0 hash=8e991d9c530d4fa1d5d253ba4cf6ab88 evaluados=1"
)

# 5. Definición de Casos de Error E1 a E7 (esperan rc=1 y stdout vacío)
CASOS_ERR=(
    "E1|000|20|10"
    "E2|000|-1|10"
    "E3|000|10|18446744073709551616"
    "E4|0g0|0|10"
    "E5|000|0|18446744073709551615"
    "E6|000|0|"
    "E7|000|1a|10"
)

TOTAL_GLOBAL_FALLOS=0

# 6. Ejecución de la batería por cada implementación
for IMPL in "${IMPLS[@]}"; do
    echo ""
    echo "============================================================"
    echo "Probando implementación: $IMPL"
    echo "============================================================"

    EXITOS=0
    TOTAL=19

    # Correr V1 - V12
    for CASO in "${CASOS_OK[@]}"; do
        IFS="|" read -r CID PREF MIN MAX EXP_RC EXP_LINE <<< "$CASO"

        salida=$($IMPL "$CADENA" "$PREF" "$MIN" "$MAX" 2>/dev/null) && rc=0 || rc=$?
        salida=$(echo "$salida" | tr -d '\r')
        obtenido=$(echo "$salida" | sed -E 's/ segundos=[^ ]+ hps=[^ ]+$//')
        lineas=$(echo "$salida" | grep -c -E '^OK|^SIN_SOLUCION' || true)

        if [ "$rc" -eq "$EXP_RC" ] && [ "$obtenido" == "$EXP_LINE" ] && [ "$lineas" -eq 1 ]; then
            # Auditoría cruzada con md5sum si encontró solución
            if [ "$rc" -eq 0 ] && command -v md5sum &> /dev/null; then
                nonce=$(echo "$salida" | sed -nE 's/.*nonce=([0-9]+).*/\1/p')
                hash_gpu=$(echo "$salida" | sed -nE 's/.*hash=([0-9a-fA-F]+).*/\1/p')
                hash_md5sum=$(printf "%s%s" "$CADENA" "$nonce" | md5sum | awk '{print $1}')
                if [ "$hash_gpu" != "$hash_md5sum" ]; then
                    echo "  [FALLO] $CID: Discrepancia con md5sum ($hash_gpu vs $hash_md5sum)"
                    continue
                fi
            fi
            echo "  [OK] $CID: rc=$rc | $obtenido"
            EXITOS=$((EXITOS + 1))
        else
            echo "  [FALLO] $CID:"
            echo "    Esperado: rc=$EXP_RC | $EXP_LINE"
            echo "    Obtenido: rc=$rc | $obtenido (lineas=$lineas)"
        fi
    done

    # Correr E1 - E7
    for CASO in "${CASOS_ERR[@]}"; do
        IFS="|" read -r CID PREF MIN MAX <<< "$CASO"

        if [ -z "$MAX" ]; then
            # E6: faltan argumentos
            salida=$($IMPL "$CADENA" "$PREF" "$MIN" 2>/dev/null) && rc=0 || rc=$?
        else
            salida=$($IMPL "$CADENA" "$PREF" "$MIN" "$MAX" 2>/dev/null) && rc=0 || rc=$?
        fi
        salida=$(echo "$salida" | tr -d '\r')

        if [ "$rc" -eq 1 ] && [ -z "$salida" ]; then
            echo "  [OK] $CID: rc=1 | stdout vacío (rechazo correcto)"
            EXITOS=$((EXITOS + 1))
        else
            echo "  [FALLO] $CID: esperado rc=1 y stdout vacío, obtenido rc=$rc salida='$salida'"
        fi
    done

    echo "------------------------------------------------------------"
    echo "Resultado para $IMPL: $EXITOS/$TOTAL pruebas superadas."
    echo "------------------------------------------------------------"

    if [ "$EXITOS" -ne "$TOTAL" ]; then
        TOTAL_GLOBAL_FALLOS=$((TOTAL_GLOBAL_FALLOS + 1))
    fi
done

echo ""
if [ "$TOTAL_GLOBAL_FALLOS" -eq 0 ]; then
    echo "============================================================"
    echo "TODAS LAS IMPLEMENTACIONES SUPERARON LA BATERÍA (19/19 OK)"
    echo "============================================================"
    exit 0
else
    echo "============================================================"
    echo "HUBO FALLOS EN $TOTAL_GLOBAL_FALLOS IMPLEMENTACIONES."
    echo "============================================================"
    exit 1
fi
