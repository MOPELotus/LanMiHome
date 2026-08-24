package dev.lotus.lanmihome

import android.content.Context
import java.net.URI
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

internal enum class W96dEnvironment { HOME, SCHOOL }
internal enum class W96dOwner { ROUTER, NIGHT_NODE, PHONE }

internal data class W96dState(
    val available: Boolean = false,
    val connected: Boolean = false,
    val name: String = "W96D",
    val address: String? = null,
    val owner: String? = null,
    val scheduled: Boolean = false,
    val paused: Boolean = false,
    val power: Boolean? = null,
    val gear: Int? = null,
    val speed: Int? = null,
    val natural: Boolean? = null,
    val turbo: Boolean? = null,
    val turboRemainingSeconds: Int? = null,
    val turboTimeSeconds: Int? = null,
    val timerRemainingSeconds: Int? = null,
    val sleepDelaySeconds: Int? = null,
    val gearDownMode: Int? = null,
    val gearSpeeds: List<Int>? = null,
    val indicator: Boolean? = null,
    val batteryVoltageMv: Int? = null,
    val batteryCurrentMa: Int? = null,
    val batteryCapacityMwh: Long? = null,
    val vbusVoltageMv: Long? = null,
    // Kept only so the parked Night Node source remains binary/source compatible.
    // The HTML interpretation of this field was disproved by differential captures.
    val vbusCurrentMa: Int? = null,
    val chargeStatus: Int? = null,
    val motorCurrentMa: Int? = null,
    val motorVoltageMv: Int? = null,
    // Same compatibility-only placeholder: FFD3[2:4] remains unknown.
    val motorBlocked: Boolean? = null,
    val serialNumber: String? = null,
    val firmwareVersion: String? = null,
    val error: String? = null,
    val updatedAt: String? = null,
) {
    companion object {
        fun fromJson(j: JSONObject) = W96dState(
            available = j.optBoolean("available", false),
            connected = j.optBoolean("connected", false),
            name = j.optString("name", "W96D"),
            address = j.stringOrNullW96d("address"),
            owner = j.stringOrNullW96d("owner"),
            scheduled = j.optBoolean("scheduled", false),
            paused = j.optBoolean("paused", false),
            power = j.boolOrNullW96d("power"),
            gear = j.intOrNullW96d("gear"),
            speed = j.intOrNullW96d("speed"),
            natural = j.boolOrNullW96d("natural"),
            turbo = j.boolOrNullW96d("turbo"),
            turboRemainingSeconds = j.intOrNullW96d("turbo_remaining_seconds"),
            turboTimeSeconds = j.intOrNullW96d("turbo_time_seconds"),
            timerRemainingSeconds = j.intOrNullW96d("timer_remaining_seconds"),
            sleepDelaySeconds = j.intOrNullW96d("sleep_delay_seconds"),
            gearDownMode = j.intOrNullW96d("gear_down_mode"),
            gearSpeeds = j.optJSONArray("gear_speeds")?.let { a ->
                List(a.length()) { index -> a.optInt(index) }
            },
            indicator = j.boolOrNullW96d("indicator"),
            batteryVoltageMv = j.intOrNullW96d("battery_voltage_mv"),
            batteryCurrentMa = j.intOrNullW96d("battery_current_ma"),
            batteryCapacityMwh = j.longOrNullW96d("battery_capacity_mwh"),
            vbusVoltageMv = j.longOrNullW96d("vbus_voltage_mv"),
            // Intentionally do not ingest legacy vbus_current_ma / motor_blocked.
            chargeStatus = j.intOrNullW96d("charge_status"),
            motorCurrentMa = j.intOrNullW96d("motor_current_ma"),
            motorVoltageMv = j.intOrNullW96d("motor_voltage_mv"),
            serialNumber = j.stringOrNullW96d("serial_number"),
            firmwareVersion = j.stringOrNullW96d("firmware_version"),
            error = j.stringOrNullW96d("error"),
            updatedAt = j.stringOrNullW96d("updated_at"),
        )
    }
}

private fun JSONObject.stringOrNullW96d(key: String): String? =
    if (has(key) && !isNull(key)) optString(key).takeIf(String::isNotBlank) else null

private fun JSONObject.boolOrNullW96d(key: String): Boolean? =
    if (has(key) && !isNull(key)) optBoolean(key) else null

private fun JSONObject.intOrNullW96d(key: String): Int? =
    if (has(key) && !isNull(key)) optInt(key) else null

private fun JSONObject.longOrNullW96d(key: String): Long? =
    if (has(key) && !isNull(key)) optLong(key) else null

internal object W96dPrefs {
    private const val PREFS = "lanmihome_w96d"
    private const val KEY_ENV = "environment"
    private const val KEY_OUTDOOR = "outdoor"
    private const val KEY_NIGHT_URL = "night_url"
    private const val KEY_ADDRESS = "ble_address"
    private const val KEY_NODE_PAUSED = "node_paused"
    private const val KEY_LAST_SPEED = "last_nonzero_speed"
    private const val KEY_SERIAL = "device_serial"
    private const val KEY_FIRMWARE = "device_firmware"

    private fun prefs(context: Context) =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun environment(context: Context): W96dEnvironment {
        val raw = prefs(context).getString(KEY_ENV, W96dEnvironment.SCHOOL.name)
        return runCatching { W96dEnvironment.valueOf(raw ?: "") }
            .getOrDefault(W96dEnvironment.SCHOOL)
    }

