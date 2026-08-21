package dev.lotus.lanmihome

/**
 * W96D BLE fan model.
 *
 * The UI does not care which BLE owner is active. Backend reports owner:
 * router / night_node / phone.
 */
data class W96DState(
    val owner: String = "unknown",
    val power: Boolean = false,
    val speed: Int = 0,
    val natural: Boolean = false,
    val turbo: Boolean = false,
    val light: Boolean = false,
    val battery: Int? = null,
    val vbus: Boolean? = null,
    val motor: String? = null
)

object W96DApi {
    const val path = "/api/v1/w96d"

    fun control(speed: Int? = null, power: Boolean? = null,
                natural: Boolean? = null, turbo: Boolean? = null,
                light: Boolean? = null): Map<String, Any> {
        val result = mutableMapOf<String, Any>()
        speed?.let { result["speed"] = it.coerceIn(0, 100) }
        power?.let { result["power"] = it }
        natural?.let { result["natural"] = it }
        turbo?.let { result["turbo"] = it }
        light?.let { result["light"] = it }
        return result
    }
}
