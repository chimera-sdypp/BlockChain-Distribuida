#!/bin/bash
# ==============================================================================
# Pilar 1 — Hit 7: Suite de pruebas automatizadas y validación cruzada
#
# Evalúa las implementaciones disponibles (Python, CPU C++, GPU CUDA) frente a:
#   - Vectores determinísticos V1-V12 (con solución y SIN_SOLUCION, bordes u64)
#   - Casos de error de argumentos E1-E7 (código 1, stdout vacío)
#   - Bordes extra de parseo X1-X8
#   - Formato exacto de la línea de stdout (la que parsea BC-27)
#   - Auditoría externa del hash con md5sum
#
# Uso: ./probar.sh        (termina con código 0 solo si todo pasa)
# ==============================================================================

set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "============================================================"
echo "Iniciando Batería de Pruebas (Hit 7 - Proof of Work con límites)"
echo "============================================================"

FALLOS_COMPILACION=0

# ¿Hay una GPU NVIDIA utilizable?
HAY_GPU=0
if command -v nvidia-smi &> /dev/null && nvidia-smi -L &> /dev/null; then
    HAY_GPU=1
fi

# Arquitectura a partir de la compute capability real (7.5 -> sm_75, 8.9 -> sm_89).
# Si no se puede consultar, sm_75 (Colab T4).
detectar_arch() {
    local cc
    cc=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -n 1 | tr -d ' .\r')
    if [[ "$cc" =~ ^[0-9]+$ ]]; then echo "sm_$cc"; else echo "sm_75"; fi
}

# 1. Compilación (si hay compilador). Se borra el binario anterior antes de compilar
#    para no probar por error uno viejo si la compilación falla.
if command -v g++ &> /dev/null && [ -f "pow_rango_cpu.cpp" ]; then
    echo "Compilando minero CPU (pow_rango_cpu.cpp)..."
    rm -f pow_rango_cpu
    if ! g++ -O3 -std=c++17 pow_rango_cpu.cpp -o pow_rango_cpu; then
        echo "[FALLO] no compiló pow_rango_cpu.cpp" >&2
        FALLOS_COMPILACION=$((FALLOS_COMPILACION + 1))
    fi
fi

if command -v nvcc &> /dev/null && [ -f "pow_rango.cu" ]; then
    ARCH=$(detectar_arch)
    echo "Compilando minero GPU (pow_rango.cu) con -arch=$ARCH..."
    rm -f pow_rango
    if ! nvcc -O3 -arch="$ARCH" pow_rango.cu -o pow_rango; then
        echo "[FALLO] no compiló pow_rango.cu" >&2
        FALLOS_COMPILACION=$((FALLOS_COMPILACION + 1))
    fi
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
else
    echo "AVISO: no hay Python o falta referencia.py; se saltea el oráculo." >&2
fi
if [ -f "./pow_rango_cpu" ] || [ -f "./pow_rango_cpu.exe" ]; then
    IMPLS+=("./pow_rango_cpu")
else
    echo "AVISO: no hay binario pow_rango_cpu; se saltea la CPU." >&2
fi
if [ -f "./pow_rango" ] || [ -f "./pow_rango.exe" ]; then
    if [ "$HAY_GPU" -eq 1 ]; then
        IMPLS+=("./pow_rango")
    else
        echo "AVISO: hay binario pow_rango pero no se detectó GPU (nvidia-smi); se saltea la GPU." >&2
    fi
else
    echo "AVISO: no hay binario pow_rango (¿falta nvcc?); se saltea la GPU." >&2
fi

