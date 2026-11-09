#!/bin/bash
# ==============================================================================
# Pilar 1 — Hit 7: Benchmark comparativo CPU vs GPU con rangos acotados
#
# Mide el rendimiento (evaluados, segundos, hashes/s) sobre rangos completos
# utilizando un prefijo imposible de encontrar en el espacio evaluado ("ffffffffffffffff").
# Genera el archivo "resultados_benchmark.csv".
# ==============================================================================

set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "============================================================"
echo "Iniciando Benchmark CPU vs GPU (Hit 7 - Proof of Work)"
echo "============================================================"

# 1. Compilación opcional si los binarios no existen
if [ ! -f "pow_rango_cpu" ] && [ ! -f "pow_rango_cpu.exe" ] && command -v g++ &> /dev/null && [ -f "pow_rango_cpu.cpp" ]; then
    echo "Compilando pow_rango_cpu con g++..."
    g++ -O3 -std=c++17 pow_rango_cpu.cpp -o pow_rango_cpu || echo "Advertencia: no se pudo compilar pow_rango_cpu"
fi

if [ ! -f "pow_rango" ] && [ ! -f "pow_rango.exe" ] && command -v nvcc &> /dev/null && [ -f "pow_rango.cu" ]; then
    ARCH="sm_75"
    if command -v nvidia-smi &> /dev/null; then
        GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n 1)
        if [[ "$GPU_NAME" =~ "40" ]]; then
            ARCH="sm_89"
        elif [[ "$GPU_NAME" =~ "T4" ]]; then
            ARCH="sm_75"
        fi
    fi
    echo "Compilando pow_rango con nvcc (-arch=$ARCH)..."
    nvcc -O3 -arch="$ARCH" pow_rango.cu -o pow_rango || echo "Advertencia: no se pudo compilar pow_rango"
fi

# 2. Detección de hardware
if command -v nvidia-smi &> /dev/null; then
    HW_GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n 1 | tr ',' ' ' | xargs)
else
    HW_GPU="NVIDIA GPU no detectada"
fi

if command -v lscpu &> /dev/null; then
    HW_CPU=$(lscpu 2>/dev/null | grep -i "Model name:" | sed 's/.*: *//' | tr ',' ' ' | xargs)
elif [ -n "${PROCESSOR_IDENTIFIER:-}" ]; then
    HW_CPU=$(echo "$PROCESSOR_IDENTIFIER" | tr ',' ' ' | xargs)
else
    HW_CPU="CPU Host"
fi

# 3. Preparación de CSV de resultados
CSV="resultados_benchmark.csv"
echo "implementacion,tamano_rango,repeticion,evaluados,segundos,hps,hardware" > "$CSV"

CADENA="hit7_bench"
PREFIJO="ffffffffffffffff"  # 16 f: imposible en estos rangos, fuerza recorrido completo
REPETICIONES=3

# Detección de comando Python
PY_CMD=""
if command -v python3 &> /dev/null; then
    PY_CMD="python3"
elif command -v python &> /dev/null; then
    PY_CMD="python"
fi

# Función auxiliar para correr una serie de benchmarks
correr_bench() {
    local impl_id="$1"
    local impl_cmd="$2"
    local hw_name="$3"
    shift 3
    local tamanos=("$@")

    echo ""
    echo "------------------------------------------------------------"
    echo "Midiendo: $impl_id ($hw_name)"
    echo "------------------------------------------------------------"

    for tam in "${tamanos[@]}"; do
        # max = tam - 1
        local max_val=$((tam - 1))
        echo ">> Rango de $tam nonces [0, $max_val]..."

        for rep in $(seq 1 $REPETICIONES); do
            salida=$($impl_cmd "$CADENA" "$PREFIJO" 0 "$max_val" 2>/dev/null) && rc=0 || rc=$?
            salida=$(echo "$salida" | tr -d '\r')

            if [ "$rc" -ne 2 ]; then
                echo "  [ERROR] Salida inesperada (rc=$rc): '$salida'" >&2
                continue
            fi

            evaluados=$(echo "$salida" | sed -nE 's/.*evaluados=([0-9]+).*/\1/p')
            segundos=$(echo "$salida" | sed -nE 's/.*segundos=([0-9.]+).*/\1/p')
            hps=$(echo "$salida" | sed -nE 's/.*hps=([0-9.]+).*/\1/p')

            if [ "$evaluados" != "$tam" ]; then
                echo "  [ERROR] evaluados ($evaluados) != tamaño ($tam)" >&2
                continue
            fi

            echo "$impl_id,$tam,$rep,$evaluados,$segundos,$hps,$hw_name" >> "$CSV"
            printf "   Repeticion %d: %9.4f s | %12.0f hashes/s\n" "$rep" "$segundos" "$hps"
        done
    done
}

# 4. Ejecución por cada implementación disponible
if [ -n "$PY_CMD" ] && [ -f "referencia.py" ]; then
    TAMANOS_PY=(100000 1000000)
    correr_bench "python" "$PY_CMD referencia.py" "$HW_CPU" "${TAMANOS_PY[@]}"
fi

BIN_CPU=""
if [ -x "./pow_rango_cpu" ] || [ -f "./pow_rango_cpu" ]; then
    BIN_CPU="./pow_rango_cpu"
elif [ -f "./pow_rango_cpu.exe" ]; then
    BIN_CPU="./pow_rango_cpu.exe"
fi

if [ -n "$BIN_CPU" ]; then
    TAMANOS_CPU=(100000 1000000 10000000 100000000)
    correr_bench "cpu" "$BIN_CPU" "$HW_CPU" "${TAMANOS_CPU[@]}"
fi

BIN_GPU=""
if [ -x "./pow_rango" ] || [ -f "./pow_rango" ]; then
    BIN_GPU="./pow_rango"
elif [ -f "./pow_rango.exe" ]; then
    BIN_GPU="./pow_rango.exe"
fi

if [ -n "$BIN_GPU" ]; then
    TAMANOS_GPU=(100000 1000000 10000000 100000000 1000000000 10000000000)
    correr_bench "gpu" "$BIN_GPU" "$HW_GPU" "${TAMANOS_GPU[@]}"
fi

echo ""
echo "============================================================"
echo "Benchmark finalizado. Resultados guardados en $CSV"
echo "============================================================"

# 5. Resumen tabular en pantalla
if [ -f "$CSV" ]; then
    echo ""
    echo "--- Resumen Promedios de Rendimiento ---"
    awk -F',' '
    NR > 1 {
        key = $1 "," $2
        suma_hps[key] += $6
        suma_seg[key] += $5
        cant[key] += 1
        impl[key] = $1
        tam[key] = $2
    }
    END {
        printf "%-10s %-12s %-14s %-16s\n", "Impl", "Tamaño", "Tiempo prom(s)", "Hashes/s prom"
        printf "------------------------------------------------------------\n"
        for (k in cant) {
            n = cant[k]
            printf "%-10s %-12s %-14.4f %-16.0f\n", impl[k], tam[k], suma_seg[k]/n, suma_hps[k]/n
        }
    }' "$CSV" | sort -k1,1 -k2,2n
fi
