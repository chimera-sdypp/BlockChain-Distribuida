"""Contrato v3 en el NCT (BC-79, BC-80): actas en la cadena, el estado desde la cadena y el caché que pierde."""

import json

from prometheus_client import REGISTRY

from comun import contrato, estado as estado_cadena
from comun.tests.claves import CUENTA, PRIVADA, Billetera, carga, cadena_actual
from nct.tests.escenario import confirmar, guardar_recibida, recibida

ORG = PRIVADA["organizador"]
FECHA = 1793836800


def estado_tx(redis, id_):
    return redis.hmget(f"tx:{id_}", "estado", "motivo")


def emision(redis):
    return int(redis.hget("nonces", "emision") or 0)


def divergencias(campo):
    return REGISTRY.get_sample_value("blockchain_estado_divergencias_total", {"campo": campo}) or 0


def test_un_alta_del_organizador_aprueba_la_cuenta_recien_cuando_entra_en_un_bloque(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    redis.hset("altas:pendientes", CUENTA["zoe"], json.dumps({"tipo": "minero", "nombre": "gpu-1", "pedida": FECHA}))
    alta = contrato.alta(ORG, 1, CUENTA["zoe"], "minero", "gpu-1", FECHA, cadena=cadena_actual())

    assert nct.validar(guardar_recibida(redis, alta)) == "pendiente"
    assert not redis.hexists("cuentas", CUENTA["zoe"])  # todavía no está en la cadena
    assert json.loads(redis.hget("altas:en_camino", CUENTA["zoe"])) == {"tipo": "minero", "nombre": "gpu-1",
                                                                        "tx": "alta-1"}
    nct.cerrar_cajas()
    id_tarea = nct.armar_si_libre()
    redis.hset(f"tarea:{id_tarea}", "prefijo", "0")
    from nct.tests.escenario import minar
    assert nct.procesar_solucion(minar(redis, id_tarea)) == "confirmada"

    assert json.loads(redis.hget("cuentas", CUENTA["zoe"])) == {"tipo": "minero", "nombre": "gpu-1", "alta": FECHA}
    assert not redis.hexists("altas:en_camino", CUENTA["zoe"]) and not redis.hexists("altas:pendientes", CUENTA["zoe"])
    assert redis.hget("nonces:confirmados", "emision") == "1"


def test_un_alta_firmada_por_otro_o_repetida_se_rechaza(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    trucha = contrato.alta(PRIVADA["alice"], 1, CUENTA["zoe"], "usuario", "zoe", FECHA, cadena=cadena_actual())
    assert nct.validar(guardar_recibida(redis, trucha)) == "rechazada"
    assert estado_tx(redis, "alta-1") == ["rechazada", "firma inválida"]


# ── Alta repetida (BC-93) ──

def test_la_prueba_del_profesor_dos_aprobaciones_seguidas_cargan_una_sola_vez(armar, redis):
    """Dos `claves aprobar --carga 50` seguidos: alta-1, carga-2, alta-3 y carga-4. Antes quedaban las cuatro y el
    saldo en 100. El alta repetida (la primera está en camino) se rechaza y la carga que la sigue se queda esperando
    su número hasta que vence: el saldo queda en 50."""
    nct = armar(espera_nonce=60)
    nct.cargar_genesis()
    zoe = CUENTA["zoe"]
    txs = [contrato.alta(ORG, 1, zoe, "usuario", "zoe", FECHA, cadena=cadena_actual()), contrato.carga(ORG, 2, zoe, 50, FECHA, cadena=cadena_actual()),
           contrato.alta(ORG, 3, zoe, "usuario", "zoe", FECHA, cadena=cadena_actual()), contrato.carga(ORG, 4, zoe, 50, FECHA, cadena=cadena_actual())]

    assert [nct.validar(guardar_recibida(redis, tx)) for tx in txs] == ["pendiente", "pendiente", "rechazada",
                                                                       "en_espera"]
    assert estado_tx(redis, "alta-3") == ["rechazada", estado_cadena.ALTA_REPETIDA]
    assert json.loads(redis.hget("altas:en_camino", zoe))["tx"] == "alta-1"  # la primera sigue en camino
    assert nct.vencer_espera(ahora=FECHA * 2) == ["carga-4"]
    nct.cerrar_cajas()
    id_tarea = nct.armar_si_libre()
    redis.hset(f"tarea:{id_tarea}", "prefijo", "0")
    from nct.tests.escenario import minar, saldo
    assert nct.procesar_solucion(minar(redis, id_tarea)) == "confirmada"
    assert saldo(redis, "zoe") == 50 and emision(redis) == 2


def test_un_alta_repetida_que_espera_su_numero_tambien_se_rechaza(armar, redis):
    """Dos réplicas: alta-3 llega antes que carga-2 y espera. Cuando entra carga-2 se destraba, y el Lua la rechaza
    aunque Python no la haya visto repetida al llegar (la primera todavía no estaba en camino)."""
    nct = armar()
    nct.cargar_genesis()
    zoe = CUENTA["zoe"]
    assert nct.validar(guardar_recibida(redis, contrato.alta(ORG, 1, zoe, "usuario", "zoe", FECHA, cadena=cadena_actual()))) == "pendiente"
    assert nct.validar(guardar_recibida(redis, contrato.alta(ORG, 3, zoe, "usuario", "zoe", FECHA, cadena=cadena_actual()))) == "en_espera"
    assert nct.validar(guardar_recibida(redis, contrato.carga(ORG, 2, zoe, 50, FECHA, cadena=cadena_actual()))) == "pendiente"
    assert estado_tx(redis, "alta-3") == ["rechazada", estado_cadena.ALTA_REPETIDA]
    assert redis.lrange("tx:pendientes", 0, -1) == ["alta-1", "carga-2"]


def test_un_alta_de_una_cuenta_que_ya_esta_en_la_cadena_se_rechaza(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    zoe = CUENTA["zoe"]
    confirmar(redis, nct, [contrato.alta(ORG, 1, zoe, "usuario", "zoe", FECHA, cadena=cadena_actual())])
    assert nct.validar(guardar_recibida(redis, contrato.alta(ORG, 2, zoe, "usuario", "zoe", FECHA, cadena=cadena_actual()))) == "rechazada"
    assert estado_tx(redis, "alta-2") == ["rechazada", estado_cadena.ALTA_REPETIDA]
    assert not redis.hexists("altas:en_camino", zoe)
    # Después de la baja, sí.
    confirmar(redis, nct, [contrato.baja(ORG, 2, zoe, "se fue", FECHA, cadena=cadena_actual())])
    assert nct.validar(guardar_recibida(redis, contrato.alta(ORG, 3, zoe, "usuario", "zoe", FECHA, cadena=cadena_actual()))) == "pendiente"


def test_el_escalador_con_permiso_da_de_alta_sus_mineros_y_nada_mas(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    escalador = PRIVADA["escalador"]
    vm = contrato.alta(escalador, 1, CUENTA["vm0"], "minero", "minero-cpu-0", FECHA, delegada=True, cadena=cadena_actual())
    assert nct.validar(guardar_recibida(redis, vm)) == "rechazada"
    assert estado_tx(redis, vm["id"])[1].startswith("sin permiso")

    confirmar(redis, nct, [contrato.permiso(ORG, 1, CUENTA["escalador"], "minero-cpu-", FECHA, cadena=cadena_actual())])
    assert redis.hget("permisos", CUENTA["escalador"]) == "minero-cpu-"
    otra = contrato.alta(escalador, 1, CUENTA["vm0"], "minero", "minero-cpu-0", FECHA + 1, delegada=True, cadena=cadena_actual())
    usuario = contrato.alta(escalador, 2, CUENTA["zoe"], "usuario", "minero-cpu-9", FECHA, delegada=True, cadena=cadena_actual())
    confirmar(redis, nct, [otra])
    assert json.loads(redis.hget("cuentas", CUENTA["vm0"]))["nombre"] == "minero-cpu-0"
    assert redis.hget("nonces:confirmados", CUENTA["escalador"]) == "1"
    assert nct.validar(guardar_recibida(redis, usuario)) == "rechazada"
    assert estado_tx(redis, usuario["id"])[1] == "sin permiso: sólo puede dar de alta mineros minero-cpu-*"


def test_una_cuenta_aprobada_a_mano_en_redis_no_llega_a_un_bloque(armar, redis, publicador):
    """HSET cuentas: el NCT la valida con el caché, pero al armar el bloque la cadena dice que no tiene alta."""
    nct = armar()
    nct.cargar_genesis()
    confirmar(redis, nct, [carga(1, "zoe", 50)])
    redis.hset("cuentas", CUENTA["zoe"], json.dumps({"tipo": "usuario", "nombre": "zoe", "alta": FECHA}))
    trucha = contrato.transaccion(PRIVADA["zoe"], CUENTA["bob"], 10, 1, FECHA, cadena=cadena_actual())
    buena = Billetera()("alice", "bob", 5)
    assert nct.validar(guardar_recibida(redis, trucha)) == "pendiente"
    assert redis.hget("comprometido", CUENTA["zoe"]) == "10"

    confirmar(redis, nct, [buena])

    assert estado_tx(redis, trucha["id"]) == ["rechazada", f"{trucha['id']}: el origen no tiene un alta en la cadena "
                                                           "(cuenta no aprobada)"]
    assert redis.hget("comprometido", CUENTA["zoe"]) == "0" and redis.hget("nonces", CUENTA["zoe"]) == "0"
    bloque = json.loads(redis.hget(f"bloque:{redis.lindex('cadena', -1)}", "transactions"))
    assert [tx["id"] for tx in bloque[1:]] == [buena["id"]]
    assert ("transaccion.estado", {"id": trucha["id"], "estado": "rechazada", "motivo": "no vale contra la cadena"}) \
        in publicador.avisos


def test_un_saldo_tocado_en_redis_lo_corrige_la_cadena(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    confirmar(redis, nct, [recibida(redis, "alice", "bob", 30)])
    redis.hincrby("saldos", CUENTA["carol"], 1000)
    redis.hset("cuentas", CUENTA["zoe"], json.dumps({"tipo": "usuario", "nombre": "zoe", "alta": FECHA}))
    antes = divergencias("saldos")

    corregido = nct.revisar_cache()

    assert corregido["saldos"] == [(CUENTA["carol"], 0, 1000)]
    assert corregido["cuentas"][0][0] == CUENTA["zoe"]
    assert redis.hget("saldos", CUENTA["carol"]) == "0" and not redis.hexists("cuentas", CUENTA["zoe"])
    assert divergencias("saldos") == antes + 1
    assert nct.revisar_cache() == {}


def test_un_saldo_tocado_no_se_puede_gastar(armar, redis):
    """Aunque el NCT valide con el saldo tocado, el bloque se arma contra la cadena y la transferencia no entra."""
    nct = armar(revisar_cada=0)
    nct.cargar_genesis()
    redis.hincrby("saldos", CUENTA["carol"], 1000)
    gasto = recibida(redis, "carol", "bob", 500)
    assert nct.validar(gasto) == "pendiente"
    confirmar(redis, nct, [recibida(redis, "alice", "bob", 1)])
    assert estado_tx(redis, gasto["id"])[0] == "rechazada"
    assert int(redis.hget("saldos", CUENTA["bob"])) == 51


def test_al_arrancar_el_estado_sale_de_la_cadena_aunque_borren_el_cache(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    confirmar(redis, nct, [recibida(redis, "alice", "bob", 30), carga(1, "carol", 7),
                           contrato.alta(ORG, 2, CUENTA["zoe"], "usuario", "zoe", FECHA, cadena=cadena_actual())])
    confirmar(redis, nct, [recibida(redis, "bob", "carol", 5)])
    antes = estado_cadena.cache_de_redis(redis)
    redis.delete("saldos", "nonces:confirmados", "cuentas")
    redis.hset("comprometido", CUENTA["alice"], 999)

    otro = armar()
    otro.arrancar()

    despues = estado_cadena.cache_de_redis(redis)
    assert {k: despues[k] for k in ("saldos", "nonces", "cuentas")} == \
        {k: {c: v for c, v in antes[k].items() if v} for k in ("saldos", "nonces", "cuentas")}
    assert redis.hget("comprometido", CUENTA["alice"]) is None
    assert otro.seguidor.estado.raiz() == nct.seguidor.ponerse_al_dia().raiz()


def test_el_snapshot_ahorra_recorrer_y_uno_tocado_no_se_usa(armar, redis):
    nct = armar(snapshot_cada=2)
    nct.cargar_genesis()
    for _ in range(3):
        confirmar(redis, nct, [recibida(redis, "alice", "bob", 1)])
    nct.seguidor.ponerse_al_dia()
    guardado = json.loads(redis.get("estado:snapshot"))
    assert guardado["altura"] == 2

    assert armar().seguidor.arrancar() == 3  # recorrió sólo el bloque 3
    guardado["saldos"][CUENTA["bob"]] += 100
    # Tocado con cuidado: la raíz del snapshot coincide con su contenido, pero no con la del bloque 3.
    guardado["raiz"] = estado_cadena.Estado.desde_dict({k: v for k, v in guardado.items() if k != "raiz"}).raiz()
    redis.set("estado:snapshot", json.dumps(guardado))
    otro = armar()
    assert otro.seguidor.arrancar() == 0  # no da la raíz que quedó en el bloque 3: desde el génesis
    assert otro.seguidor.estado.saldos[CUENTA["bob"]] == 53


def test_la_tarea_lleva_la_raiz_de_estado_y_la_cadena_que_arma_pasa_la_verificacion_estricta(armar, redis, publicador):
    nct = armar()
    nct.cargar_genesis()
    confirmar(redis, nct, [contrato.permiso(ORG, 1, CUENTA["escalador"], "minero-cpu-", FECHA, cadena=cadena_actual()),
                           carga(2, "dave", 100), recibida(redis, "alice", "bob", 3)])
    confirmar(redis, nct, [contrato.alta(PRIVADA["escalador"], 1, CUENTA["vm0"], "minero", "minero-cpu-0", FECHA,
                                         delegada=True, cadena=cadena_actual()),
                           contrato.alta(ORG, 3, CUENTA["dave"], "usuario", "dave", FECHA, cadena=cadena_actual()),
                           contrato.baja(ORG, 4, CUENTA["carol"], "", FECHA, cadena=cadena_actual())])
    confirmar(redis, nct, [contrato.transaccion(PRIVADA["dave"], CUENTA["alice"], 40, 1, FECHA, cadena=cadena_actual())])
    [*_, ultima] = publicador.tareas()
    assert ultima["raiz_estado"] == redis.hget(f"bloque:{redis.lindex('cadena', -1)}", "raiz_estado")

    reglas = estado_cadena.Reglas(organizador=CUENTA["organizador"])
    estado, malos = estado_cadena.reconstruir((b for _, b in estado_cadena.bloques_de_redis(redis)), reglas)
    assert malos == [] and estado.altura == 3
    assert estado_cadena.diferencias(estado, estado_cadena.cache_de_redis(redis)) == {}


# ── v4 (BC-95, BC-99) ──

def test_una_baja_del_organizador_veta_la_cuenta_y_el_escalador_no_la_reactiva(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    escalador, vm = PRIVADA["escalador"], CUENTA["vm0"]
    confirmar(redis, nct, [contrato.permiso(ORG, 1, CUENTA["escalador"], "minero-cpu-", FECHA, cadena=cadena_actual())])
    confirmar(redis, nct, [contrato.alta(escalador, 1, vm, "minero", "minero-cpu-0", FECHA, delegada=True,
                                         cadena=cadena_actual())])
    confirmar(redis, nct, [contrato.baja(ORG, 2, vm, "minaba mal", FECHA, cadena=cadena_actual())])
    assert not redis.hexists("cuentas", vm) and redis.hget("vetadas", vm) == "1"
    de_nuevo = contrato.alta(escalador, 2, vm, "minero", "minero-cpu-0", FECHA, delegada=True, cadena=cadena_actual())
    assert nct.validar(guardar_recibida(redis, de_nuevo)) == "rechazada"
    assert estado_tx(redis, de_nuevo["id"])[1] == estado_cadena.VETADA_POR_ORGANIZADOR
    # El caché de `vetadas` también lo corrige la cadena.
    redis.hdel("vetadas", vm)
    assert nct.revisar_cache() == {"vetadas": [(vm, True, None)]} and redis.hget("vetadas", vm) == "1"
    # El organizador sí la reactiva.
    confirmar(redis, nct, [contrato.alta(ORG, 3, vm, "minero", "minero-cpu-0", FECHA, cadena=cadena_actual())])
    assert redis.hexists("cuentas", vm) and not redis.hexists("vetadas", vm)


def test_lo_firmado_para_otra_cadena_se_rechaza_al_validar(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    vieja = "ab" * 16
    tx = contrato.transaccion(PRIVADA["alice"], CUENTA["bob"], 10, 1, FECHA, cadena=vieja)
    recarga = contrato.carga(ORG, 1, CUENTA["alice"], 10**6, FECHA, cadena=vieja)
    for firmada_antes in (tx, recarga):
        assert nct.validar(guardar_recibida(redis, firmada_antes)) == "rechazada"
        assert estado_tx(redis, firmada_antes["id"])[1] == "firma inválida"
