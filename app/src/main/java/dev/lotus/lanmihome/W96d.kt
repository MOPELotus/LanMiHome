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
    val speed: Int? = null,
    val natural: Boolean? = null,
    val turbo: Boolean? = null,
    val turboRemainingSeconds: Int? = null,
    val indicator: Boolean? = null,
    val batteryVoltageMv: Int? = null,
    val batteryCurrentMa: Int? = null,
    val batteryCapacityMwh: Long? = null,
    val vbusVoltageMv: Long? = null,
    val vbusCurrentMa: Int? = null,
    val chargeStatus: Int? = null,
    val motorCurrentMa: Int? = null,
    val motorVoltageMv: Int? = null,
    val motorBlocked: Boolean? = null,
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
            speed = j.intOrNullW96d("speed"),
            natural = j.boolOrNullW96d("natural"),
            turbo = j.boolOrNullW96d("turbo"),
            turboRemainingSeconds = j.intOrNullW96d("turbo_remaining_seconds"),
            indicator = j.boolOrNullW96d("indicator"),
            batteryVoltageMv = j.intOrNullW96d("battery_voltage_mv"),
            batteryCurrentMa = j.intOrNullW96d("battery_current_ma"),
            batteryCapacityMwh = j.longOrNullW96d("battery_capacity_mwh"),
            vbusVoltageMv = j.longOrNullW96d("vbus_voltage_mv"),
            vbusCurrentMa = j.intOrNullW96d("vbus_current_ma"),
            chargeStatus = j.intOrNullW96d("charge_status"),
            motorCurrentMa = j.intOrNullW96d("motor_current_ma"),
            motorVoltageMv = j.intOrNullW96d("motor_voltage_mv"),
            motorBlocked = j.boolOrNullW96d("motor_blocked"),
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

    fun environment(context: Context): W96dEnvironment {
        val raw = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_ENV, W96dEnvironment.SCHOOL.name)
        return runCatching { W96dEnvironment.valueOf(raw ?: "") }
            .getOrDefault(W96dEnvironment.SCHOOL)
    }

    fun setEnvironment(context: Context, value: W96dEnvironment) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putString(KEY_ENV, value.name).apply()
    }

    fun outdoor(context: Context): Boolean =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getBoolean(KEY_OUTDOOR, false)

    fun setOutdoor(context: Context, value: Boolean) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putBoolean(KEY_OUTDOOR, value).apply()
    }

    // Retained only for the parked Xiaomi 10S implementation.
    fun nightUrl(context: Context): String =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_NIGHT_URL, "")?.trim().orEmpty()

    fun setNightUrl(context: Context, value: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putString(KEY_NIGHT_URL, value.trim()).apply()
    }

    fun bleAddress(context: Context): String =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_ADDRESS, "")?.trim().orEmpty()

    fun setBleAddress(context: Context, value: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putString(KEY_ADDRESS, value.trim()).apply()
    }

    // Retained for W96dNightService source compatibility while that service is disabled.
    fun nodePaused(context: Context): Boolean =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getBoolean(KEY_NODE_PAUSED, false)

    fun setNodePaused(context: Context, value: Boolean) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putBoolean(KEY_NODE_PAUSED, value).apply()
    }
}

internal fun w96dOwner(environment: W96dEnvironment, outdoor: Boolean): W96dOwner {
    // HOME and SCHOOL intentionally share the same 24h router owner now that the
    // router remains powered overnight. Keep environment persisted for future use.
    @Suppress("UNUSED_VARIABLE")
    val retainedEnvironment = environment
    return if (outdoor) W96dOwner.PHONE else W96dOwner.ROUTER
}

internal fun w96dOwnerLabel(owner: W96dOwner): String = when (owner) {
    W96dOwner.ROUTER -> "路由器"
    W96dOwner.NIGHT_NODE -> "10S 夜间节点（已停用）"
    W96dOwner.PHONE -> "本机蓝牙"
}

internal fun routerW96dBase(primaryBase: String): String {
    val normalized = LanMiHomeApi.normalize(primaryBase)
    val uri = URI(normalized)
    val host = uri.host ?: throw IllegalArgumentException("主服务端地址缺少主机名")
    return URI(uri.scheme, null, host, 8766, "", null, null).toString().trimEnd('/')
}

// Dormant compatibility helper for W96dNightService. Active UI never calls it.
internal fun nightW96dBase(context: Context): String {
    val value = W96dPrefs.nightUrl(context)
    require(value.isNotBlank()) { "10S W96D Night Node 已停用" }
    return normalizeW96dBase(value)
}

internal fun normalizeW96dBase(raw: String): String {
    val value = raw.trim().trimEnd('/')
    require(value.startsWith("http://") || value.startsWith("https://")) {
        "W96D API 地址必须以 http:// 或 https:// 开头"
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

    // A paused router means a stale OUTDOOR release. Resume this exact owner;
    // never probe or elect another node.
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
                val detail = runCatching { JSONObject(text).optString("error") }.getOrDefault("")
                throw ApiException(
                    "W96D HTTP ${response.code}${if (detail.isBlank()) "" else ": $detail"}"
                )
            }
            if (text.isBlank()) JSONObject() else JSONObject(text)
        }
    }
}
