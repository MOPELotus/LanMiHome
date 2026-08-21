package dev.lotus.lanmihome

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import java.net.Inet4Address
import java.net.URI
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject

internal data class W96DState(
    val available: Boolean,
    val connected: Boolean = false,
    val source: String = "unknown",
    val error: String? = null,
    val name: String? = null,
    val address: String? = null,
    val power: Boolean? = null,
    val gear: Int? = null,
    val speed: Int? = null,
    val naturalWind: Boolean? = null,
    val turbo: Boolean? = null,
    val turboRemainingSeconds: Int? = null,
    val light: Boolean? = null,
    val shutdownDelaySeconds: Int? = null,
    val gearSpeeds: List<Int>? = null,
    val batteryVoltage: Double? = null,
    val batteryCurrentMa: Int? = null,
    val batteryCapacityMwh: Long? = null,
    val vbusVoltage: Double? = null,
    val vbusCurrentMa: Int? = null,
    val chargeStatus: Int? = null,
    val motorCurrentMa: Int? = null,
    val motorVoltage: Double? = null,
    val motorPowerW: Double? = null,
) {
    fun toJson(): JSONObject = JSONObject().apply {
        put("available", available); put("connected", connected); put("source", source)
        putN("error", error); putN("name", name); putN("address", address)
        putN("power", power); putN("gear", gear); putN("speed", speed)
        putN("natural_wind", naturalWind); putN("turbo", turbo); putN("turbo_remaining_seconds", turboRemainingSeconds)
        putN("light", light); putN("shutdown_delay_seconds", shutdownDelaySeconds)
        put("gear_speeds", gearSpeeds?.let { JSONArray(it) } ?: JSONObject.NULL)
        putN("battery_voltage", batteryVoltage); putN("battery_current_ma", batteryCurrentMa); putN("battery_capacity_mwh", batteryCapacityMwh)
        putN("vbus_voltage", vbusVoltage); putN("vbus_current_ma", vbusCurrentMa); putN("charge_status", chargeStatus)
        putN("motor_current_ma", motorCurrentMa); putN("motor_voltage", motorVoltage); putN("motor_power_w", motorPowerW)
    }

    companion object {
        fun from(j: JSONObject) = W96DState(
            available = j.optBoolean("available", false), connected = j.optBoolean("connected", false),
            source = j.optString("source", "backend"), error = j.str("error"), name = j.str("name"), address = j.str("address"),
            power = j.bool("power"), gear = j.int("gear"), speed = j.int("speed"), naturalWind = j.bool("natural_wind"),
            turbo = j.bool("turbo"), turboRemainingSeconds = j.int("turbo_remaining_seconds"), light = j.bool("light"),
            shutdownDelaySeconds = j.int("shutdown_delay_seconds"),
            gearSpeeds = j.optJSONArray("gear_speeds")?.let { a -> List(a.length()) { a.optInt(it) } },
            batteryVoltage = j.dbl("battery_voltage"), batteryCurrentMa = j.int("battery_current_ma"), batteryCapacityMwh = j.long("battery_capacity_mwh"),
            vbusVoltage = j.dbl("vbus_voltage"), vbusCurrentMa = j.int("vbus_current_ma"), chargeStatus = j.int("charge_status"),
            motorCurrentMa = j.int("motor_current_ma"), motorVoltage = j.dbl("motor_voltage"), motorPowerW = j.dbl("motor_power_w"),
        )
    }
}

private fun JSONObject.putN(k: String, v: Any?) { put(k, v ?: JSONObject.NULL) }
private fun JSONObject.str(k: String) = if (has(k) && !isNull(k)) optString(k).takeIf { it.isNotBlank() } else null
private fun JSONObject.bool(k: String) = if (has(k) && !isNull(k)) optBoolean(k) else null
private fun JSONObject.int(k: String) = if (has(k) && !isNull(k)) optInt(k) else null
private fun JSONObject.long(k: String) = if (has(k) && !isNull(k)) optLong(k) else null
private fun JSONObject.dbl(k: String) = if (has(k) && !isNull(k)) optDouble(k) else null

internal data class W96DBackendTarget(val base: String, val network: Network? = null)

internal object W96DTargets {
    fun build(context: Context, active: String, primary: String): List<W96DBackendTarget> {
        val candidates = mutableListOf(W96DBackendTarget(active))
        if (primary != active) candidates += W96DBackendTarget(primary)
        wifiGateway(context)?.let { candidates += it }
        val out = linkedMapOf<String, W96DBackendTarget>()
        for (target in candidates) {
            val key = sidecar(target.base)
            if (out[key] == null || (out[key]?.network == null && target.network != null)) out[key] = target
        }
        return out.values.toList()
    }

    fun sidecar(base: String): String {
        val u = URI(base.trim().trimEnd('/'))
        val host = u.host ?: error("无效服务端地址：$base")
        return URI(u.scheme ?: "http", u.userInfo, host, 8766, "", null, null).toString().trimEnd('/')
    }

    private fun wifiGateway(context: Context): W96DBackendTarget? {
        val cm = context.getSystemService(ConnectivityManager::class.java) ?: return null
        for (network in cm.allNetworks.toList().sortedByDescending { it == cm.activeNetwork }) {
            val caps = cm.getNetworkCapabilities(network) ?: continue
            if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) continue
            val gateway = cm.getLinkProperties(network)?.routes?.asSequence()?.filter { it.isDefaultRoute }
                ?.mapNotNull { it.gateway as? Inet4Address }?.firstOrNull() ?: continue
            return W96DBackendTarget("http://${gateway.hostAddress}:8765", network)
        }
        return null
    }
}

internal class W96DBackendApi(target: W96DBackendTarget) {
    private val base = W96DTargets.sidecar(target.base)
    private val jsonType = "application/json; charset=utf-8".toMediaType()
    private val client = OkHttpClient.Builder().apply {
        connectTimeout(2200, TimeUnit.MILLISECONDS); readTimeout(8, TimeUnit.SECONDS); writeTimeout(8, TimeUnit.SECONDS); callTimeout(10, TimeUnit.SECONDS)
        target.network?.let { socketFactory(it.socketFactory) }
    }.build()

    suspend fun state() = W96DState.from(request("GET", "/api/v1/w96d"))
    suspend fun patch(values: Map<String, Any>) = W96DState.from(request("PATCH", "/api/v1/w96d", JSONObject().apply { values.forEach { (k,v) -> put(k,v) } }))

    private suspend fun request(method: String, path: String, body: JSONObject? = null): JSONObject = withContext(Dispatchers.IO) {
        val rb = (body?.toString() ?: "{}").toRequestBody(jsonType)
        val b = Request.Builder().url(base + path).header("Accept", "application/json")
        when (method) { "GET" -> b.get(); "PATCH" -> b.patch(rb); else -> error(method) }
        client.newCall(b.build()).execute().use { r ->
            val text = r.body.string()
            if (!r.isSuccessful) throw ApiException("W96D HTTP ${r.code}: ${runCatching { JSONObject(text).optString("error") }.getOrDefault("")}")
            if (text.isBlank()) JSONObject() else JSONObject(text)
        }
    }
}
