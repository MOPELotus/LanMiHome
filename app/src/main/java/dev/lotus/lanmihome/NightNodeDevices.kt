package dev.lotus.lanmihome

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.Inet4Address
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.ServerSocket
import java.net.Socket
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.text.SimpleDateFormat
import java.util.Collections
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import javax.crypto.Cipher
import javax.crypto.spec.IvParameterSpec
import javax.crypto.spec.SecretKeySpec

private enum class NightDevice { FAN, LAMP }

internal class NightDeviceManager(private val config: NightNodeConfig) {
    private val lock = Any()
    private var localInfo: HotspotInfo? = null
    private var fanAddress: Inet4Address? = null
    private var lampAddress: Inet4Address? = null

    private val fanRead = linkedMapOf(
        "power" to (2 to 1), "fan_level" to (2 to 2), "mode" to (2 to 3), "swing" to (2 to 4),
        "swing_angle" to (2 to 5), "off_delay_minutes" to (3 to 1), "indicator" to (4 to 1),
        "alarm" to (5 to 1), "child_lock" to (7 to 1), "speed" to (8 to 1),
    )
    private val lampRead = linkedMapOf(
        "power" to (2 to 1), "brightness" to (2 to 2), "color_temperature" to (2 to 3),
        "default_power_on_state" to (2 to 12), "on_gradient_seconds" to (2 to 13), "off_gradient_seconds" to (2 to 14),
        "mode" to (2 to 15), "delay_enabled" to (4 to 1), "delay_minutes" to (4 to 2),
        "delay_remain_minutes" to (4 to 3), "focus_enabled" to (5 to 1), "focus_minutes" to (5 to 2),
        "rest_minutes" to (5 to 3), "recycle_number" to (5 to 4),
    )

    fun setHotspotInfo(info: HotspotInfo?) = synchronized(lock) { localInfo = info }

    fun discover(force: Boolean = false) = synchronized(lock) {
        val info = localInfo ?: return@synchronized
        val client = MiioClient(info.address)
        val candidates = linkedSetOf<Inet4Address>()
        candidates += NightNetwork.neighborAddresses(info)
        candidates += client.discover(info.broadcast)
        if (force) {
            fanAddress = null
            lampAddress = null
        }

        fun tryToken(device: NightDevice, token: String) {
            if (normalizeMiioToken(token) == null) return
            if (device == NightDevice.FAN && fanAddress != null) return
            if (device == NightDevice.LAMP && lampAddress != null) return
            candidates.forEach { address ->
                if (device == NightDevice.LAMP && address == fanAddress) return@forEach
                try {
                    val values = client.getProperties(address, token, mapOf("probe" to (2 to 1)))
                    if (values.containsKey("probe")) {
                        if (device == NightDevice.FAN) fanAddress = address else lampAddress = address
                        NightNodeRuntime.log("${if (device == NightDevice.FAN) "风扇" else "台灯"}发现：${address.hostAddress}")
                        return
                    }
                } catch (_: Exception) {
                }
            }
        }

        tryToken(NightDevice.FAN, config.fanToken)
        tryToken(NightDevice.LAMP, config.lampToken)
        publishAddresses()
    }

    fun fanState(): JSONObject = synchronized(lock) {
        deviceState(NightDevice.FAN, config.fanToken, fanRead, "夜间风扇")
    }

    fun lampState(): JSONObject = synchronized(lock) {
        deviceState(NightDevice.LAMP, config.lampToken, lampRead, "夜间台灯")
    }

    private fun deviceState(device: NightDevice, token: String, mapping: Map<String, Pair<Int, Int>>, name: String): JSONObject {
        if (normalizeMiioToken(token) == null) return JSONObject().put("available", false).put("name", name).put("error", "token not configured")
        val address = ensureAddress(device) ?: return JSONObject().put("available", false).put("name", name).put("error", "device not discovered")
        return try {
            val client = MiioClient(localInfo?.address)
            val values = client.getProperties(address, token, mapping)
            val boolKeys = if (device == NightDevice.FAN) {
                setOf("power", "swing", "indicator", "alarm", "child_lock")
            } else {
                setOf("power", "delay_enabled", "focus_enabled")
            }
            JSONObject().put("available", true).put("name", name).put("ip", address.hostAddress).apply {
                values.forEach { (key, value) ->
                    val normalized = if (key in boolKeys && value != null) anyBool(value) else value
                    put(key, normalized ?: JSONObject.NULL)
                }
            }
        } catch (e: Exception) {
            clearAddress(device, "${e.javaClass.simpleName}: ${e.message}")
            JSONObject().put("available", false).put("name", name).put("ip", address.hostAddress)
                .put("error", "${e.javaClass.simpleName}: ${e.message}")
        }
    }

