package dev.lotus.lanmihome

/**
 * W96D control abstraction.
 *
 * The UI does not know whether the current owner is router, night node,
 * or local phone BLE.
 */
class W96DRepository(private val api: Api) {
    suspend fun state(): W96DState {
        return api.w96dState()
    }

    suspend fun control(request: W96DControlRequest): W96DState {
        return api.w96dControl(request)
    }
}

data class W96DControlRequest(
    val power: Boolean? = null,
    val speed: Int? = null,
    val natural: Boolean? = null,
    val turbo: Boolean? = null,
    val light: Boolean? = null,
)

data class W96DState(
    val owner: String = "unknown",
    val power: Boolean = false,
    val speed: Int = 0,
    val natural: Boolean = false,
    val turbo: Boolean = false,
    val light: Boolean = false,
    val battery: Int? = null,
    val vbus: Boolean? = null,
    val motor: String? = null,
)