if [ ${#IMPLS[@]} -eq 0 ]; then
    echo "Error: No se encontró ninguna implementación ejecutable para probar." >&2
    exit 1
fi

TIENE_MD5SUM=0
if command -v md5sum &> /dev/null; then
    TIENE_MD5SUM=1
else
    echo "AVISO: no hay md5sum; se omite la auditoría externa del hash." >&2
fi

CADENA="hit7_chimera"

# 4. Vectores de prueba (contrato §5) + bordes extra
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
    # Extra: ceros a la izquierda en min (solo dígitos => válido, vale 4409)
    "X8|000|00004409|4409|0|OK nonce=4409 hash=00094898d022e691b49d68a88e947b2f evaluados=1"
)

# 5. Casos de error (esperan rc=1 y stdout vacío). Formato: ID|cadena|prefijo|min|max
# El campo "-" en max significa "no pasar ese argumento" (E6).
CADENA_121=$(printf 'a%.0s' $(seq 1 121))
CASOS_ERR=(
    "E1|$CADENA|000|20|10"
    "E2|$CADENA|000|-1|10"
    "E3|$CADENA|000|10|18446744073709551616"
    "E4|$CADENA|0g0|0|10"
    "E5|$CADENA|000|0|18446744073709551615"
    "E6|$CADENA|000|0|-"
    "E7|$CADENA|000|1a|10"
    "X1|$CADENA|000| 5|10"
    "X2|$CADENA|000|+5|10"
    "X3|$CADENA|000|0x10|20"
    "X4|$CADENA||0|10"
    "X5|$CADENA|000000000000000000000000000000000|0|10"
    "X6|$CADENA_121|000|0|10"
    "X7|$CADENA|000||10"
)

# Línea exacta del contrato (§4): la que va a parsear BC-27.
REGEX_LINEA='^(OK nonce=[0-9]+ hash=[0-9a-f]{32} |SIN_SOLUCION )evaluados=[0-9]+ segundos=[0-9]+\.[0-9]{6} hps=[0-9]+$'

TOTAL=$(( ${#CASOS_OK[@]} + ${#CASOS_ERR[@]} ))
TOTAL_GLOBAL_FALLOS=0

# 6. Ejecución de la batería por cada implementación
for IMPL in "${IMPLS[@]}"; do
    echo ""
    echo "============================================================"
    echo "Probando implementación: $IMPL"
    echo "============================================================"

    EXITOS=0

    for CASO in "${CASOS_OK[@]}"; do
        IFS="|" read -r CID PREF MIN MAX EXP_RC EXP_LINE <<< "$CASO"

        # $IMPL sin comillas a propósito: "python3 referencia.py" son dos palabras.
        salida=$($IMPL "$CADENA" "$PREF" "$MIN" "$MAX" 2>/dev/null) && rc=0 || rc=$?
        salida=$(printf '%s' "$salida" | tr -d '\r')
        obtenido=$(printf '%s' "$salida" | sed -E 's/ segundos=[^ ]+ hps=[^ ]+$//')
        lineas=$(printf '%s\n' "$salida" | grep -c '')

        motivo=""
        if [ "$rc" -ne "$EXP_RC" ]; then
            motivo="código $rc (esperado $EXP_RC)"
        elif [ "$lineas" -ne 1 ]; then
            motivo="stdout tiene $lineas líneas (debe ser 1)"
        elif ! [[ "$salida" =~ $REGEX_LINEA ]]; then
            motivo="formato de línea inválido"
        elif [ "$obtenido" != "$EXP_LINE" ]; then
            motivo="contenido distinto"
        elif [ "$rc" -eq 0 ] && [ "$TIENE_MD5SUM" -eq 1 ]; then
            # Auditoría externa: md5sum sobre el mismo texto cadena+nonce
            nonce=$(printf '%s' "$salida" | sed -nE 's/.*nonce=([0-9]+).*/\1/p')
            hash_impl=$(printf '%s' "$salida" | sed -nE 's/.*hash=([0-9a-f]+).*/\1/p')
            hash_md5sum=$(printf "%s%s" "$CADENA" "$nonce" | md5sum | awk '{print $1}')
            if [ "$hash_impl" != "$hash_md5sum" ]; then
                motivo="discrepancia con md5sum ($hash_impl vs $hash_md5sum)"
            fi
        fi

        if [ -z "$motivo" ]; then
            echo "  [OK] $CID: rc=$rc | $salida"
            EXITOS=$((EXITOS + 1))
        else
            echo "  [FALLO] $CID: $motivo"
            echo "    Esperado: rc=$EXP_RC | $EXP_LINE"
            echo "    Obtenido: rc=$rc | $salida"
        fi
    done

    for CASO in "${CASOS_ERR[@]}"; do
        IFS="|" read -r CID CAD PREF MIN MAX <<< "$CASO"

        if [ "$MAX" == "-" ]; then
            salida=$($IMPL "$CAD" "$PREF" "$MIN" 2>/dev/null) && rc=0 || rc=$?
        else
            salida=$($IMPL "$CAD" "$PREF" "$MIN" "$MAX" 2>/dev/null) && rc=0 || rc=$?
        fi

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
echo "============================================================"
if [ "$TOTAL_GLOBAL_FALLOS" -eq 0 ] && [ "$FALLOS_COMPILACION" -eq 0 ]; then
    echo "TODAS LAS IMPLEMENTACIONES PROBADAS SUPERARON LA BATERÍA ($TOTAL/$TOTAL)"
    echo "Implementaciones probadas: ${#IMPLS[@]} (${IMPLS[*]})"
    echo "============================================================"
    exit 0
else
    echo "HUBO FALLOS: $TOTAL_GLOBAL_FALLOS implementación(es) con fallos, $FALLOS_COMPILACION compilación(es) fallida(s)."
    echo "============================================================"
    exit 1
fi