    fun patchFan(body: JSONObject): JSONObject = synchronized(lock) {
        val address = requireAddress(NightDevice.FAN)
        val token = requireToken(config.fanToken, "fan")
        val client = MiioClient(localInfo?.address)
        val allowed = fanRead.keys
        val keys = body.keys().asSequence().toList()
        val unknown = keys.filter { it !in allowed }
        require(unknown.isEmpty()) { "unknown fan field(s): ${unknown.joinToString()}" }

        if (body.has("power")) {
            val target = body.getBoolean("power")
            val current = client.getProperties(address, token, mapOf("power" to (2 to 1)))["power"]?.let(::anyBool)
            if (current != target) {
                client.action(address, token, 2, 1)
                SystemClock.sleep(350)
                val verified = client.getProperties(address, token, mapOf("power" to (2 to 1)))["power"]?.let(::anyBool)
                check(verified == target) { "fan power verification failed" }
            }
        }

        keys.filter { it != "power" }.forEach { key ->
            val (siid, piid) = fanRead.getValue(key)
            val value: Any = when (key) {
                "speed" -> body.getInt(key).coerceIn(1, 100)
                "fan_level" -> body.getInt(key).coerceIn(1, 4)
                "mode" -> if (body.opt(key) is String) when (body.getString(key).lowercase()) { "straight" -> 0; "natural" -> 1; else -> body.getInt(key) } else body.getInt(key).coerceIn(0, 1)
                "swing", "indicator", "alarm", "child_lock" -> body.getBoolean(key)
                "swing_angle" -> body.getInt(key).also { require(it in setOf(30, 60, 90, 120, 140)) }
                "off_delay_minutes" -> body.getInt(key).coerceIn(0, 480)
                else -> body.get(key)
            }
            client.setProperty(address, token, siid, piid, value)
        }
        fanState()
    }

    fun fanAction(body: JSONObject): JSONObject = synchronized(lock) {
        val address = requireAddress(NightDevice.FAN)
        val token = requireToken(config.fanToken, "fan")
        val action = when (val name = body.optString("name")) {
            "turn-left" -> Triple(2, 2, name)
            "turn-right" -> Triple(2, 3, name)
            "toggle-mode" -> Triple(8, 1, name)
            "loop-gear" -> Triple(8, 2, name)
            else -> throw IllegalArgumentException("unknown fan action: $name")
        }
        MiioClient(localInfo?.address).action(address, token, action.first, action.second)
        JSONObject().put("ok", true).put("action", action.third)
    }

    fun patchLamp(body: JSONObject): JSONObject = synchronized(lock) {
        val address = requireAddress(NightDevice.LAMP)
        val token = requireToken(config.lampToken, "lamp")
        val client = MiioClient(localInfo?.address)
        val writable = lampRead.keys - "delay_remain_minutes"
        val keys = body.keys().asSequence().toList()
        val unknown = keys.filter { it !in writable && it != "on_then_delayoff_minutes" }
        require(unknown.isEmpty()) { "unknown lamp field(s): ${unknown.joinToString()}" }
        keys.forEach { key ->
            if (key == "on_then_delayoff_minutes") {
                client.setProperty(address, token, 6, 1, body.getInt(key).coerceIn(0, 120))
                return@forEach
            }
            val (siid, piid) = lampRead.getValue(key)
            val value: Any = when (key) {
                "power", "delay_enabled", "focus_enabled" -> body.getBoolean(key)
                "brightness" -> body.getInt(key).coerceIn(1, 100)
                "color_temperature" -> body.getInt(key).coerceIn(2700, 5100)
                "default_power_on_state" -> body.getInt(key).coerceIn(0, 2)
                "on_gradient_seconds", "off_gradient_seconds" -> body.getDouble(key).coerceIn(0.0, 5.0)
                "mode" -> body.getInt(key).coerceIn(0, 6)
                "delay_minutes" -> body.getInt(key).coerceIn(1, 120)
                "focus_minutes", "rest_minutes" -> body.getInt(key).coerceIn(1, 60)
                "recycle_number" -> body.getInt(key).coerceIn(1, 10)
                else -> body.get(key)
            }
            client.setProperty(address, token, siid, piid, value)
        }
        lampState()
    }

    fun lampAction(body: JSONObject): JSONObject = synchronized(lock) {
        val address = requireAddress(NightDevice.LAMP)
        val token = requireToken(config.lampToken, "lamp")
        val name = body.optString("name")
        val (siid, aiid, inputPiid) = when (name) {
            "toggle" -> Triple(2, 1, null)
            "brightness-up" -> Triple(2, 2, 16)
            "brightness-down" -> Triple(2, 3, 16)
            "color-temperature-up" -> Triple(2, 4, 17)
            "color-temperature-down" -> Triple(2, 5, 17)
            "bright-circle" -> Triple(6, 1, null)
            "ct-circle" -> Triple(6, 5, null)
            else -> throw IllegalArgumentException("unknown lamp action: $name")
        }
        val input = JSONArray()
        if (inputPiid != null) {
            input.put(JSONObject().put("piid", inputPiid).put("value", body.getInt("value")))
        }
        MiioClient(localInfo?.address).action(address, token, siid, aiid, input)
        JSONObject().put("ok", true).put("action", name)
    }

    private fun ensureAddress(device: NightDevice): Inet4Address? {
        val current = if (device == NightDevice.FAN) fanAddress else lampAddress
        if (current != null) return current
        discover(false)
        return if (device == NightDevice.FAN) fanAddress else lampAddress
    }

    private fun requireAddress(device: NightDevice): Inet4Address = ensureAddress(device)
        ?: throw java.io.IOException("${if (device == NightDevice.FAN) "fan" else "lamp"} not discovered")

    private fun requireToken(token: String, label: String): String = normalizeMiioToken(token)
        ?: throw IllegalStateException("$label token not configured")

    private fun anyBool(value: Any): Boolean = when (value) {
        is Boolean -> value
        is Number -> value.toInt() != 0
        is String -> value.equals("true", true) || value == "1"
        else -> false
    }

    private fun clearAddress(device: NightDevice, error: String) {
        if (device == NightDevice.FAN) fanAddress = null else lampAddress = null
        NightNodeRuntime.update { it.copy(lastError = error) }
        publishAddresses()
    }

    private fun publishAddresses() {
        NightNodeRuntime.update {
            it.copy(fanIp = fanAddress?.hostAddress, lampIp = lampAddress?.hostAddress)
        }
    }
}
