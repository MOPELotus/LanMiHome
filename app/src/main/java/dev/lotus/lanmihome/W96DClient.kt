package dev.lotus.lanmihome

import org.json.JSONObject

/**
 * W96D client facade.
 *
 * Keeps the UI independent from the selected owner:
 * router, night_node or phone.
 */
class W96DClient(private val api: LanMiHomeApi) {
    suspend fun state(): W96DState {
        val json = api.getJson("/api/v1/w96d")
        return W96DState.from(json)
    }

    suspend fun control(
        power: Boolean? = null,
        speed: Int? = null,
        natural: Boolean? = null,
        turbo: Boolean? = null,
        light: Boolean? = null,
    ) {
        val body = JSONObject()
        power?.let { body.put("power", it) }
        speed?.let { body.put("speed", it.coerceIn(0, 100)) }
        natural?.let { body.put("natural", it) }
        turbo?.let { body.put("turbo", it) }
        light?.let { body.put("light", it) }
        api.patchJson("/api/v1/w96d", body)
    }
}
