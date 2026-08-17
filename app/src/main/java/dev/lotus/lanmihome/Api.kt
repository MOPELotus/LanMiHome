package dev.lotus.lanmihome

import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

private fun JSONObject.boolOrNull(k: String) = if (has(k) && !isNull(k)) getBoolean(k) else null
private fun JSONObject.intOrNull(k: String) = if (has(k) && !isNull(k)) getInt(k) else null
private fun JSONObject.longOrNull(k: String) = if (has(k) && !isNull(k)) getLong(k) else null
private fun JSONObject.doubleOrNull(k: String) = if (has(k) && !isNull(k)) getDouble(k) else null
private fun JSONObject.stringOrNull(k: String) = if (has(k) && !isNull(k)) getString(k).takeIf { it.isNotBlank() } else null

data class FanState(
    val available: Boolean, val error: String? = null, val name: String? = null,
    val ip: String? = null, val power: Boolean? = null, val speed: Int? = null,
    val fanLevel: Int? = null, val mode: Int? = null, val swing: Boolean? = null,
    val swingAngle: Int? = null, val offDelay: Int? = null, val indicator: Boolean? = null,
    val alarm: Boolean? = null, val childLock: Boolean? = null,
) {
    companion object { fun from(j: JSONObject) = FanState(
        available=j.optBoolean("available", true), error=j.stringOrNull("error"), name=j.stringOrNull("name"),
        ip=j.stringOrNull("ip"), power=j.boolOrNull("power"), speed=j.intOrNull("speed"),
        fanLevel=j.intOrNull("fan_level"), mode=j.intOrNull("mode"), swing=j.boolOrNull("swing"),
        swingAngle=j.intOrNull("swing_angle"), offDelay=j.intOrNull("off_delay_minutes"),
        indicator=j.boolOrNull("indicator"), alarm=j.boolOrNull("alarm"), childLock=j.boolOrNull("child_lock")
    ) }
}

data class LampState(
    val available: Boolean, val error: String? = null, val name: String? = null, val ip: String? = null,
    val power: Boolean? = null, val brightness: Int? = null, val ct: Int? = null, val mode: Int? = null,
    val defaultPower: Int? = null, val onFade: Double? = null, val offFade: Double? = null,
    val delayEnabled: Boolean? = null, val delayMinutes: Int? = null, val delayRemain: Int? = null,
    val focusEnabled: Boolean? = null, val focusMinutes: Int? = null, val restMinutes: Int? = null,
    val recycle: Int? = null,
) {
    companion object { fun from(j: JSONObject) = LampState(
        available=j.optBoolean("available", true), error=j.stringOrNull("error"), name=j.stringOrNull("name"),
        ip=j.stringOrNull("ip"), power=j.boolOrNull("power"), brightness=j.intOrNull("brightness"),
        ct=j.intOrNull("color_temperature"), mode=j.intOrNull("mode"), defaultPower=j.intOrNull("default_power_on_state"),
        onFade=j.doubleOrNull("on_gradient_seconds"), offFade=j.doubleOrNull("off_gradient_seconds"),
        delayEnabled=j.boolOrNull("delay_enabled"), delayMinutes=j.intOrNull("delay_minutes"),
        delayRemain=j.intOrNull("delay_remain_minutes"), focusEnabled=j.boolOrNull("focus_enabled"),
        focusMinutes=j.intOrNull("focus_minutes"), restMinutes=j.intOrNull("rest_minutes"), recycle=j.intOrNull("recycle_number")
    ) }
}

data class SensorState(
    val available: Boolean,
    val temperature: Double? = null,
    val humidity: Double? = null,
    val battery: Int? = null,
    val rssi: Int? = null,
    val mac: String? = null,
    val ageSeconds: Long? = null,
    val receivedAt: String? = null,
    val reports: Long = 0,
) {
    companion object { fun from(j: JSONObject) = SensorState(
        available = j.optBoolean("available", false),
        temperature = j.doubleOrNull("temperature"),
        humidity = j.doubleOrNull("humidity"),
        battery = j.intOrNull("battery"),
        rssi = j.intOrNull("rssi"),
        mac = j.stringOrNull("mac"),
        ageSeconds = j.longOrNull("age_seconds"),
        receivedAt = j.stringOrNull("received_at"),
        reports = j.optLong("reports", 0),
    ) }
}