    fun setEnvironment(context: Context, value: W96dEnvironment) {
        prefs(context).edit().putString(KEY_ENV, value.name).apply()
    }

    fun outdoor(context: Context): Boolean = prefs(context).getBoolean(KEY_OUTDOOR, false)

    fun setOutdoor(context: Context, value: Boolean) {
        prefs(context).edit().putBoolean(KEY_OUTDOOR, value).apply()
    }

    fun nightUrl(context: Context): String =
        prefs(context).getString(KEY_NIGHT_URL, "")?.trim().orEmpty()

    fun setNightUrl(context: Context, value: String) {
        prefs(context).edit().putString(KEY_NIGHT_URL, value.trim()).apply()
    }

    fun bleAddress(context: Context): String =
        prefs(context).getString(KEY_ADDRESS, "")?.trim().orEmpty()

    fun setBleAddress(context: Context, value: String) {
        prefs(context).edit().putString(KEY_ADDRESS, value.trim()).apply()
    }

    fun nodePaused(context: Context): Boolean = prefs(context).getBoolean(KEY_NODE_PAUSED, false)

    fun setNodePaused(context: Context, value: Boolean) {
        prefs(context).edit().putBoolean(KEY_NODE_PAUSED, value).apply()
    }

    fun lastSpeed(context: Context): Int =
        prefs(context).getInt(KEY_LAST_SPEED, 10).coerceIn(1, 100)

    fun setLastSpeed(context: Context, value: Int) {
        if (value in 1..100) prefs(context).edit().putInt(KEY_LAST_SPEED, value).apply()
    }

    fun serialNumber(context: Context): String? =
        prefs(context).getString(KEY_SERIAL, null)?.takeIf(String::isNotBlank)

    fun firmwareVersion(context: Context): String? =
        prefs(context).getString(KEY_FIRMWARE, null)?.takeIf(String::isNotBlank)

    fun rememberDeviceInfo(context: Context, serial: String?, firmware: String?) {
        val edit = prefs(context).edit()
        serial?.takeIf(String::isNotBlank)?.let { edit.putString(KEY_SERIAL, it) }
        firmware?.takeIf(String::isNotBlank)?.let { edit.putString(KEY_FIRMWARE, it) }
        edit.apply()
    }
}

internal fun w96dOwner(environment: W96dEnvironment, outdoor: Boolean): W96dOwner {
    @Suppress("UNUSED_VARIABLE")
    val retainedEnvironment = environment
    return if (outdoor) W96dOwner.PHONE else W96dOwner.ROUTER
}

internal fun w96dOwnerLabel(owner: W96dOwner): String = when (owner) {
    W96dOwner.ROUTER -> "室内连接"
    W96dOwner.NIGHT_NODE -> "备用连接"
    W96dOwner.PHONE -> "手机直连"
}

internal fun routerW96dBase(primaryBase: String): String {
    val normalized = LanMiHomeApi.normalize(primaryBase)
    val uri = URI(normalized)
    val host = uri.host ?: throw IllegalArgumentException("连接地址无效")
    return URI(uri.scheme, null, host, 8766, "", null, null).toString().trimEnd('/')
}

internal fun nightW96dBase(context: Context): String {
    val value = W96dPrefs.nightUrl(context)
    require(value.isNotBlank()) { "备用连接当前不可用" }
    return normalizeW96dBase(value)
}

internal fun normalizeW96dBase(raw: String): String {
    val value = raw.trim().trimEnd('/')
    require(value.startsWith("http://") || value.startsWith("https://")) {
        "连接地址格式不正确"
    }
    return value
}

internal class W96dRemoteClient(rawBase: String) {
    private val base = normalizeW96dBase(rawBase)
    private val jsonType = "application/json; charset=utf-8".toMediaType()
    private val http = OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(12, TimeUnit.SECONDS)
        .writeTimeout(8, TimeUnit.SECONDS)
        .callTimeout(16, TimeUnit.SECONDS)
        .retryOnConnectionFailure(false)
        .build()

    suspend fun state(): W96dState {
        var current = W96dState.fromJson(request("GET", "/api/v1/w96d"))
        if (current.paused) {
            resume()
            current = W96dState.fromJson(request("GET", "/api/v1/w96d"))
        }
        return current
    }

    suspend fun patch(vararg pairs: Pair<String, Any>): W96dState {
        val body = JSONObject().apply { pairs.forEach { put(it.first, it.second) } }
        return W96dState.fromJson(request("PATCH", "/api/v1/w96d", body))
    }

    suspend fun release(): JSONObject = ownership("release")
    suspend fun resume(): JSONObject = ownership("resume")

    private suspend fun ownership(state: String): JSONObject =
        request("POST", "/api/v1/w96d/ownership", JSONObject().put("state", state))

    private suspend fun request(
        method: String,
        path: String,
        body: JSONObject? = null,
    ): JSONObject = withContext(Dispatchers.IO) {
        val requestBody = (body?.toString() ?: "{}").toRequestBody(jsonType)
        val builder = Request.Builder().url(base + path).header("Accept", "application/json")
        when (method) {
            "GET" -> builder.get()
            "PATCH" -> builder.patch(requestBody)
            "POST" -> builder.post(requestBody)
            else -> error(method)
        }
        http.newCall(builder.build()).execute().use { response ->
            val text = response.body.string()
            if (!response.isSuccessful) {
                throw ApiException("设备暂时无法连接")
            }
            if (text.isBlank()) JSONObject() else JSONObject(text)
        }
    }
}