data class SensorReport(
    val temperature: Double? = null,
    val humidity: Double? = null,
    val battery: Int? = null,
    val rssi: Int,
    val mac: String,
    val frameCounter: Int,
    val raw: String,
    val seenAtMs: Long,
)

data class RecoveryState(val active:Boolean, val success:Boolean, val attempts:Int, val reason:String?, val error:String?) {
    companion object { fun from(j: JSONObject)=RecoveryState(
        j.optBoolean("active"), j.optBoolean("success"), j.optInt("attempts"),
        j.stringOrNull("reason"), j.stringOrNull("last_error")
    ) }
}

class ApiException(message:String): IOException(message)

class LanMiHomeApi(rawBase: String) {
    val base = normalize(rawBase)
    private val jsonType = "application/json; charset=utf-8".toMediaType()
    private val client = OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS).readTimeout(16, TimeUnit.SECONDS)
        .writeTimeout(8, TimeUnit.SECONDS).callTimeout(20, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true).build()

    suspend fun fan() = FanState.from(request("GET", "/api/v1/fan"))
    suspend fun lamp() = LampState.from(request("GET", "/api/v1/lamp"))
    suspend fun sensor() = SensorState.from(request("GET", "/api/v1/sensor"))
    suspend fun recovery() = RecoveryState.from(request("GET", "/api/v1/system/recovery"))
    suspend fun patchFan(vararg pairs: Pair<String, Any>) = request("PATCH", "/api/v1/fan", obj(*pairs))
    suspend fun patchLamp(vararg pairs: Pair<String, Any>) = request("PATCH", "/api/v1/lamp", obj(*pairs))
    suspend fun fanAction(name:String) = request("POST", "/api/v1/fan/action", obj("name" to name))
    suspend fun lampAction(name:String, value:Int?=null): JSONObject = request(
        "POST", "/api/v1/lamp/action", if(value==null) obj("name" to name) else obj("name" to name, "value" to value)
    )
    suspend fun forceRecovery() = request("POST", "/api/v1/system/recovery/start", obj("force" to true))

    suspend fun reportSensor(report: SensorReport): JSONObject {
        val j = JSONObject()
            .put("model", "xiaomi.sensor_ht.mini")
            .put("product_id", SENSOR_PRODUCT_ID)
            .put("rssi", report.rssi)
            .put("mac", report.mac)
            .put("frame_counter", report.frameCounter)
            .put("raw", report.raw)
            .put("seen_at_ms", report.seenAtMs)
            .put("source", "android-ble-gateway")
        report.temperature?.let { j.put("temperature", it) }
        report.humidity?.let { j.put("humidity", it) }
        report.battery?.let { j.put("battery", it) }
        return request("POST", "/api/v1/sensor/report", j)
    }

    private fun obj(vararg pairs: Pair<String, Any>) = JSONObject().apply { pairs.forEach { put(it.first, it.second) } }

    private suspend fun request(method:String, path:String, body:JSONObject?=null): JSONObject = withContext(Dispatchers.IO) {
        val requestBody = (body?.toString() ?: "{}").toRequestBody(jsonType)
        val b = Request.Builder().url(base + path).header("Accept", "application/json")
        when(method) { "GET"->b.get(); "POST"->b.post(requestBody); "PATCH"->b.patch(requestBody); else->error(method) }
        client.newCall(b.build()).execute().use { r ->
            val text = r.body.string()
            if(!r.isSuccessful) {
                val detail = runCatching { JSONObject(text).optString("error") }.getOrDefault("")
                throw ApiException("HTTP ${r.code}${if(detail.isBlank()) "" else ": $detail"}")
            }
            if(text.isBlank()) JSONObject() else JSONObject(text)
        }
    }

    companion object {
        fun normalize(raw:String):String {
            val s=raw.trim().trimEnd('/')
            require(s.startsWith("http://") || s.startsWith("https://")) { "地址必须以 http:// 或 https:// 开头" }
            return s
        }
    }
}
